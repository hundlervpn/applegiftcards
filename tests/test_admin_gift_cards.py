from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models.enums import GiftCardStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.price import Price
from app.models.user import User
from app.repositories.gift_cards import add_batch
from app.services.crypto import GiftCardEncryptionKeyError, decrypt_code, encrypt_code

ADMIN_TOKEN = "test-admin-token"
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}
BATCH_URL = "/api/v1/admin/gift-cards/batch"
STATS_URL = "/api/v1/admin/stats"
VALID_BATCH = {"cards": [{"nominal_usd": 10, "codes": ["TEST-CODE-A"]}]}


@pytest.fixture(autouse=True)
def admin_settings(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def prices(db_session):
    db_session.add_all(
        [
            Price(nominal_usd=10, price_rub=Decimal("999.00")),
            Price(nominal_usd=20, price_rub=Decimal("1999.00")),
            Price(nominal_usd=50, price_rub=Decimal("4999.00")),
        ]
    )
    await db_session.commit()


@pytest.mark.parametrize(
    ("method", "url", "headers", "expected"),
    [
        ("POST", BATCH_URL, {}, 401),
        ("POST", BATCH_URL, {"X-Admin-Token": "wrong-token"}, 403),
        ("GET", STATS_URL, {}, 401),
        ("GET", STATS_URL, {"X-Admin-Token": "wrong-token"}, 403),
        ("POST", BATCH_URL, [(b"x-admin-token", b"\xff")], 403),
    ],
)
async def test_admin_access_requires_valid_token(client, db_session, method, url, headers, expected):
    response = await client.request(method, url, json=VALID_BATCH, headers=headers)
    assert response.status_code == expected
    assert await db_session.scalar(select(func.count()).select_from(GiftCard)) == 0


async def test_unconfigured_admin_token_keeps_api_closed(client, db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    get_settings.cache_clear()
    response = await client.post(BATCH_URL, json=VALID_BATCH, headers=ADMIN_HEADERS)
    assert response.status_code == 503
    assert await db_session.scalar(select(func.count()).select_from(GiftCard)) == 0


async def test_batch_stores_encrypted_available_cards(client, db_session, prices):
    response = await client.post(
        BATCH_URL,
        json={
            "cards": [
                {"nominal_usd": 10, "codes": ["TEST-CODE-A", "TEST-CODE-B"]},
                {"nominal_usd": 20, "codes": ["TEST-CODE-C"]},
            ]
        },
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201
    assert response.json() == {"added": 3}
    cards = list(await db_session.scalars(select(GiftCard)))
    assert len(cards) == 3
    assert {decrypt_code(card.code_encrypted): card.nominal_usd for card in cards} == {
        "TEST-CODE-A": 10,
        "TEST-CODE-B": 10,
        "TEST-CODE-C": 20,
    }
    for card in cards:
        assert card.code_encrypted != decrypt_code(card.code_encrypted).encode("utf-8")
        assert card.status == GiftCardStatus.available
        assert card.order_id is None
        assert card.sale_price_rub is None


async def test_code_surrounding_whitespace_is_removed(client, db_session, prices):
    response = await client.post(
        BATCH_URL,
        json={"cards": [{"nominal_usd": 10, "codes": ["  TEST-CODE-A \n"]}]},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201
    card = await db_session.scalar(select(GiftCard))
    assert decrypt_code(card.code_encrypted) == "TEST-CODE-A"


@pytest.mark.parametrize(
    "payload",
    [
        {"cards": []},
        {"cards": [{"nominal_usd": 7, "codes": ["TEST-CODE"]}]},
        {"cards": [{"nominal_usd": True, "codes": ["TEST-CODE"]}]},
        {"cards": [{"nominal_usd": "10", "codes": ["TEST-CODE"]}]},
        {"cards": [{"nominal_usd": 10, "codes": []}]},
        {"cards": [{"nominal_usd": 10, "codes": [""]}]},
        {"cards": [{"nominal_usd": 10, "codes": ["   "]}]},
        {"cards": [{"nominal_usd": 10, "codes": ["X" * 129]}]},
        {"cards": [{"nominal_usd": 10, "codes": [123]}]},
        {
            "cards": [
                {"nominal_usd": 10, "codes": ["CODE"] * 501},
                {"nominal_usd": 20, "codes": ["CODE"] * 500},
            ]
        },
    ],
)
async def test_invalid_batch_does_not_store_cards(client, db_session, payload):
    response = await client.post(BATCH_URL, json=payload, headers=ADMIN_HEADERS)
    assert response.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(GiftCard)) == 0


async def test_maximum_batch_size_is_accepted(client, db_session, prices):
    response = await client.post(
        BATCH_URL,
        json={"cards": [{"nominal_usd": 10, "codes": [f"TEST-{i}" for i in range(1000)]}]},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201
    assert response.json() == {"added": 1000}
    assert await db_session.scalar(select(func.count()).select_from(GiftCard)) == 1000


@pytest.mark.parametrize("key", ["", "not-a-valid-fernet-key"])
async def test_encryption_key_failure_does_not_store_cards(client, db_session, prices, monkeypatch, key):
    monkeypatch.setenv("FERNET_KEY", key)
    get_settings.cache_clear()
    response = await client.post(BATCH_URL, json=VALID_BATCH, headers=ADMIN_HEADERS)
    assert response.status_code == 503
    assert await db_session.scalar(select(func.count()).select_from(GiftCard)) == 0


async def test_mid_batch_encryption_failure_stores_nothing(client, db_session, prices, monkeypatch):
    def fail_on_second_code(code):
        if code == "TEST-CODE-B":
            raise GiftCardEncryptionKeyError("Test encryption failure")
        return encrypt_code(code)

    monkeypatch.setattr("app.services.admin_inventory.encrypt_code", fail_on_second_code)
    response = await client.post(
        BATCH_URL,
        json={"cards": [{"nominal_usd": 10, "codes": ["TEST-CODE-A", "TEST-CODE-B"]}]},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 503
    assert await db_session.scalar(select(func.count()).select_from(GiftCard)) == 0


async def test_database_failure_does_not_save_part_of_batch(client, db_session):
    db_session.add(Price(nominal_usd=10, price_rub=Decimal("999.00")))
    await db_session.commit()
    response = await client.post(
        BATCH_URL,
        json={
            "cards": [
                {"nominal_usd": 10, "codes": ["TEST-CODE-A"]},
                {"nominal_usd": 20, "codes": ["TEST-CODE-B"]},
            ]
        },
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 503
    assert await db_session.scalar(select(func.count()).select_from(GiftCard)) == 0


async def test_failed_batch_rolls_back_and_session_remains_usable(db_session):
    db_session.add(Price(nominal_usd=10, price_rub=Decimal("999.00")))
    await db_session.commit()
    cards = [
        GiftCard(nominal_usd=10, code_encrypted=encrypt_code("TEST-CODE-A")),
        GiftCard(nominal_usd=20, code_encrypted=encrypt_code("TEST-CODE-B")),
    ]
    with pytest.raises(IntegrityError):
        await add_batch(db_session, cards)
    assert await db_session.scalar(select(func.count()).select_from(GiftCard)) == 0
    added = await add_batch(
        db_session,
        [GiftCard(nominal_usd=10, code_encrypted=encrypt_code("TEST-CODE-C"))],
    )
    assert added == 1
    assert await db_session.scalar(select(func.count()).select_from(GiftCard)) == 1


async def test_stats_include_empty_nominals(client, prices):
    response = await client.get(STATS_URL, headers=ADMIN_HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "nominals": [
            {"nominal_usd": nominal, "available": 0, "reserved": 0, "sold": 0}
            for nominal in (10, 20, 50)
        ]
    }


async def test_stats_aggregate_states_and_exclude_blocked_cards(client, db_session, prices):
    user = User(email="admin-stats@example.com")
    db_session.add(user)
    await db_session.flush()
    order = Order(
        user_id=user.id,
        items=[{"nominal_usd": 10, "quantity": 1, "price_rub": "999.00"}],
        total_amount_rub=Decimal("999.00"),
    )
    db_session.add(order)
    await db_session.flush()
    states = [
        (10, GiftCardStatus.available),
        (10, GiftCardStatus.available),
        (10, GiftCardStatus.sold),
        (20, GiftCardStatus.reserved),
        (20, GiftCardStatus.reserved),
        (50, GiftCardStatus.blocked),
    ]
    for index, (nominal, status) in enumerate(states):
        assigned = status in {GiftCardStatus.reserved, GiftCardStatus.sold}
        db_session.add(
            GiftCard(
                nominal_usd=nominal,
                code_encrypted=encrypt_code(f"STATS-CODE-{index}"),
                status=status,
                order_id=order.id if assigned else None,
                sale_price_rub=Decimal("999.00") if assigned else None,
            )
        )
    await db_session.commit()
    response = await client.get(STATS_URL, headers=ADMIN_HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "nominals": [
            {"nominal_usd": 10, "available": 2, "reserved": 0, "sold": 1},
            {"nominal_usd": 20, "available": 0, "reserved": 2, "sold": 0},
            {"nominal_usd": 50, "available": 0, "reserved": 0, "sold": 0},
        ]
    }
