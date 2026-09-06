"""Executive Dashboard aggregates.

This screen goes to management, so the governing rule here is that a number is
either MEASURED or explicitly UNAVAILABLE. There is no third category. A bare 0
reads as "we measured zero", which for a figure we cannot compute at all is a
fabrication -- so every block below carries an `available` flag and, when false, a
`reason` the UI can show instead of a value.

SCOPE: BUSINESS UNIT and/or CUSTOMER
-----------------------------------
Both filters are optional and either may be supplied. The BU is the platform's
HARD inventory boundary (`app.models.business_unit.BusinessUnit`), so a BU-scoped
dashboard is the natural management view: it is the smallest scope inside which
the supply figures actually mean something, because inventory is held per (BU,
product) and is never shared across BUs.

Supplying a customer that does NOT belong to the named BU is a CONTRADICTION and
`resolve_scope` raises `ScopeContradiction`, which the route turns into a 400. It
is deliberately not an empty result. An empty result renders as "this scope has
no demand and no risk", which is a measured claim about a scope that cannot
exist -- precisely the fabricated zero this whole module was written to refuse.
A 400 says the request was impossible, which is the true statement, and it is the
only one of the two a caller can act on.

Six blocks:

  demand trend        total demand over the next 3 / 6 / 12 / 24 months, each
                      against the PREVIOUS same-length horizon. See
                      `_previous_horizon` -- the prior figure is genuinely
                      reconstructed from `DemandRevision` history or reported
                      unavailable. It is never estimated.
  coverage            coverage by DEMAND QUANTITY (the primary measure, on the
                      product owner's ruling), a per-status quantity snapshot,
                      and the well counts kept as reference.
  supply risk         demand not coverable even if ordered today. That is exactly
                      `CoverageStatus.UNRECOVERABLE`, which the coverage engine
                      has already determined (see
                      app.engines.order_dates.is_recoverable); this module READS
                      that verdict and never re-derives it.
  soft allocation
  coverage            how in-scope demand was actually satisfied, in quantity
                      terms: from the shared pool, from an Oracle assignment, via
                      a substitute, or not at all. Over a 12 / 18 / 24 / 36 month
                      horizon. REPLACES the old "allocation" block -- see below.
  first runout        the wells that first fall short, with customer, well, date
                      and shortfall quantity.
  incoming supply     material already on order, from the read-only projection of
                      Oracle purchase orders, with its expected-arrival horizon.

WHY "allocation" BECAME "soft allocation coverage"
-------------------------------------------------
The old block reported allocated-vs-unallocated read from `InventoryAssignment`,
i.e. Oracle's HARD assignment. The product owner reversed that: "here i like to
show soft allocation coverage not hard allocation from oracle. i was confusing so
apologies." The block now reports what THIS PLATFORM'S OWN allocation achieved,
which is the question a manager is actually asking, and it is renamed so it
cannot be mistaken for Oracle's.

Crucially it does NOT re-derive allocation. The figures come from
`app.engines.coverage.compute_customer_coverage` -- the read-only computing half
of the single coverage implementation -- through
`CustomerCoverage.consumed_from_pool` / `.consumed_from_assignment`, which are
`app.engines.allocation.AllocationOutcome`'s own maps carried out of the pass. A
second implementation of the allocation rules would drift, and it would drift on
the one screen with no downstream consumer to notice.

What is NOT computable, and why
-------------------------------
`InventoryAssignment` and `InventoryOnOrder` are local PROJECTIONS of an
Oracle-owned domain, and absence in a projection is silence rather than zero:

  * `oracle_integrated` remains False everywhere. The Oracle feed is not live;
    seeding a projection table does not make an integration exist. The two facts
    are carried in independent flags (`source` / `oracle_integrated`) for exactly
    the reason `app.engines.mrp.InventoryPosition.assigned_source` is separate
    from it.
  * A product with NO `InventoryOnOrder` row has an UNKNOWN on-order quantity,
    not a zero one, and the incoming-supply block says which of the two it is
    holding. "Nothing on order" is stated by an explicit `quantity = 0` row.

UNITS: the only place in the platform that genuinely aggregates ACROSS PRODUCTS
------------------------------------------------------------------------------
Every other quantity-bearing payload concerns one product, so one unit labels it
(`app.engines.mrp.MrpRecommendation`, `app.engines.surplus.ProductSurplus`). This
module is the exception: demand trend, quantity coverage, supply risk, the
soft-allocation channels and incoming supply all sum quantities over whatever mix
of products happens to be in scope. Since a
quantity is only meaningful inside one dimensional system (see
`app.models.product.UnitOfMeasure`), such a sum is arithmetic on unlike things
the moment two units are present -- adding metres of casing to tonnes of
accessories produces a number with no referent at all.

Reporting one anyway would be the same failure this module was written to avoid,
merely dimensional instead of statistical: a bare 0 asserts a measurement that was
never made, and a bare "1 250 000" across two units asserts a total that cannot
exist. So the rule is the one `quantity_by_unit` implements:

  * `quantities_by_unit` is ALWAYS correct and is always populated. One entry per
    unit actually present, each entry summing only its own unit. The entries are
    never added together, and there is deliberately no conversion factor anywhere
    in this platform to add them with.
  * the scalar total is accompanied by `unit_of_measure`, which is non-null EXACTLY
    when every contributing product shares one unit -- i.e. exactly when the scalar
    is safe to render. When it is null the scalar must not be shown, `notes` says
    so, and the breakdown is the answer.
  * every RATIO built from such totals (`change_pct`, `coverage_pct`,
    `unrecoverable_pct_of_in_scope_demand`, each channel's `pct`) is reported
    unavailable in the mixed case rather than computed. A percentage of two
    mixed-unit sums is not a weaker number, it is not a number.

The scalar is kept rather than deleted because the single-unit case is the normal
one -- the whole seeded demo is in metres -- and in that case it is exactly right.
What has been removed is the possibility of it being wrong SILENTLY.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session, object_session

from app.engines.company_inventory import get_position
from app.engines.coverage import (
    CustomerCoverage,
    compute_customer_coverage,
)
from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
)
from app.engines.inventory import (
    InventoryScopeMissing,
    customer_owned_map,
    on_order_rows,
)
from app.engines.units import to_metric_tonnes
from app.engines.well_dates import SATISFIED_STATUSES, well_dates
from app.models import (
    BusinessUnit,
    CoverageResult,
    CoverageStatus,
    Customer,
    DemandLine,
    DemandProfile,
    DemandRevision,
    DemandStatus,
    InventoryAssignment,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
)

_log = logging.getLogger(__name__)

DEMAND_TREND_HORIZONS = (3, 6, 12, 24)
#: The soft-allocation-coverage horizon selector, kept exactly as the old
#: allocation block had it -- it is wired into the UI and is genuinely useful on
#: the replacement block. The name stays `ALLOCATION_HORIZONS` because the query
#: parameter it validates (`allocation_horizon_months`) stays too; renaming the
#: wire format would break the screen for no gain.
ALLOCATION_HORIZONS = (12, 18, 24, 36)
#: Cumulative "arriving within N months" windows for incoming supply. The SAME
#: lengths as `DEMAND_TREND_HORIZONS`, deliberately: the only useful thing to do
#: with an incoming-supply figure is compare it against the demand for the same
#: window, and two different sets of windows would make that comparison
#: impossible to state.
ON_ORDER_ARRIVAL_HORIZONS = DEMAND_TREND_HORIZONS

#: How many wells the first-runout detail returns. A management screen wants the
#: urgent ones; the seeded demo alone has 22 wells and a real book has thousands.
#: The cap and the number omitted are BOTH reported in the payload, and the
#: omitted count is logged, so nothing is silently truncated.
FIRST_RUNOUT_WELL_CAP = 10

#: The per-status buckets of the quantity coverage snapshot, in a FIXED order so
#: the breakdown is stable between requests (a reordering breakdown looks like
#: changing data -- the same reason `quantity_by_unit` sorts).
#:
#: `NOT_EVALUATED` is not a `CoverageStatus`. It is the in-scope line with NO
#: CoverageResult row, which means the engine has not evaluated it -- and which
#: must never be folded into Uncovered (that would assert a verdict) nor dropped
#: (that would stop the breakdown summing to the total). Same convention as
#: `app.engines.mrp._unresolved` and `app.engines.well_dates`.
NOT_EVALUATED = "NotEvaluated"
COVERAGE_QUANTITY_STATUSES = (
    CoverageStatus.COVERED.value,
    CoverageStatus.COVERED_VIA_SUBSTITUTE.value,
    CoverageStatus.PENDING_APPROVAL.value,
    CoverageStatus.UNCOVERED.value,
    CoverageStatus.UNRECOVERABLE.value,
    NOT_EVALUATED,
)
#: Which buckets count towards COVERED quantity. Mirrors
#: `app.engines.coverage_view.COVERED_LINE_STATUSES` and
#: `app.engines.well_dates.SATISFIED_STATUSES` -- the same two statuses, named
#: here because this is the numerator of a percentage rather than a per-line
#: question, and the two could legitimately diverge later.
COVERED_QUANTITY_STATUSES = (
    CoverageStatus.COVERED.value,
    CoverageStatus.COVERED_VIA_SUBSTITUTE.value,
)

MIXED_UNITS_NOTE = (
    "One or more totals on this dashboard span products measured in DIFFERENT "
    "units of measure, so no single scalar total exists for them. Read "
    "`quantities_by_unit` (one entry per unit) instead of the scalar, and do not "
    "add the entries together -- this platform holds no conversion factor between "
    "units and deliberately will not invent one. Percentage changes over such "
    "totals are reported unavailable for the same reason."
)

ON_ORDER_NOTE = (
    "Incoming supply (on-order quantity) is Oracle-owned and this platform holds "
    "only a read-only projection of it. The Oracle feed is still NOT integrated "
    "(oracle_integrated=false), so every on-order figure here is seeded demo data "
    "unless its source says 'oracle'. No coverage, supply-risk or soft-allocation "
    "figure on this dashboard nets material already on order against demand -- "
    "coverage is decided from on-hand stock alone. Read the incoming_supply block "
    "beside them, never inside them. A product with no projected purchase-order "
    "row has an UNKNOWN on-order quantity, which is reported as unavailable and is "
    "not the same as a measured zero."
)

SOFT_ALLOCATION_NOTE = (
    "SOFT ALLOCATION COVERAGE -- what this platform's own allocation achieved. It "
    "is NOT Oracle's hard allocation and must not be read as one. 'From shared "
    "pool' is quantity drawn from the Business Unit's UNASSIGNED stock, which any "
    "customer in the Business Unit may reach; 'from assignment' is quantity drawn "
    "from an Oracle assignment reserved to this customer -- the line's own "
    "assignment for a HARD or HYBRID customer, or, for a SOFT customer, its own "
    "assignments pooled across its own wells (soft allocation does not tie them "
    "to specific wells, but they are still that customer's reserved steel and no "
    "neighbour may draw them); 'via substitute' is quantity satisfied by a "
    "technically approved alternative product. What it does NOT "
    "mean: it is not a reservation. This platform creates no hard reservation of "
    "any kind, so a quantity shown as drawn from the pool is not held for that "
    "line and can be drawn by earlier demand on the next recompute. The figures "
    "come from the same single coverage pass that produced the coverage verdicts, "
    "so the two blocks can never disagree."
)

INVENTORY_UTILISATION_NOTE = (
    "INVENTORY UTILISATION -- of company-owned steel already standing in the "
    "yard, how much is tied to in-window demand and how much is idle. The "
    "DENOMINATOR here is INVENTORY, not demand -- the OPPOSITE of "
    "soft_allocation_coverage, whose denominator is demand quantity. Read this "
    "block as 'of what we hold, how much is spoken for'; read soft_allocation_"
    "coverage as 'of what is owed, how was it satisfied'. Do not conflate the "
    "two -- an inventory position and a demand position share no denominator. An "
    "older block on this dashboard reported allocated-vs-unallocated read from "
    "InventoryAssignment (Oracle's HARD allocation) and was removed by "
    "product-owner instruction, exactly as the old 'allocation' block was "
    "replaced by soft_allocation_coverage -- see the module docstring's "
    "WHY-allocation-became-soft-allocation-coverage section. This block is NOT "
    "that removed block's return: it is demand NETTING against on-hand stock, "
    "never a hard/Oracle reservation and never a re-introduction of "
    "InventoryAssignment-based allocation. Customer-owned inventory is netted "
    "OUT of demand FIRST, before company stock is asked to cover anything -- the "
    "same draw order soft_allocation_coverage's FROM_CUSTOMER_OWNED channel "
    "uses -- because not doing so would overstate how much company steel is "
    "genuinely tied up. A product with no on-hand row has an UNKNOWN position: "
    "it is reported in `unknown_position`, never folded into tied or not_tied as "
    "though it were zero. Metric-tonnes figures are a presentation convenience "
    "over `quantities_by_unit`; see MVP_COMPROMISES.md C-05 for the boundary "
    "that conversion must stay behind."
)

FIRST_RUNOUT_NOTE = (
    "'First runout' is the earliest ROS date among a well's in-scope demand lines "
    "that coverage did NOT satisfy -- the first date the well is let down. It is "
    "deliberately NOT the month a product's inventory balance crosses zero (see "
    "app.engines.well_dates for the two counterexamples), because a balance curve "
    "cannot see assignments, approvals, substitution or the Business Unit "
    "boundary. Wells whose every in-scope line is satisfied have no first-runout "
    "date and are absent from this list, which is an answer rather than a gap."
)


def quantity_by_unit(
    pairs,
) -> tuple[tuple["QuantityByUnit", ...], "UnitOfMeasure | None"]:
    """Group `(unit_of_measure, quantity)` pairs into per-unit totals.

    THE single implementation of "do not silently sum across units", used by every
    cross-product aggregate on this dashboard so they cannot each decide the rule
    differently.

    Returns `(breakdown, single_unit)`:

      breakdown    One `QuantityByUnit` per unit present, ordered by the unit's
                   own value so the list is stable between calls (a breakdown that
                   reordered itself between requests would look like changing data).
                   A unit contributes an entry as soon as any line is in it, even
                   at quantity 0 -- "we have demand in tonnes and it currently
                   totals zero" is a fact, and dropping it would hide that a second
                   unit is in play at all, which is the very thing that makes the
                   scalar unsafe.
      single_unit  The one unit when there is exactly one, else None. None is the
                   signal that the neighbouring scalar total must not be rendered.

    An EMPTY input returns `((), None)`: nothing was summed, so there is no unit to
    claim. Callers that want to distinguish "no demand" from "mixed units" have the
    line count beside them; both correctly refuse to label a scalar, because in
    neither case is there one unit the scalar is in.
    """
    totals: dict[UnitOfMeasure, float] = {}
    for unit, quantity in pairs:
        totals[unit] = totals.get(unit, 0.0) + quantity
    breakdown = tuple(
        QuantityByUnit(unit_of_measure=unit, quantity=totals[unit])
        for unit in sorted(totals, key=lambda u: u.value)
    )
    single = breakdown[0].unit_of_measure if len(breakdown) == 1 else None
    return breakdown, single


def _add_months(moment: datetime, months: int) -> datetime:
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    # Clamp the day so e.g. 31 Jan + 1 month lands on the last day of February
    # rather than raising.
    day = min(moment.day, _days_in_month(year, month))
    return moment.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year + (month // 12), month % 12 + 1, 1) - date(year, month, 1)).days


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Measure:
    """A figure that may not exist. `available=False` means DO NOT render a
    number -- render `reason`."""

    available: bool
    value: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class QuantityByUnit:
    """One unit's share of a cross-product aggregate. Never added to another."""

    unit_of_measure: UnitOfMeasure
    quantity: float


