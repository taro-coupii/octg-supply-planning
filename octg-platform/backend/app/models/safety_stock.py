import uuid

from sqlalchemy import Column, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class SafetyStock(Base):
    """The planner-set safety stock for ONE product, in that product's unit.

    PLATFORM-OWNED (like lead-time components and the coverage scope default):
    Oracle holds no such table, so this one is writable through Administration.

    WHERE IT IS ALLOWED TO ACT -- and where it is not
    -------------------------------------------------
    Safety stock changes WHEN TO ORDER, not WHETHER DEMAND IS COVERED. It is
    read by the Material Order Requirements engine (app.engines.mor), where
    dipping below it triggers an order requirement. It is deliberately NOT
    read by the coverage engine: coverage answers "is there steel for this
    demand line", a question of fact, and folding a planning buffer into it
    would produce "Uncovered" verdicts for wells whose steel physically
    exists. If that boundary is ever crossed, it must be a deliberate,
    documented decision -- not a convenient import.

    One row per product, no row means "no safety stock set" (a real, common
    state -- rendered as such, never as 0-with-implied-authority).
    """

    __tablename__ = "safety_stocks"
    __table_args__ = (
        UniqueConstraint("product_id", name="uq_safety_stock_product"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    #: In the product's own unit_of_measure -- no unit column, no conversion.
    quantity = Column(Float, nullable=False)
    #: Free-text rationale, display-only.
    note = Column(String, nullable=True)

    product = relationship("Product")
