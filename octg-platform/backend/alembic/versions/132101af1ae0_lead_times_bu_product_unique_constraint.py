"""lead_times bu_product unique constraint

Revision ID: 132101af1ae0
Revises: 29a15444c917
Create Date: 2026-08-19 19:19:54.699607

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '132101af1ae0'
down_revision: Union[str, Sequence[str], None] = '29a15444c917'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("lead_times", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_lead_times_bu_product", ["business_unit_id", "product_id"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("lead_times", schema=None) as batch_op:
        batch_op.drop_constraint("uq_lead_times_bu_product", type_="unique")
