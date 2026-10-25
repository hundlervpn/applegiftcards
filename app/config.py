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
    admin_api_token: SecretStr = SecretStr("")

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
    billing_scheduler_interval_seconds: int = 600
    # Absolute http(s) link in the dunning email. Blank or unsafe values fall back.
    card_update_url: str = "http://localhost:8000/account/payment-method"

    jwt_secret_key: str = "dev-insecure-jwt-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    # Fernet key (url-safe base64 of 32 bytes). Empty → encrypt/decrypt fail at use.
    fernet_key: SecretStr = SecretStr("")


@lru_cache
def get_settings() -> Settings:
    return Settings()
