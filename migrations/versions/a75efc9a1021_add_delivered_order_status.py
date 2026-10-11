from typing import Sequence, Union

from alembic import op

revision: str = "a75efc9a1021"
down_revision: Union[str, Sequence[str], None] = "dfbeac00cd2c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'delivered'")


def downgrade() -> None:
    op.execute("ALTER TABLE orders ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE order_status RENAME TO order_status_with_delivered")
    op.execute(
        "CREATE TYPE order_status AS ENUM "
        "('pending_payment', 'paid', 'completed', 'cancelled')"
    )
    op.execute(
        "ALTER TABLE orders ALTER COLUMN status TYPE order_status "
        "USING (CASE WHEN status::text = 'delivered' THEN 'completed' "
        "ELSE status::text END)::order_status"
    )
    op.execute("DROP TYPE order_status_with_delivered")
    op.execute(
        "ALTER TABLE orders ALTER COLUMN status "
        "SET DEFAULT 'pending_payment'::order_status"
    )
