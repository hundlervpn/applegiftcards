from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import OrderStatus
from app.money import Rub


class GiftCardItem(BaseModel):
    nominal_usd: int
    code: str


class OrderHistoryItem(BaseModel):
    id: int
    status: OrderStatus
    total_amount_rub: Rub
    created_at: datetime
    paid_at: datetime | None = None
    cards: list[GiftCardItem] = Field(default_factory=list)


class OrderHistoryResponse(BaseModel):
    orders: list[OrderHistoryItem]
    total: int = Field(ge=0)
