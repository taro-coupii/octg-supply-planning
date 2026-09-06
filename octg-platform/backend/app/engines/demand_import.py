"""Excel demand import -- parse, match, apply.

    Upload -> Staging -> Match Existing Demand -> Review -> Apply

The user decides
----------------
`parse_and_stage` never touches demand. It writes `DemandImportBatch` /
`DemandImportRow` only, each row carrying the matcher's SUGGESTION
(`DemandImportMatchType`) and an untouched `decision = PENDING`. `apply_batch`
acts exclusively on rows whose decision the user has set. There is no code path
by which uploading a file changes a demand line.

THE COLUMN CONTRACT
-------------------
Row 1 of the first worksheet is the header. Header matching is
case-insensitive and ignores surrounding whitespace, underscores and spaces
(``ROS Date``, ``ros_date`` and ``rosdate`` are the same column).

    Column     Required  Accepted header aliases        Accepted values
    --------------------------------------------------------------------------
    well       yes       well, well name, well_id       exact well name (case-
                                                        insensitive) or well id
    product    yes       product, product description,  product description
                         product_id, item               (case-insensitive) or
                                                        product id
    quantity   yes       quantity, qty, volume          number > 0; thousands
                                                        separators tolerated
    ros_date   yes       ros date, ros, required_date    a real Excel date cell,
                                                        or text YYYY-MM-DD /
                                                        YYYY/MM/DD
    status     no        status, demand status          Planned | Budgeted |
                                                        Confirmed
                         ^ a statement about the row's WELL -- see below
    profile    no        profile, demand profile        Primary | Contingency

Deliberately NOT supported: ambiguous text dates such as ``03/04/2027``. There
is no way to know whether that is 3 April or 4 March, and a silently mis-read ROS
date is a silently wrong order-by date. Such a cell produces a per-row error
naming the two accepted text formats.

WHAT A `status` CELL MEANS: A STATEMENT ABOUT THE WELL
-----------------------------------------------------
Demand status is a property of the WELL, not of the line
(`app.models.well.Well.demand_status`). A spreadsheet is nonetheless row-shaped,
and the workbook planners actually use carries one status per row, so the column
stays -- with its meaning stated rather than assumed:

    A row's `status` cell asserts the demand status OF THE WELL NAMED IN THAT ROW.

Consequences, all of them deliberate:

  * Applying a row whose status differs from the well's current status is a
    WELL-LEVEL change. It goes through
    `app.engines.coverage.set_well_demand_status`, so it cascades a revision to
    EVERY line of that well -- including lines this spreadsheet never mentioned.
    That is the honest behaviour: confirming a well confirms it, and pretending
    the file only touched the rows it listed would leave the other lines' history
    claiming a status the well no longer has. It is applied ONCE per well however
    many rows named it.
  * The status change is applied BEFORE the row's own quantity/ROS revision, so
    each line's history reads "status moved, then quantity moved" rather than
    recording a new quantity against a superseded status.

TWO ROWS FOR THE SAME WELL THAT DISAGREE
----------------------------------------
Refused, per row, at STAGING -- never reconciled. If two rows name the same well
with two different EXPLICIT statuses, the file is asserting that one well is at two
statuses, which is exactly the state the platform no longer represents. There is no
defensible tie-break: "last row wins" and "most committed wins" are both a guess
about which line of a spreadsheet the planner meant, and either one would silently
move every line of the well.

So each disagreeing row becomes a per-row ERROR naming the row it conflicts with
and both statuses -- the same treatment an in-file duplicate gets, for the same
reason. The rest of the file stages normally, and the fix is a one-cell edit and a
re-upload.

Omitted status / profile
------------------------
When the column is absent or the cell is blank:

  * status INHERITS THE WELL'S CURRENT STATUS, for a REVISION and a NEW row
    alike. A blank cell is not an assertion, so it must change nothing -- and
    since status is now well-level, "changing nothing" is the only safe reading:
    defaulting a NEW row to Planned (which is what this did before, when status
    was a line column and a fresh Planned line was harmlessly out of scope) would
    now assert that the whole well is Planned and drop every line of it out of
    coverage scope. The old rule's safety property survives in a stronger form:
    an import with no status column cannot move anybody's coverage scope at all.
  * profile still defaults to Primary on a NEW row and inherits the matched
    line's profile on a REVISION. Profile is genuinely per line, so nothing about
    it changed.

Defensiveness
-------------
Every failure that is about ONE row is a per-row error and never an exception:
missing cells, non-numeric or non-positive quantity, unparseable date, unknown
well, unknown product, and rows duplicating an earlier row of the same file. Only
failures that make the whole FILE unusable raise `DemandImportError` (not a
workbook, empty sheet, missing required column) -- there is no per-row answer to
give in those cases.

Apply goes through `apply_revision`, always
------------------------------------------
`app.engines.coverage.apply_revision` is the only writer of a demand LINE used
here, and `app.engines.coverage.set_well_demand_status` the only writer of a well's
demand status, so every applied row produces a `DemandRevision` and an
`ImpactRecord` (the Home Dashboard's "Demand Changes" card reads the latter) and
triggers a coverage recompute. `DemandLine` and `Well` are never mutated directly.

A NEW row is created as a zero-quantity line at `current_revision_no = 0` and
then immediately put through `apply_revision`, which writes revision 1 carrying
the imported values. That keeps one writer for demand instead of two, and makes
revision 1 of an imported line a real, inspectable row rather than an implicit
starting state. The zero-quantity moment exists only inside the transaction.

CONFLICTS -- FILE versus LIVE, which is a different question from FILE versus FILE
---------------------------------------------------------------------------------
There are now TWO disagreement checks in this module and they must not be confused,
because they answer different questions and have opposite outcomes:

  `_flag_status_disagreements`   FILE vs FILE. Two rows of the same upload put one
                                 well at two statuses. The FILE is internally
                                 incoherent, there is no honest tie-break, and the
                                 row becomes an ERROR that can only be skipped. See
                                 the section above.

  `detect_conflict`              FILE vs LIVE. The row is perfectly valid and
                                 internally consistent; it simply disagrees with the
                                 data it would land on. That is not a mistake -- it
                                 is usually the whole point of the upload -- so it is
                                 never an error. It is GATED: it requires an explicit
                                 override approval, on top of accept/skip, before
                                 `apply_batch` will write it.

Two kinds of file-vs-live conflict, and why only these two
---------------------------------------------------------
  WELL_DEMAND_STATUS   The row's status cell is EXPLICIT and names a status the
                       well does not currently have. Gated because applying it is
                       not the change the file appears to describe: demand status is
                       a WELL property, so it cascades a revision to every line of
                       that well -- including lines this spreadsheet never listed --
                       and moves the whole well in or out of coverage scope. A
                       planner is entitled to make that change; they are not
                       entitled to make it without being told what it costs.
                       (A BLANK status cell asserts nothing and inherits the well's
                       current status, so it can never conflict.)

  CONCURRENT_REVISION  The matched demand line has been revised since this batch was
                       staged -- `DemandImportRow.baseline_revision_no` no longer
                       equals `DemandLine.current_revision_no`. Somebody else moved
                       the quantity or the ROS through the demand API, a scenario
                       apply or another import, and the reviewer is looking at a diff
                       computed against a value that no longer exists. Applying would
                       overwrite a change nobody in this review ever saw. This is the
                       lost-update problem, and the honest resolution is a human
                       looking at the real current value.

AN ORDINARY REVISION IS NOT A CONFLICT, DELIBERATELY
----------------------------------------------------
"The file says 6500 and the line says 5000" is the ORDINARY case -- it is what a
revision IS -- and gating it would make every single import require two approvals
per row, which is not a safety feature but a reason to stop reading the screen. The
conflict cases above are precisely the two where the diff the reviewer was shown is
NOT the change that would actually happen. Everything else applies exactly as it did
before this feature existed; there is one apply path, not two.

TEMPLATE DOWNLOAD -- the same contract, filled in with the live book
-------------------------------------------------------------------
`build_template` writes an .xlsx whose header is the canonical column contract and
whose body is the customer's CURRENT demand, one row per line. Re-uploading it
unmodified stages every row as an exact-match revision with no conflicts, which is
the correctness bar the round-trip test pins. See that function for the scope and
row-cap decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.engines.coverage import apply_revision, set_well_demand_status
from app.quantities import parse_quantity_cell
from app.models import (
    DemandImportBatch,
    DemandImportBatchStatus,
    DemandImportDecision,
    DemandImportMatchType,
    DemandImportRow,
    DemandLine,
    DemandProfile,
    DemandStatus,
    Product,
    Well,
)


class DemandImportError(Exception):
    """The FILE is unusable -- not a workbook, empty, or missing a required
    column. Row-level problems never raise; they are staged as row errors."""


class DemandImportConflictUnapproved(DemandImportError):
    """Apply was asked to write rows that conflict with live data, unapproved.

    Raised BEFORE anything is written, and the refusal is ALL-OR-NOTHING -- the
    same decision `app.engines.scenario_apply.apply_to_base_plan` makes about
    supply overrides, for the same reason. Applying the unconflicted half of a
    batch and marking the batch Applied would leave the conflicting rows
    permanently unappliable (a batch cannot be applied twice), so approving the
    override afterwards would be too late and the user would have to re-upload the
    whole file to recover a decision they had already made.

    `blocked` is ((row_id, row_number, RowConflict), ...) so the API can answer
    structurally rather than make a client parse the prose.
    """

    def __init__(self, message: str, blocked: tuple):
        super().__init__(message)
        self.blocked = blocked


# ---------------------------------------------------------------------------
# Header handling
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = ("well", "product", "quantity", "ros_date")
OPTIONAL_COLUMNS = ("status", "profile")

# canonical name -> accepted header spellings, already normalised
_ALIASES: dict[str, tuple[str, ...]] = {
    "well": ("well", "wellname", "wellid", "wellno", "wellnumber"),
    "product": (
        "product",
        "productdescription",
        "productid",
        "item",
        "itemdescription",
        "material",
    ),
    "quantity": ("quantity", "qty", "volume", "footage"),
    "ros_date": ("rosdate", "ros", "requireddate", "requiredonsite", "rosdt"),
    "status": ("status", "demandstatus"),
    "profile": ("profile", "demandprofile"),
}

_ACCEPTED_DATE_TEXT = "YYYY-MM-DD or YYYY/MM/DD"


def _normalise_header(value: object) -> str:
    return "".join(str(value or "").lower().split()).replace("_", "").replace("-", "")


def _map_headers(header_cells: tuple) -> dict[str, int]:
    """{canonical column: 0-based index}. Raises if a required column is absent."""
    normalised = [_normalise_header(cell) for cell in header_cells]
    found: dict[str, int] = {}
    for canonical, spellings in _ALIASES.items():
        for idx, name in enumerate(normalised):
            if name and name in spellings and canonical not in found:
                found[canonical] = idx
    missing = [c for c in REQUIRED_COLUMNS if c not in found]
    if missing:
        raise DemandImportError(
            "Missing required column(s): "
            + ", ".join(missing)
            + f". Required columns are {', '.join(REQUIRED_COLUMNS)} "
            f"(optional: {', '.join(OPTIONAL_COLUMNS)}). Header row read as: "
            + ", ".join(str(c) for c in header_cells if c is not None)
        )
    return found


# ---------------------------------------------------------------------------
# Cell parsing -- every function returns (value, error) and never raises
# ---------------------------------------------------------------------------


def _text(cell: object) -> str | None:
    if cell is None:
        return None
    if isinstance(cell, float) and cell.is_integer():
        text = str(int(cell))
    else:
        text = str(cell)
    text = text.strip()
    return text or None


def _parse_quantity(cell: object, *, unit=None) -> tuple[float | None, str | None]:
    """Demand is > 0 -- a line for nothing is not demand. app.quantities holds
    the rule, including whole-number-only for PC/JT once the product is known."""
    return parse_quantity_cell(cell, kind="demand", unit=unit, label="quantity")


def _parse_ros(cell: object) -> tuple[datetime | None, str | None]:
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return None, "ROS date is empty"
    if isinstance(cell, datetime):
        return cell, None
    if isinstance(cell, date):
        return datetime(cell.year, cell.month, cell.day), None
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        # An Excel serial number in a General-formatted cell. Deliberately
        # refused: 45000 is a perfectly plausible quantity typed into the wrong
        # column, and guessing would turn a user's mistake into a silent 2023 ROS.
        return None, (
            f"ROS date {cell!r} is a bare number. Format the cell as a date, or "
            f"use text {_ACCEPTED_DATE_TEXT}"
        )
    text = str(cell).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt), None
        except ValueError:
            continue
    return None, (
        f"ROS date {str(cell).strip()!r} is not a date. Use a real Excel date "
        f"cell, or text {_ACCEPTED_DATE_TEXT} (ambiguous formats such as "
        "03/04/2027 are refused on purpose -- day and month cannot be told apart)"
    )


def _parse_enum(cell: object, enum_cls, label: str):
    """Case-insensitive lookup by VALUE ("Confirmed") or NAME ("CONFIRMED")."""
    text = _text(cell)
    if text is None:
        return None, None
    wanted = text.strip().lower()
    for member in enum_cls:
        if wanted in (member.value.lower(), member.name.lower()):
            return member, None
    allowed = ", ".join(m.value for m in enum_cls)
    return None, f"{label} {text!r} is not recognised (allowed: {allowed})"


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


@dataclass
class _Staged:
    """Working state for one spreadsheet row before it becomes a model row."""

    row_number: int
    raw: dict[str, str | None]
    well: Well | None = None
    product: Product | None = None
    quantity: float | None = None
    ros_date: datetime | None = None
    status: DemandStatus | None = None
    profile: DemandProfile | None = None
    error: str | None = None


def _load_lookup(db: Session) -> tuple[dict[str, Well], dict[str, Product]]:
    """Name/id -> entity maps, built in two queries.

    Built up front rather than queried per row: a 500-row upload would otherwise
    issue 1000 lookups. Keys are lower-cased; an id key never collides with a name
    key in practice (ids are uuids) and ids are inserted first so a name can never
    shadow one.
    """
    wells: dict[str, Well] = {}
    for well in db.query(Well).all():
        wells.setdefault(well.id.lower(), well)
    for well in db.query(Well).all():
        wells.setdefault((well.name or "").strip().lower(), well)

    products: dict[str, Product] = {}
    for product in db.query(Product).all():
        products.setdefault(product.id.lower(), product)
    for product in db.query(Product).all():
        if product.description:
            products.setdefault(product.description.strip().lower(), product)
    return wells, products


def _read_rows(file_bytes: bytes) -> tuple[str, list[tuple], dict[str, int]]:
    """(sheet_name, data rows, header index map). Raises DemandImportError."""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover -- dependency is pinned
        raise DemandImportError(
            "openpyxl is not installed; the server cannot read .xlsx uploads"
        ) from exc

    import io

    try:
        workbook = load_workbook(
            io.BytesIO(file_bytes), read_only=True, data_only=True
        )
    except Exception as exc:
        raise DemandImportError(
            "File could not be read as an .xlsx workbook "
            f"({type(exc).__name__}). Save it as Excel Workbook (.xlsx) and retry."
        ) from exc

    try:
        sheet = workbook.worksheets[0] if workbook.worksheets else None
        if sheet is None:
            raise DemandImportError("Workbook contains no worksheets")
        rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
        sheet_name = sheet.title
    finally:
        workbook.close()

    # Trailing fully-empty rows are an artefact of how Excel stores sheets, not
    # user data.
    while rows and all(cell is None or str(cell).strip() == "" for cell in rows[-1]):
        rows.pop()

    if not rows:
        raise DemandImportError(
            "The first worksheet is empty. Expected a header row followed by "
            f"demand rows ({', '.join(REQUIRED_COLUMNS)})."
        )

    headers = _map_headers(rows[0])
    return sheet_name, rows[1:], headers


def parse_and_stage(
    db: Session, file_bytes: bytes, filename: str | None = None
) -> DemandImportBatch:
    """Parse `file_bytes`, match every row, and stage a batch. Mutates no demand.

    Raises `DemandImportError` only for file-level failures. Row-level failures
    are staged with `match_type = ERROR` and an `error` message, and never stop
    the remaining rows from being staged.
    """
    sheet_name, data_rows, headers = _read_rows(file_bytes)
    wells, products = _load_lookup(db)

    staged: list[_Staged] = []
    # (well_id, product_id, ros day, quantity) -> first spreadsheet row number,
    # so an in-file duplicate names the row it duplicates.
    seen: dict[tuple, int] = {}

    for offset, cells in enumerate(data_rows):
        row_number = offset + 2  # header is row 1
        if all(cell is None or str(cell).strip() == "" for cell in cells):
            continue  # blank separator row inside the data -- not an error

        def cell_at(name: str) -> object:
            idx = headers.get(name)
            if idx is None or idx >= len(cells):
                return None
            return cells[idx]

        raw = {
            "well": _text(cell_at("well")),
            "product": _text(cell_at("product")),
            "quantity": _text(cell_at("quantity")),
            "ros_date": _text(cell_at("ros_date")),
            "status": _text(cell_at("status")),
            "profile": _text(cell_at("profile")),
        }
        item = _Staged(row_number=row_number, raw=raw)
        problems: list[str] = []

        if raw["well"] is None:
            problems.append("well is empty")
        else:
            item.well = wells.get(raw["well"].lower())
            if item.well is None:
                problems.append(f"unknown well {raw['well']!r}")

        if raw["product"] is None:
            problems.append("product is empty")
        else:
            item.product = products.get(raw["product"].lower())
            if item.product is None:
                problems.append(f"unknown product {raw['product']!r}")

        item.quantity, err = _parse_quantity(
            cell_at("quantity"),
            unit=item.product.unit_of_measure if item.product else None,
        )
        if err:
            problems.append(err)
        item.ros_date, err = _parse_ros(cell_at("ros_date"))
        if err:
            problems.append(err)
        item.status, err = _parse_enum(cell_at("status"), DemandStatus, "status")
        if err:
            problems.append(err)
        item.profile, err = _parse_enum(cell_at("profile"), DemandProfile, "profile")
        if err:
            problems.append(err)

        if not problems:
            key = (
                item.well.id,
                item.product.id,
                item.ros_date.date(),
                item.quantity,
            )
            first = seen.get(key)
            if first is not None:
                problems.append(
                    f"duplicate of row {first} in this file (same well, product, "
                    "ROS date and quantity)"
                )
            else:
                seen[key] = row_number

        item.error = "; ".join(problems) if problems else None
        staged.append(item)

    _flag_status_disagreements(staged)

    batch = DemandImportBatch(
        filename=filename,
        sheet_name=sheet_name,
        status=DemandImportBatchStatus.STAGED,
        row_count=len(staged),
        error_count=sum(1 for s in staged if s.error),
    )
    db.add(batch)
    db.flush()

    for item in staged:
        db.add(_stage_row(db, batch, item))
    db.flush()
    return batch


def _flag_status_disagreements(staged: list[_Staged]) -> None:
    """Turn "two rows, one well, two EXPLICIT statuses" into per-row errors.

    Demand status is a property of the well, so a file that says Confirmed on one
    row and Budgeted on another row of the SAME well is asserting a state the
    platform does not represent. There is no honest tie-break -- see the module
    docstring -- so every row in the disagreement is marked ERROR and names the row
    it conflicts with. The FIRST row of the well keeps the answer; every later row
    that contradicts it is flagged, which is the same "names the row it duplicates"
    convention the in-file duplicate check uses, and it means the error message can
    always point backwards to something the user can already see.

    Rows with a BLANK status cell are not part of the disagreement: a blank asserts
    nothing. Rows that already failed validation are skipped -- they have no
    trustworthy well to group by.

    Mutates `staged` in place.
    """
    first_by_well: dict[str, tuple[int, DemandStatus]] = {}
    for item in staged:
        if item.error or item.well is None or item.status is None:
            continue
        seen = first_by_well.get(item.well.id)
        if seen is None:
            first_by_well[item.well.id] = (item.row_number, item.status)
            continue
        first_row, first_status = seen
        if item.status == first_status:
            continue
        item.error = (
            f"status {item.status.value!r} contradicts row {first_row} of this "
            f"file, which gives well {item.well.name!r} status "
            f"{first_status.value!r}. Demand status is a property of the WELL, so "
            "one well cannot be at two statuses -- confirming a well confirms "
            "every line of it. Correct one of the two cells and re-upload."
        )


def _stage_row(
    db: Session, batch: DemandImportBatch, item: _Staged
) -> DemandImportRow:
    row = DemandImportRow(
        batch_id=batch.id,
        row_number=item.row_number,
        raw_well=item.raw["well"],
        raw_product=item.raw["product"],
        raw_quantity=item.raw["quantity"],
        raw_ros_date=item.raw["ros_date"],
        raw_status=item.raw["status"],
        raw_profile=item.raw["profile"],
        decision=DemandImportDecision.PENDING,
    )
    if item.error:
        row.match_type = DemandImportMatchType.ERROR
        row.error = item.error
        row.match_reason = (
            "Row could not be validated, so no match was attempted. Fix the "
            "spreadsheet and re-upload, or skip this row."
        )
        # Whatever DID parse is still stored, so the review screen can show the
        # user how far the row got.
        row.well_id = item.well.id if item.well else None
        row.product_id = item.product.id if item.product else None
        row.quantity = item.quantity
        row.ros_date = item.ros_date
        row.status = item.status
        row.profile = item.profile
        return row

    assert item.well is not None and item.product is not None
    assert item.quantity is not None and item.ros_date is not None

    row.well_id = item.well.id
    row.product_id = item.product.id
    row.quantity = item.quantity
    row.ros_date = item.ros_date

    match_type, matched, reason = _match(db, item)
    row.match_type = match_type
    row.matched_demand_line_id = matched.id if matched else None
    row.match_reason = reason

    # The BASELINE -- what the matched line looked like AT THIS MOMENT. Recorded so
    # that a revision landing between now and apply is detectable rather than
    # silently overwritten. See `DemandImportRow.baseline_revision_no` and
    # `detect_conflict`.
    if matched is not None:
        row.baseline_revision_no = matched.current_revision_no
        row.baseline_quantity = matched.quantity
        row.baseline_ros_date = matched.ros_date

    # Status / profile resolution -- see the module docstring.
    #
    # Status is resolved against the WELL, not the matched line: it is a well-level
    # fact, and an omitted cell must therefore leave the well exactly as it is
    # whether this row is a revision or a brand-new line.
    if item.status is not None:
        row.status = item.status
    else:
        row.status = item.well.demand_status

    if item.profile is not None:
        row.profile = item.profile
    elif matched is not None and match_type == DemandImportMatchType.REVISION:
        row.profile = matched.profile
    else:
        row.profile = DemandProfile.PRIMARY

    return row


def _match(
    db: Session, item: _Staged
) -> tuple[DemandImportMatchType, DemandLine | None, str]:
    """Suggest REVISION or NEW for a validated row.

    Matching key is Well + Product + ROS + Quantity, per the spec. Well +
    Product identifies the candidate set; ROS and quantity then decide whether
    the row is recognisably the SAME demand (hence a revision) or different
    demand (hence new).

    A candidate line is always returned when one exists, even alongside a NEW
    suggestion, because the user may override the suggestion and needs a target.
    """
    assert item.well and item.product and item.ros_date and item.quantity is not None
    candidates = (
        db.query(DemandLine)
        .filter(
            DemandLine.well_id == item.well.id,
            DemandLine.product_id == item.product.id,
        )
        .all()
    )
    if not candidates:
        return (
            DemandImportMatchType.NEW,
            None,
            "No existing demand for this well and product -- suggesting new demand.",
        )

    target_day = item.ros_date.date()
    same_ros = [c for c in candidates if c.ros_date.date() == target_day]
    same_qty = [c for c in candidates if c.quantity == item.quantity]

    exact = next((c for c in same_ros if c.quantity == item.quantity), None)
    if exact is not None:
        return (
            DemandImportMatchType.REVISION,
            exact,
            "Exact match on well, product, ROS date and quantity. Applying would "
            "record a revision with no change; skipping is usually right.",
        )

    if same_ros:
        line = same_ros[0]
        return (
            DemandImportMatchType.REVISION,
            line,
            f"Same well, product and ROS date; quantity differs "
            f"({line.quantity:g} -> {item.quantity:g}). Suggesting a revision.",
        )

    if same_qty:
        line = min(same_qty, key=lambda c: abs((c.ros_date.date() - target_day).days))
        return (
            DemandImportMatchType.REVISION,
            line,
            f"Same well, product and quantity; ROS date differs "
            f"({line.ros_date.date().isoformat()} -> {target_day.isoformat()}). "
            "Suggesting a revision.",
        )

    nearest = min(
        candidates, key=lambda c: abs((c.ros_date.date() - target_day).days)
    )
    return (
        DemandImportMatchType.NEW,
        nearest,
        "Existing demand for this well and product was found, but neither its ROS "
        f"date nor its quantity matches (nearest is {nearest.quantity:g} at "
        f"{nearest.ros_date.date().isoformat()}). Suggesting new demand -- accept "
        "as a revision instead if this is really the same demand re-planned.",
    )


# ---------------------------------------------------------------------------
# Conflict detection -- FILE versus LIVE
# ---------------------------------------------------------------------------

#: A conflict's kind. A plain string vocabulary rather than a database enum,
#: because a conflict is never STORED -- it is derived on every read (see
#: `app.models.demand_import.DemandImportRow.baseline_revision_no` for why), so
#: there is no column for an enum type to constrain and adding one would give
#: assurance about a value the schema never sees.
CONFLICT_WELL_DEMAND_STATUS = "WellDemandStatus"
CONFLICT_CONCURRENT_REVISION = "ConcurrentRevision"


@dataclass(frozen=True)
class RowConflict:
    """ONE reason a staged row disagrees with live data. Scalars only.

    `current_value` / `file_value` are RENDERED strings, deliberately: the review
    screen shows them as a before -> after diff and the two sides of a conflict are
    not always the same type (a status is an enum, a quantity a float, an ROS a
    date). Formatting once, here, is what keeps the screen and the refusal message
    saying the same thing.
    """

    kind: str
    #: What disagrees, named as the user sees it in the spreadsheet.
    field: str
    current_value: str
    file_value: str
    detail: str
    well_id: str | None = None
    demand_line_id: str | None = None
    #: For a WELL_DEMAND_STATUS conflict: how many demand lines of that well would
    #: receive a revision. Includes lines this spreadsheet never mentioned, which is
    #: the entire reason the conflict is gated.
    cascade_line_count: int = 0


def detect_conflict(db: Session, row: DemandImportRow) -> RowConflict | None:
    """The row's FILE-versus-LIVE conflict, computed fresh. Writes nothing.

    THE single authority on what a conflict is. Called by the API on every read (so
    the review screen is never showing a stale verdict), by the preview, and again
    by `apply_batch` at the instant it would write -- because a conflict can appear
    between the reviewer approving and the reviewer pressing Apply, and the check
    that matters is the last one.

    Returns `None` for the ordinary case, which is most rows: see the module
    docstring for why an ordinary revision is deliberately NOT a conflict.

    An ERROR row never conflicts: it cannot be applied at all, so there is nothing
    for an override approval to authorise, and reporting a conflict on it would
    offer the user a decision that changes nothing.
    """
    if row.match_type == DemandImportMatchType.ERROR:
        return None

    # ---- 1. The status cell asserts something the well is not ------------
    # `raw_status` is the VERBATIM cell text, so it is non-null exactly when the
    # planner typed a status. A blank cell inherits the well's current status at
    # staging (see `_stage_row`) and therefore can never disagree with it -- testing
    # `row.status` alone would report a conflict on every blank-status row of a well
    # whose status changed after staging, which is not something the file asserted.
    if row.raw_status is not None and row.status is not None and row.well_id:
        well = db.get(Well, row.well_id)
        if well is not None and well.demand_status != row.status:
            cascade = (
                db.query(DemandLine).filter(DemandLine.well_id == well.id).count()
            )
            return RowConflict(
                kind=CONFLICT_WELL_DEMAND_STATUS,
                field="status",
                current_value=well.demand_status.value,
                file_value=row.status.value,
                well_id=well.id,
                demand_line_id=row.matched_demand_line_id,
                cascade_line_count=cascade,
                detail=(
                    f"This row says well {well.name!r} is "
                    f"{row.status.value!r}; it is {well.demand_status.value!r} "
                    "today. Demand status is a property of the WELL, so applying "
                    f"this row does not only change the line it names -- it writes a "
                    f"revision to all {cascade} demand line(s) of {well.name!r}, "
                    "including any this spreadsheet never listed, and moves the "
                    "whole well into or out of coverage scope. Review the coverage "
                    "impact and approve the override, or correct the status cell and "
                    "re-upload."
                ),
            )

    # ---- 2. Somebody revised the target line since this batch was staged --
    # Only for a row that would actually WRITE that line. A NEW-suggestion row
    # carries the nearest candidate purely so the planner may overrule the
    # suggestion; until they do, drift on a line this row will not touch is noise.
    targets_line = row.match_type == DemandImportMatchType.REVISION or (
        row.decision == DemandImportDecision.ACCEPT_REVISION
    )
    if targets_line and row.matched_demand_line_id and row.baseline_revision_no:
        line = db.get(DemandLine, row.matched_demand_line_id)
        if line is not None and line.current_revision_no != row.baseline_revision_no:
            moved = []
            if row.baseline_quantity is not None and (
                line.quantity != row.baseline_quantity
            ):
                moved.append(
                    f"quantity {row.baseline_quantity:g} -> {line.quantity:g}"
                )
            if row.baseline_ros_date is not None and (
                line.ros_date.date() != row.baseline_ros_date.date()
            ):
                moved.append(
                    f"ROS {row.baseline_ros_date.date().isoformat()} -> "
                    f"{line.ros_date.date().isoformat()}"
                )
            # A revision that moved NEITHER quantity nor ROS is real and worth
            # reporting: a well-status cascade writes exactly such a revision to
            # every line of the well, and it means the line's status -- and
            # therefore whether it is in coverage scope at all -- has changed.
            movement = "; ".join(moved) if moved else (
                "its quantity and ROS are unchanged, so the revision came from a "
                "well demand-status change -- which may have moved this line in or "
                "out of coverage scope"
            )
            return RowConflict(
                kind=CONFLICT_CONCURRENT_REVISION,
                field="quantity / ros_date",
                current_value=(
                    f"{line.quantity:g} at {line.ros_date.date().isoformat()} "
                    f"(revision {line.current_revision_no})"
                ),
                file_value=(
                    f"{row.quantity:g} at {row.ros_date.date().isoformat()}"
                    if row.quantity is not None and row.ros_date is not None
                    else "(not parsed)"
                ),
                well_id=row.well_id,
                demand_line_id=line.id,
                detail=(
                    "The demand line this row would revise has been revised by "
                    f"somebody else since this file was staged: it was at revision "
                    f"{row.baseline_revision_no} then and is at revision "
                    f"{line.current_revision_no} now ({movement}). The diff shown to "
                    "you was computed against a value that no longer exists, so "
                    "applying this row would overwrite a change nobody reviewing "
                    "this batch has seen. Review the coverage impact and approve the "
                    "override, or re-download the template and re-upload against the "
                    "current book."
                ),
            )

    return None


def requires_override_approval(db: Session, row: DemandImportRow) -> bool:
    """True when `apply_batch` would refuse this row for want of an approval."""
    return detect_conflict(db, row) is not None and not row.override_approved


# ---------------------------------------------------------------------------
# Template download -- the current book, in the shape the parser reads back
# ---------------------------------------------------------------------------

#: The template's header row, in order. These are the CANONICAL column names, not
#: aliases: `_normalise_header` maps each one onto its own canonical key, so a
#: generated file round-trips through `_map_headers` by construction rather than by
#: a coincidence of spelling that a later alias edit could break.
TEMPLATE_COLUMNS = (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS)


@dataclass(frozen=True)
class Template:
    """A generated workbook plus what a caller needs to describe it."""

    content: bytes
    filename: str
    row_count: int
    customer_name: str
    well_name: str | None = None
    notes: tuple[str, ...] = ()


def build_template(db: Session, customer, well=None) -> Template:
    """An .xlsx of `customer`'s CURRENT demand, in the import column contract.

    THE CORRECTNESS BAR: re-uploading this file unmodified must stage every row as
    an exact-match REVISION with no errors and no conflicts. That is what makes it a
    safe starting point for an edit -- a planner changes the cells they mean to
    change and nothing else moves. `tests/test_demand_import_template.py` pins it.

    SCOPE: ONE CUSTOMER, REQUIRED
    -----------------------------
    Customer, because that is the scope everything else in this platform is scoped
    to -- coverage is computed per customer pool
    (`app.engines.coverage.recompute_customer`), a scenario belongs to exactly one
    customer, and MRP, sharing and the executive dashboard all take a `customer_id`.
    A planner works one customer's programme.

    An "all customers" export was considered and rejected. Not for size: because a
    single re-upload of it would be one action able to move any well in the platform,
    including wells belonging to customers the planner never opened, and the import
    flow's whole discipline is that a change is reviewed at the scope it affects. The
    optional `well` argument narrows FURTHER, which is always safe.

    NO ROW CAP, DELIBERATELY
    ------------------------
    A blank template is a hint and could be truncated harmlessly. This one's entire
    purpose is completeness: it is the current book, and a silently truncated export
    would hand a planner a file that LOOKS whole. They would edit it, re-upload it,
    see a clean result -- and the omitted lines, having never been mentioned, would
    simply not be there, with nothing anywhere saying so. (Import never deletes, so
    the omission is not destructive; it is undetectable, which is worse.) So the
    export is always the full in-scope book. The 20 MB upload limit is the only
    ceiling, and a demand book large enough to reach it in .xlsx would be hundreds of
    thousands of lines.

    PRODUCT IS WRITTEN AS ITS DESCRIPTION
    -------------------------------------
    `_load_lookup` accepts either the description or the id, and the description is
    what a planner reads and edits. The id is used only when a product has no
    description at all, so the round-trip cannot break on a catalogue gap.

    STATUS IS THE WELL'S, REPEATED ON EVERY ROW OF THAT WELL
    -------------------------------------------------------
    Because that is what the column MEANS (see the module docstring): a status cell
    asserts the demand status of the row's well. Writing the well's current value
    makes an unmodified re-upload assert exactly what is already true -- so no
    conflict is raised, and `set_well_demand_status` would be a no-op even if one
    were approved. Editing one row's status cell is how a planner moves the well;
    editing only SOME rows of a well produces the file-vs-file disagreement error at
    staging, which is the correct refusal and not a template defect.

    KNOWN LIMITATION, STATED RATHER THAN HIDDEN
    ------------------------------------------
    Two live lines of the same well and product with the same ROS date and the same
    quantity, differing only in `profile`, export as two rows that the parser's
    in-file duplicate check (which keys on well + product + ROS + quantity, not
    profile) reads as duplicates -- so the second becomes an ERROR row on re-upload.
    The template reports this in its Notes sheet when it detects such a pair rather
    than dropping a line to make the round-trip look clean; the export is a faithful
    picture of the book, and a picture that quietly omits demand is the one thing it
    must not be.
    """
    from openpyxl import Workbook

    from app.models import PlanningNode

    query = (
        db.query(DemandLine)
        .join(Well, DemandLine.well_id == Well.id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id == customer.id)
    )
    if well is not None:
        query = query.filter(DemandLine.well_id == well.id)
    lines = query.all()
    # Sorted in Python on the values a planner scans by, so the order is stable
    # across dialects rather than dependent on the database's collation.
    lines.sort(
        key=lambda ln: (
            (ln.well.name or "").lower(),
            ln.ros_date,
            (ln.product.description or ln.product_id or "").lower(),
            ln.id,
        )
    )

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Demand"
    sheet.append(list(TEMPLATE_COLUMNS))

    seen: dict[tuple, int] = {}
    duplicate_pairs: list[str] = []
    for offset, line in enumerate(lines):
        product_key = line.product.description or line.product_id
        sheet.append(
            [
                line.well.name,
                product_key,
                line.quantity,
                line.ros_date.date(),
                line.well.demand_status.value,
                line.profile.value,
            ]
        )
        # ROS is written as a real DATE cell, so `_parse_ros` reads it back as a date
        # rather than through the text formats -- and so a planner editing it in Excel
        # gets a date picker instead of a string that must match YYYY-MM-DD exactly.
        sheet.cell(row=offset + 2, column=4).number_format = "yyyy-mm-dd"

        key = (line.well_id, line.product_id, line.ros_date.date(), line.quantity)
        first = seen.get(key)
        if first is None:
            seen[key] = offset + 2
        else:
            duplicate_pairs.append(
                f"rows {first} and {offset + 2} ({line.well.name}, {product_key}, "
                f"{line.ros_date.date().isoformat()}, {line.quantity:g})"
            )

    notes = [
        "This is NOT a blank template. Every row below the header is a demand line "
        "that exists in the platform right now.",
        f"Scope: customer {customer.name!r}"
        + (f", well {well.name!r}" if well is not None else "")
        + f" -- {len(lines)} demand line(s), the complete in-scope book (no row cap).",
        "Edit the cells you mean to change and upload this file at Demand Import. "
        "Re-uploading it UNCHANGED stages every row as an exact-match revision and "
        "changes nothing.",
        "The `status` column is the WELL's demand status, repeated on every row of "
        "that well. Changing it on one row asserts a new status for the whole well "
        "and cascades a revision to every line of it -- the review screen will flag "
        "that as a conflict and require an explicit override approval. Changing it "
        "on only SOME rows of a well is refused at staging: one well cannot be at "
        "two statuses.",
        "Do not add, remove or rename the header row. Header matching ignores case, "
        "spaces and underscores. ROS dates must stay real date cells (or text "
        f"{_ACCEPTED_DATE_TEXT}); ambiguous forms such as 03/04/2027 are refused "
        "rather than guessed.",
        "Adding a row for a well and product with no existing demand creates NEW "
        "demand. Deleting a row does NOT delete demand -- an import never removes a "
        "line, it only revises and creates.",
    ]
    if duplicate_pairs:
        notes.append(
            "WARNING -- this export contains line(s) the importer's in-file "
            "duplicate check cannot tell apart, because they share a well, product, "
            "ROS date and quantity and differ only by profile: "
            + "; ".join(duplicate_pairs)
            + ". Re-uploading unmodified will stage the later row of each pair as an "
            "ERROR. Nothing is wrong with the data; the duplicate check keys on four "
            "columns and profile is not one of them. Edit or delete one row of each "
            "pair before uploading, or upload the two profiles in separate files."
        )

    note_sheet = workbook.create_sheet("Notes")
    note_sheet.append(["How to use this file"])
    for note in notes:
        note_sheet.append([note])
    note_sheet.column_dimensions["A"].width = 120
    # The parser reads worksheets[0] ONLY, so this second sheet is inert on
    # re-upload -- which is why the guidance can live in the workbook instead of
    # only on a screen the planner may not still have open.

    import io

    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()

    safe_customer = "".join(
        ch if (ch.isalnum() or ch in "-_") else "-" for ch in customer.name
    ).strip("-") or "customer"
    safe_well = (
        "-"
        + (
            "".join(
                ch if (ch.isalnum() or ch in "-_") else "-" for ch in (well.name or "")
            ).strip("-")
        )
        if well is not None
        else ""
    )
    return Template(
        content=buffer.getvalue(),
        filename=f"demand-{safe_customer}{safe_well}.xlsx",
        row_count=len(lines),
        customer_name=customer.name,
        well_name=well.name if well is not None else None,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    batch_id: str
    revised_count: int = 0
    created_count: int = 0
    skipped_count: int = 0
    pending_count: int = 0
    error_count: int = 0
    failed_row_ids: tuple[str, ...] = ()
    impact_record_ids: tuple[str, ...] = ()
    #: Wells whose DEMAND STATUS this batch changed, and how many demand lines the
    #: cascade revised. Reported separately from `revised_count` (which counts
    #: SPREADSHEET ROWS applied) because a status change touches lines the file
    #: never mentioned -- a number the user must be shown rather than left to infer.
    well_status_changed_ids: tuple[str, ...] = ()
    well_status_cascaded_line_count: int = 0


def apply_batch(db: Session, batch: DemandImportBatch) -> ApplyResult:
    """Apply the rows the USER accepted. Every demand write goes through
    `apply_revision`, so history and impact records always exist.

    Rows left PENDING are counted and otherwise untouched -- the user has not
    decided about them, and deciding on their behalf is precisely what this flow
    exists to avoid. Rows marked SKIP change nothing at all.

    CONFLICTING ROWS ARE GATED ON A SECOND DECISION
    -----------------------------------------------
    Before anything is written, every ACCEPTED row is re-checked with
    `detect_conflict`. A row that disagrees with the live data it would land on and
    whose `override_approved` is still False raises
    `DemandImportConflictUnapproved` and NOTHING in the batch is applied. See that
    exception for why the refusal is all-or-nothing, and the module docstring for
    what does and does not count as a conflict. A row with no conflict -- the
    common case -- reaches exactly the same code it always did.
    """
    if batch.status == DemandImportBatchStatus.APPLIED:
        raise DemandImportError(
            f"Batch {batch.id} has already been applied. Upload the file again to "
            "import further changes."
        )

    # ---- The conflict gate, BEFORE any write -----------------------------
    # Re-derived here rather than trusted from the review screen: live data moves,
    # and a conflict can appear between the reviewer reading the diff and the
    # reviewer pressing Apply. The check that decides is the last one.
    #
    # A row with no conflict passes straight through, so the ordinary import is not
    # slowed down by a single extra query per unconflicted row beyond the detector's
    # own cheap lookups.
    blocked: list[tuple[str, int, RowConflict]] = []
    for row in batch.rows:
        if row.match_type == DemandImportMatchType.ERROR:
            continue
        if row.decision not in (
            DemandImportDecision.ACCEPT_REVISION,
            DemandImportDecision.ACCEPT_NEW,
        ):
            continue
        conflict = detect_conflict(db, row)
        if conflict is not None and not row.override_approved:
            blocked.append((row.id, row.row_number, conflict))
    if blocked:
        raise DemandImportConflictUnapproved(
            f"{len(blocked)} accepted row(s) of this batch conflict with the live "
            "demand book and the override has not been approved. NOTHING was "
            "applied -- the refusal is all-or-nothing, because a partly-applied "
            "batch could never be finished (a batch cannot be applied twice). "
            "Review each conflict's coverage impact and approve the override, or "
            "skip the row.\n  - "
            + "\n  - ".join(
                f"Excel row {number}: {conflict.detail}"
                for _row_id, number, conflict in blocked
            ),
            blocked=tuple(blocked),
        )

    result = ApplyResult(batch_id=batch.id)
    impacts: list[str] = []
    failed: list[str] = []

    # ---- Well demand statuses FIRST --------------------------------------
    # A row status cell is a statement about its WELL (see the module docstring),
    # so it is applied at well level, once per well, and BEFORE any per-line
    # revision. Doing it first is what makes each line history read "status moved,
    # then quantity moved": `apply_revision` snapshots the well CURRENT status
    # into the revision it writes, so the other order would record the imported
    # quantity against a status the well has just stopped having.
    #
    # Only rows the USER accepted are consulted. A PENDING or SKIP row status is
    # not a decision. Staging has already refused any file whose rows disagree
    # about one well, so there is nothing left to reconcile here.
    status_wanted: dict[str, DemandStatus] = {}
    for row in batch.rows:
        if row.match_type == DemandImportMatchType.ERROR:
            continue
        if row.decision not in (
            DemandImportDecision.ACCEPT_REVISION,
            DemandImportDecision.ACCEPT_NEW,
        ):
            continue
        if row.well_id is None or row.status is None:
            continue
        status_wanted.setdefault(row.well_id, row.status)

    changed_wells: list[str] = []
    cascaded = 0
    for well_id, wanted in sorted(status_wanted.items()):
        well = db.get(Well, well_id)
        if well is None or well.demand_status == wanted:
            continue
        change = set_well_demand_status(db, well, wanted)
        changed_wells.append(well_id)
        cascaded += change.revised_line_count
        impacts.extend(change.impact_record_ids)
    result.well_status_changed_ids = tuple(changed_wells)
    result.well_status_cascaded_line_count = cascaded

    for row in batch.rows:
        if row.match_type == DemandImportMatchType.ERROR:
            result.error_count += 1
            if row.decision == DemandImportDecision.SKIP:
                result.skipped_count += 1
            continue
        if row.decision == DemandImportDecision.SKIP:
            result.skipped_count += 1
            continue
        if row.decision == DemandImportDecision.PENDING:
            result.pending_count += 1
            continue

        if row.decision == DemandImportDecision.ACCEPT_REVISION:
            line = (
                db.get(DemandLine, row.matched_demand_line_id)
                if row.matched_demand_line_id
                else None
            )
            if line is None:
                row.apply_error = (
                    "Accepted as a revision but the matched demand line no longer "
                    "exists. Re-upload the file to re-match."
                )
                failed.append(row.id)
                continue
            impact = apply_revision(
                db, line, row.quantity, row.ros_date, row.profile
            )
            row.applied = True
            row.applied_demand_line_id = line.id
            impacts.append(impact.id)
            result.revised_count += 1
            continue

        # ACCEPT_NEW. Created at revision 0 then immediately revised, so
        # `apply_revision` remains the single writer of demand and revision 1
        # carries the imported values. See the module docstring.
        line = DemandLine(
            well_id=row.well_id,
            product_id=row.product_id,
            quantity=0.0,
            ros_date=row.ros_date,
            profile=DemandProfile.PRIMARY,
            current_revision_no=0,
        )
        db.add(line)
        db.flush()
        impact = apply_revision(db, line, row.quantity, row.ros_date, row.profile)
        row.applied = True
        row.applied_demand_line_id = line.id
        impacts.append(impact.id)
        result.created_count += 1

    batch.status = DemandImportBatchStatus.APPLIED
    batch.applied_at = datetime.utcnow()
    db.flush()

    result.failed_row_ids = tuple(failed)
    result.impact_record_ids = tuple(impacts)
    return result
