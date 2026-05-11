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


def _client_ip_for_gate(request: web.Request) -> str:
    """Первый hop из X-Forwarded-For (nginx) либо адрес сокета."""
    fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return fwd or (request.remote or "")


def _client_fingerprint(request: web.Request) -> str:
    """
    Идентификация «устройства» для лимита.

    По умолчанию только IP: клиенты вроде Happ меняют User-Agent между запросами,
    из-за чего каждое обновление подписки выглядело как новое устройство → 403 и нет
    заголовка subscription-userinfo (трафик/лимит в UI).
    SUBSCRIPTION_GATE_FINGERPRINT_USE_UA=true — прежняя схема IP+UA (строже к раздаче ссылки).
    """
    ip = _client_ip_for_gate(request).strip()
    if settings.subscription_gate_fingerprint_use_ua:
        ua = request.headers.get("User-Agent") or ""
        raw = f"{ip}\0{ua}".encode("utf-8", errors="replace")
    elif ip:
        raw = ip.encode("utf-8", errors="replace")
    else:
        # Нет IP в запросе (редко) — не схлопываем всех в один хеш.
        ua = request.headers.get("User-Agent") or ""
        raw = f"\0{ua}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def _subscription_body_plaintext_uri_list(raw: bytes) -> bool:
    """
    Директивы Happ (#profile-...) добавлять только перед классической plaintext-подпиской со схемами URI.
    Если вставить их перед JSON/YAML/Clash, клиент может выдать «нет ссылок» (Happ error 39).
    """
    stem = raw.lstrip(b"\xef\xbb\xbf").lstrip()
    if not stem:
        return False
    if stem.startswith(
        (
            b"{",
            b"[",
            b"port:",
            b"proxies:",
            b"proxy-groups:",
            b"%YAML",
            b"rules:",
            b"mixed-port:",
        )
    ):
        return False
    window = stem[:8192]
    return bool(
        window.startswith(
            (
                b"vless://",
                b"vmess://",
                b"ss://",
                b"trojan://",
                b"hysteria2://",
                b"hy2://",
                b"#",
            )
        )
        or b"vless://" in window
        or b"vmess://" in window
        or (b"hysteria2://" in window or b"hy2://" in window or b"trojan://" in window or b"ss://" in window)
    )


def _happ_directive_prefix_for_body(upstream_body: bytes) -> bytes:
    """Строки в начале plaintext-подписки для Happ (автообновление и имя профиля)."""
    hrs = int(settings.subscription_happ_profile_update_hours or 0)
    title = (settings.subscription_happ_profile_title or "").strip()
    probe = upstream_body.lstrip(b"\xef\xbb\xbf")[:8192].decode("utf-8", errors="replace").lower()
    lines: list[str] = []
    if hrs >= 1 and "profile-update-interval" not in probe:
        lines.append(f"#profile-update-interval: {hrs}")
    if title and "profile-title" not in probe:
        lines.append(f"#profile-title: {title}")
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


def _split_content_type_for_aiohttp(raw: str | None) -> tuple[str, str | None]:
    """
    aiohttp.web.Response не принимает charset внутри content_type (ValueError).
    Разбираем заголовок upstream, например text/plain; charset=utf-8.
    """
    s = (raw or "").strip() or "text/plain; charset=utf-8"
    parts = [p.strip() for p in s.split(";") if p.strip()]
    if not parts:
        return "text/plain", "utf-8"
    mime = parts[0]
    charset: str | None = None
    for p in parts[1:]:
        if p.lower().startswith("charset="):
            charset = p.split("=", 1)[1].strip().strip("'\"")
    return mime, charset


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
                # Единый источник для шлюза: SUBSCRIPTION_MAX_DEVICES в .env (поле subscriptions.max_devices
                # обновляется при выдаче подписки, но на доступ по ссылке не влияет).
                max_dev = int(settings.subscription_max_devices)
                if n >= max_dev:
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

    mime, charset = _split_content_type_for_aiohttp(resp.headers.get("content-type"))
    body = resp.content
    prefix = _happ_directive_prefix_for_body(body)
    if prefix and _subscription_body_plaintext_uri_list(body):
        body = prefix + body

    # Заголовки Marzban для клиентов (Happ, v2raytun и т.д.): трафик, срок, название профиля.
    # httpx сопоставляет имена без учёта регистра.
    _SUBSCRIPTION_PASS_THROUGH_HEADERS = (
        "subscription-userinfo",
        "support-url",
        "profile-title",
        "profile-update-interval",
        "profile-web-page-url",
        "announcement",
    )
    extra: dict[str, str] = {}
    for hk in _SUBSCRIPTION_PASS_THROUGH_HEADERS:
        hv = resp.headers.get(hk)
        if hv:
            extra[hk] = hv

    return web.Response(
        status=resp.status_code,
        body=body,
        content_type=mime,
        charset=charset,
        **({"headers": extra} if extra else {}),
    )


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
