from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import SubscriptionStatus


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "dunning_attempts >= 0",
            name="subscriptions_dunning_attempts_check",
        ),
        CheckConstraint(
            "char_length(btrim(payment_method_id)) > 0",
            name="subscriptions_payment_method_id_check",
        ),
        Index("ix_subscriptions_user_id", "user_id"),
        Index("ix_subscriptions_status_next_billing_at", "status", "next_billing_at"),
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
    payment_method_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status", native_enum=True),
        nullable=False,
        default=SubscriptionStatus.active,
        server_default=text("'active'::subscription_status"),
    )
    next_billing_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dunning_attempts: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
