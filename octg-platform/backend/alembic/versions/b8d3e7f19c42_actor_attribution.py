"""Server-recorded actors (F09)

Adversarial review 2026-09-06, F09: the only attribution this platform kept was
free text from the request body (`scenarios.created_by`,
`demand_import_rows.override_approved_by`), and substitution decisions and inline
company-inventory edits kept none. Additive only:

  * `scenarios.created_by_user_id`
  * `demand_import_rows.override_approved_by_user_id`
  * `well_substitution_approvals.requested_by_user_id` / `decided_by_user_id`
  * new `company_inventory_edits` append-only log

All nullable: rows written before this migration have no recorded actor and must
not be made to claim one. No FOREIGN KEY to users: these are records of who
acted and must survive the user being removed later.

Revision ID: b8d3e7f19c42
Revises: f7c3a91b5e24
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d3e7f19c42"
down_revision = "f7c3a91b5e24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scenarios") as b:
        b.add_column(
            sa.Column(
                "created_by_user_id", sa.String(36), nullable=True
            )
        )
    with op.batch_alter_table("demand_import_rows") as b:
        b.add_column(
            sa.Column(
                "override_approved_by_user_id",
                sa.String(36),
                nullable=True,
            )
        )
    with op.batch_alter_table("well_substitution_approvals") as b:
        b.add_column(
            sa.Column(
                "requested_by_user_id", sa.String(36), nullable=True
            )
        )
        b.add_column(
            sa.Column(
                "decided_by_user_id", sa.String(36), nullable=True
            )
        )
    op.create_table(
        "company_inventory_edits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("row_kind", sa.String(), nullable=False),
        sa.Column("row_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("edited_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("company_inventory_edits")
    with op.batch_alter_table("well_substitution_approvals") as b:
        b.drop_column("decided_by_user_id")
        b.drop_column("requested_by_user_id")
    with op.batch_alter_table("demand_import_rows") as b:
        b.drop_column("override_approved_by_user_id")
    with op.batch_alter_table("scenarios") as b:
        b.drop_column("created_by_user_id")
