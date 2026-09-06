"""Excel upload of CUSTOMER-OWNED inventory -- parse, validate, replace.

    Upload -> Validate -> Replace positions (one call)

WHY THERE IS NO STAGING / REVIEW / DECISION STEP, unlike the demand import
-------------------------------------------------------------------------
`app.engines.demand_import` implements a five-step flow
(Upload -> Staging -> Match -> Review -> Apply) whose whole reason for existing is
that THE USER MUST DECIDE something the system cannot decide for them: is this
spreadsheet row a REVISION of an existing demand line, or is it NEW demand? Both
readings are plausible for the same row, they produce different histories, and
guessing wrong rewrites a record of what somebody committed to. So the system
suggests and the user chooses.

An inventory upload has no such question, and inventing one would be cargo-culting
the shape of the other flow rather than reusing its reasoning.

    A DEMAND LINE HAS A HISTORY. An INVENTORY POSITION HAS A CURRENT VALUE.

"Customer C owns 4000 metres of product P" is a stock count. A newer count
REPLACES the older one -- that is what a stock count is. There is no reading under
which the second number is "additional demand" and no reading under which keeping
both is right. A "conflict with an existing row" therefore does not mean what it
means in a demand import: it does not need adjudicating, it means the position is
being restated, which is the ordinary and expected case (`replaced_count` reports
how often it happened, because losing your previous declared quantity is worth
being told about, not worth being asked about).

Offering a Review step here would be offering a choice with one honest answer,
which trains users to click through review screens -- and the demand import's
review screen is one they must actually read.

WHAT IS REUSED FROM THE DEMAND IMPORT, DELIBERATELY
--------------------------------------------------
Everything that is about READING A SPREADSHEET DEFENSIVELY, imported rather than
copied so the two uploads cannot drift into disagreeing about what a cell means:

    `_read_rows`-style header mapping   case/space/underscore-insensitive aliases
    `_text`                            cell -> trimmed text, ints not "4000.0"
    `_parse_quantity`                  numbers, thousands separators, non-finite
    `_parse_date`                      unambiguous dates ONLY (see below)
    per-row errors, never an exception  one bad row never fails the batch
    `DemandImportError`                the file-level failure type, shared

AMBIGUOUS DATES ARE REFUSED, exactly as they are there: `03/04/2027` could be 3
April or 4 March and a silently mis-read date is a silently wrong answer. The only
optional date column here is `as_of_date` (when the customer counted the stock),
and a wrong one would misrepresent how current the position is -- which is the
whole reason the column exists.

THE COLUMN CONTRACT
-------------------
Row 1 of the first worksheet is the header. Header matching is case-insensitive and
ignores surrounding whitespace, underscores and hyphens (``Product ID``,
``product_id`` and ``productid`` are the same column).

    Column       Required  Accepted header aliases          Accepted values
    ------------------------------------------------------------------------------
    product      yes       product, product description,    product description
                           product_id, item, material       (case-insensitive) or
                                                            product id
    quantity     yes       quantity, qty, volume, footage,   a number >= 0;
                           on hand, owned quantity          thousands separators
                                                            tolerated
    customer     no        customer, customer name,          exact customer name
                           operator, customer_id            (case-insensitive) or
                                                            customer id
    as_of_date   no        as of date, as of, count date,    a real Excel date
                           stock date                       cell, or text
                                                            YYYY-MM-DD / YYYY/MM/DD
    location     no        location, yard, warehouse, site   free text, stored on
                                                            source_reference

QUANTITY MAY BE ZERO HERE, AND THAT IS A REAL DIFFERENCE FROM THE DEMAND IMPORT
------------------------------------------------------------------------------
`demand_import._parse_quantity` refuses `0` -- a demand line for nothing is not
demand. An inventory row for nothing IS a fact: "we have run our count and we own
none of this". Refusing it would leave a customer no way to state that except by
omitting the row, which is the one thing the model works hard to keep
distinguishable (see
`app.models.customer_owned_inventory.CustomerOwnedInventory`). So zero is accepted
and negative is refused -- you cannot own less than nothing.

THE CUSTOMER IS NAMED BY THE CALLER, AND A ROW MAY ONLY AGREE WITH IT
--------------------------------------------------------------------
`parse_and_replace` takes the customer as an argument, because whose inventory is
being uploaded is an authorisation-shaped fact and must not be decided by a cell in
a file. The optional `customer` COLUMN exists because the workbook planners
actually use carries one (the source workbook has separate
``Consignment Surplus List`` and ``AkerBP Owned Surplus List`` tabs, i.e. ownership
is a real dimension of their data), and a file that names a DIFFERENT customer is
refused PER ROW rather than ignored: silently loading AkerBP's declared stock
against another operator would be the single worst outcome this endpoint has, and
silently ignoring the cell would hide that the file was for somebody else.

DUPLICATE ROWS FOR THE SAME PRODUCT INSIDE ONE FILE
--------------------------------------------------
Refused per row, naming the row it duplicates -- the same treatment and the same
reason as the demand import's in-file duplicate check. There is no defensible
tie-break: "last row wins" and "sum them" are both a guess about whether the
planner listed one position twice or two yards separately, and the two guesses
differ by the whole quantity. A one-cell edit and a re-upload is cheap; a wrongly
doubled inventory position is a well that does not get its pipe.

The rest of the file loads normally. `error_count` is reported and every error names
its spreadsheet row, so the user can see exactly what did not land.

WHAT THIS WRITES, AND WHAT IT TRIGGERS
--------------------------------------
`CustomerOwnedInventoryUpload` (one row: the audit trail AND the fact that an upload
happened at all -- see the model) plus one `CustomerOwnedInventory` row per accepted
product, created or replaced. Coverage is then recomputed ONCE for the whole
customer through `app.engines.coverage.recompute_customer`, because customer-owned
stock is consumed FIRST and an upload therefore changes verdicts across every well
of that customer at once. Nothing else in the platform is touched -- in particular
no Oracle projection, which this platform never writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.engines.coverage import recompute_customer
from app.engines.demand_import import (
    DemandImportError,
    _normalise_header,
    _text,
)
from app.quantities import parse_quantity_cell
from app.models import (
    Customer,
    CustomerOwnedInventory,
    CustomerOwnedInventoryUpload,
    Product,
)

#: Shared with the demand import so the same file-level failure reaches the API as
#: the same type and the same status code. Re-exported under a local name because a
#: caller of THIS engine should not have to import the demand one to catch its
#: errors.
CustomerOwnedImportError = DemandImportError

REQUIRED_COLUMNS = ("product", "quantity")
OPTIONAL_COLUMNS = ("customer", "as_of_date", "location")

#: canonical name -> accepted header spellings, already normalised by
#: `_normalise_header` (lower-cased, whitespace/underscore/hyphen stripped).
_ALIASES: dict[str, tuple[str, ...]] = {
    "product": (
        "product",
        "productdescription",
        "productid",
        "item",
        "itemdescription",
        "material",
    ),
    "quantity": (
        "quantity",
        "qty",
        "volume",
        "footage",
        "onhand",
        "onhandqty",
        "ownedquantity",
        "ownedqty",
    ),
    "customer": ("customer", "customername", "customerid", "operator"),
    "as_of_date": ("asofdate", "asof", "countdate", "stockdate", "inventorydate"),
    "location": ("location", "yard", "warehouse", "site"),
}

_ACCEPTED_DATE_TEXT = "YYYY-MM-DD or YYYY/MM/DD"


def _map_headers(header_cells: tuple) -> dict[str, int]:
    """{canonical column: 0-based index}. Raises if a required column is absent.

    Deliberately the same shape as `demand_import._map_headers`, including the error
    message listing the header row as it was actually read -- "missing required
    column" is useless without showing the user what the parser saw.
    """
    normalised = [_normalise_header(cell) for cell in header_cells]
    found: dict[str, int] = {}
    for canonical, spellings in _ALIASES.items():
        for idx, name in enumerate(normalised):
            if name and name in spellings and canonical not in found:
                found[canonical] = idx
    missing = [c for c in REQUIRED_COLUMNS if c not in found]
    if missing:
        raise CustomerOwnedImportError(
            "Missing required column(s): "
            + ", ".join(missing)
            + f". Required columns are {', '.join(REQUIRED_COLUMNS)} "
            f"(optional: {', '.join(OPTIONAL_COLUMNS)}). Header row read as: "
            + ", ".join(str(c) for c in header_cells if c is not None)
        )
    return found


def _parse_owned_quantity(cell: object, *, unit=None) -> tuple[float | None, str | None]:
    """Not `demand_import._parse_quantity`: that refuses 0 because a demand line
    for nothing is not demand. Here 0 is the FACT "we counted and own none". The
    coercion and the rest of the rule are shared in app.quantities."""
    return parse_quantity_cell(cell, kind="stock", unit=unit, label="quantity")


def _parse_as_of(cell: object) -> tuple[datetime | None, str | None]:
    """The date the stock was counted. Optional; ambiguous text REFUSED.

    Same rule and same wording as `demand_import._parse_ros`, and it matters for the
    same kind of reason: this date is how a planner judges whether to trust the
    quantity, so silently reading 03/04/2027 as the wrong month would make a
    three-month-old count look current (or the reverse).
    """
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return None, None  # optional -- blank is not an error
    if isinstance(cell, datetime):
        return cell, None
    if isinstance(cell, date):
        return datetime(cell.year, cell.month, cell.day), None
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        # An Excel serial number in a General-formatted cell. Refused for the same
        # reason as there: 45000 is a plausible QUANTITY typed into the wrong column,
        # and guessing would turn a user's mistake into a silent 2023 stock count.
        return None, (
            f"as-of date {cell!r} is a bare number. Format the cell as a date, or "
            f"use text {_ACCEPTED_DATE_TEXT}"
        )
    text = str(cell).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt), None
        except ValueError:
            continue
    return None, (
        f"as-of date {str(cell).strip()!r} is not a date. Use a real Excel date "
        f"cell, or text {_ACCEPTED_DATE_TEXT} (ambiguous formats such as 03/04/2027 "
        "are refused on purpose -- day and month cannot be told apart)"
    )


def _read_rows(file_bytes: bytes) -> tuple[str, list[tuple], dict[str, int]]:
    """(sheet_name, data rows, header index map). Raises on a file-level failure.

    Deliberately a near-twin of `demand_import._read_rows` -- read_only, data_only,
    trailing blank rows dropped -- differing only in the header map it builds and the
    wording of the empty-sheet message. It is a separate function rather than a
    shared one because the shared part is three openpyxl arguments while the
    DIFFERENT part is the column contract, and a parameterised reader would make the
    contract the caller's problem.
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover -- dependency is pinned
        raise CustomerOwnedImportError(
            "openpyxl is not installed; the server cannot read .xlsx uploads"
        ) from exc

    import io

    try:
        workbook = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise CustomerOwnedImportError(
            "File could not be read as an .xlsx workbook "
            f"({type(exc).__name__}). Save it as Excel Workbook (.xlsx) and retry."
        ) from exc

    try:
        sheet = workbook.worksheets[0] if workbook.worksheets else None
        if sheet is None:
            raise CustomerOwnedImportError("Workbook contains no worksheets")
        rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
        sheet_name = sheet.title
    finally:
        workbook.close()

    while rows and all(cell is None or str(cell).strip() == "" for cell in rows[-1]):
        rows.pop()

    if not rows:
        raise CustomerOwnedImportError(
            "The first worksheet is empty. Expected a header row followed by "
            f"customer-owned inventory rows ({', '.join(REQUIRED_COLUMNS)})."
        )

    headers = _map_headers(rows[0])
    return sheet_name, rows[1:], headers


