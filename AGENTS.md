# AWE — agent context

Backend магазина Apple Gift Cards (команда SchoolX, осень 2026).

## Stack

Python 3.12+, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2 (async) + asyncpg, PostgreSQL, uvicorn.

## Architecture (layered)

```
Client → api/ (HTTP, thin routes) → services/ (business logic) → repositories/ (DB) → PostgreSQL
                ↕
           schemas/ (DTO in/out)
```

- `app/main.py` — точка входа, только `FastAPI()` + `include_router`
- `app/dependencies.py` — DI-фабрики (`Depends`, `@lru_cache` для settings)
- `app/config.py` — `Settings` из `.env`
- `app/db/session.py` — `create_async_engine`, `async_sessionmaker`, `get_db`
- Бизнес-API — под `/v1/...` (версионирование с первого дня)
- Роуты тонкие: валидация/статусы HTTP; логика — в сервисах; сервисы без импортов FastAPI
- Ошибки сервиса → исключения Python; роут переводит в HTTP-статусы

## Project-specific

- Схема БД: `docs/schema.sql` / `docs/schema.dbml` (users, orders, gift_cards, prices)
- YooKassa-клиент: `app/services/payments.py`
- Секреты только в `.env`, не коммитить

## Conventions

- `api/v1/tasks.py` — роуты (мн. число); `schemas/task.py` — модель (ед. число)
- Абсолютные импорты: `from app.schemas.task import ...`
- DTO раздельно: Create / Update / Response — не одна модель на всё

### Деньги (RUB)

- Везде **копейки**: `int`, без `float` / `Decimal` в API, сервисах и БД (`BIGINT`, суффикс поля `*_kopecks`, напр. `amount_kopecks`, `price_kopecks`).
- В JSON и Pydantic — целое число; для валидации DTO можно `from app.money import Kopecks`.
- Перевод в рубли со строкой `"123.45"` — только на **границах** с внешними системами (YooKassa): `app.money.kopecks_to_yookassa_rub`.
