import asyncio
import json
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from sqlalchemy import func, select

from app.models.enums import GiftCardStatus, OrderStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.user import User
from app.services.tokens import create_access_token

CATALOG = "/api/v1/catalog"
ORDERS = "/api/v1/orders/one-time"


async def test_empty_catalog(client, provider):
    response = await client.get(CATALOG)
    assert response.status_code == 200
    assert response.json() == []
    assert provider.requests == []


async def test_catalog_counts_only_available_and_orders_nominals(client, stock, checkout, session_factory):
    cards = await stock(5, nominal=20, price="3000.50")
    await stock(0, nominal=50, price="7500.00")
    await stock(1, nominal=10)
    reserved_id = await checkout(nominal=20)
    sold_id = await checkout(email="sold@example.com", nominal=20)
    async with session_factory() as session:
        sold = await session.scalar(select(GiftCard).where(GiftCard.order_id == sold_id))
        sold.status = GiftCardStatus.sold
        blocked = await session.get(GiftCard, cards[-1])
        blocked.status = GiftCardStatus.blocked
        await session.commit()

    response = await client.get(CATALOG)
    assert response.status_code == 200
    assert response.json() == [
        {"nominal_usd": 10, "price_rub": "1500.00", "available_count": 1, "is_available": True},
        {"nominal_usd": 20, "price_rub": "3000.50", "available_count": 2, "is_available": True},
        {"nominal_usd": 50, "price_rub": "7500.00", "available_count": 0, "is_available": False},
    ]
    assert reserved_id != sold_id


async def test_checkout_commits_user_order_reservation_and_payment(client, stock, provider, session_factory):
    cards = await stock()
    response = await client.post(ORDERS, json={"email": "Buyer@EXAMPLE.COM", "nominal_usd": 10})
    assert response.status_code == 201
    order_id = response.json()["order_id"]
    assert response.json()["payment_url"] == f"https://yookassa.test/pay/payment-{order_id}"
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        user = await session.get(User, order.user_id)
        reserved = await session.get(GiftCard, cards[0])
        available = await session.get(GiftCard, cards[1])
        assert user.email == "buyer@example.com"
        assert order.status is OrderStatus.pending_payment
        assert order.total_amount_rub == Decimal("1500.00")
        assert order.items == [{"nominal_usd": 10, "quantity": 1, "price_rub": "1500.00"}]
        assert order.paid_at is None and order.email_sent_at is None
        assert reserved.status is GiftCardStatus.reserved
        assert reserved.order_id == order_id
        assert reserved.sale_price_rub == order.total_amount_rub
        assert available.status is GiftCardStatus.available
        assert available.order_id is None
    request, = provider.requests
    payload = json.loads(request.content)
    assert request.headers["Idempotence-Key"] == str(uuid5(NAMESPACE_URL, f"giftcodes:yookassa:order:{order_id}"))
    assert payload["amount"] == {"value": "1500.00", "currency": "RUB"}
    assert payload["metadata"] == {"order_id": str(order_id)}
    assert payload["capture"] is True
    assert payload["confirmation"] == {"type": "redirect", "return_url": "https://shop.test/payment/complete"}
    assert "save_payment_method" not in payload


async def test_empty_stock_returns_409_without_order_or_provider_request(client, stock, provider, session_factory):
    await stock(0)
    response = await client.post(ORDERS, json={"email": "buyer@example.com", "nominal_usd": 10})
    assert response.status_code == 409
    assert response.json()["detail"] == "No gift cards are available for this nominal"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(User)) == 0
    assert provider.requests == []


@pytest.mark.parametrize("payload", [
    {},
    {"email": "invalid", "nominal_usd": 10},
    {"email": "buyer@example.com", "nominal_usd": -10},
    {"email": "buyer@example.com", "nominal_usd": 7},
    {"email": "buyer@example.com", "nominal_usd": 50},
])
async def test_invalid_checkout_returns_422_without_writes(client, stock, provider, session_factory, payload):
    await stock()
    response = await client.post(ORDERS, json=payload)
    assert response.status_code == 422
    assert provider.requests == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(User)) == 0
        assert set(await session.scalars(select(GiftCard.status))) == {GiftCardStatus.available}


@pytest.mark.parametrize("failure,expected", [
    ("timeout", 503), ("connection", 503), ("credentials", 503),
    ("http_500", 502), ("json", 502), ("schema", 502),
])
async def test_acquiring_failure_rolls_back_everything_and_allows_retry(
    client, stock, provider, integration_settings, session_factory, failure, expected
):
    cards = await stock(1)

    def fail(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if failure == "connection":
            raise httpx.ConnectError("test connection failure", request=request)
        if failure == "http_500":
            return httpx.Response(500, json={"description": "provider unavailable"})
        if failure == "json":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(200, json={"id": "missing-confirmation"})

    provider.respond = fail
    if failure == "credentials":
        integration_settings.yookassa_secret_key = ""
    response = await client.post(ORDERS, json={"email": "buyer@example.com", "nominal_usd": 10})
    assert response.status_code == expected
    assert "test-secret" not in response.text
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(User)) == 0
        card = await session.get(GiftCard, cards[0])
        assert card.status is GiftCardStatus.available
        assert card.order_id is None and card.sale_price_rub is None
    assert len(provider.requests) == (0 if failure == "credentials" else 1)
    integration_settings.yookassa_secret_key = "test-secret"
    provider.respond = None
    retry = await client.post(ORDERS, json={"email": "buyer@example.com", "nominal_usd": 10})
    assert retry.status_code == 201


async def test_missing_subscription_returns_404_with_real_authorization(client, session_factory, provider):
    async with session_factory() as session:
        user = User(email="owner@example.com")
        session.add(user)
        await session.commit()
        token = create_access_token(user.id)
    response = await client.post(
        "/api/v1/subscriptions/999999/cancel", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Subscription not found"}
    assert provider.requests == []


async def test_concurrent_checkouts_cannot_oversell(client, stock, provider, session_factory):
    cards = await stock(3)
    barrier = asyncio.Barrier(8)

    async def buy(index):
        await barrier.wait()
        return await client.post(ORDERS, json={"email": f"buyer{index}@example.com", "nominal_usd": 10})

    responses = await asyncio.wait_for(asyncio.gather(*(buy(i) for i in range(8))), timeout=10)
    assert sorted(response.status_code for response in responses) == [201] * 3 + [409] * 5
    order_ids = {response.json()["order_id"] for response in responses if response.status_code == 201}
    assert len(order_ids) == len(cards)
    assert len(provider.requests) == len(cards)
    async with session_factory() as session:
        stored = list(await session.scalars(select(GiftCard)))
        assert {card.order_id for card in stored} == order_ids
        assert all(card.status is GiftCardStatus.reserved for card in stored)
        assert await session.scalar(select(func.count()).select_from(Order)) == len(cards)
        assert await session.scalar(select(func.count()).select_from(User)) == len(cards)
