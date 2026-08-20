import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class BusinessUnit(Base):
    """Absolute inventory boundary (spec §3): stock never crosses a BU."""

    __tablename__ = "business_units"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("business_units.id"))

    parent: Mapped["BusinessUnit | None"] = relationship(
        back_populates="children", remote_side=[id]
    )
    children: Mapped[list["BusinessUnit"]] = relationship(back_populates="parent")
