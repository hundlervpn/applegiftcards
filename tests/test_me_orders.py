from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.models.enums import GiftCardStatus, OrderStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.price import Price
from app.models.user import User
from app.services import tokens
from app.services.crypto import encrypt_code

URL = "/api/v1/me/orders"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
PRICES = {10: Decimal("999.00"), 20: Decimal("1999.00")}


@pytest.fixture(autouse=True)
def history_settings(monkeypatch):
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("JWT_SECRET_KEY", "scrum-43-test-signing-secret-at-least-32-bytes")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def buyers(db_session):
    first = User(email="first@example.com")
    second = User(email="second@example.com")
    db_session.add_all(
        [first, second]
        + [Price(nominal_usd=nominal, price_rub=price) for nominal, price in PRICES.items()]
    )
    await db_session.commit()
    return first, second


def authorization(user):
    return {"Authorization": f"Bearer {tokens.create_access_token(user.id)}"}


async def add_order(session, user, status, codes=(), created_at=NOW, card_status=GiftCardStatus.sold):
    quantities = Counter(nominal for nominal, _ in codes) or Counter({10: 1})
    order = Order(
        user_id=user.id,
        status=status,
        items=[
            {"nominal_usd": nominal, "quantity": quantity, "price_rub": str(PRICES[nominal])}
            for nominal, quantity in quantities.items()
        ],
        total_amount_rub=sum(PRICES[nominal] * quantity for nominal, quantity in quantities.items()),
        created_at=created_at,
        paid_at=created_at if status in {OrderStatus.paid, OrderStatus.delivered} else None,
    )
    session.add(order)
    await session.flush()
    for nominal, code in codes:
        session.add(
            GiftCard(
                nominal_usd=nominal,
                code_encrypted=encrypt_code(code) if isinstance(code, str) else code,
                status=card_status,
                order_id=order.id,
                sale_price_rub=PRICES[nominal],
            )
        )
    return order


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer invalid"}])
async def test_missing_or_invalid_authorization_returns_401(client, headers):
    response = await client.get(URL, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("kind", ["refresh", "expired", "wrong-signature", "unknown-user", "inactive"])
async def test_rejects_unusable_credentials(client, db_session, buyers, kind):
    user = buyers[0]
    if kind == "refresh":
        token = tokens.create_refresh_token(user.id)
    elif kind in {"expired", "wrong-signature"}:
        settings = get_settings()
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": str(user.id),
                "type": "access",
                "iat": now - timedelta(minutes=2),
                "exp": now - timedelta(minutes=1) if kind == "expired" else now + timedelta(minutes=15),
            },
            settings.jwt_secret_key if kind == "expired" else "another-signing-secret-at-least-32-bytes",
            algorithm=settings.jwt_algorithm,
        )
    elif kind == "unknown-user":
        token = tokens.create_access_token(user.id + 10000)
    else:
        user.is_active = False
        await db_session.commit()
        token = tokens.create_access_token(user.id)
    response = await client.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_empty_history_returns_empty_page(client, buyers):
    response = await client.get(URL, headers=authorization(buyers[0]))
    assert response.status_code == 200
    assert response.json() == {"orders": [], "total": 0}


@pytest.mark.parametrize("status", list(OrderStatus))
async def test_history_fields_and_card_visibility_follow_order_status(client, db_session, buyers, status):
    order = await add_order(db_session, buyers[0], status, codes=[(10, "TEST-STATUS-CODE")])
    await db_session.commit()
    response = await client.get(URL, headers=authorization(buyers[0]))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["orders"][0]
    assert set(item) == {"id", "status", "total_amount_rub", "created_at", "paid_at", "cards"}
    assert item["id"] == order.id
    assert item["status"] == status.value
    assert item["total_amount_rub"] == "999.00"
    assert datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")) == NOW
    if status in {OrderStatus.paid, OrderStatus.delivered}:
        assert item["cards"] == [{"nominal_usd": 10, "code": "TEST-STATUS-CODE"}]
        assert datetime.fromisoformat(item["paid_at"].replace("Z", "+00:00")) == NOW
    else:
        assert item["cards"] == []
        assert item["paid_at"] is None


