"""users table for platform logins

Additive only: one new table, no existing row or column is touched.

The table is created empty. Dev logins are provisioned by
`seed/seed_users.py` (idempotent, safe to re-run), NOT by this migration --
data does not belong in schema migrations, and an Entra-backed deployment
will provision users by a different route entirely.

Revision ID: c8d41a92e7f3
Revises: b3e97f1c4a62
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "c8d41a92e7f3"
down_revision = "b3e97f1c4a62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("ADMIN", "PLANNER", name="userrole"),
            nullable=False,
        ),
        sa.Column(
            "business_unit_id",
            sa.String(36),
            sa.ForeignKey("business_units.id"),
            nullable=True,
        ),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("users")
