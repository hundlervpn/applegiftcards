from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.models.enums import SubscriptionStatus
from app.models.price import Price
from app.models.subscription import Subscription
from app.models.user import User
from app.repositories.subscriptions import SubscriptionRepository
from app.services import payment_methods, tokens

URL = "/api/v1/me/subscriptions"
JWT_SECRET = "scrum-44-test-signing-secret-at-least-32-bytes"
API_SECRET = "test-yookassa-secret"
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def subscription_settings(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", JWT_SECRET)
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "test-shop")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", API_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def payment_api(monkeypatch):
    client_type = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": request.url.path.rsplit("/", 1)[-1],
                "type": "bank_card",
                "saved": True,
                "status": "active",
                "title": "private card title",
                "card": {
                    "last4": "4242",
                    "first6": "555555",
                    "number": "5555555555554242",
                    "expiry_month": "12",
                    "expiry_year": "2030",
                    "csc": "123",
                },
                "metadata": {"secret": "private-metadata"},
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        payment_methods.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    return requests


async def add_user(session, email="subscriber@example.com", *, active=True):
    user = User(email=email, is_active=active)
    session.add(user)
    await session.commit()
    return user


async def add_price(session, nominal_usd=10, price="1500.25"):
    session.add(Price(nominal_usd=nominal_usd, price_rub=Decimal(price)))
    await session.commit()


async def add_subscription(
    session,
    user_id,
    *,
    nominal_usd=10,
    status=SubscriptionStatus.active,
    method_id="pm_own",
    created_at=NOW,
):
    subscription = Subscription(
        user_id=user_id,
        nominal_usd=nominal_usd,
        payment_method_id=method_id,
        status=status,
        next_billing_at=NOW + timedelta(days=30),
        created_at=created_at,
    )
    session.add(subscription)
    await session.commit()
    return subscription


def headers(user_id):
    return {"Authorization": f"Bearer {tokens.create_access_token(user_id)}"}


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic dXNlcjpwYXNz", "Bearer garbage", "Bearer"],
)
async def test_requires_bearer_access_token(client, payment_api, authorization):
    response = await client.get(
        URL,
        headers={} if authorization is None else {"Authorization": authorization},
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert payment_api == []


@pytest.mark.parametrize("kind", ["refresh", "expired", "wrong_signature", "unknown", "inactive"])
async def test_invalid_access_is_rejected(client, db_session, payment_api, kind):
    user = await add_user(db_session, active=kind != "inactive")
    user_id = user.id + 1000 if kind == "unknown" else user.id
    token = tokens.create_access_token(user_id)
    if kind == "refresh":
        token = tokens.create_refresh_token(user_id)
    elif kind in {"expired", "wrong_signature"}:
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": str(user_id),
                "type": "access",
                "iat": now - timedelta(minutes=2),
                "exp": now + timedelta(minutes=-1 if kind == "expired" else 5),
            },
            "another-test-signing-secret-at-least-32-bytes" if kind == "wrong_signature" else JWT_SECRET,
            algorithm="HS256",
        )
    response = await client.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert payment_api == []


async def test_empty_subscriptions_do_not_call_provider(client, db_session, payment_api):
    user = await add_user(db_session)
    response = await client.get(URL, headers=headers(user.id))
    assert response.status_code == 200
    assert response.json() == {"subscriptions": []}
    assert payment_api == []


async def test_returns_all_statuses_and_only_masked_card_data(client, db_session, payment_api):
    user = await add_user(db_session)
    await add_price(db_session)
    subscriptions = [
        await add_subscription(db_session, user.id, status=status)
        for status in SubscriptionStatus
    ]
    response = await client.get(URL, headers=headers(user.id))
    assert response.status_code == 200
    items = response.json()["subscriptions"]
    assert [item["id"] for item in items] == [subscription.id for subscription in reversed(subscriptions)]
    assert {item["status"] for item in items} == {status.value for status in SubscriptionStatus}
    for item in items:
        assert set(item) == {
            "id", "nominal_usd", "status", "card_mask", "next_billing_at", "price_rub", "created_at",
        }
        assert item["nominal_usd"] == 10
        assert Decimal(item["price_rub"]) == Decimal("1500.25")
        assert item["card_mask"] == "**** 4242"
        assert datetime.fromisoformat(item["next_billing_at"]) == NOW + timedelta(days=30)
        assert datetime.fromisoformat(item["created_at"]) == NOW
    for private in ("pm_own", "555555", "2030", "private card title", "private-metadata", API_SECRET):
        assert private not in response.text
    assert len(payment_api) == 1


