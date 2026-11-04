import time
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import get_settings
from app.db.session import get_db
from app.main import create_app
from app.models.user import User
from app.rate_limit import client_ip
from app.services import tokens

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
ORDER_URL = "/api/v1/orders/one-time"

PASSWORD = "sup3r-secret"
GENERIC_401_DETAIL = "Invalid email or password"


async def test_register_returns_201_tokens_and_normalizes_email(client, db_session):
    response = await client.post(
        REGISTER_URL,
        json={"email": f"  NewUser@Example.COM  ", "password": PASSWORD},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]

    user = await db_session.scalar(select(User).where(User.email == "newuser@example.com"))
    assert user is not None, "email должен сохраниться нормализованным (.strip().lower())"
    assert user.hashed_password is not None
    assert user.hashed_password != PASSWORD
    assert user.is_active is True


async def test_register_duplicate_email_returns_409(client):
    first = await client.post(REGISTER_URL, json={"email": "dup@example.com", "password": PASSWORD})
    second = await client.post(REGISTER_URL, json={"email": "DUP@example.com", "password": PASSWORD})

    assert first.status_code == 201
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


async def test_register_rejects_weak_passwords_422(client):
    for password in ("short1", "nodigitsatall", "12345678"):
        response = await client.post(
            REGISTER_URL,
            json={"email": "weak@example.com", "password": password},
        )
        assert response.status_code == 422, password