#: Default for every `*_tonnes` field before its block computes it. Explicitly
#: "not computed" rather than 0 t -- the same never-fabricate rule as Measure.
_TONNES_NOT_COMPUTED = Measure(available=False, reason="Not computed.")


def pairs_to_tonnes(pairs, label: str) -> Measure:
    """Convert `(product, quantity)` pairs to one metric-tonnes Measure.

    THE shared converter behind every `*_tonnes` headline on this dashboard
    (the older `_tonnes_measure` predates it and stays put -- it additionally
    builds the utilisation block's per-item `unconvertible` list, which the
    other blocks do not carry). Floor semantics, identical to that function:

      * every pair converts        -> available, exact total
      * some pairs cannot convert  -> available, PARTIAL total, `reason` names
                                      the excluded count -- a floor, never a 0
                                      silently added for the missing steel
      * nothing converts / no qty  -> unavailable with the reason

    `label` names the quantity in reasons ("demand", "shortfall", ...).
    """
    total = 0.0
    converted = 0
    unconvertible = 0
    contributing = 0
    for product, qty in pairs:
        if qty <= 0:
            continue
        contributing += 1
        # MVP-COMPROMISE[C-05]: Executive-Dashboard display-layer conversion --
        # coverage/MRP/demand COMPARISONS must never gain a call like this.
        # MVP-COMPROMISE[C-06]: a NULL Product.weight makes this quantity
        # unconvertible, not a fabricated 0 t.
        result = to_metric_tonnes(product.unit_of_measure, qty, product.weight)
        if result.available:
            total += result.tonnes
            converted += 1
        else:
            unconvertible += 1
    if contributing == 0:
        return Measure(
            available=False,
            reason=f"No {label} quantity in this scope to convert.",
        )
    if converted == 0:
        return Measure(
            available=False,
            reason=(
                f"No {label} quantity in this scope could be converted to "
                "metric tonnes (missing weight or a unit with no conversion). "
                "Read the native per-unit figures beside this one."
            ),
        )
    if unconvertible:
        return Measure(
            available=True,
            value=total,
            reason=(
                f"{unconvertible} {label} quantity/quantities excluded because "
                "they could not be converted to metric tonnes -- this total is "
                "a floor, not a complete answer. The native per-unit figures "
                "beside this one are complete."
            ),
        )
    return Measure(available=True, value=total)


def _tonnes_change_pct(current: Measure, previous: Measure) -> Measure:
    """Percentage change between two MT Measures, refused when undefined."""
    if not current.available or not previous.available:
        return Measure(
            available=False,
            reason=(
                "Both periods must convert to metric tonnes for an MT change "
                "to exist."
            ),
        )
    if previous.value in (0, 0.0):
        return Measure(
            available=False,
            reason=(
                "Prior-period tonnes were zero, so a percentage change is "
                "undefined. Compare the absolute figures instead."
            ),
        )
    if current.reason or previous.reason:
        # Either side is a FLOOR (partial conversion): a ratio of floors can
        # move for conversion reasons rather than demand reasons, so it is
        # refused rather than rendered as a trend.
        return Measure(
            available=False,
            reason=(
                "One of the periods' tonnes totals is partial (some "
                "quantities could not convert), so a change between them "
                "would mix conversion gaps into the trend."
            ),
        )
    return Measure(
        available=True,
        value=(current.value - previous.value) / previous.value * 100.0,
    )


@dataclass(frozen=True)
class HorizonDemand:
    months: int
    window_start: datetime
    window_end: datetime
    #: CROSS-PRODUCT TOTAL. Safe to render only when `unit_of_measure` is non-null;
    #: see the module docstring's UNITS section.
    current_total: float
    current_line_count: int
    previous: Measure
    previous_as_of: datetime | None
    change_pct: Measure
    definition: str
    #: Non-null exactly when every product contributing to `current_total` shares
    #: one unit. Null means "no single scalar total exists" -- render
    #: `quantities_by_unit`. Declared last only because the preceding fields have no
    #: defaults; it is not an afterthought.
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: tuple[QuantityByUnit, ...] = ()
    #: METRIC-TONNES HEADLINES (display-layer conversion, C-05): the same
    #: quantities as above, converted via Product.weight. Floor semantics --
    #: a quantity that cannot convert is excluded and named in `reason`, never
    #: added as 0 t. The native figures above remain the ground truth.
    current_tonnes: Measure = _TONNES_NOT_COMPUTED
    #: Converted with TODAY'S product weights -- weight has no revision
    #: history, and inventing one would invent a trend.
    previous_tonnes: Measure = _TONNES_NOT_COMPUTED
    #: Change of the MT totals. Exists even for mixed-unit horizons (where the
    #: native `change_pct` is undefined), because tonnes ARE one unit.
    change_pct_tonnes: Measure = _TONNES_NOT_COMPUTED


