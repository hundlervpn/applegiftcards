"""Безакцептное списание периода подписки по сохранённому токену ЮKassa.

См. docs/sdd-scrum-36-recurring-payments.md
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.enums import GiftCardStatus, OrderStatus, SubscriptionStatus
from app.models.order import Order
from app.models.subscription import Subscription
from app.repositories import inventory as inventory_repo
from app.repositories import orders as orders_repo
from app.repositories import prices as prices_repo
from app.repositories import users as users_repo
from app.repositories.subscriptions import SubscriptionRepository
from app.services.alerts import notify_low_stock_task
from app.services.crypto import GiftCardCryptoError, decrypt_code
from app.services.email import send_gift_card_code_email
from app.services.order_fulfillment import GiftCardDelivery, mark_order_delivered
from app.services.payments import AcquirerError, create_recurring_payment

logger = logging.getLogger(__name__)

_BILLING_PERIOD = timedelta(days=30)


class RecurringChargeError(Exception):
    """Безакцептное списание не завершилось успешной выдачей."""


class RecurringChargeNotAllowed(RecurringChargeError):
    """Подписку нельзя списывать: статус, токен или период уже обработан."""


class RecurringPaymentDeclined(RecurringChargeError):
    """ЮKassa не подтвердила успешное списание на сумму номинала."""


class RecurringFulfillmentError(RecurringChargeError):
    """Карту нельзя выдать, запрос в ЮKassa не отправлялся.

    Дата биллинга не сдвигается.
    """


@dataclass(frozen=True)
class RecurringChargeResult:
    order_id: int
    subscription_id: int
    external_payment_id: str
    delivery: GiftCardDelivery


def recurring_idempotence_key(subscription_id: int, billing_at: datetime) -> str:
    """Ключ периода: uuid5 от id подписки и календарной даты биллинга в UTC."""
    if billing_at.tzinfo is None or billing_at.tzinfo.utcoffset(billing_at) is None:
        raise ValueError("billing date must be timezone-aware")
    billing_date = billing_at.astimezone(timezone.utc).date().isoformat()
    return str(
        uuid5(
            NAMESPACE_URL,
            f"giftcodes:yookassa:subscription:{subscription_id}:{billing_date}",
        )
    )


def _price_rub_json(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.01')):.2f}"


def _is_successful_charge(
    status: str,
    paid: bool,
    currency: str,
    amount: Decimal,
    expected: Decimal,
) -> bool:
    return (
        status == "succeeded"
        and paid is True
        and currency == "RUB"
        and amount == expected
    )


async def charge_subscription(
    session: AsyncSession,
    subscription: Subscription,
) -> RecurringChargeResult:
    """Списывает текущий период подписки и выдаёт одну карту со склада."""
    if subscription.id is None:
        raise RecurringChargeNotAllowed("subscription is not persisted")

    # Снимок до SELECT FOR UPDATE: populate_existing перезапишет тот же объект,
    # если период уже сдвинул другой воркер.
    expected_billing_at = subscription.next_billing_at
    if (
        expected_billing_at.tzinfo is None
        or expected_billing_at.tzinfo.utcoffset(expected_billing_at) is None
    ):
        raise RecurringChargeNotAllowed("billing date must be timezone-aware")

    locked = await SubscriptionRepository(session).get_for_update(subscription.id)
    if locked is None:
        raise RecurringChargeNotAllowed("subscription was not found")
    if locked.status != SubscriptionStatus.active:
        await session.rollback()
        raise RecurringChargeNotAllowed("subscription is not active")
    if locked.next_billing_at != expected_billing_at:
        await session.rollback()
        raise RecurringChargeNotAllowed("subscription billing date changed")

    token = locked.payment_method_id.strip()
    if not token:
        await session.rollback()
        raise RecurringChargeNotAllowed("payment method is missing")

    price = await prices_repo.get_by_nominal(session, locked.nominal_usd)
    if price is None:
        await session.rollback()
        raise RecurringChargeNotAllowed("nominal is not for sale")

    user = await users_repo.get_by_id(session, locked.user_id)
    if user is None or not user.email:
        await session.rollback()
        raise RecurringChargeNotAllowed("subscriber was not found")

    # Строка карты держится до commit/rollback, поэтому второй воркер её не заберёт.
    # Статус в базе меняется только вместе с оплаченным заказом.
    cards = await inventory_repo.lock_available_cards(session, locked.nominal_usd, 1)
    if len(cards) != 1:
        await session.rollback()
        raise RecurringFulfillmentError(
            "No gift card available for the subscription nominal"
        )
    card = cards[0]
    try:
        code = decrypt_code(card.code_encrypted)
    except GiftCardCryptoError as exc:
        await session.rollback()
        raise RecurringFulfillmentError(
            "Gift card code could not be decrypted"
        ) from exc

    settings = get_settings()
    idempotence_key = recurring_idempotence_key(locked.id, expected_billing_at)
    try:
        payment = await create_recurring_payment(
            shop_id=settings.yookassa_shop_id,
            secret_key=settings.yookassa_secret_key,
            amount_rub=price.price_rub,
            payment_method_id=token,
            idempotence_key=idempotence_key,
            description=f"Подписка №{locked.id}",
            metadata={
                "subscription_id": str(locked.id),
                "nominal_usd": str(locked.nominal_usd),
            },
        )
    except AcquirerError:
        await session.rollback()
        raise

    if not _is_successful_charge(
        payment.status,
        payment.paid,
        payment.currency,
        payment.amount_value,
        price.price_rub,
    ):
        logger.warning(
            "Recurring charge for subscription %s was not successful: %s %s %s",
            locked.id,
            payment.external_payment_id,
            payment.status,
            payment.currency,
        )
        await session.rollback()
        raise RecurringPaymentDeclined(
            f"YooKassa payment {payment.external_payment_id} is {payment.status}"
        )

    try:
        order = await orders_repo.add(
            session,
            Order(
                user_id=locked.user_id,
                status=OrderStatus.paid,
                items=[
                    {
                        "nominal_usd": locked.nominal_usd,
                        "quantity": 1,
                        "price_rub": _price_rub_json(price.price_rub),
                    }
                ],
                total_amount_rub=price.price_rub,
                paid_at=datetime.now(timezone.utc),
            ),
        )
        card.status = GiftCardStatus.reserved
        card.order_id = order.id
        card.sale_price_rub = price.price_rub
        await session.flush()
        card.status = GiftCardStatus.sold

        locked.next_billing_at = expected_billing_at + _BILLING_PERIOD
        locked.dunning_attempts = 0
        delivery = GiftCardDelivery(
            order_id=order.id,
            recipient_email=user.email,
            gift_card_codes=(code,),
            items=tuple(order.items),
            total_amount_rub=order.total_amount_rub,
            created_at=order.created_at,
        )
        await session.commit()
    except Exception:
        logger.warning(
            "Recurring charge for subscription %s was not fulfilled, payment %s",
            locked.id,
            payment.external_payment_id,
        )
        await session.rollback()
        raise

    logger.info(
        "Recurring charge succeeded for subscription %s, order %s, payment %s",
        locked.id,
        delivery.order_id,
        payment.external_payment_id,
    )
    enqueue_gift_card_delivery(delivery)
    return RecurringChargeResult(
        order_id=delivery.order_id,
        subscription_id=locked.id,
        external_payment_id=payment.external_payment_id,
        delivery=delivery,
    )


def enqueue_gift_card_delivery(delivery: GiftCardDelivery) -> None:
    """Ставит письмо с кодом, отметку delivered и проверку остатка.

    Тот же набор хвостовых задач, что у вебхука разовой оплаты. FastAPI сюда
    не импортируется: списание вызывает воркер, а не HTTP-роут.
    """
    loop = asyncio.get_running_loop()
    order_id = delivery.order_id
    loop.create_task(
        _run_delivery_task("email", send_gift_card_code_email(delivery)),
        name=f"gift-card-email-{order_id}",
    )
    loop.create_task(
        _run_delivery_task("delivered", mark_order_delivered(order_id)),
        name=f"gift-card-delivered-{order_id}",
    )
    loop.create_task(
        _run_delivery_task("low-stock", notify_low_stock_task()),
        name=f"gift-card-low-stock-{order_id}",
    )


async def _run_delivery_task(label: str, coro) -> None:
    try:
        await coro
    except Exception:
        logger.exception("Recurring delivery task %s failed", label)