async def test_users_only_see_their_own_orders_and_codes(client, db_session, buyers):
    first, second = buyers
    own = await add_order(db_session, first, OrderStatus.paid, codes=[(10, "FIRST-CODE")])
    other = await add_order(db_session, second, OrderStatus.delivered, codes=[(10, "SECOND-CODE")])
    await add_order(db_session, second, OrderStatus.paid, codes=[(10, b"broken-foreign-ciphertext")])
    await db_session.commit()
    response = await client.get(URL, params={"user_id": second.id}, headers=authorization(first))
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["orders"]] == [own.id]
    assert response.json()["orders"][0]["cards"] == [{"nominal_usd": 10, "code": "FIRST-CODE"}]
    assert "SECOND-CODE" not in response.text
    response = await client.get(URL, params={"offset": 1}, headers=authorization(second))
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["orders"][0]["id"] == other.id
    assert "FIRST-CODE" not in response.text


async def test_pagination_counts_all_own_orders_and_orders_newest_first(client, db_session, buyers):
    first, second = buyers
    orders = [
        await add_order(
            db_session, first, OrderStatus.pending_payment, created_at=NOW + timedelta(minutes=i)
        )
        for i in range(5)
    ]
    await add_order(db_session, second, OrderStatus.pending_payment, created_at=NOW + timedelta(days=1))
    await db_session.commit()
    response = await client.get(URL, params={"limit": 2, "offset": 1}, headers=authorization(first))
    assert response.status_code == 200
    assert response.json()["total"] == 5
    assert [item["id"] for item in response.json()["orders"]] == [orders[3].id, orders[2].id]


async def test_equal_creation_times_use_order_id_as_tie_breaker(client, db_session, buyers):
    orders = [await add_order(db_session, buyers[0], OrderStatus.pending_payment) for _ in range(3)]
    await db_session.commit()
    response = await client.get(URL, params={"limit": 2}, headers=authorization(buyers[0]))
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["orders"]] == [orders[2].id, orders[1].id]


async def test_offset_past_last_order_keeps_total(client, db_session, buyers):
    await add_order(db_session, buyers[0], OrderStatus.pending_payment)
    await db_session.commit()
    response = await client.get(URL, params={"offset": 100}, headers=authorization(buyers[0]))
    assert response.status_code == 200
    assert response.json() == {"orders": [], "total": 1}


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"limit": -1}, {"offset": -1}, {"limit": "bad"}, {"offset": "bad"}])
async def test_invalid_pagination_returns_422(client, buyers, query):
    response = await client.get(URL, params=query, headers=authorization(buyers[0]))
    assert response.status_code == 422


async def test_default_limit_and_maximum_limit(client, db_session, buyers):
    for _ in range(25):
        await add_order(db_session, buyers[0], OrderStatus.pending_payment)
    await db_session.commit()
    response = await client.get(URL, headers=authorization(buyers[0]))
    assert response.status_code == 200
    assert len(response.json()["orders"]) == 20
    assert response.json()["total"] == 25
    response = await client.get(URL, params={"limit": 100}, headers=authorization(buyers[0]))
    assert response.status_code == 200
    assert len(response.json()["orders"]) == 25


async def test_multiple_purchased_cards_are_decrypted_and_keep_nominals(client, db_session, buyers):
    order = await add_order(
        db_session,
        buyers[0],
        OrderStatus.delivered,
        codes=[(10, "FIRST-CODE"), (20, "SECOND-CODE"), (10, "THIRD-CODE")],
    )
    await db_session.commit()
    response = await client.get(URL, headers=authorization(buyers[0]))
    assert response.status_code == 200
    assert response.json()["orders"][0]["id"] == order.id
    assert response.json()["orders"][0]["total_amount_rub"] == "3997.00"
    assert response.json()["orders"][0]["cards"] == [
        {"nominal_usd": 10, "code": "FIRST-CODE"},
        {"nominal_usd": 20, "code": "SECOND-CODE"},
        {"nominal_usd": 10, "code": "THIRD-CODE"},
    ]


