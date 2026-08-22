"""Customer-owned inventory endpoints.

    GET  /customer-owned-inventory/{customer_id}           the declared position
    GET  /customer-owned-inventory/{customer_id}/template  download it as an .xlsx
    POST /customer-owned-inventory/{customer_id}/uploads   upload an .xlsx, replace it
    GET  /customer-owned-inventory/{customer_id}/uploads   upload history

THESE ARE THE PLATFORM'S FIRST INVENTORY WRITE ENDPOINTS, AND THAT IS CORRECT
----------------------------------------------------------------------------
`InventoryOnHand`, `InventoryOnOrder` and `InventoryAssignment` deliberately expose
NO mutation endpoint anywhere in this API: they are read-only projections of
Oracle-owned data, and a platform that let a planner edit its own copy would be
claiming ownership of a fact it does not own.

Customer-owned inventory is the opposite case, not an exception to the rule. Oracle
holds only OUR OWN company's inventory; customer-owned material is absent from it
entirely, arrives by spreadsheet, and is therefore owned BY THIS PLATFORM. See
`app.models.customer_owned_inventory.CustomerOwnedInventory` for the full statement
of the boundary.

There is no separate Apply step. An inventory position has a current value rather
than a history, so a newer count REPLACES the older one and there is nothing for a
user to adjudicate -- see `app.engines.customer_owned_import` for why the demand
import's staging/review flow would be a choice with one honest answer here.
"""

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.customer_owned_import import (
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    CustomerOwnedImportError,
    build_template,
    parse_and_replace,
)
from app.engines.inventory import customer_has_uploaded
from app.models import (
    Customer,
    CustomerOwnedInventory,
    CustomerOwnedInventoryUpload,
    Product,
)
from app.schemas import (
    CustomerOwnedInventoryOut,
    CustomerOwnedInventoryRowOut,
    CustomerOwnedUploadOut,
    CustomerOwnedUploadRowOut,
    CustomerOwnedUploadSummaryOut,
)

router = APIRouter(
    prefix="/customer-owned-inventory", tags=["customer-owned-inventory"]
)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# 20 MB, matching the demand import. An inventory sheet is a few thousand rows of
# text; anything larger is a mistake or an attack, and refusing it up front is
# cheaper than parsing it.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

COLUMN_CONTRACT = {
    "required": list(REQUIRED_COLUMNS),
    "optional": list(OPTIONAL_COLUMNS),
}


def _customer(db: Session, customer_id: str) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@router.get("/{customer_id}", response_model=CustomerOwnedInventoryOut)
def get_customer_owned_inventory(customer_id: str, db: Session = Depends(get_db)):
    """The customer's declared customer-owned position.

    `has_uploaded` must be read BEFORE `positions`. An empty list means two entirely
    different things depending on it -- "owns none of anything, and told us so" versus
    "nobody has ever told us" -- and the whole reason the upload record exists is to
    keep those apart. The `note` states whichever one applies in prose, so a screen
    that renders only the note is still honest.
    """
    customer = _customer(db, customer_id)
    uploaded = customer_has_uploaded(db, customer.id)

    rows = (
        db.query(CustomerOwnedInventory)
        .filter(CustomerOwnedInventory.customer_id == customer.id)
        .all()
    )
    positions = []
    for row in rows:
        product = db.get(Product, row.product_id)
        positions.append(
            CustomerOwnedInventoryRowOut(
                product_id=row.product_id,
                product_description=(
                    (product.description or product.id) if product else None
                ),
                quantity=row.quantity,
                # The PRODUCT's unit. There is no second unit for customer-owned
                # steel -- it is the same SKU whoever owns it.
                unit_of_measure=product.unit_of_measure,
                source_system=row.source_system,
                source_reference=row.source_reference,
                uploaded_at=row.uploaded_at,
            )
        )
    positions.sort(key=lambda p: (p.product_description or p.product_id))

    last_upload = (
        db.query(CustomerOwnedInventoryUpload)
        .filter(CustomerOwnedInventoryUpload.customer_id == customer.id)
        .order_by(CustomerOwnedInventoryUpload.uploaded_at.desc())
        .first()
    )

    if not uploaded:
        note = (
            f"No customer-owned inventory has ever been uploaded for "
            f"{customer.name!r}. The platform therefore holds NO customer-owned data "
            "about this customer -- that is NOT the same as owning none, and no "
            "screen may render it as 0. Coverage draws nothing from this tier until a "
            f"position is uploaded (POST /customer-owned-inventory/{customer.id}"
            "/uploads)."
        )
    elif not positions:
        note = (
            f"{customer.name!r} has uploaded a position and it declares NO owned "
            "material at all. This is a measured fact, not missing data."
        )
    else:
        note = (
            f"{len(positions)} declared position(s). Customer-owned inventory is "
            "consumed FIRST for the same product, ahead of company stock and ahead of "
            "any Oracle assignment. It can never be offered to another customer. Each "
            "row's uploaded_at is the as-of date of the count -- these figures are "
            "only as current as the last file that arrived."
        )

    return CustomerOwnedInventoryOut(
        customer_id=customer.id,
        customer_name=customer.name,
        business_unit_id=customer.business_unit_id,
        has_uploaded=uploaded,
        last_uploaded_at=last_upload.uploaded_at if last_upload else None,
        positions=positions,
        note=note,
    )


