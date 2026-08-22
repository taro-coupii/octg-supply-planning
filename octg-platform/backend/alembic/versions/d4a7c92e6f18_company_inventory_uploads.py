"""company_inventory_uploads -- audit trail for the on-hand maintenance surface

MVP-COMPROMISE[C-03]: this table exists only because there is no Oracle interface
in the MVP for the three read-only inventory projections
(``inventory_on_hand``, ``inventory_on_order``, ``inventory_assignments``). See
``app.engines.company_inventory``'s module docstring and MVP_COMPROMISES.md C-03.

Adds ONE table: ``company_inventory_uploads``, one row per accepted spreadsheet
that restated a Business Unit's ``inventory_on_hand`` position. Modelled directly
on ``customer_owned_inventory_uploads`` (see ``c8e37b45d1a0``) -- same columns,
same reason for existing (audit trail AND the fact that an upload happened).

NO NEW TABLE FOR THE POSITIONS THEMSELVES
------------------------------------------
Unlike the customer-owned migration, this revision does NOT create a table for the
quantities: ``inventory_on_hand`` already exists (it is the Oracle projection this
surface writes onto, under the ``PLATFORM_MAINTAINABLE_SOURCES`` gate) and needs no
schema change -- ``source_system`` and ``synced_at`` were added when that table was
created. Only the audit trail is new.

NO NEW ENUM TYPE
-----------------
Same reasoning as ``c8e37b45d1a0``: ``source_system`` is a plain ``String``
("manual" is the only value this table's rows will ever carry), so there is no
inline ``sa.Enum`` for autogenerate to duplicate on a second ``create_table``.

PURELY ADDITIVE
---------------
One new table. No existing table is altered, no existing column touched, no data
migrated. Safe to run against a database an application server is holding open.

Revision ID: d4a7c92e6f18
Revises: a3d70f19c845
Create Date: 2026-08-10 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4a7c92e6f18'
down_revision: Union[str, Sequence[str], None] = 'a3d70f19c845'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'company_inventory_uploads',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('business_unit_id', sa.String(length=36), nullable=False),
        sa.Column('filename', sa.String(), nullable=True),
        sa.Column('sheet_name', sa.String(), nullable=True),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('applied_count', sa.Integer(), nullable=False),
        sa.Column('error_count', sa.Integer(), nullable=False),
        sa.Column('replaced_count', sa.Integer(), nullable=False),
        sa.Column('created_count', sa.Integer(), nullable=False),
        sa.Column(
            'uploaded_at',
            sa.DateTime(),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.Column('source_system', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['business_unit_id'], ['business_units.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Drops the audit trail only.

    Unlike ``c8e37b45d1a0``'s downgrade, this one does NOT lose the quantities
    themselves -- ``inventory_on_hand`` is untouched, because it always was the
    Oracle projection table and this revision never altered it. What is lost is
    the HISTORY of which manual uploads produced the figures currently standing in
    it, and the row counts of past corrections. The current on-hand values survive
    a downgrade unchanged.
    """
    op.drop_table('company_inventory_uploads')
