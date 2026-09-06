"""Import approval basis and scenario version (F07, F08)

Adversarial review 2026-09-06. F07: an import override approval now records the
basis it was given against (`demand_import_rows.override_approval_basis`) and
lapses when that changes. F08: `scenarios.version` is bumped on every change and
an apply must name the version it previewed.

Additive only. Existing approvals have no basis and are therefore treated as
lapsed -- nobody can say what they authorised -- and need re-approving; existing
scenarios start at version 1.

Revision ID: d9f1b6c83a27
Revises: c7e2a9d41f58
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op

revision = "d9f1b6c83a27"
down_revision = "c7e2a9d41f58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("demand_import_rows") as b:
        b.add_column(sa.Column("override_approval_basis", sa.String(), nullable=True))
    with op.batch_alter_table("scenarios") as b:
        b.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )


def downgrade() -> None:
    with op.batch_alter_table("scenarios") as b:
        b.drop_column("version")
    with op.batch_alter_table("demand_import_rows") as b:
        b.drop_column("override_approval_basis")
