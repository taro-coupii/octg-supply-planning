"""products.unit_of_measure -- every quantity in the platform gets a unit

*** THIS MIGRATION ASSUMES A UNIT FOR EVERY EXISTING PRODUCT. ***

`products.unit_of_measure` is NOT NULL with no server default, so the column
cannot simply be added to a populated table. Existing rows are BACK-FILLED to
``'MTR'`` (metres), and this section exists because that back-fill is an
ASSUMPTION about data this migration cannot inspect.

WHY 'MTR' IS THE HONEST BACK-FILL *FOR THIS DATABASE*
-----------------------------------------------------
Three facts, all checkable:

  * Every product row that has ever existed in this schema is a tubular -- casing
    or tubing -- created either by ``seed/seed_from_workbook.py`` or by the Excel
    demand import, and both build products from the workbook's OCTG catalogue.
  * The source workbook quantifies those items in ``Mtr`` columns. It also carries
    ``(PC)`` and ``(MT)`` columns, which is exactly why the new column is an enum
    with three members rather than being hard-wired to metres -- but no row of
    either kind has been loaded into this schema, because nothing before this
    revision could have recorded which one a row was.
  * The pilot customer is Norwegian and buys by the metre.

So for the databases this revision will actually run against, metres is not a
guess, it is the value the rows already meant implicitly.

WHAT THAT DOES *NOT* LICENCE
----------------------------
If you are running this against a database whose products were loaded from
somewhere else -- accessories counted by the piece, or a framework contract
denominated in metric tonnes -- then this back-fill WILL SILENTLY MISLABEL THEM,
and a mislabelled unit is worse than a missing one: `app.engines.executive
.quantity_by_unit` groups totals on this column, so a wrongly-labelled row is
added into the wrong bucket and the resulting total looks perfectly plausible.
There is no way for a migration to detect this. CHECK the products table before
you run it, and correct any non-metre rows immediately afterwards:

    UPDATE products SET unit_of_measure = 'PC' WHERE type = 'ACC';   -- example

There is deliberately NO conversion factor anywhere in this platform (metres per
tonne depends on the product's weight per metre and on the mill tolerance of the
joints actually delivered), so a wrong unit here cannot be repaired by arithmetic
later -- only by restating the fact.

WHAT downgrade() CAN AND CANNOT DO
----------------------------------
``downgrade()`` is real and complete AS SCHEMA: it drops the column and, on
postgres, drops the enum type, leaving exactly the pre-migration shape.

It CANNOT recover the values, and that loss is genuine rather than theoretical.
Any unit a user or a later seed set to something OTHER than metres -- the whole
reason the column is an enum -- is DESTROYED by the downgrade and is not
recoverable by re-running the upgrade, which would back-fill every row to metres
again. Down-then-up is therefore LOSSY for exactly the rows the column was added
to describe. If you need those values, take a backup first; this migration is not
one.

Nothing else is lost: no other table references the column, and quantities
themselves are untouched.

WHY THE WELL DATES ARE NOT IN HERE
----------------------------------
The other half of this change set adds `earliest_ros_date` and
`first_runout_date` to the well payloads, and they get NO columns. They are
DERIVED -- computed per request by `app.engines.well_dates` from the in-scope
demand lines and their coverage verdicts -- and this project's settled position on
derived data is to recompute or delete it rather than let it go stale (see
`app.models.coverage.CoverageResult`, whose rows are deleted the moment a line
leaves scope). A stored `first_runout_date` would be wrong the instant anybody
revised a quantity, and nothing would say so. `product_description` on the demand
payloads is likewise a read through an existing foreign key, not new state.

NOTES ON THE MECHANICS
----------------------
The project's autogenerate trap applies and has been fixed the same way
``ee59aca3b091`` and ``7ebf91b5526c`` fix it. Autogenerate emitted a bare inline
``sa.Enum('MTR', 'PC', 'MT', name='unitofmeasure')``, which on postgres issues its
own ``CREATE TYPE``; ``DATABASE_URL`` defaults to postgres, so a second mention of
the type by any later table would raise ``DuplicateObject``. The enum is therefore
declared ONCE as a module-level object whose postgres variant carries
``create_type=False``, and the type is created explicitly at the top of
``upgrade()`` and dropped at the bottom of ``downgrade()``. On sqlite an enum is
VARCHAR + a CHECK constraint, so nothing standalone is created there.

Enum MEMBER NAMES are stored, not values, matching every other enum column in this
schema -- so the column holds ``'MTR'``, not ``'Mtr'``.

The column is added NULLABLE, back-filled, and only then altered to NOT NULL. That
three-step order is required: a NOT NULL column with no server default cannot be
added to a table that already has rows. It is done inside
``batch_alter_table`` because sqlite cannot alter a column's nullability in place;
``render_as_batch=True`` is configured in ``alembic/env.py`` and recreates the
table around the change. Because it is a table rewrite on sqlite, this migration is
NOT safe to run against a database an application server is holding open -- stop
the server first.

Revision ID: f2b6d4c81a37
Revises: ee59aca3b091
Create Date: 2026-08-05 13:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f2b6d4c81a37'
down_revision: Union[str, Sequence[str], None] = 'ee59aca3b091'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UOM_VALUES = ('MTR', 'PC', 'MT')
_UOM_ENUM_NAME = 'unitofmeasure'

#: The value every pre-existing row is given. See the module docstring for why
#: this is honest for this database and what to check before trusting it for yours.
_BACKFILL = 'MTR'

# Declared once; the postgres variant never auto-creates its type (see docstring).
UNIT_OF_MEASURE = sa.Enum(
    *_UOM_VALUES, name=_UOM_ENUM_NAME
).with_variant(
    postgresql.ENUM(*_UOM_VALUES, name=_UOM_ENUM_NAME, create_type=False),
    'postgresql',
)


def _create_enum_types() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return  # sqlite has no standalone enum types
    postgresql.ENUM(*_UOM_VALUES, name=_UOM_ENUM_NAME).create(bind, checkfirst=True)


def _drop_enum_types() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    postgresql.ENUM(*_UOM_VALUES, name=_UOM_ENUM_NAME).drop(bind, checkfirst=True)


def upgrade() -> None:
    """Add products.unit_of_measure NOT NULL, back-filling existing rows to metres."""
    _create_enum_types()

    # Step 1: nullable, so it can be added to a populated table at all.
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('unit_of_measure', UNIT_OF_MEASURE, nullable=True)
        )

    # Step 2: back-fill. Bound parameter rather than an interpolated literal --
    # the value is a constant here, but the habit is what keeps the next edit safe.
    op.execute(
        sa.text(
            'UPDATE products SET unit_of_measure = :unit '
            'WHERE unit_of_measure IS NULL'
        ).bindparams(unit=_BACKFILL)
    )

    # Step 3: tighten to NOT NULL. An unlabelled quantity is the defect this column
    # exists to fix, so the constraint is the point of the migration and not a
    # decoration on it -- a nullable column would let the next insert reintroduce
    # exactly the bare number the product owner reported.
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.alter_column(
            'unit_of_measure', existing_type=UNIT_OF_MEASURE, nullable=False
        )


def downgrade() -> None:
    """Drop the column and (on postgres) the enum type.

    LOSSY for any row whose unit was not metres -- see the module docstring. The
    schema comes back exactly as it was; the facts do not.
    """
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('unit_of_measure')

    _drop_enum_types()