@router.get("/{customer_id}/template")
def download_customer_owned_template(customer_id: str, db: Session = Depends(get_db)):
    """Download the customer's CURRENT declared position as an .xlsx, ready to re-upload.

    NOT a blank template. Every row is a position the platform holds right now, so a
    planner edits real values instead of retyping the position beside a column hint --
    and re-uploading the file UNMODIFIED restates every position at the same quantity
    (every row "Replaced" with `previous_quantity == quantity`, no errors), which is
    the property that makes it a safe starting point. See
    `app.engines.customer_owned_import.build_template`;
    `tests/test_customer_owned_template.py` pins the round trip.

    A GET, and it writes nothing: generating a file is a read of the declared position.

    Two DIFFERENT empty cases, both a valid header-only workbook rather than a 404 or
    an error, and both named in prose on the Notes sheet:

      * never uploaded -- the platform holds no customer-owned data about this
        customer, so there is nothing to export and the file is a form to type the
        first position into. This is the state the button matters MOST in, which is
        why it is not gated on there being data;
      * uploaded and declaring nothing -- a measured fact, and a different fact.

    `X-Customer-Owned-Has-Uploaded` is the discriminator, and it is a separate header
    from the row count on purpose: zero rows alone cannot tell the two apart, and this
    is the same distinction `GET /customer-owned-inventory/{id}` refuses to collapse.

    No route-order hazard here (unlike `/demand-imports/template`, which had to be
    declared before `/{batch_id}`): `/{customer_id}/template` has two path segments
    and `/{customer_id}` has one, so they cannot shadow each other.
    """
    customer = _customer(db, customer_id)
    template = build_template(db, customer)
    return Response(
        content=template.content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{template.filename}"',
            # So a client can say "3 declared positions" without opening the workbook,
            # and so the round-trip test can assert the export was not empty by
            # accident. Exposed through CORS explicitly: a browser cannot read a
            # response header that is not listed there, and the frontend runs on
            # another origin.
            "X-Customer-Owned-Row-Count": str(template.row_count),
            "X-Customer-Owned-Has-Uploaded": (
                "true" if template.has_uploaded else "false"
            ),
            "Access-Control-Expose-Headers": (
                "Content-Disposition, X-Customer-Owned-Row-Count, "
                "X-Customer-Owned-Has-Uploaded"
            ),
        },
    )


@router.post(
    "/{customer_id}/uploads", response_model=CustomerOwnedUploadOut, status_code=201
)
async def upload_customer_owned_inventory(
    customer_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload an .xlsx of customer-owned inventory and REPLACE the declared position.

    The customer comes from the PATH, never from the file. Whose inventory is being
    loaded is an authorisation-shaped fact, and a row naming a different customer is
    refused as a per-row error rather than ignored -- see
    `app.engines.customer_owned_import`.

    File-level problems (not a workbook, empty first sheet, a missing required column)
    are 400s with an explanation, because there is no per-row answer to give.
    Row-level problems -- unknown product, non-numeric or negative quantity, an
    ambiguous as-of date, in-file duplicates, the wrong customer -- are reported as
    rows with `action = "Error"` and never stop the rest of the file from loading.

    Coverage is recomputed for the whole customer as part of this call, and
    `coverage_changes` reports every well whose rollup moved -- including wells the
    file never mentioned, because customer-owned stock is drawn first and one uploaded
    quantity reallocates the customer's whole pool.
    """
    customer = _customer(db, customer_id)

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
        result = parse_and_replace(db, customer, payload, filename=name or None)
    except CustomerOwnedImportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()

    product_ids = sorted({r.product_id for r in result.rows if r.product_id})
    units = (
        {
            p.id: p.unit_of_measure
            for p in db.query(Product).filter(Product.id.in_(product_ids))
        }
        if product_ids
        else {}
    )
    return CustomerOwnedUploadOut(
        upload_id=result.upload_id,
        customer_id=result.customer_id,
        filename=result.filename,
        sheet_name=result.sheet_name,
        row_count=result.row_count,
        created_count=result.created_count,
        replaced_count=result.replaced_count,
        applied_count=result.applied_count,
        error_count=result.error_count,
        rows=[
            CustomerOwnedUploadRowOut(
                row_number=row.row_number,
                action=row.action,
                raw_product=row.raw_product,
                raw_quantity=row.raw_quantity,
                product_id=row.product_id,
                product_description=row.product_description,
                quantity=row.quantity,
                previous_quantity=row.previous_quantity,
                # Null exactly when no product matched. An error row has no validated
                # product and therefore no unit; guessing one would label a cell
                # nobody has checked.
                unit_of_measure=units.get(row.product_id) if row.product_id else None,
                error=row.error,
            )
            for row in result.rows
        ],
        coverage_changes={
            well_id: [before, after]
            for well_id, (before, after) in result.coverage_changes.items()
        },
        column_contract=COLUMN_CONTRACT,
    )


@router.get(
    "/{customer_id}/uploads", response_model=list[CustomerOwnedUploadSummaryOut]
)
def list_customer_owned_uploads(customer_id: str, db: Session = Depends(get_db)):
    """Upload history, newest first -- "when did this data last arrive"."""
    _customer(db, customer_id)
    return (
        db.query(CustomerOwnedInventoryUpload)
        .filter(CustomerOwnedInventoryUpload.customer_id == customer_id)
        .order_by(CustomerOwnedInventoryUpload.uploaded_at.desc())
        .all()
    )
