"""Add subscriptions table for recurring billing.

Revision ID: 0004_subscriptions
Revises: 0003_scrum34
Create Date: 2026-10-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004_subscriptions"
down_revision: Union[str, Sequence[str], None] = "0003_scrum34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("nominal_usd", sa.SmallInteger(), nullable=False),
        sa.Column("payment_method_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "past_due", "cancelled", name="subscription_status"),
            server_default=sa.text("'active'::subscription_status"),
            nullable=False,
        ),
        sa.Column("next_billing_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "dunning_attempts",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "dunning_attempts >= 0",
            name="subscriptions_dunning_attempts_check",
        ),
        sa.CheckConstraint(
            "char_length(btrim(payment_method_id)) > 0",
            name="subscriptions_payment_method_id_check",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            initially="IMMEDIATE",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["nominal_usd"],
            ["prices.nominal_usd"],
            ondelete="RESTRICT",
            initially="IMMEDIATE",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index(
        "ix_subscriptions_status_next_billing_at",
        "subscriptions",
        ["status", "next_billing_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_status_next_billing_at", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    sa.Enum(name="subscription_status").drop(op.get_bind(), checkfirst=True)
