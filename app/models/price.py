from decimal import Decimal

from sqlalchemy import CheckConstraint, Numeric, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_ALLOWED_NOMINALS = (1, 2, 3, 4, 5, 10, 15, 20, 25, 30, 40, 50, 100, 200)


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (
        CheckConstraint(
            f"nominal_usd IN ({', '.join(str(n) for n in _ALLOWED_NOMINALS)})",
            name="prices_nominal_usd_check",
        ),
        CheckConstraint("price_rub > 0", name="prices_price_rub_check"),
    )

    nominal_usd: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    price_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