async def test_only_current_users_subscriptions_and_methods_are_loaded(client, db_session, payment_api):
    user = await add_user(db_session)
    other = await add_user(db_session, "other@example.com")
    await add_price(db_session)
    own = await add_subscription(db_session, user.id)
    await add_subscription(db_session, other.id, method_id="pm_foreign")
    response = await client.get(URL, params={"user_id": other.id}, headers=headers(user.id))
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["subscriptions"]] == [own.id]
    assert len(payment_api) == 1
    assert payment_api[0].url.path.endswith("/pm_own")
    assert "pm_foreign" not in response.text


async def test_other_users_subscriptions_do_not_affect_empty_history(client, db_session, payment_api):
    user = await add_user(db_session)
    other = await add_user(db_session, "other@example.com")
    await add_price(db_session)
    await add_subscription(db_session, other.id, method_id="pm_foreign")
    response = await client.get(URL, headers=headers(user.id))
    assert response.status_code == 200
    assert response.json() == {"subscriptions": []}
    assert payment_api == []


async def test_returns_current_status_date_and_price(client, db_session, payment_api):
    user = await add_user(db_session)
    await add_price(db_session)
    subscription = await add_subscription(db_session, user.id)
    for status in SubscriptionStatus:
        subscription.status = status
        subscription.next_billing_at += timedelta(days=1)
        price = await db_session.get(Price, 10)
        price.price_rub += Decimal("0.01")
        await db_session.commit()
        response = await client.get(URL, headers=headers(user.id))
        assert response.status_code == 200
        item = response.json()["subscriptions"][0]
        assert item["status"] == status.value
        assert datetime.fromisoformat(item["next_billing_at"]) == subscription.next_billing_at
        assert Decimal(item["price_rub"]) == price.price_rub


async def test_joins_prices_for_each_nominal_and_sorts_newest_first(client, db_session, payment_api):
    user = await add_user(db_session)
    await add_price(db_session, 10, "1500.25")
    await add_price(db_session, 20, "2999.99")
    newer = await add_subscription(db_session, user.id, nominal_usd=20, created_at=NOW)
    older = await add_subscription(db_session, user.id, created_at=NOW - timedelta(days=1))
    response = await client.get(URL, headers=headers(user.id))
    assert response.status_code == 200
    items = response.json()["subscriptions"]
    assert [item["id"] for item in items] == [newer.id, older.id]
    assert [Decimal(item["price_rub"]) for item in items] == [Decimal("2999.99"), Decimal("1500.25")]


@pytest.mark.parametrize("size", [0, 1, 20])
async def test_database_query_count_is_constant(client, db_session, db_engine, payment_api, size):
    user = await add_user(db_session)
    await add_price(db_session)
    for index in range(size):
        await add_subscription(db_session, user.id, method_id=f"pm_{index}")
    statements = []

    def count_selects(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", count_selects)
    try:
        response = await client.get(URL, headers=headers(user.id))
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", count_selects)
    assert response.status_code == 200
    assert len(response.json()["subscriptions"]) == size
    assert len(statements) == 2
    assert len(payment_api) == size


async def test_database_failure_returns_generic_503(client, db_session, payment_api, monkeypatch, caplog):
    user = await add_user(db_session)
    monkeypatch.setattr(
        SubscriptionRepository,
        "list_for_user_with_prices",
        AsyncMock(side_effect=SQLAlchemyError("private connection string")),
    )
    response = await client.get(URL, headers=headers(user.id))
    assert response.status_code == 503
    assert response.json() == {"detail": "Subscriptions are unavailable"}
    assert "private connection string" not in caplog.text
    assert payment_api == []


async def test_missing_provider_credentials_keep_subscriptions_available(
    client, db_session, payment_api, monkeypatch,
):
    user = await add_user(db_session)
    await add_price(db_session)
    subscription = await add_subscription(db_session, user.id)
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "")
    get_settings.cache_clear()
    response = await client.get(URL, headers=headers(user.id))
    assert response.status_code == 200
    item = response.json()["subscriptions"][0]
    assert item["id"] == subscription.id
    assert item["status"] == "active"
    assert item["card_mask"] is None
    assert payment_api == []


async def test_provider_timeout_keeps_subscriptions_available(client, db_session, monkeypatch, caplog):
    user = await add_user(db_session)
    await add_price(db_session)
    await add_subscription(db_session, user.id)
    client_type = httpx.AsyncClient

    def handler(request):
        raise httpx.ReadTimeout(f"private provider error {API_SECRET}", request=request)

    monkeypatch.setattr(
        payment_methods.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    response = await client.get(URL, headers=headers(user.id))
    assert response.status_code == 200
    item = response.json()["subscriptions"][0]
    assert item["card_mask"] is None
    assert item["status"] == "active"
    assert API_SECRET not in response.text + caplog.text
