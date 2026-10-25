import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.models.enums import GiftCardStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.user import User
from app.services.inventory import GiftCardUnavailableError, reserve_gift_cards_for_order


async def pending_order(factory, index, items=None):
    items = items or [{"nominal_usd": 10, "quantity": 1, "price_rub": "1500.00"}]
    async with factory() as session:
        user = User(email=f"lock-buyer{index}@example.com")
        session.add(user)
        await session.flush()
        order = Order(
            user_id=user.id,
            items=items,
            total_amount_rub=sum(
                (Decimal(item["price_rub"]) * item["quantity"] for item in items),
                Decimal("0.00"),
            ),
        )
        session.add(order)
        await session.commit()
        return order.id


async def test_skip_locked_selects_next_card_in_an_independent_transaction(stock, session_factory):
    card_ids = await stock(2)
    first_order = await pending_order(session_factory, 1)
    second_order = await pending_order(session_factory, 2)
    async with session_factory() as holder, session_factory() as contender:
        first_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        second_pid = await contender.scalar(text("SELECT pg_backend_pid()"))
        assert first_pid != second_pid
        locked = await holder.scalar(select(GiftCard).where(GiftCard.id == card_ids[0]).with_for_update())
        assert locked.status is GiftCardStatus.available
        order = await contender.get(Order, second_order)
        reserved = await asyncio.wait_for(reserve_gift_cards_for_order(contender, order), timeout=3)
        assert [card.id for card in reserved] == [card_ids[1]]
        await contender.commit()
        await holder.rollback()
        order = await holder.get(Order, first_order)
        reserved = await reserve_gift_cards_for_order(holder, order)
        assert [card.id for card in reserved] == [card_ids[0]]
        await holder.commit()
    async with session_factory() as check:
        cards = list(await check.scalars(select(GiftCard).order_by(GiftCard.id)))
        assert [card.order_id for card in cards] == [first_order, second_order]
        assert all(card.status is GiftCardStatus.reserved for card in cards)


async def test_uncommitted_reservation_is_skipped_and_rollback_releases_stock(stock, session_factory):
    card_ids = await stock(1)
    first_order = await pending_order(session_factory, 1)
    second_order = await pending_order(session_factory, 2)
    async with session_factory() as holder, session_factory() as contender:
        first = await holder.get(Order, first_order)
        assert [card.id for card in await reserve_gift_cards_for_order(holder, first)] == card_ids
        second = await contender.get(Order, second_order)
        with pytest.raises(GiftCardUnavailableError):
            await asyncio.wait_for(reserve_gift_cards_for_order(contender, second), timeout=3)
        await contender.rollback()
        await holder.rollback()
        second = await contender.get(Order, second_order)
        reserved = await reserve_gift_cards_for_order(contender, second)
        assert [card.id for card in reserved] == card_ids
        await contender.commit()
    async with session_factory() as check:
        card = await check.get(GiftCard, card_ids[0])
        assert card.order_id == second_order
        assert card.status is GiftCardStatus.reserved


async def test_repeated_reservation_of_one_order_keeps_the_same_card(stock, session_factory):
    card_ids = await stock(2)
    order_id = await pending_order(session_factory, 1)
    async with session_factory() as first:
        order = await first.get(Order, order_id)
        first_cards = await reserve_gift_cards_for_order(first, order)
        first_ids = [card.id for card in first_cards]
        await first.commit()
    async with session_factory() as second:
        order = await second.get(Order, order_id)
        again = await reserve_gift_cards_for_order(second, order)
        assert [card.id for card in again] == first_ids
        await second.commit()
        other = await second.get(GiftCard, card_ids[1])
        assert other.status is GiftCardStatus.available and other.order_id is None


async def test_insufficient_mixed_order_does_not_keep_locks_or_partial_reservations(stock, session_factory):
    cards = await stock(1)
    await stock(0, nominal=20)
    items = [
        {"nominal_usd": 10, "quantity": 1, "price_rub": "1500.00"},
        {"nominal_usd": 20, "quantity": 1, "price_rub": "3000.00"},
    ]
    mixed_id = await pending_order(session_factory, 1, items)
    retry_id = await pending_order(session_factory, 2)
    async with session_factory() as first:
        order = await first.get(Order, mixed_id)
        with pytest.raises(GiftCardUnavailableError):
            await reserve_gift_cards_for_order(first, order)
        await first.rollback()
    async with session_factory() as second:
        order = await second.get(Order, retry_id)
        reserved = await asyncio.wait_for(reserve_gift_cards_for_order(second, order), timeout=3)
        assert [card.id for card in reserved] == cards
        await second.commit()


async def test_requested_nominal_is_reserved_without_taking_other_stock(stock, session_factory):
    await stock(2, nominal=20)
    card_ids = await stock(1, nominal=10)
    order_id = await pending_order(session_factory, 1)
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        reserved = await reserve_gift_cards_for_order(session, order)
        assert [card.id for card in reserved] == card_ids
        await session.commit()
        other = list(await session.scalars(select(GiftCard).where(GiftCard.nominal_usd == 20)))
        assert all(card.status is GiftCardStatus.available and card.order_id is None for card in other)
