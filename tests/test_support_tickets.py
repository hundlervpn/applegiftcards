import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import BackgroundTasks
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import support
from app.config import get_settings
from app.main import create_app
from app.models.enums import OrderStatus, SupportTicketStatus
from app.models.order import Order
from app.models.support_ticket import SupportTicket
from app.models.user import User
from app.schemas.support_ticket import SupportTicketCreate
from app.services import alerts, tokens

URL = "/api/v1/support/tickets"
SECRET = "scrum65-test-jwt-secret-at-least-sixty-four-characters-for-isolated-tests"
BODY = {"subject": "Код не активируется", "message": "При активации кода появляется ошибка."}


@pytest.fixture(autouse=True)
def support_settings(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def owner(db_session):
    user = User(email="support-buyer@example.com")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def other(db_session):
    user = User(email="other-support-buyer@example.com")
    db_session.add(user)
    await db_session.commit()
    return user


def headers(user):
    return {"Authorization": "Bearer " + tokens.create_access_token(user.id)}


async def make_order(session, user):
    order = Order(
        user_id=user.id,
        status=OrderStatus.delivered,
        items=[{"nominal_usd": 10, "quantity": 1, "price_rub": "999.00"}],
        total_amount_rub=Decimal("999.00"),
    )
    session.add(order)
    await session.commit()
    return order


async def test_create_ticket_with_own_order_persists_open_ticket(client, db_session, owner):
    order = await make_order(db_session, owner)
    response = await client.post(URL, json={**BODY, "order_id": order.id}, headers=headers(owner))
    assert response.status_code == 201
    data = response.json()
    assert data == {
        "id": data["id"],
        "order_id": order.id,
        **BODY,
        "status": "open",
        "created_at": data["created_at"],
    }
    assert data["id"] > 0
    assert datetime.fromisoformat(data["created_at"]).tzinfo is not None
    ticket = await db_session.get(SupportTicket, data["id"])
    assert ticket.user_id == owner.id
    assert ticket.order_id == order.id
    assert ticket.status is SupportTicketStatus.open
    assert ticket.message == BODY["message"]


@pytest.mark.parametrize("order_field", [{}, {"order_id": None}])
async def test_general_ticket_does_not_require_order(client, db_session, owner, order_field):
    response = await client.post(URL, json={**BODY, **order_field}, headers=headers(owner))
    assert response.status_code == 201
    assert response.json()["order_id"] is None
    assert await db_session.scalar(select(func.count()).select_from(SupportTicket)) == 1


async def test_foreign_and_missing_order_have_same_response(
    client, db_session, owner, other, monkeypatch
):
    order = await make_order(db_session, other)
    notify = AsyncMock()
    monkeypatch.setattr(support, "notify_support_ticket", notify)
    for order_id in (order.id, order.id + 100000):
        response = await client.post(
            URL, json={**BODY, "order_id": order_id}, headers=headers(owner)
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Order not found"}
    assert await db_session.scalar(select(func.count()).select_from(SupportTicket)) == 0
    notify.assert_not_awaited()


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("credential", ["missing", "malformed", "expired", "refresh", "inactive"])
async def test_authentication_required(client, db_session, owner, method, credential):
    request_headers = headers(owner)
    if credential == "missing":
        request_headers = {}
    elif credential == "malformed":
        request_headers = {"Authorization": "Bearer not-a-valid-jwt"}
    elif credential == "expired":
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": str(owner.id),
                "type": "access",
                "iat": now - timedelta(hours=2),
                "exp": now - timedelta(hours=1),
            },
            SECRET,
            algorithm="HS256",
        )
        request_headers = {"Authorization": "Bearer " + token}
    elif credential == "refresh":
        request_headers = {"Authorization": "Bearer " + tokens.create_refresh_token(owner.id)}
    else:
        owner.is_active = False
        await db_session.commit()
    kwargs = {"headers": request_headers}
    if method == "post":
        kwargs["json"] = BODY
    response = await getattr(client, method)(URL, **kwargs)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert await db_session.scalar(select(func.count()).select_from(SupportTicket)) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject", "abcd"),
        ("subject", "s" * 121),
        ("subject", "     "),
        ("message", "short msg"),
        ("message", "m" * 2001),
        ("message", " " * 20),
        ("subject", None),
        ("message", None),
        ("subject", 12345),
        ("order_id", 0),
        ("order_id", -1),
        ("order_id", 2**63),
        ("order_id", 1.5),
        ("order_id", True),
        ("order_id", "1"),
        ("user_id", 99),
        ("status", "resolved"),
        ("id", 123),
        ("created_at", "2026-01-01"),
    ],
)
async def test_invalid_payload_cannot_create_ticket(client, db_session, owner, field, value):
    response = await client.post(URL, json={**BODY, field: value}, headers=headers(owner))
    assert response.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(SupportTicket)) == 0


