from functools import lru_cache
from ipaddress import ip_network

from pydantic import SecretStr, field_validator
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

    # Per-IP quotas (slowapi / limits). One counter per API process.
    rate_limit_enabled: bool = True
    auth_register_rate_limit: str = "3/minute"
    auth_login_rate_limit: str = "5/minute"
    order_create_rate_limit: str = "10/minute"
    # "*" trusts every direct peer. Otherwise a comma-separated list of IPs or CIDRs.
    # Loopback and RFC1918 cover Nginx on the host in front of the container port.
    trusted_proxies: str = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

    @field_validator(
        "auth_register_rate_limit",
        "auth_login_rate_limit",
        "order_create_rate_limit",
    )
    @classmethod
    def _valid_rate_limit(cls, value: str) -> str:
        from limits import parse

        parse(value)
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def _valid_trusted_proxies(cls, value: str) -> str:
        if value.strip() == "*":
            return "*"
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            if "/" not in part:
                part = f"{part}/128" if ":" in part else f"{part}/32"
            ip_network(part, strict=False)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
