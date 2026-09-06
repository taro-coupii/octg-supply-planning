"""Excel demand import endpoints.

    POST   /demand-imports                        upload + stage + match
    GET    /demand-imports                        list batches
    GET    /demand-imports/template               download the CURRENT book as .xlsx
    GET    /demand-imports/{id}                   the batch, for review
    PATCH  /demand-imports/{id}/rows/{row_id}     the user's decision
    GET    .../rows/{row_id}/conflict-preview     coverage impact of one conflict
    POST   .../rows/{row_id}/override-approval    approve overriding a conflict
    POST   /demand-imports/{id}/apply             apply accepted rows

Upload stages and suggests; it never changes demand. Apply acts only on rows the
user decided -- and, for a row that conflicts with the live book, only after a
SECOND explicit decision approving the override. See `app.engines.demand_import`
for the column contract, the matching rules and what does and does not count as a
conflict, and `app.engines.demand_import_preview` for the read-only impact panel.

ROUTE ORDER MATTERS HERE
------------------------
`/template` is declared BEFORE `/{batch_id}`. FastAPI matches in declaration order,
so the reverse order would make every template request a lookup for a batch whose id
is the string "template" -- a 404 that looks like a missing batch.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.engines.demand_import import (
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    DemandImportConflictUnapproved,
    DemandImportError,
    apply_batch,
    build_template,
    detect_conflict,
    parse_and_stage,
)
from app.engines.demand_import_preview import (
    ConflictPreviewUnavailable,
    preview_row_conflict,
)
from app.auth.scope import bus_of_import_batch, planner_bu
from app.models import (
    Customer,
    DemandImportBatch,
    DemandImportBatchStatus,
    DemandImportDecision,
    DemandImportMatchType,
    DemandImportRow,
    PlanningNode,
    Well,
)
from app.schemas import (
    DemandImportApplyOut,
    DemandImportBatchOut,
    DemandImportBatchSummaryOut,
    DemandImportDecisionIn,
    DemandImportOverrideApprovalIn,
    DemandImportRowOut,
    ImportConflictPreviewOut,
)

router = APIRouter(prefix="/demand-imports", tags=["demand-import"])

XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

# 20 MB. A demand sheet is a few thousand rows of text; anything larger is a
# mistake or an attack, and refusing it up front is cheaper than parsing it.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _row_out(db: Session, row: DemandImportRow) -> DemandImportRowOut:
    """One staged row, with its conflict verdict computed FRESH.

    `db` is taken so `detect_conflict` can compare the row against live data on
    every read. That is not a convenience: a conflict is a statement about the
    relationship between this row and a book that keeps moving, so serving a stored
    flag would show the reviewer an answer that was true when the file was uploaded.
    Nothing here writes -- `detect_conflict` is a pure read.
    """
    matched = row.matched_demand_line
    conflict = detect_conflict(db, row)
    return DemandImportRowOut(
        id=row.id,
        row_number=row.row_number,
        raw_well=row.raw_well,
        raw_product=row.raw_product,
        raw_quantity=row.raw_quantity,
        raw_ros_date=row.raw_ros_date,
        raw_status=row.raw_status,
        raw_profile=row.raw_profile,
        well_id=row.well_id,
        well_name=row.well.name if row.well else None,
        product_id=row.product_id,
        product_description=(
            (row.product.description or row.product.id) if row.product else None
        ),
        quantity=row.quantity,
        # The MATCHED product's unit, so the review screen labels both `quantity`
        # and `matched_quantity` while a planner decides. None exactly when no
        # product matched -- an unmatched or error row has no product and therefore
        # no unit, and guessing one would put a confident label on a cell that has
        # not been validated at all. Such a row shows `raw_quantity` instead.
        unit_of_measure=row.product.unit_of_measure if row.product else None,
        ros_date=row.ros_date,
        status=row.status,
        profile=row.profile,
        match_type=row.match_type,
        matched_demand_line_id=row.matched_demand_line_id,
        matched_quantity=matched.quantity if matched else None,
        matched_ros_date=matched.ros_date if matched else None,
        # The matched line's WELL's demand status -- there is no line-level status.
        # Shown so the review screen can say what the well is today beside what the
        # spreadsheet asserts it should be.
        matched_status=matched.well.demand_status if matched else None,
        matched_profile=matched.profile if matched else None,
        match_reason=row.match_reason,
        error=row.error,
        decision=row.decision,
        is_conflict=conflict is not None,
        conflict_kind=conflict.kind if conflict else None,
        conflict_field=conflict.field if conflict else None,
        conflict_current_value=conflict.current_value if conflict else None,
        conflict_file_value=conflict.file_value if conflict else None,
        conflict_detail=conflict.detail if conflict else None,
        conflict_cascade_line_count=conflict.cascade_line_count if conflict else 0,
        requires_override_approval=(
            conflict is not None and not row.override_approved
        ),
        override_approved=row.override_approved,
        override_approved_at=row.override_approved_at,
        override_approved_by=row.override_approved_by,
        baseline_revision_no=row.baseline_revision_no,
        baseline_quantity=row.baseline_quantity,
        baseline_ros_date=row.baseline_ros_date,
        current_revision_no=matched.current_revision_no if matched else None,
        applied=row.applied,
        applied_demand_line_id=row.applied_demand_line_id,
        apply_error=row.apply_error,
    )


def _batch_out(db: Session, batch: DemandImportBatch) -> DemandImportBatchOut:
    rows = list(batch.rows)
    row_outs = [_row_out(db, r) for r in rows]
    return DemandImportBatchOut(
        id=batch.id,
        filename=batch.filename,
        sheet_name=batch.sheet_name,
        status=batch.status,
        row_count=batch.row_count,
        error_count=batch.error_count,
        pending_count=sum(
            1
            for r in rows
            if r.decision == DemandImportDecision.PENDING
            and r.match_type != DemandImportMatchType.ERROR
        ),
        revision_suggestion_count=sum(
            1 for r in rows if r.match_type == DemandImportMatchType.REVISION
        ),
        new_suggestion_count=sum(
            1 for r in rows if r.match_type == DemandImportMatchType.NEW
        ),
        conflict_count=sum(1 for r in row_outs if r.is_conflict),
        # Only ACCEPTED rows are counted as blocking: a conflicting row left Pending
        # or Skipped blocks nothing, because apply was never going to touch it. This
        # is the number the Apply button gates on, so counting the others would
        # disable Apply over rows nobody is trying to write.
        unapproved_conflict_count=sum(
            1
            for r, out in zip(rows, row_outs)
            if out.requires_override_approval
            and r.decision
            in (
                DemandImportDecision.ACCEPT_REVISION,
                DemandImportDecision.ACCEPT_NEW,
            )
        ),
        created_at=batch.created_at,
        applied_at=batch.applied_at,
        column_contract={
            "required": list(REQUIRED_COLUMNS),
            "optional": list(OPTIONAL_COLUMNS),
        },
        rows=row_outs,
    )


def _load(db: Session, batch_id: str) -> DemandImportBatch:
    batch = (
        db.query(DemandImportBatch)
        .options(
            joinedload(DemandImportBatch.rows).joinedload(DemandImportRow.well),
            joinedload(DemandImportBatch.rows).joinedload(DemandImportRow.product),
            joinedload(DemandImportBatch.rows).joinedload(
                DemandImportRow.matched_demand_line
            ),
        )
        .filter(DemandImportBatch.id == batch_id)
        .one_or_none()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found")
    return batch


@router.post("", response_model=DemandImportBatchOut, status_code=201)
async def create_demand_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    bu_scope: str | None = Depends(planner_bu),
):
    """Upload an .xlsx demand sheet. Stages and matches; changes NO demand.

    File-level problems (not a workbook, empty first sheet, a missing required
    column) are 400s with an explanation, because there is no per-row answer to
    give. Row-level problems -- unknown well or product, bad quantity,
    unparseable date, in-file duplicates -- are staged as rows with
    `match_type = "Error"` and an `error` message, and never stop the rest of the
    batch from being staged.
    """
    name = (file.filename or "").strip()
    if name and not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{name!r} is not an .xlsx file. Save the sheet as Excel Workbook "
                "(.xlsx) and retry -- .xls and .csv are not supported."
            ),
        )
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File is {len(payload)} bytes; the limit is {MAX_UPLOAD_BYTES}"
            ),
        )

    try:
        batch = parse_and_stage(db, payload, filename=name or None)
    except DemandImportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    if bu_scope is not None:
        # Owner ruling 2026-09-06 (F01 batches): one row outside the planner's
        # Business Unit refuses the WHOLE upload, and nothing is staged. A
        # partially staged batch would look finished while silently missing
        # rows, which is the state the ruling exists to prevent.
        db.flush()
        foreign = bus_of_import_batch(db, batch.id) - {bu_scope}
        if foreign:
            db.rollback()
            raise HTTPException(
                status_code=403,
                detail=(
                    "This workbook names wells in another Business Unit, so none "
                    "of it was staged. Your account is confined to one Business "
                    "Unit; remove the foreign rows and upload again."
                ),
            )
    db.commit()
    return _batch_out(db, _load(db, batch.id))


@router.get("", response_model=list[DemandImportBatchSummaryOut])
def list_demand_imports(
    db: Session = Depends(get_db), bu_scope: str | None = Depends(planner_bu)
):
    batches = (
        db.query(DemandImportBatch)
        .order_by(DemandImportBatch.created_at.desc())
        .all()
    )
    if bu_scope is not None:
        # A batch belongs to the Business Units its rows' wells belong to. A
        # planner sees batches that touch only their own BU (or none yet).
        batches = [
            b for b in batches
            if not (bus_of_import_batch(db, b.id) - {bu_scope})
        ]
    return [
        DemandImportBatchSummaryOut(
            id=b.id,
            filename=b.filename,
            status=b.status,
            row_count=b.row_count,
            error_count=b.error_count,
            created_at=b.created_at,
            applied_at=b.applied_at,
        )
        for b in batches
    ]


@router.get("/template")
def download_demand_template(
    customer_id: str = Query(
        ...,
        description=(
            "The customer whose CURRENT demand book to export. Required -- see "
            "app.engines.demand_import.build_template for why there is no "
            "all-customers export."
        ),
    ),
    well_id: str | None = Query(
        None, description="Optional: narrow the export to one well of that customer."
    ),
    db: Session = Depends(get_db),
):
    """Download the customer's CURRENT demand as an .xlsx in the import contract.

    NOT a blank template. Every row is a demand line that exists right now, so a
    planner edits real values instead of retyping the book beside a column hint --
    and re-uploading the file UNMODIFIED stages every row as an exact-match revision
    with no errors and no conflicts, which is the property that makes it a safe
    starting point. `tests/test_demand_import_template.py` pins that round trip.

    A GET, and it writes nothing: generating a file is a read of the demand book.

    A customer with no demand at all yields a header-only workbook rather than a 404.
    The scope exists, and "this customer's book is empty" is a true answer that a 404
    ("no such thing") would misreport -- the same distinction the rest of this
    platform draws between an absent row and a zero. The Notes sheet says so.
    """
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(
            status_code=404, detail=f"Customer {customer_id!r} not found"
        )

    well = None
    if well_id is not None:
        well = db.get(Well, well_id)
        if well is None:
            raise HTTPException(
                status_code=404, detail=f"Well {well_id!r} not found"
            )
        owner = (
            db.query(PlanningNode.customer_id)
            .filter(PlanningNode.id == well.planning_node_id)
            .scalar()
        )
        if owner != customer_id:
            # Refused rather than silently widened to the whole customer or narrowed
            # to nothing. Either fallback would hand the planner a file whose scope
            # is not the scope they asked for, and the file's whole value is that its
            # contents are exactly the book they are about to edit.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Well {well.name!r} does not belong to customer "
                    f"{customer.name!r}. A template is scoped to one customer's own "
                    "demand, because that is the scope coverage is computed at."
                ),
            )

    template = build_template(db, customer, well=well)
    return Response(
        content=template.content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{template.filename}"',
            # So a client can say "12 demand lines" without opening the workbook, and
            # so the round-trip test can assert the export was not empty by accident.
            # Exposed through CORS explicitly: a browser cannot read a response header
            # that is not listed there, and the frontend runs on another origin.
            "X-Demand-Row-Count": str(template.row_count),
            "Access-Control-Expose-Headers": (
                "Content-Disposition, X-Demand-Row-Count"
            ),
        },
    )


@router.get("/{batch_id}", response_model=DemandImportBatchOut)
def get_demand_import(batch_id: str, db: Session = Depends(get_db)):
    """The staged batch for the Review step, with each row's suggestion, the
    matched line's current values to diff against, and any per-row error."""
    return _batch_out(db, _load(db, batch_id))


