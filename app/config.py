from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
import json


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(alias="BOT_TOKEN")
    provider_token: str = Field(default="", alias="PROVIDER_TOKEN")
    payments_provider_name: str = Field(default="telegram-payments", alias="PAYMENTS_PROVIDER_NAME")
    admin_ids: str = Field(default="", alias="ADMIN_IDS")

    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")

    vpn_provider: str = Field(default="marzban", alias="VPN_PROVIDER")
    panel_url: str = Field(alias="PANEL_URL")
    panel_username: str = Field(alias="PANEL_USERNAME")
    panel_password: str = Field(alias="PANEL_PASSWORD")

    trial_hours: int = Field(default=3, alias="TRIAL_HOURS")
    check_interval_minutes: int = Field(default=10, alias="CHECK_INTERVAL_MINUTES")
    provision_retries: int = Field(default=3, alias="PROVISION_RETRIES")
    event_max_retries: int = Field(default=30, alias="EVENT_MAX_RETRIES")
    reminder_hours_before: int = Field(default=24, alias="REMINDER_HOURS_BEFORE")
    vpn_nodes_json: str = Field(default="[]", alias="VPN_NODES_JSON")
    # Для VLESS REALITY + Vision в Marzban часто нужно: xtls-rprx-vision
    marzban_vless_flow: str = Field(default="", alias="MARZBAN_VLESS_FLOW")

    @property
    def admin_id_set(self) -> set[int]:
        return {int(v.strip()) for v in self.admin_ids.split(",") if v.strip()}

    @property
    def vpn_nodes(self) -> list[dict]:
        try:
            data = json.loads(self.vpn_nodes_json)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


settings = Settings()
