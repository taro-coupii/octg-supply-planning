"""substitution master data gets its uniqueness constraints

WHY A MIGRATION AT ALL FOR AN "API ONLY" FEATURE
================================================
Adding CRUD for ``technical_substitutions`` and ``customer_substitution_rules``
needed no schema change -- both tables already existed and no column moved. This
revision adds the two UNIQUE constraints those tables never had, and it is here
because making the tables WRITEABLE is what turned their absence from a latent
tidiness issue into a reachable defect.

Seed data contained one row per pair by construction; nothing could create a second
one. Now anything can. Without a constraint:

  * ``technical_substitutions`` -- ``app.engines.substitution.find_candidates``
    appends one ``SubstitutionCandidate`` per technical row, so a duplicate (A, B)
    makes B appear TWICE in the candidate list for every demand line on A, with the
    same physical quantity counted against each. A planner would read twice the
    choice that exists.
  * ``customer_substitution_rules`` -- ``find_candidates`` collapses the rules into a
    dict keyed on ``to_product_id``, so with two rows for one (customer, from, to) the
    winner is whichever row the query returned LAST. An explicit customer VETO could
    be silently overridden by a stale ``allowed=True`` row, and no screen could show
    which one was in force. This is the more dangerous of the two.

``app.api.admin`` pre-checks both and answers 409 with a sentence naming the existing
row, so the constraint is not the ordinary path. It is the backstop for two concurrent
requests that both pass the pre-check -- the same division of labour
``uq_lead_time_component_dimension_value`` and ``_integrity_conflict`` already have,
and what makes "a client never sees a raw database error" true rather than usually
true.

ORDERED PAIRS, DELIBERATELY
---------------------------
``uq_technical_substitution_pair`` constrains (from, to) as an ORDERED pair. A
technical substitution is directional -- see
``app.models.substitution.TechnicalSubstitution`` -- so (A, B) and (B, A) are two
different engineering claims and both may legitimately exist. A constraint on the
unordered pair would forbid registering a genuine reciprocal compatibility.

THIS MIGRATION REFUSES RATHER THAN DEDUPLICATES
===============================================
``upgrade()`` checks for pre-existing duplicates FIRST and raises with the offending
pairs listed if it finds any. It does not delete "the extra" row, and that is the
point rather than laziness: for a customer rule the two rows may hold OPPOSITE
``allowed`` values, so choosing one is choosing a customer's commercial position, and
a migration picking the arbitrary survivor would silently grant or revoke a permission
with a schema change's authority behind it. An operator must decide which row is
correct and remove the other -- both are visible through
``GET /admin/customer-substitution-rules`` -- and then re-run. On a database with no
duplicates, which includes every seeded one, the check is a no-op.

NOTES ON THE MECHANICS
----------------------
sqlite cannot add a constraint in place, hence ``batch_alter_table`` (``env.py`` sets
``render_as_batch=True``). Batch mode RECREATES each table, so this migration is not
safe to run against a database an application server is holding open -- stop the
server first.

The project's usual autogenerate trap does NOT apply here: neither table has an enum
column, so nothing emits a ``sa.Enum(..., name=...)`` and there is no ``CREATE TYPE``
to duplicate on postgres. ``well_substitution_approvals`` -- the one substitution
table that DOES carry an enum (``substitutionapprovalstatus``) -- is deliberately
untouched by this revision, so the trap has no way in.

``downgrade()`` drops both constraints and loses nothing: no data is written, moved or
deleted in either direction.

Revision ID: d5f2a4b91c70
Revises: f1a83c60d27b
Create Date: 2026-08-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd5f2a4b91c70'
down_revision: Union[str, Sequence[str], None] = 'f1a83c60d27b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TECHNICAL_CONSTRAINT = 'uq_technical_substitution_pair'
_RULE_CONSTRAINT = 'uq_customer_substitution_rule_pair'


def _refuse_if_duplicates(table: str, columns: Sequence[str]) -> None:
    """Raise, listing the offending key tuples, if `table` already has duplicates.

    Checked before the constraint is created so the failure names the DATA that has to
    be fixed rather than surfacing as a driver-level "UNIQUE constraint failed" from
    inside a table rebuild. See the module docstring for why nothing is deduplicated
    automatically.
    """
    key = ', '.join(columns)
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                f'SELECT {key}, COUNT(*) AS n FROM {table} '  # noqa: S608 - fixed identifiers
                f'GROUP BY {key} HAVING COUNT(*) > 1'
            )
        )
        .fetchall()
    )
    if not rows:
        return
    listed = '; '.join(
        '(' + ', '.join(str(v) for v in row[:-1]) + f') x{row[-1]}' for row in rows
    )
    raise RuntimeError(
        f'{table} already contains {len(rows)} duplicate ({key}) group(s), so the '
        f'unique constraint cannot be created: {listed}. This migration deliberately '
        'does NOT pick a survivor -- for a customer rule the duplicates may hold '
        'OPPOSITE `allowed` values, so choosing one would silently decide a customer\'s '
        'commercial position. Inspect the rows (GET /admin/technical-substitutions, '
        'GET /admin/customer-substitution-rules), delete the incorrect ones, and re-run '
        '`alembic upgrade head`. Nothing has been changed.'
    )


def upgrade() -> None:
    """Add the two uniqueness constraints. Writes no data; refuses on duplicates."""
    _refuse_if_duplicates(
        'technical_substitutions', ('from_product_id', 'to_product_id')
    )
    _refuse_if_duplicates(
        'customer_substitution_rules',
        ('customer_id', 'from_product_id', 'to_product_id'),
    )

    with op.batch_alter_table('technical_substitutions', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            _TECHNICAL_CONSTRAINT, ['from_product_id', 'to_product_id']
        )
    with op.batch_alter_table('customer_substitution_rules', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            _RULE_CONSTRAINT, ['customer_id', 'from_product_id', 'to_product_id']
        )


def downgrade() -> None:
    """Drop both constraints. Fully reversible -- no row is touched either way."""
    with op.batch_alter_table('customer_substitution_rules', schema=None) as batch_op:
        batch_op.drop_constraint(_RULE_CONSTRAINT, type_='unique')
    with op.batch_alter_table('technical_substitutions', schema=None) as batch_op:
        batch_op.drop_constraint(_TECHNICAL_CONSTRAINT, type_='unique')
