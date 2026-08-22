from sqlalchemy import Column, Enum as SAEnum, ForeignKey, String
from sqlalchemy.orm import relationship

from app.db import Base
from app.models.customer import _uuid
from app.models.demand import DemandStatus


class Well(Base):
    """One well, and THE authority on its demand status.

    TWO STATUS COLUMNS, AND THEY ARE OPPOSITE KINDS OF FACT
    -------------------------------------------------------
    Do not read `demand_status` and `coverage_status` as a pair. They are named
    alike and they are nothing alike:

      demand_status    PLANNER-SET INPUT. How firm this well's programme is
                       (Planned / Budgeted / Confirmed). Nothing derives it; a
                       human decides it, and the only sanctioned writer is
                       `app.engines.coverage.set_well_demand_status`, which also
                       records the change in every affected line's revision
                       history. It is an INPUT to the coverage calculation -- the
                       status filter selects wells by it.
      coverage_status  ENGINE-WRITTEN OUTPUT. The rollup of this well's demand
                       lines' `CoverageResult` verdicts, computed by
                       `app.engines.coverage.recompute_customer` and never edited
                       by a user or an API handler. NULL means "not evaluated".

    So one is cause and the other is effect. A future edit that treats
    `demand_status` as derived would delete a planner's decision on the next
    recompute; one that lets a handler assign `coverage_status` would publish a
    verdict the engine never reached.

    WHY DEMAND STATUS LIVES HERE AND NOT ON DemandLine
    --------------------------------------------------
    The product owner's correction, verbatim:

        Status essentially never varies between the demand lines of a single
        well. If the well is confirmed, every line belonging to it is confirmed.
        Demand of both the primary and the contingency profile coexisting inside
        a confirmed well, on the other hand, is an everyday occurrence.

    Status does not vary within a well: confirming a well confirms every line of
    it. It was previously a column on `DemandLine`, which made "one well at two
    statuses" a representable state -- and the demo data represented it, which is
    what produced the unreadable coverage grid the owner reported (filtering to
    Confirmed dropped a LINE while leaving its WELL, so 14 wells / 15 lines became
    14 wells / 14 lines and no count could be explained).

    The alternative -- keep the column on the line and merely VALIDATE that a
    well's lines agree -- was rejected. It leaves the invalid state representable
    in the database and defends against it only in code, which is not this
    codebase's habit: an unmapped Business Unit means "isolated" rather than
    "shares with everything", an absent `CoverageResult` means "not evaluated"
    rather than "fine", and an absent `InventoryOnHand` row raises rather than
    defaulting to 0. Same principle. With the column here the old invalid state is
    unrepresentable rather than merely disallowed.

    `DemandProfile` (Primary / Contingency) deliberately STAYS on `DemandLine`:
    the owner names a Confirmed well holding both profiles as the routine case, so
    profile genuinely does vary line by line. The status filter therefore selects
    WELLS and the profile filter selects LINES -- see
    `app.engines.coverage.compute_customer_coverage`.
    """

    __tablename__ = "wells"

    id = Column(String(36), primary_key=True, default=_uuid)
    planning_node_id = Column(String(36), ForeignKey("planning_nodes.id"), nullable=False)
    name = Column(String, nullable=False)

    #: PLANNER-SET INPUT -- see the class docstring. NOT NULL: a well whose
    #: firmness is unknown cannot be filtered honestly, and defaulting the
    #: question away is what the nullable-column version of this idea would do.
    demand_status = Column(
        SAEnum(DemandStatus), nullable=False, default=DemandStatus.PLANNED
    )

    # Cached rollup, ENGINE-WRITTEN OUTPUT (see app.engines.coverage). Never
    # edited directly by users or API handlers.
    coverage_status = Column(String, nullable=True)

    planning_node = relationship("PlanningNode", back_populates="wells")
    demand_lines = relationship("DemandLine", back_populates="well")