@router.patch(
    "/{batch_id}/rows/{row_id}", response_model=DemandImportRowOut
)
def set_row_decision(
    batch_id: str,
    row_id: str,
    body: DemandImportDecisionIn,
    db: Session = Depends(get_db),
):
    """Record the USER's decision for one staged row.

    Rejected outright:
      * accepting a row that failed validation -- there is nothing valid to apply;
      * accepting a row as a revision when no existing demand line was matched.
    Both are 400s rather than silent downgrades, because silently applying
    something other than what the user chose is the one thing this flow must not
    do. `Skip` is always allowed.
    """
    batch = _load(db, batch_id)
    if batch.status == DemandImportBatchStatus.APPLIED:
        raise HTTPException(
            status_code=409,
            detail="Batch has already been applied; decisions can no longer change",
        )
    row = next((r for r in batch.rows if r.id == row_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Import row not found")

    decision = body.decision
    if decision != DemandImportDecision.SKIP:
        if row.match_type == DemandImportMatchType.ERROR:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Row {row.row_number} failed validation ({row.error}). It can "
                    "only be skipped; fix the spreadsheet and re-upload."
                ),
            )
        if (
            decision == DemandImportDecision.ACCEPT_REVISION
            and row.matched_demand_line_id is None
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Row {row.row_number} matched no existing demand line, so it "
                    "cannot be accepted as a revision. Accept it as new demand or "
                    "skip it."
                ),
            )

    row.decision = decision
    db.commit()
    db.refresh(row)
    return _row_out(db, row)


