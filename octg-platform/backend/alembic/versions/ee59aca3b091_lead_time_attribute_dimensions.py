"""lead_time_components becomes attribute-based (OD/WT, Grade, Connection, Logistics)

*** THIS MIGRATION DESTROYS DATA. ***

It DELETES EVERY ROW of ``lead_time_components`` and drops the columns
``grade_type`` and ``component``. A RE-SEED (or a real load of lead-time
configuration) is REQUIRED afterwards, or every product's lead time resolves to
"not modelled":

    alembic upgrade head
    python -m seed.seed_from_workbook --reset     # DESTRUCTIVE: deletes all rows

Read the next two sections before running this on a database whose contents you
care about.

WHY THE EXISTING ROWS ARE NOT CONVERTED
---------------------------------------
The old shape was ``(grade_type, component_name, months)`` and the engine summed
every row matching the product's ``grade_type``. The new shape is
``(dimension, attribute_value, months)`` where dimension is one of OD/WT, Grade,
Connection, Logistics -- the four the spec builds a lead time from. The old rows
cannot be faithfully converted, and a plausible-looking conversion is worse than
none:

  * There is NO OD/WT information and NO CONNECTION information in the old table.
    That absence is the entire defect being closed -- two products of the same
    grade_type but different size, weight and connection were guaranteed identical
    lead times because those dimensions were unrepresentable. Nothing in this
    schema can recover values that were never recorded, and inventing them
    (spreading "Ex-mill" across OD/WT, or assuming +0 for Connection) would
    manufacture configuration with a migration's authority behind it. The engine
    would then compute confident order dates and UNRECOVERABLE verdicts from it.
  * The one apparently clean mapping -- a non-shipping row like "Ex-mill" becoming
    a Grade row keyed by its grade_type, and "Sailing"/"Shipping" becoming a
    Logistics row -- is not clean either. "Ex-mill" is a MILL LEAD TIME, driven
    mostly by size and wall; filing it under Grade puts a real number under the
    wrong dimension permanently, where nobody reviewing the Grade column would
    ever think to question it.
  * It would also not always be POSSIBLE: two non-shipping components for one
    grade_type ("Ex-mill" + "Threading") both map to the single Grade row for that
    grade_type and collide with the new unique constraint, so the migration would
    fail on some databases and silently mis-file data on the rest.

So this migration converts NOTHING. It deletes the rows instead, which makes the
absence LOUD: with no components, `app.engines.lead_time` reports
``modelled=False, total_months=0``, coverage never says UNRECOVERABLE (absent data
means "cannot judge") and MRP's reason text says the order date is indicative only
and names the dimensions to configure. That is how you find out the configuration
is missing, instead of reading a wrong date off a screen.

WHAT downgrade() CAN AND CANNOT DO
----------------------------------
``downgrade()`` is real: it restores the exact pre-migration SCHEMA
(``grade_type`` and ``component``, both ``VARCHAR NOT NULL``, no unique
constraint) so revision e5c2f81a4b90's application code can start against it.

It CANNOT restore the VALUES -- neither the old rows (``upgrade()`` deleted them
and nothing here remembers them) nor the new ones. Saying so plainly rather than
letting somebody discover it: ``downgrade()`` DELETES every row of the table on
the way back, because the columns it re-creates are NOT NULL and there is no
correct value to put in them for an attribute-keyed row (an OD/WT row has no
grade_type at all). After a downgrade the table is EMPTY and the old code will
compute a total lead time of 0 for every product. If you need either generation of
values back, restore from a backup taken before the migration -- this migration is
not that backup.

NOTES ON THE MECHANICS
----------------------
This ALTERS an existing table, and sqlite can neither drop a column nor add a
constraint in place, which is why ``render_as_batch=True`` is configured in
``alembic/env.py``: batch mode recreates the table around the change. Because it is
a table rewrite, this migration is NOT safe to run against a database an
application server is holding open -- stop the server first.

The rows are deleted BEFORE the columns are added, deliberately. ``dimension`` and
``attribute_value`` are ``NOT NULL`` with no server default, so adding them to a
populated table would fail outright; and there is no default that would be
truthful anyway.

The project's usual autogenerate trap DOES apply here: alembic emitted a bare
inline ``sa.Enum('OD_WT', ..., name='leadtimedimension')``, which on postgres
issues its own ``CREATE TYPE``. ``DATABASE_URL`` defaults to postgres, and a
second mention of the type by any later table would then raise
``DuplicateObject``. Following ``7ebf91b5526c_initial_schema.py`` and
``c4f1a90b7de2_scenario_planning.py``: the enum is declared ONCE as a module-level
object whose postgres variant carries ``create_type=False``, and the type is
created explicitly at the top of ``upgrade()`` and dropped at the bottom of
``downgrade()``. sqlite behaviour is unchanged (an enum there is VARCHAR + CHECK).

Enum MEMBER NAMES, not values, are stored -- matching every other enum column in
this schema. So the column holds 'OD_WT', not 'OD/WT'.

Revision ID: ee59aca3b091
Revises: e5c2f81a4b90
Create Date: 2026-08-05 10:05:04.703671

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'ee59aca3b091'
down_revision: Union[str, Sequence[str], None] = 'e5c2f81a4b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DIMENSION_VALUES = ('OD_WT', 'GRADE', 'CONNECTION', 'LOGISTICS')
_DIMENSION_ENUM_NAME = 'leadtimedimension'

# Declared once; the postgres variant never auto-creates its type (see docstring).
LEAD_TIME_DIMENSION = sa.Enum(
    *_DIMENSION_VALUES, name=_DIMENSION_ENUM_NAME
).with_variant(
    postgresql.ENUM(
        *_DIMENSION_VALUES, name=_DIMENSION_ENUM_NAME, create_type=False
    ),
    'postgresql',
)


def _create_enum_types() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return  # sqlite has no standalone enum types
    postgresql.ENUM(*_DIMENSION_VALUES, name=_DIMENSION_ENUM_NAME).create(
        bind, checkfirst=True
    )


def _drop_enum_types() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    postgresql.ENUM(*_DIMENSION_VALUES, name=_DIMENSION_ENUM_NAME).drop(
        bind, checkfirst=True
    )


def upgrade() -> None:
    """Re-key lead_time_components by attribute dimension. DESTRUCTIVE."""
    _create_enum_types()

    # The old rows carry no OD/WT and no Connection information, so they cannot be
    # converted -- see the module docstring. Deleted here, before the NOT NULL
    # columns are added.
    op.execute(sa.text('DELETE FROM lead_time_components'))

    with op.batch_alter_table('lead_time_components', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('dimension', LEAD_TIME_DIMENSION, nullable=False)
        )
        batch_op.add_column(sa.Column('attribute_value', sa.String(), nullable=False))
        batch_op.add_column(sa.Column('label', sa.String(), nullable=True))
        batch_op.create_unique_constraint(
            'uq_lead_time_component_dimension_value', ['dimension', 'attribute_value']
        )
        batch_op.drop_column('grade_type')
        batch_op.drop_column('component')


def downgrade() -> None:
    """Restore the grade_type + component SCHEMA. The table comes back EMPTY.

    The columns being re-created are NOT NULL and no attribute-keyed row has a
    truthful value for them, so the rows are deleted rather than mangled. See the
    module docstring.
    """
    op.execute(sa.text('DELETE FROM lead_time_components'))

    with op.batch_alter_table('lead_time_components', schema=None) as batch_op:
        batch_op.drop_constraint(
            'uq_lead_time_component_dimension_value', type_='unique'
        )
        batch_op.add_column(sa.Column('grade_type', sa.VARCHAR(), nullable=False))
        batch_op.add_column(sa.Column('component', sa.VARCHAR(), nullable=False))
        batch_op.drop_column('label')
        batch_op.drop_column('attribute_value')
        batch_op.drop_column('dimension')

    _drop_enum_types()
