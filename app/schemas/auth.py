from pydantic import BaseModel, EmailStr, Field, field_validator

# bcrypt учитывает только первые 72 байта пароля, поэтому ограничиваем заранее.
PASSWORD_MAX_BYTES = 72
PASSWORD_MIN_LENGTH = 8


def _normalize_email(value: object) -> object:
    if isinstance(value, str):
        return value.strip().lower()
    return value


class UserRegister(BaseModel):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)

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
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return _normalize_email(value)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)