def _row_of(db: Session, batch_id: str, row_id: str):
    batch = _load(db, batch_id)
    row = next((r for r in batch.rows if r.id == row_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Import row not found")
    return batch, row


@router.get(
    "/{batch_id}/rows/{row_id}/conflict-preview",
    response_model=ImportConflictPreviewOut,
)
def get_row_conflict_preview(
    batch_id: str, row_id: str, db: Session = Depends(get_db)
):
    """Coverage impact of approving and applying ONE conflicting row. READ-ONLY.

    Requesting this approves nothing and persists nothing -- `is_what_if` is True in
    every response and the engine asserts the session was not modified. It exists so
    the planner being asked to authorise an override can see what the override
    actually does, which for a well-status conflict is strictly more than the row's
    own diff shows.

    404 when the row does not exist. 409 when the row does not conflict: there is no
    override to preview and none to approve, and answering with zeros would let the
    screen render "approving this changes nothing" about a row that needs no approval.
    """
    _batch, row = _row_of(db, batch_id, row_id)
    try:
        impact = preview_row_conflict(db, row)
    except ConflictPreviewUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return ImportConflictPreviewOut(
        **{
            **impact.__dict__,
            "revised_line_ids": list(impact.revised_line_ids),
            "line_changes": [c.__dict__ for c in impact.line_changes],
            "well_changes": [c.__dict__ for c in impact.well_changes],
            "notes": list(impact.notes),
        }
    )


@router.post(
    "/{batch_id}/rows/{row_id}/override-approval",
    response_model=DemandImportRowOut,
)
def set_row_override_approval(
    batch_id: str,
    row_id: str,
    body: DemandImportOverrideApprovalIn,
    db: Session = Depends(get_db),
):
    """Approve -- or withdraw approval for -- overriding one row's conflict.

    A SECOND decision, distinct from accept/skip. Accept/skip says "this is the
    change I want"; this says "I have seen that it disagrees with the live book and I
    approve overriding that". `apply_batch` refuses a conflicting, unapproved row and
    applies NOTHING in the batch until it is resolved.

    Refused with 400 when the row does not conflict. An approval that authorises
    nothing is not harmless: it would sit on the row looking like a considered
    decision, and every row could carry one, which would turn the flag from a signal
    into decoration. Withdrawal (`approved: false`) is always allowed -- a planner
    may change their mind, and refusing to let them un-approve would be worse than
    refusing to let them approve.

    409 once the batch is applied, matching `set_row_decision`: an applied batch is a
    record of what happened.
    """
    batch, row = _row_of(db, batch_id, row_id)
    if batch.status == DemandImportBatchStatus.APPLIED:
        raise HTTPException(
            status_code=409,
            detail=(
                "Batch has already been applied; override approvals can no longer "
                "change"
            ),
        )

    if body.approved and detect_conflict(db, row) is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Row {row.row_number} does not conflict with the live demand book, "
                "so there is no override to approve. Accept or skip it in the "
                "ordinary way."
            ),
        )

    row.override_approved = body.approved
    if body.approved:
        row.override_approved_at = datetime.utcnow()
        row.override_approved_by = (body.approved_by or "").strip() or None
    else:
        # Cleared, not kept. A withdrawn approval that retained its timestamp and
        # attributor would read as an approval that had happened, which is the one
        # thing this row must not claim.
        row.override_approved_at = None
        row.override_approved_by = None
    db.commit()
    db.refresh(row)
    return _row_out(db, row)


