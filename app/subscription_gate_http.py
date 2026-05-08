"""HTTP-шлюз: GET /sub/{token} → проверка лимита устройств → прокси на реальный URL Marzban."""

from __future__ import annotations

import hashlib
import logging

import httpx
from aiohttp import web
from sqlalchemy import func, select

from app.config import settings
from app.datetime_util import utc_now_naive
from app.db.models import Subscription, SubscriptionDevice
from app.db.session import SessionLocal

log = logging.getLogger(__name__)

_gateway_runner: web.AppRunner | None = None


def _client_fingerprint(request: web.Request) -> str:
    fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    host = fwd or (request.remote or "")
    ua = request.headers.get("User-Agent") or ""
    raw = f"{host}\0{ua}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


async def handle_subscription_gate(request: web.Request) -> web.StreamResponse:
    try:
        return await _handle_subscription_gate_impl(request)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("subscription_gate_unhandled")
        raise web.HTTPInternalServerError(
            text="Внутренняя ошибка шлюза. Смотрите логи сервиса бота."
        ) from None


async def _handle_subscription_gate_impl(request: web.Request) -> web.StreamResponse:
    token = (request.match_info.get("token") or "").strip()
    if not token:
        raise web.HTTPNotFound(text="Not found")
    fp = _client_fingerprint(request)

    upstream: str = ""
    async with SessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                select(Subscription).where(Subscription.sub_gate_token == token).with_for_update()
            )
            sub = r.scalars().one_or_none()
            if not sub or not (sub.upstream_subscription_url or "").strip():
                raise web.HTTPNotFound(text="Подписка не найдена.")
            if sub.status != "active" or sub.ends_at <= utc_now_naive():
                raise web.HTTPForbidden(text="Подписка неактивна или истекла.")
            exist = await session.scalar(
                select(SubscriptionDevice.id).where(
                    SubscriptionDevice.subscription_id == sub.id,
                    SubscriptionDevice.fingerprint_sha256 == fp,
                )
            )
            if exist is None:
                cnt = await session.scalar(
                    select(func.count())
                    .select_from(SubscriptionDevice)
                    .where(SubscriptionDevice.subscription_id == sub.id)
                )
                n = int(cnt or 0)
                if n >= int(sub.max_devices or 2):
                    raise web.HTTPForbidden(
                        text="Достигнут лимит устройств по этой ссылке.",
                    )
                session.add(
                    SubscriptionDevice(
                        subscription_id=sub.id,
                        fingerprint_sha256=fp,
                        first_seen_at=utc_now_naive(),
                    )
                )
            upstream = (sub.upstream_subscription_url or "").strip()

    headers: dict[str, str] = {}
    ua = request.headers.get("User-Agent")
    if ua:
        headers["User-Agent"] = ua
    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            resp = await client.get(upstream, headers=headers)
    except httpx.HTTPError:
        log.exception("subscription_gate_upstream_failed")
        raise web.HTTPBadGateway(text="Временно не удалось получить подписку.")

    ct = resp.headers.get("content-type", "text/plain; charset=utf-8")
    return web.Response(status=resp.status_code, body=resp.content, content_type=ct)


def create_gate_app() -> web.Application:
    app = web.Application()
    # Некоторые клиенты запрашивают URL с завершающим слэшем.
    app.router.add_get("/sub/{token}", handle_subscription_gate)
    app.router.add_get("/sub/{token}/", handle_subscription_gate)
    return app


async def start_subscription_gate_server() -> None:
    global _gateway_runner
    if not (settings.subscription_gate_public_base or "").strip():
        log.info("subscription_gate off: SUBSCRIPTION_GATE_PUBLIC_BASE empty")
        return
    if _gateway_runner is not None:
        return
    app = create_gate_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=settings.subscription_gate_listen_host,
        port=settings.subscription_gate_listen_port,
    )
    await site.start()
    _gateway_runner = runner
    log.info(
        "subscription gate listening %s:%s",
        settings.subscription_gate_listen_host,
        settings.subscription_gate_listen_port,
    )


async def stop_subscription_gate_server() -> None:
    global _gateway_runner
    if _gateway_runner is None:
        return
    await _gateway_runner.cleanup()
    _gateway_runner = None
