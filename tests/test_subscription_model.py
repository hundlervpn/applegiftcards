from sqlalchemy import BigInteger, CheckConstraint, DateTime, SmallInteger, String

import app.models as models
from app.db.base import Base
from app.models.enums import SubscriptionStatus
from app.models.subscription import Subscription


def test_subscription_status_values():
    assert [status.value for status in SubscriptionStatus] == [
        "active",
        "past_due",
        "cancelled",
    ]


def test_subscription_is_registered_on_metadata():
    assert models.Subscription is Subscription
    assert "subscriptions" in Base.metadata.tables


def test_subscription_columns_and_nullability():
    columns = Subscription.__table__.c

    assert isinstance(columns.id.type, BigInteger)
    assert columns.id.primary_key
    assert columns.id.identity is not None
    assert columns.id.nullable is False

    assert isinstance(columns.user_id.type, BigInteger)
    assert columns.user_id.nullable is False

    assert isinstance(columns.nominal_usd.type, SmallInteger)
    assert columns.nominal_usd.nullable is False

    assert isinstance(columns.payment_method_id.type, String)
    assert columns.payment_method_id.type.length == 64
    assert columns.payment_method_id.nullable is False

    assert columns.status.type.name == "subscription_status"
    assert columns.status.type.native_enum is True
    assert list(columns.status.type.enums) == ["active", "past_due", "cancelled"]
    assert columns.status.nullable is False
    assert "'active'" in str(columns.status.server_default.arg)

    assert isinstance(columns.next_billing_at.type, DateTime)
    assert columns.next_billing_at.type.timezone is True
    assert columns.next_billing_at.nullable is False

    assert isinstance(columns.dunning_attempts.type, SmallInteger)
    assert columns.dunning_attempts.nullable is False
    assert str(columns.dunning_attempts.server_default.arg) == "0"

    assert isinstance(columns.created_at.type, DateTime)
    assert columns.created_at.type.timezone is True
    assert columns.created_at.nullable is False

    assert isinstance(columns.updated_at.type, DateTime)
    assert columns.updated_at.type.timezone is True
    assert columns.updated_at.nullable is False
    assert columns.updated_at.onupdate is not None


def test_subscription_foreign_keys_match_existing_restrict_style():
    foreign_keys = {
        (fk.parent.name, fk.column.table.name, fk.column.name): fk
        for fk in Subscription.__table__.foreign_keys
    }

    user_fk = foreign_keys[("user_id", "users", "id")]
    price_fk = foreign_keys[("nominal_usd", "prices", "nominal_usd")]

    assert set(foreign_keys) == {
        ("user_id", "users", "id"),
        ("nominal_usd", "prices", "nominal_usd"),
    }
    for fk in (user_fk, price_fk):
        assert fk.ondelete == "RESTRICT"
        assert fk.deferrable is True
        assert fk.initially == "IMMEDIATE"


def test_subscription_checks_and_billing_index():
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Subscription.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks["subscriptions_dunning_attempts_check"] == "dunning_attempts >= 0"
    assert "btrim(payment_method_id)" in checks["subscriptions_payment_method_id_check"]

    billing_index = next(
        index
        for index in Subscription.__table__.indexes
        if index.name == "ix_subscriptions_status_next_billing_at"
    )
    assert [column.name for column in billing_index.columns] == ["status", "next_billing_at"]
    assert billing_index.unique is False

    user_index = next(
        index for index in Subscription.__table__.indexes if index.name == "ix_subscriptions_user_id"
    )
    assert [column.name for column in user_index.columns] == ["user_id"]


def test_subscription_insert_defaults():
    assert Subscription.__table__.c.status.default.arg is SubscriptionStatus.active
    assert Subscription.__table__.c.dunning_attempts.default.arg == 0
