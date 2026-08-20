import enum
from datetime import date, datetime, timezone

from sqlalchemy import Enum as SAEnum
from sqlalchemy import DateTime, Date, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid
from app.models.demand import DemandProfile
from app.models.product import UnitOfMeasure


class DemandImportStatus(enum.Enum):
    PENDING = "pending"
    APPLIED = "applied"
    DISCARDED = "discarded"


class DemandImport(Base):
    """spec §データモデル: demand_imports. Import staging, customer-scoped (裁定D-1)."""

    __tablename__ = "demand_imports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[DemandImportStatus] = mapped_column(
        SAEnum(DemandImportStatus, values_callable=lambda e: [m.value for m in e]),
        default=DemandImportStatus.PENDING,
        nullable=False,
    )


class DemandImportRow(Base):
    """spec §データモデル: demand_import_rows. Staged rows for a pending import."""

    __tablename__ = "demand_import_rows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    import_id: Mapped[str] = mapped_column(ForeignKey("demand_imports.id"), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    well_name: Mapped[str] = mapped_column(String(200), nullable=False)
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
