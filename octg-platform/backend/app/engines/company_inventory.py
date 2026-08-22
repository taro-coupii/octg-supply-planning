"""Maintenance surface for the platform's COMPANY-OWNED inventory projections.

MVP-COMPROMISE[C-03]: the platform writes to Oracle-owned inventory projections.
    WHY:    design principle #5 says these three tables are read-only projections
            of an Oracle-owned domain. There is no Oracle interface in the MVP,
            so seeded rows are uncorrectable without this surface.
    REMOVE: when the Oracle feed is live it stamps its own `source_system`, and
            the PLATFORM_MAINTAINABLE_SOURCES gate then makes those rows
            read-only with no code change. Decide then whether to keep this
            surface as a manual-correction path (if kept, it needs an audit log)
            or delete it. See MVP_COMPROMISES.md C-03.

WHY THIS EXISTS, AND WHY IT ALMOST DID NOT
-------------------------------------------
`InventoryOnHand`, `InventoryOnOrder` and `InventoryAssignment` are read-only local
PROJECTIONS of Oracle-owned data (see each model's docstring). Design principle #5 is
explicit: this platform never creates, releases or overwrites a hard reservation, and
until now that principle was enforced the simple way -- by exposing no write endpoint
for any of the three tables anywhere.

But in the MVP there is no Oracle interface at all. Every row in these three tables
got there from a seed script, and a wrong seed value had no correction path except
editing the database by hand. That is not a smaller problem than the one the read-only
rule solves; it is the same problem wearing a different hat -- an unreachable wrong
number is exactly as unhelpful to a planner as an overridable right one would be
dangerous.

THE GATE THAT RESOLVES THE TENSION
-----------------------------------
`source_system` already exists on all three models, always has, and already
distinguishes `"synthetic"` (seeded demo data with no real source) from `"oracle"`
(a real feed, not yet connected). The resolution is to make that column load-bearing:

    A write here may only ever touch a row whose source_system is one this platform
    itself is allowed to originate. A row stamped "oracle" is refused, unconditionally,
    citing Oracle as the fix.

`PLATFORM_MAINTAINABLE_SOURCES` is that set. It is `{"synthetic", "manual"}` --
"synthetic" because that is what every seeded row already carries, and "manual"
because every row THIS module writes is stamped with it (see below). The moment a real
Oracle feed lands and starts stamping `source_system="oracle"`, every row it touches
becomes automatically un-writable through this surface with NO CODE CHANGE here: the
gate is a membership test against a row's own column, not a mode flag this module has
to be told to flip. That is the whole point of building it this way rather than as a
feature flag -- a feature flag has to be remembered; a column value cannot be forgotten
because it is the same column the read side already renders.

WHAT "MANUAL" MEANS, AND WHY IT IS STAMPED ON EVERY WRITE
----------------------------------------------------------
Every successful write through this module -- inline edit, inline create, or a
template upload -- stamps `source_system="manual"` and `synced_at=<now>` on the row it
touched, REGARDLESS of what the row's `source_system` was before the write (which, by
the gate above, was already "synthetic" or "manual" or the write would have been
refused). This is deliberate and it is the whole reason a planner can trust the
distinction on screen: "manual" does not mean "seed data since amended" and "synthetic"
does not mean "a human touched this once but resaved the old label" -- the label is a
straight answer to "does a human need to double-check this figure", and the timestamp
is a straight answer to "when did a human last look at it".

COVERAGE IS RECOMPUTED ON EVERY SUCCESSFUL WRITE, NEVER RE-DERIVED HERE
-------------------------------------------------------------------------
On-hand quantity is the foundation of every coverage verdict (`app.engines.coverage`).
`HANDOFF.md` section 7 records the recurring failure mode this module exists to avoid
a THIRD occurrence of: a stored `CoverageResult` row silently outliving the on-hand
figure it was computed from, because nothing told it to recompute. Exactly like
`app.engines.customer_owned_import.parse_and_replace`,
`app.engines.lead_time_admin.apply_lead_time_change` and
`app.engines.substitution_admin.apply_substitution_change`, every write in this module
finishes by calling `app.engines.coverage.recompute_customer` -- never a second
implementation of the coverage rules -- and reports which wells' verdicts moved.

`_recompute_business_unit` is the orchestration wrapper for that call, scoped to every
customer of the AFFECTED Business Unit (an on-hand or on-order edit can move the
verdict of every customer sharing that BU's pool, not just one). It follows the exact
shape `apply_substitution_change` already established: one SAVEPOINT per customer, so
a customer whose pass cannot resolve its own inventory (raises `InventoryRowMissing` /
`InventoryScopeMissing`) is rolled back to its prior stored state and reported as a
failure rather than aborting -- or half-writing -- everyone else's recompute. This is
orchestration, not a second coverage algorithm: the only function that ever computes or
persists a verdict is `recompute_customer` itself.

An assignment edit is narrower -- it belongs to one demand line, hence one well, hence
one customer -- so only that customer is recomputed, through the same
`recompute_customer` call, inside the same savepoint pattern for consistency.

READ SIDE: THE UNKNOWN-VS-ZERO RULE, KEPT INTACT
--------------------------------------------------
This module changes nothing about how a missing row is read. An absent
`InventoryOnHand` row is still UNKNOWN, never zero; an absent `InventoryOnOrder` row is
still UNKNOWN, distinct from an explicit `quantity=0` row that states "nothing on
order"; and every quantity is reported beside the `Product.unit_of_measure` it was
measured in. `get_position` is a NEW read (there was no company-inventory read surface
before this), but it resolves quantities through no route other than the ones
`app.engines.inventory` already exposes for exactly the same figures used elsewhere,
and does not re-derive the unknown-vs-zero rule.

TEMPLATE / UPLOAD IS SCOPED TO ON-HAND ONLY, AND THAT IS A DELIBERATE NARROWING
----------------------------------------------------------------------------------
`InventoryOnHand` has a UNIQUE constraint on (business_unit_id, product_id) -- one
declared quantity per product, exactly like `CustomerOwnedInventory` per (customer,
product). "Replace the position named in this file" therefore has one honest meaning
for it, the same one `customer_owned_import.parse_and_replace` already implements.

`InventoryOnOrder` does NOT have that constraint, on purpose (see its docstring):
several rows per (BU, product) are the ordinary, correct state of a delivery schedule
with more than one PO line. "Replace the position" has NO single meaning for a
multi-row fact -- replace by what key, when three rows for the same product might be
three different purchase orders with three different arrival dates that a planner needs
to keep apart? Forcing a spreadsheet grain onto that would either collapse real POs
into one row (destroying the arrival-date history the model exists to carry) or invent
a row-matching heuristic with no textual key to match on. Neither is honest, so
on-order (and, for the same reason of per-row granularity, `InventoryAssignment`, which
is keyed to an individual demand line) is edited ONLY through the inline
create/edit/delete endpoints below, which already handle one row at a time without
ambiguity.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.engines.coverage import recompute_customer
from app.engines.demand_import import DemandImportError, _normalise_header, _text
from app.engines.inventory import InventoryRowMissing, InventoryScopeMissing
from app.models import (
    BusinessUnit,
    CompanyInventoryUpload,
    Customer,
    DemandLine,
    InventoryAssignment,
    InventoryOnHand,
    InventoryOnOrder,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
)

#: THE gate. A row may be written by this module only when its CURRENT
#: `source_system` is one of these. "synthetic" is what every seeded row already
#: carries; "manual" is what every write through this module stamps (see the module
#: docstring). A real Oracle feed will stamp its own rows "oracle", which is NOT in
#: this set -- so those rows become read-only here automatically, with no code
#: change, the instant the feed exists.
#:
#: MVP-COMPROMISE[C-03]: this constant IS the compromise. See the module docstring
#: and MVP_COMPROMISES.md C-03 for why this surface exists at all and what removes
#: it (or keeps it, with an audit log, as a deliberate emergency path).
PLATFORM_MAINTAINABLE_SOURCES = frozenset({"synthetic", "manual"})

CompanyInventoryImportError = DemandImportError


class NotMaintainable(Exception):
    """A write targeted a row whose `source_system` is not platform-maintainable.

    Carries enough to build the 403 without a second query: which row, what it is
    currently stamped, and the fact that the fix is upstream.
    """

    def __init__(self, row_kind: str, row_id: str, source_system: str):
        self.row_kind = row_kind
        self.row_id = row_id
        self.source_system = source_system
        super().__init__(
            f"{row_kind} row {row_id} is sourced from {source_system!r}, not "
            f"{sorted(PLATFORM_MAINTAINABLE_SOURCES)!r}. This platform never "
            "creates, releases or overwrites a hard reservation, and it does not "
            "overwrite an Oracle-fed row either -- the fix belongs upstream, in "
            "Oracle (or in whatever produced this feed), not on this screen."
        )


def _editability(source_system: str | None) -> tuple[bool, str | None]:
    """(editable, reason) for a row carrying `source_system`.

    The read side and the write side must agree about which rows are touchable, so
    both go through this one function -- `get_position` renders the flag, and every
    write function below raises `NotMaintainable` using the same test.
    """
    resolved = source_system or "synthetic"
    if resolved in PLATFORM_MAINTAINABLE_SOURCES:
        return True, None
    return False, (
        f"Sourced from {resolved!r}. This platform never overwrites a row fed by a "
        "real upstream system -- correct it there (Oracle, or whatever produced "
        "this feed) and it will be reflected here once re-synced."
    )


def _require_maintainable(row_kind: str, row_id: str, source_system: str | None) -> None:
    editable, _ = _editability(source_system)
    if not editable:
        raise NotMaintainable(row_kind, row_id, source_system or "synthetic")


# ---------------------------------------------------------------------------
# READ SIDE
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OnHandRow:
    """On-hand for one (Business Unit, product). `known=False` means NO row exists
    -- the quantity is UNKNOWN, never zero. See `InventoryOnHand`.
    """

    business_unit_id: str
    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure
    known: bool
    row_id: str | None = None
    quantity: float | None = None
    source_system: str | None = None
    synced_at: datetime | None = None
    editable: bool = True
    not_editable_reason: str | None = None


@dataclass(frozen=True)
class OnOrderRow:
    """One `InventoryOnOrder` row (one PO line). Several may exist per product --
    see the module docstring for why that is normal and why this is per-row, not
    per-product.
    """

    row_id: str
    business_unit_id: str
    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure
    quantity: float
    expected_arrival_date: datetime | None
    source_system: str
    source_reference: str | None
    synced_at: datetime | None
    editable: bool
    not_editable_reason: str | None


@dataclass(frozen=True)
class AssignmentLine:
    """One `InventoryAssignment` row: a quantity tied to ONE demand line."""

    row_id: str
    demand_line_id: str
    well_id: str
    well_name: str | None
    customer_id: str
    quantity: float
    unit_of_measure: UnitOfMeasure
    source_system: str
    source_reference: str | None
    synced_at: datetime | None
    editable: bool
    not_editable_reason: str | None


@dataclass(frozen=True)
class ProductAssignmentGroup:
    """Oracle assignment quantity for one product, aggregated, with its lines.

    `total_quantity` is a plain sum because every line here is the SAME product and
    therefore the same unit -- unlike the cross-product aggregates in
    `app.engines.executive`, this is not a case `quantity_by_unit` needs to guard.
    """

    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure
    total_quantity: float
    lines: tuple[AssignmentLine, ...]


@dataclass(frozen=True)
class CompanyInventoryPosition:
    """The company-owned inventory position for one Business Unit (optionally one
    product), across all three Oracle-projection tables.
    """

    business_unit_id: str
    business_unit_name: str
    on_hand: tuple[OnHandRow, ...]
    on_order: tuple[OnOrderRow, ...]
    assignments: tuple[ProductAssignmentGroup, ...]


def _product_label(product: Product) -> str:
    return product.description or product.id


def get_position(
    db: Session, business_unit_id: str, product_id: str | None = None
) -> CompanyInventoryPosition:
    """The company-owned position for `business_unit_id`.

    Scope of the product list, when `product_id` is None
    -----------------------------------------------------
    Every product touched by ANY of the three tables in this BU -- an on-hand row,
    an on-order row, or an assignment on a demand line belonging to this BU -- is
    included. A product is never manufactured into the list from the catalogue
    alone: a catalogue entry with no data of any kind in this BU is not this BU's
    business to report on, and would force a `known=False` on-hand row into every
    response regardless of relevance.

    When `product_id` IS given, that product is always represented (even with
    `known=False` and no on-order/assignment rows at all) because the caller asked
    about it by name -- an explicit ask must always get an explicit answer, unknown
    or not, never a silent omission.
    """
    bu = db.get(BusinessUnit, business_unit_id)
    if bu is None:
        raise ValueError(f"No BusinessUnit {business_unit_id}")

    on_hand_rows = (
        db.query(InventoryOnHand)
        .filter(InventoryOnHand.business_unit_id == business_unit_id)
        .all()
    )
    on_order_rows_all = (
        db.query(InventoryOnOrder)
        .filter(InventoryOnOrder.business_unit_id == business_unit_id)
        .all()
    )
    assignment_rows = (
        db.query(InventoryAssignment)
        .join(DemandLine, InventoryAssignment.demand_line_id == DemandLine.id)
        .join(Well, DemandLine.well_id == Well.id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .join(Customer, PlanningNode.customer_id == Customer.id)
        .filter(Customer.business_unit_id == business_unit_id)
        .all()
    )

    if product_id is not None:
        on_hand_rows = [r for r in on_hand_rows if r.product_id == product_id]
        on_order_rows_all = [r for r in on_order_rows_all if r.product_id == product_id]
        assignment_rows = [r for r in assignment_rows if r.product_id == product_id]
        product_ids = {product_id}
    else:
        product_ids = (
            {r.product_id for r in on_hand_rows}
            | {r.product_id for r in on_order_rows_all}
            | {r.product_id for r in assignment_rows}
        )

    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(product_ids))} if product_ids else {}
    if product_id is not None and product_id not in products:
        product = db.get(Product, product_id)
        if product is not None:
            products[product_id] = product

    on_hand_by_product = {r.product_id: r for r in on_hand_rows}
    out_on_hand: list[OnHandRow] = []
    for pid in sorted(product_ids):
        product = products.get(pid)
        if product is None:
            continue
        row = on_hand_by_product.get(pid)
        if row is None:
            out_on_hand.append(
                OnHandRow(
                    business_unit_id=business_unit_id,
                    product_id=pid,
                    product_description=_product_label(product),
                    unit_of_measure=product.unit_of_measure,
                    known=False,
                    editable=True,
                    not_editable_reason=None,
                )
            )
        else:
            editable, reason = _editability(row.source_system)
            out_on_hand.append(
                OnHandRow(
                    business_unit_id=business_unit_id,
                    product_id=pid,
                    product_description=_product_label(product),
                    unit_of_measure=product.unit_of_measure,
                    known=True,
                    row_id=row.id,
                    quantity=max(0.0, row.quantity or 0.0),
                    source_system=row.source_system,
                    synced_at=row.synced_at,
                    editable=editable,
                    not_editable_reason=reason,
                )
            )

    out_on_order: list[OnOrderRow] = []
    for row in sorted(
        on_order_rows_all,
        key=lambda r: (
            _product_label(products.get(r.product_id)) if products.get(r.product_id) else r.product_id,
            r.expected_arrival_date or datetime.max,
        ),
    ):
        product = products.get(row.product_id)
        editable, reason = _editability(row.source_system)
        out_on_order.append(
            OnOrderRow(
                row_id=row.id,
                business_unit_id=business_unit_id,
                product_id=row.product_id,
                product_description=_product_label(product) if product else None,
                unit_of_measure=(
                    product.unit_of_measure if product else UnitOfMeasure.MTR
                ),
                quantity=max(0.0, row.quantity or 0.0),
                expected_arrival_date=row.expected_arrival_date,
                source_system=row.source_system,
                source_reference=row.source_reference,
                synced_at=row.synced_at,
                editable=editable,
                not_editable_reason=reason,
            )
        )

    by_product_assignments: dict[str, list[InventoryAssignment]] = defaultdict(list)
    for row in assignment_rows:
        by_product_assignments[row.product_id].append(row)

    out_assignments: list[ProductAssignmentGroup] = []
    for pid, rows in sorted(
        by_product_assignments.items(),
        key=lambda item: (
            _product_label(products[item[0]]) if products.get(item[0]) else item[0]
        ),
    ):
        product = products.get(pid)
        lines: list[AssignmentLine] = []
        for row in rows:
            demand_line = db.get(DemandLine, row.demand_line_id)
            well = demand_line.well if demand_line is not None else None
            customer_id = (
                well.planning_node.customer_id
                if well is not None and well.planning_node is not None
                else ""
            )
            editable, reason = _editability(row.source_system)
            lines.append(
                AssignmentLine(
                    row_id=row.id,
                    demand_line_id=row.demand_line_id,
                    well_id=well.id if well else "",
                    well_name=well.name if well else None,
                    customer_id=customer_id,
                    quantity=max(0.0, row.quantity or 0.0),
                    unit_of_measure=(
                        product.unit_of_measure if product else UnitOfMeasure.MTR
                    ),
                    source_system=row.source_system,
                    source_reference=row.source_reference,
                    synced_at=row.synced_at,
                    editable=editable,
                    not_editable_reason=reason,
                )
            )
        lines.sort(key=lambda l: l.demand_line_id)
        out_assignments.append(
            ProductAssignmentGroup(
                product_id=pid,
                product_description=_product_label(product) if product else None,
                unit_of_measure=(
                    product.unit_of_measure if product else UnitOfMeasure.MTR
                ),
                total_quantity=sum(l.quantity for l in lines),
                lines=tuple(lines),
            )
        )

    return CompanyInventoryPosition(
        business_unit_id=business_unit_id,
        business_unit_name=bu.name,
        on_hand=tuple(out_on_hand),
        on_order=tuple(out_on_order),
        assignments=tuple(out_assignments),
    )


# ---------------------------------------------------------------------------
# COVERAGE RECOMPUTATION -- orchestration only; the algorithm lives in
# `app.engines.coverage`
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecomputeFailure:
    customer_id: str
    customer_name: str
    reason: str


@dataclass(frozen=True)
class RecomputeReport:
    recomputed_customer_ids: tuple[str, ...]
    #: {well_id: (before, after)} for every well whose rollup MOVED.
    well_changes: dict[str, tuple[str | None, str | None]]
    failures: tuple[RecomputeFailure, ...] = ()


def _recompute_business_unit(db: Session, business_unit_id: str) -> RecomputeReport:
    """Recompute every customer of `business_unit_id`, one SAVEPOINT each.

    Exactly the pattern `app.engines.substitution_admin.apply_substitution_change`
    already established: a customer whose pass cannot resolve its own inventory is
    rolled back to its prior stored verdicts (never left half-written) and reported
    as a failure rather than aborting the whole write. The only function that
    computes or persists a verdict is `recompute_customer`; this wrapper only
    orchestrates calling it per customer and diffing `Well.coverage_status` before
    and after.
    """
    customers = (
        db.query(Customer).filter(Customer.business_unit_id == business_unit_id).all()
    )
    wells = (
        db.query(Well)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id.in_([c.id for c in customers]))
        .all()
        if customers
        else []
    )
    before = {well.id: well.coverage_status for well in wells}

    recomputed: list[str] = []
    failures: list[RecomputeFailure] = []
    for customer in customers:
        try:
            with db.begin_nested():
                recompute_customer(db, customer)
            recomputed.append(customer.id)
        except (InventoryRowMissing, InventoryScopeMissing) as exc:
            failures.append(
                RecomputeFailure(
                    customer_id=customer.id,
                    customer_name=customer.name,
                    reason=str(exc),
                )
            )

    changes: dict[str, tuple[str | None, str | None]] = {}
    for well in db.query(Well).filter(Well.id.in_(list(before))).all():
        was = before.get(well.id)
        if was != well.coverage_status:
            changes[well.id] = (was, well.coverage_status)

    return RecomputeReport(
        recomputed_customer_ids=tuple(sorted(recomputed)),
        well_changes=changes,
        failures=tuple(sorted(failures, key=lambda f: f.customer_name)),
    )


def _recompute_single_customer(db: Session, customer: Customer) -> RecomputeReport:
    """`_recompute_business_unit`'s narrower sibling, for an assignment edit --
    which belongs to exactly one demand line, hence one well, hence one customer.
    Same savepoint discipline, same failure reporting, so the two write paths give
    a caller the identical shape of report.
    """
    wells = (
        db.query(Well)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id == customer.id)
        .all()
    )
    before = {well.id: well.coverage_status for well in wells}
    failures: list[RecomputeFailure] = []
    recomputed: list[str] = []
    try:
        with db.begin_nested():
            recompute_customer(db, customer)
        recomputed.append(customer.id)
    except (InventoryRowMissing, InventoryScopeMissing) as exc:
        failures.append(
            RecomputeFailure(
                customer_id=customer.id, customer_name=customer.name, reason=str(exc)
            )
        )

    changes: dict[str, tuple[str | None, str | None]] = {}
    for well in db.query(Well).filter(Well.id.in_(list(before))).all():
        was = before.get(well.id)
        if was != well.coverage_status:
            changes[well.id] = (was, well.coverage_status)

    return RecomputeReport(
        recomputed_customer_ids=tuple(recomputed),
        well_changes=changes,
        failures=tuple(failures),
    )


# ---------------------------------------------------------------------------
# WRITE SIDE -- inline single-row edit / create / delete
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteResult:
    """Before/after for one row write, plus the coverage consequences it caused."""

    row_id: str | None
    before: dict
    after: dict | None
    recompute: RecomputeReport


def set_on_hand(
    db: Session, row_id: str, quantity: float
) -> WriteResult:
    """Set the quantity of an existing `InventoryOnHand` row. Refuses a
    non-maintainable row with `NotMaintainable` (-> 403 at the API layer).
    """
    row = db.get(InventoryOnHand, row_id)
    if row is None:
        raise LookupError(f"No InventoryOnHand row {row_id}")
    _require_maintainable("InventoryOnHand", row_id, row.source_system)
    if quantity < 0:
        raise ValueError("On-hand quantity cannot be negative")

    before = {
        "quantity": row.quantity,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    row.quantity = quantity
    row.source_system = "manual"
    row.synced_at = datetime.utcnow()
    db.flush()
    after = {
        "quantity": row.quantity,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    report = _recompute_business_unit(db, row.business_unit_id)
    return WriteResult(row_id=row.id, before=before, after=after, recompute=report)


def create_on_hand(
    db: Session, business_unit_id: str, product_id: str, quantity: float
) -> WriteResult:
    """Create an `InventoryOnHand` row where none exists. A row already existing
    for (business_unit_id, product_id) is a caller error -- use `set_on_hand`.
    """
    if quantity < 0:
        raise ValueError("On-hand quantity cannot be negative")
    existing = (
        db.query(InventoryOnHand)
        .filter(
            InventoryOnHand.business_unit_id == business_unit_id,
            InventoryOnHand.product_id == product_id,
        )
        .one_or_none()
    )
    if existing is not None:
        raise ValueError(
            f"An InventoryOnHand row already exists for this (Business Unit, "
            f"product) pair (id={existing.id}). Use the edit endpoint instead."
        )
    row = InventoryOnHand(
        business_unit_id=business_unit_id,
        product_id=product_id,
        quantity=quantity,
        source_system="manual",
        synced_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    after = {
        "quantity": row.quantity,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    report = _recompute_business_unit(db, business_unit_id)
    return WriteResult(row_id=row.id, before={}, after=after, recompute=report)


def delete_on_hand(db: Session, row_id: str) -> WriteResult:
    row = db.get(InventoryOnHand, row_id)
    if row is None:
        raise LookupError(f"No InventoryOnHand row {row_id}")
    _require_maintainable("InventoryOnHand", row_id, row.source_system)
    before = {
        "quantity": row.quantity,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    business_unit_id = row.business_unit_id
    db.delete(row)
    db.flush()
    report = _recompute_business_unit(db, business_unit_id)
    return WriteResult(row_id=row_id, before=before, after=None, recompute=report)


def set_on_order(
    db: Session,
    row_id: str,
    quantity: float,
    expected_arrival_date: datetime | None = None,
) -> WriteResult:
    row = db.get(InventoryOnOrder, row_id)
    if row is None:
        raise LookupError(f"No InventoryOnOrder row {row_id}")
    _require_maintainable("InventoryOnOrder", row_id, row.source_system)
    if quantity < 0:
        raise ValueError("On-order quantity cannot be negative")

    before = {
        "quantity": row.quantity,
        "expected_arrival_date": row.expected_arrival_date,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    row.quantity = quantity
    row.expected_arrival_date = expected_arrival_date
    row.source_system = "manual"
    row.synced_at = datetime.utcnow()
    db.flush()
    after = {
        "quantity": row.quantity,
        "expected_arrival_date": row.expected_arrival_date,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    # On-order is not an input to any coverage verdict (see
    # `app.models.inventory_on_order.InventoryOnOrder`), so there is nothing for a
    # recompute to change -- but the report shape is still returned, empty, so a
    # caller does not need a different response shape for a channel that happens
    # not to move anything.
    report = RecomputeReport(recomputed_customer_ids=(), well_changes={})
    return WriteResult(row_id=row.id, before=before, after=after, recompute=report)


def create_on_order(
    db: Session,
    business_unit_id: str,
    product_id: str,
    quantity: float,
    expected_arrival_date: datetime | None = None,
) -> WriteResult:
    if quantity < 0:
        raise ValueError("On-order quantity cannot be negative")
    row = InventoryOnOrder(
        business_unit_id=business_unit_id,
        product_id=product_id,
        quantity=quantity,
        expected_arrival_date=expected_arrival_date,
        source_system="manual",
        synced_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    after = {
        "quantity": row.quantity,
        "expected_arrival_date": row.expected_arrival_date,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    report = RecomputeReport(recomputed_customer_ids=(), well_changes={})
    return WriteResult(row_id=row.id, before={}, after=after, recompute=report)


def delete_on_order(db: Session, row_id: str) -> WriteResult:
    row = db.get(InventoryOnOrder, row_id)
    if row is None:
        raise LookupError(f"No InventoryOnOrder row {row_id}")
    _require_maintainable("InventoryOnOrder", row_id, row.source_system)
    before = {
        "quantity": row.quantity,
        "expected_arrival_date": row.expected_arrival_date,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    db.delete(row)
    db.flush()
    report = RecomputeReport(recomputed_customer_ids=(), well_changes={})
    return WriteResult(row_id=row_id, before=before, after=None, recompute=report)


def _assignment_customer(db: Session, demand_line_id: str) -> Customer:
    demand_line = db.get(DemandLine, demand_line_id)
    if demand_line is None:
        raise ValueError(f"No DemandLine {demand_line_id}")
    well = demand_line.well
    if well is None or well.planning_node is None:
        raise ValueError(f"DemandLine {demand_line_id} has no resolvable customer")
    return well.planning_node.customer


def set_assignment(db: Session, row_id: str, quantity: float) -> WriteResult:
    row = db.get(InventoryAssignment, row_id)
    if row is None:
        raise LookupError(f"No InventoryAssignment row {row_id}")
    _require_maintainable("InventoryAssignment", row_id, row.source_system)
    if quantity < 0:
        raise ValueError("Assigned quantity cannot be negative")

    before = {
        "quantity": row.quantity,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    customer = _assignment_customer(db, row.demand_line_id)
    row.quantity = quantity
    row.source_system = "manual"
    row.synced_at = datetime.utcnow()
    db.flush()
    after = {
        "quantity": row.quantity,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    report = _recompute_single_customer(db, customer)
    return WriteResult(row_id=row.id, before=before, after=after, recompute=report)


def create_assignment(
    db: Session, demand_line_id: str, product_id: str, quantity: float
) -> WriteResult:
    if quantity < 0:
        raise ValueError("Assigned quantity cannot be negative")
    customer = _assignment_customer(db, demand_line_id)
    row = InventoryAssignment(
        demand_line_id=demand_line_id,
        product_id=product_id,
        quantity=quantity,
        source_system="manual",
        synced_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    after = {
        "quantity": row.quantity,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    report = _recompute_single_customer(db, customer)
    return WriteResult(row_id=row.id, before={}, after=after, recompute=report)


def delete_assignment(db: Session, row_id: str) -> WriteResult:
    row = db.get(InventoryAssignment, row_id)
    if row is None:
        raise LookupError(f"No InventoryAssignment row {row_id}")
    _require_maintainable("InventoryAssignment", row_id, row.source_system)
    before = {
        "quantity": row.quantity,
        "source_system": row.source_system,
        "synced_at": row.synced_at,
    }
    customer = _assignment_customer(db, row.demand_line_id)
    db.delete(row)
    db.flush()
    report = _recompute_single_customer(db, customer)
    return WriteResult(row_id=row_id, before=before, after=None, recompute=report)


# ---------------------------------------------------------------------------
# TEMPLATE DOWNLOAD + XLSX UPLOAD -- ON-HAND ONLY. See the module docstring for
# why on-order and assignment rows are deliberately excluded from this path.
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = ("product", "on_hand_quantity")
OPTIONAL_COLUMNS = ()

_ALIASES: dict[str, tuple[str, ...]] = {
    "product": (
        "product",
        "productdescription",
        "productid",
        "item",
        "itemdescription",
        "material",
    ),
    "on_hand_quantity": (
        "onhandquantity",
        "onhand",
        "onhandqty",
        "quantity",
        "qty",
    ),
}


def _map_headers(header_cells: tuple) -> dict[str, int]:
    normalised = [_normalise_header(cell) for cell in header_cells]
    found: dict[str, int] = {}
    for canonical, spellings in _ALIASES.items():
        for idx, name in enumerate(normalised):
            if name and name in spellings and canonical not in found:
                found[canonical] = idx
    missing = [c for c in REQUIRED_COLUMNS if c not in found]
    if missing:
        raise CompanyInventoryImportError(
            "Missing required column(s): "
            + ", ".join(missing)
            + f". Required columns are {', '.join(REQUIRED_COLUMNS)}. Header row "
            "read as: " + ", ".join(str(c) for c in header_cells if c is not None)
        )
    return found


def _parse_on_hand_quantity(cell: object) -> tuple[float | None, str | None]:
    """Same rule as `customer_owned_import._parse_owned_quantity`: 0 is a valid,
    meaningful "this BU holds none of it"; negative is refused.
    """
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return None, "on_hand_quantity is empty"
    if isinstance(cell, bool):
        return None, f"on_hand_quantity {cell!r} is not a number"
    if isinstance(cell, (int, float)):
        value = float(cell)
    else:
        cleaned = str(cell).strip().replace(",", "").replace(" ", "")
        try:
            value = float(cleaned)
        except ValueError:
            return None, f"on_hand_quantity {str(cell).strip()!r} is not a number"
    if value != value or value in (float("inf"), float("-inf")):
        return None, "on_hand_quantity is not a finite number"
    if value < 0:
        return None, (
            f"on_hand_quantity cannot be negative, got {value:g}. A Business Unit "
            "cannot hold less than nothing; use 0 to state that none is held."
        )
    return value, None


def _read_rows(file_bytes: bytes) -> tuple[str, list[tuple], dict[str, int]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover -- dependency is pinned
        raise CompanyInventoryImportError(
            "openpyxl is not installed; the server cannot read .xlsx uploads"
        ) from exc

    import io

    try:
        workbook = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise CompanyInventoryImportError(
            "File could not be read as an .xlsx workbook "
            f"({type(exc).__name__}). Save it as Excel Workbook (.xlsx) and retry."
        ) from exc

    try:
        sheet = workbook.worksheets[0] if workbook.worksheets else None
        if sheet is None:
            raise CompanyInventoryImportError("Workbook contains no worksheets")
        rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
        sheet_name = sheet.title
    finally:
        workbook.close()

    while rows and all(cell is None or str(cell).strip() == "" for cell in rows[-1]):
        rows.pop()

    if not rows:
        raise CompanyInventoryImportError(
            "The first worksheet is empty. Expected a header row followed by "
            f"on-hand inventory rows ({', '.join(REQUIRED_COLUMNS)})."
        )

    headers = _map_headers(rows[0])
    return sheet_name, rows[1:], headers


@dataclass(frozen=True)
class RowOutcome:
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
    business_unit_id: str
    filename: str | None = None
    sheet_name: str | None = None
    row_count: int = 0
    created_count: int = 0
    replaced_count: int = 0
    error_count: int = 0
    rows: tuple[RowOutcome, ...] = ()
    recompute: RecomputeReport = field(
        default_factory=lambda: RecomputeReport(recomputed_customer_ids=(), well_changes={})
    )

    @property
    def applied_count(self) -> int:
        return self.created_count + self.replaced_count


def parse_and_replace_on_hand(
    db: Session,
    business_unit: BusinessUnit,
    file_bytes: bytes,
    filename: str | None = None,
) -> UploadResult:
    """Parse `file_bytes` and REPLACE `business_unit`'s on-hand position for every
    product the file names. Products it does NOT name keep their existing row --
    same replace-only-what-is-named semantics as
    `app.engines.customer_owned_import.parse_and_replace`, for the identical reason:
    a partial count of one yard must not silently zero everything else the BU holds.

    A row whose EXISTING `source_system` is not platform-maintainable is refused
    PER ROW (action="Error"), not for the whole batch -- consistent with every other
    row-level refusal in this upload, and with the single-row endpoints' 403, just
    reported the batch way instead of raised.
    """
    sheet_name, data_rows, headers = _read_rows(file_bytes)

    products: dict[str, Product] = {}
    for product in db.query(Product).all():
        products.setdefault(product.id.lower(), product)
    for product in db.query(Product).all():
        if product.description:
            products.setdefault(product.description.strip().lower(), product)

    outcomes: list[RowOutcome] = []
    accepted: dict[str, tuple[int, float]] = {}

    for offset, cells in enumerate(data_rows):
        row_number = offset + 2
        if all(cell is None or str(cell).strip() == "" for cell in cells):
            continue

        def cell_at(name: str) -> object:
            idx = headers.get(name)
            if idx is None or idx >= len(cells):
                return None
            return cells[idx]

        raw_product = _text(cell_at("product"))
        raw_quantity = _text(cell_at("on_hand_quantity"))
        problems: list[str] = []

        product: Product | None = None
        if raw_product is None:
            problems.append("product is empty")
        else:
            product = products.get(raw_product.lower())
            if product is None:
                problems.append(f"unknown product {raw_product!r}")

        quantity, err = _parse_on_hand_quantity(cell_at("on_hand_quantity"))
        if err:
            problems.append(err)

        if not problems and product is not None:
            first = accepted.get(product.id)
            if first is not None:
                problems.append(
                    f"duplicate of row {first[0]} in this file (same product). "
                    "On-hand is single-valued per product; combine the rows and "
                    "re-upload."
                )
            else:
                existing = (
                    db.query(InventoryOnHand)
                    .filter(
                        InventoryOnHand.business_unit_id == business_unit.id,
                        InventoryOnHand.product_id == product.id,
                    )
                    .one_or_none()
                )
                if existing is not None:
                    editable, reason = _editability(existing.source_system)
                    if not editable:
                        problems.append(
                            f"row for {raw_product!r} is sourced from "
                            f"{existing.source_system!r}; {reason}"
                        )

        if problems:
            outcomes.append(
                RowOutcome(
                    row_number=row_number,
                    action="Error",
                    raw_product=raw_product,
                    raw_quantity=raw_quantity,
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
        accepted[product.id] = (row_number, quantity)

    upload = CompanyInventoryUpload(
        business_unit_id=business_unit.id,
        filename=filename,
        sheet_name=sheet_name,
        row_count=len(outcomes) + len(accepted),
        error_count=len(outcomes),
        source_system="manual",
    )
    db.add(upload)
    db.flush()

    existing_rows = {
        row.product_id: row
        for row in db.query(InventoryOnHand).filter(
            InventoryOnHand.business_unit_id == business_unit.id
        )
    }

    created = replaced = 0
    now = datetime.utcnow()
    for product_id, (row_number, quantity) in sorted(
        accepted.items(), key=lambda item: item[1][0]
    ):
        product = db.get(Product, product_id)
        row = existing_rows.get(product_id)
        if row is None:
            db.add(
                InventoryOnHand(
                    business_unit_id=business_unit.id,
                    product_id=product_id,
                    quantity=quantity,
                    source_system="manual",
                    synced_at=now,
                )
            )
            created += 1
            previous = None
            action = "Created"
        else:
            previous = row.quantity
            row.quantity = quantity
            row.source_system = "manual"
            row.synced_at = now
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

    report = _recompute_business_unit(db, business_unit.id)

    return UploadResult(
        upload_id=upload.id,
        business_unit_id=business_unit.id,
        filename=filename,
        sheet_name=sheet_name,
        row_count=upload.row_count,
        created_count=created,
        replaced_count=replaced,
        error_count=len(outcomes) - created - replaced,
        rows=tuple(sorted(outcomes, key=lambda o: o.row_number)),
        recompute=report,
    )


TEMPLATE_COLUMNS = (*REQUIRED_COLUMNS,)


@dataclass(frozen=True)
class OnHandTemplate:
    content: bytes
    filename: str
    row_count: int


def build_on_hand_template(db: Session, business_unit: BusinessUnit) -> OnHandTemplate:
    """An .xlsx of `business_unit`'s CURRENT on-hand position, ready to re-upload.

    NOT a blank template -- mirrors
    `app.engines.customer_owned_import.build_template` exactly: every row is a
    quantity the platform holds right now, and re-uploading the file unmodified
    reports every row "Replaced" with `previous_quantity == quantity`.
    """
    from openpyxl import Workbook

    rows = (
        db.query(InventoryOnHand)
        .filter(InventoryOnHand.business_unit_id == business_unit.id)
        .all()
    )
    products = {p.id: p for p in db.query(Product).all()}

    def _sort_key(row):
        product = products.get(row.product_id)
        return (
            ((product.description if product else None) or row.product_id).lower(),
            row.product_id,
        )

    rows.sort(key=_sort_key)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Company On-Hand Inventory"
    sheet.append(list(TEMPLATE_COLUMNS))

    for row in rows:
        product = products.get(row.product_id)
        product_key = (product.description if product else None) or row.product_id
        sheet.append([product_key, row.quantity])

    notes: list[str] = []
    if rows:
        notes.append(
            "This is NOT a blank template. Every row below the header is the "
            f"on-hand quantity {business_unit.name!r} currently holds, per the "
            "platform's own records."
        )
    else:
        notes.append(
            f"THIS FILE IS EMPTY because {business_unit.name!r} has no on-hand "
            "rows at all yet. Type products and quantities into the rows below "
            "and upload the file to state them for the first time."
        )
    notes.extend(
        [
            "This upload REPLACES the on-hand quantity for the products the file "
            "NAMES, and leaves every other product's row untouched. To declare "
            "that none is held, keep the row and put 0 in on_hand_quantity.",
            "A row whose EXISTING source is an Oracle feed (once connected) will "
            "be refused: this platform never overwrites a row fed by a real "
            "upstream system. Only rows currently marked 'synthetic' or 'manual' "
            "can be edited here.",
            "Uploading this file triggers a coverage recompute for every customer "
            "in this Business Unit -- on-hand quantity is the foundation of every "
            "coverage verdict, so a changed figure can move wells this file never "
            "mentions.",
        ]
    )

    note_sheet = workbook.create_sheet("Notes")
    note_sheet.append(["How to use this file"])
    for note in notes:
        note_sheet.append([note])
    note_sheet.column_dimensions["A"].width = 120

    import io

    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()

    safe_name = (
        "".join(
            ch if (ch.isalnum() or ch in "-_") else "-" for ch in (business_unit.name or "")
        ).strip("-")
        or "business-unit"
    )
    return OnHandTemplate(
        content=buffer.getvalue(),
        filename=f"company-on-hand-{safe_name}.xlsx",
        row_count=len(rows),
    )
