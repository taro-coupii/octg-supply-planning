"""inventory_on_order.booking_status -- PO'ed vs Booked

Additive only: one nullable column, no existing row touched. NULL means the
(future) Oracle feed did not state the booking stage; the UI reports that as
"not stated", never as either value.

Revision ID: e5b2c7d94a18
Revises: c8d41a92e7f3
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "e5b2c7d94a18"
down_revision = "c8d41a92e7f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inventory_on_order",
        sa.Column("booking_status", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inventory_on_order", "booking_status")
