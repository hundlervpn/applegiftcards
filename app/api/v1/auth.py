from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.auth import RefreshRequest, TokenResponse, UserLogin, UserRegister
from app.services import auth as auth_service
from app.services.auth import EmailAlreadyRegisteredError, InvalidCredentialsError, InvalidRefreshTokenError
from app.services.tokens import TokenDecodeError, TokenExpiredError

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201, response_model=TokenResponse)
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


@router.post("/login", response_model=TokenResponse)
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


@router.post("/refresh", response_model=TokenResponse)
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
