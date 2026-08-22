"""unitofmeasure gains FT and JT

ADDITIVE ONLY. No existing row is touched: every product currently stamped
'MTR', 'PC' or 'MT' keeps that value exactly. This migration only widens the set
of values the enum column will accept, for `app.engines.units`'s metric-tonnes
conversion layer (see `app.models.product.UnitOfMeasure` and MVP_COMPROMISES.md
C-04/C-05/C-06).

MECHANICS, split by dialect
----------------------------
Postgres: `unitofmeasure` is a NATIVE enum type (see `f2b6d4c81a37`, which
declared it with `create_type=False` on the postgres variant precisely so it is
created/altered explicitly rather than by autogenerate). Adding a member to a
live postgres enum type is `ALTER TYPE ... ADD VALUE`, which this migration
issues twice, once per new member, each guarded with `IF NOT EXISTS` so the
migration is safe to re-run. `ALTER TYPE ... ADD VALUE` cannot run inside the
same transaction as other statements that read the type on some postgres
versions; this migration does nothing else in `upgrade()`, so that restriction is
not in play here.

SQLite: there is no native enum type. `sa.Enum` becomes VARCHAR, and whether a
CHECK constraint enforces membership is a SQLAlchemy/Alembic rendering detail
this project does not otherwise rely on (see `f2b6d4c81a37`'s own note that
"sqlite has no standalone enum types"). Tests in this project create the schema
via `Base.metadata.create_all` directly from the current model, not by running
migrations, so a CHECK-constraint mismatch here has no test consequence -- but
the honest fix for a real sqlite deployment is still applied: the column is
recreated (via batch mode) with the widened set of allowed values, exactly the
same pattern `f2b6d4c81a37` uses for sqlite's inability to alter a column in
place.

`downgrade()` is NOT a clean reverse of an already-used FT/JT value. Postgres has
no `ALTER TYPE ... DROP VALUE`; removing a value from a live enum type requires
recreating the type, which this migration does not attempt because it would
destroy any 'FT'/'JT' row created in the meantime. `downgrade()` therefore
raises rather than silently leaving the additional values in place while the
model layer no longer expects them -- see the docstring on `downgrade()` itself.

Revision ID: b3e97f1c4a62
Revises: d4a7c92e6f18
Create Date: 2026-08-10 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b3e97f1c4a62'
down_revision: Union[str, Sequence[str], None] = 'd4a7c92e6f18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UOM_ENUM_NAME = 'unitofmeasure'
_OLD_VALUES = ('MTR', 'PC', 'MT')
_NEW_VALUES = ('MTR', 'PC', 'MT', 'FT', 'JT')

_NEW_ENUM = sa.Enum(*_NEW_VALUES, name=_UOM_ENUM_NAME).with_variant(
    postgresql.ENUM(*_NEW_VALUES, name=_UOM_ENUM_NAME, create_type=False),
    'postgresql',
)
_OLD_ENUM = sa.Enum(*_OLD_VALUES, name=_UOM_ENUM_NAME).with_variant(
    postgresql.ENUM(*_OLD_VALUES, name=_UOM_ENUM_NAME, create_type=False),
    'postgresql',
)


def upgrade() -> None:
    """Widen `unitofmeasure` to accept 'FT' and 'JT'. No row is rewritten."""
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # Additive, IF NOT EXISTS makes this safe to re-run. Existing rows
        # ('MTR'/'PC'/'MT') are untouched -- ADD VALUE only widens what the type
        # accepts, it does not rewrite any column value.
        op.execute("ALTER TYPE unitofmeasure ADD VALUE IF NOT EXISTS 'FT'")
        op.execute("ALTER TYPE unitofmeasure ADD VALUE IF NOT EXISTS 'JT'")
    else:
        # sqlite: no native enum type to alter. Recreate the column with the
        # widened value set, same batch-mode pattern `f2b6d4c81a37` uses for the
        # column's original creation. Existing values are preserved by the batch
        # copy; nothing is back-filled or reinterpreted.
        with op.batch_alter_table('products', schema=None) as batch_op:
            batch_op.alter_column(
                'unit_of_measure',
                existing_type=_OLD_ENUM,
                type_=_NEW_ENUM,
                existing_nullable=False,
            )


def downgrade() -> None:
    """Refused.

    Postgres has no `ALTER TYPE ... DROP VALUE`; removing 'FT'/'JT' requires
    recreating the enum type, which would fail (or silently destroy data) the
    moment any row has actually been written as 'FT' or 'JT'. There is no way for
    a migration to know, at downgrade time, whether that has happened. Rather
    than guess, this raises -- exactly the "loud rather than a wrong number"
    convention this codebase applies to unconvertible quantities themselves.

    To actually reverse this: confirm no `products` row has `unit_of_measure` in
    ('FT', 'JT'), then hand-write a type-recreation migration for the target
    dialect(s) in use.
    """
    raise NotImplementedError(
        "Cannot safely downgrade past b3e97f1c4a62: removing 'FT'/'JT' from the "
        "unitofmeasure enum requires recreating the type, which is only safe if "
        "no row uses either value. Verify that by hand, then write a "
        "dialect-specific downgrade; this migration deliberately does not guess."
    )
