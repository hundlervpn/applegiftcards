from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AWE"
    debug: bool = False
    database_url: str

    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    payment_return_url: str = "http://localhost:8000/health"

    smtp_host: str = "sandbox.smtp.mailtrap.io"
    smtp_port: int = 2525
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from_email: str = "no-reply@example.com"
    smtp_starttls: bool = True
    smtp_use_tls: bool = False
    smtp_timeout_seconds: float = 10.0

    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""
    low_stock_threshold: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
