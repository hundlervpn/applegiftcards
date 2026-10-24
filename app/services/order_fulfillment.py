import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session_maker
from app.models.enums import GiftCardStatus, OrderStatus
from app.repositories import inventory as inventory_repo
from app.repositories import orders as orders_repo
from app.repositories import users as users_repo
from app.schemas.payment_webhook import VerifiedPayment
from app.services.crypto import GiftCardCryptoError, decrypt_code
from app.services.inventory import InvalidOrderItemsError, get_card_plan
from app.services.subscriptions import (
    SubscriptionActivationError,
    activate_subscription,
    is_subscription_payment,
    saved_payment_method_id,
)

logger = logging.getLogger(__name__)
MAX_ORDER_ID = 2**63 - 1


@dataclass(frozen=True)
class GiftCardDelivery:
    order_id: int
    recipient_email: str
    gift_card_codes: tuple[str, ...]
    items: tuple[dict, ...] = ()
    total_amount_rub: Decimal | None = None
    created_at: datetime | None = None


class OrderFulfillmentUnavailable(Exception):
    pass


def _get_order_id(payment: VerifiedPayment) -> int | None:
    value = payment.metadata.get("order_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= MAX_ORDER_ID else None
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return None
    if len(value) > 19:
        return None
    order_id = int(value)
    return order_id if 0 < order_id <= MAX_ORDER_ID else None


async def fulfill_verified_payment(
    session: AsyncSession,
    payment: VerifiedPayment,
) -> GiftCardDelivery | None:
    if payment.status != "succeeded":
        return None

    order_id = _get_order_id(payment)
    if order_id is None:
        logger.warning("Verified YooKassa payment has no valid order_id metadata")
        return None

    order = await orders_repo.get_for_update(session, order_id)
    if order is None:
        logger.warning("Verified YooKassa payment refers to an unknown order")
        return None
    if order.status != OrderStatus.pending_payment:
        return None

    if payment.amount.currency != "RUB" or payment.amount.value != order.total_amount_rub:
        logger.warning("Verified YooKassa payment amount does not match the order")
        return None

    try:
        expected_cards = Counter(
            {
                nominal: len(prices)
                for nominal, prices in get_card_plan(order.items).items()
            }
        )
    except InvalidOrderItemsError as exc:
        raise OrderFulfillmentUnavailable(
            "Order items cannot be used for gift card delivery"
        ) from exc

    cards = await inventory_repo.lock_reserved_cards_for_order(session, order.id)
    actual_cards = Counter(card.nominal_usd for card in cards)
    if not cards or actual_cards != expected_cards:
        raise OrderFulfillmentUnavailable(
            "Reserved gift cards do not match the order"
        )

    user = await users_repo.get_by_id(session, order.user_id)
    if user is None:
        raise OrderFulfillmentUnavailable("Order owner was not found")

    try:
        gift_card_codes = tuple(
            decrypt_code(card.code_encrypted) for card in cards
        )
    except GiftCardCryptoError as exc:
        raise OrderFulfillmentUnavailable(
            "Reserved gift card code could not be decrypted"
        ) from exc

    if is_subscription_payment(payment):
        # Та же транзакция, что и pending_payment → paid. Повтор вебхука
        # видит заказ уже не pending и сюда не доходит.
        nominals = list(expected_cards)
        if len(nominals) != 1:
            logger.warning("Subscription payment does not have a single nominal")
            raise OrderFulfillmentUnavailable(
                "Subscription order must contain a single nominal"
            )
        payment_method_id = saved_payment_method_id(payment)
        if payment_method_id is None:
            logger.warning("Subscription payment has no saved payment method")
            raise OrderFulfillmentUnavailable(
                "Subscription payment has no saved payment method"
            )
        try:
            await activate_subscription(
                session,
                user_id=order.user_id,
                nominal_usd=nominals[0],
                payment_method_id=payment_method_id,
            )
        except SubscriptionActivationError as exc:
            raise OrderFulfillmentUnavailable(
                "Subscription could not be activated"
            ) from exc

    for card in cards:
        card.status = GiftCardStatus.sold
    order.status = OrderStatus.paid
    order.paid_at = datetime.now(timezone.utc)
    await session.commit()

    return GiftCardDelivery(
        order_id=order.id,
        recipient_email=user.email,
        gift_card_codes=gift_card_codes,
        items=tuple(order.items),
        total_amount_rub=order.total_amount_rub,
        created_at=order.created_at,
    )


async def mark_order_delivered(order_id: int) -> None:
    async with get_session_maker()() as session:
        order = await orders_repo.get_for_update(session, order_id)
        if order is None or order.status != OrderStatus.paid:
            return
        order.status = OrderStatus.delivered
        order.email_sent_at = datetime.now(timezone.utc)
        await session.commit()
