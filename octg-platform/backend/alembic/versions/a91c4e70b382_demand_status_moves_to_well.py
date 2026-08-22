"""demand status becomes a property of the WELL, not of the demand line

WHAT MOVES
----------
  + ``wells.demand_status``            NOT NULL enum, back-filled (see below)
  - ``demand_lines.status``            DROPPED
  + ``scenario_overrides.target_well_id``  nullable FK, for WELL overrides
  ~ ``scenario_overrides``             existing DEMAND_LINE/``status`` override rows
                                       are CONVERTED to WELL/``demand_status``
  = ``demand_revisions.status``        UNTOUCHED, and deliberately so

``demand_revisions.status`` stays because a revision is a SNAPSHOT OF WHAT THE
STATE WAS, not a live copy of where the state lives. It is what
``app.engines.executive._state_as_of`` reconstructs the demand book from and what
the Home Dashboard's "Demand Changes" card reports transitions off. Dropping it
would not tidy anything; it would delete history. See
``app.models.demand.DemandRevision``.

*** THE BACK-FILL RULE, STATED LOUDLY BECAUSE IT IS A CHOICE ***
================================================================
Each well's ``demand_status`` is set to the MOST COMMITTED status held by ANY of
its demand lines, on the ordering

    Confirmed  >  Budgeted  >  Planned

A well with NO demand lines gets ``PLANNED``.

This is a CHOICE, not a derivation. The state being eliminated is precisely a well
whose lines DISAGREE -- that shape was representable before this migration and the
demo data represented it -- so for those wells there is no single pre-existing fact
to read. Any rule is a decision about what the planner meant, and the decision made
here is stated rather than left to be discovered by whoever hits it.

Why "most committed" and not the alternatives:

  * MOST COMMITTED (chosen). The platform's default coverage scope is Confirmed
    only. A well that held even one Confirmed line held demand somebody had
    actually committed to; sending that well to Budgeted or Planned would drop
    ALL of its demand out of coverage scope silently -- the well would simply
    stop having a verdict, disappearing from the coverage grid and the MRP
    recommendations with no error anywhere. Promoting the well instead keeps that
    demand visible and evaluated. If the promotion is wrong for a given well, the
    symptom is a well appearing in scope that a planner can see and correct in one
    action; the opposite error is a well silently vanishing, which nobody sees.
  * LEAST COMMITTED. Rejected for exactly that reason: it is the silent-vanishing
    direction.
  * MOST COMMON / MAJORITY. Rejected. It is arithmetic dressed as judgement -- two
    Planned stage lines would outvote a Confirmed production string -- and it has no
    story a planner could be told.
  * REFUSE TO MIGRATE UNLESS EVERY WELL AGREES. Considered seriously, and rejected
    for THIS schema: disagreement is the normal state of the data this runs
    against (the demo seed's Eagle-01 held a Confirmed line and a Budgeted line),
    so a refusal would simply make the migration unrunnable and invite someone to
    hand-edit rows under time pressure. The honest handling of a guess that cannot
    be avoided is to name it, which is what this docstring does.

AFTER RUNNING THIS, CHECK THE PROMOTED WELLS. They are exactly the wells whose
lines disagreed, and this query lists them BEFORE you upgrade (run it first, keep
the output):

    SELECT w.id, w.name, COUNT(DISTINCT d.status) AS statuses,
           GROUP_CONCAT(DISTINCT d.status) AS which
    FROM wells w JOIN demand_lines d ON d.well_id = w.id
    GROUP BY w.id, w.name HAVING COUNT(DISTINCT d.status) > 1;

There is no way to run it afterwards -- ``demand_lines.status`` is gone -- which is
why it is here rather than in a follow-up note.

NO REVISION IS WRITTEN FOR THE BACK-FILL
----------------------------------------
The wells get their status by ``UPDATE``, not through
``app.engines.coverage.set_well_demand_status``. So no ``DemandRevision`` and no
``ImpactRecord`` record it, and the Home Dashboard will not show a wave of status
changes dated today. That is deliberate: nothing actually changed about the plan,
the representation changed, and manufacturing a revision per line would assert that
a planner made a decision at the moment of a schema migration. Each line's existing
revision history is left exactly as it is -- the rows are true, they record the
status the line really carried at the time.

SCENARIO OVERRIDES ARE CONVERTED, NOT DELETED
---------------------------------------------
``(DEMAND_LINE, 'status')`` is no longer a legal override --
``app.engines.overrides.OVERRIDE_FIELDS`` does not list it, and the resolver
re-validates every row it reads, so such a row would make
``GET /scenarios/{id}/preview`` fail with 409 forever. Each one is rewritten in
place to ``(WELL, 'demand_status')`` pointing at the line's OWN well, which is the
same question the planner asked expressed at the granularity that now exists. The
scenario keeps its history and keeps previewing.

WHAT ``downgrade()`` CAN AND CANNOT RESTORE
------------------------------------------
It is real and it restores the SHAPE completely: ``demand_lines.status`` comes back
NOT NULL, populated from each line's well, ``wells.demand_status`` and
``scenario_overrides.target_well_id`` are dropped, and the converted overrides are
turned back into ``(DEMAND_LINE, 'status')`` rows.

Plainly, what it CANNOT restore:

  * **The original per-line statuses.** Every line of a well comes back carrying
    the WELL's single status. For a well whose lines disagreed before ``upgrade()``,
    that disagreement is gone and cannot be recovered from this schema -- the facts
    were overwritten by the back-fill and there is nowhere else they are recorded
    in a form the downgrade could read. (They are visible in
    ``demand_revisions.status`` for lines that had revisions, but a revision is a
    past state and using it as a present one would be a second guess on top of the
    first. This migration does not do that.) Down-then-up is therefore LOSSY for
    exactly the rows the change was about. Take a backup first; this migration is
    not one.
  * **WHICH demand line a converted scenario override originally named.** Going
    forward it names a well, and a well has many lines. The downgrade re-targets
    such an override at that well's EARLIEST-ROS line (deterministic, and stated
    here so it is not mistaken for the original). A scenario round-tripped through
    down-and-up therefore keeps its question but may have changed which line it is
    recorded against.

MECHANICS
---------
``render_as_batch=True`` is configured in ``alembic/env.py``, so sqlite gets a
table rebuild around the add/drop and the NOT NULL tightening; the column is added
NULLABLE, back-filled, and only then constrained, because a NOT NULL column with no
server default cannot be added to a populated table.

The project's autogenerate trap applies and is fixed the established way (see
``7ebf91b5526c_initial_schema.py``): autogenerate emits a bare inline
``sa.Enum(..., name='demandstatus')`` per table, and on postgres each one issues its
own ``CREATE TYPE``, so a second mention raises ``DuplicateObject``. The enum is
declared ONCE here as a module-level object whose postgres variant carries
``create_type=False``. ``demandstatus`` ALREADY EXISTS in this database (it is used
by ``demand_revisions.status`` and by ``demand_import_rows.status``), so this
revision must NOT create it and must NOT drop it -- neither ``upgrade`` nor
``downgrade`` touches the type at all. That is the difference from
``f2b6d4c81a37``, which introduced its enum and therefore owns it.

``scenariotargetkind`` DOES need a new member, ``'WELL'``. On postgres that is
``ALTER TYPE ... ADD VALUE``, which is why it is issued explicitly and only for
that dialect; on sqlite an enum is VARCHAR + a CHECK constraint, and the batch
rebuild of ``scenario_overrides`` (for the added column) regenerates the constraint
from the model, so nothing extra is needed. Postgres cannot REMOVE an enum value,
so ``downgrade()`` leaves ``'WELL'`` in the type and says so -- an unused enum
member is inert, and the alternative (recreate the type, rewrite every dependent
column) is a far bigger hammer than the problem.

Because sqlite rebuilds three tables here, this migration is NOT safe to run
against a database an application server is holding open. Stop the server first.

Revision ID: a91c4e70b382
Revises: f2b6d4c81a37
Create Date: 2026-08-05 16:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a91c4e70b382'
down_revision: Union[str, Sequence[str], None] = 'f2b6d4c81a37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEMAND_STATUS_VALUES = ('PLANNED', 'BUDGETED', 'CONFIRMED')
_DEMAND_STATUS_NAME = 'demandstatus'

#: Declared once; the postgres variant never auto-creates the type. The type is
#: PRE-EXISTING in this schema (demand_revisions.status, demand_import_rows.status),
#: so this revision neither creates nor drops it -- see the module docstring.
DEMAND_STATUS = sa.Enum(
    *_DEMAND_STATUS_VALUES, name=_DEMAND_STATUS_NAME
).with_variant(
    postgresql.ENUM(
        *_DEMAND_STATUS_VALUES, name=_DEMAND_STATUS_NAME, create_type=False
    ),
    'postgresql',
)

#: Firmness order, most committed LAST. The back-fill takes the MAX under this
#: ordering across a well's lines. Stated as data so the rule is one readable line
#: rather than three CASE expressions.
_FIRMNESS = ('PLANNED', 'BUDGETED', 'CONFIRMED')

#: A well with no demand lines at all. Nothing has been committed for it, so the
#: least committed status is the only claim the data supports.
_NO_LINES = 'PLANNED'


def _add_well_enum_member() -> None:
    """Add 'WELL' to the postgres ``scenariotargetkind`` type.

    sqlite needs nothing: its enums are CHECK constraints, and the batch rebuild
    below regenerates ``scenario_overrides`` from the model, which already lists
    the new member.
    """
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    # IF NOT EXISTS so a re-run (or a database stamped mid-flight) is not fatal.
    op.execute("ALTER TYPE scenariotargetkind ADD VALUE IF NOT EXISTS 'WELL'")


def upgrade() -> None:
    """Move demand status from the line to the well. See the module docstring for
    the back-fill rule -- it is a CHOICE and it is stated there in full."""
    _add_well_enum_member()

    # ---- 1. wells.demand_status, added NULLABLE so a populated table takes it ----
    with op.batch_alter_table('wells', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('demand_status', DEMAND_STATUS, nullable=True)
        )

    # ---- 2. back-fill: the MOST COMMITTED status any of the well's lines held ----
    # One UPDATE per status, applied least-committed first, so a later pass can only
    # ever promote a well further. That ordering is what makes "maximum firmness"
    # true without a portable MAX-over-an-enum expression, which sqlite and postgres
    # would spell differently.
    for status in _FIRMNESS:
        op.execute(
            sa.text(
                'UPDATE wells SET demand_status = :status '
                'WHERE id IN (SELECT well_id FROM demand_lines '
                '             WHERE status = :status)'
            ).bindparams(status=status)
        )
    # Wells with no demand lines were never touched above.
    op.execute(
        sa.text(
            'UPDATE wells SET demand_status = :fallback '
            'WHERE demand_status IS NULL'
        ).bindparams(fallback=_NO_LINES)
    )

    # ---- 3. tighten to NOT NULL -------------------------------------------
    # The constraint is the point of the change, not a decoration on it: a nullable
    # demand_status would let the next insert reintroduce "firmness unknown", and
    # the status filter has no honest answer for such a well -- it would be silently
    # excluded from every default-filter screen.
    with op.batch_alter_table('wells', schema=None) as batch_op:
        batch_op.alter_column(
            'demand_status', existing_type=DEMAND_STATUS, nullable=False
        )

    # ---- 4. scenario_overrides: new target column, then convert the rows ----
    with op.batch_alter_table('scenario_overrides', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('target_well_id', sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            'fk_scenario_overrides_target_well_id_wells',
            'wells',
            ['target_well_id'],
            ['id'],
        )

    # `(DEMAND_LINE, 'status')` is no longer a legal override and would make the
    # scenario's preview fail forever. Rewritten in place to the well-level form:
    # same question, expressed at the granularity that now exists.
    op.execute(
        sa.text(
            "UPDATE scenario_overrides "
            "SET target_well_id = (SELECT well_id FROM demand_lines "
            "                      WHERE demand_lines.id = "
            "                            scenario_overrides.target_demand_line_id) "
            "WHERE target_kind = 'DEMAND_LINE' AND field_name = 'status'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE scenario_overrides "
            "SET target_kind = 'WELL', field_name = 'demand_status', "
            "    target_demand_line_id = NULL "
            "WHERE target_kind = 'DEMAND_LINE' AND field_name = 'status' "
            "  AND target_well_id IS NOT NULL"
        )
    )
    # An override whose demand line has since been deleted has no well to point at,
    # so it cannot be converted. Deleted rather than left behind as a row that would
    # 409 every preview of its scenario from now on; it already referenced nothing.
    op.execute(
        sa.text(
            "DELETE FROM scenario_overrides "
            "WHERE target_kind = 'DEMAND_LINE' AND field_name = 'status'"
        )
    )

    # ---- 5. drop demand_lines.status --------------------------------------
    with op.batch_alter_table('demand_lines', schema=None) as batch_op:
        batch_op.drop_column('status')


def downgrade() -> None:
    """Put ``demand_lines.status`` back, populated from each line's well.

    Real, and LOSSY in one direction -- see the module docstring's "WHAT
    ``downgrade()`` CAN AND CANNOT RESTORE". In short: every line of a well comes
    back at the WELL's single status, so a pre-upgrade disagreement between a well's
    lines is not recoverable, and a converted scenario override is re-targeted at
    the well's earliest-ROS line rather than at whichever line it originally named.
    """
    # ---- 1. demand_lines.status, added NULLABLE then filled from the well ----
    with op.batch_alter_table('demand_lines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', DEMAND_STATUS, nullable=True))

    op.execute(
        sa.text(
            'UPDATE demand_lines SET status = '
            '(SELECT demand_status FROM wells WHERE wells.id = demand_lines.well_id)'
        )
    )
    # Belt and braces: a line whose well row is missing (only possible if foreign
    # keys were not enforced) would otherwise block the NOT NULL below. It gets the
    # same fallback a well with no lines got on the way up, and the fact that the
    # value is a fallback is what this comment exists to record.
    op.execute(
        sa.text(
            'UPDATE demand_lines SET status = :fallback WHERE status IS NULL'
        ).bindparams(fallback=_NO_LINES)
    )

    with op.batch_alter_table('demand_lines', schema=None) as batch_op:
        batch_op.alter_column(
            'status', existing_type=DEMAND_STATUS, nullable=False
        )

    # ---- 2. scenario_overrides: convert WELL overrides back, drop the column ----
    # Earliest ROS, then id, so the choice is deterministic. It is a CHOICE and not
    # a recovery: the original line is not recorded anywhere after the upgrade.
    op.execute(
        sa.text(
            "UPDATE scenario_overrides "
            "SET target_kind = 'DEMAND_LINE', field_name = 'status', "
            "    target_demand_line_id = "
            "      (SELECT id FROM demand_lines "
            "       WHERE demand_lines.well_id = scenario_overrides.target_well_id "
            "       ORDER BY demand_lines.ros_date, demand_lines.id LIMIT 1) "
            "WHERE target_kind = 'WELL' AND field_name = 'demand_status'"
        )
    )
    # A well with no demand lines leaves such an override pointing at nothing, which
    # is not a legal DEMAND_LINE override. Deleted, for the same reason the upgrade
    # deletes unconvertible rows: a persisted illegal override 409s every preview of
    # its scenario.
    op.execute(
        sa.text(
            "DELETE FROM scenario_overrides "
            "WHERE target_kind = 'WELL' OR "
            "      (field_name = 'status' AND target_demand_line_id IS NULL)"
        )
    )

    with op.batch_alter_table('scenario_overrides', schema=None) as batch_op:
        batch_op.drop_constraint(
            'fk_scenario_overrides_target_well_id_wells', type_='foreignkey'
        )
        batch_op.drop_column('target_well_id')

    # ---- 3. drop wells.demand_status --------------------------------------
    with op.batch_alter_table('wells', schema=None) as batch_op:
        batch_op.drop_column('demand_status')

    # The `demandstatus` enum type is NOT dropped: it pre-dates this revision and is
    # still used by demand_revisions.status and demand_import_rows.status. Nor is
    # 'WELL' removed from `scenariotargetkind` -- postgres cannot remove an enum
    # value, and an unused member is inert. Both are stated rather than silently
    # skipped.
