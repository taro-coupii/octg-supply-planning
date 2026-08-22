"""safety_stocks table -- planner-set safety stock per product

Additive only. Created empty: safety stock is a planner decision entered
through Administration, not something a migration should invent.

Revision ID: f7c3a91b5e24
Revises: e5b2c7d94a18
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "f7c3a91b5e24"
down_revision = "e5b2c7d94a18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safety_stocks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "product_id",
            sa.String(36),
            sa.ForeignKey("products.id"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.UniqueConstraint("product_id", name="uq_safety_stock_product"),
    )


def downgrade() -> None:
    op.drop_table("safety_stocks")