# ---------------------------------------------------------------------------
# Result reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RowOutcome:
    """What happened to ONE spreadsheet row. Scalars only -- no ORM row escapes.

    `action` is one of "Created" | "Replaced" | "Error". "Replaced" is not a warning
    and not a conflict to resolve: an inventory position has a current value and a
    newer count supersedes it (see the module docstring). It is reported because
    `previous_quantity` is a number the user has just lost sight of, and they should
    be shown it rather than have to remember it.
    """

    row_number: int
    action: str
    raw_product: str | None = None
    raw_quantity: str | None = None
    product_id: str | None = None
    product_description: str | None = None
    quantity: float | None = None
    previous_quantity: float | None = None
    error: str | None = None


@dataclass
class UploadResult:
    upload_id: str
    customer_id: str
    filename: str | None = None
    sheet_name: str | None = None
    row_count: int = 0
    created_count: int = 0
    replaced_count: int = 0
    error_count: int = 0
    rows: tuple[RowOutcome, ...] = ()
    #: Wells of this customer whose coverage rollup MOVED as a result of the upload,
    #: as {well_id: (before, after)}. Reported because an inventory upload silently
    #: changing a coverage verdict is exactly the kind of consequence a user should
    #: be shown at the moment they caused it -- customer-owned stock is drawn first,
    #: so an upload can flip wells the file never mentioned.
    coverage_changes: dict[str, tuple[str | None, str | None]] = field(
        default_factory=dict
    )

    @property
    def applied_count(self) -> int:
        return self.created_count + self.replaced_count


