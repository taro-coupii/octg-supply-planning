import enum
from datetime import date, datetime, timezone

from sqlalchemy import CheckConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy import DateTime, Date, ForeignKey, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid
from app.models.product import UnitOfMeasure


class DemandProfile(enum.Enum):
    PRIMARY = "Primary"
    CONTINGENCY = "Contingency"


class DemandRevisionSource(enum.Enum):
    IMPORT = "import"
    MANUAL = "manual"
    STATUS_CHANGE = "status_change"


class DemandLine(Base):
    """spec §データモデル: demand_lines. No status column (§3-2: status lives on Well only)."""

    __tablename__ = "demand_lines"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    well_id: Mapped[str] = mapped_column(ForeignKey("wells.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[UnitOfMeasure] = mapped_column(
        SAEnum(UnitOfMeasure, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    ros_date: Mapped[date] = mapped_column(Date, nullable=False)
    profile: Mapped[DemandProfile] = mapped_column(
        SAEnum(DemandProfile, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )


class DemandRevision(Base):
    """spec §データモデル: demand_revisions. Append-only (no UPDATE/DELETE API)."""

    __tablename__ = "demand_revisions"
    __table_args__ = (
        UniqueConstraint("well_id", "revision_no", name="uq_demand_revisions_well_id_revision_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    well_id: Mapped[str] = mapped_column(ForeignKey("wells.id"), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    source: Mapped[DemandRevisionSource] = mapped_column(
        SAEnum(DemandRevisionSource, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
