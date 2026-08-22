"""demand_lines.created_at -- make the demand trend able to accumulate

WHAT THIS IS FOR
----------------
The Executive Dashboard's prior-period demand comparison reports
``available: false`` because a demand line's state at a past date cannot be
reconstructed. ``DemandRevision`` is append-only and timestamped, so a line that
HAS a revision at or before the comparison date can be reconstructed -- but a
line with no such revision was ambiguous between "existed then and never changed"
and "did not exist yet". Those give different prior-period totals, so there was no
honest number.

This revision closes the structural half of that gap by adding
``demand_lines.created_at`` (``NOT NULL``, server default ``now()``). The
behavioural half is in the application: every demand line now writes its initial
state as revision 1 at creation -- see
``app.models.demand._write_initial_revision``, which attaches the guarantee to the
INSERT itself so seed, tests, the Excel import and any future route all get it.

Together those two make "unavailable now, accumulates going forward" TRUE rather
than merely stated. Without them the figure would have stayed unavailable forever.

EXISTING ROWS ARE NOT BACK-FILLED, AND CANNOT BE
------------------------------------------------
Rows that already exist get the server default -- i.e. the timestamp of THIS
MIGRATION, not of their real creation, which was never recorded anywhere in this
schema. That is unavoidable for a ``NOT NULL`` column, and it is called out here
rather than left to be discovered:

  * A pre-existing demand line's true creation moment does not exist as data. It
    is not in ``demand_lines``, it is not in ``demand_revisions`` (those rows only
    exist for lines that were revised), and no audit table records it.
  * No initial revision is invented for those rows either. Writing a synthetic
    revision 1 carrying the line's CURRENT values would assert that the line has
    never changed since creation, which is exactly the ambiguity this change
    exists to remove -- with a migration's authority behind the guess.
  * Consequence: for any comparison date earlier than this migration, a
    pre-existing line still has no reconstructable state, and
    ``app.engines.executive._state_as_of`` still reports the prior-period figure
    unavailable and says why. As soon as the comparison date is later than this
    migration, the created_at facts are genuine and the figure computes.

If a truthful historical trend is needed sooner than it accumulates, it has to
come from a source that actually recorded the history (an Oracle extract, a
backup), loaded as real ``demand_revisions`` rows. This migration is not that
source and does not pretend to be.

MECHANICS
---------
One added column, ``NOT NULL`` with a server default so populated tables can take
it in a single step. sqlite cannot add a ``NOT NULL`` column with no default,
which is why the server default is not dropped afterwards -- unlike
``d3a17c56e0b4``'s downgrade, the default here is part of the intended model
definition (``server_default=func.now()`` in ``app.models.demand.DemandLine``), so
keeping it is what makes the schema match the models and the drift check empty.

``render_as_batch=True`` is configured in ``alembic/env.py`` for sqlite, so
``batch_alter_table`` is used. No table is created and no enum type is touched, so
the project's usual autogenerate trap -- a separate inline ``sa.Enum(..., name=...)``
per ``create_table``, which raises ``DuplicateObject`` on postgres (see
``7ebf91b5526c_initial_schema.py``) -- does not arise here. Nothing was
hand-corrected on that account.

Revision ID: e5c2f81a4b90
Revises: d3a17c56e0b4
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5c2f81a4b90'
down_revision: Union[str, Sequence[str], None] = 'd3a17c56e0b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add demand_lines.created_at. Existing rows get this migration's timestamp
    -- see the module docstring for why nothing better exists."""
    with op.batch_alter_table('demand_lines', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'created_at',
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            )
        )


def downgrade() -> None:
    """Drop the column again. Real, and lossy in one direction only.

    The timestamps are lost, which for rows that predated ``upgrade()`` costs
    nothing (they were the migration's own clock, not real facts). For rows created
    AFTER the upgrade it discards a genuine fact, and re-upgrading cannot recover
    it -- those rows would come back holding the second migration's timestamp.

    The initial-revision rows written by ``app.models.demand
    ._write_initial_revision`` are NOT deleted here. They are ordinary,
    append-only ``demand_revisions`` rows carrying real values; the older
    application code reads them happily (it already reconstructs state from
    revisions), and deleting history to undo a schema change would destroy data
    this migration never created.
    """
    with op.batch_alter_table('demand_lines', schema=None) as batch_op:
        batch_op.drop_column('created_at')
