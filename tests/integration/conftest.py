import json
from base64 import b64encode
from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db.session import get_db
from app.main import create_app
from app.models.enums import GiftCardStatus
from app.models.gift_card import GiftCard
from app.models.price import Price
from app.services.crypto import encrypt_code

REAL_ASYNC_CLIENT = httpx.AsyncClient
ORDER_URL = "/api/v1/orders/one-time"


class YooKassaSandbox:
    def __init__(self):
        self.requests = []
        self.payments = {}
        self.respond = None

    async def handle(self, request):
        self.requests.append(request)
        assert request.url.host == "api.yookassa.ru"
        assert request.headers["Authorization"] == "Basic " + b64encode(b"test-shop:test-secret").decode()
        if self.respond is not None:
            return self.respond(request)
        if request.method == "POST" and request.url.path == "/v3/payments":
            payload = json.loads(request.content)
            payment_id = f"payment-{payload['metadata']['order_id']}"
            payment = {
                "id": payment_id,
                "status": "pending",
                "amount": payload["amount"],
                "metadata": payload["metadata"],
                "confirmation": {
                    "confirmation_url": f"https://yookassa.test/pay/{payment_id}",
                },
            }
            self.payments[payment_id] = payment
            return httpx.Response(200, json=payment)
        if request.method == "GET" and request.url.path.startswith("/v3/payments/"):
            payment = self.payments.get(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=payment) if payment else httpx.Response(404)
        raise AssertionError(f"Unexpected external request: {request.method} {request.url}")

    def notification(self, order_id, status="succeeded"):
        payment = self.payments[f"payment-{order_id}"]
        payment["status"] = status
        return {
            "type": "notification",
            "event": f"payment.{status}",
            "object": deepcopy(payment),
        }


@pytest.fixture(autouse=True)
def integration_settings(monkeypatch):
    values = {
        "DEBUG": "false",
        "YOOKASSA_SHOP_ID": "test-shop",
        "YOOKASSA_SECRET_KEY": "test-secret",
        "PAYMENT_RETURN_URL": "https://shop.test/payment/complete",
        "FERNET_KEY": Fernet.generate_key().decode(),
        "SMTP_HOST": "smtp.test",
        "SMTP_PORT": "2525",
        "SMTP_USER": "test-mailer",
        "SMTP_PASSWORD": "test-password",
        "SMTP_FROM_EMAIL": "sender@example.com",
        "SMTP_STARTTLS": "true",
        "SMTP_USE_TLS": "false",
        "SMTP_TIMEOUT_SECONDS": "2",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_ADMIN_CHAT_ID": "",
        "JWT_SECRET_KEY": "scrum-31-test-signing-secret-at-least-32-bytes",
        "JWT_ALGORITHM": "HS256",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    sandbox = YooKassaSandbox()
    transport = httpx.MockTransport(sandbox.handle)

    def provider_client(**kwargs):
        return REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", provider_client)
    return sandbox


@pytest.fixture(autouse=True)
def smtp(monkeypatch):
    send = AsyncMock(return_value=({}, "queued"))
    monkeypatch.setattr("app.services.email.aiosmtplib.send", send)
    return send


@pytest.fixture
def session_factory(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.get_session_maker", lambda: factory)
    monkeypatch.setattr("app.services.order_fulfillment.get_session_maker", lambda: factory)
    return factory


@pytest.fixture
def application(session_factory, integration_settings):
    async def override_db():
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: integration_settings
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(application):
    async with REAL_ASYNC_CLIENT(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as http:
        yield http


@pytest.fixture
def stock(session_factory):
    async def seed(count=2, nominal=10, price="1500.00"):
        async with session_factory() as session:
            if await session.get(Price, nominal) is None:
                session.add(Price(nominal_usd=nominal, price_rub=Decimal(price)))
                await session.flush()
            cards = [
                GiftCard(
                    nominal_usd=nominal,
                    code_encrypted=encrypt_code(f"TEST-{nominal}-{index}"),
                    status=GiftCardStatus.available,
                )
                for index in range(count)
            ]
            session.add_all(cards)
            await session.commit()
            return [card.id for card in cards]

    return seed


@pytest.fixture
def checkout(client):
    async def create(email="buyer@example.com", nominal=10):
        response = await client.post(
            ORDER_URL, json={"email": email, "nominal_usd": nominal}
        )
        assert response.status_code == 201, response.text
        return response.json()["order_id"]

    return create
