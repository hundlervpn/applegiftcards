from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import OrderStatus
from app.money import Rub


class GiftCardItem(BaseModel):
    nominal_usd: int = Field(description="Номинал подарочной карты в долларах США", examples=[10])
    code: str = Field(
        description="Расшифрованный код активации купленной карты",
        examples=["DEMO-CODE-DO-NOT-REDEEM"],
    )


class OrderHistoryItem(BaseModel):
    id: int = Field(description="Идентификатор заказа", examples=[123])
    status: OrderStatus = Field(description="Текущий статус заказа")
    total_amount_rub: Rub = Field(description="Итоговая стоимость заказа в рублях", examples=["1500.00"])
    created_at: datetime = Field(description="Дата создания заказа с часовым поясом")
    paid_at: datetime | None = Field(default=None, description="Дата оплаты с часовым поясом или null")
    cards: list[GiftCardItem] = Field(
        default_factory=list,
        description="Купленные карты для paid/delivered; для остальных статусов список пуст",
    )


class OrderHistoryResponse(BaseModel):
    orders: list[OrderHistoryItem] = Field(description="Страница заказов текущего пользователя")
    total: int = Field(ge=0, description="Количество всех заказов пользователя до пагинации", examples=[1])
