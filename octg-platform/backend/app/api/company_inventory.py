"""Company-owned inventory MAINTENANCE endpoints.

MVP-COMPROMISE[C-03]: every write endpoint in this router restates a figure design
principle #5 calls a read-only Oracle projection. There is no Oracle interface in
the MVP, so a wrong seeded row had no correction path before this router existed.
See `app.engines.company_inventory`'s module docstring and MVP_COMPROMISES.md C-03
for the full reasoning, the gate that gets removed automatically once a real
Oracle feed lands, and the decision that is still open for that day (keep this as
an audited emergency path, or delete it).

    GET    /company-inventory                              the position for a BU
    GET    /company-inventory/{business_unit_id}/template   xlsx of the current
                                                            on-hand position
    POST   /company-inventory/{business_unit_id}/uploads    xlsx upload, replaces
                                                            on-hand for the BU
    GET    /company-inventory/{business_unit_id}/uploads    upload history
    PATCH  /company-inventory/on-hand/{row_id}
    POST   /company-inventory/on-hand
    DELETE /company-inventory/on-hand/{row_id}
    PATCH  /company-inventory/on-order/{row_id}
    POST   /company-inventory/on-order
    DELETE /company-inventory/on-order/{row_id}
    PATCH  /company-inventory/assignments/{row_id}
    POST   /company-inventory/assignments
    DELETE /company-inventory/assignments/{row_id}

THE GATE, AT THE API LAYER
---------------------------
`app.engines.company_inventory.NotMaintainable` is caught here, once, and turned
into 403 with a detail naming the row, its current `source_system`, and the fact
that the fix belongs upstream. Every write function in the engine raises the same
exception for the same reason, so this is the only place the status code is
decided -- exactly the division of labour `app.engines.inventory`'s
`InventoryScopeMissing` / `InventoryRowMissing` already have with `app.main`.

TEMPLATE / UPLOAD IS ON-HAND ONLY
----------------------------------
See `app.engines.company_inventory` for why: on-hand is single-valued per (BU,
product), exactly like customer-owned inventory, so "replace the position named in
this file" has one honest meaning. On-order (several rows per product are normal)
and assignments (one row per demand line) are edited only through the inline
single-row endpoints below.
"""

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db import get_db
from app.engines.company_inventory import (
    REQUIRED_COLUMNS,
    CompanyInventoryImportError,
    NotMaintainable,
    build_on_hand_template,
    create_assignment,
    create_on_hand,
    create_on_order,
    delete_assignment,
    delete_on_hand,
    delete_on_order,
    get_position,
    parse_and_replace_on_hand,
    set_assignment,
    set_on_hand,
    set_on_order,
)
from app.models import BusinessUnit, CompanyInventoryUpload, Product, User
from app.schemas import (
    CompanyAssignmentCreateIn,
    CompanyAssignmentEditIn,
    CompanyInventoryPositionOut,
    CompanyInventoryUploadOut,
    CompanyInventoryUploadRowOut,
    CompanyInventoryUploadSummaryOut,
    CompanyInventoryWriteOut,
    CompanyOnHandCreateIn,
    CompanyOnHandEditIn,
    CompanyOnOrderCreateIn,
    CompanyOnOrderEditIn,
    RecomputeFailureOut,
)