# ---------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------


def parse_and_replace(
    db: Session,
    customer: Customer,
    file_bytes: bytes,
    filename: str | None = None,
) -> UploadResult:
    """Parse `file_bytes` and REPLACE `customer`'s declared customer-owned position.

    Raises `CustomerOwnedImportError` only for file-level failures (not a workbook,
    empty first sheet, a missing required column) -- there is no per-row answer to
    give in those cases. Every row-level failure is reported as a `RowOutcome` with
    `action="Error"` and never stops the remaining rows from loading.

    Replacement is PER (customer, product) named in the file. Products the file does
    NOT mention keep their existing position, which is the only safe reading: a
    planner uploading a partial count of one yard must not silently zero everything
    they hold elsewhere. What the file's ARRIVAL does establish is that this customer
    has declared a position at all, which is what lets
    `app.engines.inventory.customer_owned_map` report "owns none" rather than "no
    data" for a product with no row -- see
    `app.models.customer_owned_inventory.CustomerOwnedInventoryUpload`.
    """
    sheet_name, data_rows, headers = _read_rows(file_bytes)

    # Lookup maps built up front rather than queried per row -- same reasoning as
    # `demand_import._load_lookup`: a 500-row upload would otherwise issue 1000
    # lookups. Ids are inserted first so a description can never shadow one.
    products: dict[str, Product] = {}
    for product in db.query(Product).all():
        products.setdefault(product.id.lower(), product)
    for product in db.query(Product).all():
        if product.description:
            products.setdefault(product.description.strip().lower(), product)

    customer_keys = {customer.id.lower(), (customer.name or "").strip().lower()}

    outcomes: list[RowOutcome] = []
    # {product_id: (row_number, quantity, as_of, location)} for accepted rows, so an
    # in-file duplicate can name the row it duplicates.
    accepted: dict[str, tuple[int, float, datetime | None, str | None]] = {}

    for offset, cells in enumerate(data_rows):
        row_number = offset + 2  # header is row 1
        if all(cell is None or str(cell).strip() == "" for cell in cells):
            continue  # blank separator row inside the data -- not an error

        def cell_at(name: str) -> object:
            idx = headers.get(name)
            if idx is None or idx >= len(cells):
                return None
            return cells[idx]

        raw_product = _text(cell_at("product"))
        raw_quantity = _text(cell_at("quantity"))
        problems: list[str] = []

        product: Product | None = None
        if raw_product is None:
            problems.append("product is empty")
        else:
            product = products.get(raw_product.lower())
            if product is None:
                problems.append(f"unknown product {raw_product!r}")

        quantity, err = _parse_owned_quantity(
            cell_at("quantity"),
            unit=product.unit_of_measure if product is not None else None,
        )
        if err:
            problems.append(err)

        as_of, err = _parse_as_of(cell_at("as_of_date"))
        if err:
            problems.append(err)

        location = _text(cell_at("location"))

        # WHOSE inventory. The caller decided; a cell may only agree. Refusing rather
        # than ignoring, because a file naming another operator is a file that was
        # sent to the wrong place, and loading part of it would be worse than loading
        # none of it.
        raw_customer = _text(cell_at("customer"))
        if raw_customer is not None and raw_customer.strip().lower() not in customer_keys:
            problems.append(
                f"customer {raw_customer!r} does not match the customer this upload "
                f"is for ({customer.name!r}). Whose inventory this is cannot be "
                "decided by a cell in the file -- upload it against the right "
                "customer, or correct the cell."
            )

        if not problems and product is not None:
            first = accepted.get(product.id)
            if first is not None:
                problems.append(
                    f"duplicate of row {first[0]} in this file (same product). An "
                    "inventory position is single-valued per product, and there is "
                    "no safe way to guess whether these are one position listed "
                    "twice or two locations to be added -- the two readings differ "
                    "by the whole quantity. Combine them into one row and re-upload."
                )

        if problems:
            outcomes.append(
                RowOutcome(
                    row_number=row_number,
                    action="Error",
                    raw_product=raw_product,
                    raw_quantity=raw_quantity,
                    # Whatever DID parse is still reported, so the user can see how
                    # far the row got -- same as the demand import's error rows.
                    product_id=product.id if product else None,
                    product_description=(
                        (product.description or product.id) if product else None
                    ),
                    quantity=quantity,
                    error="; ".join(problems),
                )
            )
            continue

        assert product is not None and quantity is not None
        accepted[product.id] = (row_number, quantity, as_of, location)

    # ---- write -----------------------------------------------------------
    # Coverage BEFORE, so the result can report which wells moved. Read off the
    # stored rollups rather than recomputed: they are what the user was last shown.
    before_rollups = {
        well.id: well.coverage_status
        for well in _customer_wells(db, customer)
    }

    upload = CustomerOwnedInventoryUpload(
        customer_id=customer.id,
        filename=filename,
        sheet_name=sheet_name,
        row_count=len(outcomes) + len(accepted),
        error_count=len(outcomes),
        source_system="customer-upload",
    )
    db.add(upload)
    db.flush()

    existing = {
        row.product_id: row
        for row in db.query(CustomerOwnedInventory).filter(
            CustomerOwnedInventory.customer_id == customer.id
        )
    }

    created = replaced = 0
    now = datetime.utcnow()
    for product_id, (row_number, quantity, as_of, location) in sorted(
        accepted.items(), key=lambda item: item[1][0]
    ):
        product = db.get(Product, product_id)
        row = existing.get(product_id)
        # `uploaded_at` is the AS-OF date when the file states one, and the upload
        # time otherwise. The as-of date is the better answer wherever it exists:
        # what a planner needs is how old the COUNT is, not how recently somebody
        # got round to sending it.
        stamped = as_of or now
        if row is None:
            db.add(
                CustomerOwnedInventory(
                    customer_id=customer.id,
                    product_id=product_id,
                    quantity=quantity,
                    source_system="customer-upload",
                    source_reference=_reference(filename, location),
                    uploaded_at=stamped,
                    upload_id=upload.id,
                )
            )
            created += 1
            previous = None
            action = "Created"
        else:
            previous = row.quantity
            row.quantity = quantity
            row.source_system = "customer-upload"
            row.source_reference = _reference(filename, location)
            row.uploaded_at = stamped
            row.upload_id = upload.id
            replaced += 1
            action = "Replaced"
        outcomes.append(
            RowOutcome(
                row_number=row_number,
                action=action,
                raw_product=(product.description or product.id) if product else None,
                raw_quantity=f"{quantity:g}",
                product_id=product_id,
                product_description=(
                    (product.description or product.id) if product else None
                ),
                quantity=quantity,
                previous_quantity=previous,
            )
        )

    upload.created_count = created
    upload.replaced_count = replaced
    upload.applied_count = created + replaced
    db.flush()

    # Coverage is recomputed for the WHOLE customer, once. Customer-owned stock is
    # drawn FIRST for its product, so one uploaded quantity can move verdicts on
    # wells this file never mentioned -- exactly like a demand revision, and for the
    # same reason the coverage pass is pool-wide. `recompute_customer` is the only
    # sanctioned writer of a verdict and this engine does not second-guess it.
    recompute_customer(db, customer)

    changes = {}
    for well in _customer_wells(db, customer):
        after = well.coverage_status
        before = before_rollups.get(well.id)
        if before != after:
            changes[well.id] = (before, after)

    return UploadResult(
        upload_id=upload.id,
        customer_id=customer.id,
        filename=filename,
        sheet_name=sheet_name,
        row_count=upload.row_count,
        created_count=created,
        replaced_count=replaced,
        error_count=len(outcomes) - created - replaced,
        rows=tuple(sorted(outcomes, key=lambda o: o.row_number)),
        coverage_changes=changes,
    )


