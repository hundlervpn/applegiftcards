# SDD: SCRUM-36 — Безакцептное автосписание по сохранённому токену

**Статус:** реализовано по тексту задачи  
**Поток:** А — рекуррент и эквайринг

> Отказ кассы с SCRUM-38 больше не оставляет дату и `dunning_attempts` как были: подписка уходит в dunning. См. `docs/sdd-scrum-38-dunning-management.md`. Успешное списание ниже не менялось, кроме возврата `past_due` в `active`.

См. активацию подписки: `docs/sdd-scrum-35-save-payment-method.md`.

---

## 1. Контекст

Первый платёж подписки сохраняет `payment_method_id` и ставит `next_billing_at = now() + 30 days`. Дальше касса должна списывать тот же номинал без редиректа покупателя. Таблица `subscriptions` и выборка `list_active_due_for_billing` уже есть. Нет сервиса, который по токену создаёт платёж ЮKassa, заказ и выдачу карты.

## 2. Цели (DoD)

1. `charge_subscription(session, subscription)` делает `POST /v3/payments` без участия пользователя: `payment_method_id`, `capture=true`, заголовок `Idempotence-Key`.
2. При успехе создаётся заказ `paid`, со склада берётся одна карта (`FOR UPDATE SKIP LOCKED`), переводится в `sold`, ставится задача письма с кодом.
3. `next_billing_at` сдвигается на +30 дней от текущей даты биллинга, `dunning_attempts` становится 0.
4. Тесты на моке HTTP ЮKassa, без реальной кассы.

## 3. Не-цели

- Планировщик, который сам обходит due-подписки. Выборка уже есть, этот сервис вызывают с конкретной строкой.
- Dunning: перевод в `past_due`, рост `dunning_attempts`, паузы между попытками.
- Новый URL, колонки и миграции. Идентификатор платежа ЮKassa в заказе не хранится.
- Обрыв процесса после ответа `succeeded` и до `commit`. Повтор в течение суток попадает в тот же Idempotence-Key. Отдельные inflight-поля под это не заводим.

## 4. Решение

| Тема | Выбор |
|------|--------|
| Оркестрация | `app/services/recurring_payments.py`. HTTP остаётся в клиенте `app/services/payments.py` (`create_recurring_payment`) |
| Сигнатура | `charge_subscription(session, subscription) -> RecurringChargeResult`. Настройки ЮKassa — `get_settings()` |
| Кого списываем | Только `status=active` и непустой `payment_method_id`. `past_due` и `cancelled` — ошибка до HTTP |
| Сумма | Текущая `prices.price_rub` номинала подписки, строка с копейками через `decimal_to_yookassa_rub` |
| Тело платежа | `amount`, `capture: true`, `payment_method_id`, `description`, `metadata`. Без `confirmation` и без `save_payment_method` — покупатель не в браузере |
| Metadata | `subscription_id`, `nominal_usd` строками. `order_id` нет: заказа ещё нет, и вебхук `payment.succeeded` без `order_id` выходит до выдачи. Повторной выдачи из вебхука не будет |
| Idempotence-Key | `uuid5(NAMESPACE_URL, "giftcodes:yookassa:subscription:{id}:{YYYY-MM-DD}")`. Дата — календарный день `next_billing_at` в UTC. Повтор того же периода бьёт в тот же ключ ЮKassa |
| Успех кассы | HTTP 2xx, `status=succeeded`, `paid=true`, валюта `RUB`, сумма равна цене. Иначе заказ не создаётся, дата биллинга не двигается |
| Заказ | Новый `orders`: `user_id` подписчика, одна позиция номинала, `status=paid`, `paid_at=now()`, `total_amount_rub` = цена |
| Склад до кассы | `lock_available_cards` (`FOR UPDATE SKIP LOCKED`) и расшифровка кода до `POST`. Блокировка строки держится до конца транзакции. Статус карты в базе в этот момент ещё `available` |
| Нет карты или код не читается | `rollback`, в ЮKassa запрос не уходит, дата биллинга не двигается |
| Заказ и склад после успеха | В той же транзакции: заказ сразу `paid`, карта `reserved` с `order_id` и `sale_price_rub`, затем `sold`. Один `commit` вместе с сдвигом даты. Отказ кассы происходит до записи заказа, карта остаётся `available` |
| Подписка | После успешной кассы: `next_billing_at = прежняя + 30 суток`, `dunning_attempts = 0`. Сдвиг от даты периода, а не от `now()`, чтобы опоздание джоба не съезжало календарь |
| Блокировка строки | `SELECT … FOR UPDATE` с `populate_existing` до HTTP. В ключ и в сравнение берётся снимок `next_billing_at` до обновления identity map. Если в БД дата уже другая — второй воркер этот период не списывает |
| Письмо | После commit `enqueue_gift_card_delivery`: задачи `send_gift_card_code_email`, `mark_order_delivered` и `notify_low_stock_task`. Сервис не импортирует FastAPI. Ошибка SMTP не откатывает списание |
| Транзакция | Успех — `commit`. Любой отказ — `rollback`. В сессии не должно быть чужой незакоммиченной работы |

Ключ периода не включает `order_id`: заказ появляется только после `succeeded`.

## 5. Интеграция

| Место | Действие |
|-------|----------|
| `app/services/payments.py` | `create_recurring_payment` и разбор ответа без `confirmation_url` |
| `app/services/recurring_payments.py` | Списание, заказ, склад, сдвиг даты, очередь письма |
| `app/repositories/subscriptions.py` | `get_for_update` |
| Роуты, схема БД | Без изменений |

## 6. Тесты

Мок `httpx`, база `awe_test`.

- Ключ: uuid5 от id и UTC-даты, тот же календарный день в другой зоне, другой id или день — другой ключ.
- POST: URL `/v3/payments`, Basic auth, `capture`, `payment_method_id`, без `confirmation`, заголовок равен ключу периода.
- Успех: заказ `paid`, карта `sold`, письмо с email и кодом, `next_billing_at` ровно +30 дней, `dunning_attempts=0`.
- Отказ кассы и неверная сумма: заказа нет, карта `available`, дата и dunning прежние, письма нет.
- Пустой склад и битый код: исключения до HTTP, запроса в ЮKassa нет, дата не сдвигается.
- Неактивная подписка и уже сдвинутая другим воркером дата: HTTP нет.