@pytest.mark.parametrize("subject,message", [("s" * 5, "m" * 10), ("s" * 120, "m" * 2000)])
async def test_valid_boundaries_and_whitespace_normalization(client, owner, subject, message):
    response = await client.post(
        URL,
        json={"subject": "  " + subject + "  ", "message": "\n" + message + "\t"},
        headers=headers(owner),
    )
    assert response.status_code == 201
    assert response.json()["subject"] == subject
    assert response.json()["message"] == message


async def test_list_isolated_paginated_and_includes_current_statuses(
    client, db_session, owner, other
):
    created = datetime(2026, 11, 1, tzinfo=timezone.utc)
    tickets = [
        SupportTicket(user_id=owner.id, **BODY, status=status, created_at=created)
        for status in SupportTicketStatus
    ]
    foreign = SupportTicket(
        user_id=other.id,
        subject="Чужое обращение",
        message="Приватное описание другого пользователя",
    )
    db_session.add_all([*tickets, foreign])
    await db_session.commit()
    response = await client.get(
        URL, params={"limit": 2, "offset": 1, "user_id": other.id}, headers=headers(owner)
    )
    assert response.status_code == 200
    assert response.json()["total"] == 4
    assert [item["id"] for item in response.json()["tickets"]] == [tickets[2].id, tickets[1].id]
    assert [item["status"] for item in response.json()["tickets"]] == ["resolved", "in_progress"]
    own = await client.get(URL, headers=headers(owner))
    assert {item["status"] for item in own.json()["tickets"]} == {
        status.value for status in SupportTicketStatus
    }
    assert foreign.message not in own.text
    another = await client.get(URL, headers=headers(other))
    assert another.json()["total"] == 1
    assert [item["id"] for item in another.json()["tickets"]] == [foreign.id]


async def test_list_empty_and_beyond_last_page(client, owner):
    for params in ({}, {"offset": 100}):
        response = await client.get(URL, params=params, headers=headers(owner))
        assert response.status_code == 200
        assert response.json() == {"tickets": [], "total": 0}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "bad"}])
async def test_invalid_pagination(client, owner, params):
    assert (await client.get(URL, params=params, headers=headers(owner))).status_code == 422


async def test_listing_has_constant_number_of_queries(client, db_engine, db_session, owner):
    db_session.add_all([SupportTicket(user_id=owner.id, **BODY) for _ in range(25)])
    await db_session.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "support_tickets" in statement:
            statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", capture)
    try:
        response = await client.get(URL, params={"limit": 25}, headers=headers(owner))
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    assert len(response.json()["tickets"]) == 25
    assert len(statements) == 2


