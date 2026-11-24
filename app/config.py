from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_name: str = "ChitoProject"
    public_base_url: str = "http://127.0.0.1:8000"
    database_url: str = "sqlite+aiosqlite:///./chitoproject-dev.db"
    secret_key: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr | None = None
    bale_bot_token: SecretStr | None = None
    telegram_bot_username: str = ""
    bale_bot_username: str = ""
    uploads_dir: Path = Path("./data/uploads")
    max_upload_mb: int = 20
    max_archive_mb: int = 500
    session_ttl_hours: int = 12
    login_max_attempts: int = 5
    login_lock_minutes: int = 15
    bot_polling_timeout: int = 25
    bot_api_timeout: float = 35.0
    bot_worker_concurrency: int = 8

    @field_validator("telegram_bot_token", "bale_bot_token", mode="before")
    @classmethod
    def blank_tokens_are_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def is_production(self) -> bool:
        return self.app_env.casefold() == "production"

    @model_validator(mode="after")
    def validate_runtime_configuration(self) -> "Settings":
        if self.max_upload_mb < 1 or self.max_upload_mb > 100:
            raise ValueError("MAX_UPLOAD_MB must be between 1 and 100")
        if self.max_archive_mb < 20 or self.max_archive_mb > 10000:
            raise ValueError("MAX_ARCHIVE_MB must be between 20 and 10000")
        if not 1 <= self.bot_worker_concurrency <= 16:
            raise ValueError("BOT_WORKER_CONCURRENCY must be between 1 and 16")
        if self.is_production:
            secret = self.secret_key.get_secret_value()
            if len(secret.encode()) < 32 or secret.startswith("GENERATE_"):
                raise ValueError("SECRET_KEY must contain at least 32 random bytes in production")
            if not self.database_url.startswith("postgresql+asyncpg://"):
                raise ValueError("Production requires PostgreSQL using the asyncpg driver")
            if not self.public_base_url.startswith("https://"):
                raise ValueError("PUBLIC_BASE_URL must use HTTPS in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
