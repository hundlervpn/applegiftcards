from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import OrderStatus


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total_amount_rub > 0", name="orders_total_amount_rub_check"),
        CheckConstraint(
            "jsonb_typeof(items) = 'array' AND items <> '[]'::jsonb",
            name="orders_items_check",
        ),
        Index("ix_orders_user_id", "user_id"),
        Index("ix_orders_status_reserved_until", "status", "reserved_until"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="IMMEDIATE",
        ),
        nullable=False,
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", native_enum=True),
        server_default=text("'pending_payment'::order_status"),
        nullable=False,
    )
    items: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    total_amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reserved_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now() + interval '15 minutes'"),
        nullable=False,
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
