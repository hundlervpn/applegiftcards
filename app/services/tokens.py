from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt

from app.config import get_settings

TokenType = Literal["access", "refresh"]


class TokenDecodeError(Exception):
    pass


class TokenExpiredError(TokenDecodeError):
    pass


def create_access_token(user_id: int) -> str:
    return _create_token(user_id, "access")


def create_refresh_token(user_id: int) -> str:
    return _create_token(user_id, "refresh")


def issue_token_pair(user_id: int) -> tuple[str, str]:
    return create_access_token(user_id), create_refresh_token(user_id)


def decode_access_token(token: str) -> int:
    return decode_token(token, expected_type="access")


def decode_refresh_token(token: str) -> int:
    return decode_token(token, expected_type="refresh")


def decode_token(token: str, expected_type: TokenType) -> int:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenDecodeError("Invalid token") from exc

    if payload.get("type") != expected_type:
        raise TokenDecodeError("Invalid token type")
    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenDecodeError("Invalid token subject") from exc


def _create_token(user_id: int, token_type: TokenType) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    lifetime = (
        timedelta(minutes=settings.access_token_expire_minutes)
        if token_type == "access"
        else timedelta(days=settings.refresh_token_expire_days)
    )
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "iat": now,
        "exp": now + lifetime,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
