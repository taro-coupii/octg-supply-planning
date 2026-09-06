"""MRP engine -- Phase 2.

MRP here is a RECOMMENDATION process, not an order-writing process. Mill
ordering is the last resort, evaluated strictly after everything cheaper has
been tried:

    Coverage -> Substitution -> Customer Approval -> Mill Order

So this engine never looks at raw demand. It looks at the CoverageResult rows
the coverage engine already wrote, and only considers a demand line for
procurement once that line came out of the coverage/substitution pass genuinely
unresolved -- i.e. UNCOVERED or UNRECOVERABLE. Lines that are COVERED,
COVERED_VIA_SUBSTITUTE or PENDING_APPROVAL produce no recommendation:
respectively the stock exists, the substitute closed it, or the ball is in the
customer's court and ordering steel would pre-empt their decision.

A line with NO CoverageResult at all is a fourth case and is treated as
UNRESOLVED, never as covered. A missing row means the coverage engine has not
evaluated that line yet (a well it never ran for), and unevaluated demand
vanishing from MRP is a fail-silent bug on exactly the path that must fail loud.
Such lines surface as recommendations and their reason says so explicitly.

Recoverability is evaluated LIVE, here, once
--------------------------------------------
Nothing recomputes coverage as the calendar advances -- recompute_customer fires
only on revision and approval decisions -- so a stored UNCOVERED status can be
months old while the line's order-by date has quietly passed. This engine
therefore never uses the stored status as a recoverability discriminator: both
the grouping key and the row's `unrecoverable` verdict come from ONE live
`is_recoverable` evaluation per line, at one moment in time. Mixing a stored
status into the grouping key while recomputing the verdict live is what let
orderable demand be aggregated into an UNRECOVERABLE row and reported as
hopeless.

Demand is charged to the product that SATISFIED it
--------------------------------------------------
`CoverageResult.fulfilled_by_product_id` records which product actually covered
each line. The runout projection and the By Item line list both attribute a line
to that product, falling back to the line's own product when nothing satisfied
it. Substitution decisions are READ from coverage here and never recomputed.

Two output layers, per spec:

  Layer 1  mrp_summary(db, customer_id=None) -> list[MrpRecommendation]
           One row per product needing procurement.

  Layer 2  by_item(db, product_id) -> ByItemAnalysis
           The modern equivalent of the workbook's "By Item" sheet: every
           consuming well and demand line, the inventory position, a monthly
           runout projection, and the recommendation overlaid on that timeline.

All date arithmetic lives in app.engines.order_dates -- read its docstring for
the required_ship_date / recommended_order_date derivation and for how products
with no lead-time components are handled.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
)
from app.engines.inventory import (
    CustomerOwnedPosition,
    InventoryRowMissing,
    _position_from_rows,
    customer_owned_for,
    on_order_rows,
    total_customer_owned_all_customers,
    total_on_hand_all_bus,
    total_on_order_all_bus,
    total_on_order_rows_all_bus,
)
from app.engines.lead_time import LeadTimeBreakdown, od_wt_key, resolve_lead_time
from app.engines.order_dates import (
    is_recoverable,
    months_to_timedelta,
    order_feasibility,
    order_feasibility_with_breakdown,
)
from app.models import (
    BusinessUnit,
    CoverageResult,
    CoverageStatus,
    Customer,
    DemandLine,
    DemandProfile,
    InventoryAssignment,
    InventoryOnHand,
    InventoryOnOrder,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
)

# Re-exported so callers can reach the helper through the engine they are
# already using. The definition lives in order_dates to keep coverage.py's
# import of it cycle-free.
__all__ = [
    "order_feasibility",
    "UNRESOLVED_STATUSES",
    "MrpRecommendation",
    "ByItemAnalysis",
    "ByItemDemandLine",
    "InventoryPosition",
    "RunoutPoint",
    "mrp_summary",
    "by_item",
    "on_order_runout",
    "OnOrderRunout",
    "recommendations_for_lines",
    "total_on_hand_all_bus",
    "total_on_order_all_bus",
]

# The only coverage outcomes that justify recommending a mill order.
UNRESOLVED_STATUSES = (CoverageStatus.UNCOVERED, CoverageStatus.UNRECOVERABLE)

# How far past the last demand month the runout projection keeps reporting a
# flat balance, so a product that never runs out still returns a readable curve.
RUNOUT_TAIL_MONTHS = 3


@dataclass
class MrpRecommendation:
    """Layer 1 row: one product that needs procurement.

    `lead_time` is the ATTRIBUTE BREAKDOWN behind `lead_time_months` -- the OD/WT,
    Grade, Connection and Logistics terms that were summed, each with the
    attribute value it matched on. It rides alongside the scalar on this row (and
    is not a separate lookup) for one reason: `recommended_order_date` is the
    number a planner acts on, and it is only trustworthy if the screen showing it
    can also show why it is that date. Spec Key Discovery #13 -- users trust this
    model BECAUSE the calculation is transparent -- makes the breakdown part of
    the answer, not an optional detail view. It comes from the same
    `resolve_lead_time` call that produced the dates, so it can never explain a
    different number than the one displayed.
    """

    product_id: str
    product_description: str | None
    #: THE NET SHORTFALL: what no stock has been drawn for, summed over the lines
    #: (F04, product-owner ruling 2026-09-06 "net shortfall is the figure, show the
    #: breakdown"). The four figures below are that breakdown; `demand_quantity`
    #: minus the three draws is `quantity`, line by line.
    quantity: float
    #: The unit `quantity` is in -- this product's, since a recommendation row is
    #: always one product's demand (the grouping key is (product, recoverability),
    #: see `_recommendations_for_lines`). Never a mixture, so a single value is
    #: correct rather than merely convenient.
    unit_of_measure: UnitOfMeasure
    ros_date: datetime
    required_ship_date: date
    recommended_order_date: date
    lead_time_months: float
    unrecoverable: bool
    reason: str
    demand_line_ids: list[str] = field(default_factory=list)
    lead_time: LeadTimeBreakdown | None = None
    demand_quantity: float = 0.0
    drawn_customer_owned: float = 0.0
    drawn_company: float = 0.0
    drawn_substitute: float = 0.0
    #: Lines whose stored verdict predates the net columns (or was never
    #: computed): counted at their WHOLE quantity, since nothing is known to have
    #: been drawn. Named so the reason can say so.
    whole_line_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LineNet:
    """One line's net position as MRP consumes it: demand, the three draws, and
    the residual a mill order must cover. Built from `CoverageResult` for the
    stored path and from `app.engines.coverage.LineCoverage` for a preview, so
    both read the ONE allocation that produced the verdict."""

    demand: float
    customer_owned: float
    company: float
    substitute: float
    residual: float
    #: False when the verdict carried no net figures (pre-F04 row or never
    #: evaluated) and the whole line is being treated as residual.
    known: bool = True


def _stored_net(db: Session, line) -> LineNet:
    result = db.get(CoverageResult, line.id)
    if result is None or result.residual is None:
        return LineNet(line.quantity, 0.0, 0.0, 0.0, line.quantity, known=False)
    return LineNet(
        demand=result.demand_quantity if result.demand_quantity is not None else line.quantity,
        customer_owned=result.drawn_customer_owned or 0.0,
        company=result.drawn_company or 0.0,
        substitute=result.drawn_substitute or 0.0,
        residual=result.residual,
    )


@dataclass
class ByItemDemandLine:
    """One demand line charged against the product being analysed.

    `product_id` / `product_description` are the line's OWN product, which is not
    necessarily the product this By Item page is about. Coverage charges a line's
    consumption to whichever product actually satisfied it
    (`CoverageResult.fulfilled_by_product_id`), so a substitute's page
    legitimately lists lines that ordered something else. Without these fields a
    planner reading that page has no way to tell, and the obvious guess -- that
    every row ordered the page's product -- is wrong exactly when it matters.
    """

    demand_line_id: str
    well_id: str
    well_name: str
    #: The owning customer, via the well's planning node. The By Item list mixes
    #: every customer's lines, so a row without this is unattributable.
    customer_name: str | None
    #: Primary / Contingency -- the workbook's "Mtr" vs "Mtr - Cont" split.
    profile: str
    quantity: float
    #: The LINE's own product's unit, matching `product_id`. On a substitute's page
    #: this can differ from the page's own unit, which is the same reason
    #: `product_id` is carried here at all.
    unit_of_measure: UnitOfMeasure
    ros_date: datetime
    coverage_status: str | None
    coverage_reason: str | None
    product_id: str
    product_description: str | None


@dataclass
class InventoryPosition:
    """`on_hand` is the total across EVERY Business Unit, and says so.

    It comes from `app.engines.inventory.total_on_hand_all_bus`, i.e. the sum of
    the `InventoryOnHand` rows for this product. The legacy global
    `Product.on_hand_qty` scalar it used to read has been dropped.

    Summing across BUs is correct HERE and nowhere else. MRP is a system-wide
    procurement view: `mrp_summary(customer_id=None)` and `by_item` aggregate
    demand across every customer of every BU, so the matching supply figure is
    what the company holds in total. It is NOT a coverage figure and must never be
    used as one -- coverage is per BU and goes through `on_hand_for` /
    `on_hand_map`, which is why those are the only functions the coverage engine
    calls. `bu_breakdown` is deliberately absent for now: MRP has no BU filter yet,
    so there is nowhere to show it. Adding a BU filter to the MRP screens means
    replacing this total with a scoped resolution, not decorating it.

    A product with no `InventoryOnHand` row in any BU makes `by_item` raise:
    unknown is not zero, and a runout curve opening at a fabricated 0 is a
    confident wrong number.

    `assigned` is now summed from the InventoryAssignment table -- the local
    projection of Oracle-owned inventory assignments that the HARD/HYBRID
    allocation policies read. It is a carve-out of `on_hand`, not an addition to
    it: `on_hand - assigned` is the unassigned pool.

    `on_order` NOW HAS A LOCAL SOURCE, AND IT IS STILL NOT THE ORACLE FEED
    ---------------------------------------------------------------------
    It was hardcoded 0. That was a placeholder standing in for "we do not know",
    which is exactly the lie-by-omission the rest of this codebase refuses, so it
    is gone: the figure is now resolved from
    `app.models.inventory_on_order.InventoryOnOrder`, the read-only local
    projection of Oracle-owned purchase orders, through
    `app.engines.inventory.total_on_order_all_bus`.

    It is typed `float | None`, and None means UNKNOWN -- no projected row exists
    for this product in any Business Unit. It is never 0 in that case; an explicit
    0 means "measured, and nothing is on order", which is a different fact. See
    `app.engines.inventory.OnOrderPosition`.

    Honesty about provenance is kept in SEPARATE flags rather than one:

      oracle_integrated  Still False, and unchanged in meaning: "the Oracle feed
                         is live". Seeding a projection table does not make an
                         integration exist, so this stays False while
                         `on_order_source` says "synthetic".
      assigned_source    Where the ASSIGNED number came from: "unavailable" when
                         no assignment rows exist, "synthetic" when every
                         contributing row is seeded demo data, "oracle" once real
                         feed rows back it, "mixed" if both.
      on_order_source    The same vocabulary, for the ON-ORDER number, resolved
                         independently. A database can legitimately hold a real
                         assignment feed and synthetic POs, or the reverse.

    Collapsing any of them would force a lie -- flipping oracle_integrated True
    would claim an integration that does not exist, and leaving assigned or
    on_order at 0 would discard data the platform is already acting on.
    """

    #: CUSTOMER-OWNED STOCK IS REPORTED SEPARATELY, AND IT CHANGES WHAT TO ORDER
    #: -------------------------------------------------------------------------
    #: `on_hand` is COMPANY-owned only (`InventoryOnHand`, all BUs). `customer_owned`
    #: is the total customers own of this product
    #: (`app.models.customer_owned_inventory.CustomerOwnedInventory`, summed across
    #: every customer -- see
    #: `app.engines.inventory.total_customer_owned_all_customers` for why an all-
    #: customer sum is right on THIS screen and nowhere else).
    #:
    #: They are kept apart rather than added, and a planner must be shown both,
    #: because the two figures answer different questions and the difference decides
    #: a purchase order:
    #:
    #:   on_hand         steel WE own and can sell to anybody who needs it.
    #:   customer_owned  steel we do NOT own. It will be consumed FIRST against that
    #:                   customer's own demand (see `app.engines.allocation`), so it
    #:                   genuinely reduces what has to be ordered -- which is why the
    #:                   runout projection opens from BOTH -- but it can never be
    #:                   redirected to another customer, and treating it as ours
    #:                   would have us promise it to somebody else.
    #:
    #: `float | None`: None means UNKNOWN (no customer has ever uploaded a position),
    #: 0.0 means measured and no customer owns any. Same rule as `on_order`.
    #: `customer_owned_source` carries the provenance in its own vocabulary --
    #: "customer-upload" | "synthetic" | "mixed" | "declared-none" | "unavailable".
    #: "oracle" is deliberately not among them: Oracle does not hold this data.
    on_hand: float
    #: Labels every quantity on this model. All of them are quantities of the ONE
    #: product being analysed, so one value serves. Note this makes the all-BU sum
    #: in `on_hand` dimensionally safe by construction: it is the same SKU in every
    #: Business Unit, and a SKU has exactly one unit
    #: (`app.models.product.Product`), so the BU sum is a scope decision and never a
    #: units one. The genuinely cross-PRODUCT aggregates are on the Executive
    #: Dashboard, and those refuse to produce a bare scalar -- see
    #: `app.engines.executive.quantity_by_unit`.
    unit_of_measure: UnitOfMeasure = UnitOfMeasure.MTR
    assigned: float = 0.0
    #: Incoming supply. None means UNKNOWN (no projected purchase-order row);
    #: 0.0 means measured and nothing is on order. Read `on_order_source` beside
    #: it -- see the class docstring.
    on_order: float | None = None
    oracle_integrated: bool = False
    assigned_source: str = "unavailable"
    on_order_source: str = "unavailable"
    #: Earliest / latest promised arrival across the contributing rows, so a
    #: planner can see WHEN the incoming quantity lands rather than only that it
    #: exists. Null when nothing is on order, when nothing is known, or when the
    #: rows carry no promise date (a raised-but-unacknowledged PO is real, and is
    #: reported in `on_order_undated` rather than dated to today).
    on_order_earliest_arrival: date | None = None
    on_order_latest_arrival: date | None = None
    on_order_undated: float = 0.0
    #: `on_order` split by booking stage (workbook: PO'ed vs Book'ed).
    #: `on_order_unstated` collects rows whose feed did not state a stage --
    #: reported, never defaulted into either bucket. The three sum to
    #: `on_order` whenever it is known.
    on_order_poed: float = 0.0
    on_order_booked: float = 0.0
    on_order_unstated: float = 0.0
    customer_owned: float | None = None
    customer_owned_source: str = "unavailable"


@dataclass
class RunoutPoint:
    """One month of the projection. Every balance is in `unit_of_measure`.

    The unit rides on each point rather than only on the parent analysis so a chart
    series is self-describing: whoever plots `closing_balance` can label the axis
    from the same rows, without reaching back up. It is the same value on every
    point of a series -- all three figures are quantities of the one product the
    series is about.
    """

    month: str  # "YYYY-MM"
    opening_balance: float
    demand: float
    closing_balance: float
    unit_of_measure: UnitOfMeasure = UnitOfMeasure.MTR
    #: `demand` split by profile: demand_primary + demand_contingency == demand.
    #: Mirrors the workbook By Item tab's "Mtr" / "Mtr - Cont" columns.
    demand_primary: float = 0.0
    demand_contingency: float = 0.0
    #: `closing_balance` split by OWNERSHIP, mirroring the workbook's
    #: "Cust Balance" / "Owned Balance". Customer-owned stock is drawn FIRST
    #: (the platform-wide draw order -- app.engines.allocation), so the
    #: customer balance depletes before the company one. This is an AGGREGATE
    #: approximation: the real allocation consumes each customer's own stock
    #: against their own demand only; a system-wide curve cannot express that
    #: per-customer wall, and says so in `by_item`'s notes rather than
    #: pretending to. closing_customer_owned + closing_company ==
    #: closing_balance while both are >= 0; once the total goes negative the
    #: deficit lives in closing_company.
    closing_customer_owned: float = 0.0
    closing_company: float = 0.0
    #: `opening_balance` split by the same ownership tiers, BEFORE this month's
    #: demand draws. opening_customer_owned + opening_company == opening_balance
    #: while the balance is non-negative (deficits live in the company tier).
    opening_customer_owned: float = 0.0
    opening_company: float = 0.0
    #: Supply arriving at the start of this month, split by CERTAINTY -- the
    #: distinction the 2026-08-12 ledger rework exists to draw. `on_order` is
    #: REAL InventoryOnOrder quantity promised for this month; `recommended` is
    #: HYPOTHETICAL supply (this engine's own recommendation, or a scenario's
    #: what-if order) that nobody has placed. Blending them would present a
    #: suggestion as a promise.
    incoming_on_order: float = 0.0
    incoming_recommended: float = 0.0


@dataclass
class ByItemAnalysis:
    product_id: str
    product_description: str | None
    #: The analysed product's unit, stated once for the whole report. Every quantity
    #: reached directly from here is in it; the per-row units inside `demand_lines`
    #: belong to those lines' own products and may differ.
    unit_of_measure: UnitOfMeasure
    inventory: InventoryPosition
    demand_lines: list[ByItemDemandLine]
    runout: list[RunoutPoint]
    runout_month: str | None
    recommendation: MrpRecommendation | None
    #: SECOND, ADDITIVE series: what the runout looks like if the RECOVERABLE
    #: portion of this product's own recommended order is placed and arrives.
    #: See `by_item` for the arrival-date derivation, the recoverable-only
    #: quantity choice, and why this is a distinct series rather than merged
    #: into `runout`. Empty (identical to `runout`) when this product has no
    #: recoverable recommendation of its own to inject.
    runout_with_recommended_order: list[RunoutPoint] = field(default_factory=list)
    #: The runout month of the series above, mirroring `runout_month`. Equal to
    #: `runout_month` when the injected order does not push the shortfall out
    #: (e.g. it lands after the shortfall already occurred, or there is no
    #: recoverable recommendation to inject), None when the order eliminates
    #: the shortfall inside the projected window entirely.
    runout_month_with_recommended_order: str | None = None
    recommendations: list[MrpRecommendation] = field(default_factory=list)
    #: THE MONTHLY LEDGER (2026-08-12 product-owner rework): the one full-plan
    #: series a planner reads month by month -- opening balance (carried from
    #: last month's close, ownership-split), plus REAL on-order arrivals, plus
    #: the RECOMMENDED order's arrival (reported separately -- a suggestion is
    #: not a promise), minus demand, giving the ownership-split ending balance.
    #: Kept BESIDE the three question-specific curves above, not replacing
    #: them: `runout` still answers "what if nothing arrives", the with-order
    #: series "what does the recommendation alone buy".
    ledger: list[RunoutPoint] = field(default_factory=list)
    #: First month the LEDGER's closing balance goes negative -- i.e. even with
    #: everything on order and the recommendation placed. None = no shortfall.
    ledger_runout_month: str | None = None
    #: Undated on-order quantity that could NOT be placed in any ledger month.
    #: Reported so the ledger's incoming column never silently understates the
    #: order book -- same rule as everywhere else: no month, no netting.
    ledger_undated_on_order: float = 0.0
    # The product's attribute lead-time breakdown, ALWAYS present -- unlike the
    # copy carried on each recommendation, which only exists where there is
    # unresolved demand. By Item is the "explain this item" screen, and a planner
    # asking "how long would this take to order" must get an answer for a fully
    # covered product too (that is exactly when they are deciding whether to lean
    # on stock or order ahead). Same resolver, so it agrees with any
    # recommendation shown beside it.
    lead_time: LeadTimeBreakdown | None = None


def _month_key(value: date | datetime) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _included_lines(
    db: Session, customer_id: str | None = None, business_unit_id: str | None = None
) -> list[DemandLine]:
    """Demand lines that the coverage engine would have evaluated, i.e. passing
    the same status/profile filters, optionally narrowed to one customer."""
    # The status filter selects WELLS (`Well.demand_status`), so the join to
    # `wells` is unconditional now rather than only present when a customer was
    # named. It removes nothing on its own -- `demand_lines.well_id` is a NOT NULL
    # foreign key.
    query = (
        db.query(DemandLine)
        .join(Well, DemandLine.well_id == Well.id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
    )
    if customer_id is not None:
        query = query.filter(PlanningNode.customer_id == customer_id)
    if business_unit_id is not None:
        # A planner's system-wide MRP is their Business Unit's MRP (F01).
        query = query.join(Customer, PlanningNode.customer_id == Customer.id).filter(
            Customer.business_unit_id == business_unit_id
        )
    # The platform's CURRENT scope, resolved per call. MRP must recommend against
    # exactly the demand coverage evaluated -- an MRP built from the shipped
    # constant while coverage ran on an adjusted scope would recommend mill orders
    # for lines nothing had judged, and omit them for lines that were judged short.
    status_filter = effective_status_filter(db)
    profile_filter = effective_profile_filter(db)
    query = query.filter(
        Well.demand_status.in_(sorted(status_filter, key=lambda s: s.value))
    )
    return [line for line in query.all() if line.profile in profile_filter]


def _unresolved(db: Session, lines: list[DemandLine]) -> list[DemandLine]:
    """Lines that coverage left unresolved, INCLUDING never-evaluated lines.

    A missing CoverageResult is deliberately grouped with UNCOVERED /
    UNRECOVERABLE rather than with the covered statuses. It means the coverage
    engine has never run for that line's well, so the honest reading is "not yet
    evaluated" -- which must surface loudly in MRP, not disappear.
    """
    out: list[DemandLine] = []
    for line in lines:
        result = db.get(CoverageResult, line.id)
        if result is None or result.status in UNRESOLVED_STATUSES:
            out.append(line)
    return out


def _unevaluated(db: Session, lines: list[DemandLine]) -> list[DemandLine]:
    """Subset of `lines` with no CoverageResult row at all."""
    return [line for line in lines if db.get(CoverageResult, line.id) is None]


def _recommendation_for(
    db: Session,
    product: Product,
    unresolved: list[DemandLine],
    unrecoverable: bool,
    today: date | None = None,
    net_by_line: dict[str, LineNet] | None = None,
) -> MrpRecommendation:
    """Aggregate one product's unresolved demand into a single recommendation.

    Quantity is the sum of the lines' NET shortfall -- demand less what coverage
    already drew for them (F04); the driving ROS is the EARLIEST of them, because
    one order placed for that date also serves the later ones. `net_by_line`
    supplies the per-line position for a preview; the stored path reads
    `CoverageResult`.

    `unrecoverable` is passed IN, not recomputed: it is the same live verdict
    that put these lines in the same group, so the row's flag, its reason string
    and its grouping can never disagree with each other.
    """
    today = today or date.today()
    nets = {
        line.id: (net_by_line[line.id] if net_by_line is not None else _stored_net(db, line))
        for line in unresolved
    }
    quantity = sum(n.residual for n in nets.values())
    demand_total = sum(n.demand for n in nets.values())
    drawn_owned = sum(n.customer_owned for n in nets.values())
    drawn_company = sum(n.company for n in nets.values())
    drawn_substitute = sum(n.substitute for n in nets.values())
    whole_line_ids = [lid for lid, n in nets.items() if not n.known]
    driving_line = min(unresolved, key=lambda line: line.ros_date)
    ship_by, order_by, lead_months, breakdown = order_feasibility_with_breakdown(
        db, product, driving_line.ros_date
    )

    n = len(unresolved)
    unevaluated = _unevaluated(db, unresolved)
    note = (
        f"; {len(unevaluated)} of them have NO coverage result yet (never "
        "evaluated) -- run coverage for the owning well(s)"
        if unevaluated
        else ""
    )
    stale = [lid for lid in whole_line_ids if lid not in {l.id for l in unevaluated}]
    if stale:
        note += (
            f"; {len(stale)} of them have a verdict with no net figures "
            "(computed before the net-shortfall columns) and are counted whole "
            "-- recompute the owning well(s)"
        )
    drawn = drawn_owned + drawn_company + drawn_substitute
    net_phrase = (
        f"{quantity:g} net shortfall across {n} demand line(s) "
        f"({demand_total:g} demanded, {drawn:g} already drawn from stock)"
    )

    if unrecoverable:
        reason = (
            f"{net_phrase}; ROS "
            f"{driving_line.ros_date.date().isoformat()} is inside the "
            f"{lead_months:g} month lead time -- UNRECOVERABLE by mill order even "
            "if ordered today, escalate (rescope ROS, borrow, or source "
            f"externally){note}"
        )
    elif lead_months <= 0:
        # Not modelled: either nothing matched, or an incomplete set matched and
        # `app.engines.lead_time` refused to total it. Name the missing
        # dimensions -- "configure OD/WT" is actionable, "no lead time" is not.
        missing = ", ".join(breakdown.missing_dimensions) or "any dimension"
        reason = (
            f"{net_phrase}; no "
            f"lead-time components configured for {missing} "
            f"(grade_type '{product.grade_type}', OD/WT '{od_wt_key(product)}', "
            f"connection '{product.connection}') -- lead time is NOT MODELLED "
            f"and the order date is indicative only{note}"
        )
    else:
        reason = (
            f"{net_phrase} after "
            f"coverage and substitution; order by {order_by.isoformat()} to ship "
            f"{ship_by.isoformat()} ({lead_months:g} month lead time){note}"
        )

    return MrpRecommendation(
        product_id=product.id,
        product_description=product.description,
        quantity=quantity,
        unit_of_measure=product.unit_of_measure,
        ros_date=driving_line.ros_date,
        required_ship_date=ship_by,
        recommended_order_date=order_by,
        lead_time_months=lead_months,
        unrecoverable=unrecoverable,
        reason=reason,
        demand_line_ids=[line.id for line in unresolved],
        lead_time=breakdown,
        demand_quantity=demand_total,
        drawn_customer_owned=drawn_owned,
        drawn_company=drawn_company,
        drawn_substitute=drawn_substitute,
        whole_line_ids=whole_line_ids,
    )


def _recommendations_for_lines(
    db: Session,
    unresolved: list[DemandLine],
    today: date | None = None,
    net_by_line: dict[str, LineNet] | None = None,
) -> list[MrpRecommendation]:
    """Group unresolved lines into recommendation rows.

    Grouped by product AND by each line's OWN live recoverability. Without the
    second key an unrecoverable line with an early ROS would drag its product's
    whole row to UNRECOVERABLE and hide the fact that the later demand is still
    perfectly orderable -- two different actions (escalate vs. place the order)
    deserve two rows.

    The second key is the live `is_recoverable` verdict for that line, NOT the
    stored CoverageResult.status. Coverage is only recomputed on revisions and
    approval decisions, so the stored status ages: a line stored as UNCOVERED
    whose order-by date has since passed would be grouped with genuinely
    recoverable demand and then the whole row -- including thousands of tonnes of
    still-orderable steel -- would be reported unrecoverable on the strength of
    the earliest line. Every line now lands in the bucket its own feasibility
    dictates, and that same verdict is what the row reports.
    """
    today = today or date.today()
    grouped: dict[tuple[str, bool], list[DemandLine]] = {}
    for line in unresolved:
        unrec = not is_recoverable(db, line.product, line.ros_date, today=today)
        grouped.setdefault((line.product_id, unrec), []).append(line)

    rows = []
    for (product_id, unrec), lines in grouped.items():
        product = db.get(Product, product_id)
        if product is None:
            continue
        rows.append(
            _recommendation_for(
                db, product, lines, unrecoverable=unrec, today=today,
                net_by_line=net_by_line,
            )
        )

    rows.sort(key=lambda r: (r.recommended_order_date, r.product_id))
    return rows


def recommendations_for_lines(
    db: Session,
    unresolved: list[DemandLine],
    today: date | None = None,
    net_by_line: dict[str, LineNet] | None = None,
) -> list[MrpRecommendation]:
    """Public entry to the grouping above, for callers that have already decided
    which lines are unresolved.

    Exists so `app.engines.scenario.preview` can produce MRP rows for a
    HYPOTHETICAL coverage answer -- one that is not in `CoverageResult` and must
    never be -- without a second copy of the grouping rule or the lead-time
    arithmetic. `lines` may be `app.engines.overrides.LineView` instances; only
    `id`, `product`, `product_id`, `quantity` and `ros_date` are read.

    Reads only. It is the caller's job to have determined unresolvedness by the
    same definition `_unresolved` uses (UNCOVERED / UNRECOVERABLE / no verdict),
    and -- for a hypothetical pass -- to hand the per-line net position in
    `net_by_line`, since the stored `CoverageResult` does not describe it.
    """
    return _recommendations_for_lines(
        db, unresolved, today=today, net_by_line=net_by_line
    )


def mrp_summary(
    db: Session,
    customer_id: str | None = None,
    business_unit_id: str | None = None,
    today: date | None = None,
) -> list[MrpRecommendation]:
    """Layer 1: recommendation rows for every product with unresolved demand.

    Ordered by recommended_order_date so the most urgent action is first.
    """
    unresolved = _unresolved(db, _included_lines(db, customer_id, business_unit_id))
    # A line whose product row is gone cannot be recommended for order; it
    # would AttributeError inside is_recoverable. It still surfaces on the
    # Material Order Requirements grid as an unavailable row with its reason,
    # so dropping it HERE loses nothing the platform reports elsewhere.
    unresolved = [line for line in unresolved if line.product is not None]
    return _recommendations_for_lines(db, unresolved, today=today)


def _runout_series(
    on_hand: float,
    lines: list[DemandLine],
    today: date | None = None,
    unit_of_measure: UnitOfMeasure = UnitOfMeasure.MTR,
    incoming_by_month: dict[str, float] | None = None,
    customer_owned_opening: float = 0.0,
    incoming_on_order_by_month: dict[str, float] | None = None,
) -> tuple[list[RunoutPoint], str | None]:
    """Monthly inventory balance projection.

    `on_hand` is the OPENING BALANCE and is passed in rather than resolved here. Its
    caller (`by_item`) opens it from BOTH ownership tiers -- company on-hand plus
    customer-owned stock -- because both are drawn against the demand this curve
    subtracts. The split is not needed inside the projection: a month's closing
    balance is one quantity of one product, and which tier the last metre came out of
    changes nothing about when the balance goes negative. Where the split DOES matter
    (what to order, and whose steel it is) is `InventoryPosition`, which reports both
    figures separately.

    `lines` must already be the lines CHARGED to this product (see
    `_lines_charged_to`), not merely the lines whose product_id matches it.

    Algorithm: bucket every such demand line by the calendar month of its
    ROS date, then walk months forward from the current month, subtracting that
    month's demand from the running balance. The first month whose CLOSING
    balance is negative is the runout month -- the point where committed demand
    exceeds what is physically on hand. The series always runs at least to the
    last demand month plus RUNOUT_TAIL_MONTHS so a healthy product still shows a
    flat tail rather than an empty chart.

    Demand due in a month already past is charged to the first bucket (the
    current month): it is still an open commitment, and quietly dropping it
    would flatter the curve.

    `unit_of_measure` is stamped on every point purely so the series is
    self-describing (see `RunoutPoint`). It is PASSED IN rather than derived from
    `lines`, and that is deliberate: `lines` are the lines CHARGED to the product,
    which under substitution includes lines whose own product is a different one,
    so their units are not necessarily the unit of the balance being projected. The
    balance is a quantity of the analysed product, so it is the ANALYSED product's
    unit -- which only the caller knows. Deriving it from the lines would silently
    mislabel exactly the substitution case `_lines_charged_to` exists to handle.

    `incoming_by_month` is {"YYYY-MM": qty} of HYPOTHETICAL supply assumed to
    arrive at the START of that month, added to the opening balance before that
    month's demand is subtracted. It exists so `by_item` can project a SECOND,
    additive "if the recommended order is placed" series without a second
    implementation of the month-walk -- see the caller for what quantity and
    which arrival date it passes. `None`/`{}` (the default) reproduces the
    original no-incoming-supply behaviour exactly, which is what the BASELINE
    runout series (the one `by_item.runout` reports) still calls this with: this
    function does not, on its own, account for any REAL InventoryOnOrder either.
    That is a pre-existing gap in the baseline curve, not something this
    parameter's addition changes -- see `by_item` for why it is left as
    documented rather than fixed here.

    `incoming_on_order_by_month` is the same shape for REAL InventoryOnOrder
    arrivals. The two dicts are added identically inside the walk -- the split
    exists so each `RunoutPoint` can report `incoming_on_order` and
    `incoming_recommended` separately (a promise and a suggestion are different
    facts; see the field comments). Callers pass whichever tiers apply.
    """
    today = today or date.today()
    demand_by_month: dict[str, float] = {}
    primary_by_month: dict[str, float] = {}
    for line in lines:
        key = _month_key(max(_month_start(line.ros_date), _month_start(today)))
        demand_by_month[key] = demand_by_month.get(key, 0.0) + line.quantity
        if line.profile == DemandProfile.PRIMARY:
            primary_by_month[key] = primary_by_month.get(key, 0.0) + line.quantity

    incoming_by_month = incoming_by_month or {}
    incoming_on_order_by_month = incoming_on_order_by_month or {}

    # The walk must run at least to the last DEMAND month plus the tail, same as
    # always, but ALSO at least to the last INCOMING month -- an order landing
    # after every demand line's ROS (a real possibility: the recommended date is
    # driven by the EARLIEST unresolved line, see `_recommendation_for`) must
    # still appear on the chart rather than arriving one month past where the
    # series stopped walking.
    last_keys = [
        k
        for k in (*demand_by_month, *incoming_by_month, *incoming_on_order_by_month)
    ]
    last = max(last_keys) if last_keys else _month_key(today)
    year, month = today.year, today.month
    end_year, end_month = int(last[:4]), int(last[5:])
    for _ in range(RUNOUT_TAIL_MONTHS):
        end_year, end_month = _next_month(end_year, end_month)

    series: list[RunoutPoint] = []
    runout_month: str | None = None
    balance = on_hand
    # Ownership split: the customer tier depletes first (draw order), the
    # company tier absorbs the rest -- and any eventual deficit. Incoming
    # (hypothetical or PO) supply is company steel, never customer-owned.
    cust_balance = max(0.0, min(customer_owned_opening, on_hand))
    while (year, month) <= (end_year, end_month):
        key = f"{year:04d}-{month:02d}"
        demand = demand_by_month.get(key, 0.0)
        primary = primary_by_month.get(key, 0.0)
        incoming_recommended = incoming_by_month.get(key, 0.0)
        incoming_on_order = incoming_on_order_by_month.get(key, 0.0)
        opening = balance + incoming_on_order + incoming_recommended
        balance = opening - demand
        # Opening split BEFORE the draw: incoming supply is company steel, so
        # the customer tier carries over from last month's close unchanged.
        opening_cust = cust_balance
        drawn_from_cust = min(cust_balance, demand)
        cust_balance -= drawn_from_cust
        series.append(
            RunoutPoint(
                month=key,
                opening_balance=opening,
                demand=demand,
                closing_balance=balance,
                unit_of_measure=unit_of_measure,
                demand_primary=primary,
                demand_contingency=demand - primary,
                closing_customer_owned=cust_balance,
                closing_company=balance - cust_balance,
                opening_customer_owned=opening_cust,
                opening_company=opening - opening_cust,
                incoming_on_order=incoming_on_order,
                incoming_recommended=incoming_recommended,
            )
        )
        if runout_month is None and balance < 0:
            runout_month = key
        year, month = _next_month(year, month)

    return series, runout_month


def _month_start(value: date | datetime) -> date:
    return date(value.year, value.month, 1)


def _charged_product_id(db: Session, line: DemandLine) -> str:
    """Which product's inventory this line's quantity is drawn from.

    Read straight off the coverage verdict: `fulfilled_by_product_id` when
    something actually satisfied the line, otherwise the line's own product.

    That fallback is the right answer for every unsatisfied outcome. An
    UNCOVERED, UNRECOVERABLE or PENDING_APPROVAL line has drawn nothing, so it
    still represents an open commitment against its OWN product -- which is
    precisely what should push that product's runout curve negative. Only once a
    substitute has genuinely been drawn does the charge move to the substitute.
    """
    result = db.get(CoverageResult, line.id)
    if result is not None and result.fulfilled_by_product_id:
        return result.fulfilled_by_product_id
    return line.product_id


def _lines_charged_to(db: Session, product_id: str) -> list[DemandLine]:
    """Every included demand line whose consumption is charged to `product_id`.

    This is NOT "lines whose product_id matches". A line of product P covered via
    substitute Q consumes Q's stock, so it is charged to Q and excluded from P.
    Bucketing by product_id alone made the substitute look permanently untouched
    (planners then double-spent it) while charging the primary for demand that was
    already covered -- wrong in both directions at once.
    """
    return [
        line
        for line in _included_lines(db)
        if _charged_product_id(db, line) == product_id
    ]


def _inventory_position(
    db: Session,
    product: Product,
    business_unit_id: str | None = None,
    customer_id: str | None = None,
) -> InventoryPosition:
    """Inventory position for `product`.

    Unscoped (the default): on-hand / on-order summed across every BU and
    customer-owned across every customer -- correct for the system-wide MRP
    screens ONLY (see InventoryPosition).

    Scoped (`business_unit_id` / `customer_id` given): the SAME position,
    restricted to one Business Unit's stock and one customer's owned steel.
    This exists for the customer-filtered Material Order Requirements grid:
    netting ONE customer's demand against EVERY BU's stock and OTHER
    customers' owned parcels would cross both of the platform's ownership
    walls, so the filtered grid asks for the scoped figure instead. One
    implementation for both shapes, so they can never drift.
    """
    if business_unit_id is None:
        on_hand = total_on_hand_all_bus(db, product)
    else:
        bu_rows = (
            db.query(InventoryOnHand)
            .filter(
                InventoryOnHand.product_id == product.id,
                InventoryOnHand.business_unit_id == business_unit_id,
            )
            .all()
        )
        if not bu_rows:
            bu = db.get(BusinessUnit, business_unit_id)
            bu_label = bu.name if bu is not None else business_unit_id
            raise InventoryRowMissing(
                f"Product {product.description or product.id} (id={product.id}) "
                f"has no InventoryOnHand row in Business Unit "
                f"{bu_label}, so its on-hand quantity THERE is "
                "UNKNOWN. It is deliberately not reported as 0.",
                product_id=product.id,
            )
        on_hand = max(
            0.0, sum(max(0.0, row.quantity or 0.0) for row in bu_rows)
        )
    assignment_query = db.query(InventoryAssignment).filter(
        InventoryAssignment.product_id == product.id
    )
    if customer_id is not None:
        # Only this customer's own reservations: an assignment to somebody
        # else's line is not part of THIS customer's position.
        assignment_query = (
            assignment_query.join(
                DemandLine, InventoryAssignment.demand_line_id == DemandLine.id
            )
            .join(Well, DemandLine.well_id == Well.id)
            .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .filter(PlanningNode.customer_id == customer_id)
        )
    rows = assignment_query.all()
    assigned = sum(max(0.0, r.quantity or 0.0) for r in rows)
    sources = {(r.source_system or "synthetic") for r in rows}
    if not rows:
        assigned_source = "unavailable"
    elif sources == {"oracle"}:
        assigned_source = "oracle"
    elif "oracle" in sources:
        assigned_source = "mixed"
    else:
        assigned_source = "synthetic"

    # Incoming supply, from the read-only projection of Oracle's purchase orders.
    # `known=False` (no row anywhere) stays None rather than becoming 0 -- see
    # InventoryPosition and app.engines.inventory.OnOrderPosition.
    if business_unit_id is None:
        incoming = total_on_order_all_bus(db, product)
    else:
        incoming = _position_from_rows(
            on_order_rows(db, business_unit_id, {product.id})
        )
    # Customer-owned stock. Never raises and never renders as 0 when unknown -- see
    # the class docstring for why this figure is reported beside `on_hand` rather
    # than inside it.
    if customer_id is None:
        owned = total_customer_owned_all_customers(db, product)
    else:
        customer = db.get(Customer, customer_id)
        if customer is not None:
            owned = customer_owned_for(db, customer, product)
        else:
            # An unknown customer id gets UNKNOWN, never the all-customer
            # total: widening a customer-scoped position to everybody's owned
            # steel would cross the "customer-owned is never shared" wall.
            owned = CustomerOwnedPosition(
                known=False, quantity=None, source_system="unavailable"
            )

    # Booking-stage split of the on-order rows (PO'ed vs Booked vs unstated).
    on_order_query = db.query(InventoryOnOrder).filter(
        InventoryOnOrder.product_id == product.id
    )
    if business_unit_id is not None:
        on_order_query = on_order_query.filter(
            InventoryOnOrder.business_unit_id == business_unit_id
        )
    on_order_rows_all = on_order_query.all()
    poed = sum(
        max(0.0, r.quantity or 0.0)
        for r in on_order_rows_all
        if r.booking_status == "PO"
    )
    booked = sum(
        max(0.0, r.quantity or 0.0)
        for r in on_order_rows_all
        if r.booking_status == "Booked"
    )
    unstated = sum(
        max(0.0, r.quantity or 0.0)
        for r in on_order_rows_all
        if r.booking_status not in ("PO", "Booked")
    )

    return InventoryPosition(
        on_hand=on_hand,
        unit_of_measure=product.unit_of_measure,
        assigned=assigned,
        assigned_source=assigned_source,
        on_order=incoming.quantity,
        on_order_source=incoming.source_system,
        on_order_earliest_arrival=(
            incoming.earliest_expected_arrival.date()
            if incoming.earliest_expected_arrival is not None
            else None
        ),
        on_order_latest_arrival=(
            incoming.latest_expected_arrival.date()
            if incoming.latest_expected_arrival is not None
            else None
        ),
        on_order_undated=incoming.undated_quantity,
        on_order_poed=poed,
        on_order_booked=booked,
        on_order_unstated=unstated,
        customer_owned=owned.quantity,
        customer_owned_source=owned.source_system,
        # UNCHANGED, and deliberately: a seeded projection is not a live feed.
        oracle_integrated=False,
    )


def by_item(db: Session, product_id: str, today: date | None = None) -> ByItemAnalysis:
    """Layer 2: full per-product justification for `product_id`.

    Raises ValueError if the product does not exist.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise ValueError(f"Product {product_id} not found")

    # Resolved BEFORE anything else is computed, so a product whose on-hand
    # quantity is unknown fails the whole call rather than returning a report with
    # one silently-fabricated number in it.
    position = _inventory_position(db, product)

    lines = _lines_charged_to(db, product_id)
    lines.sort(key=lambda l: l.ros_date)

    out_lines = []
    for line in lines:
        result = db.get(CoverageResult, line.id)
        out_lines.append(
            ByItemDemandLine(
                demand_line_id=line.id,
                well_id=line.well_id,
                well_name=line.well.name,
                customer_name=(
                    line.well.planning_node.customer.name
                    if line.well.planning_node is not None
                    and line.well.planning_node.customer is not None
                    else None
                ),
                profile=line.profile.value,
                quantity=line.quantity,
                ros_date=line.ros_date,
                coverage_status=result.status.value if result is not None else None,
                coverage_reason=result.reason if result is not None else None,
                product_id=line.product_id,
                product_description=line.product.description,
                unit_of_measure=line.product.unit_of_measure,
            )
        )

    series, runout_month = _runout_series(
        # BOTH ownership tiers. The runout curve answers "when does committed demand
        # exceed the steel that will be drawn against it", and customer-owned stock IS
        # drawn -- first, ahead of company stock (see `app.engines.allocation`). A
        # curve opening from company stock alone would run out earlier than reality
        # and recommend ordering material the customer already owns.
        #
        # `customer_owned` is None when UNKNOWN (nobody has uploaded anything), and
        # None contributes nothing here. That is not the same claim as "no customer
        # owns any" -- the claim is made where it belongs, on
        # `InventoryPosition.customer_owned` / `customer_owned_source`, which the
        # screen renders as unavailable. A projection has to open from SOME number,
        # and the only defensible one is the steel actually known to exist.
        position.on_hand + (position.customer_owned or 0.0),
        lines,
        today=today,
        unit_of_measure=product.unit_of_measure,
        customer_owned_opening=position.customer_owned or 0.0,
    )

    # `recommendation` is the most urgent row (earliest order date) so the UI
    # has one headline to overlay on the runout timeline; `recommendations`
    # carries every row, since a product can need both an escalation for its
    # unrecoverable demand and a normal order for the rest.
    recommendations = _recommendations_for_lines(db, _unresolved(db, lines), today=today)

    # ---- "if the recommended order is placed" series -----------------------
    #
    # WHICH RECOMMENDATIONS: only rows for THIS product (product_id ==
    # product.id) -- `recommendations` here can also contain rows for a
    # DIFFERENT product when a substituted line's own product still shows up
    # in `_unresolved` (see `_recommendations_for_lines`, which groups by each
    # line's OWN product, not the product this By Item page is about). Ordering
    # somebody else's product is not "this item's recommended order arriving".
    #
    # WHICH QUANTITY: only the RECOVERABLE rows (`unrecoverable=False`).
    # Injecting the UNRECOVERABLE portion would assume a mill order for demand
    # the engine has already determined cannot land by its ROS even if placed
    # today -- i.e. the projection would show steel arriving to save a
    # shortfall the same engine calls impossible to save. That is a LESS
    # HONEST claim than simply not projecting it, so it is left out; the
    # unrecoverable quantity still shows up as unmet demand on both series,
    # which is the truthful picture.
    #
    # ARRIVAL DATE: `recommended_order_date` (when to place the order) PLUS the
    # SAME product's total lead time (when it then arrives) -- reusing
    # `resolve_lead_time` rather than a second calculation, exactly as
    # `order_dates.order_feasibility` does internally. Each recommendation row
    # already carries `lead_time_months` from that same resolution (see
    # `_recommendation_for`), so no second lookup happens here.
    #
    # ADDITIVITY / real InventoryOnOrder: `_runout_series` (the function
    # `series` above and this one both call) does NOT fold in real
    # `InventoryOnOrder` rows today -- that is a PRE-EXISTING GAP in the
    # baseline curve, not introduced here. Fixing it would mean resolving
    # `on_order_rows` inside `_runout_series` and is left OUT OF SCOPE for this
    # change: `runout`/`runout_month` are read by
    # `tests/test_seed_honesty_paths.py`'s pinned rollup and by every existing
    # MRP test, and folding in real on-order rows would shift those baseline
    # numbers as a side effect of an unrelated feature. The two series stay
    # visibly SEPARATE fields (never merged into one) for the same reason
    # `InventoryPosition.on_order` is reported beside `on_hand` rather than
    # inside it: a hypothetical assumption must never be indistinguishable
    # from a measured fact.
    incoming_by_month: dict[str, float] = {}
    for rec in recommendations:
        if rec.product_id != product.id or rec.unrecoverable:
            continue
        arrival_date = rec.recommended_order_date + months_to_timedelta(
            rec.lead_time_months
        )
        key = _month_key(arrival_date)
        incoming_by_month[key] = incoming_by_month.get(key, 0.0) + rec.quantity

    series_with_order, runout_month_with_order = _runout_series(
        position.on_hand + (position.customer_owned or 0.0),
        lines,
        today=today,
        unit_of_measure=product.unit_of_measure,
        incoming_by_month=incoming_by_month,
        # Same ownership split as the baseline series above: without it the
        # with-order curve treated the whole opening as company steel and its
        # customer/company decomposition disagreed with the baseline's.
        customer_owned_opening=position.customer_owned or 0.0,
    )

    # THE MONTHLY LEDGER -- the full plan in one walk: real dated on-order
    # arrivals AND the recommended order's arrival, each reported in its own
    # incoming column (promise vs suggestion). Undated on-order quantity has no
    # month to land in and is reported beside the ledger instead of netted --
    # the same rule the MOR grid and InventoryPosition apply.
    on_order_by_month: dict[str, float] = {}
    undated_on_order = 0.0
    for on_order_row in total_on_order_rows_all_bus(db, product):
        if on_order_row.expected_arrival_date is None:
            undated_on_order += on_order_row.quantity
            continue
        arrival_key = _month_key(_as_date(on_order_row.expected_arrival_date))
        on_order_by_month[arrival_key] = (
            on_order_by_month.get(arrival_key, 0.0) + on_order_row.quantity
        )
    ledger, ledger_runout_month = _runout_series(
        position.on_hand + (position.customer_owned or 0.0),
        lines,
        today=today,
        unit_of_measure=product.unit_of_measure,
        incoming_by_month=incoming_by_month,
        customer_owned_opening=position.customer_owned or 0.0,
        incoming_on_order_by_month=on_order_by_month,
    )

    return ByItemAnalysis(
        product_id=product.id,
        product_description=product.description,
        unit_of_measure=product.unit_of_measure,
        inventory=position,
        demand_lines=out_lines,
        runout=series,
        runout_month=runout_month,
        runout_with_recommended_order=series_with_order,
        runout_month_with_recommended_order=runout_month_with_order,
        recommendation=recommendations[0] if recommendations else None,
        recommendations=recommendations,
        lead_time=resolve_lead_time(db, product),
        ledger=ledger,
        ledger_runout_month=ledger_runout_month,
        ledger_undated_on_order=undated_on_order,
    )


