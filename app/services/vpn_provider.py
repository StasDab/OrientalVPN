from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse

import httpx

from app.services.node_registry import marzban_provision_options
from app.services.server_selector import VpnNode


def _public_subscription_url(raw: str, panel_base: str) -> str:
    """Подменить localhost/127.0.0.1 в ссылке подписки на публичный хост панели (как в браузере)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    panel_base = panel_base.rstrip("/")
    base = panel_base if "://" in panel_base else f"https://{panel_base}"
    try:
        pb = urlparse(base)
        p = urlparse(raw)
    except ValueError:
        return raw
    host = (p.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return urlunparse((pb.scheme, pb.netloc, p.path, p.params, p.query, p.fragment))
    return raw


def _marzban_require_ok(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        body = (response.text or "")[:4000]
    except Exception:
        body = "<no body>"
    raise RuntimeError(
        f"Marzban API {response.status_code} {response.request.method} {response.url}: {body}"
    )


@dataclass
class ProvisionResult:
    external_user_id: str
    subscription_url: str
    ends_at: datetime


class MarzbanAdapter:
    def __init__(self, panel_url: str, username: str, password: str) -> None:
        self.panel_url = panel_url.rstrip("/")
        self.username = username
        self.password = password
        self._token: str | None = None

    async def _get_token(self) -> str:
        if self._token:
            return self._token
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.panel_url}/api/admin/token",
                data={"username": self.username, "password": self.password},
            )
            _marzban_require_ok(response)
            data = response.json()
            token = data.get("access_token")
            if not token:
                raise RuntimeError("Marzban token not found in response")
            self._token = token
            return token

    async def provision_access(
        self,
        tg_id: int,
        location_code: str,
        *,
        node: VpnNode | None = None,
        days: int | None = None,
        hours: int | None = None,
    ) -> ProvisionResult:
        token = await self._get_token()
        username = f"tg_{tg_id}"
        if hours is not None:
            delta = timedelta(hours=hours)
        elif days is not None:
            delta = timedelta(days=days)
        else:
            delta = timedelta(days=30)
        expire_at = int((datetime.utcnow() + delta).timestamp())
        inbound_tag, vless_settings = marzban_provision_options(node, location_code)

        # Marzban UserCreate: proxies не пустой; inbounds — теги как в панели (Core / Xray).
        payload = {
            "username": username,
            "status": "active",
            "expire": expire_at,
            "note": f"Telegram user {tg_id}",
            "proxies": {"vless": vless_settings},
            "inbounds": {"vless": [inbound_tag]},
            "on_hold_timeout": 0,
            "on_hold_expire_duration": 0,
            "data_limit": 0,
            "data_limit_reset_strategy": "no_reset",
        }

        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.panel_url}/api/user", json=payload, headers=headers)
            if response.status_code == 401:
                self._token = None
                token = await self._get_token()
                headers = {"Authorization": f"Bearer {token}"}
                response = await client.post(f"{self.panel_url}/api/user", json=payload, headers=headers)
            if response.status_code == 409:
                # Пользователь tg_* уже есть (обрыв прошлой попытки). PUT только expire/status
                # на части инсталляций Marzban даёт 500 — надёжнее удалить и создать заново.
                del_r = await client.delete(
                    f"{self.panel_url}/api/user/{username}",
                    headers=headers,
                )
                if del_r.status_code not in (200, 404):
                    _marzban_require_ok(del_r)
                response = await client.post(
                    f"{self.panel_url}/api/user", json=payload, headers=headers
                )
                if response.status_code == 401:
                    self._token = None
                    token = await self._get_token()
                    headers = {"Authorization": f"Bearer {token}"}
                    response = await client.post(
                        f"{self.panel_url}/api/user", json=payload, headers=headers
                    )
                _marzban_require_ok(response)
                user_data = response.json()
            else:
                _marzban_require_ok(response)
                user_data = response.json()

            # Полная ссылка с токеном и правильным хостом (POST иногда без subscription_url или с 127.0.0.1).
            get_r = await client.get(
                f"{self.panel_url}/api/user/{username}",
                headers=headers,
            )
            _marzban_require_ok(get_r)
            user_data = get_r.json()

        subscription_url = _public_subscription_url(
            (user_data.get("subscription_url") or "").strip(),
            self.panel_url,
        )
        if not subscription_url:
            raise RuntimeError(
                "Marzban не вернул subscription_url. На сервере Marzban в его .env задайте "
                "XRAY_SUBSCRIPTION_URL_PREFIX=https://ваш-домен-панели (без /dashboard/), "
                "перезапустите Marzban и снова выдайте подписку."
            )
        return ProvisionResult(
            external_user_id=username,
            subscription_url=subscription_url,
            ends_at=datetime.utcfromtimestamp(expire_at),
        )

    async def set_expire(self, external_user_id: str, expire_at_ts: int) -> None:
        token = await self._get_token()
        payload = {"expire": expire_at_ts, "status": "active"}
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.put(
                f"{self.panel_url}/api/user/{external_user_id}",
                json=payload,
                headers=headers,
            )
            _marzban_require_ok(response)

    async def disable_access(self, external_user_id: str) -> None:
        token = await self._get_token()
        payload = {"status": "disabled"}
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.put(
                f"{self.panel_url}/api/user/{external_user_id}",
                json=payload,
                headers=headers,
            )
            _marzban_require_ok(response)
