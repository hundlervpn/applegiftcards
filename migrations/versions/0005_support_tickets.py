"""Add support tickets (SCRUM-65)."""

import sqlalchemy as sa
from alembic import op

revision: str = "0005_support_tickets"
down_revision: str | None = "0004_subscriptions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("subject", sa.String(120), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "in_progress", "resolved", "closed", name="support_ticket_status"),
            server_default=sa.text("'open'::support_ticket_status"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "char_length(btrim(subject)) BETWEEN 5 AND 120",
            name="support_tickets_subject_length_check",
        ),
        sa.CheckConstraint(
            "char_length(btrim(message)) BETWEEN 10 AND 2000",
            name="support_tickets_message_length_check",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", deferrable=True, initially="IMMEDIATE"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], ondelete="RESTRICT", deferrable=True, initially="IMMEDIATE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_support_tickets_user_created", "support_tickets", ["user_id", "created_at", "id"]
    )
    op.create_index("ix_support_tickets_order_id", "support_tickets", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_support_tickets_order_id", table_name="support_tickets")
    op.drop_index("ix_support_tickets_user_created", table_name="support_tickets")
    op.drop_table("support_tickets")
    sa.Enum(name="support_ticket_status").drop(op.get_bind(), checkfirst=True)
