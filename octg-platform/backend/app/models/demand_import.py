"""Excel demand-import STAGING tables.

Per the spec the import flow is

    Upload -> Staging -> Match Existing Demand -> Review -> Apply

and the decisive property is that **the user decides the outcome**. The system
parses, matches and SUGGESTS; it never auto-applies. These two tables are that
staging area: a batch holds the file, a row holds one parsed spreadsheet line
together with the suggestion the matcher made and the decision the user took.

Nothing here is demand. Until `POST /demand-imports/{id}/apply` runs, no
`DemandLine`, `DemandRevision` or `ImpactRecord` exists for any of it, and a
batch that is never applied leaves the demand model untouched.

Rows are deliberately kept even when they FAIL validation
---------------------------------------------------------
A real user uploads malformed files. A bad row is staged with `error` set and
`match_type = ERROR` rather than being dropped or aborting the upload, so the
review screen can show the user exactly which spreadsheet row was wrong and why.
One bad row never blocks the rest of the batch. `raw_*` columns keep the original
cell text verbatim, because the whole point of an error row is to show the user
what they actually typed.

A THIRD state beyond "valid" and "error": CONFLICT
--------------------------------------------------
An ERROR row is one the FILE got wrong -- an unknown well, an unparseable date.
A CONFLICT row is perfectly valid and nonetheless disagrees with the LIVE data it
would land on: it asserts a demand status the well does not have, or it revises a
line somebody else has revised since this batch was staged. Nothing about such a
row is malformed, so refusing it would be wrong; applying it silently would be
worse. It is gated instead -- see `DemandImportRow.override_approved` and
`app.engines.demand_import.detect_conflict`.
"""

import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db import Base
from app.models.customer import _uuid
from app.models.demand import DemandProfile, DemandStatus


class DemandImportBatchStatus(str, enum.Enum):
    STAGED = "Staged"
    APPLIED = "Applied"


class DemandImportMatchType(str, enum.Enum):
    """The matcher's SUGGESTION for one staged row -- never an instruction.

    REVISION  an existing demand line was matched on Well + Product and differs
              in ROS and/or quantity, so the row most likely revises it.
    NEW       no existing demand line for this Well + Product (or none whose ROS
              or quantity is recognisable as the same demand), so the row most
              likely creates new demand.
    ERROR     the row could not be validated at all. Not appliable; the user may
              only skip it.
    """

    REVISION = "Revision"
    NEW = "New"
    ERROR = "Error"


class DemandImportDecision(str, enum.Enum):
    """The USER's decision. PENDING until they make one; apply touches nothing
    else."""

    PENDING = "Pending"
    ACCEPT_REVISION = "AcceptRevision"
    ACCEPT_NEW = "AcceptNew"
    SKIP = "Skip"


class DemandImportBatch(Base):
    __tablename__ = "demand_import_batches"

    id = Column(String(36), primary_key=True, default=_uuid)
    filename = Column(String, nullable=True)
    sheet_name = Column(String, nullable=True)
    status = Column(
        SAEnum(DemandImportBatchStatus),
        nullable=False,
        default=DemandImportBatchStatus.STAGED,
    )
    row_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    applied_at = Column(DateTime, nullable=True)

    rows = relationship(
        "DemandImportRow",
        back_populates="batch",
        order_by="DemandImportRow.row_number",
        cascade="all, delete-orphan",
    )


