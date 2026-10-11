from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus
from app.models.order import Order
from app.repositories import orders as orders_repo
from app.repositories import prices as prices_repo
from app.repositories import users as users_repo
from app.services.inventory import reserve_gift_cards_for_order


class NominalNotForSaleError(Exception):
    def __init__(self, nominal_usd: int) -> None:
        self.nominal_usd = nominal_usd
        super().__init__(f"nominal_usd {nominal_usd} is not for sale")


def _price_rub_json(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.01')):.2f}"


async def create_one_time_order(
    session: AsyncSession,
    email: str,
    nominal_usd: int,
) -> int:
    price = await prices_repo.get_by_nominal(session, nominal_usd)
    if price is None:
        raise NominalNotForSaleError(nominal_usd)

    user = await users_repo.get_or_create(session, email.strip().lower())
    order = await orders_repo.add(
        session,
        Order(
            user_id=user.id,
            status=OrderStatus.pending_payment,
            items=[
                {
                    "nominal_usd": nominal_usd,
                    "quantity": 1,
                    "price_rub": _price_rub_json(price.price_rub),
                }
            ],
            total_amount_rub=price.price_rub,
        ),
    )
    try:
        await reserve_gift_cards_for_order(session, order)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return order.id
