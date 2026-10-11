from decimal import Decimal

from pydantic import BaseModel, Field, computed_field


class CatalogItemResponse(BaseModel):
    nominal_usd: int
    price_rub: Decimal
    available_count: int = Field(ge=0)

    @computed_field
    @property
    def is_available(self) -> bool:
        return self.available_count > 0
