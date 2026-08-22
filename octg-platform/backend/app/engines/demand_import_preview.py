"""Coverage impact of applying ONE conflicting import row. Read-only what-if.

The question this answers
------------------------
    "This staged row disagrees with the live book. If I approved the override and
    applied it, what would happen to coverage?"

A planner is being asked to authorise a change whose consequences are, by
definition, larger or different than the diff they were shown -- a well-status
assertion cascades to lines the spreadsheet never listed, and a concurrent-revision
conflict means the diff was computed against a value that no longer exists. Asking
for that authorisation without showing the consequence would be asking for a rubber
stamp.

IT REUSES THE SCENARIO MACHINERY RATHER THAN INVENTING A SECOND ONE
------------------------------------------------------------------
There is exactly one implementation of the coverage rules
(`app.engines.coverage.compute_customer_coverage`) and exactly one way to ask it
"what if these values were different"
(`app.engines.overrides.ScenarioOverrides`). This module uses both. It builds
TRANSIENT `ScenarioOverride` objects describing what the row would do, hands them
to the same resolver a scenario preview uses, and diffs two passes of the same
coverage function -- so a conflict preview cannot disagree with a scenario preview,
with the official verdict, or with what applying actually does, because all four are
the same function.

WHY THE ROW DOES NOT BECOME A REAL `Scenario`
--------------------------------------------
Reusing the RESOLVER is not the same as reusing the TABLE, and the table was
considered and rejected. Four reasons, in decreasing order of how decisive they are:

  1. `ScenarioOverride` CANNOT EXPRESS HALF OF AN IMPORT. Every target kind names an
     existing row (`app.models.scenario.ScenarioTargetKind`); there is no override
     that creates a demand line. An import batch routinely creates new demand, so a
     batch could not be represented as a scenario at all -- only its revisions
     could, and a mechanism that covers half the batch is a second mechanism, not a
     reuse.
  2. A `Scenario` IS SCOPED TO ONE CUSTOMER by construction (see
     `app.models.scenario.Scenario`), because that is the scope coverage is computed
     at. An uploaded spreadsheet is scoped to whatever wells the planner put in it
     and routinely spans customers. Making a batch into scenarios would mean
     splitting one Apply into N applies with no defensible behaviour when the third
     one fails.
  3. APPLIED IS TERMINAL AND IMMUTABLE for a scenario, and a batch already has its
     own Staged -> Applied lifecycle plus a per-row decision. Overlaying the two
     needs reconciliation rules ("the scenario is Applied but its row was skipped")
     that nobody asked for and that no screen could render honestly.
  4. A scenario is a NAMED, SHARED CONVERSATION ARTEFACT -- "what was agreed with
     the customer". A conflicting import row is not an agreement with a customer; it
     is an operator noticing that a spreadsheet is out of step with the book. Filing
     it in the scenario list would put dozens of machine-generated rows in the place
     planners keep their customer conversations, and the scenario list would stop
     being readable.

So the shape is reused and the apparatus is not: the row carries a lightweight
approval decision (`app.models.demand_import.DemandImportRow.override_approved`),
this module gives it a preview built from the scenario engine's own parts, and
applying still goes through the SAME `apply_revision` /
`set_well_demand_status` calls the direct-accept path has always used. One
implementation of applying, not two.

IT WRITES NOTHING
-----------------
The same four layers as `app.engines.sharing` and `app.engines.scenario`,
deliberately copied rather than reinvented:

  1. It never imports the persisting code path. `recompute_customer`,
     `apply_revision`, `set_well_demand_status`, `CoverageResult` and `apply_batch`
     are all absent from this file's imports, so no edit here can reach a write
     without first adding an import -- a visible, reviewable act. (This is also why
     the preview lives here and not in `app.engines.demand_import`, which
     legitimately writes: a module that writes cannot also be a module that
     structurally cannot.)
  2. Every result is a frozen dataclass of scalars. No ORM row and no `LineView`
     escapes.
  3. The whole computation runs inside `Session.no_autoflush`.
  4. `_assert_no_writes` compares the session's pending sets before and after and
     raises if anything changed.

The `ScenarioOverride` objects it constructs are TRANSIENT -- never `db.add`ed --
so they are absent from `Session.new` and layer 4 stays meaningful. That is the
whole reason they can be built at all.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.engines.coverage import CustomerCoverage, compute_customer_coverage
from app.engines.demand_import import (
    CONFLICT_CONCURRENT_REVISION,
    CONFLICT_WELL_DEMAND_STATUS,
    RowConflict,
    detect_conflict,
)
from app.engines.overrides import NO_OVERRIDES, ScenarioOverrides
from app.models import (
    CoverageStatus,
    DemandImportRow,
    PlanningNode,
    ScenarioOverride,
    ScenarioTargetKind,
    Well,
)

_RESOLVED = (CoverageStatus.COVERED, CoverageStatus.COVERED_VIA_SUBSTITUTE)


class ConflictPreviewUnavailable(RuntimeError):
    """The row has no conflict, so there is nothing to preview.

    A distinct exception rather than an empty result: asking for the coverage impact
    of a conflict that does not exist is a caller mistake, and returning zeros would
    let a screen render "approving this changes nothing" about a row that needs no
    approval at all.
    """


# --------------------------------------------------------------------------
# Result shapes -- frozen scalars (layer 2)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewLineChange:
    """One demand line, before -> after, if the override were approved+applied."""

    demand_line_id: str
    well_id: str
    well_name: str
    product_id: str
    product_description: str | None
    #: Labels `quantity_before` and `quantity_after`. One value serves both: an
    #: import row can restate a quantity but never a product's unit.
    unit_of_measure: object
    coverage_before: str
    coverage_after: str
    reason_before: str | None
    reason_after: str | None
    quantity_before: float
    quantity_after: float
    ros_date_before: datetime
    ros_date_after: datetime
    changed: bool
    #: True when this line is one the SPREADSHEET ROW actually names. False marks a
    #: line dragged along by the well-status cascade or by inventory being
    #: reallocated -- which is the most valuable thing this panel shows, because it
    #: is exactly what the row's own diff cannot tell the reviewer.
    named_by_the_row: bool


@dataclass(frozen=True)
class PreviewWellChange:
    well_id: str
    well_name: str
    coverage_before: str | None
    coverage_after: str | None
    changed: bool


@dataclass(frozen=True)
class ImportConflictPreview:
    """Read-only impact of approving and applying ONE conflicting row.

    `is_what_if` is always True and is serialised deliberately, exactly as
    `app.engines.scenario.ScenarioImpact` does it: the screen must be able to label
    this panel without inferring anything. No number in here is the coverage verdict.
    """

    batch_id: str
    row_id: str
    row_number: int
    conflict_kind: str
    conflict_field: str
    conflict_current_value: str
    conflict_file_value: str
    conflict_detail: str

    customer_id: str
    customer_name: str
    well_id: str | None = None
    well_name: str | None = None

    #: Demand lines that would receive a revision if this row were applied. For a
    #: well-status conflict this is EVERY line of the well, not only the line the
    #: spreadsheet row names.
    revised_line_ids: tuple[str, ...] = ()
    cascade_line_count: int = 0

    line_changes: tuple[PreviewLineChange, ...] = ()
    well_changes: tuple[PreviewWellChange, ...] = ()

    covered_lines_before: int = 0
    covered_lines_after: int = 0
    unrecoverable_lines_before: int = 0
    unrecoverable_lines_after: int = 0
    changed_line_count: int = 0
    changed_well_count: int = 0

    is_what_if: bool = True
    notes: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Write tripwire (layer 4)
# --------------------------------------------------------------------------


def _pending_snapshot(db: Session) -> tuple[int, int, int]:
    return (len(db.new), len(db.dirty), len(db.deleted))


def _assert_no_writes(db: Session, before: tuple[int, int, int]) -> None:
    """Fail loudly if the preview left anything pending in the session.

    Layer 4 of the no-write guarantee. If a future edit introduces a write here this
    raises during the preview, rather than letting a what-if quietly become real on
    the request's commit -- which, in this flow, would mean an upload changing demand
    without anybody deciding anything, the single thing the import flow exists to
    prevent.
    """
    after = _pending_snapshot(db)
    if after != before:
        raise AssertionError(
            "demand-import conflict preview must not modify the session: pending "
            f"(new, dirty, deleted) went from {before} to {after}. This is a "
            "read-only projection -- demand is written only by "
            "app.engines.coverage.apply_revision / set_well_demand_status, reached "
            "from app.engines.demand_import.apply_batch after the user has both "
            "accepted the row and approved the override."
        )


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def preview_row_conflict(
    db: Session, row: DemandImportRow
) -> ImportConflictPreview:
    """Coverage impact of approving and applying `row`. Writes nothing.

    Raises `ConflictPreviewUnavailable` when the row has no conflict.
    """
    conflict = detect_conflict(db, row)
    if conflict is None:
        raise ConflictPreviewUnavailable(
            f"Row {row.row_number} of this batch does not conflict with the live "
            "demand book, so there is no override to preview and none to approve. "
            "Accept or skip it in the ordinary way."
        )

    before_pending = _pending_snapshot(db)

    with db.no_autoflush:
        well = db.get(Well, row.well_id) if row.well_id else None
        if well is None:
            raise ConflictPreviewUnavailable(
                f"Row {row.row_number} resolved no well, so there is no customer "
                "pool to compute a coverage impact against."
            )
        customer = well.planning_node.customer

        overrides = _overrides_for(db, row, conflict, customer)
        resolver = ScenarioOverrides(overrides, customer)

        base = compute_customer_coverage(db, customer, None, None, NO_OVERRIDES)
        after = compute_customer_coverage(db, customer, None, None, resolver)

        well_names = {
            w.id: w.name
            for w in db.query(Well)
            .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .filter(PlanningNode.customer_id == customer.id)
        }
        revised = _revised_line_ids(db, row, conflict)
        # DISTINCT from `revised`, deliberately. `revised` is every line applying
        # would write to -- which for a status conflict is the whole well. `named` is
        # only the line the SPREADSHEET ROW itself points at. The difference between
        # the two sets is exactly what the reviewer is not being told by the row's own
        # diff, so collapsing them would delete the panel's whole reason to exist.
        named = (
            frozenset({row.matched_demand_line_id})
            if _would_revise(row) and row.matched_demand_line_id
            else frozenset()
        )
        line_changes = _line_changes(base, after, well_names, named)
        well_changes = _well_changes(base, after, well_names)

        impact = ImportConflictPreview(
            batch_id=row.batch_id,
            row_id=row.id,
            row_number=row.row_number,
            conflict_kind=conflict.kind,
            conflict_field=conflict.field,
            conflict_current_value=conflict.current_value,
            conflict_file_value=conflict.file_value,
            conflict_detail=conflict.detail,
            customer_id=customer.id,
            customer_name=customer.name,
            well_id=well.id,
            well_name=well.name,
            revised_line_ids=revised,
            cascade_line_count=len(revised),
            line_changes=line_changes,
            well_changes=well_changes,
            covered_lines_before=_covered(base),
            covered_lines_after=_covered(after),
            unrecoverable_lines_before=_unrecoverable(base),
            unrecoverable_lines_after=_unrecoverable(after),
            changed_line_count=sum(1 for c in line_changes if c.changed),
            changed_well_count=sum(1 for c in well_changes if c.changed),
            notes=_notes(row, conflict, customer, revised),
        )

    _assert_no_writes(db, before_pending)
    return impact


# --------------------------------------------------------------------------
# Translating a conflicting row into overrides
# --------------------------------------------------------------------------


def _overrides_for(
    db: Session, row: DemandImportRow, conflict: RowConflict, customer
) -> list[ScenarioOverride]:
    """TRANSIENT `ScenarioOverride` rows describing what applying `row` would do.

    Never `db.add`ed -- see the module docstring's layer 4. They exist only to be
    read by `ScenarioOverrides`, which is the one indirection the coverage engine
    consults, so the preview is computed by the real coverage rules rather than by a
    second reading of them.

    BOTH halves of the row are modelled, not only the conflicting half. A row can
    assert a new well status AND revise a quantity, and applying it does both; a
    preview showing only the status change would understate the consequence of the
    very approval it is asking for.
    """
    overrides: list[ScenarioOverride] = []

    # The WELL's demand status, when the row asserts one the well does not have.
    # Modelled as a WELL override for exactly the reason that kind exists: status is
    # a well-level fact, so every line of the well must be seen at the new status by
    # the one coverage implementation.
    if row.raw_status is not None and row.status is not None and row.well_id:
        well = db.get(Well, row.well_id)
        if well is not None and well.demand_status != row.status:
            overrides.append(
                ScenarioOverride(
                    target_kind=ScenarioTargetKind.WELL,
                    target_well_id=row.well_id,
                    field_name="demand_status",
                    value_text=row.status.value,
                )
            )

    # The line's own quantity / ROS / profile, when this row would revise a line.
    # `apply_batch` writes these through `apply_revision`; the resolver reads them
    # through `LineView`, so the two see the same three values.
    if _would_revise(row) and row.matched_demand_line_id:
        if row.quantity is not None:
            overrides.append(
                ScenarioOverride(
                    target_kind=ScenarioTargetKind.DEMAND_LINE,
                    target_demand_line_id=row.matched_demand_line_id,
                    field_name="quantity",
                    value_number=float(row.quantity),
                )
            )
        if row.ros_date is not None:
            overrides.append(
                ScenarioOverride(
                    target_kind=ScenarioTargetKind.DEMAND_LINE,
                    target_demand_line_id=row.matched_demand_line_id,
                    field_name="ros_date",
                    value_date=row.ros_date,
                )
            )
        if row.profile is not None:
            overrides.append(
                ScenarioOverride(
                    target_kind=ScenarioTargetKind.DEMAND_LINE,
                    target_demand_line_id=row.matched_demand_line_id,
                    field_name="profile",
                    value_text=row.profile.value,
                )
            )

    # A row accepted as NEW demand creates a line, and there is deliberately no
    # override kind that can express that (see the module docstring). Such a row is
    # also never a CONCURRENT_REVISION conflict, so the only way to be here is a
    # well-status conflict on a new row -- whose status half IS modelled above. The
    # new line's own contribution to the pool is not, and `_notes` says so out loud
    # rather than letting the panel read as complete.
    del conflict, customer
    return overrides


def _would_revise(row: DemandImportRow) -> bool:
    from app.models import DemandImportDecision, DemandImportMatchType

    return row.decision == DemandImportDecision.ACCEPT_REVISION or (
        row.decision != DemandImportDecision.ACCEPT_NEW
        and row.match_type == DemandImportMatchType.REVISION
    )


def _revised_line_ids(
    db: Session, row: DemandImportRow, conflict: RowConflict
) -> tuple[str, ...]:
    """Every demand line applying `row` would write a revision to.

    For a well-status conflict that is EVERY line of the well -- the number the
    reviewer is really being asked to authorise -- not just the one the spreadsheet
    row names.
    """
    from app.models import DemandLine

    ids: set[str] = set()
    if conflict.kind == CONFLICT_WELL_DEMAND_STATUS and row.well_id:
        ids.update(
            line_id
            for (line_id,) in db.query(DemandLine.id).filter(
                DemandLine.well_id == row.well_id
            )
        )
    if _would_revise(row) and row.matched_demand_line_id:
        ids.add(row.matched_demand_line_id)
    return tuple(sorted(ids))


# --------------------------------------------------------------------------
# Diffing -- the same shape app.engines.scenario produces
# --------------------------------------------------------------------------


def _line_changes(
    base: CustomerCoverage,
    after: CustomerCoverage,
    well_names: dict[str, str],
    named: frozenset[str],
) -> tuple[PreviewLineChange, ...]:
    """The union of both passes' line sets.

    Union, not the base's: a status assertion that confirms a well brings all of its
    lines INTO scope that the base pass never evaluated (and demoting one takes them
    out). Both directions are real coverage impacts and neither may be dropped
    because one side lacks a row -- "NotEvaluated" is a distinct outcome, not a
    missing value.
    """
    products = {
        view.id: view.product
        for view in (*base.included_views, *after.included_views)
    }
    out: list[PreviewLineChange] = []
    for line_id in sorted(set(base.by_line) | set(after.by_line)):
        b = base.by_line.get(line_id)
        a = after.by_line.get(line_id)
        product = products.get(line_id)
        ref = a or b
        before_status = b.status.value if b else "NotEvaluated"
        after_status = a.status.value if a else "NotEvaluated"
        out.append(
            PreviewLineChange(
                demand_line_id=line_id,
                well_id=ref.well_id,
                well_name=well_names.get(ref.well_id, ref.well_id),
                product_id=ref.product_id,
                product_description=product.description if product else None,
                unit_of_measure=product.unit_of_measure if product else None,
                coverage_before=before_status,
                coverage_after=after_status,
                reason_before=b.reason if b else None,
                reason_after=a.reason if a else None,
                quantity_before=b.quantity if b else (a.quantity if a else 0.0),
                quantity_after=a.quantity if a else (b.quantity if b else 0.0),
                ros_date_before=b.ros_date if b else a.ros_date,
                ros_date_after=a.ros_date if a else b.ros_date,
                changed=before_status != after_status,
                named_by_the_row=line_id in named,
            )
        )
    return tuple(out)


def _well_changes(
    base: CustomerCoverage, after: CustomerCoverage, well_names: dict[str, str]
) -> tuple[PreviewWellChange, ...]:
    out: list[PreviewWellChange] = []
    for well_id in sorted(set(base.well_status) | set(after.well_status)):
        b = base.well_status.get(well_id)
        a = after.well_status.get(well_id)
        out.append(
            PreviewWellChange(
                well_id=well_id,
                well_name=well_names.get(well_id, well_id),
                coverage_before=b,
                coverage_after=a,
                changed=b != a,
            )
        )
    return tuple(out)


def _covered(computed: CustomerCoverage) -> int:
    return sum(1 for v in computed.by_line.values() if v.status in _RESOLVED)


def _unrecoverable(computed: CustomerCoverage) -> int:
    return sum(
        1
        for v in computed.by_line.values()
        if v.status == CoverageStatus.UNRECOVERABLE
    )


def _notes(
    row: DemandImportRow, conflict: RowConflict, customer, revised: tuple[str, ...]
) -> tuple[str, ...]:
    from app.models import DemandImportDecision

    notes = [
        "WHAT-IF ONLY. Nothing in this result has been persisted, and asking for it "
        "has not approved anything. The official coverage verdict is unchanged; it "
        "moves only when this batch is applied, which requires BOTH accepting the "
        "row and approving this override.",
        "Computed by the platform's one coverage implementation "
        "(app.engines.coverage.compute_customer_coverage), run twice -- once as the "
        "data is, once with this row's values resolved in through the same override "
        "layer a scenario preview uses. It cannot disagree with the official "
        "verdict, because it is the same function.",
        f"Every quantity above was resolved inside customer {customer.name!r}'s own "
        "pool. An import row cannot reach across a customer or Business Unit "
        "boundary.",
    ]

    if conflict.kind == CONFLICT_WELL_DEMAND_STATUS:
        notes.append(
            f"Approving this override moves well {row.raw_well!r} from "
            f"{conflict.current_value} to {conflict.file_value} and writes a revision "
            f"to all {len(revised)} demand line(s) of it, including any this "
            "spreadsheet never listed. Lines below marked as NOT named by the row are "
            "the ones you would be changing without having asked to."
        )
    if conflict.kind == CONFLICT_CONCURRENT_REVISION:
        notes.append(
            "The 'before' column is the line as it is NOW, not as it was when this "
            "file was staged -- which is the whole point: the diff on the review "
            "screen was computed against a value somebody else has since replaced. "
            "If their change was the right one, skip this row instead of approving it."
        )
    if row.decision == DemandImportDecision.ACCEPT_NEW:
        notes.append(
            "This row is accepted as NEW demand, and the new line's own draw on the "
            "pool is NOT modelled above: the override vocabulary can restate an "
            "existing line but cannot create one, and inventing a synthetic line here "
            "would be a second implementation of what applying does. The figures "
            "above are therefore the impact of the STATUS half of this row only. "
            "Treat the covered counts as an upper bound."
        )
    return tuple(notes)


__all__ = [
    "ConflictPreviewUnavailable",
    "ImportConflictPreview",
    "PreviewLineChange",
    "PreviewWellChange",
    "preview_row_conflict",
]