def _reference(filename: str | None, location: str | None) -> str | None:
    """Provenance string for one row: the file, and the yard if the file said one.

    One column rather than two because `source_reference` is a human breadcrumb --
    "where did this number come from" -- and the platform makes no decision from it.
    A separate structured `location` column would imply that it does, and would
    invite somebody to start allocating per yard, which the model has no scope for.
    """
    parts = [p for p in (filename, location) if p]
    return " / ".join(parts) if parts else None


def _customer_wells(db: Session, customer: Customer):
    """Every well of `customer`, for the before/after coverage comparison.

    Imported locally from the coverage engine's own helper rather than re-joined here
    so the "which wells belong to this customer" question has one answer.
    """
    from app.engines.coverage import _customer_wells as wells_of

    return wells_of(db, customer)


# ---------------------------------------------------------------------------
# Template download -- the current declared position, in the shape the parser
# reads back
# ---------------------------------------------------------------------------

#: The template's header row, in order. These are the CANONICAL column names, not
#: aliases: `_normalise_header` maps each one onto its own canonical key, so a
#: generated file round-trips through `_map_headers` by construction rather than by
#: a coincidence of spelling that a later edit to `_ALIASES` could break. Same
#: decision, and the same reason, as `demand_import.TEMPLATE_COLUMNS`.
TEMPLATE_COLUMNS = (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS)


