"""drop products.on_hand_qty -- BU-scoped inventory becomes mandatory

*** THIS MIGRATION DESTROYS DATA. ***

It DROPS the column ``products.on_hand_qty``. Every value in it is deleted and
this migration does not copy them anywhere. Read the two sections below before
running it on any database whose contents you care about.

WHY THE VALUES ARE NOT MIGRATED INTO inventory_on_hand
------------------------------------------------------
The obvious-looking kindness would be, for each product with a non-zero
``on_hand_qty``, to insert an ``inventory_on_hand`` row carrying that quantity.
There is no correct way to do it, and doing it wrongly is worse than not doing it:

  * ``on_hand_qty`` was ONE GLOBAL SCALAR with no Business Unit dimension. That
    absence is not an inconvenience, it is the entire defect being closed. The
    information "which BU holds these 8000 tubulars" was never recorded, so no
    query over this schema can recover it.
  * Attributing it to one BU (the product's "main" BU, the first BU, the only BU
    with demand for it) would INVENT a fact. The invented row is
    indistinguishable from a real Oracle-projected one afterwards, and the
    coverage engine would then compute confident verdicts from it. That is the
    same class of error as the leak itself, with a migration's authority behind it.
  * Copying it to EVERY BU is strictly the old bug, written down: two BUs each
    reading the same global number as their own.
  * Splitting it across BUs by any ratio is arithmetic performed on data that does
    not exist.

So this migration migrates NOTHING and requires a RE-SEED (or a real load from
Oracle) to restore usable quantities:

    alembic upgrade head
    python -m seed.seed_from_workbook --reset     # DESTRUCTIVE: deletes all rows

After the upgrade, any (Business Unit, product) pair with no ``inventory_on_hand``
row makes the coverage engine RAISE ``InventoryRowMissing`` rather than treat the
quantity as 0 -- see ``app.engines.inventory``. That is deliberate and it is how
you will find out that a quantity is missing, instead of reading a wrong number
off a screen. It also means a production database cannot be limped along after
this upgrade: the inventory rows must be loaded before coverage will compute.

WHAT downgrade() CAN AND CANNOT DO
----------------------------------
``downgrade()`` is real: it re-creates the column with its original type
(``Float``, ``NOT NULL``, no server default) so the schema matches revision
b7f4c1a92e30 exactly and the older application code can start against it.

It CANNOT restore the VALUES. They were dropped by ``upgrade()`` and nothing in
this database remembers them. Every row comes back holding **0**. Saying so here
rather than letting somebody discover it: after a downgrade the old code will read
``on_hand_qty = 0`` for every product and report everything Uncovered. If you need
the old values back, restore from a backup taken before the upgrade -- this
migration is not that backup.

NOTES ON THE MECHANICS
----------------------
Unlike the three revisions before it, this one ALTERS an existing table. sqlite
cannot drop a column natively, which is why ``render_as_batch=True`` is configured
in ``alembic/env.py``: batch mode recreates the table around the change. Because
it is a table rewrite, this migration is NOT safe to run against a database an
application server is holding open -- stop the server first.

The project's usual autogenerate trap (a separate inline ``sa.Enum(..., name=...)``
per table, which raises ``DuplicateObject`` on postgres -- see
``7ebf91b5526c_initial_schema.py``) does not apply here: no table is created, no
enum type is created or dropped, and ``products`` has no enum column at all (its
string columns are plain ``String``). Nothing was hand-corrected on that account,
and no ``_enum`` helper or ``_create_enum_types`` pair is needed.

Revision ID: d3a17c56e0b4
Revises: b7f4c1a92e30
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd3a17c56e0b4'
down_revision: Union[str, Sequence[str], None] = 'b7f4c1a92e30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop products.on_hand_qty. DESTRUCTIVE -- see the module docstring."""
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('on_hand_qty')


def downgrade() -> None:
    """Re-create the column. Restores the SCHEMA; every row gets 0.

    Two steps, because a NOT NULL column cannot be added to a populated table
    without a default: add it WITH ``server_default='0'`` so existing rows are
    filled, then drop the server default so the column definition matches
    b7f4c1a92e30 byte for byte (the original had a python-side default only).
    """
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'on_hand_qty',
                sa.Float(),
                nullable=False,
                server_default=sa.text('0'),
            )
        )
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.alter_column('on_hand_qty', server_default=None)
