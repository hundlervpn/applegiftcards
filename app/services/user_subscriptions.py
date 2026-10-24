import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.repositories.subscriptions import SubscriptionRepository
from app.schemas.me_subscription import SubscriptionItem
from app.services.payment_methods import get_card_masks

logger = logging.getLogger(__name__)


class UserSubscriptionsUnavailable(Exception):
    pass


async def list_user_subscriptions(
    session: AsyncSession,
    user_id: int,
    settings: Settings,
) -> list[SubscriptionItem]:
    try:
        rows = await SubscriptionRepository(session).list_for_user_with_prices(user_id)
    except SQLAlchemyError as exc:
        logger.warning("User subscriptions are unavailable (%s)", type(exc).__name__)
        raise UserSubscriptionsUnavailable("Could not read user subscriptions") from exc

    masks = await get_card_masks([subscription.payment_method_id for subscription, _ in rows], settings)
    return [
        SubscriptionItem(
            id=subscription.id,
            nominal_usd=subscription.nominal_usd,
            status=subscription.status,
            card_mask=masks[subscription.payment_method_id],
            next_billing_at=subscription.next_billing_at,
            price_rub=price_rub,
            created_at=subscription.created_at,
        )
        for subscription, price_rub in rows
    ]
