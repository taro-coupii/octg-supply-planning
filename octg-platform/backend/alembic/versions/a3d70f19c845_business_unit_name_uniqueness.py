"""business_units.name gets its uniqueness constraint

WHY A MIGRATION FOR A FEATURE THAT IS MOSTLY NEW ENDPOINTS
=========================================================
Adding ``POST /business-units`` and ``PATCH /customers/{id}`` needed no schema change:
``business_units`` already existed, and ``customers.business_unit_id`` and
``customers.allocation_policy`` are existing columns -- the FK is already nullable
(deliberately -- see ``app.models.customer.Customer``) and the enum already holds all
three values. Nothing was added, widened or moved.

This revision adds the ONE constraint the change makes necessary, and it is here for
exactly the reason ``d5f2a4b91c70`` gave for the substitution tables: making a table
WRITEABLE is what turns a missing constraint from a latent tidiness issue into a
reachable defect. Seed data held one row per Business Unit by construction; nothing
could create a second "Tubular North America". ``POST /business-units`` can.

The name is not decoration. The id is a uuid, so the NAME is the only handle a human
has on a Business Unit -- it is what the Administration tree, every coverage
provenance label and the new customer-remap dropdown render. Two identically named
BUs would put an operator one indistinguishable click from remapping a customer into
the wrong inventory pool, and a Business Unit is an ABSOLUTE inventory boundary
(``app.models.business_unit.BusinessUnit``): that is a different warehouse, not a
different label. Every ``InventoryOnHand`` / ``InventoryOnOrder`` resolution and the
``InventoryAssignment`` netting scope would silently change meaning for that customer.

``app.api.customers.create_business_unit`` pre-checks CASE-INSENSITIVELY and answers
409 with a sentence naming the existing row, so the constraint is not the ordinary
path. It is the backstop for two concurrent requests that both pass the pre-check --
the same division of labour ``uq_lead_time_component_dimension_value`` and
``uq_technical_substitution_pair`` already have, and what makes "a client never sees a
raw database error" true rather than usually true.

CASE-SENSITIVE ON PURPOSE, AND THE ASYMMETRY IS DELIBERATE
----------------------------------------------------------
The constraint is a plain UNIQUE on ``name``, so it is case-sensitive: sqlite would
need a functional index on ``lower(name)`` to be otherwise, and batch-mode table
recreation plus a functional index is a great deal of mechanism for a backstop. The
ENDPOINT is stricter than the database, which is the right direction for the two to
disagree in -- every ordinary write is refused case-insensitively with an explanation,
and the constraint still catches the concurrent duplicate it exists for.

THIS MIGRATION REFUSES RATHER THAN RENAMES
==========================================
``upgrade()`` checks for pre-existing duplicate names FIRST and raises with them
listed. It does not append a suffix to "the extra" one, and that is the point rather
than laziness: a Business Unit name is how operators identify which physical inventory
pool they are looking at, and a migration inventing "Tubular North America (2)" would
be relabelling a warehouse with a schema change's authority behind it. An operator must
decide which name is correct -- both rows are visible through ``GET /business-units``
-- and then re-run. On a database with no duplicates, which includes every seeded one,
the check is a no-op.

NOTES ON THE MECHANICS
----------------------
sqlite cannot add a constraint in place, hence ``batch_alter_table`` (``env.py`` sets
``render_as_batch=True``). Batch mode RECREATES the table, so this migration is not
safe to run against a database an application server is holding open -- stop the
server first.

THE PROJECT'S ENUM-DUPLICATION TRAP DOES NOT APPLY. ``business_units`` has exactly two
columns, ``id`` and ``name``, and neither is an enum -- so nothing renders a
``sa.Enum(..., name=...)``, there is no ``CREATE TYPE`` for postgres to reject as a
duplicate, and the batch recreation cannot re-emit one. ``customers``, which DOES carry
the ``allocationpolicy`` enum, is deliberately UNTOUCHED by this revision: the two
columns the new endpoints write already exist exactly as the models declare them, so
the trap has no way in.

``downgrade()`` drops the constraint and loses nothing: no data is written, moved or
deleted in either direction.

Revision ID: a3d70f19c845
Revises: d5f2a4b91c70
Create Date: 2026-08-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3d70f19c845'
down_revision: Union[str, Sequence[str], None] = 'd5f2a4b91c70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CONSTRAINT = 'uq_business_unit_name'


def _refuse_if_duplicate_names() -> None:
    """Raise, listing the offending names, if ``business_units`` already has duplicates.

    Checked before the constraint is created so the failure names the DATA that has to
    be fixed rather than surfacing as a driver-level "UNIQUE constraint failed" from
    inside a table rebuild. See the module docstring for why nothing is renamed
    automatically.
    """
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                'SELECT name, COUNT(*) AS n FROM business_units '
                'GROUP BY name HAVING COUNT(*) > 1'
            )
        )
        .fetchall()
    )
    if not rows:
        return
    listed = '; '.join(f'{row[0]!r} x{row[1]}' for row in rows)
    raise RuntimeError(
        f'business_units already contains {len(rows)} duplicate name(s), so the unique '
        f'constraint cannot be created: {listed}. This migration deliberately does NOT '
        'rename a survivor -- a Business Unit name is how an operator identifies which '
        'physical inventory pool they are looking at, and inventing a suffix would '
        'relabel a warehouse with a schema change\'s authority behind it. Inspect the '
        'rows (GET /business-units), decide which name is correct, correct the other, '
        'and re-run `alembic upgrade head`. Nothing has been changed.'
    )


def upgrade() -> None:
    """Add the name uniqueness constraint. Writes no data; refuses on duplicates."""
    _refuse_if_duplicate_names()
    with op.batch_alter_table('business_units') as batch:
        batch.create_unique_constraint(_CONSTRAINT, ['name'])


def downgrade() -> None:
    """Drop the constraint. Loses nothing -- no data moves in either direction."""
    with op.batch_alter_table('business_units') as batch:
        batch.drop_constraint(_CONSTRAINT, type_='unique')
