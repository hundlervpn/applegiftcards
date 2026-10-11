"""initial

Revision ID: dfbeac00cd2c
Revises:
Create Date: 2026-10-11 19:07:50.597437

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "dfbeac00cd2c"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "prices",
        sa.Column("nominal_usd", sa.SmallInteger(), nullable=False),
        sa.Column("price_rub", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.CheckConstraint(
            "nominal_usd IN (1, 2, 3, 4, 5, 10, 15, 20, 25, 30, 40, 50, 100, 200)",
            name="prices_nominal_usd_check",
        ),
        sa.CheckConstraint("price_rub > 0", name="prices_price_rub_check"),
        sa.PrimaryKeyConstraint("nominal_usd"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending_payment",
                "paid",
                "completed",
                "cancelled",
                name="order_status",
            ),
            server_default=sa.text("'pending_payment'::order_status"),
            nullable=False,
        ),
        sa.Column(
            "items",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment='[{"nominal_usd":10,"quantity":2,"price_rub":1000.00}]',
        ),
        sa.Column("total_amount_rub", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "reserved_until",
            sa.DateTime(timezone=True),
            server_default=sa.text("now() + interval '15 minutes'"),
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(items) = 'array' AND items <> '[]'::jsonb",
            name="orders_items_check",
        ),
        sa.CheckConstraint("total_amount_rub > 0", name="orders_total_amount_rub_check"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            initially="IMMEDIATE",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orders_status_reserved_until", "orders", ["status", "reserved_until"])
    op.create_index("ix_orders_user_id", "orders", ["user_id"])
    op.create_table(
        "gift_cards",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("nominal_usd", sa.SmallInteger(), nullable=False),
        sa.Column("code_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("available", "reserved", "sold", "blocked", name="gift_card_status"),
            server_default=sa.text("'available'::gift_card_status"),
            nullable=False,
        ),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("sale_price_rub", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status IN ('reserved', 'sold') AND order_id IS NOT NULL "
            "AND sale_price_rub IS NOT NULL AND sale_price_rub > 0) "
            "OR (status IN ('available', 'blocked') AND order_id IS NULL "
            "AND sale_price_rub IS NULL)",
            name="gift_cards_status_order_sale_check",
        ),
        sa.ForeignKeyConstraint(
            ["nominal_usd"],
            ["prices.nominal_usd"],
            ondelete="RESTRICT",
            initially="IMMEDIATE",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            ondelete="RESTRICT",
            initially="IMMEDIATE",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gift_cards_nominal_usd_status", "gift_cards", ["nominal_usd", "status"])
    op.create_index("ix_gift_cards_order_id", "gift_cards", ["order_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_gift_cards_order_id", table_name="gift_cards")
    op.drop_index("ix_gift_cards_nominal_usd_status", table_name="gift_cards")
    op.drop_table("gift_cards")
    op.drop_index("ix_orders_user_id", table_name="orders")
    op.drop_index("ix_orders_status_reserved_until", table_name="orders")
    op.drop_table("orders")
    op.drop_table("users")
    op.drop_table("prices")
    sa.Enum(name="gift_card_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="order_status").drop(op.get_bind(), checkfirst=True)
