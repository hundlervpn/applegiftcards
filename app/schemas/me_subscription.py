from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import SubscriptionStatus
from app.money import Rub


class SubscriptionItem(BaseModel):
    id: int = Field(description="Идентификатор подписки", examples=[42])
    nominal_usd: int = Field(description="Номинал карты в долларах США", examples=[10])
    status: SubscriptionStatus = Field(description="Текущий статус: active, past_due или cancelled")
    card_mask: str | None = Field(
        default=None,
        pattern=r"^\*{4} [0-9]{4}$",
        description="Только маска банковской карты; null, если она недоступна или способ оплаты не карточный",
        examples=["**** 4242"],
    )
    next_billing_at: datetime = Field(description="Дата следующего автосписания с часовым поясом")
    price_rub: Rub = Field(description="Текущая цена номинала в рублях", examples=["1500.00"])
    created_at: datetime = Field(description="Дата создания подписки с часовым поясом")


class SubscriptionListResponse(BaseModel):
    subscriptions: list[SubscriptionItem] = Field(description="Все подписки текущего пользователя")
