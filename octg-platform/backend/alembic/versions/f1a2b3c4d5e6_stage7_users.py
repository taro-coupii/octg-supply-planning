"""stage7 users table (auth)

Revision ID: f1a2b3c4d5e6
Revises: 4544b6337278
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = '4544b6337278'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'users',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('email', sa.String(length=200), nullable=False),
        sa.Column('password_hash', sa.String(length=300), nullable=False),
        sa.Column('role', sa.Enum('ADMIN', 'PLANNER', name='userrole'), nullable=False),
        sa.Column('business_unit_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ['business_unit_id'], ['business_units.id'],
            name=op.f('fk_users_business_unit_id_business_units'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
        sa.UniqueConstraint('email', name=op.f('uq_users_email')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('users')
