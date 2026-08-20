import enum

from sqlalchemy import Enum as SAEnum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class AllocationPolicy(enum.Enum):
    SOFT = "SOFT"
    HARD = "HARD"
    HYBRID = "HYBRID"


class Customer(Base):
    """Default planning boundary (spec §4). allocation_policy added stage 4 (§裁定A-1)."""

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    business_unit_id: Mapped[str | None] = mapped_column(ForeignKey("business_units.id"))
    allocation_policy: Mapped[AllocationPolicy] = mapped_column(
        SAEnum(AllocationPolicy, values_callable=lambda e: [m.value for m in e]),
        default=AllocationPolicy.SOFT,
        server_default=AllocationPolicy.SOFT.value,
        nullable=False,
    )