class DemandImportRow(Base):
    __tablename__ = "demand_import_rows"

    id = Column(String(36), primary_key=True, default=_uuid)
    batch_id = Column(
        String(36), ForeignKey("demand_import_batches.id"), nullable=False
    )
    # 1-based spreadsheet row number, so an error message can point the user at
    # the row they can actually see in Excel.
    row_number = Column(Integer, nullable=False)

    # Verbatim cell text, kept so an error row can show what was typed.
    raw_well = Column(String, nullable=True)
    raw_product = Column(String, nullable=True)
    raw_quantity = Column(String, nullable=True)
    raw_ros_date = Column(String, nullable=True)
    raw_status = Column(String, nullable=True)
    raw_profile = Column(String, nullable=True)

    # Parsed values. All nullable: a row that failed validation has some or all
    # of them missing, and that is a legitimate stored state.
    well_id = Column(String(36), ForeignKey("wells.id"), nullable=True)
    product_id = Column(String(36), ForeignKey("products.id"), nullable=True)
    quantity = Column(Float, nullable=True)
    ros_date = Column(DateTime, nullable=True)
    status = Column(SAEnum(DemandStatus), nullable=True)
    profile = Column(SAEnum(DemandProfile), nullable=True)

    match_type = Column(SAEnum(DemandImportMatchType), nullable=False)
    # Set whenever ANY existing line for this Well + Product was found, even when
    # the suggestion is NEW -- the user is allowed to override the suggestion and
    # needs a target line to revise.
    matched_demand_line_id = Column(
        String(36), ForeignKey("demand_lines.id"), nullable=True
    )
    match_reason = Column(String, nullable=True)
    error = Column(String, nullable=True)

    decision = Column(
        SAEnum(DemandImportDecision),
        nullable=False,
        default=DemandImportDecision.PENDING,
    )

    # ---- The BASELINE: what was live when this row was staged --------------
    #
    # Recorded so that "the world moved under this batch" is DETECTABLE rather than
    # inferred. Between staging and apply somebody else may revise the matched line
    # -- through the demand API, a scenario apply, or another import -- and applying
    # this row would then silently overwrite a change nobody in this review ever
    # saw. `app.engines.demand_import.detect_conflict` compares these three against
    # the line's CURRENT values and reports a CONFLICT when they disagree.
    #
    # There is deliberately NO stored `is_conflict` column. A conflict is a
    # statement about the relationship between this row and LIVE data, and live data
    # keeps moving after staging, so a stored flag would be the answer to a question
    # asked at the wrong moment -- and the review screen would show a stale one.
    # `is_conflict` is therefore always DERIVED, freshly, by the one detector, and
    # `apply_batch` re-derives it at the instant it writes. What is persisted is only
    # the FACT the detector needs (the baseline) and the DECISION a human made about
    # it (the approval below).
    #
    # All three are NULL on a row with no matched line -- a genuinely new demand line
    # has no prior live value to have drifted from, which is exactly why a new row is
    # never a conflict.
    baseline_revision_no = Column(Integer, nullable=True)
    baseline_quantity = Column(Float, nullable=True)
    baseline_ros_date = Column(DateTime, nullable=True)

    # ---- The override APPROVAL --------------------------------------------
    #
    # A SECOND, distinct decision, on top of `decision`. Accept/skip answers "is
    # this row the change I want?"; this answers "I have seen that it conflicts with
    # live data and I approve overriding that". `apply_batch` refuses to write a
    # conflicting row until this is True, and a row with NO conflict never needs it
    # -- the common case is not slowed down.
    #
    # `override_approved_by` is ATTRIBUTION ONLY, exactly like
    # `app.models.scenario.Scenario.created_by`: no code branches on it and it must
    # not start to. This platform has no user model, and inventing one here would be
    # an access-control claim the schema cannot keep.
    override_approved = Column(Boolean, nullable=False, default=False)
    override_approved_at = Column(DateTime, nullable=True)
    override_approved_by = Column(String, nullable=True)
    #: WHO ACTUALLY APPROVED: the authenticated user, set by the server (F09).
    #: `override_approved_by` above is free "on behalf of" text.
    # No FOREIGN KEY on purpose: this is the RECORD of who acted, and it must
    # survive that user later being removed. Resolved view-only.
    override_approved_by_user_id = Column(String(36), nullable=True)
    #: WHAT was approved (F07, owner ruling 2026-09-06): a fingerprint of the
    #: target line's revision and values, the well's demand status and this row's
    #: proposed values at the moment of approval -- see
    #: `app.engines.demand_import.approval_basis`. When the live state no longer
    #: matches, the approval has LAPSED: it stays recorded (it happened) but no
    #: longer authorises the write, and the row needs approving again.
    override_approval_basis = Column(String, nullable=True)

    applied = Column(Boolean, nullable=False, default=False)
    applied_demand_line_id = Column(
        String(36), ForeignKey("demand_lines.id"), nullable=True
    )
    override_approved_by_user = relationship(
        "User",
        primaryjoin="foreign(DemandImportRow.override_approved_by_user_id) == User.id",
        viewonly=True,
    )

    @property
    def override_approved_by_user_name(self) -> str | None:
        u = self.override_approved_by_user
        return u.display_name if u else None
    apply_error = Column(String, nullable=True)

    batch = relationship("DemandImportBatch", back_populates="rows")
    well = relationship("Well")
    product = relationship("Product")
    matched_demand_line = relationship(
        "DemandLine", foreign_keys=[matched_demand_line_id]
    )
