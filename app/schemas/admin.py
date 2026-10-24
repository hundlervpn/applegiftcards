from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.models.price import ALLOWED_NOMINALS

MAX_CODES_PER_REQUEST = 1000
MAX_CODE_LENGTH = 128

GiftCardCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CODE_LENGTH),
]


class GiftCardBatchItem(BaseModel):
    nominal_usd: int = Field(strict=True)
    codes: list[GiftCardCode] = Field(min_length=1, max_length=MAX_CODES_PER_REQUEST)

    @field_validator("nominal_usd")
    @classmethod
    def nominal_is_allowed(cls, value: int) -> int:
        if value not in ALLOWED_NOMINALS:
            allowed = ", ".join(str(item) for item in ALLOWED_NOMINALS)
            raise ValueError(f"must be one of: {allowed}")
        return value


class GiftCardBatchCreate(BaseModel):
    cards: list[GiftCardBatchItem] = Field(min_length=1, max_length=MAX_CODES_PER_REQUEST)

    @field_validator("cards")
    @classmethod
    def total_codes_within_limit(cls, value: list[GiftCardBatchItem]) -> list[GiftCardBatchItem]:
        total = sum(len(item.codes) for item in value)
        if total > MAX_CODES_PER_REQUEST:
            raise ValueError(f"batch must contain at most {MAX_CODES_PER_REQUEST} codes")
        return value


class GiftCardBatchResponse(BaseModel):
    added: int = Field(ge=0)


class NominalStats(BaseModel):
    nominal_usd: int
    available: int = Field(ge=0)
    reserved: int = Field(ge=0)
    sold: int = Field(ge=0)


class AdminStatsResponse(BaseModel):
    nominals: list[NominalStats]
