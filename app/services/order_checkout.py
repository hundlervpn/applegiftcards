from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.orders import create_one_time_order
from app.services.payments import create_payment


@dataclass(frozen=True)
class OneTimeOrderCheckout:
    order_id: int
    payment_url: str


async def create_one_time_order_checkout(
    session: AsyncSession,
    email: str,
    nominal_usd: int,
    settings: Settings,
) -> OneTimeOrderCheckout:
    try:
        order = await create_one_time_order(
            session,
            email=email,
            nominal_usd=nominal_usd,
        )
        payment_session = await create_payment(
            shop_id=settings.yookassa_shop_id,
            secret_key=settings.yookassa_secret_key,
            order_id=order.id,
            amount_rub=order.total_amount_rub,
            return_url=settings.payment_return_url,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return OneTimeOrderCheckout(
        order_id=order.id,
        payment_url=str(payment_session.payment_url),
    )
