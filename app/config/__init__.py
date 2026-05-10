"""
config – Centralised settings loaded from environment variables / .env file.

All other modules import from here instead of reading os.environ directly.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings resolved from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── PostgreSQL ─────────────────────────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "domainflip"
    postgres_user: str = "domainflip"
    postgres_password: str = "changeme"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ── Redis ──────────────────────────────────────────────────────────────────
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # ── WhoisXML API ───────────────────────────────────────────────────────────
    whoisxml_api_key: str = ""
    whoisxml_api_url: str = "https://www.whoisxmlapi.com/whoisserver/WhoisService"

    # ── Domain valuation API ──────────────────────────────────────────────────
    valuation_api_key: str = ""
    valuation_api_url: str = "https://api.estibot.com/v1/valuation"
    valuation_min_usd: float = 500.0

    # ── Registrar APIs ─────────────────────────────────────────────────────────
    dynadot_api_key: str = ""
    namejet_api_key: str = ""
    namejet_api_secret: str = ""
    registrar_proxy_url: str = ""
    registrar_cooldown_seconds: int = 60

    # ── Notifications ─────────────────────────────────────────────────────────
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notification_timeout_seconds: int = 10

    # ── Monitor tuning ─────────────────────────────────────────────────────────
    monitor_check_interval_seconds: int = 30
    monitor_max_workers: int = 10
    monitor_concurrency: int = 100
    monitor_request_timeout_seconds: int = 10

    # ── Sniper timing ──────────────────────────────────────────────────────────
    sniper_lead_time_seconds: int = 120
    sniper_poll_interval_ms: int = 500
    sniper_dry_run: bool = False


# Module-level singleton – import this in other modules.
settings = Settings()
