from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid
from app.models.product import UnitOfMeasure


class CustomerOwnedInventory(Base):
    """spec §データモデル: customer_owned_inventory. Only stock the platform writes."""

    __tablename__ = "customer_owned_inventory"
    __table_args__ = (
        UniqueConstraint("customer_id", "product_id", name="uq_customer_owned_inventory_customer_product"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[UnitOfMeasure] = mapped_column(
        SAEnum(UnitOfMeasure, values_callable=lambda e: [m.value for m in e])
    )


class CustomerOwnedUpload(Base):
    """spec §データモデル: customer_owned_uploads. has_uploaded is determined by row existence here."""

    __tablename__ = "customer_owned_uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    filename: Mapped[str] = mapped_column(String(200))
    row_count: Mapped[int] = mapped_column(Integer)
