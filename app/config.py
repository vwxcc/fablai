from __future__ import annotations

from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    telegram_bot_token: str = ""
    telegram_webhook_url: str = ""
    public_base_url: str = ""
    telegram_webhook_secret: str = ""
    telegram_webhook_path: str = "/telegram/webhook"
    telegram_polling: bool = True

    admin_telegram_ids_raw: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")
    admin_password: str = ""
    app_secret: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/fablai.db"
    default_model: str = ""

    user_token_limit: int = Field(default=1_000_000, ge=1_000, le=100_000_000)
    user_limit_window_seconds: int = Field(default=43_200, ge=60, le=604_800)

    agent_max_steps: int = Field(default=80, ge=1, le=500)
    agent_max_runtime_seconds: int = Field(default=900, ge=10, le=7_200)
    agent_max_tool_result_chars: int = Field(default=20_000, ge=1_000, le=100_000)
    agent_max_parallel_tools: int = Field(default=5, ge=1, le=50)

    temp_upload_root: str = "/tmp/fablai-requests"
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    max_fetch_bytes: int = Field(default=1_500_000, ge=10_000, le=10_000_000)

    @property
    def admin_telegram_ids(self) -> set[int]:
        result: set[int] = set()
        for item in self.admin_telegram_ids_raw.split(","):
            try:
                if item.strip():
                    result.add(int(item.strip()))
            except ValueError:
                continue
        return result

    @property
    def webhook_path(self) -> str:
        value = "/" + self.telegram_webhook_path.strip().strip("/")
        return value or "/telegram/webhook"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