@dataclass(frozen=True)
class DemandTrend:
    horizons: tuple[HorizonDemand, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverageQuantityByStatus:
    """One coverage status's share of in-scope demand, in QUANTITY terms.

    The "quantity coverage snapshot" the product owner asked for. One headline
    percentage says how much is covered; this says what the rest actually IS, and
    the five outcomes need five different actions -- PendingApproval needs a
    customer decision, Uncovered needs a mill order, Unrecoverable needs an
    escalation. Collapsing them into "not covered" throws away the only part of
    the answer a manager can act on.

    `status` is a `CoverageStatus` value, or `NOT_EVALUATED` for an in-scope line
    with no verdict at all. The buckets partition the in-scope book exactly, so
    they sum to the coverage block's total -- per unit, which is the only way a
    sum is defined here.
    """

    status: str
    quantity: float
    line_count: int
    well_count: int
    counts_as_covered: bool
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: tuple[QuantityByUnit, ...] = ()
    #: MT headline for this bucket (display-layer, C-05; floor semantics).
    tonnes: Measure = _TONNES_NOT_COMPUTED


@dataclass(frozen=True)
class CoverageSummary:
    """Coverage measured by DEMAND QUANTITY, with the well counts kept beside it.

    THE PRIMARY MEASURE IS QUANTITY, on the product owner's ruling: "coverage
    should be based on quantity from lines not number of wells. while good to keep
    well situation for reference". So `coverage_pct` is now the quantity ratio and
    the well-count ratio is `well_coverage_pct` -- both are served, neither is
    hidden, and the primary one is the one the owner asked for.

    The two measures answer genuinely different questions and routinely disagree:
    one well short by 50 metres and one short by 40 000 count the same in the well
    ratio and nothing alike in the quantity ratio. That is why the counts are kept
    rather than dropped -- "2 of 3 wells are covered" is what a planner walks the
    floor with -- and why they are no longer the headline.

    UNITS: `coverage_pct` is a RATIO of two cross-product sums, so it is reported
    None when the in-scope book spans more than one unit of measure. A percentage
    of metres-plus-tonnes over metres-plus-tonnes is not a weaker number, it is
    not a number. `quantities_by_unit` is always correct and always populated.
    """

    available: bool
    #: QUANTITY-based coverage ratio -- the primary measure. None when unavailable
    #: or when the contributing products do not share one unit.
    coverage_pct: float | None
    covered_quantity: float
    total_quantity: float
    unit_of_measure: UnitOfMeasure | None = None
    covered_quantities_by_unit: tuple[QuantityByUnit, ...] = ()
    total_quantities_by_unit: tuple[QuantityByUnit, ...] = ()
    #: The quantity coverage SNAPSHOT: one entry per status, partitioning
    #: `total_quantity`. Ordered by `COVERAGE_QUANTITY_STATUSES`.
    by_status: tuple[CoverageQuantityByStatus, ...] = ()
    #: REFERENCE, kept explicitly at the owner's request. The well-count ratio the
    #: block used to report as its headline.
    well_coverage_pct: float | None = None
    covered_well_count: int = 0
    uncovered_well_count: int = 0
    evaluated_well_count: int = 0
    unevaluated_well_count: int = 0
    in_scope_line_count: int = 0
    reason: str | None = None
    #: MT headlines (display-layer, C-05; floor semantics). `coverage_pct_tonnes`
    #: is computed from the MT totals, so it EXISTS for a mixed-unit book where
    #: the native `coverage_pct` is rightly None -- tonnes are one unit.
    covered_tonnes: Measure = _TONNES_NOT_COMPUTED
    total_tonnes: Measure = _TONNES_NOT_COMPUTED
    coverage_pct_tonnes: Measure = _TONNES_NOT_COMPUTED


@dataclass(frozen=True)
class SupplyRisk:
    available: bool
    #: CROSS-PRODUCT TOTAL -- see the module docstring's UNITS section.
    unrecoverable_quantity: float
    unrecoverable_line_count: int
    affected_well_count: int
    #: A ratio of two cross-product totals, so it is None in the mixed-unit case
    #: rather than computed from sums of unlike things.
    unrecoverable_pct_of_in_scope_demand: float | None
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: tuple[QuantityByUnit, ...] = ()
    reason: str | None = None
    note: str = ON_ORDER_NOTE
    #: MT headline (display-layer, C-05; floor semantics).
    unrecoverable_tonnes: Measure = _TONNES_NOT_COMPUTED


@dataclass(frozen=True)
class SoftAllocationChannel:
    """One way in-scope demand was satisfied (or was not), in quantity terms.

    FIVE channels partition the horizon's demand exactly, listed in the order the
    steel is actually drawn: from the customer's OWN inventory, from an Oracle
    assignment reserved to the customer, from the Business Unit's shared unassigned
    pool, via an approved substitute, and not satisfied at all. `key` is the stable
    machine name, `label` is renderable.
    """

    key: str
    label: str
    quantity: float
    line_count: int
    #: Share of the horizon's total demand. None when the horizon spans several
    #: units of measure -- a share of a mixed-unit sum is undefined.
    pct: float | None = None
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: tuple[QuantityByUnit, ...] = ()
    #: MT headline for this channel (display-layer, C-05; floor semantics).
    tonnes: Measure = _TONNES_NOT_COMPUTED


#: The five channels, in the order the steel is DRAWN -- which is also the order a
#: manager reads them: the customer's own material first (the ruling), then the two
#: company sources, then the one that drew a different product, then the gap.
#:
#: `from_own_assignment` is RESERVED company steel: a HARD/HYBRID line's own Oracle
#: assignment, or a SOFT customer's assignments pooled across its own wells. The
#: engine records a SOFT customer's pooled draw inside `consumed_from_pool` (that is
#: what "the pool" meant to a per-customer pass) and separately in
#: `consumed_from_assignment_block`; since D01 the shared pool and a customer's
#: reservations are different tiers -- a neighbour can reach one and never the
#: other -- so the block is moved here and `from_shared_pool` is strictly the
#: Business Unit's unassigned steel (owner ruling 2026-09-06). Calling reserved
#: steel "shared unassigned pool" on the one screen management reads would have
#: been the same kind of misstatement the customer-owned channel exists to avoid.
#: `from_customer_owned` is listed FIRST because it is drawn first: for the same
#: product, customer-owned inventory is consumed ahead of any company steel and ahead
#: of any Oracle assignment (see `app.engines.allocation`). Reading the channels top
#: to bottom therefore reads the actual draw order, which is what makes the block
#: explainable rather than merely accurate.
#:
#: It is a channel of its own rather than part of `from_shared_pool` because it is
#: not our steel. Folding it in would report material the company does not own as
#: company inventory that had been allocated, on the one screen management uses to
#: judge how well the company is covering its customers.
FROM_CUSTOMER_OWNED = "from_customer_owned"
FROM_POOL = "from_shared_pool"
FROM_ASSIGNMENT = "from_own_assignment"
VIA_SUBSTITUTE = "via_substitute"
NOT_SATISFIED = "not_satisfied"
SOFT_ALLOCATION_CHANNELS = (
    (
        FROM_CUSTOMER_OWNED,
        "Drawn from the customer's OWN inventory (consumed first, ahead of "
        "company stock)",
    ),
    (
        FROM_POOL,
        "Drawn from the Business Unit's shared unassigned pool (soft allocation)",
    ),
    (
        FROM_ASSIGNMENT,
        "Drawn from an Oracle assignment reserved to this customer (its own "
        "line's under HARD/HYBRID; pooled across its own wells under SOFT)",
    ),
    (VIA_SUBSTITUTE, "Satisfied via an approved substitute product"),
    (NOT_SATISFIED, "Not satisfied by any of the above"),
)


@dataclass(frozen=True)
class SoftAllocationCoverage:
    """How in-scope demand over the horizon was ACTUALLY satisfied.

    Replaces the old `allocation` block. See `SOFT_ALLOCATION_NOTE` and the module
    docstring for the rename and what the number does and does not mean.

    Every figure here originates in ONE `compute_customer_coverage` pass per
    customer in scope -- the read-only computing half of the single coverage
    implementation -- so this block cannot disagree with the coverage block beside
    it and no allocation rule is expressed twice in the codebase.
    """

    horizon_months: int
    available: bool
    #: CROSS-PRODUCT TOTAL -- see the module docstring's UNITS section.
    total_quantity: float | None
    total_line_count: int = 0
    channels: tuple[SoftAllocationChannel, ...] = ()
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: tuple[QuantityByUnit, ...] = ()
    #: Provenance of the ASSIGNMENT channel only, in the vocabulary
    #: `app.engines.mrp.InventoryPosition.assigned_source` uses. The pool and
    #: substitute channels are computed by this platform from its own rules, so
    #: they have no upstream provenance to state; the assignment channel is drawn
    #: from an Oracle projection and does.
    assignment_source: str = "unavailable"
    #: Customers whose coverage pass could not be run (no Business Unit, so no
    #: inventory pool). Named rather than skipped: their demand is in scope and its
    #: absence from these figures has to be visible.
    unresolved_customer_ids: tuple[str, ...] = ()
    reason: str | None = None
    note: str = SOFT_ALLOCATION_NOTE
    #: MT headline for the horizon total (display-layer, C-05; floor semantics).
    total_tonnes: Measure = _TONNES_NOT_COMPUTED


@dataclass(frozen=True)
class FirstRunoutWell:
    """One well that falls short, with the customer and well named.

    `shortfall_quantity` is the summed quantity of this well's in-scope demand
    lines that coverage did NOT satisfy. It is knowable exactly -- coverage already
    decided which lines those are -- so it is reported rather than estimated. It is
    a CROSS-PRODUCT sum within one well, so the units rule applies here too: a well
    whose unsatisfied lines span two units gets a null scalar unit and the
    breakdown.
    """

    well_id: str
    well_name: str
    customer_id: str | None
    customer_name: str | None
    business_unit_id: str | None
    first_runout_date: datetime
    shortfall_quantity: float
    shortfall_line_count: int
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: tuple[QuantityByUnit, ...] = ()
    #: MT headline for this well's shortfall (display-layer, C-05; floor
    #: semantics).
    shortfall_tonnes: Measure = _TONNES_NOT_COMPUTED


@dataclass(frozen=True)
class FirstRunoutDetail:
    """The wells that first fall short, earliest first, with the cap made explicit.

    `cap`, `total_well_count` and `omitted_well_count` are all in the payload so a
    truncated list can SAY it is truncated. A management screen that silently
    showed 10 of 40 shortfalls would understate the problem by a factor of four,
    and the reader would have no way to tell.
    """

    available: bool
    wells: tuple[FirstRunoutWell, ...] = ()
    #: Wells with a first-runout date IN SCOPE, before the cap.
    total_well_count: int = 0
    returned_well_count: int = 0
    cap: int = FIRST_RUNOUT_WELL_CAP
    omitted_well_count: int = 0
    truncated: bool = False
    #: The earliest date across the WHOLE in-scope set, not merely the returned
    #: page. Identical to `wells[0].first_runout_date` whenever anything is
    #: returned, and stated separately so it survives any future paging.
    earliest_first_runout_date: datetime | None = None
    reason: str | None = None
    note: str = FIRST_RUNOUT_NOTE


@dataclass(frozen=True)
class OnOrderArrival:
    """On-order quantity expected to land within `months` from now. CUMULATIVE.

    Cumulative rather than a disjoint bucket so it lines up with the demand-trend
    horizons, which are also cumulative: "6 000 arriving within 6 months against
    18 000 of demand in the next 6 months" is a sentence a manager can act on,
    and it is only true if both sides count the same way.
    """

    months: int
    window_end: datetime
    quantity: float
    row_count: int
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: tuple[QuantityByUnit, ...] = ()
    #: MT headline for this cumulative horizon (display-layer, C-05; floor
    #: semantics).
    tonnes: Measure = _TONNES_NOT_COMPUTED


@dataclass(frozen=True)
class IncomingSupply:
    """Material already on order into the scoped Business Unit(s).

    A NEW block. It was deliberately absent while `on_order` was a hardcoded 0,
    because a bare 0 on a management screen reads as "nothing is on order" -- a
    measurement nobody had made. It exists now that there is a real local
    projection to read (`app.models.inventory_on_order.InventoryOnOrder`), and it
    carries the provenance that keeps the distinction honest:

      source              "unavailable" | "synthetic" | "oracle" | "mixed" -- where
                          THESE rows came from.
      oracle_integrated   Still False. The feed is not live. Independent of
                          `source` on purpose; see `ON_ORDER_NOTE`.

    `products_with_no_data` is the count of in-scope demanded products with no
    projected row at all. Their on-order quantity is UNKNOWN, and it is neither
    added in as 0 nor quietly ignored -- it is counted and named, so a reader can
    see how much of the answer is missing.
    """

    available: bool
    #: CROSS-PRODUCT TOTAL -- see the module docstring's UNITS section. None when
    #: unavailable.
    quantity: float | None = None
    unit_of_measure: UnitOfMeasure | None = None
    quantities_by_unit: tuple[QuantityByUnit, ...] = ()
    row_count: int = 0
    product_count: int = 0
    products_with_no_data: int = 0
    products_with_nothing_on_order: int = 0
    earliest_expected_arrival: datetime | None = None
    latest_expected_arrival: datetime | None = None
    #: MT headlines (display-layer, C-05; floor semantics).
    tonnes: Measure = _TONNES_NOT_COMPUTED
    undated_tonnes: Measure = _TONNES_NOT_COMPUTED
    #: Quantity on rows with NO expected arrival date -- a raised but
    #: unacknowledged PO. Reported separately rather than dated to today, which
    #: would place unscheduled steel inside every arrival horizon below.
    undated_quantity: float = 0.0
    by_arrival_horizon: tuple[OnOrderArrival, ...] = ()
    source: str = "unavailable"
    oracle_integrated: bool = False
    reason: str | None = None
    note: str = ON_ORDER_NOTE


@dataclass(frozen=True)
class InventoryUtilisationProduct:
    """Tied vs not-tied on-hand for ONE (Business Unit, product), this horizon.

    `tied + not_tied == on_hand_quantity` EXACTLY, by construction -- see
    `inventory_utilisation`'s netting formula. `customer_owned_quantity` is
    subtracted from demand BEFORE the netting, so it is reported here for audit
    but does not itself appear in the tied/not_tied split.
    """

    business_unit_id: str
    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure
    on_hand_quantity: float
    demand_in_window: float
    #: The part of `demand_in_window` whose ROS MONTH has already passed --
    #: counted (it still claims steel) but labelled its own bucket so an
    #: overdue book can never masquerade as a healthy forward one.
    demand_overdue: float
    customer_owned_quantity: float
    tied: float
    not_tied: float
    #: Per-row MT conversions (display-layer, C-05). Unavailable when the
    #: product cannot convert (no weight / PC unit) -- never a fabricated 0.
    tied_tonnes: Measure = _TONNES_NOT_COMPUTED
    not_tied_tonnes: Measure = _TONNES_NOT_COMPUTED


@dataclass(frozen=True)
class UnknownPositionProduct:
    """A product with in-scope demand but NO `InventoryOnHand` row in its Business
    Unit. Its position is UNKNOWN, not zero -- it appears here and in NEITHER
    `tied` nor `not_tied`. See `app.engines.company_inventory.OnHandRow.known`.
    """

    business_unit_id: str
    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure


@dataclass(frozen=True)
class UnconvertibleQuantity:
    """A tied-or-not_tied quantity `app.engines.units.to_metric_tonnes` could not
    convert (PC, or a NULL `Product.weight` -- see MVP-COMPROMISE[C-06]). Named
    rather than silently dropped from the metric-tonnes headline.
    """

    business_unit_id: str
    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure
    #: Which side of the split this quantity belongs to: "tied" or "not_tied".
    side: str
    quantity: float
    reason: str


@dataclass(frozen=True)
class InventoryUtilisation:
    """Of company-owned steel standing in the yard, how much is tied to in-window
    demand and how much is idle. See `INVENTORY_UTILISATION_NOTE` for what this
    block is and, just as importantly, what it is NOT.
    """

    horizon_months: int
    available: bool
    #: Metric-tonnes headline. `Measure.available=False` when NOTHING in that
    #: side could be converted; a partial total (some products excluded) is still
    #: `available=True` with `reason` naming the exclusion -- see `unconvertible`.
    #: MVP-COMPROMISE[C-05]: this conversion call is the Dashboard-presentation-
    #: only boundary described in `app.engines.units` and MVP_COMPROMISES.md C-05.
    tied_tonnes: "Measure"
    not_tied_tonnes: "Measure"
    products: tuple[InventoryUtilisationProduct, ...] = ()
    #: Native-unit breakdowns, via `quantity_by_unit` -- never re-implemented.
    tied_by_unit: tuple[QuantityByUnit, ...] = ()
    not_tied_by_unit: tuple[QuantityByUnit, ...] = ()
    #: Every quantity excluded from the metric-tonnes totals above, with why.
    unconvertible: tuple[UnconvertibleQuantity, ...] = ()
    unknown_position: tuple[UnknownPositionProduct, ...] = ()
    unknown_position_count: int = 0
    reason: str | None = None
    note: str = INVENTORY_UTILISATION_NOTE


@dataclass(frozen=True)
class ExecutiveScope:
    """WHICH scope produced this payload, stated on the payload.

    Both filters are optional. `label` and `description` are renderable prose so
    the screen can title itself without re-deriving the scope from the ids.
    """

    business_unit_id: str | None = None
    business_unit_name: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    label: str = "All Business Units"
    description: str = (
        "Every Business Unit and every customer. Note the Business Unit is the hard "
        "inventory boundary, so an unscoped total spans pools that cannot supply "
        "one another."
    )
    #: The customers whose demand is in scope, so the reader can audit the
    #: denominator of every figure below.
    customer_ids: tuple[str, ...] = ()


class ScopeContradiction(ValueError):
    """A customer was named together with a Business Unit it does not belong to.

    Raised rather than answered with an empty result -- see the module docstring's
    SCOPE section. `app.api.dashboard` maps it to a 400.
    """


@dataclass(frozen=True)
class ExecutiveSummary:
    generated_at: datetime
    customer_id: str | None
    scope: ExecutiveScope
    demand_trend: DemandTrend
    coverage: CoverageSummary
    supply_risk: SupplyRisk
    soft_allocation_coverage: SoftAllocationCoverage
    first_runout: FirstRunoutDetail
    incoming_supply: IncomingSupply
    inventory_utilisation: InventoryUtilisation
    #: Convenience mirrors of `scope`, so a client that only wants to caption the
    #: screen need not descend. `customer_id` is kept at the top level because it
    #: was already there and clients read it.
    business_unit_id: str | None = None
    business_unit_name: str | None = None
    customer_name: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Demand trend
# ---------------------------------------------------------------------------


def _in_scope(line: DemandLine) -> bool:
    """Same scope the coverage engine applies: status by WELL, profile by line.

    The scope is resolved through the LINE'S OWN SESSION rather than taken as a
    parameter, and that is a deliberate trade rather than an oversight. This
    predicate is called from eight places in this module, every one of them inside a
    comprehension over `_scoped_lines`; threading two arguments through all eight
    would give eight opportunities for one of them to keep reading the shipped
    constant after an administrator adjusted the scope, and an executive dashboard
    whose demand trend used one scope and whose coverage block used another is
    precisely the kind of unexplainable disagreement this codebase writes single
    definitions to avoid.

    It costs no query per line. `app.engines.coverage_scope` fetches the settings
    row by PRIMARY KEY, which SQLAlchemy serves from the session identity map after
    the first load, so a loop over every demand line on the platform resolves the
    scope once. `object_session` is never None here -- every line reaching this
    predicate came out of a query on the caller's session.
    """
    db = object_session(line)
    return (
        line.well.demand_status in effective_status_filter(db)
        and line.profile in effective_profile_filter(db)
    )


def _scoped_lines(
    db: Session,
    customer_id: str | None,
    business_unit_id: str | None = None,
) -> list[DemandLine]:
    """Every demand line inside the requested scope, in-scope or not.

    The BU filter walks the full hierarchy -- DemandLine -> Well -> PlanningNode ->
    Customer -> business_unit_id -- because the BU is not recorded on any row below
    Customer. `PlanningNode.customer_id` is present on every node (see
    `app.engines.coverage._customer_wells`), so this stays a flat join with no
    recursive parent walk.

    Both filters are ANDed. A customer outside the named BU therefore selects
    nothing, which is exactly why `resolve_scope` refuses that combination before
    it gets here -- an empty result would be indistinguishable from a real empty
    book.
    """
    query = db.query(DemandLine)
    if customer_id is not None or business_unit_id is not None:
        query = query.join(Well, DemandLine.well_id == Well.id).join(
            PlanningNode, Well.planning_node_id == PlanningNode.id
        )
    if customer_id is not None:
        query = query.filter(PlanningNode.customer_id == customer_id)
    if business_unit_id is not None:
        query = query.join(Customer, PlanningNode.customer_id == Customer.id).filter(
            Customer.business_unit_id == business_unit_id
        )
    return query.all()


def _scoped_wells(
    db: Session,
    customer_id: str | None,
    business_unit_id: str | None = None,
) -> list[Well]:
    """Every well inside the requested scope. Same join and same rules as above."""
    query = db.query(Well)
    if customer_id is not None or business_unit_id is not None:
        query = query.join(PlanningNode, Well.planning_node_id == PlanningNode.id)
    if customer_id is not None:
        query = query.filter(PlanningNode.customer_id == customer_id)
    if business_unit_id is not None:
        query = query.join(Customer, PlanningNode.customer_id == Customer.id).filter(
            Customer.business_unit_id == business_unit_id
        )
    return query.all()


def resolve_scope(
    db: Session,
    customer_id: str | None = None,
    business_unit_id: str | None = None,
) -> ExecutiveScope:
    """Validate and describe the requested scope. THE one place scope is decided.

    Raises:
      LookupError          the named customer or Business Unit does not exist. The
                           route turns it into a 404; it is separated from the
                           contradiction below because they are different mistakes
                           and deserve different codes.
      ScopeContradiction   both were named and the customer is not in that BU.

    Why the contradiction is refused rather than answered with an empty result
    ------------------------------------------------------------------------
    An empty result is a MEASUREMENT: "this scope holds no demand, no risk and no
    shortfall". For a scope that cannot exist, that is a fabrication of the exact
    kind every `available` flag in this module exists to prevent -- and it is the
    worst kind, because it looks like good news. A 400 states what is true (the
    request was impossible) and is the only one of the two a caller can fix.

    A customer with NO Business Unit is a special case worth naming: it contradicts
    every BU, so naming it alongside any BU raises. On its own it is a legitimate
    (if broken) scope, and the blocks that need an inventory pool report their own
    refusal for it rather than this function pre-empting them.
    """
    customer = None
    if customer_id is not None:
        customer = db.get(Customer, customer_id)
        if customer is None:
            raise LookupError(f"Customer {customer_id!r} not found")

    business_unit = None
    if business_unit_id is not None:
        business_unit = db.get(BusinessUnit, business_unit_id)
        if business_unit is None:
            raise LookupError(f"Business Unit {business_unit_id!r} not found")

    if customer is not None and business_unit is not None:
        if customer.business_unit_id != business_unit.id:
            actual = (
                f"Business Unit {customer.business_unit_id!r}"
                if customer.business_unit_id is not None
                else "no Business Unit at all"
            )
            raise ScopeContradiction(
                f"Customer {customer.name!r} ({customer.id}) belongs to {actual}, "
                f"not to Business Unit {business_unit.name!r} ({business_unit.id}), "
                "so the requested scope is empty by construction. This is refused "
                "rather than answered: an empty dashboard would report no demand, "
                "no supply risk and no shortfall for this scope, which reads as a "
                "measurement of a scope that does not exist. Drop one of the two "
                "filters."
            )

    if customer is not None:
        customer_ids = (customer.id,)
        label = customer.name
        description = (
            f"Customer {customer.name}. Inventory is pooled across this customer's "
            "own wells and no further; coverage here cannot be moved by another "
            "customer's demand."
        )
        if business_unit is not None:
            label = f"{business_unit.name} / {customer.name}"
    elif business_unit is not None:
        customer_ids = tuple(
            sorted(
                row_id
                for (row_id,) in db.query(Customer.id).filter(
                    Customer.business_unit_id == business_unit.id
                )
            )
        )
        label = business_unit.name
        description = (
            f"Business Unit {business_unit.name}, covering "
            f"{len(customer_ids)} customer(s). The Business Unit is the hard "
            "inventory boundary, so every supply figure below is drawn from one "
            "pool that no other BU can reach."
        )
    else:
        customer_ids = tuple(sorted(row_id for (row_id,) in db.query(Customer.id)))
        label = "All Business Units"
        description = (
            "Every Business Unit and every customer. Note the Business Unit is the "
            "hard inventory boundary, so an unscoped total spans pools that cannot "
            "supply one another."
        )

    return ExecutiveScope(
        business_unit_id=(business_unit.id if business_unit is not None else None),
        business_unit_name=(business_unit.name if business_unit is not None else None),
        customer_id=(customer.id if customer is not None else None),
        customer_name=(customer.name if customer is not None else None),
        label=label,
        description=description,
        customer_ids=customer_ids,
    )


@dataclass(frozen=True)
class _HistoricState:
    quantity: float
    ros_date: datetime
    #: The DEMAND STATUS AS IT WAS, read from `DemandRevision.status`.
    #:
    #: Since the live column moved to `Well.demand_status` this is a snapshot of
    #: the line's WELL's status at `as_of`, not of a per-line status -- which is
    #: what makes the reconstruction still work, and still work per line. A status
    #: change writes a revision to every line of the well
    #: (`app.engines.coverage.set_well_demand_status`), so each line's own history
    #: records it and nothing has to be reconstructed by looking sideways at a
    #: sibling. Reading `Well.demand_status` here instead would be wrong: that is
    #: today's value, and this dataclass is about a past one.
    status: DemandStatus
    profile: DemandProfile


def _state_as_of(
    db: Session, as_of: datetime, lines: list[DemandLine]
) -> tuple[dict[str, _HistoricState] | None, str | None]:
    """Reconstruct each line's state at `as_of` from `DemandRevision`, or explain
    why it cannot be done.

    `DemandRevision` is append-only and carries `created_at`, so the state of a
    line at any past moment is its latest revision at or before that moment. That
    works for every line that HAS such a revision.

    The structural gap that used to make this permanently impossible is CLOSED:
    `DemandLine` now carries `created_at`, and every demand line's initial state is
    written as revision 1 at creation (see
    `app.models.demand._write_initial_revision`). So for any line created from that
    change onwards, a revision at or before `as_of` exists whenever the line
    existed, and "existed and never changed" is no longer indistinguishable from
    "did not exist".

    It still does not work for rows created BEFORE that change. They have no
    initial revision and no creation timestamp, and those facts do not exist to be
    back-filled -- inventing them would invent a trend. So the answer remains
    unavailable while any such line is in scope, and becomes available on its own
    as the history accumulates.

    (Lines DELETED since `as_of` are a second, smaller gap: revision history for
    a removed line is removed with it. Recorded in the returned reason when the
    first gap does not already make the answer unavailable.)
    """
    line_ids = {line.id for line in lines}
    if not line_ids:
        return None, "There is no demand in scope, so there is nothing to compare."

    # A line CREATED after the comparison date verifiably did not exist then, so
    # it contributes nothing and -- crucially -- is not "unknown". This is what
    # `created_at` buys: without it every newly-added line would look like a hole
    # and the figure would never become available.
    not_yet_existing = {
        line.id
        for line in lines
        if line.created_at is not None and line.created_at > as_of
    }

    revisions = (
        db.query(DemandRevision)
        .filter(DemandRevision.demand_line_id.in_(sorted(line_ids)))
        .order_by(DemandRevision.revision_no)
        .all()
    )
    latest: dict[str, DemandRevision] = {}
    for rev in revisions:
        if rev.created_at is None or rev.created_at > as_of:
            continue
        current = latest.get(rev.demand_line_id)
        if current is None or (rev.created_at, rev.revision_no) >= (
            current.created_at,
            current.revision_no,
        ):
            latest[rev.demand_line_id] = rev

    missing = sorted(line_ids - set(latest) - not_yet_existing)
    if missing:
        return None, (
            f"Not computable yet: {len(missing)} of {len(line_ids)} in-scope demand "
            f"lines have neither a creation timestamp nor revision history at or "
            f"before {as_of.date().isoformat()}, so whether they existed then is "
            "unknown. A prior-period total is only genuine once every line's state "
            "at the comparison date can be reconstructed; guessing would invent a "
            "trend. These are rows that predate demand-line history being recorded "
            "-- the missing facts cannot be back-filled, so they are not invented. "
            "Every demand line created from now on carries a created_at and an "
            "initial revision, so this figure accumulates and becomes available on "
            "its own once the comparison date is later than that change."
        )

    return {
        line_id: _HistoricState(
            quantity=rev.quantity,
            ros_date=rev.ros_date,
            status=rev.status,
            profile=rev.profile,
        )
        for line_id, rev in latest.items()
    }, None


def _previous_horizon(
    db: Session,
    lines: list[DemandLine],
    months: int,
    now: datetime,
) -> tuple[Measure, datetime]:
    """Total demand for the SAME forward horizon, as it stood `months` ago.

    "Previous 3 months" is ambiguous for forward-looking ROS-dated demand: the
    demand book has no history of its own, only the ROS dates it currently
    carries, and summing ROS dates in the PAST measures something else entirely
    (work already delivered), not a comparable prior book.

    The reading implemented here is the one the data can actually support: take
    the vantage point `as_of = now - months`, reconstruct the demand book as it
    was on that date from `DemandRevision`, and total ITS next-`months` horizon
    `[as_of, as_of + months)`. Same horizon length, same forward direction,
    earlier vantage point -- so "demand for the next 3 months is up 12%" means the
    book grew, not that the calendar moved.

    Returns `Measure(available=False, reason=...)` when the reconstruction is not
    possible. It never estimates.

    The third element is the contributing `(product, quantity)` pairs (empty
    when unavailable), for the MT headline. The product is the line's CURRENT
    product row: `Product.weight` has no revision history, so the historic
    quantities are converted with today's weights -- inventing a past weight
    would invent a trend.
    """
    as_of = _add_months(now, -months)
    window_start, window_end = as_of, _add_months(as_of, months)

    state, reason = _state_as_of(db, as_of, lines)
    if state is None:
        return Measure(available=False, reason=reason), as_of, []

    products_by_line = {line.id: line.product for line in lines}
    total = 0.0
    pairs: list[tuple[Product, float]] = []
    for line_id, historic in state.items():
        if (
            historic.status in effective_status_filter(db)
            and historic.profile in effective_profile_filter(db)
            and window_start <= historic.ros_date < window_end
        ):
            total += historic.quantity
            pairs.append((products_by_line[line_id], historic.quantity))
    return Measure(available=True, value=total), as_of, pairs


def demand_trend(
    db: Session,
    customer_id: str | None = None,
    now: datetime | None = None,
    business_unit_id: str | None = None,
) -> DemandTrend:
    now = now or datetime.utcnow()
    all_lines = _scoped_lines(db, customer_id, business_unit_id)
    in_scope = [line for line in all_lines if _in_scope(line)]

    horizons: list[HorizonDemand] = []
    for months in DEMAND_TREND_HORIZONS:
        window_end = _add_months(now, months)
        window = [line for line in in_scope if now <= line.ros_date < window_end]
        current_total = sum(line.quantity for line in window)
        by_unit, single_unit = quantity_by_unit(
            (line.product.unit_of_measure, line.quantity) for line in window
        )

        previous, as_of, previous_pairs = _previous_horizon(
            db, in_scope, months, now
        )
        current_tonnes = pairs_to_tonnes(
            ((line.product, line.quantity) for line in window), "demand"
        )
        previous_tonnes = (
            pairs_to_tonnes(previous_pairs, "prior-period demand")
            if previous.available
            else Measure(available=False, reason=previous.reason)
        )
        if single_unit is None and by_unit:
            # Mixed units: `current_total` is a sum of unlike things, so a
            # percentage change over it is not a weaker number, it is not a number.
            # Refused here rather than computed and captioned, for the same reason
            # this module refuses a 0 for an unmeasured figure.
            change = Measure(
                available=False,
                reason=(
                    "Demand in this horizon spans products measured in different "
                    "units of measure ("
                    + ", ".join(entry.unit_of_measure.value for entry in by_unit)
                    + "), so there is no single total to express a change in. "
                    "Compare the per-unit figures in quantities_by_unit."
                ),
            )
        elif not previous.available:
            change = Measure(
                available=False,
                reason="No prior-period total exists, so no change can be shown.",
            )
        elif previous.value in (0, 0.0):
            change = Measure(
                available=False,
                reason=(
                    "Prior-period demand was zero, so a percentage change is "
                    "undefined. Compare the absolute figures instead."
                ),
            )
        else:
            change = Measure(
                available=True,
                value=(current_total - previous.value) / previous.value * 100.0,
            )

        horizons.append(
            HorizonDemand(
                months=months,
                window_start=now,
                window_end=window_end,
                current_total=current_total,
                current_line_count=len(window),
                unit_of_measure=single_unit,
                quantities_by_unit=by_unit,
                previous=previous,
                previous_as_of=as_of,
                change_pct=change,
                current_tonnes=current_tonnes,
                previous_tonnes=previous_tonnes,
                change_pct_tonnes=_tonnes_change_pct(
                    current_tonnes, previous_tonnes
                ),
                definition=(
                    f"Current: in-scope demand with ROS in the next {months} "
                    f"months. Previous: the same forward {months}-month horizon "
                    f"as the demand book stood on {as_of.date().isoformat()}, "
                    "reconstructed from revision history."
                ),
            )
        )

    # The scope is NAMED from the effective filters, not restated as the
    # shipped constants: with a persisted or per-request scope the constants
    # would describe a filter that was not applied.
    scope_statuses = ", ".join(
        sorted(s.value for s in effective_status_filter(db))
    )
    scope_profiles = " or ".join(
        sorted(p.value for p in effective_profile_filter(db))
    )
    notes = [
        "Demand totals count only lines the coverage engine evaluates "
        f"({scope_statuses} status, {scope_profiles} profile) so the trend "
        "and the coverage figures describe the same book of demand.",
    ]
    if any(h.unit_of_measure is None and h.quantities_by_unit for h in horizons):
        notes.append(MIXED_UNITS_NOTE)
    return DemandTrend(horizons=tuple(horizons), notes=tuple(notes))


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def coverage_summary(
    db: Session,
    customer_id: str | None = None,
    business_unit_id: str | None = None,
) -> CoverageSummary:
    """Coverage by DEMAND QUANTITY, with the well counts kept as reference.

    THE PRIMARY MEASURE IS QUANTITY. The product owner's ruling: "coverage should
    be based on quantity from lines not number of wells. while good to keep well
    situation for reference, let's change logic and also add quantity coverage
    snapshot." So the numerator and denominator are quantities of steel, taken from
    the demand lines themselves.

    Both denominators are auditable and both are reported:

      QUANTITY   every IN-SCOPE demand line (Confirmed well, Primary or Contingency
                 profile) in the requested scope. An in-scope line with no
                 CoverageResult row is NOT dropped -- it lands in the
                 `NotEvaluated` bucket of the snapshot, so the buckets partition
                 the book exactly and a missing verdict cannot flatter the ratio.
                 A line the filters exclude is not demand this dashboard claims to
                 have measured, so it is out of the denominator entirely.
      WELLS      wells the engine actually evaluated, i.e. `coverage_status is not
                 None`. A well with no in-scope demand is neither covered nor
                 uncovered; counting it either way invents a fact. Unchanged from
                 before, and now reported as `well_coverage_pct`.

    The verdict is READ from `CoverageResult`, never re-derived -- the same
    discipline `supply_risk` follows for the same reason.
    """
    wells = _scoped_wells(db, customer_id, business_unit_id)
    lines = [
        line
        for line in _scoped_lines(db, customer_id, business_unit_id)
        if _in_scope(line)
    ]

    # ---- reference: the well counts, exactly as they were ------------------
    evaluated = [w for w in wells if w.coverage_status is not None]
    unevaluated = len(wells) - len(evaluated)
    covered_wells = sum(
        1 for w in evaluated if w.coverage_status == CoverageStatus.COVERED.value
    )
    uncovered_wells = len(evaluated) - covered_wells
    well_pct = covered_wells / len(evaluated) * 100.0 if evaluated else None

    if not lines:
        return CoverageSummary(
            available=False,
            coverage_pct=None,
            covered_quantity=0.0,
            total_quantity=0.0,
            well_coverage_pct=well_pct,
            covered_well_count=covered_wells,
            uncovered_well_count=uncovered_wells,
            evaluated_well_count=len(evaluated),
            unevaluated_well_count=unevaluated,
            in_scope_line_count=0,
            reason=(
                "No demand line is in scope, so there is no quantity to express a "
                "coverage percentage of. A coverage percentage over an empty book "
                "would be meaningless, not 0%."
            ),
        )

    # {status bucket: [(product, quantity, well_id)]} -- built once, then reduced,
    # so the snapshot and the headline can never be computed from different rows.
    # The PRODUCT (not merely its unit) is kept so the MT headlines convert from
    # exactly the same rows.
    grouped: dict[str, list[tuple[Product, float, str]]] = {
        status: [] for status in COVERAGE_QUANTITY_STATUSES
    }
    for line in lines:
        result = line.coverage_result
        bucket = result.status.value if result is not None else NOT_EVALUATED
        grouped[bucket].append((line.product, line.quantity, line.well_id))

    by_status: list[CoverageQuantityByStatus] = []
    for status in COVERAGE_QUANTITY_STATUSES:
        entries = grouped[status]
        breakdown, single = quantity_by_unit(
            (product.unit_of_measure, qty) for product, qty, _well_id in entries
        )
        by_status.append(
            CoverageQuantityByStatus(
                status=status,
                quantity=sum(qty for _p, qty, _w in entries),
                line_count=len(entries),
                well_count=len({well_id for _p, _q, well_id in entries}),
                counts_as_covered=status in COVERED_QUANTITY_STATUSES,
                unit_of_measure=single,
                quantities_by_unit=breakdown,
                tonnes=pairs_to_tonnes(
                    ((product, qty) for product, qty, _w in entries), "demand"
                ),
            )
        )

    covered_pairs = [
        (product, qty)
        for status in COVERED_QUANTITY_STATUSES
        for product, qty, _w in grouped[status]
    ]
    covered_by_unit, covered_unit = quantity_by_unit(
        (product.unit_of_measure, qty) for product, qty in covered_pairs
    )
    total_by_unit, total_unit = quantity_by_unit(
        (line.product.unit_of_measure, line.quantity) for line in lines
    )
    covered_quantity = sum(qty for _p, qty in covered_pairs)
    total_quantity = sum(line.quantity for line in lines)

    covered_tonnes = pairs_to_tonnes(covered_pairs, "covered demand")
    total_tonnes = pairs_to_tonnes(
        ((line.product, line.quantity) for line in lines), "demand"
    )
    if not (covered_tonnes.available or covered_pairs):
        # Zero covered lines is a real 0 t against a real denominator -- distinct
        # from "could not convert", which stays unavailable.
        covered_tonnes = Measure(available=True, value=0.0)
    if (
        covered_tonnes.available
        and total_tonnes.available
        and total_tonnes.value
        and not (covered_tonnes.reason or total_tonnes.reason)
    ):
        coverage_pct_tonnes = Measure(
            available=True,
            value=covered_tonnes.value / total_tonnes.value * 100.0,
        )
    else:
        coverage_pct_tonnes = Measure(
            available=False,
            reason=(
                "An MT coverage percentage needs both totals fully converted "
                "to metric tonnes; one of them is unavailable or partial."
            ),
        )

    # A percentage needs numerator and denominator in the SAME single unit. A
    # single-unit numerator against a mixed-unit denominator is still not a
    # percentage of anything -- the same test `supply_risk` applies.
    comparable = (
        total_unit is not None
        and (covered_unit is None or covered_unit == total_unit)
        and (covered_unit is not None or not covered_pairs)
    )

    return CoverageSummary(
        available=True,
        coverage_pct=(
            covered_quantity / total_quantity * 100.0
            if total_quantity and comparable
            else None
        ),
        covered_quantity=covered_quantity,
        total_quantity=total_quantity,
        unit_of_measure=total_unit,
        covered_quantities_by_unit=covered_by_unit,
        total_quantities_by_unit=total_by_unit,
        by_status=tuple(by_status),
        well_coverage_pct=well_pct,
        covered_well_count=covered_wells,
        uncovered_well_count=uncovered_wells,
        evaluated_well_count=len(evaluated),
        unevaluated_well_count=unevaluated,
        in_scope_line_count=len(lines),
        covered_tonnes=covered_tonnes,
        total_tonnes=total_tonnes,
        coverage_pct_tonnes=coverage_pct_tonnes,
        reason=(
            None
            if comparable
            else (
                "In-scope demand spans several units of measure, so no single "
                "coverage PERCENTAGE is reported -- a ratio of two sums that each "
                "add unlike units is undefined, not merely imprecise. Read "
                "covered_quantities_by_unit against total_quantities_by_unit, and "
                "the per-status breakdown in by_status."
            )
        ),
    )


# ---------------------------------------------------------------------------
# Supply risk
# ---------------------------------------------------------------------------


def supply_risk(
    db: Session,
    customer_id: str | None = None,
    business_unit_id: str | None = None,
) -> SupplyRisk:
    """Demand that cannot be covered even if a mill order were placed today.

    That determination is NOT made here. The coverage engine already made it, per
    line, via `app.engines.order_dates.is_recoverable`, and recorded it as
    `CoverageStatus.UNRECOVERABLE`. This function reads those rows and sums them.
    Re-deriving recoverability would produce a second, drifting definition of the
    single most alarming number on the dashboard.

    The status/profile filters are re-applied (as `app.api.dashboard` does) so a
    `CoverageResult` row that outlived its scope can never inflate the risk.
    """
    query = (
        db.query(DemandLine, CoverageResult)
        .join(CoverageResult, CoverageResult.demand_line_id == DemandLine.id)
        .filter(CoverageResult.status == CoverageStatus.UNRECOVERABLE)
    )
    # The SAME scope join `_scoped_lines` uses, so the numerator and the
    # denominator below are drawn from one definition of "in scope".
    if customer_id is not None or business_unit_id is not None:
        query = query.join(Well, DemandLine.well_id == Well.id).join(
            PlanningNode, Well.planning_node_id == PlanningNode.id
        )
    if customer_id is not None:
        query = query.filter(PlanningNode.customer_id == customer_id)
    if business_unit_id is not None:
        query = query.join(Customer, PlanningNode.customer_id == Customer.id).filter(
            Customer.business_unit_id == business_unit_id
        )
    rows = [(line, cr) for line, cr in query.all() if _in_scope(line)]

    all_in_scope = [
        line
        for line in _scoped_lines(db, customer_id, business_unit_id)
        if _in_scope(line)
    ]
    in_scope_total = sum(line.quantity for line in all_in_scope)
    quantity = sum(line.quantity for line, _cr in rows)

    by_unit, single_unit = quantity_by_unit(
        (line.product.unit_of_measure, line.quantity) for line, _cr in rows
    )
    # The RATIO's denominator is the whole in-scope book, so it has its own unit
    # question -- and a percentage is only meaningful when numerator and denominator
    # are in the SAME single unit. Both are checked; a numerator that happens to be
    # single-unit against a mixed-unit denominator is still not a percentage of
    # anything.
    _denominator_by_unit, denominator_unit = quantity_by_unit(
        (line.product.unit_of_measure, line.quantity) for line in all_in_scope
    )
    comparable = (
        single_unit is not None
        and denominator_unit is not None
        and single_unit == denominator_unit
    )

    return SupplyRisk(
        available=True,
        unrecoverable_quantity=quantity,
        unrecoverable_line_count=len(rows),
        affected_well_count=len({line.well_id for line, _cr in rows}),
        unrecoverable_pct_of_in_scope_demand=(
            quantity / in_scope_total * 100.0
            if in_scope_total and comparable
            else None
        ),
        unit_of_measure=single_unit,
        quantities_by_unit=by_unit,
        unrecoverable_tonnes=(
            pairs_to_tonnes(
                ((line.product, line.quantity) for line, _cr in rows),
                "unrecoverable",
            )
            if rows
            # No unrecoverable rows is a REAL 0 t, not "nothing to convert".
            else Measure(available=True, value=0.0)
        ),
        reason=(
            None
            if comparable or not rows
            else (
                "Unrecoverable demand and/or the in-scope book it is compared "
                "against spans several units of measure, so no percentage is "
                "reported. The per-unit quantities are in quantities_by_unit."
            )
        ),
    )


# ---------------------------------------------------------------------------
# Soft allocation coverage -- REPLACES the old Oracle-hard-assignment block
# ---------------------------------------------------------------------------


def _assignment_source(db: Session, line_ids: list[str]) -> str:
    """Provenance of the assignment rows behind the `from_own_assignment` channel.

    Same vocabulary as `app.engines.mrp.InventoryPosition.assigned_source`.
    "unavailable" means no assignment row touches any line in scope -- which for
    THIS block is a fine, fully-available answer (a pool draw needs no
    assignment), not the refusal the old allocation block had to make.
    """
    if not line_ids:
        return "unavailable"
    sources = {
        (row.source_system or "synthetic")
        for row in db.query(InventoryAssignment).filter(
            InventoryAssignment.demand_line_id.in_(sorted(line_ids))
        )
    }
    if not sources:
        return "unavailable"
    if sources == {"oracle"}:
        return "oracle"
    if "oracle" in sources:
        return "mixed"
    return "synthetic"


def soft_allocation_coverage(
    db: Session,
    horizon_months: int = 12,
    customer_id: str | None = None,
    now: datetime | None = None,
    business_unit_id: str | None = None,
) -> SoftAllocationCoverage:
    """How in-scope demand over the horizon was ACTUALLY satisfied, by quantity.

    THE FIGURES COME FROM THE COVERAGE PASS, NOT FROM A SECOND DERIVATION
    --------------------------------------------------------------------
    One `compute_customer_coverage` call per customer in scope -- the read-only
    computing half of the platform's single coverage implementation -- and the
    channel quantities are read off `CustomerCoverage.consumed_from_pool` /
    `.consumed_from_assignment` / `.consumed_from_assignment_block`, which are
    `app.engines.allocation.AllocationOutcome`'s own maps carried out of that pass
    unchanged (the block is the reserved part of a SOFT customer's pool draw, and
    is reported under the assignment channel -- see `SOFT_ALLOCATION_CHANNELS`). Nothing
    here re-implements a policy, re-sorts by ROS, or decides what an assignment
    means. That constraint is not stylistic: two implementations of the allocation
    rules would drift, and a management summary is the one consumer with nothing
    downstream to notice that it had.

    THE FIVE CHANNELS PARTITION DEMAND, NOT STEEL
    ---------------------------------------------
    They sum to the horizon's demand quantity exactly, per unit. One consequence
    worth stating: a line that drew PARTIALLY on its own product's pool and was
    then satisfied by a substitute is reported wholly under `via_substitute`,
    because the substitute is what met the requirement. The residual pool draw is
    real and is not lost -- it is in the line's coverage reason text and in the
    residual pool the next pass sees -- but it satisfied no demand, so counting it
    as a satisfying channel would make the channels sum to more than the book.

    The `from_customer_owned` channel is the ownership split the coverage pass now
    reports (`CustomerCoverage.consumed_from_customer_owned`), read off that pass
    exactly like the other two draw channels. It is never merged into
    `from_shared_pool`: that material is not the company's, and this is the screen
    management judges the company's own coverage performance on.

    A CUSTOMER WITH NO BUSINESS UNIT
    -------------------------------
    Has no inventory pool, so `compute_customer_coverage` raises
    `InventoryScopeMissing` for it (see `app.models.customer.Customer`). Its id is
    reported in `unresolved_customer_ids` and its demand is EXCLUDED from every
    total here, with the reason saying so. `InventoryRowMissing` is deliberately
    NOT caught: an absent on-hand row is incomplete upstream data that the whole
    platform refuses over, and swallowing it here would report a smaller book as
    though it were the book.
    """
    if horizon_months not in ALLOCATION_HORIZONS:
        raise ValueError(
            f"horizon_months must be one of {ALLOCATION_HORIZONS}, got {horizon_months}"
        )
    now = now or datetime.utcnow()
    window_end = _add_months(now, horizon_months)

    lines = [
        line
        for line in _scoped_lines(db, customer_id, business_unit_id)
        if _in_scope(line) and now <= line.ros_date < window_end
    ]
    if not lines:
        return SoftAllocationCoverage(
            horizon_months=horizon_months,
            available=False,
            total_quantity=0.0,
            reason=(
                f"No in-scope demand has an ROS date inside the next "
                f"{horizon_months} months, so there is nothing to describe the "
                "satisfaction of."
            ),
        )

    # One pass per customer that owns any of these lines. Keyed so a customer with
    # lines in several wells is computed ONCE -- the pass is pool-wide by design.
    customers_by_line: dict[str, Customer] = {
        line.id: line.well.planning_node.customer for line in lines
    }
    passes: dict[str, CustomerCoverage] = {}
    unresolved: list[str] = []
    for customer in {c.id: c for c in customers_by_line.values()}.values():
        try:
            passes[customer.id] = compute_customer_coverage(db, customer)
        except InventoryScopeMissing:
            unresolved.append(customer.id)

    # (product, quantity) rather than (unit, quantity): the PRODUCT is kept so
    # the per-channel MT headlines convert from exactly the same pairs the
    # native figures sum.
    channel_pairs: dict[str, list[tuple[Product, float]]] = {
        key: [] for key, _label in SOFT_ALLOCATION_CHANNELS
    }
    counted_lines: list[DemandLine] = []
    for line in lines:
        customer = customers_by_line[line.id]
        computed = passes.get(customer.id)
        if computed is None:
            continue  # unresolved customer -- named in the payload, not counted
        verdict = computed.by_line.get(line.id)
        if verdict is None:
            # The pass excluded the line. It cannot happen while `_in_scope` mirrors
            # the engine's default filters, and if the two ever diverge the honest
            # bucket is "not satisfied by any of the above" rather than a silent drop.
            channel_pairs[NOT_SATISFIED].append((line.product, line.quantity))
            counted_lines.append(line)
            continue

        counted_lines.append(line)
        product = line.product
        if verdict.status == CoverageStatus.COVERED_VIA_SUBSTITUTE:
            channel_pairs[VIA_SUBSTITUTE].append((product, line.quantity))
            continue

        # Capped in DRAW ORDER -- customer-owned, then assignment, then pool -- which
        # is the order `app.engines.allocation` actually consumes them in. The caps
        # are what keep the channels a partition of DEMAND: each channel can only ever
        # claim the part of the line the previous ones did not.
        from_customer_owned = min(
            max(0.0, computed.consumed_from_customer_owned.get(line.id, 0.0)),
            line.quantity,
        )
        # Reserved steel: a HARD/HYBRID line's own assignment, or the part of a SOFT
        # customer's pool draw that came from its own pooled assignments (the
        # engine records that part in BOTH maps; it is counted once, here).
        block = max(0.0, computed.consumed_from_assignment_block.get(line.id, 0.0))
        from_assignment = min(
            max(0.0, computed.consumed_from_assignment.get(line.id, 0.0)) + block,
            line.quantity - from_customer_owned,
        )
        from_pool = min(
            max(0.0, computed.consumed_from_pool.get(line.id, 0.0) - block),
            line.quantity - from_customer_owned - from_assignment,
        )
        channel_pairs[FROM_CUSTOMER_OWNED].append((product, from_customer_owned))
        channel_pairs[FROM_ASSIGNMENT].append((product, from_assignment))
        channel_pairs[FROM_POOL].append((product, from_pool))
        channel_pairs[NOT_SATISFIED].append(
            (
                product,
                max(
                    0.0,
                    line.quantity
                    - from_customer_owned
                    - from_assignment
                    - from_pool,
                ),
            )
        )

    total_by_unit, total_unit = quantity_by_unit(
        (line.product.unit_of_measure, line.quantity) for line in counted_lines
    )
    total = sum(line.quantity for line in counted_lines)
    mixed = total_unit is None and bool(total_by_unit)

    channels: list[SoftAllocationChannel] = []
    for key, label in SOFT_ALLOCATION_CHANNELS:
        pairs = channel_pairs[key]
        breakdown, single = quantity_by_unit(
            (product.unit_of_measure, qty) for product, qty in pairs
        )
        quantity = sum(qty for _p, qty in pairs)
        channels.append(
            SoftAllocationChannel(
                key=key,
                label=label,
                quantity=quantity,
                line_count=sum(1 for _p, qty in pairs if qty > 0),
                # A SHARE of a mixed-unit total is undefined, not imprecise, so it
                # is withheld rather than computed and captioned.
                pct=(quantity / total * 100.0 if total and not mixed else None),
                unit_of_measure=single,
                quantities_by_unit=breakdown,
                tonnes=(
                    pairs_to_tonnes(pairs, "demand")
                    if any(qty > 0 for _p, qty in pairs)
                    # A channel nothing drew from is a real 0 t, not
                    # "nothing to convert".
                    else Measure(available=True, value=0.0)
                ),
            )
        )

    reasons: list[str] = []
    if mixed:
        reasons.append(
            "In-scope demand over this horizon spans several units of measure, so "
            "no channel PERCENTAGE is reported -- a share of a total that adds "
            "unlike units is undefined. Read each channel's quantities_by_unit."
        )
    if unresolved:
        reasons.append(
            f"{len(unresolved)} customer(s) in scope have no Business Unit and "
            "therefore no inventory pool, so no allocation could be computed for "
            "them. Their demand is EXCLUDED from every figure in this block rather "
            "than counted as unsatisfied, which would blame them for a mapping gap. "
            "Map them to a Business Unit (Customer.business_unit_id)."
        )
    assignment_source = _assignment_source(db, [line.id for line in counted_lines])
    if assignment_source == "synthetic":
        reasons.append(
            "Assignment rows behind the 'from own assignment' channel are seeded "
            "demo data, not an Oracle feed."
        )

    return SoftAllocationCoverage(
        horizon_months=horizon_months,
        available=bool(counted_lines),
        total_quantity=total,
        total_line_count=len(counted_lines),
        channels=tuple(channels),
        unit_of_measure=total_unit,
        quantities_by_unit=total_by_unit,
        assignment_source=assignment_source,
        unresolved_customer_ids=tuple(sorted(unresolved)),
        reason=(" ".join(reasons) or None),
        total_tonnes=pairs_to_tonnes(
            ((line.product, line.quantity) for line in counted_lines), "demand"
        ),
    )


# ---------------------------------------------------------------------------
# First runout, with customer and well detail
# ---------------------------------------------------------------------------


def first_runout_detail(
    db: Session,
    customer_id: str | None = None,
    business_unit_id: str | None = None,
    cap: int = FIRST_RUNOUT_WELL_CAP,
) -> FirstRunoutDetail:
    """The wells that first fall short, earliest date first, customer named.

    The date comes from `app.engines.well_dates.well_dates`, which is THE
    definition of `first_runout_date` in this platform and which deliberately does
    not reuse the MRP runout projection -- read that module's docstring for the two
    counterexamples that settle it. Nothing is re-derived here; this function adds
    the customer, the well name and the shortfall quantity, and orders the result.

    `well_dates` answers for any number of wells in TWO queries, so this is cheap
    for a whole Business Unit.

    THE SHORTFALL QUANTITY IS KNOWN, SO IT IS REPORTED
    -------------------------------------------------
    It is the summed quantity of this well's in-scope lines whose `CoverageResult`
    is not one of the satisfied statuses -- including lines with NO verdict, on the
    same convention `well_dates` itself uses: unevaluated demand is not known to be
    covered. That set is exactly the set whose earliest ROS produced the date, so
    the two figures describe the same lines.

    THE CAP IS IN THE PAYLOAD
    ------------------------
    A management screen wants the urgent wells, not all of them, so the list is
    capped. `cap`, `total_well_count`, `returned_well_count`, `omitted_well_count`
    and `truncated` are all served, and the omission is logged. Silently returning
    10 of 40 shortfalls would understate the problem fourfold with nothing on the
    screen to say so.
    """
    wells = _scoped_wells(db, customer_id, business_unit_id)
    if not wells:
        return FirstRunoutDetail(
            available=False,
            cap=cap,
            reason=(
                "There is no well in this scope, so there is no first-runout date to "
                "report."
            ),
        )

    dates = well_dates(db, [w.id for w in wells])

    # (product, quantity): the product is kept so each well's MT shortfall
    # headline converts from exactly the pairs the native figures sum.
    shortfall_pairs: dict[str, list[tuple[Product, float]]] = {}
    for line in _scoped_lines(db, customer_id, business_unit_id):
        if not _in_scope(line):
            continue
        result = line.coverage_result
        status_value = result.status.value if result is not None else None
        if status_value in SATISFIED_STATUSES:
            continue
        shortfall_pairs.setdefault(line.well_id, []).append(
            (line.product, line.quantity)
        )

    rows: list[FirstRunoutWell] = []
    for well in wells:
        entry = dates.get(well.id)
        if entry is None or entry.first_runout_date is None:
            continue
        node = well.planning_node
        customer = node.customer if node is not None else None
        pairs = shortfall_pairs.get(well.id, [])
        breakdown, single = quantity_by_unit(
            (product.unit_of_measure, qty) for product, qty in pairs
        )
        rows.append(
            FirstRunoutWell(
                well_id=well.id,
                well_name=well.name,
                customer_id=(customer.id if customer is not None else None),
                customer_name=(customer.name if customer is not None else None),
                business_unit_id=(
                    customer.business_unit_id if customer is not None else None
                ),
                first_runout_date=entry.first_runout_date,
                shortfall_quantity=sum(qty for _p, qty in pairs),
                shortfall_line_count=len(pairs),
                unit_of_measure=single,
                quantities_by_unit=breakdown,
                shortfall_tonnes=pairs_to_tonnes(pairs, "shortfall"),
            )
        )

    if not rows:
        return FirstRunoutDetail(
            available=True,
            cap=cap,
            total_well_count=0,
            returned_well_count=0,
            reason=(
                "No well in this scope has an unsatisfied in-scope demand line, so "
                "no well has a first-runout date. That is an answer, not a gap -- "
                "see the note."
            ),
        )

    # Earliest first; ties broken on the well NAME so the order is total and
    # identical between requests. Two wells due the same day would otherwise swap
    # places for no reason a reader could explain -- the same rule
    # `app.engines.well_dates.sort_by_earliest_ros` applies to its own ordering.
    rows.sort(key=lambda row: (row.first_runout_date, row.well_name))
    omitted = max(0, len(rows) - cap)
    if omitted:
        _log.info(
            "executive first_runout_detail: returning %d of %d wells with a "
            "first-runout date (cap=%d, omitted=%d); scope customer_id=%r "
            "business_unit_id=%r. The omission is reported in the payload as "
            "omitted_well_count / truncated.",
            cap,
            len(rows),
            cap,
            omitted,
            customer_id,
            business_unit_id,
        )

    return FirstRunoutDetail(
        available=True,
        wells=tuple(rows[:cap]),
        total_well_count=len(rows),
        returned_well_count=len(rows[:cap]),
        cap=cap,
        omitted_well_count=omitted,
        truncated=bool(omitted),
        earliest_first_runout_date=rows[0].first_runout_date,
        reason=(
            (
                f"{len(rows)} wells in this scope fall short; the {cap} earliest are "
                f"returned and {omitted} are omitted. The full list is on the "
                "Coverage Workspace."
            )
            if omitted
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Incoming supply (on order)
# ---------------------------------------------------------------------------


def _scoped_business_unit_ids(
    db: Session,
    customer_id: str | None,
    business_unit_id: str | None,
) -> tuple[list[str], bool]:
    """(business unit ids in scope, whether any scoped customer is unmapped).

    On-order rows are held per (Business Unit, product), so the incoming-supply
    question is a BU question however the dashboard was scoped. A customer-scoped
    request resolves to that customer's OWN Business Unit and no other -- a BU is
    never crossed, on any path.
    """
    if business_unit_id is not None:
        return [business_unit_id], False
    query = db.query(Customer.business_unit_id)
    if customer_id is not None:
        query = query.filter(Customer.id == customer_id)
    values = [row_id for (row_id,) in query]
    unmapped = any(value is None for value in values)
    return sorted({value for value in values if value is not None}), unmapped


def incoming_supply(
    db: Session,
    customer_id: str | None = None,
    business_unit_id: str | None = None,
    now: datetime | None = None,
) -> IncomingSupply:
    """Material already on order into the scoped Business Unit(s).

    Read from `app.models.inventory_on_order.InventoryOnOrder` through
    `app.engines.inventory.on_order_rows` -- the one resolver, so this block cannot
    disagree with the on-order figure MRP's `InventoryPosition` reports.

    Scoped to the products the in-scope demand actually touches. A BU may hold
    purchase orders for products nothing in scope needs, and counting those would
    answer a question nobody asked ("how much steel is coming") in place of the one
    they did ("how much of what we need is coming").

    THE THREE STATES, KEPT APART
    ---------------------------
      unavailable         no projected purchase-order row exists for any in-scope
                          product. The quantity is UNKNOWN. Reported with a reason;
                          never 0.
      measured, zero      rows exist and total nothing -- "nothing is on order".
                          `products_with_nothing_on_order` counts the products this
                          is explicitly true of.
      measured, positive  the normal case.

    `products_with_no_data` counts in-scope products with no row at all even when
    others do have one, so a partly-known answer says how partial it is.
    """
    now = now or datetime.utcnow()
    bu_ids, any_unmapped = _scoped_business_unit_ids(db, customer_id, business_unit_id)

    lines = [
        line
        for line in _scoped_lines(db, customer_id, business_unit_id)
        if _in_scope(line)
    ]
    units_by_product = {line.product_id: line.product.unit_of_measure for line in lines}
    # The Product rows themselves, for the MT headlines (weight lives there).
    products_by_id = {line.product_id: line.product for line in lines}
    product_ids = set(units_by_product)

    if not product_ids:
        return IncomingSupply(
            available=False,
            reason=(
                "No demand is in scope, so there is no product whose incoming supply "
                "could be reported."
            ),
        )
    if not bu_ids:
        return IncomingSupply(
            available=False,
            product_count=len(product_ids),
            reason=(
                "No Business Unit is in scope"
                + (
                    " -- the customer is not mapped to one, so it has no inventory "
                    "pool and no purchase order can be attributed to it. Map the "
                    "customer to a Business Unit (Customer.business_unit_id)."
                    if any_unmapped
                    else ". On-order material is held per (Business Unit, product), "
                    "so there is nothing to report against."
                )
            ),
        )

    rows = [row for bu_id in bu_ids for row in on_order_rows(db, bu_id, product_ids)]
    products_with_rows = {row.product_id for row in rows}
    no_data = len(product_ids - products_with_rows)

    if not rows:
        return IncomingSupply(
            available=False,
            product_count=len(product_ids),
            products_with_no_data=no_data,
            reason=(
                f"No purchase-order row is projected for any of the "
                f"{len(product_ids)} in-scope product(s) in this scope, so the "
                "on-order quantity is UNKNOWN. It is deliberately not reported as "
                "0: 'nothing is on order' is stated by an explicit zero-quantity "
                "row, and this is the absence of any row at all. The Oracle "
                "purchase-order feed is not integrated (oracle_integrated=false)."
            ),
        )

    per_product_totals: dict[str, float] = {}
    for row in rows:
        per_product_totals[row.product_id] = per_product_totals.get(
            row.product_id, 0.0
        ) + max(0.0, row.quantity or 0.0)
    nothing_on_order = sum(1 for total in per_product_totals.values() if total == 0.0)

    by_unit, single = quantity_by_unit(
        (units_by_product[row.product_id], max(0.0, row.quantity or 0.0))
        for row in rows
    )
    total = sum(max(0.0, row.quantity or 0.0) for row in rows)

    dated = [row for row in rows if row.expected_arrival_date is not None]
    arrivals: list[OnOrderArrival] = []
    for months in ON_ORDER_ARRIVAL_HORIZONS:
        horizon_end = _add_months(now, months)
        # CUMULATIVE: everything promised before `horizon_end`, including anything
        # already overdue -- a late PO is still incoming supply, and dropping it
        # would flatter the position.
        inside = [row for row in dated if row.expected_arrival_date < horizon_end]
        breakdown, arrival_unit = quantity_by_unit(
            (units_by_product[row.product_id], max(0.0, row.quantity or 0.0))
            for row in inside
        )
        arrivals.append(
            OnOrderArrival(
                months=months,
                window_end=horizon_end,
                quantity=sum(max(0.0, row.quantity or 0.0) for row in inside),
                row_count=len(inside),
                unit_of_measure=arrival_unit,
                quantities_by_unit=breakdown,
                tonnes=(
                    pairs_to_tonnes(
                        (
                            (
                                products_by_id[row.product_id],
                                max(0.0, row.quantity or 0.0),
                            )
                            for row in inside
                        ),
                        "on-order",
                    )
                    if any(max(0.0, row.quantity or 0.0) > 0 for row in inside)
                    # Nothing (or only zero rows) promised inside this window is
                    # a measured 0 t.
                    else Measure(available=True, value=0.0)
                ),
            )
        )

    sources = {(row.source_system or "synthetic") for row in rows}
    if sources == {"oracle"}:
        source = "oracle"
    elif "oracle" in sources:
        source = "mixed"
    else:
        source = "synthetic"

    reasons: list[str] = []
    if single is None:
        reasons.append(
            "Incoming supply spans products measured in different units of measure, "
            "so there is no single scalar total. Read quantities_by_unit."
        )
    if no_data:
        reasons.append(
            f"{no_data} of {len(product_ids)} in-scope product(s) have NO projected "
            "purchase-order row, so their on-order quantity is unknown and is not "
            "included above. This total is therefore a floor, not a complete answer."
        )
    if source == "synthetic":
        reasons.append(
            "These rows are seeded demo data, not an Oracle feed "
            "(oracle_integrated=false)."
        )

    return IncomingSupply(
        available=True,
        quantity=total,
        unit_of_measure=single,
        quantities_by_unit=by_unit,
        row_count=len(rows),
        product_count=len(product_ids),
        products_with_no_data=no_data,
        products_with_nothing_on_order=nothing_on_order,
        earliest_expected_arrival=(
            min(row.expected_arrival_date for row in dated) if dated else None
        ),
        latest_expected_arrival=(
            max(row.expected_arrival_date for row in dated) if dated else None
        ),
        undated_quantity=sum(
            max(0.0, row.quantity or 0.0)
            for row in rows
            if row.expected_arrival_date is None
        ),
        by_arrival_horizon=tuple(arrivals),
        tonnes=(
            pairs_to_tonnes(
                (
                    (products_by_id[row.product_id], max(0.0, row.quantity or 0.0))
                    for row in rows
                ),
                "on-order",
            )
            if total > 0
            # Rows exist and total nothing: "nothing is on order" is a measured
            # 0 t, kept distinct from "could not convert".
            else Measure(available=True, value=0.0)
        ),
        undated_tonnes=(
            pairs_to_tonnes(
                (
                    (products_by_id[row.product_id], max(0.0, row.quantity or 0.0))
                    for row in rows
                    if row.expected_arrival_date is None
                ),
                "undated on-order",
            )
            if any(
                row.expected_arrival_date is None
                and max(0.0, row.quantity or 0.0) > 0
                for row in rows
            )
            # No positive undated quantity: the undated bucket is a measured 0 t.
            else Measure(available=True, value=0.0)
        ),
        source=source,
        # UNCHANGED, and independent of `source` on purpose: a seeded projection is
        # not a live integration. See ON_ORDER_NOTE.
        oracle_integrated=False,
        reason=(" ".join(reasons) or None),
    )


def _tonnes_measure(
    items: list[InventoryUtilisationProduct],
    products: dict[str, Product],
    field_name: str,
    side_label: str,
) -> tuple["Measure", list[UnconvertibleQuantity]]:
    """Sum `getattr(item, field_name)` (tied or not_tied) to metric tonnes.

    Reuses `app.engines.units.to_metric_tonnes` -- one implementation of the
    conversion, called once per product quantity here. Never adds 0.0 for a
    quantity that could not convert: see the return-shape rules on
    `InventoryUtilisation.tied_tonnes`.
    """
    total = 0.0
    any_converted = False
    unconvertible: list[UnconvertibleQuantity] = []
    contributing = [item for item in items if getattr(item, field_name) > 0]

    for item in contributing:
        product = products[item.product_id]
        qty = getattr(item, field_name)
        # MVP-COMPROMISE[C-05]: this call is the Executive-Dashboard-presentation
        # boundary `app.engines.units` and MVP_COMPROMISES.md C-05 describe --
        # coverage/MRP/demand comparisons must never gain a call like this one.
        # MVP-COMPROMISE[C-06]: `Product.weight` is nullable; a NULL here makes
        # this specific quantity unconvertible, not a fabricated 0 t.
        result = to_metric_tonnes(item.unit_of_measure, qty, product.weight)
        if result.available:
            total += result.tonnes
            any_converted = True
        else:
            unconvertible.append(
                UnconvertibleQuantity(
                    business_unit_id=item.business_unit_id,
                    product_id=item.product_id,
                    product_description=item.product_description,
                    unit_of_measure=item.unit_of_measure,
                    side=side_label,
                    quantity=qty,
                    reason=result.reason,
                )
            )

    if not contributing:
        return (
            Measure(
                available=False,
                reason=f"No {side_label} quantity in this scope to convert.",
            ),
            unconvertible,
        )
    if unconvertible and not any_converted:
        return (
            Measure(
                available=False,
                reason=(
                    f"No {side_label} quantity in this scope could be converted "
                    "to metric tonnes -- see `unconvertible`."
                ),
            ),
            unconvertible,
        )
    if unconvertible:
        return (
            Measure(
                available=True,
                value=total,
                reason=(
                    f"{len(unconvertible)} {side_label} quantity/quantities "
                    "excluded from this metric-tonnes total because they could "
                    "not be converted (see `unconvertible`) -- this total is a "
                    "floor, not a complete answer."
                ),
            ),
            unconvertible,
        )
    return Measure(available=True, value=total), unconvertible


def inventory_utilisation(
    db: Session,
    customer_id: str | None = None,
    business_unit_id: str | None = None,
    horizon_months: int = 12,
    now: datetime | None = None,
) -> InventoryUtilisation:
    """Of company-owned steel standing in the yard, how much is tied to in-window
    demand and how much is idle.

    THE DENOMINATOR IS INVENTORY, NOT DEMAND -- explicitly the opposite of
    `soft_allocation_coverage`, whose denominator is demand quantity. See
    `INVENTORY_UTILISATION_NOTE`; it is repeated there and here on purpose so the
    two blocks are never conflated by a reader who only saw one docstring.

    NOT A RETURN OF THE OLD `InventoryAssignment`-BASED ALLOCATION BLOCK
    ----------------------------------------------------------------------
    An older Executive Dashboard block reported allocated-vs-unallocated read
    from `InventoryAssignment`, Oracle's HARD allocation, and it was removed by
    product-owner instruction -- see the module docstring's "WHY allocation
    BECAME soft allocation coverage" section for the same kind of removal
    happening to the sibling block. This function is demand NETTING against
    on-hand stock: it creates no reservation, reads no `InventoryAssignment` row,
    and must not be read as that block's return under a new name.

    NETTING, PER (BUSINESS UNIT, PRODUCT)
    --------------------------------------
        demand_in_window = in-scope demand line quantities, ROS in [now, now+horizon)
        customer_owned   = declared CustomerOwnedInventory for that product, held
                            by the customers contributing that demand
        tied     = min(on_hand, max(0, demand_in_window - customer_owned))
        not_tied = on_hand - tied

    Customer-owned is subtracted FIRST -- the same draw order
    `soft_allocation_coverage`'s FROM_CUSTOMER_OWNED channel uses (customer-owned
    stock is consumed before company stock for the same product). Skipping this
    step would overstate `tied`: demand a customer can already meet from their
    own declared stock is not actually pulling on OUR steel.

    THE BUSINESS UNIT IS AN ABSOLUTE BOUNDARY
    ------------------------------------------
    Netting never crosses a BU -- on-hand in one BU can never be reported "tied"
    by demand sitting in another. `business_unit_id=None` does not net across
    every BU as one pool; it resolves the in-scope BUs (same helper
    `incoming_supply` uses, `_scoped_business_unit_ids`) and nets each
    separately, exactly like `incoming_supply` already does for on-order.

    A PRODUCT WITH NO ON-HAND ROW HAS AN UNKNOWN POSITION
    -------------------------------------------------------
    `company_inventory.get_position`'s `OnHandRow.known == False` means no
    `InventoryOnHand` row exists. That product is reported in `unknown_position`
    and appears in NEITHER `tied` nor `not_tied` -- never folded in as though the
    position were zero.
    """
    if horizon_months not in ALLOCATION_HORIZONS:
        raise ValueError(
            f"horizon_months must be one of {ALLOCATION_HORIZONS}, got {horizon_months}"
        )
    now = now or datetime.utcnow()
    window_end = _add_months(now, horizon_months)

    bu_ids, any_unmapped = _scoped_business_unit_ids(db, customer_id, business_unit_id)
    if not bu_ids:
        return InventoryUtilisation(
            horizon_months=horizon_months,
            available=False,
            tied_tonnes=Measure(available=False, reason="No Business Unit is in scope."),
            not_tied_tonnes=Measure(
                available=False, reason="No Business Unit is in scope."
            ),
            reason=(
                "No Business Unit is in scope"
                + (
                    " -- the customer is not mapped to one, so it has no "
                    "inventory pool to net against. Map the customer to a "
                    "Business Unit (Customer.business_unit_id)."
                    if any_unmapped
                    else ". Inventory utilisation is held per (Business Unit, "
                    "product), so there is nothing to net."
                )
            ),
        )

    # OVERDUE DEMAND COUNTS (product-owner decision, 2026-08-12): a demand line
    # whose ROS has already passed still needs steel -- lateness does not
    # cancel it -- so it is counted into the tie AND reported as its own
    # labelled bucket (`demand_overdue`), never silently folded in.
    #
    # Overdue is judged at MONTH granularity (ROS month strictly before the
    # current month), platform-wide (2026-08-14 ruling): MOR already worked in
    # months, and QA caught the same product reporting 0 overdue on one screen
    # and 3,250 on the other under one shared label. A line due EARLIER THIS
    # MONTH is not yet overdue anywhere.
    overdue_cutoff = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    lines = [
        line
        for line in _scoped_lines(db, customer_id, business_unit_id)
        if _in_scope(line) and line.ros_date < window_end
    ]

    # Group in-scope demand by (business_unit_id, product_id), and remember which
    # customers contributed to each so customer-owned stock can be netted against
    # exactly the customers actually demanding that product here -- never a
    # customer elsewhere in the platform who happens to own the same SKU.
    demand_by_key: dict[tuple[str, str], float] = defaultdict(float)
    overdue_by_key: dict[tuple[str, str], float] = defaultdict(float)
    customers_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    products: dict[str, Product] = {}
    for line in lines:
        well = line.well
        planning_node = well.planning_node if well is not None else None
        customer = planning_node.customer if planning_node is not None else None
        bu_id = customer.business_unit_id if customer is not None else None
        if bu_id is None or bu_id not in bu_ids:
            continue  # unmapped, or resolved to a BU this scope did not select
        key = (bu_id, line.product_id)
        demand_by_key[key] += line.quantity
        if line.ros_date < overdue_cutoff:
            overdue_by_key[key] += line.quantity
        customers_by_key[key].add(customer.id)
        products[line.product_id] = line.product

    # NO EARLY RETURN WHEN THERE IS NO DEMAND.
    #
    # An earlier version bailed out here with `available=False` ("nothing to net
    # against"). That was backwards. This block's denominator is INVENTORY, so a
    # Business Unit holding steel that nothing in the window asks for does not
    # have an unanswerable question -- it has the most emphatic possible answer:
    # all of it is idle. Refusing to report that hid exactly the situation the
    # block exists to surface. Emptiness is now decided at the end, on whether any
    # on-hand position was found at all.

    # Customer-owned stock, per key -- reuses `inventory.customer_owned_map`
    # (the ONE customer-owned reader) rather than re-querying
    # `CustomerOwnedInventory` here.
    customer_cache: dict[str, Customer] = {}
    customer_owned_by_key: dict[tuple[str, str], float] = {}
    for key, customer_ids in customers_by_key.items():
        _bu_id, product_id = key
        total_owned = 0.0
        for cid in customer_ids:
            customer = customer_cache.get(cid)
            if customer is None:
                customer = db.get(Customer, cid)
                customer_cache[cid] = customer
            position = customer_owned_map(db, customer, {product_id})[product_id]
            total_owned += position.drawable
        customer_owned_by_key[key] = total_owned

    # On-hand, per BU -- reuses `company_inventory.get_position` (the ONE on-hand
    # reader with the known-vs-unknown rule already implemented).
    #
    # THE UNIVERSE OF PRODUCTS IS THE ON-HAND POSITION, NOT THE DEMAND.
    # ----------------------------------------------------------------
    # This is the whole correctness of the block and it is easy to get backwards.
    # An earlier version built this set from `demand_by_key`, i.e. only products
    # something in the window was asking for. Every product with stock on the
    # ground and NO demand in the window then vanished from the report -- and
    # that stock is precisely the idle steel this block was written to find. The
    # symptom was that `tied + not_tied` did not add up to the Business Unit's
    # actual on-hand total, and that the total moved when the horizon moved,
    # which a yard position obviously cannot do.
    #
    # Demand keys are UNIONED in on top, so a product that is demanded but has no
    # `InventoryOnHand` row still reaches the unknown-position branch below rather
    # than being silently dropped.
    positions_by_bu: dict[str, object] = {}
    products_by_bu: dict[str, set[str]] = defaultdict(set)
    for bu_id in bu_ids:
        position = get_position(db, bu_id)
        positions_by_bu[bu_id] = position
        for on_hand_row in position.on_hand:
            products_by_bu[bu_id].add(on_hand_row.product_id)
    for bu_id, product_id in demand_by_key:
        products_by_bu[bu_id].add(product_id)

    # `products` was populated from the demand lines only. The on-hand-only
    # products just added have no entry yet, and `_tonnes_measure` looks up
    # `products[product_id]` for the weight, so fill them in here.
    missing_product_ids = {
        product_id
        for product_ids in products_by_bu.values()
        for product_id in product_ids
        if product_id not in products
    }
    if missing_product_ids:
        for product in (
            db.query(Product).filter(Product.id.in_(missing_product_ids)).all()
        ):
            products[product.id] = product

    utilisation_products: list[InventoryUtilisationProduct] = []
    unknown_position: list[UnknownPositionProduct] = []
    tied_pairs: list[tuple[UnitOfMeasure, float]] = []
    not_tied_pairs: list[tuple[UnitOfMeasure, float]] = []

    for bu_id, product_ids in products_by_bu.items():
        position = positions_by_bu[bu_id]
        on_hand_by_product = {row.product_id: row for row in position.on_hand}
        for product_id in product_ids:
            row = on_hand_by_product.get(product_id)
            if row is None:
                # This product touched none of the three Oracle-projection
                # tables in this BU at all -- ask explicitly, per
                # `get_position`'s own contract ("an explicit ask must always
                # get an explicit answer, unknown or not").
                row = get_position(db, bu_id, product_id).on_hand[0]

            if not row.known:
                unknown_position.append(
                    UnknownPositionProduct(
                        business_unit_id=bu_id,
                        product_id=product_id,
                        product_description=row.product_description,
                        unit_of_measure=row.unit_of_measure,
                    )
                )
                continue

            key = (bu_id, product_id)
            on_hand = row.quantity or 0.0
            # `.get`, not `[]`: a product held in the yard with NOTHING asking for
            # it in this window has no demand key at all, and that is a real and
            # important answer (all of it is idle), not a missing one. Indexing a
            # defaultdict here would also have quietly grown `demand_by_key` while
            # iterating over a set derived from it.
            demand = demand_by_key.get(key, 0.0)
            owned = customer_owned_by_key.get(key, 0.0)
            tied = min(on_hand, max(0.0, demand - owned))
            not_tied = on_hand - tied
            product_row = products.get(product_id)
            utilisation_products.append(
                InventoryUtilisationProduct(
                    business_unit_id=bu_id,
                    product_id=product_id,
                    product_description=row.product_description,
                    unit_of_measure=row.unit_of_measure,
                    on_hand_quantity=on_hand,
                    demand_in_window=demand,
                    demand_overdue=overdue_by_key.get(key, 0.0),
                    customer_owned_quantity=owned,
                    tied=tied,
                    not_tied=not_tied,
                    # A zero on either side is a measured 0 t, not "nothing
                    # to convert" -- the honest-refusal path is reserved for
                    # rows that genuinely cannot convert.
                    tied_tonnes=(
                        Measure(available=True, value=0.0)
                        if tied <= 0
                        else pairs_to_tonnes(
                            [(product_row, tied)] if product_row is not None else [],
                            "tied",
                        )
                    ),
                    not_tied_tonnes=(
                        Measure(available=True, value=0.0)
                        if not_tied <= 0
                        else pairs_to_tonnes(
                            [(product_row, not_tied)] if product_row is not None else [],
                            "not tied",
                        )
                    ),
                )
            )
            tied_pairs.append((row.unit_of_measure, tied))
            not_tied_pairs.append((row.unit_of_measure, not_tied))

    tied_by_unit, _ = quantity_by_unit(tied_pairs)
    not_tied_by_unit, _ = quantity_by_unit(not_tied_pairs)

    tied_tonnes, tied_unconvertible = _tonnes_measure(
        utilisation_products, products, "tied", "tied"
    )
    not_tied_tonnes, not_tied_unconvertible = _tonnes_measure(
        utilisation_products, products, "not_tied", "not_tied"
    )

    reasons: list[str] = []
    if unknown_position:
        reasons.append(
            f"{len(unknown_position)} product(s) with in-scope demand have NO "
            "InventoryOnHand row in their Business Unit, so their position is "
            "UNKNOWN. They are excluded from tied/not_tied and listed in "
            "`unknown_position` rather than treated as zero."
        )
    if not utilisation_products and not unknown_position:
        # Genuinely nothing to report: no product in scope has an on-hand row and
        # none is demanded either. `available=False` below, and an unavailable
        # figure must always carry its reason rather than render as a bare zero.
        reasons.append(
            "No product in the Business Unit(s) in scope has an InventoryOnHand "
            "row, and no in-scope demand falls inside the window, so there is no "
            "inventory position to report as tied or idle."
        )
    elif not demand_by_key:
        # Stock, but nothing asking for it. A real answer, and the one most worth
        # saying out loud -- so it is said, rather than left implicit in a 100%
        # not-tied figure a reader might mistake for a rendering fault.
        reasons.append(
            f"No in-scope demand has an ROS date inside the next "
            f"{horizon_months} months, so ALL company-owned stock in scope is "
            "idle over this window."
        )

    return InventoryUtilisation(
        horizon_months=horizon_months,
        available=bool(utilisation_products) or bool(unknown_position),
        tied_tonnes=tied_tonnes,
        not_tied_tonnes=not_tied_tonnes,
        products=tuple(
            sorted(utilisation_products, key=lambda p: (p.business_unit_id, p.product_id))
        ),
        tied_by_unit=tied_by_unit,
        not_tied_by_unit=not_tied_by_unit,
        unconvertible=tuple(tied_unconvertible + not_tied_unconvertible),
        unknown_position=tuple(
            sorted(unknown_position, key=lambda p: (p.business_unit_id, p.product_id))
        ),
        unknown_position_count=len(unknown_position),
        reason=(" ".join(reasons) or None),
    )


def executive_summary(
    db: Session,
    customer_id: str | None = None,
    allocation_horizon_months: int = 12,
    now: datetime | None = None,
    business_unit_id: str | None = None,
    inventory_utilisation_horizon_months: int = 12,
) -> ExecutiveSummary:
    """Every block, for one scope. `resolve_scope` decides the scope, once.

    `allocation_horizon_months` keeps its name: it is the wire parameter the UI
    already sends, and it now selects the SOFT ALLOCATION COVERAGE horizon. The
    figure it drives changed; the selector did not.

    `inventory_utilisation_horizon_months` is the SEPARATE selector for the
    inventory-utilisation block, reusing the same `ALLOCATION_HORIZONS` set of
    legal values -- see `inventory_utilisation` for why its horizon is a
    genuinely different question from the allocation one and therefore a
    genuinely different parameter, not a shared one.
    """
    now = now or datetime.utcnow()
    scope = resolve_scope(
        db, customer_id=customer_id, business_unit_id=business_unit_id
    )

    trend = demand_trend(
        db, customer_id=customer_id, now=now, business_unit_id=business_unit_id
    )
    risk = supply_risk(db, customer_id=customer_id, business_unit_id=business_unit_id)
    soft_allocation = soft_allocation_coverage(
        db,
        horizon_months=allocation_horizon_months,
        customer_id=customer_id,
        now=now,
        business_unit_id=business_unit_id,
    )
    coverage = coverage_summary(
        db, customer_id=customer_id, business_unit_id=business_unit_id
    )
    runout = first_runout_detail(
        db, customer_id=customer_id, business_unit_id=business_unit_id
    )
    incoming = incoming_supply(
        db, customer_id=customer_id, business_unit_id=business_unit_id, now=now
    )
    utilisation = inventory_utilisation(
        db,
        customer_id=customer_id,
        business_unit_id=business_unit_id,
        horizon_months=inventory_utilisation_horizon_months,
        now=now,
    )

    notes = [
        f"Scope: {scope.label}. {scope.description}",
        ON_ORDER_NOTE,
        "Coverage is measured by DEMAND QUANTITY. The well counts beside it are "
        "reference figures and the two routinely disagree -- one well short by 50 "
        "and one short by 40 000 count alike by well and nothing alike by quantity.",
        "Every block carries an `available` flag. When it is false the figure "
        "could not be computed from available data and no number is supplied "
        "-- a zero is never substituted for an unknown.",
    ]
    # Surfaced at the TOP level, not only inside the block that hit it: a reader
    # skimming the headline totals is exactly the reader who needs to be told that
    # one of them is not a single number.
    mixed_anywhere = (
        any(h.unit_of_measure is None and h.quantities_by_unit for h in trend.horizons)
        or (risk.unit_of_measure is None and bool(risk.quantities_by_unit))
        or (
            coverage.available
            and coverage.unit_of_measure is None
            and bool(coverage.total_quantities_by_unit)
        )
        or (soft_allocation.available and soft_allocation.unit_of_measure is None)
        or (incoming.available and incoming.unit_of_measure is None)
    )
    if mixed_anywhere:
        notes.append(MIXED_UNITS_NOTE)

    return ExecutiveSummary(
        generated_at=now,
        customer_id=customer_id,
        business_unit_id=business_unit_id,
        business_unit_name=scope.business_unit_name,
        customer_name=scope.customer_name,
        scope=scope,
        demand_trend=trend,
        coverage=coverage,
        supply_risk=risk,
        soft_allocation_coverage=soft_allocation,
        first_runout=runout,
        incoming_supply=incoming,
        inventory_utilisation=utilisation,
        notes=tuple(notes),
    )
