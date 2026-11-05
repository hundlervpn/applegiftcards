from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import SupportTicketStatus


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(subject)) BETWEEN 5 AND 120",
            name="support_tickets_subject_length_check",
        ),
        CheckConstraint(
            "char_length(btrim(message)) BETWEEN 10 AND 2000",
            name="support_tickets_message_length_check",
        ),
        Index("ix_support_tickets_user_created", "user_id", "created_at", "id"),
        Index("ix_support_tickets_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT", deferrable=True, initially="IMMEDIATE"),
        nullable=False,
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="RESTRICT", deferrable=True, initially="IMMEDIATE"),
    )
    subject: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SupportTicketStatus] = mapped_column(
        Enum(SupportTicketStatus, name="support_ticket_status", native_enum=True),
        nullable=False,
        default=SupportTicketStatus.open,
        server_default=text("'open'::support_ticket_status"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
