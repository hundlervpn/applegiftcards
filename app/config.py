from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "AWE"
    debug: bool = False
    database_url: str

    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    payment_return_url: str = "http://localhost:8000/health"


@lru_cache
def get_settings() -> Settings:
    return Settings()
