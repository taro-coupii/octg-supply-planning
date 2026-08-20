import enum
from datetime import date

from sqlalchemy import Date, Enum as SAEnum, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid
from app.models.product import UnitOfMeasure


class BookingStatus(enum.Enum):
    POED = "PO'ed"
    BOOKED = "Book'ed"


class InventoryOnHand(Base):
    """spec §データモデル: inventory_on_hand. Oracle read-only projection (§3-5)."""

    __tablename__ = "inventory_on_hand"
    __table_args__ = (
        UniqueConstraint("business_unit_id", "product_id", name="uq_inventory_on_hand_bu_product"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_unit_id: Mapped[str] = mapped_column(ForeignKey("business_units.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[UnitOfMeasure] = mapped_column(
        SAEnum(UnitOfMeasure, values_callable=lambda e: [m.value for m in e])
    )
    source_system: Mapped[str] = mapped_column(String(50), default="manual")


class InventoryAssignment(Base):
    """spec §データモデル: inventory_assignments. Hard allocation; Oracle read-only projection."""

    __tablename__ = "inventory_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_unit_id: Mapped[str] = mapped_column(ForeignKey("business_units.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[UnitOfMeasure] = mapped_column(
        SAEnum(UnitOfMeasure, values_callable=lambda e: [m.value for m in e])
    )
    reference: Mapped[str | None] = mapped_column(String(200))


class InventoryOnOrder(Base):
    """spec §データモデル: inventory_on_order. Oracle read-only projection; expected_date nullable."""

    __tablename__ = "inventory_on_order"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_unit_id: Mapped[str] = mapped_column(ForeignKey("business_units.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[UnitOfMeasure] = mapped_column(
        SAEnum(UnitOfMeasure, values_callable=lambda e: [m.value for m in e])
    )
    expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    booking_status: Mapped[BookingStatus] = mapped_column(
        SAEnum(BookingStatus, values_callable=lambda e: [m.value for m in e])
    )
