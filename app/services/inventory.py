from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import GiftCardStatus, OrderStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.repositories import inventory as inventory_repo


class InvalidOrderItemsError(ValueError):
    pass


class GiftCardUnavailableError(Exception):
    pass


class OrderNotReservableError(Exception):
    pass


def get_card_plan(items: Any) -> dict[int, list[Decimal]]:
    if not isinstance(items, list) or not items:
        raise InvalidOrderItemsError

    plan: dict[int, list[Decimal]] = defaultdict(list)
    for item in items:
        if not isinstance(item, dict):
            raise InvalidOrderItemsError
        nominal = item.get("nominal_usd")
        quantity = item.get("quantity")
        if (
            not isinstance(nominal, int)
            or isinstance(nominal, bool)
            or nominal <= 0
            or not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or quantity <= 0
        ):
            raise InvalidOrderItemsError
        try:
            unit_price = Decimal(str(item["price_rub"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise InvalidOrderItemsError from exc
        if not unit_price.is_finite() or unit_price <= 0:
            raise InvalidOrderItemsError
        plan[nominal].extend([unit_price] * quantity)

    return dict(plan)


async def reserve_gift_cards_for_order(
    session: AsyncSession,
    order: Order,
) -> list[GiftCard]:
    if order.id is None:
        raise OrderNotReservableError

    locked_order = await inventory_repo.lock_order(session, order.id)
    if locked_order is None or locked_order.status != OrderStatus.pending_payment:
        raise OrderNotReservableError
    plan = get_card_plan(locked_order.items)

    existing_cards = await inventory_repo.lock_reserved_cards_for_order(
        session,
        locked_order.id,
    )
    expected_counts = Counter(
        {nominal: len(prices) for nominal, prices in plan.items()}
    )
    if existing_cards:
        actual_counts = Counter(card.nominal_usd for card in existing_cards)
        if actual_counts != expected_counts:
            raise GiftCardUnavailableError
        return existing_cards

    selected_by_nominal: dict[int, list[GiftCard]] = {}
    for nominal in sorted(plan):
        cards = await inventory_repo.lock_available_cards(
            session,
            nominal,
            len(plan[nominal]),
        )
        if len(cards) != len(plan[nominal]):
            raise GiftCardUnavailableError
        selected_by_nominal[nominal] = cards

    reserved: list[GiftCard] = []
    for nominal in sorted(plan):
        for card, price in zip(
            selected_by_nominal[nominal],
            plan[nominal],
            strict=True,
        ):
            card.status = GiftCardStatus.reserved
            card.order_id = locked_order.id
            card.sale_price_rub = price
            reserved.append(card)

    await session.flush()
    return reserved
