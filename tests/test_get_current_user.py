import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import jwt as pyjwt
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.session import get_db
from app.dependencies import CurrentUser, get_current_user
from app.models.user import User
from app.services import tokens


def make_user(user_id: int = 1, is_active: bool = True) -> User:
    return User(
        id=user_id,
        email="buyer@example.com",
        hashed_password=None,
        is_active=is_active,
    )


def make_session(user: User | None) -> AsyncMock:
    session = AsyncMock()
    session.get = AsyncMock(return_value=user)
    return session


class GetCurrent_userDirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_active_user_for_valid_access_token(self) -> None:
        user = make_user(user_id=42)
        token = tokens.create_access_token(42)

        result = await get_current_user(
            credentials=self._credentials(token),
            session=make_session(user),
        )

        self.assertIs(result, user)

    async def test_missing_header_raises_401(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(credentials=None, session=make_session(None))
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_garbage_token_raises_401_invalid_token(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(
                credentials=self._credentials("not-a-jwt"),
                session=make_session(None),
            )
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Invalid token")

    async def test_expired_token_raises_401_token_has_expired(self) -> None:
        settings = get_settings()
        now = datetime.now(timezone.utc)
        expired = pyjwt.encode(
            {
                "sub": "1",
                "type": "access",
                "iat": now - timedelta(minutes=2),
                "exp": now - timedelta(minutes=1),
            },
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )

        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(
                credentials=self._credentials(expired),
                session=make_session(make_user()),
            )
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Token has expired")

    async def test_refresh_token_used_as_access_raises_401(self) -> None:
        refresh = tokens.create_refresh_token(1)

        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(
                credentials=self._credentials(refresh),
                session=make_session(make_user()),
            )
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_unknown_user_raises_401(self) -> None:
        token = tokens.create_access_token(999)

        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(
                credentials=self._credentials(token),
                session=make_session(None),
            )
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_inactive_user_raises_401(self) -> None:
        token = tokens.create_access_token(1)

        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(
                credentials=self._credentials(token),
                session=make_session(make_user(is_active=False)),
            )
        self.assertEqual(ctx.exception.status_code, 401)

    @staticmethod
    def _credentials(token: str):
        from fastapi.security import HTTPAuthorizationCredentials

        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class GetCurrent_userEndpointTests(unittest.TestCase):
    """Проверка wiring: зависимость работает как Annotated[User, Depends(...)]."""

    def setUp(self) -> None:
        self.user = make_user(user_id=7)
        app = FastAPI()

        @app.get("/me")
        async def me(user: CurrentUser) -> dict:
            return {"email": user.email}

        async def override_db():
            yield make_session(self.user)

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_bearer_header_returns_current_user(self) -> None:
        token = tokens.create_access_token(7)
        response = self.client.get(
            "/me",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"email": "buyer@example.com"})

    def test_without_header_returns_401(self) -> None:
        response = self.client.get("/me")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def test_with_garbage_header_returns_401(self) -> None:
        response = self.client.get(
            "/me",
            headers={"Authorization": "Bearer garbage"},
        )

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
