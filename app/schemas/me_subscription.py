from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import SubscriptionStatus
from app.money import Rub


class SubscriptionItem(BaseModel):
    id: int
    nominal_usd: int
    status: SubscriptionStatus
    card_mask: str | None = Field(default=None, pattern=r"^\*{4} [0-9]{4}$")
    next_billing_at: datetime
    price_rub: Rub
    created_at: datetime


class SubscriptionListResponse(BaseModel):
    subscriptions: list[SubscriptionItem]