async def test_reserved_cards_are_not_exposed_in_paid_order(client, db_session, buyers):
    order = await add_order(db_session, buyers[0], OrderStatus.paid, codes=[(10, "SOLD-CODE")])
    db_session.add(
        GiftCard(
            nominal_usd=10,
            code_encrypted=b"broken-reserved-ciphertext",
            status=GiftCardStatus.reserved,
            order_id=order.id,
            sale_price_rub=PRICES[10],
        )
    )
    await db_session.commit()
    response = await client.get(URL, headers=authorization(buyers[0]))
    assert response.status_code == 200
    assert response.json()["orders"][0]["cards"] == [{"nominal_usd": 10, "code": "SOLD-CODE"}]


@pytest.mark.parametrize("failure", ["corrupt-code", "missing-key", "different-key"])
async def test_decryption_errors_return_503_without_exposing_ciphertext(client, db_session, buyers, monkeypatch, failure):
    code = b"broken-ciphertext" if failure == "corrupt-code" else "TEST-SECRET-CODE"
    await add_order(db_session, buyers[0], OrderStatus.paid, codes=[(10, code)])
    await db_session.commit()
    if failure != "corrupt-code":
        monkeypatch.setenv("FERNET_KEY", "" if failure == "missing-key" else Fernet.generate_key().decode("ascii"))
        get_settings.cache_clear()
    response = await client.get(URL, headers=authorization(buyers[0]))
    assert response.status_code == 503
    assert response.json() == {"detail": "Order history is unavailable"}
    assert "TEST-SECRET-CODE" not in response.text
    assert "broken-ciphertext" not in response.text


async def test_unpaid_order_does_not_decrypt_codes(client, db_session, buyers):
    await add_order(
        db_session, buyers[0], OrderStatus.pending_payment, codes=[(10, b"broken-unpaid-ciphertext")]
    )
    await db_session.commit()
    response = await client.get(URL, headers=authorization(buyers[0]))
    assert response.status_code == 200
    assert response.json()["orders"][0]["cards"] == []


async def test_only_cards_from_requested_page_are_decrypted(client, db_session, buyers):
    await add_order(db_session, buyers[0], OrderStatus.paid, codes=[(10, b"broken-old-ciphertext")])
    newest = await add_order(
        db_session, buyers[0], OrderStatus.paid, codes=[(10, "NEWEST-CODE")], created_at=NOW + timedelta(days=1)
    )
    await db_session.commit()
    response = await client.get(URL, params={"limit": 1}, headers=authorization(buyers[0]))
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["orders"][0]["id"] == newest.id
    assert response.json()["orders"][0]["cards"] == [{"nominal_usd": 10, "code": "NEWEST-CODE"}]


@pytest.mark.parametrize("limit", [1, 20])
async def test_paid_history_has_constant_query_count(client, db_engine, db_session, buyers, limit):
    for index in range(20):
        await add_order(db_session, buyers[0], OrderStatus.paid, codes=[(10, f"TEST-CODE-{index}")])
    await db_session.commit()
    queries = []

    def record_query(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record_query)
    try:
        response = await client.get(URL, params={"limit": limit}, headers=authorization(buyers[0]))
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record_query)
    assert response.status_code == 200
    assert len(response.json()["orders"]) == limit
    assert len(queries) == 4


async def test_history_without_paid_orders_skips_card_query(client, db_engine, db_session, buyers):
    await add_order(db_session, buyers[0], OrderStatus.pending_payment)
    await db_session.commit()
    queries = []

    def record_query(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record_query)
    try:
        response = await client.get(URL, headers=authorization(buyers[0]))
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record_query)
    assert response.status_code == 200
    assert len(queries) == 3


async def test_database_failure_returns_503(client, buyers, monkeypatch):
    monkeypatch.setattr(
        "app.services.order_history.orders_repo.list_for_user",
        AsyncMock(side_effect=SQLAlchemyError("Test storage failure")),
    )
    response = await client.get(URL, headers=authorization(buyers[0]))
    assert response.status_code == 503
    assert response.json() == {"detail": "Order history is unavailable"}