@router.post("/{batch_id}/apply", response_model=DemandImportApplyOut)
def apply_demand_import(batch_id: str, db: Session = Depends(get_db)):
    """Apply the accepted rows.

    Every demand write goes through `app.engines.coverage.apply_revision`, so each
    applied row produces a `DemandRevision` and an `ImpactRecord` (the Home
    Dashboard's "Demand Changes" card) and triggers a coverage recompute.
    `DemandLine` is never mutated directly.

    Rows still PENDING are left alone and counted -- the user has not decided, and
    deciding for them is exactly what this flow exists to prevent. Skipped rows
    change nothing.

    409 with `blocked_row_ids` when an ACCEPTED row conflicts with the live book and
    its override is not approved. NOTHING is applied in that case -- the refusal is
    all-or-nothing, because a batch cannot be applied twice, so a partial apply would
    leave the conflicting rows permanently unappliable. See
    `app.engines.demand_import.DemandImportConflictUnapproved`.
    """
    batch = _load(db, batch_id)
    try:
        result = apply_batch(db, batch)
    except DemandImportConflictUnapproved as exc:
        db.rollback()
        # A structured body as well as the prose, so a client can highlight the
        # offending rows rather than parse a message. `error` is a stable
        # machine-readable discriminator, following app.main's handlers.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "demand_import_conflicts_unapproved",
                "detail": str(exc),
                "blocked_row_ids": [row_id for row_id, _n, _c in exc.blocked],
                "blocked_rows": [
                    {
                        "row_id": row_id,
                        "row_number": number,
                        "conflict_kind": conflict.kind,
                        "conflict_detail": conflict.detail,
                    }
                    for row_id, number, conflict in exc.blocked
                ],
            },
        )
    except DemandImportError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    db.commit()

    batch = _load(db, batch_id)
    return DemandImportApplyOut(
        batch_id=result.batch_id,
        status=batch.status,
        revised_count=result.revised_count,
        created_count=result.created_count,
        skipped_count=result.skipped_count,
        pending_count=result.pending_count,
        error_count=result.error_count,
        failed_row_ids=list(result.failed_row_ids),
        impact_record_ids=list(result.impact_record_ids),
        well_status_changed_ids=list(result.well_status_changed_ids),
        well_status_cascaded_line_count=result.well_status_cascaded_line_count,
        batch=_batch_out(db, batch),
    )
