"""Scenarios are scoped to a Business Unit, not a customer (D01)

Product-owner ruling 2026-09-06. Coverage is allocated across the whole Business
Unit, so a scenario confined to one customer could neither preview nor honestly
describe what applying it would do to the pool it shares.

`business_unit_id` is backfilled from each scenario's customer's Business Unit,
which is exactly the pool that scenario was previewed against, so no scenario
changes meaning. A scenario whose customer had NO Business Unit could never have
been previewed at all (the pass raised) and is deleted rather than pointed at an
invented pool.

Revision ID: e4a17c9b3d60
Revises: d9f1b6c83a27
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op

revision = "e4a17c9b3d60"
down_revision = "d9f1b6c83a27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM scenario_overrides WHERE scenario_id IN ("
        " SELECT s.id FROM scenarios s JOIN customers c ON c.id = s.customer_id"
        " WHERE c.business_unit_id IS NULL)"
    )
    op.execute(
        "DELETE FROM scenarios WHERE customer_id IN ("
        " SELECT id FROM customers WHERE business_unit_id IS NULL)"
    )
    with op.batch_alter_table("scenarios") as b:
        b.add_column(sa.Column("business_unit_id", sa.String(36), nullable=True))
    op.execute(
        "UPDATE scenarios SET business_unit_id = ("
        " SELECT c.business_unit_id FROM customers c WHERE c.id = scenarios.customer_id)"
    )
    with op.batch_alter_table("scenarios") as b:
        b.alter_column("business_unit_id", existing_type=sa.String(36), nullable=False)
        b.create_foreign_key(
            "fk_scenarios_business_unit", "business_units", ["business_unit_id"], ["id"]
        )
        b.drop_column("customer_id")


def downgrade() -> None:
    """Irreversible in meaning: a Business-Unit scenario has no single customer to
    go back to. The column is restored empty so the schema shape can be rolled
    back, and any scenario is left without an owner rather than assigned a guess."""
    with op.batch_alter_table("scenarios") as b:
        b.add_column(sa.Column("customer_id", sa.String(36), nullable=True))
        b.drop_column("business_unit_id")
