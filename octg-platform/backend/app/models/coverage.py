import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class CoverageVerdict(enum.Enum):
    COVERED = "Covered"
    COVERED_VIA_SUBSTITUTE = "CoveredViaSubstitute"
    PENDING_APPROVAL = "PendingApproval"
    UNCOVERED = "Uncovered"
    UNRECOVERABLE = "Unrecoverable"


class CoverageResult(Base):
    """spec §データモデル: coverage_results. Written only by the coverage engine (§3-6).

    Absence of a row means the demand line has never been evaluated (NotEvaluated) —
    never treated as 0 or Covered.
    """

    __tablename__ = "coverage_results"
    __table_args__ = (
        UniqueConstraint("demand_line_id", name="uq_coverage_results_demand_line_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    demand_line_id: Mapped[str] = mapped_column(ForeignKey("demand_lines.id"), nullable=False)
    verdict: Mapped[CoverageVerdict] = mapped_column(
        SAEnum(CoverageVerdict, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    covered_qty: Mapped[float] = mapped_column(Float, nullable=False)
    covered_via: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
