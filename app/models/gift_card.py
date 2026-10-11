from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    Numeric,
    SmallInteger,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import GiftCardStatus


class GiftCard(Base):
    __tablename__ = "gift_cards"
    __table_args__ = (
        CheckConstraint(
            "(status IN ('reserved', 'sold') AND order_id IS NOT NULL "
            "AND sale_price_rub IS NOT NULL AND sale_price_rub > 0) "
            "OR (status IN ('available', 'blocked') AND order_id IS NULL "
            "AND sale_price_rub IS NULL)",
            name="gift_cards_status_order_sale_check",
        ),
        Index("ix_gift_cards_nominal_usd_status", "nominal_usd", "status"),
        Index("ix_gift_cards_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    nominal_usd: Mapped[int] = mapped_column(
        SmallInteger,
        ForeignKey(
            "prices.nominal_usd",
            ondelete="RESTRICT",
            deferrable=True,
            initially="IMMEDIATE",
        ),
        nullable=False,
    )
    code_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[GiftCardStatus] = mapped_column(
        Enum(GiftCardStatus, name="gift_card_status", native_enum=True),
        server_default=text("'available'::gift_card_status"),
        nullable=False,
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "orders.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="IMMEDIATE",
        ),
    )
    sale_price_rub: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
