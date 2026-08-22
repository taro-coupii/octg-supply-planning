"""inventory on order -- the read-only projection of Oracle purchase orders

Adds ONE table, ``inventory_on_order``. See
app.models.inventory_on_order.InventoryOnOrder for the shape, the system boundary
and -- most importantly -- why a MISSING row means "unknown" rather than "zero".

PURELY ADDITIVE
---------------
One new table. No existing table is altered, no existing column is touched and no
data is migrated, so this is safe to run against a database an application server
is holding open. ``on_order`` was previously a hardcoded ``0.0`` in
``app.engines.mrp.InventoryPosition`` with no storage behind it at all, so there
is nothing to back-fill: no row anywhere in any existing database ever carried an
on-order quantity, and inventing rows here would fabricate purchase orders.

NO NEW ENUM TYPE, AND NO DUPLICATE-TYPE TRAP
--------------------------------------------
The known autogenerate trap in this project (see ``7ebf91b5526c_initial_schema``
and ``c4f1a90b7de2_scenario_planning``) is that alembic emits a bare
``sa.Enum(..., name=...)`` inline in every ``create_table`` that mentions it,
which on postgres makes the SECOND such table raise ``DuplicateObject``. It does
not bite here because this table declares no enum column: ``source_system`` is a
plain ``String`` carrying the provenance vocabulary
("synthetic" / "oracle" / "mixed"), exactly as ``inventory_on_hand`` and
``inventory_assignments`` already do.

That is a deliberate consistency rather than a shortcut. The vocabulary is shared
with ``app.engines.mrp.InventoryPosition.assigned_source``, which also carries
"unavailable" -- a value no ROW can hold, since a row's existence is what makes
the figure available. A database enum would either have to include a value that
cannot occur in the column or disagree with the API's own vocabulary; a String
does neither. Consequently ``_create_enum_types`` / ``_drop_enum_types`` are
absent from this revision because there is genuinely nothing for them to do.

NO UNIQUE CONSTRAINT ON (business_unit_id, product_id)
------------------------------------------------------
Unlike ``inventory_on_hand``, where the pair IS the fact and the constraint keeps
it single-valued. Here one row is one expected arrival -- i.e. one purchase-order
line -- so several rows per pair are the normal, correct shape of a delivery
schedule. A unique constraint would force the projection to collapse a schedule
into a total and throw away every ``expected_arrival_date`` but one, which is the
field the dashboard's arrival horizon is built from.

An index on the pair is added instead: every read goes through
``app.engines.inventory.on_order_rows``, which filters on business_unit_id and
(usually) product_id.

Revision ID: b1d9e04f7a52
Revises: a91c4e70b382
Create Date: 2026-08-05 10:20:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b1d9e04f7a52'
down_revision: Union[str, Sequence[str], None] = 'a91c4e70b382'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create ``inventory_on_order``."""
    op.create_table(
        'inventory_on_order',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('business_unit_id', sa.String(length=36), nullable=False),
        sa.Column('product_id', sa.String(length=36), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('expected_arrival_date', sa.DateTime(), nullable=True),
        sa.Column('source_system', sa.String(), nullable=False),
        sa.Column('source_reference', sa.String(), nullable=True),
        sa.Column('synced_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['business_unit_id'], ['business_units.id'], ),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_inventory_on_order_bu_product',
        'inventory_on_order',
        ['business_unit_id', 'product_id'],
        unique=False,
    )


def downgrade() -> None:
    """Drop the table and its index.

    A real downgrade, not a ``pass``. The table has no dependents -- nothing
    references ``inventory_on_order.id`` -- so dropping it is complete and
    reversible by re-running ``upgrade``.

    It DOES destroy data, and that is the honest behaviour rather than a reason to
    refuse: the rows are a read-only projection of Oracle-owned purchase orders, so
    every one of them can be re-derived by re-running the feed (or, today,
    ``python -m seed.seed_from_workbook --reset``). This platform holds no
    on-order fact that Oracle does not already own, which is precisely why
    dropping the projection loses nothing that cannot be rebuilt.

    The index is dropped explicitly before the table. sqlite and postgres both drop
    an index with its table, so this is belt-and-braces -- but an explicit drop is
    what makes the downgrade readable as the exact inverse of the upgrade.
    """
    op.drop_index('ix_inventory_on_order_bu_product', table_name='inventory_on_order')
    op.drop_table('inventory_on_order')
