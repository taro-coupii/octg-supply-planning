"""Net shortfall on coverage results (F04)

Adversarial review 2026-09-06, F04: MRP re-ordered the WHOLE of every unresolved
line even when coverage had already drawn part of it from stock, so a partial
draw was counted twice -- once as consumed stock, once as steel to order.
Product-owner ruling: the net shortfall is the figure, with the breakdown shown.

Additive only. All nullable: a verdict computed before these columns existed has
no net figures, and MRP counts such a line whole (the old behaviour) and says so
until its well is recomputed.

Revision ID: c7e2a9d41f58
Revises: b8d3e7f19c42
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op

revision = "c7e2a9d41f58"
down_revision = "b8d3e7f19c42"
branch_labels = None
depends_on = None

_COLUMNS = (
    "demand_quantity",
    "drawn_customer_owned",
    "drawn_company",
    "drawn_substitute",
    "residual",
)


def upgrade() -> None:
    with op.batch_alter_table("coverage_results") as b:
        for name in _COLUMNS:
            b.add_column(sa.Column(name, sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("coverage_results") as b:
        for name in reversed(_COLUMNS):
            b.drop_column(name)