@dataclass(frozen=True)
class OwnedTemplate:
    """A generated workbook plus what a caller needs to describe it.

    `has_uploaded` is carried alongside `row_count` deliberately. Zero rows means two
    entirely different things -- "nobody has ever told us what this customer owns"
    versus "the customer counted and owns nothing" -- and a caller handed only a row
    count could not tell them apart. It is the same discriminator
    `GET /customer-owned-inventory/{id}` exposes, for the same reason.
    """

    content: bytes
    filename: str
    row_count: int
    customer_name: str
    has_uploaded: bool
    notes: tuple[str, ...] = ()


def build_template(db: Session, customer: Customer) -> OwnedTemplate:
    """An .xlsx of `customer`'s CURRENT declared position, in the upload contract.

    THE CORRECTNESS BAR: re-uploading this file unmodified must report every row as
    a "Replaced" whose `previous_quantity` equals its `quantity`, with no errors.
    That is the inventory analogue of the demand template's exact-match-revision bar
    (`app.engines.demand_import.build_template`) -- there is no Replaced-vs-Created
    question to get wrong here, because every exported row is a position that already
    exists, so "Replaced with the same number" IS this flow's spelling of "nothing
    moved". `tests/test_customer_owned_template.py` pins it.

    SCOPE: ONE CUSTOMER, FROM THE PATH
    ----------------------------------
    Not a query parameter, unlike the demand template, because the whole
    customer-owned API is already path-scoped per customer and the upload endpoint it
    feeds is too. There is deliberately no all-customers export: a single re-upload of
    one would be one action able to restate any customer's declared position,
    including customers the planner never opened, and whose inventory is being written
    is the authorisation-shaped fact this engine refuses to let a file decide.

    NO ROW CAP, DELIBERATELY -- same reasoning as the demand template. This file's
    entire purpose is completeness: a silently truncated export would hand a planner
    a file that LOOKS whole, and because an upload replaces only the products the file
    NAMES, the omitted positions would survive untouched with nothing anywhere saying
    they had been left out. Not destructive; undetectable, which is worse.

    PRODUCT IS WRITTEN AS ITS DESCRIPTION
    -------------------------------------
    `parse_and_replace` accepts either the description or the id, and the description
    is what a planner reads and edits. The id is used only when a product has no
    description at all, so the round trip cannot break on a catalogue gap.

    THE `customer` COLUMN IS FILLED IN, AND THAT IS NOT REDUNDANT
    ------------------------------------------------------------
    Whose inventory this is still comes from the upload PATH and never from the file.
    The cell is written because the parser refuses a row naming a DIFFERENT customer,
    so a filled-in cell turns "this file was uploaded against the wrong operator" from
    a silent success into a per-row refusal -- which is exactly the outcome that
    column exists to produce. Writing the name (not the id) keeps it readable.

    `as_of_date` IS WRITTEN; `location` IS DELIBERATELY LEFT BLANK
    -------------------------------------------------------------
    `as_of_date` carries each row's `uploaded_at`, as a real date cell. Leaving it
    blank was considered and rejected: on re-upload a blank as-of stamps the position
    with the upload time, which would make a three-month-old count look like it was
    taken today -- the single most misleading thing this file could do, and the exact
    failure the column exists to prevent. The one honest cost is that a re-upload
    truncates the stored time-of-day to midnight of the same DATE (the column is a
    date, and `_parse_as_of` returns midnight for a date cell). The date -- the part a
    planner judges staleness by -- is preserved exactly. The Notes sheet says so.

    `location` is left EMPTY on purpose. The model has no location field: a location
    from a file is folded into `source_reference` as "filename / location" by
    `_reference`, and there is no way to split that composite back apart -- a yard
    called "Aberdeen" and a file called "Aberdeen.xlsx" are indistinguishable in the
    stored string. Writing the whole `source_reference` into the `location` cell would
    round-trip into "new-file / old-file / old-yard", corrupting the provenance
    breadcrumb a little further on every cycle. So the column is present (so a planner
    CAN state a yard) and blank (so the file asserts nothing the platform does not
    know). The existing `source_reference` is reproduced in the Notes sheet, per
    product, so the information is not lost -- just not put somewhere it would decay.

    EMPTY FILE, TWO DIFFERENT REASONS, BOTH VALID AND BOTH SAID OUT LOUD
    -------------------------------------------------------------------
    A header-only workbook, never a 404 and never an error -- "there is nothing to
    export" is a true answer about a customer that exists, and the platform's standing
    rule is that an absent row and a zero are not the same claim. But nor are the two
    ways of being empty:

      has_uploaded = False    The platform holds NO customer-owned data about this
                              customer. The file is a starting point to TYPE a first
                              position into.
      has_uploaded = True     The customer declared a position and it names no owned
      and no positions        material at all. A measured fact.

    The Notes sheet states whichever one applies, in prose, and the caller gets the
    flag. They produce the SAME file shape on purpose -- the workbook is a form, and a
    form's usefulness must not depend on which of the two states you are in; the
    difference is a fact about the customer, not about the columns.

    And the Notes sheet warns about the one consequence that is easy to walk into:
    uploading the header-only file for a never-uploaded customer is not a no-op. It
    creates an upload record, which flips that customer from "no data" to "declares no
    owned material" -- a real assertion, made on the customer's behalf. Worth saying,
    because it is the one edit-nothing-and-upload path in this flow that changes a
    fact.
    """
    from openpyxl import Workbook

    from app.engines.inventory import customer_has_uploaded

    has_uploaded = customer_has_uploaded(db, customer.id)

    rows = (
        db.query(CustomerOwnedInventory)
        .filter(CustomerOwnedInventory.customer_id == customer.id)
        .all()
    )
    products = {p.id: p for p in db.query(Product).all()}

    def _sort_key(row):
        product = products.get(row.product_id)
        return (
            ((product.description if product else None) or row.product_id).lower(),
            row.product_id,
        )

    # Sorted in Python on the value a planner scans by, so the order is stable across
    # dialects rather than dependent on the database's collation -- the same order the
    # GET endpoint returns positions in, so the file matches the screen.
    rows.sort(key=_sort_key)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Customer-Owned Inventory"
    sheet.append(list(TEMPLATE_COLUMNS))

    references: list[str] = []
    for offset, row in enumerate(rows):
        product = products.get(row.product_id)
        product_key = (product.description if product else None) or row.product_id
        sheet.append(
            [
                product_key,
                row.quantity,
                customer.name,
                row.uploaded_at.date() if row.uploaded_at is not None else None,
                # `location`: deliberately blank. See the docstring.
                None,
            ]
        )
        # A real DATE cell, so `_parse_as_of` reads it back as a date rather than
        # through the text formats -- and so a planner editing it in Excel gets a
        # date picker instead of a string that must match YYYY-MM-DD exactly.
        sheet.cell(row=offset + 2, column=4).number_format = "yyyy-mm-dd"
        if row.source_reference:
            references.append(f"{product_key}: {row.source_reference}")

    notes: list[str] = []
    if rows:
        # Only claimed when there ARE rows. Saying "every row below is a declared
        # position" above an empty sheet would be a true-but-vacuous sentence sitting
        # where the reader is looking for why their file is empty.
        notes.append(
            "This is NOT a blank template. Every row below the header is a position "
            f"{customer.name!r} has DECLARED and that the platform holds right now."
        )
    if not has_uploaded:
        notes.append(
            "THIS FILE IS EMPTY BECAUSE NO CUSTOMER-OWNED INVENTORY HAS EVER BEEN "
            f"UPLOADED FOR {customer.name!r}. The platform holds NO customer-owned "
            "data about this customer -- that is NOT the same as the customer owning "
            "none, and coverage draws nothing from this tier until a position "
            "arrives. Type the position into the rows below and upload the file to "
            "state it for the first time."
        )
        notes.append(
            "WARNING: uploading this file UNCHANGED is not a no-op. An empty file is "
            "accepted, and its arrival records that a position has been declared -- "
            "which moves this customer from 'no data' to 'declares no owned material "
            "at all'. That is a real assertion about the customer. Only do it if it "
            "is true."
        )
    elif not rows:
        notes.append(
            f"THIS FILE IS EMPTY BECAUSE {customer.name!r} HAS UPLOADED A POSITION "
            "AND IT DECLARES NO OWNED MATERIAL AT ALL. That is a measured fact, not "
            "missing data -- a different state from 'never uploaded', which this "
            "sheet would say instead. Add rows below to declare material."
        )
    else:
        notes.append(
            f"Scope: customer {customer.name!r} -- {len(rows)} declared position(s), "
            "the complete position (no row cap)."
        )
    notes.extend(
        [
            "Edit the cells you mean to change and upload this file at "
            "Customer-Owned Inventory. Re-uploading it UNCHANGED restates every "
            "position at the same quantity: each row is reported as 'Replaced' with "
            "previous = new, and nothing moves.",
            "An upload REPLACES the position for the products the file NAMES, and "
            "leaves every other product's position untouched. Deleting a row "
            "therefore does NOT set that position to zero -- it leaves the old "
            "quantity in place. To declare that none is owned, keep the row and put "
            "0 in `quantity`. 0 is a valid, meaningful quantity here: it states 'we "
            "counted and own none'.",
            "Required columns: "
            + ", ".join(REQUIRED_COLUMNS)
            + ". Optional: "
            + ", ".join(OPTIONAL_COLUMNS)
            + ". Do not remove or rename the header row; header matching ignores "
            "case, spaces, underscores and hyphens.",
            "`customer` is filled in with this customer's name. Whose inventory is "
            "being loaded is decided by the customer you upload against, never by "
            "the file -- the cell is there so that uploading this file against the "
            "WRONG customer is refused row by row instead of silently loading one "
            "operator's stock against another. Leave it alone.",
            "`as_of_date` is when the count was taken (each row's current stamp). "
            f"Keep it a real date cell, or text {_ACCEPTED_DATE_TEXT} -- ambiguous "
            "forms such as 03/04/2027 are refused rather than guessed. Note that "
            "re-uploading keeps the DATE and resets the stored time of day to "
            "midnight; the date is what staleness is judged on. Update this cell "
            "whenever you update a quantity, or the new number will be stamped with "
            "the old count's date.",
            "`location` is deliberately left BLANK, even for rows that already have "
            "provenance. There is no location field on a position: a location is "
            "folded into a free-text source reference together with the filename, "
            "and the two cannot be separated again, so re-exporting it would corrupt "
            "the breadcrumb a little more on every round trip. Fill it in if you "
            "want to record a yard; leave it empty and only the filename is "
            "recorded.",
            "There is no staging or review step. This upload takes effect "
            "immediately and recomputes coverage for every well of this customer -- "
            "customer-owned stock is drawn FIRST for its product, so one changed "
            "quantity can move wells this file never mentions.",
            "Each product may appear only ONCE. Two rows for the same product are "
            "refused per row, because 'one position listed twice' and 'two yards to "
            "be added' differ by the whole quantity and there is no safe guess.",
        ]
    )
    if references:
        notes.append(
            "Existing source references (shown here rather than in the `location` "
            "column, for the reason above): " + "; ".join(references)
        )

    note_sheet = workbook.create_sheet("Notes")
    note_sheet.append(["How to use this file"])
    for note in notes:
        note_sheet.append([note])
    note_sheet.column_dimensions["A"].width = 120
    # `_read_rows` reads worksheets[0] ONLY, so this second sheet is inert on
    # re-upload -- which is why the guidance can live in the workbook instead of only
    # on a screen the planner may not still have open.

    import io

    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()

    safe_customer = (
        "".join(
            ch if (ch.isalnum() or ch in "-_") else "-" for ch in (customer.name or "")
        ).strip("-")
        or "customer"
    )
    return OwnedTemplate(
        content=buffer.getvalue(),
        filename=f"customer-owned-{safe_customer}.xlsx",
        row_count=len(rows),
        customer_name=customer.name,
        has_uploaded=has_uploaded,
        notes=tuple(notes),
    )