async def test_notification_is_queued_as_background_task(db_session, owner, monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(support, "notify_support_ticket", notify)
    tasks = BackgroundTasks()
    ticket = await support.post_ticket(SupportTicketCreate(**BODY), owner, tasks, db_session)
    notify.assert_not_awaited()
    await tasks()
    notify.assert_awaited_once_with(ticket.id, owner.email, ticket.subject, None)


async def test_telegram_is_called_after_commit_with_expected_plain_text(
    client, db_engine, db_session, owner, monkeypatch
):
    order = await make_order(db_session, owner)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "support-test-bot-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "-100123456789")
    get_settings.cache_clear()
    captured = []
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def handler(request):
        async with maker() as session:
            ticket = await session.scalar(select(SupportTicket))
            assert ticket is not None
        captured.append(json.loads(request.content))
        assert request.method == "POST"
        assert str(request.url) == "https://api.telegram.org/botsupport-test-bot-token/sendMessage"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        alerts.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    response = await client.post(URL, json={**BODY, "order_id": order.id}, headers=headers(owner))
    assert response.status_code == 201
    assert captured == [
        {
            "chat_id": "-100123456789",
            "text": f"📩 Новый тикет поддержки #{response.json()['id']} от {owner.email}!\nТема: {BODY['subject']}\nЗаказ: #{order.id}",
        }
    ]


@pytest.mark.parametrize("token,chat", [("", ""), ("token", ""), ("", "123")])
async def test_unconfigured_telegram_is_skipped(monkeypatch, token, chat):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", chat)
    get_settings.cache_clear()
    telegram = AsyncMock()
    await alerts.notify_support_ticket(
        1, "user@example.com", BODY["subject"], None, client=telegram
    )
    telegram.post.assert_not_awaited()


@pytest.mark.parametrize("failure", ["http", "network", "timeout"])
async def test_telegram_failures_keep_ticket_and_do_not_leak_secrets(
    client, db_session, owner, monkeypatch, caplog, failure
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "private-support-bot-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "123")
    get_settings.cache_clear()

    def handler(request):
        if failure == "network":
            raise httpx.ConnectError("private-support-bot-token", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-support-bot-token", request=request)
        return httpx.Response(500, text="private-support-bot-token")

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        alerts.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    response = await client.post(URL, json=BODY, headers=headers(owner))
    assert response.status_code == 201
    assert await db_session.get(SupportTicket, response.json()["id"]) is not None
    assert "Could not send Telegram alert" in caplog.text
    assert "private-support-bot-token" not in caplog.text


async def test_failed_commit_rolls_back_and_does_not_schedule_alert(
    client, db_session, owner, monkeypatch
):
    notify = AsyncMock()
    monkeypatch.setattr(support, "notify_support_ticket", notify)
    monkeypatch.setattr(
        AsyncSession, "commit", AsyncMock(side_effect=SQLAlchemyError("private storage details"))
    )
    response = await client.post(URL, json=BODY, headers=headers(owner))
    assert response.status_code == 503
    assert response.json() == {"detail": "Support tickets are unavailable"}
    assert await db_session.scalar(select(func.count()).select_from(SupportTicket)) == 0
    notify.assert_not_awaited()


async def test_list_storage_failure_returns_safe_error(client, owner, monkeypatch):
    monkeypatch.setattr(
        "app.services.support_tickets.tickets_repo.list_for_user",
        AsyncMock(side_effect=SQLAlchemyError("private storage details")),
    )
    response = await client.get(URL, headers=headers(owner))
    assert response.status_code == 503
    assert response.json() == {"detail": "Support tickets are unavailable"}


def test_support_openapi_documents_bearer_routes_and_validation():
    schema = create_app().openapi()
    assert "support" in {tag["name"] for tag in schema["tags"]}
    for method, code in (("post", "201"), ("get", "200")):
        route = schema["paths"][URL][method]
        assert route["tags"] == ["support"]
        assert route["security"] == [{"BearerAuth": []}]
        assert {code, "401", "422", "503"} <= route["responses"].keys()
    assert "404" in schema["paths"][URL]["post"]["responses"]
    fields = schema["components"]["schemas"]["SupportTicketCreate"]["properties"]
    assert (fields["subject"]["minLength"], fields["subject"]["maxLength"]) == (5, 120)
    assert (fields["message"]["minLength"], fields["message"]["maxLength"]) == (10, 2000)
    assert schema["components"]["schemas"]["SupportTicketStatus"]["enum"] == [
        "open",
        "in_progress",
        "resolved",
        "closed",
    ]