@dataclass(frozen=True)
class OnOrderRunout:
    """The runout curve WITH real incoming supply folded in, for ONE product.

    A THIRD series, beside `ByItemAnalysis.runout` (on-hand only) and
    `runout_with_recommended_order` (on-hand plus a hypothetical order this engine
    recommends). This one opens from the same on-hand balance and adds the
    material that is ALREADY ON ORDER, at the dates it is expected to land.

    WHY IT IS A SEPARATE SERIES AND NOT A FIX TO `runout`
    ----------------------------------------------------
    `by_item` documents the absence of on-order from the baseline curve as a
    deliberate, pinned property, and it stays that way: `runout`/`runout_month`
    are read by the MRP screen and pinned by `tests/test_seed_honesty_paths.py`,
    and the platform's stated policy (`app.engines.executive.ON_ORDER_NOTE`) is
    that on-order is read BESIDE coverage figures, never inside them. Folding it
    into the measured curve would break that policy and move pinned baselines.

    So this is an explicitly-labelled projection that a caller must ask for. Its
    only consumer today is `app.engines.scenario.preview`, which computes it TWICE
    -- once with the real arrival dates, once with a scenario's PO_ARRIVAL
    override applied -- and reports the difference. Because BOTH sides of that
    diff include on-order, the delta is purely the effect of moving the date, and
    no number the official screens show is disturbed.

    NOT A COVERAGE FIGURE. `runout_month` here is a projection of a monthly
    balance; it does not and must not move a Covered/Uncovered verdict.
    """

    product_id: str
    unit_of_measure: UnitOfMeasure
    runout: list[RunoutPoint]
    runout_month: str | None
    #: {"YYYY-MM": qty} actually folded in, so a caller can show its working.
    incoming_by_month: dict[str, float]
    #: Rows whose `expected_arrival_date` is null. Counted in `on_order_total` but
    #: in no month, because an unacknowledged PO has no date to project it at --
    #: same rule as `InventoryPosition.on_order_undated`.
    undated_quantity: float
    on_order_total: float
    #: Earliest expected arrival among DATED rows, after any shift. None when every
    #: row is undated or there are no rows.
    earliest_arrival: date | None
    #: Total HYPOTHETICAL quantity folded in -- purchase orders that DO NOT EXIST,
    #: asserted by a `PO_ARRIVAL.new_order` scenario override. 0.0 on every call that
    #: passed no `hypothetical_orders`, which is every caller outside a scenario
    #: preview.
    #:
    #: REPORTED SEPARATELY FROM `on_order_total` ON PURPOSE. `on_order_total` is the
    #: sum of real `InventoryOnOrder` rows and stays exactly that -- a figure a
    #: planner can go and check in Oracle. Adding an invented quantity into it would
    #: make the two indistinguishable in the one field most likely to be quoted as
    #: fact. `incoming_by_month` DOES contain both, because that is the projection
    #: being asked for, and `hypothetical_by_month` says which part is invented.
    hypothetical_total: float = 0.0
    #: {"YYYY-MM": qty} of the hypothetical part alone, so a caller can show which
    #: months of `incoming_by_month` are hypothesis rather than promise.
    hypothetical_by_month: dict[str, float] = field(default_factory=dict)


