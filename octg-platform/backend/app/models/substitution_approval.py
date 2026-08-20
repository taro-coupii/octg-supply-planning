import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class SubstitutionApprovalStatus(enum.Enum):
    PENDING = "Pending"
    APPROVED = "Approved"
    REJECTED = "Rejected"


class SubstitutionApproval(Base):
    """spec §データモデル: substitution_approvals. Decided rows are immutable (redecide is 409)."""

    __tablename__ = "substitution_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    well_id: Mapped[str] = mapped_column(ForeignKey("wells.id"), nullable=False)
    demand_line_id: Mapped[str] = mapped_column(ForeignKey("demand_lines.id"), nullable=False)
    technical_substitution_id: Mapped[str] = mapped_column(
        ForeignKey("technical_substitutions.id"), nullable=False
    )
    status: Mapped[SubstitutionApprovalStatus] = mapped_column(
        SAEnum(SubstitutionApprovalStatus, values_callable=lambda e: [m.value for m in e]),
        default=SubstitutionApprovalStatus.PENDING,
        server_default=SubstitutionApprovalStatus.PENDING.value,
        nullable=False,
    )
    customer_approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    well_approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
