from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx


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
            response.raise_for_status()
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
        inbound_tag = f"loc-{location_code.lower()}"

        # Marzban UserCreate требует непустой proxies (см. validate_proxies в User).
        payload = {
            "username": username,
            "status": "active",
            "expire": expire_at,
            "note": f"Telegram user {tg_id}",
            "proxies": {"vless": {}},
            "inbounds": {"vless": [inbound_tag]},
            "on_hold_timeout": 0,
            "on_hold_expire_duration": 0,
            "data_limit": 0,
            "data_limit_reset_strategy": "no_reset",
        }

        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{self.panel_url}/api/user", json=payload, headers=headers)
            if response.status_code == 409:
                patch_payload = {"expire": expire_at, "status": "active"}
                patch = await client.put(
                    f"{self.panel_url}/api/user/{username}",
                    json=patch_payload,
                    headers=headers,
                )
                patch.raise_for_status()
                user_data = patch.json()
            else:
                response.raise_for_status()
                user_data = response.json()

        subscription_url = user_data.get("subscription_url", "")
        if not subscription_url:
            subscription_url = f"{self.panel_url}/sub/{username}"
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
            response.raise_for_status()

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
            response.raise_for_status()