async def test_login_returns_200_and_tokens(client):
    await client.post(REGISTER_URL, json={"email": "login@example.com", "password": PASSWORD})

    response = await client.post(
        LOGIN_URL,
        json={"email": "Login@Example.com", "password": PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert tokens.decode_access_token(body["access_token"]) >= 1


async def test_login_wrong_password_returns_401(client):
    await client.post(REGISTER_URL, json={"email": "wrongpw@example.com", "password": PASSWORD})

    response = await client.post(
        LOGIN_URL,
        json={"email": "wrongpw@example.com", "password": "other-passw0rd"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_401_DETAIL


async def test_login_unknown_email_returns_same_401_without_disclosure(client):
    known = await client.post(
        LOGIN_URL,
        json={"email": "wrongpw@example.com", "password": "other-passw0rd"},
    )
    unknown = await client.post(
        LOGIN_URL,
        json={"email": "nobody@example.com", "password": "other-passw0rd"},
    )

    assert known.status_code == unknown.status_code == 401
    assert known.json()["detail"] == unknown.json()["detail"]


async def test_login_inactive_user_returns_401(client, db_session):
    await client.post(REGISTER_URL, json={"email": "inactive@example.com", "password": PASSWORD})
    user = await db_session.scalar(select(User).where(User.email == "inactive@example.com"))
    user.is_active = False
    await db_session.commit()

    response = await client.post(
        LOGIN_URL,
        json={"email": "inactive@example.com", "password": PASSWORD},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == GENERIC_401_DETAIL


async def test_refresh_returns_new_token_pair(client):
    registered = await client.post(REGISTER_URL, json={"email": "refresh@example.com", "password": PASSWORD})

    response = await client.post(REFRESH_URL, json={"refresh_token": registered.json()["refresh_token"]})

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    user_id = tokens.decode_access_token(body["access_token"])
    assert tokens.decode_refresh_token(body["refresh_token"]) == user_id


async def test_refresh_rejects_access_token_as_refresh(client):
    registered = await client.post(REGISTER_URL, json={"email": "wrongtype@example.com", "password": PASSWORD})

    response = await client.post(REFRESH_URL, json={"refresh_token": registered.json()["access_token"]})

    assert response.status_code == 401


async def test_refresh_rejects_garbage_token(client):
    response = await client.post(REFRESH_URL, json={"refresh_token": "not-a-jwt"})

    assert response.status_code == 401


async def test_refresh_rejects_expired_token_with_token_has_expired(client):
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expired = pyjwt.encode(
        {
            "sub": "1",
            "type": "refresh",
            "iat": now - timedelta(minutes=2),
            "exp": now - timedelta(minutes=1),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    response = await client.post(REFRESH_URL, json={"refresh_token": expired})

    assert response.status_code == 401
    assert response.json()["detail"] == "Token has expired"


def assert_too_many_requests(response, limit: str) -> None:
    assert response.status_code == 429
    assert response.json() == {"detail": "Too Many Requests"}
    assert response.headers["x-ratelimit-limit"] == limit
    assert response.headers["x-ratelimit-remaining"] == "0"
    retry_after = int(response.headers["retry-after"])
    assert 1 <= retry_after <= 60
    assert int(response.headers["x-ratelimit-reset"]) >= int(time.time())


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/auth/login",
            "raw_path": b"/api/v1/auth/login",
            "query_string": b"",
            "headers": headers,
            "client": (peer, 12345),
            "server": ("test", 80),
        }
    )


def test_forwarded_for_is_ignored_when_peer_is_not_a_trusted_proxy():
    assert client_ip(_request("203.0.113.50", "198.51.100.77")) == "203.0.113.50"


def test_forwarded_for_uses_the_client_nginx_accepted():
    assert client_ip(_request("127.0.0.1", "198.51.100.1, 203.0.113.10")) == "203.0.113.10"
    assert client_ip(_request("172.18.0.1", "203.0.113.10")) == "203.0.113.10"
    assert client_ip(_request("172.18.0.1")) == "172.18.0.1"


async def test_register_rate_limit_returns_429_with_headers(client):
    for index in range(3):
        response = await client.post(
            REGISTER_URL,
            json={"email": f"rate-limit-{index}@example.com", "password": PASSWORD},
        )
        assert response.status_code == 201, response.text

    blocked = await client.post(
        REGISTER_URL,
        json={"email": "rate-limit-blocked@example.com", "password": PASSWORD},
    )
    assert_too_many_requests(blocked, "3")


async def test_login_rate_limit_returns_429(client):
    payload = {"email": "nobody-rate@example.com", "password": "other-passw0rd"}
    for _ in range(5):
        response = await client.post(LOGIN_URL, json=payload)
        assert response.status_code == 401

    blocked = await client.post(LOGIN_URL, json=payload)
    assert_too_many_requests(blocked, "5")


async def test_order_create_rate_limit_returns_429(client):
    for _ in range(10):
        response = await client.post(ORDER_URL, json={})
        assert response.status_code == 422

    blocked = await client.post(ORDER_URL, json={})
    assert_too_many_requests(blocked, "10")


async def test_rate_limit_uses_client_ip_from_x_forwarded_for(client):
    real_client = "203.0.113.10"
    other_client = "203.0.113.20"
    # Клиент дописал чужой адрес, Nginx добавил настоящий справа.
    chain = f"198.51.100.8, {real_client}"

    for index in range(3):
        response = await client.post(
            REGISTER_URL,
            json={"email": f"fwd-{index}@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": chain},
        )
        assert response.status_code == 201, response.text

    blocked = await client.post(
        REGISTER_URL,
        json={"email": "fwd-blocked@example.com", "password": PASSWORD},
        headers={"X-Forwarded-For": real_client},
    )
    assert_too_many_requests(blocked, "3")

    spoofed_only = await client.post(
        REGISTER_URL,
        json={"email": "fwd-spoof@example.com", "password": PASSWORD},
        headers={"X-Forwarded-For": "198.51.100.8"},
    )
    assert spoofed_only.status_code == 201

    other = await client.post(
        REGISTER_URL,
        json={"email": "fwd-other@example.com", "password": PASSWORD},
        headers={"X-Forwarded-For": other_client},
    )
    assert other.status_code == 201
    login = await client.post(
        LOGIN_URL,
        json={"email": "fwd-other@example.com", "password": PASSWORD},
        headers={"X-Forwarded-For": other_client},
    )
    assert login.status_code == 200


async def test_exceeded_register_does_not_open_db_session():
    opened = 0

    async def override_get_db():
        nonlocal opened
        opened += 1
        # yield оставляет зависимость генератором, как настоящий get_db.
        # До yield запрос уже дошёл до сессии — лимит этого не делает.
        if opened:
            raise HTTPException(status_code=503, detail="db opened")
        yield None

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        for index in range(3):
            response = await http.post(
                REGISTER_URL,
                json={"email": f"nodb-{index}@example.com", "password": PASSWORD},
            )
            assert response.status_code == 503
        blocked = await http.post(
            REGISTER_URL,
            json={"email": "nodb-blocked@example.com", "password": PASSWORD},
        )
    assert_too_many_requests(blocked, "3")
    assert opened == 3