def on_order_runout(
    db: Session,
    product_id: str,
    arrival_override: date | None = None,
    today: date | None = None,
    hypothetical_orders: Sequence[tuple[float, date | datetime]] | None = None,
) -> OnOrderRunout:
    """`by_item`'s runout curve, with real `InventoryOnOrder` folded in.

    `arrival_override` restates the EARLIEST expected arrival date of this
    product's on-order rows. Every OTHER dated row shifts by the SAME offset, so
    the delivery SCHEDULE keeps its shape.

    Why a shift rather than "set every row to this date": several rows for one
    (BU, product) pair are normal and correct -- that is what a delivery schedule
    is (see `app.models.inventory_on_order.InventoryOnOrder`). Collapsing three
    POs landing months apart onto a single date would answer a question nobody
    asked and would overstate the supply available in that month threefold. A
    uniform shift is the honest reading of "the mill pulled our deliveries in by
    six weeks", and in the ordinary single-row case it degenerates to exactly
    "move that PO to this date".

    UNDATED rows are NOT shifted onto a date. A PO with no promise date has no
    schedule position to move, so inventing one from the override would fabricate
    supply in a month; it stays out of `incoming_by_month` and is reported in
    `undated_quantity`.

    `hypothetical_orders` is `((quantity, expected_arrival), ...)` of purchase orders
    that DO NOT EXIST -- what a `PO_ARRIVAL.new_order` scenario override asserts.
    Each one is added to `incoming_by_month` at its own month, ADDITIVELY BESIDE the
    real rows rather than replacing them, so a product with three real POs and one
    hypothetical order projects all four. It is a projection input and nothing else:
    no `InventoryOnOrder` row is created, `on_order_total` still reports only the
    real rows, and the hypothetical part is reported separately in
    `hypothetical_total` / `hypothetical_by_month`.

    The two parameters are INDEPENDENT and compose. `arrival_override` shifts the
    real schedule; `hypothetical_orders` adds supply that was never promised. Passing
    both models "the mill pulled our deliveries in AND we placed an emergency order",
    which is one of the questions worth asking. A hypothetical order is NOT shifted
    by `arrival_override`: it has no promised date to be early or late against, and
    its date is already the planner's own choice, so shifting it would move a date
    the planner had just set.

    Reuses `_runout_series`, `_inventory_position` and `_lines_charged_to`
    verbatim -- there is no second month-walk here, which is the whole point of
    `_runout_series` taking `incoming_by_month`. The hypothetical quantity travels
    through that SAME hook, so there is no separate "with a new order" projection to
    drift from this one -- exactly as `by_item` already injects its
    `runout_with_recommended_order` supply through the same argument.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise ValueError(f"Product {product_id} not found")

    position = _inventory_position(db, product)
    lines = _lines_charged_to(db, product_id)

    rows = total_on_order_rows_all_bus(db, product)
    dated = [r for r in rows if r.expected_arrival_date is not None]
    undated_quantity = sum(r.quantity for r in rows if r.expected_arrival_date is None)

    # The shift is computed from the earliest DATED row, which is the same row the
    # Executive Dashboard and `InventoryPosition.on_order_earliest_arrival` call
    # the earliest arrival -- so the date a planner sees on screen is the date the
    # override restates, and the gesture means what the picture showed.
    base_earliest = (
        min(_as_date(r.expected_arrival_date) for r in dated) if dated else None
    )
    shift_days = 0
    if arrival_override is not None and base_earliest is not None:
        shift_days = (_as_date(arrival_override) - base_earliest).days

    incoming_by_month: dict[str, float] = {}
    arrivals: list[date] = []
    for row in dated:
        arrival = _as_date(row.expected_arrival_date) + timedelta(days=shift_days)
        arrivals.append(arrival)
        key = _month_key(arrival)
        incoming_by_month[key] = incoming_by_month.get(key, 0.0) + row.quantity

    # The hypothetical part, accumulated into the SAME dict the real rows landed in
    # -- that additivity is the whole behaviour, and doing it here rather than in a
    # second series keeps one month-walk.
    hypothetical_by_month: dict[str, float] = {}
    for quantity, arrival in hypothetical_orders or ():
        key = _month_key(_as_date(arrival))
        hypothetical_by_month[key] = hypothetical_by_month.get(key, 0.0) + quantity
        incoming_by_month[key] = incoming_by_month.get(key, 0.0) + quantity

    # Real rows and hypothetical orders travel through the walk in their own
    # tiers, so every point's incoming_on_order / incoming_recommended split is
    # honest here too. `incoming_by_month` (the merged dict) is still what this
    # function REPORTS -- its callers read the total.
    real_by_month = {
        key: qty - hypothetical_by_month.get(key, 0.0)
        for key, qty in incoming_by_month.items()
    }
    series, runout_month = _runout_series(
        position.on_hand + (position.customer_owned or 0.0),
        lines,
        today=today,
        unit_of_measure=product.unit_of_measure,
        incoming_by_month=hypothetical_by_month,
        # Same ownership split as the baseline runout (see by_item).
        customer_owned_opening=position.customer_owned or 0.0,
        incoming_on_order_by_month=real_by_month,
    )

    return OnOrderRunout(
        product_id=product.id,
        unit_of_measure=product.unit_of_measure,
        runout=series,
        runout_month=runout_month,
        incoming_by_month=incoming_by_month,
        undated_quantity=undated_quantity,
        on_order_total=sum(r.quantity for r in rows),
        # REAL dated arrivals only. A hypothetical order is not an "expected
        # arrival" -- nobody expects it -- and letting one become the earliest
        # arrival would put an invented date in the field the Executive Dashboard
        # and `InventoryPosition.on_order_earliest_arrival` call Oracle's promise.
        # It is also what `_supply_runout_changes` diffs to report `shift_days`, so
        # a hypothetical order must leave it alone or it would read as a shift.
        earliest_arrival=min(arrivals) if arrivals else None,
        hypothetical_total=sum(hypothetical_by_month.values()),
        hypothetical_by_month=hypothetical_by_month,
    )


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value
