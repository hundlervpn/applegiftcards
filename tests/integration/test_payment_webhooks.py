import asyncio
from copy import deepcopy

import aiosmtplib
import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.models.enums import GiftCardStatus, OrderStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.subscription import Subscription

WEBHOOK = "/api/v1/payments/webhook"


async def assert_pending(session_factory, order_id, smtp):
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order.status is OrderStatus.pending_payment
        assert order.paid_at is None and order.email_sent_at is None
        cards = list(await session.scalars(select(GiftCard).where(GiftCard.order_id == order_id)))
        assert len(cards) == 1
        assert cards[0].status is GiftCardStatus.reserved
    smtp.assert_not_awaited()


async def test_purchase_webhook_email_and_duplicate_are_consistent(
    client, stock, checkout, provider, session_factory, smtp
):
    card_ids = await stock(2)
    order_id = await checkout()
    notification = provider.notification(order_id)
    first = await client.post(WEBHOOK, json=notification)
    assert first.status_code == 200
    assert first.content == b""
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        card = await session.get(GiftCard, card_ids[0])
        other = await session.get(GiftCard, card_ids[1])
        assert order.status is OrderStatus.delivered
        assert order.paid_at is not None and order.email_sent_at >= order.paid_at
        timestamps = (order.paid_at, order.email_sent_at)
        assert card.status is GiftCardStatus.sold
        assert card.order_id == order_id
        assert other.status is GiftCardStatus.available and other.order_id is None
        assert b"TEST-10-0" not in card.code_encrypted
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 0
    smtp.assert_awaited_once()
    message = smtp.await_args.args[0]
    assert message["To"] == "buyer@example.com"
    assert message["From"] == "sender@example.com"
    assert str(order_id) in message["Subject"]
    plain = message.get_body(("plain",)).get_content()
    assert "TEST-10-0" in plain and "TEST-10-1" not in plain
    assert "1500.00" in plain
    assert smtp.await_args.kwargs == {
        "hostname": "smtp.test", "port": 2525, "username": "test-mailer",
        "password": "test-password", "timeout": 2.0, "start_tls": True,
    }
    for _ in range(3):
        repeat = await client.post(WEBHOOK, json=notification)
        assert repeat.status_code == 200
    smtp.assert_awaited_once()
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert (order.paid_at, order.email_sent_at) == timestamps
        assert await session.scalar(select(func.count()).select_from(GiftCard).where(GiftCard.status == GiftCardStatus.sold)) == 1
    lookups = [request for request in provider.requests if request.method == "GET"]
    assert len(lookups) == 4
    assert {request.url.path for request in lookups} == {f"/v3/payments/payment-{order_id}"}
    catalog = (await client.get("/api/v1/catalog")).json()
    assert catalog[0]["available_count"] == 1


async def test_parallel_duplicate_webhooks_deliver_exactly_once(client, stock, checkout, provider, smtp, session_factory):
    await stock(2)
    order_id = await checkout()
    notification = provider.notification(order_id)
    barrier = asyncio.Barrier(5)

    async def notify():
        await barrier.wait()
        return await client.post(WEBHOOK, json=notification)

    responses = await asyncio.wait_for(asyncio.gather(*(notify() for _ in range(5))), timeout=10)
    assert [response.status_code for response in responses] == [200] * 5
    smtp.assert_awaited_once()
    async with session_factory() as session:
        assert (await session.get(Order, order_id)).status is OrderStatus.delivered
        assert await session.scalar(select(func.count()).select_from(GiftCard).where(GiftCard.status == GiftCardStatus.sold)) == 1
        assert await session.scalar(select(func.count()).select_from(GiftCard).where(GiftCard.status == GiftCardStatus.available)) == 1


