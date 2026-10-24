from pydantic import BaseModel, EmailStr, Field, HttpUrl, field_validator

from app.models.price import ALLOWED_NOMINALS


class OneTimeOrderCreate(BaseModel):
    email: EmailStr = Field(max_length=254)
    nominal_usd: int
    is_subscription: bool = False

    @field_validator("nominal_usd")
    @classmethod
    def nominal_is_allowed(cls, value: int) -> int:
        if value not in ALLOWED_NOMINALS:
            allowed = ", ".join(str(item) for item in ALLOWED_NOMINALS)
            raise ValueError(f"must be one of: {allowed}")
        return value


class OneTimeOrderResponse(BaseModel):
    order_id: int
    payment_url: HttpUrl
