import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus
from app.models.gift_card import GiftCard
from app.repositories import inventory as inventory_repo
from app.repositories import orders as orders_repo
from app.schemas.me import GiftCardItem, OrderHistoryItem
from app.services.crypto import GiftCardCryptoError, decrypt_code

logger = logging.getLogger(__name__)

_PAID_STATUSES = frozenset({OrderStatus.paid, OrderStatus.delivered})


class OrderHistoryUnavailable(Exception):
    pass


async def list_user_orders(
    session: AsyncSession,
    user_id: int,
    limit: int,
    offset: int,
) -> tuple[list[OrderHistoryItem], int]:
    try:
        orders = await orders_repo.list_for_user(session, user_id, limit=limit, offset=offset)
        total = await orders_repo.count_for_user(session, user_id)

        paid_order_ids = [order.id for order in orders if order.status in _PAID_STATUSES]
        cards_by_order: dict[int, list[GiftCard]] = {}
        if paid_order_ids:
            cards = await inventory_repo.list_sold_for_orders(session, paid_order_ids)
            for card in cards:
                if card.order_id is not None:
                    cards_by_order.setdefault(card.order_id, []).append(card)

        items = [
            OrderHistoryItem(
                id=order.id,
                status=order.status,
                total_amount_rub=order.total_amount_rub,
                created_at=order.created_at,
                paid_at=order.paid_at,
                cards=[
                    GiftCardItem(
                        nominal_usd=card.nominal_usd,
                        code=decrypt_code(card.code_encrypted),
                    )
                    for card in cards_by_order.get(order.id, [])
                ],
            )
            for order in orders
        ]
    except (SQLAlchemyError, GiftCardCryptoError) as exc:
        logger.warning("Order history is unavailable (%s)", type(exc).__name__)
        raise OrderHistoryUnavailable("Could not read order history") from exc

    return items, total