@pytest.mark.parametrize("mutation", ["empty", "negative", "zero", "long_id", "missing_id", "wrong_type"])
async def test_invalid_notification_returns_400_before_lookup(client, stock, checkout, provider, smtp, session_factory, mutation):
    await stock()
    order_id = await checkout()
    body = provider.notification(order_id)
    if mutation == "empty":
        body = {}
    elif mutation in {"negative", "zero"}:
        body["object"]["amount"]["value"] = "-100.00" if mutation == "negative" else "0.00"
    elif mutation == "long_id":
        body["object"]["id"] = "x" * 65
    elif mutation == "missing_id":
        del body["object"]["id"]
    else:
        body["type"] = "payment"
    count = len(provider.requests)
    response = await client.post(WEBHOOK, json=body)
    assert response.status_code == 400
    assert len(provider.requests) == count
    await assert_pending(session_factory, order_id, smtp)


async def test_malformed_json_returns_400(client, provider, smtp):
    response = await client.post(WEBHOOK, content=b"{", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert provider.requests == []
    smtp.assert_not_awaited()


@pytest.mark.parametrize("mismatch", ["id", "status", "amount", "metadata", "event", "unsupported_status"])
async def test_untrusted_notification_returns_403_without_fulfillment(
    client, stock, checkout, provider, session_factory, smtp, mismatch
):
    await stock()
    order_id = await checkout()
    body = provider.notification(order_id)
    if mismatch == "event":
        body["event"] = "payment.canceled"
    elif mismatch == "unsupported_status":
        body["object"]["status"] = "refunded"
        body["event"] = "payment.refunded"
    else:
        returned = deepcopy(body["object"])
        replacements = {"id": "different-id", "status": "canceled", "amount": {"value": "1.00", "currency": "RUB"}, "metadata": {"order_id": "999999"}}
        returned[mismatch] = replacements[mismatch]
        provider.respond = lambda request: httpx.Response(200, json=returned)
    count = len(provider.requests)
    response = await client.post(WEBHOOK, json=body)
    assert response.status_code == 403
    assert len(provider.requests) - count == (0 if mismatch in {"event", "unsupported_status"} else 1)
    await assert_pending(session_factory, order_id, smtp)


@pytest.mark.parametrize("provider_status,expected", [(404, 403), (401, 503), (403, 503), (429, 503), (500, 503), (503, 503)])
async def test_verification_http_errors_preserve_reservation(
    client, stock, checkout, provider, session_factory, smtp, provider_status, expected
):
    await stock()
    order_id = await checkout()
    body = provider.notification(order_id)
    provider.respond = lambda request: httpx.Response(provider_status, json={"description": "private provider error"})
    response = await client.post(WEBHOOK, json=body)
    assert response.status_code == expected
    assert "private provider error" not in response.text
    await assert_pending(session_factory, order_id, smtp)


@pytest.mark.parametrize("failure", ["credentials", "timeout", "json", "schema"])
async def test_verification_unavailable_returns_503_without_fulfillment(
    client, stock, checkout, provider, session_factory, smtp, integration_settings, failure
):
    await stock()
    order_id = await checkout()
    body = provider.notification(order_id)

    def fail(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if failure == "json":
            return httpx.Response(200, content=b"invalid json")
        return httpx.Response(200, json={"id": "no-amount"})

    provider.respond = fail
    if failure == "credentials":
        integration_settings.yookassa_secret_key = ""
    response = await client.post(WEBHOOK, json=body)
    assert response.status_code == 503
    assert response.json()["detail"] == "Could not verify notification with YooKassa"
    await assert_pending(session_factory, order_id, smtp)


@pytest.mark.parametrize("change", ["amount", "currency", "unknown_order", "invalid_order", "pending", "canceled"])
async def test_verified_but_inapplicable_payment_is_acknowledged_without_delivery(
    client, stock, checkout, provider, session_factory, smtp, change
):
    await stock()
    order_id = await checkout()
    payment = provider.payments[f"payment-{order_id}"]
    if change == "amount":
        payment["amount"]["value"] = "1.00"
    elif change == "currency":
        payment["amount"]["currency"] = "USD"
    elif change in {"unknown_order", "invalid_order"}:
        payment["metadata"]["order_id"] = "999999" if change == "unknown_order" else "invalid"
    body = provider.notification(order_id, change if change in {"pending", "canceled"} else "succeeded")
    response = await client.post(WEBHOOK, json=body)
    assert response.status_code == 200
    await assert_pending(session_factory, order_id, smtp)


@pytest.mark.parametrize("failure", ["ciphertext", "missing_reservation"])
async def test_fulfillment_failure_returns_503_and_can_be_retried(
    client, stock, checkout, provider, session_factory, smtp, failure
):
    cards = await stock()
    order_id = await checkout()
    body = provider.notification(order_id)
    async with session_factory() as session:
        card = await session.get(GiftCard, cards[0])
        encrypted = card.code_encrypted
        if failure == "ciphertext":
            card.code_encrypted = b"corrupt ciphertext"
        else:
            card.status = GiftCardStatus.available
            card.order_id = None
            card.sale_price_rub = None
        await session.commit()
    response = await client.post(WEBHOOK, json=body)
    assert response.status_code == 503
    assert response.json()["detail"] == "Could not complete the paid order"
    smtp.assert_not_awaited()
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order.status is OrderStatus.pending_payment and order.paid_at is None
        assert await session.scalar(select(func.count()).select_from(GiftCard).where(GiftCard.status == GiftCardStatus.sold)) == 0
        card = await session.get(GiftCard, cards[0])
        card.code_encrypted = encrypted
        card.status = GiftCardStatus.reserved
        card.order_id = order_id
        card.sale_price_rub = order.total_amount_rub
        await session.commit()
    assert (await client.post(WEBHOOK, json=body)).status_code == 200
    smtp.assert_awaited_once()


async def test_response_is_sent_before_email_and_other_requests_keep_running(
    application, client, stock, checkout, provider, smtp, session_factory
):
    await stock()
    order_id = await checkout()
    body = provider.notification(order_id)
    email_started, release_email = asyncio.Event(), asyncio.Event()
    messages = []

    async def slow_smtp(*args, **kwargs):
        email_started.set()
        await release_email.wait()
        return {}, "queued"

    smtp.side_effect = slow_smtp

    async def observe(scope, receive, send):
        async def capture(message):
            messages.append(message)
            await send(message)
        await application(scope, receive, capture)

    async with AsyncClient(transport=httpx.ASGITransport(app=observe), base_url="http://test") as http:
        task = asyncio.create_task(http.post(WEBHOOK, json=body))
        try:
            await asyncio.wait_for(email_started.wait(), timeout=5)
            assert any(message["type"] == "http.response.start" and message["status"] == 200 for message in messages)
            assert any(message["type"] == "http.response.body" and not message.get("more_body", False) for message in messages)
            assert not task.done()
            catalog = await asyncio.wait_for(client.get("/api/v1/catalog"), timeout=5)
            assert catalog.status_code == 200
            async with session_factory() as session:
                order = await session.get(Order, order_id)
                assert order.status is OrderStatus.paid and order.email_sent_at is None
        finally:
            release_email.set()
            response = await asyncio.wait_for(task, timeout=5)
        assert response.status_code == 200
    async with session_factory() as session:
        assert (await session.get(Order, order_id)).status is OrderStatus.delivered


async def test_smtp_failure_keeps_paid_order_and_does_not_sell_another_card(
    application, stock, checkout, provider, smtp, session_factory
):
    await stock()
    order_id = await checkout()
    body = provider.notification(order_id)
    smtp.side_effect = aiosmtplib.SMTPException("test SMTP failure")
    async with AsyncClient(
        transport=httpx.ASGITransport(app=application, raise_app_exceptions=False), base_url="http://test"
    ) as http:
        assert (await http.post(WEBHOOK, json=body)).status_code == 200
        assert (await http.post(WEBHOOK, json=body)).status_code == 200
    smtp.assert_awaited_once()
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order.status is OrderStatus.paid
        assert order.paid_at is not None and order.email_sent_at is None
        assert await session.scalar(select(func.count()).select_from(GiftCard).where(GiftCard.status == GiftCardStatus.sold)) == 1
        assert await session.scalar(select(func.count()).select_from(GiftCard).where(GiftCard.status == GiftCardStatus.available)) == 1