router = APIRouter(prefix="/company-inventory", tags=["company-inventory"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Same limit as the customer-owned upload -- an on-hand sheet is a few thousand
# rows of text; anything larger is a mistake or an attack.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

COLUMN_CONTRACT = {"required": list(REQUIRED_COLUMNS), "optional": []}


def _business_unit(db: Session, business_unit_id: str) -> BusinessUnit:
    bu = db.get(BusinessUnit, business_unit_id)
    if bu is None:
        raise HTTPException(status_code=404, detail="Business Unit not found")
    return bu


def _write_out(result, row_kind: str) -> CompanyInventoryWriteOut:
    return CompanyInventoryWriteOut(
        row_id=result.row_id,
        before=result.before,
        after=result.after,
        recomputed_customer_ids=list(result.recompute.recomputed_customer_ids),
        coverage_changes={
            well_id: [before, after]
            for well_id, (before, after) in result.recompute.well_changes.items()
        },
        recompute_failures=[
            RecomputeFailureOut(
                customer_id=f.customer_id,
                customer_name=f.customer_name,
                reason=f.reason,
            )
            for f in result.recompute.failures
        ],
    )


def _handle_not_maintainable(exc: NotMaintainable) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


# ---------------------------------------------------------------------------
# READ
# ---------------------------------------------------------------------------


@router.get("", response_model=CompanyInventoryPositionOut)
def get_company_inventory(
    business_unit_id: str, product_id: str | None = None, db: Session = Depends(get_db)
):
    """The company-owned inventory position for one Business Unit.

    `product_id` narrows to one product (and always represents it, even with no
    data at all -- an explicit ask gets an explicit answer). Without it, every
    product touched by any of the three tables in this BU is returned; see
    `app.engines.company_inventory.get_position` for exactly which products that
    is and why the catalogue alone does not decide it.
    """
    _business_unit(db, business_unit_id)
    if product_id is not None and db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    position = get_position(db, business_unit_id, product_id)
    return CompanyInventoryPositionOut(
        business_unit_id=position.business_unit_id,
        business_unit_name=position.business_unit_name,
        on_hand=[row.__dict__ for row in position.on_hand],
        on_order=[row.__dict__ for row in position.on_order],
        assignments=[
            {
                "product_id": g.product_id,
                "product_description": g.product_description,
                "unit_of_measure": g.unit_of_measure,
                "total_quantity": g.total_quantity,
                "lines": [line.__dict__ for line in g.lines],
            }
            for g in position.assignments
        ],
    )


# ---------------------------------------------------------------------------
# TEMPLATE + UPLOAD (on-hand only)
# ---------------------------------------------------------------------------


@router.get("/{business_unit_id}/template")
def download_company_inventory_template(
    business_unit_id: str, db: Session = Depends(get_db)
):
    """Download the Business Unit's CURRENT on-hand position as an .xlsx.

    NOT a blank template -- see `app.engines.company_inventory.build_on_hand_template`.
    """
    bu = _business_unit(db, business_unit_id)
    template = build_on_hand_template(db, bu)
    return Response(
        content=template.content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{template.filename}"',
            "X-Company-Inventory-Row-Count": str(template.row_count),
            "Access-Control-Expose-Headers": (
                "Content-Disposition, X-Company-Inventory-Row-Count"
            ),
        },
    )


@router.post(
    "/{business_unit_id}/uploads",
    response_model=CompanyInventoryUploadOut,
    status_code=201,
)
async def upload_company_inventory(
    business_unit_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload an .xlsx of on-hand quantities and REPLACE the named products'
    position for this Business Unit.

    MVP-COMPROMISE[C-03]: bulk restatement of an Oracle-owned projection. See the
    module docstring.

    A row targeting a product whose EXISTING on-hand row is not
    platform-maintainable is reported as a per-row error (never aborts the batch),
    same as every other row-level refusal here.
    """
    bu = _business_unit(db, business_unit_id)

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
            detail=f"File is {len(payload)} bytes; the limit is {MAX_UPLOAD_BYTES}",
        )

    try:
        result = parse_and_replace_on_hand(db, bu, payload, filename=name or None)
    except CompanyInventoryImportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()

    product_ids = sorted({r.product_id for r in result.rows if r.product_id})
    units = (
        {p.id: p.unit_of_measure for p in db.query(Product).filter(Product.id.in_(product_ids))}
        if product_ids
        else {}
    )
    return CompanyInventoryUploadOut(
        upload_id=result.upload_id,
        business_unit_id=result.business_unit_id,
        filename=result.filename,
        sheet_name=result.sheet_name,
        row_count=result.row_count,
        created_count=result.created_count,
        replaced_count=result.replaced_count,
        applied_count=result.applied_count,
        error_count=result.error_count,
        rows=[
            CompanyInventoryUploadRowOut(
                row_number=row.row_number,
                action=row.action,
                raw_product=row.raw_product,
                raw_quantity=row.raw_quantity,
                product_id=row.product_id,
                product_description=row.product_description,
                quantity=row.quantity,
                previous_quantity=row.previous_quantity,
                unit_of_measure=units.get(row.product_id) if row.product_id else None,
                error=row.error,
            )
            for row in result.rows
        ],
        recomputed_customer_ids=list(result.recompute.recomputed_customer_ids),
        coverage_changes={
            well_id: [before, after]
            for well_id, (before, after) in result.recompute.well_changes.items()
        },
        recompute_failures=[
            RecomputeFailureOut(
                customer_id=f.customer_id, customer_name=f.customer_name, reason=f.reason
            )
            for f in result.recompute.failures
        ],
        column_contract=COLUMN_CONTRACT,
    )


@router.get(
    "/{business_unit_id}/uploads",
    response_model=list[CompanyInventoryUploadSummaryOut],
)
def list_company_inventory_uploads(business_unit_id: str, db: Session = Depends(get_db)):
    """Upload history, newest first."""
    _business_unit(db, business_unit_id)
    return (
        db.query(CompanyInventoryUpload)
        .filter(CompanyInventoryUpload.business_unit_id == business_unit_id)
        .order_by(CompanyInventoryUpload.uploaded_at.desc())
        .all()
    )


# ---------------------------------------------------------------------------
# INLINE ON-HAND
# ---------------------------------------------------------------------------


@router.patch("/on-hand/{row_id}", response_model=CompanyInventoryWriteOut)
def edit_on_hand(row_id: str, body: CompanyOnHandEditIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """MVP-COMPROMISE[C-03]: see the module docstring."""
    try:
        result = set_on_hand(db, row_id, body.quantity, actor_user_id=user.id)
    except NotMaintainable as exc:
        db.rollback()
        raise _handle_not_maintainable(exc)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return _write_out(result, "InventoryOnHand")


@router.post("/on-hand", response_model=CompanyInventoryWriteOut, status_code=201)
def add_on_hand(body: CompanyOnHandCreateIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """MVP-COMPROMISE[C-03]: see the module docstring."""
    _business_unit(db, body.business_unit_id)
    if db.get(Product, body.product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        result = create_on_hand(
            db, body.business_unit_id, body.product_id, body.quantity,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    db.commit()
    return _write_out(result, "InventoryOnHand")


@router.delete("/on-hand/{row_id}", response_model=CompanyInventoryWriteOut)
def remove_on_hand(row_id: str, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """MVP-COMPROMISE[C-03]: see the module docstring."""
    try:
        result = delete_on_hand(db, row_id, actor_user_id=user.id)
    except NotMaintainable as exc:
        db.rollback()
        raise _handle_not_maintainable(exc)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()
    return _write_out(result, "InventoryOnHand")


# ---------------------------------------------------------------------------
# INLINE ON-ORDER
# ---------------------------------------------------------------------------


@router.patch("/on-order/{row_id}", response_model=CompanyInventoryWriteOut)
def edit_on_order(row_id: str, body: CompanyOnOrderEditIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """MVP-COMPROMISE[C-03]: see the module docstring."""
    try:
        result = set_on_order(
            db, row_id, body.quantity, body.expected_arrival_date,
            actor_user_id=user.id,
        )
    except NotMaintainable as exc:
        db.rollback()
        raise _handle_not_maintainable(exc)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return _write_out(result, "InventoryOnOrder")


@router.post("/on-order", response_model=CompanyInventoryWriteOut, status_code=201)
def add_on_order(body: CompanyOnOrderCreateIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """MVP-COMPROMISE[C-03]: see the module docstring."""
    _business_unit(db, body.business_unit_id)
    if db.get(Product, body.product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        result = create_on_order(
            db,
            body.business_unit_id,
            body.product_id,
            body.quantity,
            body.expected_arrival_date,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return _write_out(result, "InventoryOnOrder")


@router.delete("/on-order/{row_id}", response_model=CompanyInventoryWriteOut)
def remove_on_order(row_id: str, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """MVP-COMPROMISE[C-03]: see the module docstring."""
    try:
        result = delete_on_order(db, row_id, actor_user_id=user.id)
    except NotMaintainable as exc:
        db.rollback()
        raise _handle_not_maintainable(exc)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()
    return _write_out(result, "InventoryOnOrder")


# ---------------------------------------------------------------------------
# INLINE ASSIGNMENTS
# ---------------------------------------------------------------------------


@router.patch("/assignments/{row_id}", response_model=CompanyInventoryWriteOut)
def edit_assignment(row_id: str, body: CompanyAssignmentEditIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """MVP-COMPROMISE[C-03]: see the module docstring."""
    try:
        result = set_assignment(db, row_id, body.quantity, actor_user_id=user.id)
    except NotMaintainable as exc:
        db.rollback()
        raise _handle_not_maintainable(exc)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return _write_out(result, "InventoryAssignment")


@router.post("/assignments", response_model=CompanyInventoryWriteOut, status_code=201)
def add_assignment(body: CompanyAssignmentCreateIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """MVP-COMPROMISE[C-03]: see the module docstring."""
    from app.models import DemandLine

    if db.get(DemandLine, body.demand_line_id) is None:
        raise HTTPException(status_code=404, detail="Demand line not found")
    if db.get(Product, body.product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        result = create_assignment(
            db, body.demand_line_id, body.product_id, body.quantity,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return _write_out(result, "InventoryAssignment")


@router.delete("/assignments/{row_id}", response_model=CompanyInventoryWriteOut)
def remove_assignment(row_id: str, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """MVP-COMPROMISE[C-03]: see the module docstring."""
    try:
        result = delete_assignment(db, row_id, actor_user_id=user.id)
    except NotMaintainable as exc:
        db.rollback()
        raise _handle_not_maintainable(exc)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()
    return _write_out(result, "InventoryAssignment")
