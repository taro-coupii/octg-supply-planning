import enum

from sqlalchemy import Column, DateTime, Enum as SAEnum, Float, ForeignKey, String
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db import Base
from app.models.customer import _uuid


class CoverageStatus(str, enum.Enum):
    COVERED = "Covered"
    COVERED_VIA_SUBSTITUTE = "CoveredViaSubstitute"
    PENDING_APPROVAL = "PendingApproval"
    UNCOVERED = "Uncovered"
    UNRECOVERABLE = "Unrecoverable"


class CoverageResult(Base):
    """Engine-written coverage outcome for a demand line. Never manually
    edited -- always produced by app.engines.coverage.recompute_well() (which
    delegates to recompute_customer).

    Derived data, and treated as such: a row exists only while its demand line
    is IN SCOPE of the active status/profile filters. When a revision moves a
    line out of scope the engine DELETES its row rather than leaving a stale
    verdict behind, so the absence of a row means "not currently evaluated" and
    never "fine".
    """

    __tablename__ = "coverage_results"

    demand_line_id = Column(String(36), ForeignKey("demand_lines.id"), primary_key=True)
    status = Column(SAEnum(CoverageStatus), nullable=False)
    reason = Column(String, nullable=True)

    # WHICH product actually satisfied this line, when one did.
    #
    # Set to the line's own product for COVERED, and to the substitute's product
    # for COVERED_VIA_SUBSTITUTE. NULL for every unsatisfied outcome
    # (PENDING_APPROVAL, UNCOVERED, UNRECOVERABLE) -- nothing has been drawn, so
    # nothing is fulfilling it.
    #
    # This exists so the MRP runout projection can charge each line's
    # consumption to the product it was really drawn from. Without it a
    # substituted line was charged to its own (untouched) product while the
    # substitute's stock looked permanently free, so the same tonnage could be
    # committed twice. MRP READS this; it must never re-derive substitution
    # decisions of its own.
    fulfilled_by_product_id = Column(
        String(36), ForeignKey("products.id"), nullable=True
    )

    # MVP-COMPROMISE[C-08]: recompute is trigger-driven only -- no background job.
    #     WHY:    no scheduler exists in the MVP; computed_at (surfaced on the
    #             coverage grid since 2026-08-12) at least makes the age visible.
    #     REMOVE: recompute on feed updates or on a schedule.
    computed_at = Column(DateTime, nullable=False, server_default=func.now())

    demand_line = relationship("DemandLine", back_populates="coverage_result")
    fulfilled_by_product = relationship("Product")


class ImpactRecord(Base):
    """Before/after snapshot captured whenever a DemandRevision is applied.
    Powers the Home Dashboard 'Demand Changes' card."""

    __tablename__ = "impact_records"

    id = Column(String(36), primary_key=True, default=_uuid)
    demand_line_id = Column(String(36), ForeignKey("demand_lines.id"), nullable=False)
    well_id = Column(String(36), ForeignKey("wells.id"), nullable=False)

    quantity_before = Column(Float, nullable=True)
    quantity_after = Column(Float, nullable=True)
    ros_date_before = Column(DateTime, nullable=True)
    ros_date_after = Column(DateTime, nullable=True)
    status_before = Column(String, nullable=True)
    status_after = Column(String, nullable=True)
    coverage_before = Column(String, nullable=True)
    coverage_after = Column(String, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
