import enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class UserRole(enum.Enum):
    ADMIN = "ADMIN"
    PLANNER = "PLANNER"


class User(Base):
    """spec §データモデル: users. Invariant: PLANNER requires business_unit_id;
    ADMIN with business_unit_id=None means all-BU access."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    business_unit_id: Mapped[str | None] = mapped_column(ForeignKey("business_units.id"))


@event.listens_for(User, "before_insert")
@event.listens_for(User, "before_update")
def _validate_planner_requires_bu(mapper, connection, target: User) -> None:
    """Model-layer invariant (spec §データモデル): PLANNER must have a
    business_unit_id; ADMIN with business_unit_id=None means all-BU access."""
    if target.role == UserRole.PLANNER and target.business_unit_id is None:
        raise ValueError("PLANNER requires business_unit_id")
