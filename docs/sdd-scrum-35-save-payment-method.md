# SDD: SCRUM-35 — Привязка токена карты ЮKassa и создание подписки

**Статус:** реализовано по тексту задачи (таблица `subscriptions` уже есть на main)  
**Поток:** А — рекуррент и эквайринг

См. также модель из PR с хранением подписок: `app/models/subscription.py`.

---

## 1. Контекст

Разовый заказ (`POST /api/v1/orders/one-time`) создаёт платёж ЮKassa без сохранения способа оплаты. Вебхук `payment.succeeded` переводит заказ в `paid` и выдаёт gift card. Таблица `subscriptions` уже умеет хранить `payment_method_id`, статус и `next_billing_at`, но никто её не заполняет.

## 2. Цели (DoD)

1. В запросе создания платежа при заказе подписки уходит `save_payment_method: true`.
2. Вебхук `payment.succeeded` берёт `payment_method.id` из ответа ЮKassa и пишет активную подписку.
3. Повторная доставка того же вебхука не создаёт вторую подписку.
4. Поведение разового заказа не меняется: флаг не передаётся, подписка не создаётся.

## 3. Не-цели

- Рекуррентное списание через 30 дней и dunning.
- Отдельный URL `/orders/subscription`.
- Колонка `order_id` / уникальный ключ на `subscriptions` (схему, только что влитую в main, не расширяем).
- Отмена заказа и возврат, если метод так и не сохранился.

## 4. Решение

| Тема | Выбор |
|------|--------|
| Флаг заказа | `is_subscription: bool = false` в теле `POST /api/v1/orders/one-time` |
| Клиент ЮKassa | `create_payment(..., save_payment_method: bool = False)`. `true` добавляет в JSON `save_payment_method: true` и `metadata.is_subscription = "true"`. `false` — этих полей нет |
| Откуда вебхук знает, что это подписка | Строка `metadata.is_subscription == "true"`, которую ЮKassa возвращает в GET `/payments/{id}`. Тело вебхука само по себе не источник токена |
| Токен | `payment.payment_method.id` только если `payment_method.saved is true`, после `strip`, длина 1..64 |
| Когда писать строку | В той же транзакции, что и `pending_payment → paid`, после проверки суммы, резерва и расшифровки кода |
| Поля строки | `user_id` заказа, единственный `nominal_usd` из items, `status=active`, `dunning_attempts=0`, `next_billing_at = now() + interval '30 days'` (часы БД, та же транзакция) |
| Идемпотентность | Как у выдачи карты: `SELECT … FOR UPDATE` по заказу. Повтор видит статус не `pending_payment` и выходит до INSERT. Отдельный unique не нужен: `payment_method_id` повторяется у одной карты на разные подписки |
| Нет токена или `saved != true` | Исключение до commit → HTTP 503, заказ остаётся `pending_payment`. Повтор вебхука может завершить активацию. Уже оплаченный заказ без повторного INSERT |
| Первый период | Gift card по заказу выдаётся как сейчас. Подписка — соглашение о следующем списании, а не замена выдачи |
| Чужой `payment_method` в теле вебхука | Игнорируется. Сверяются только id, status, amount, metadata. Токен берётся из GET ЮKassa |

Ключ идемпотентности ЮKassa по-прежнему `uuid5(order_id)`. Повтор создания сессии для того же заказа возвращает тот же платёж, а не второй.

## 5. Интеграция

| Место | Действие |
|-------|----------|
| `app/schemas/order.py` | Поле `is_subscription` |
| `app/api/v1/orders.py` | Прокидывает флаг в checkout |
| `app/services/order_checkout.py` | При `is_subscription` вызывает `create_payment(save_payment_method=True)` |
| `app/services/payments.py` | Кладёт флаг и metadata в тело платежа |
| `app/schemas/payment_webhook.py` | Необязательный `payment_method.id` / `saved` |
| `app/services/subscriptions.py` | Разбор флага, токена и INSERT |
| `app/services/order_fulfillment.py` | Активация подписки в транзакции выдачи |
| Роут вебхука | Без новых веток: 503 уже мапится из `OrderFulfillmentUnavailable` |

Несколько разных номиналов в одном заказе-подписке — ошибка активации (в таблице один `nominal_usd`). Текущий API создаёт одну позицию.

## 6. Тесты

Мок HTTP ЮKassa, без реальной кассы.

- Создание платежа: `save_payment_method` и `metadata.is_subscription` только при флаге.
- Checkout и `POST /orders/one-time` прокидывают флаг; без флага — `false` и прежний JSON.
- GET платежа, а не тело вебхука, отдаёт `payment_method.id`.
- `payment.succeeded` пишет одну активную подписку с `next_billing_at` ровно на 30 суток.
- Повтор вебхука — та же строка.
- Разовый успешный платёж с `payment_method` подписку не создаёт.
- Подписка без сохранённого метода — 503, заказ `pending_payment`, строк 0.
