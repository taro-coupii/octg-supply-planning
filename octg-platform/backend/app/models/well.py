import enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class DemandStatus(enum.Enum):
    PLANNED = "Planned"
    BUDGETED = "Budgeted"
    CONFIRMED = "Confirmed"


class Well(Base):
    """spec §データモデル: wells. demand_status lives here only (§3-2)."""

    __tablename__ = "wells"
    __table_args__ = (
        UniqueConstraint("customer_id", "name", name="uq_wells_customer_id_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200))
    demand_status: Mapped[DemandStatus] = mapped_column(
        SAEnum(DemandStatus, values_callable=lambda e: [m.value for m in e]),
        default=DemandStatus.PLANNED,
        nullable=False,
    )
