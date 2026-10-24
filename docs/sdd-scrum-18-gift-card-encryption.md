# SDD: SCRUM-18 — Симметричное шифрование кодов gift cards

**Статус:** согласовано (имена/`FERNET_KEY`/без админ-эндпоинта)  
**Стек:** Python 3.12, `cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256)

---

## 1. Контекст

`gift_cards.code_encrypted` (`bytea`) сейчас по смыслу «зашифровано», но seed писал plaintext UTF-8, а fulfillment читал `.decode("utf-8")`. Нужен единый модуль Fernet и перевод записи/чтения на него.

## 2. Цели (DoD)

1. `app/services/crypto.py`: `encrypt_code(str) -> bytes`, `decrypt_code(bytes) -> str` + unit-тесты (roundtrip и негативные кейсы).
2. `.env.example`: `FERNET_KEY` с валидным тестовым ключом; поле в `Settings`.
3. При сохранении в БД в `code_encrypted` — ciphertext (seed; админ-загрузка — позже).
4. Сырой код активации не в логах и не в HTTP-ответах API (plaintext только внутри delivery → email).
5. PR по этому SDD.

## 3. Не-цели

- Админ-эндпоинт загрузки кодов (будущий потребитель `encrypt_code`).
- Ротация ключей / MultiFernet.
- Миграция уже залитого plaintext (локально: пересидить).
- Смена типа колонки.

## 4. Решение

| Тема | Выбор |
|------|--------|
| Алгоритм | Fernet (`cryptography`) |
| Env | `FERNET_KEY` → `Settings.fernet_key: SecretStr` |
| API | `encrypt_code` / `decrypt_code` |
| Ключ пустой/битый | ошибка при вызове, не при старте app |
| Старый plaintext | без fallback |

Генерация ключа:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 5. Интеграция

| Место | Действие |
|-------|----------|
| `scripts/seed_data.py` | `encrypt_code(code)` → `code_encrypted` |
| `order_fulfillment.fulfill_verified_payment` | `decrypt_code(...)` перед `GiftCardDelivery` |
| Будущий admin upload | вызовет `encrypt_code` (не в этом PR) |

Ошибки crypto в fulfillment → `OrderFulfillmentUnavailable` (без plaintext в сообщении).

## 6. Тесты

`tests/test_crypto.py`: roundtrip; разный ciphertext при повторном encrypt; пустой plaintext; пустой/битый ключ; чужой ключ / битый token.
