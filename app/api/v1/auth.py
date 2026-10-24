from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openapi import (
    EMAIL_CONFLICT_RESPONSE,
    INVALID_CREDENTIALS_RESPONSE,
    INVALID_REFRESH_RESPONSE,
    TOKEN_PAIR_RESPONSE,
)
from app.db.session import get_db
from app.schemas.auth import RefreshRequest, TokenResponse, UserLogin, UserRegister
from app.services import auth as auth_service
from app.services.auth import EmailAlreadyRegisteredError, InvalidCredentialsError, InvalidRefreshTokenError
from app.services.tokens import TokenDecodeError, TokenExpiredError

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=201,
    response_model=TokenResponse,
    summary="Регистрация пользователя",
    description=(
        "Создаёт пользователя и выдаёт пару JWT access/refresh. Email нормализуется: "
        "пробелы по краям удаляются, символы приводятся к нижнему регистру. "
        "Пароль содержит минимум 8 символов, хотя бы одну букву и цифру, не более 72 байт UTF-8. "
        "Сроки действия токенов задаются настройками сервиса."
    ),
    responses={201: TOKEN_PAIR_RESPONSE, 409: EMAIL_CONFLICT_RESPONSE},
)
async def register(
    body: UserRegister,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    try:
        _, access_token, refresh_token = await auth_service.register_user(
            db,
            email=str(body.email),
            password=body.password,
        )
    except EmailAlreadyRegisteredError as exc:
        raise HTTPException(
            status_code=409,
            detail="User with this email already exists",
        ) from exc
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Вход по email и паролю",
    description=(
        "Проверяет email и пароль и выдаёт пару JWT access/refresh. Email нормализуется. "
        "Неверный пароль, неизвестный email и неактивный пользователь дают одинаковый ответ 401. "
        "Access-токен используется для запросов к личному кабинету через BearerAuth."
    ),
    responses={200: TOKEN_PAIR_RESPONSE, 401: INVALID_CREDENTIALS_RESPONSE},
)
async def login(
    body: UserLogin,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    try:
        _, access_token, refresh_token = await auth_service.login_user(
            db,
            email=str(body.email),
            password=body.password,
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Обновление пары JWT",
    description=(
        "Принимает refresh_token в JSON и выпускает пару access/refresh для активного пользователя. "
        "Заголовок Authorization не требуется. Access-токен нельзя использовать вместо refresh-токена. "
        "Ранее выданные токены не отзываются этим запросом и действуют до своего срока истечения."
    ),
    responses={200: TOKEN_PAIR_RESPONSE, 401: INVALID_REFRESH_RESPONSE},
)
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    try:
        _, access_token, refresh_token = await auth_service.refresh_token_pair(
            db,
            refresh_token=body.refresh_token,
        )
    except TokenExpiredError as exc:
        raise HTTPException(status_code=401, detail="Token has expired") from exc
    except (TokenDecodeError, InvalidRefreshTokenError) as exc:
        raise HTTPException(status_code=401, detail="Invalid refresh token") from exc
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)
