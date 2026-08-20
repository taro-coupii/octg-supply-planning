from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class LeadTime(Base):
    """spec §データモデル: lead_times. Both null = global default; most specific row wins."""

    __tablename__ = "lead_times"
    __table_args__ = (
        CheckConstraint("months > 0", name="months_positive"),
        UniqueConstraint("business_unit_id", "product_id", name="uq_lead_times_bu_product"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_unit_id: Mapped[str | None] = mapped_column(ForeignKey("business_units.id"))
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"))
    months: Mapped[int] = mapped_column(Integer)
