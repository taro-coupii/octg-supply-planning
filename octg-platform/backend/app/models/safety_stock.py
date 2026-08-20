from sqlalchemy import CheckConstraint, Enum as SAEnum, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid
from app.models.product import UnitOfMeasure


class SafetyStock(Base):
    """spec §データモデル: safety_stocks. Row absent = unset (null); present with 0 = explicit zero."""

    __tablename__ = "safety_stocks"
    __table_args__ = (
        UniqueConstraint("business_unit_id", "product_id", name="uq_safety_stocks_bu_product"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_unit_id: Mapped[str] = mapped_column(ForeignKey("business_units.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[UnitOfMeasure] = mapped_column(
        SAEnum(UnitOfMeasure, values_callable=lambda e: [m.value for m in e])
    )
