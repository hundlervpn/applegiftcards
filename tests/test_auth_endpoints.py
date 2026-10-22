from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from sqlalchemy import select

from app.config import get_settings
from app.models.user import User
from app.services import tokens

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"

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
