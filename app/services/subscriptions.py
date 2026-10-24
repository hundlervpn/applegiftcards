"""Активация подписки после оплаты с сохранённым методом ЮKassa.

См. docs/sdd-scrum-35-save-payment-method.md
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SubscriptionStatus
from app.models.subscription import Subscription
from app.repositories.subscriptions import SubscriptionRepository
from app.schemas.payment_webhook import VerifiedPayment

_NEXT_BILLING_AT_SQL = "SELECT now() + interval '30 days'"
_MAX_PAYMENT_METHOD_ID_LENGTH = 64


class SubscriptionActivationError(Exception):
    """Подписку нельзя записать в текущей транзакции оплаты."""


def is_subscription_payment(payment: VerifiedPayment) -> bool:
    value = payment.metadata.get("is_subscription")
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


def saved_payment_method_id(payment: VerifiedPayment) -> str | None:
    method = payment.payment_method
    if method is None or method.saved is not True or not isinstance(method.id, str):
        return None
    token = method.id.strip()
    if not token or len(token) > _MAX_PAYMENT_METHOD_ID_LENGTH:
        return None
    return token


async def activate_subscription(
    session: AsyncSession,
    *,
    user_id: int,
    nominal_usd: int,
    payment_method_id: str,
) -> Subscription:
    next_billing_at = await session.scalar(text(_NEXT_BILLING_AT_SQL))
    if next_billing_at is None:
        raise SubscriptionActivationError("Could not compute the next billing date")

    subscription = Subscription(
        user_id=user_id,
        nominal_usd=nominal_usd,
        payment_method_id=payment_method_id,
        status=SubscriptionStatus.active,
        next_billing_at=next_billing_at,
        dunning_attempts=0,
    )
    return await SubscriptionRepository(session).add(subscription)
