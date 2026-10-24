from pydantic import BaseModel, EmailStr, Field, field_validator

# bcrypt учитывает только первые 72 байта пароля, поэтому ограничиваем заранее.
PASSWORD_MAX_BYTES = 72
PASSWORD_MIN_LENGTH = 8


def _normalize_email(value: object) -> object:
    if isinstance(value, str):
        return value.strip().lower()
    return value


class UserRegister(BaseModel):
    email: EmailStr = Field(
        max_length=254,
        description="Email пользователя; пробелы по краям удаляются, регистр приводится к нижнему",
        examples=["buyer@example.com"],
    )
    password: str = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=128,
        description="Не менее 8 символов, минимум одна буква и цифра, не более 72 байт UTF-8",
        examples=["DemoPass123"],
        json_schema_extra={"format": "password", "writeOnly": True},
    )

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return _normalize_email(value)

    @field_validator("password")
    @classmethod
    def password_is_strong(cls, value: str) -> str:
        if len(value.encode("utf-8")) > PASSWORD_MAX_BYTES:
            raise ValueError(f"password must be at most {PASSWORD_MAX_BYTES} bytes long")
        if not any(ch.isalpha() for ch in value):
            raise ValueError("password must contain at least one letter")
        if not any(ch.isdigit() for ch in value):
            raise ValueError("password must contain at least one digit")
        return value


class UserLogin(BaseModel):
    email: EmailStr = Field(
        max_length=254,
        description="Email зарегистрированного пользователя; нормализуется перед проверкой",
        examples=["buyer@example.com"],
    )
    password: str = Field(
        min_length=1,
        max_length=128,
        description="Пароль пользователя",
        examples=["DemoPass123"],
        json_schema_extra={"format": "password", "writeOnly": True},
    )

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return _normalize_email(value)


class TokenResponse(BaseModel):
    access_token: str = Field(description="JWT для доступа к /me/* через заголовок Authorization: Bearer")
    refresh_token: str = Field(description="JWT для обновления пары токенов через /auth/refresh")
    token_type: str = Field(default="bearer", description="Схема авторизации для access-токена")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(
        min_length=1,
        description="Refresh-токен из ответа register, login или refresh; access-токен не подходит",
        examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.demo-refresh.signature"],
    )
