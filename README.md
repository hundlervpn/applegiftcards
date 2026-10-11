# AWE

Backend магазина Apple Gift Cards.

## Локальный запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
docker compose up -d
.venv/bin/uvicorn app.main:app --reload
```

Проверка: [http://localhost:8000/health](http://localhost:8000/health) — при работающей БД в ответе `"database": "ok"`.

Остановить PostgreSQL: `docker compose down` (данные сохраняются в volume `postgres_data`).
