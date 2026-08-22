import enum

from sqlalchemy import (
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    event,
    select,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db import Base
from app.models.customer import _uuid


class DemandStatus(str, enum.Enum):
    """How firm a well's programme is. A property of the WELL, not of the line.

    The column lives on `app.models.well.Well.demand_status`; see that class for
    why. It is still declared here because `DemandRevision` records it as a
    historical snapshot and because `DemandImportRow` stages it off a spreadsheet
    cell.
    """

    PLANNED = "Planned"
    BUDGETED = "Budgeted"
    CONFIRMED = "Confirmed"


class DemandProfile(str, enum.Enum):
    PRIMARY = "Primary"
    CONTINGENCY = "Contingency"


class DemandLine(Base):
    """One product requirement of one well: a quantity, a date and a profile.

    THERE IS NO `status` COLUMN HERE, DELIBERATELY
    ---------------------------------------------
    Demand status is a property of the WELL --
    `app.models.well.Well.demand_status` -- because status does not vary within a
    well (the product owner: confirming a well confirms every line of it). Read it
    as `line.well.demand_status`; there is intentionally no shortcut property on
    this class, because a `line.status` that merely forwarded would re-create
    exactly the impression the move exists to remove.

    `profile` DOES live here: a Confirmed well routinely carries both Primary and
    Contingency demand, so profile genuinely varies line by line.
    """

    __tablename__ = "demand_lines"

    id = Column(String(36), primary_key=True, default=_uuid)
    well_id = Column(String(36), ForeignKey("wells.id"), nullable=False)
    product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    quantity = Column(Float, nullable=False)
    ros_date = Column(DateTime, nullable=False)
    profile = Column(SAEnum(DemandProfile), nullable=False, default=DemandProfile.PRIMARY)
    current_revision_no = Column(Integer, nullable=False, default=1)
    #: When this demand line first existed. Required so a past state of the demand
    #: book can be reconstructed: without it, a line with no revision at or before
    #: a comparison date is indistinguishable between "existed and never changed"
    #: and "did not exist yet", which is what made the Executive Dashboard's
    #: prior-period comparison unavailable. Server default, so every insert gets
    #: one whatever route created the row.
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    well = relationship("Well", back_populates="demand_lines")
    product = relationship("Product")
    revisions = relationship(
        "DemandRevision",
        back_populates="demand_line",
        order_by="DemandRevision.revision_no",
        # Deleting a demand line deletes its history with it. Necessary now that
        # EVERY line has at least one revision from creation: without the cascade
        # SQLAlchemy would try to NULL the revision's demand_line_id, which is NOT
        # NULL, and any delete would raise. It is also the honest behaviour --
        # `app.engines.executive._state_as_of` already documents that a removed
        # line's history goes with it, since a revision pointing at nothing
        # reconstructs nothing.
        cascade="all, delete-orphan",
    )
    coverage_result = relationship(
        "CoverageResult", back_populates="demand_line", uselist=False
    )


class DemandRevision(Base):
    """Full revision history for a demand line. Rows are append-only -- a
    revision is never edited or deleted once written.

    WHY `status` IS STILL A COLUMN HERE WHEN THE LIVE COLUMN MOVED TO Well
    ---------------------------------------------------------------------
    A revision is a SNAPSHOT OF WHAT THE STATE WAS, not a live copy of where the
    state lives. Demand status was a fact about this line's well at the moment the
    revision was written, and recording it here is what makes the history
    reconstructable -- `app.engines.executive._state_as_of` rebuilds each line's
    past state (quantity, ROS, status, profile) purely from these rows, and the
    Home Dashboard's "Demand Changes" card and `ImpactRecord` report status
    transitions off the same facts. Dropping the column would not simplify
    anything; it would delete history and make both features unanswerable.

    That is also how the functional spec is honoured. The spec lists "Demand
    Status Changes" among the changes the revision model records AND says
    revisions occur at demand-line level. Both hold, because a status change is a
    WELL-LEVEL OPERATION THAT WRITES A REVISION TO EVERY LINE OF THAT WELL (see
    `app.engines.coverage.set_well_demand_status`). The well is the single
    authority on the current value; each line's history stays complete.

    Consequence worth stating: within one well, the revisions of all its lines
    that were written by the same status change carry the SAME `status`. That is
    not redundancy to be normalised away later -- it is the per-line history the
    spec asks for, and each row is independently meaningful because each line also
    carries its own quantity, ROS and profile at that moment.
    """

    __tablename__ = "demand_revisions"

    id = Column(String(36), primary_key=True, default=_uuid)
    demand_line_id = Column(String(36), ForeignKey("demand_lines.id"), nullable=False)
    revision_no = Column(Integer, nullable=False)
    quantity = Column(Float, nullable=False)
    ros_date = Column(DateTime, nullable=False)
    status = Column(SAEnum(DemandStatus), nullable=False)
    profile = Column(SAEnum(DemandProfile), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    demand_line = relationship("DemandLine", back_populates="revisions")


@event.listens_for(DemandLine, "after_insert")
def _write_initial_revision(mapper, connection, target: DemandLine) -> None:
    """Record a newly-created demand line's INITIAL state as a revision row.

    Why this is an ORM event rather than a call in each creator
    ----------------------------------------------------------
    History has to be complete from creation or it cannot be reconstructed at all
    (see `app.engines.executive._state_as_of`). "Complete" is a property of every
    insert, not of the polite call sites, so the guarantee is attached to the
    insert itself: seed, tests, imports and any future route all get it, and there
    is no second convention to keep in step.

    The invariant it establishes, stated once
    ----------------------------------------
        A DemandRevision numbered `current_revision_no` always exists.

    That is exactly what `app.engines.coverage.apply_revision` already assumes
    when it appends `current_revision_no + 1`, so the two agree by construction.

    It follows that this writes NOTHING when a line is inserted at
    `current_revision_no = 0`. That is not a special case bolted on: the Excel
    import path (`app.engines.demand_import.apply_batch`) deliberately creates a
    zero-quantity line at revision 0 and then calls `apply_revision`, so that
    `apply_revision` stays the single writer of demand and revision 1 carries the
    IMPORTED values rather than a placeholder. Under the invariant above, revision
    0 means "no state has been committed yet", and the row that arrives moments
    later is revision 1 -- the same numbering the direct path produces. One
    convention, two entry points.

    Existing rows are not back-filled. The facts do not exist -- see the Alembic
    revision's docstring.

    Where the STATUS in this snapshot comes from
    -------------------------------------------
    From the line's WELL, read with a SELECT against the `wells` table through the
    flush's own `connection` rather than through `target.well`. Two reasons, both
    mechanical: a relationship access inside `after_insert` would lazy-load (or
    autoflush) mid-flush, and `app.models.well` imports THIS module, so importing
    `Well` here would be a cycle. `Base.metadata.tables` needs neither.

    The well row is guaranteed to be present: `demand_lines.well_id` is a NOT NULL
    foreign key and SQLAlchemy inserts parent tables before dependent ones, so by
    the time this fires the well exists inside this transaction. If it somehow does
    not, this RAISES rather than writing a revision with a guessed status -- a
    history row is only worth having if it is true.
    """
    if (target.current_revision_no or 0) < 1:
        return
    wells = Base.metadata.tables["wells"]
    status = connection.execute(
        select(wells.c.demand_status).where(wells.c.id == target.well_id)
    ).scalar()
    if status is None:
        raise RuntimeError(
            f"Demand line {target.id!r} names well {target.well_id!r}, which has no "
            "row (or no demand_status) in this transaction, so the initial revision "
            "cannot record a true demand status. Create the well first."
        )
    connection.execute(
        DemandRevision.__table__.insert().values(
            id=_uuid(),
            demand_line_id=target.id,
            revision_no=target.current_revision_no,
            quantity=target.quantity,
            ros_date=target.ros_date,
            status=status,
            profile=target.profile,
            created_at=target.created_at or func.now(),
        )
    )
