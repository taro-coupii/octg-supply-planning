from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.engines.allocation import allocate_detailed
# The two filters, and the resolver that decides which pair is actually in force.
#
# THE CONSTANTS ARE RE-EXPORTED FROM HERE ON PURPOSE. They were defined in this
# module for most of the platform's life and are imported from it by
# `app.api.wells`, `app.api.demand`, `app.api.dashboard`, `app.engines.mrp`,
# `app.engines.executive`, `app.engines.sharing`, `app.engines.scenario`,
# `app.engines.well_dates`, `app.engines.coverage_view` and
# `tests/test_coverage_engine.py`. They moved to `app.engines.coverage_scope`
# because eight of those modules need the filters and none of them needs the
# coverage engine, and the re-export keeps every one of those imports -- and the
# test that PINS the two sets -- working unchanged.
#
# They are now the SHIPPED FALLBACK, not the answer. The answer is whatever
# `effective_status_filter` / `effective_profile_filter` resolve for the session,
# which is the persisted platform-wide default when an administrator has set one.
# See `app.engines.coverage_scope` for why a constant could not have become the
# answer itself.
from app.engines.coverage_scope import (  # noqa: F401  (re-exported)
    DEFAULT_PROFILE_FILTER,
    DEFAULT_STATUS_FILTER,
    effective_profile_filter,
    effective_status_filter,
)
from app.engines.inventory import ownership_pool_map, scoped_customer_ids
from app.engines.order_dates import is_recoverable, order_feasibility
from app.engines.overrides import NO_OVERRIDES, LineView, OverrideResolver
from app.engines.substitution import (
    BLOCK_INVENTORY,
    BLOCK_ORACLE_RELEASE,
    RECOMMENDED_ACTION,
    PendingSubstituteLoad,
    SubstitutionApprovalStatus,
    _approval_by_date_for_product_ros,
    find_candidates,
)
from app.models import (
    AllocationPolicy,
    CoverageResult,
    CoverageStatus,
    Customer,
    DemandLine,
    DemandProfile,
    DemandRevision,
    DemandStatus,
    ImpactRecord,
    InventoryAssignment,
    PlanningNode,
    Product,
    TechnicalSubstitution,
    Well,
)

# The two filters, the asymmetry between them, the shipped values and the
# consequences of changing them are all documented ONCE, in
# `app.engines.coverage_scope`, alongside the row that can now override them.
# Nothing is repeated here: a second copy of that reasoning is a second copy to
# fall out of date, and the filters no longer live in this module.
#
# What this engine needs to say about them is only this: `status_filter` and
# `profile_filter` default to `None` on every entry point below, and `None` means
# "resolve the platform's CURRENT default for this session". It does NOT mean the
# shipped constant. A caller that passes an explicit pair is asking a deliberate
# ad-hoc question (`app.engines.coverage_view`'s read-only projection is the one
# in-tree example) and gets exactly what it asked for.


def _shortage_phrase(
    policy: AllocationPolicy,
    line: DemandLine,
    assigned_qty: float,
    standalone: bool,
    drawn_from_pool: float = 0.0,
    reserved_elsewhere: float = 0.0,
    drawn_from_customer_owned: float = 0.0,
) -> str:
    """Why this line's own product could not cover it, worded for the policy.

    Under HARD "insufficient on-hand inventory" would be an outright lie -- the
    stock is very often sitting there, just earmarked for somebody else -- so the
    reason has to name the assignment, not the shelf. `standalone` distinguishes
    the sentence used on its own from the clause that gets a substitute note
    appended.

    `drawn_from_pool` is the PARTIAL draw this line took in ROS order without
    completing itself (see app.engines.allocation). It has to be stated: the
    quantity is genuinely gone, a later line will find it missing, and a reason
    that said only "insufficient inventory" would leave a planner unable to
    explain where the steel went.

    `reserved_elsewhere` is the quantity of this line's OWN product that exists in
    this Business Unit but is hard-assigned to a demand line belonging to ANOTHER
    customer, so it was carved out before allocating (see `_assignment_context`).
    Under SOFT that carve-out is new, and the reason has to follow it: the shelf is
    not empty, the steel is spoken for. Saying "insufficient on-hand inventory"
    there would be the same lie HARD's wording was written to avoid, so SOFT now
    names the assignment for exactly the same reason. When releasing the assignment
    would actually close the gap, the sentence also carries the Oracle action --
    `app.engines.substitution.RECOMMENDED_ACTION[BLOCK_ORACLE_RELEASE]` verbatim,
    so the substitute path and the own-product path give a planner ONE instruction
    rather than two paraphrases of it.

    `drawn_from_customer_owned` is the quantity taken from the CUSTOMER'S OWN
    uploaded stock, which is drawn before anything else under every policy (see
    `app.engines.allocation`). It HAS to be stated, and for a sharper reason than
    the pool draw: a planner looking at a short line who knows their own material
    was on the dock will assume the platform ignored it. Saying only "insufficient
    on-hand inventory" would send them to argue about the wrong thing -- and it
    would be a lie of the same kind HARD's wording was written to avoid, because the
    steel was not ignored, it was spent and it was not enough. Every policy's
    sentence therefore leads with the customer-owned draw when there was one, and
    every "still short by" figure nets it off.
    """
    owned = max(0.0, drawn_from_customer_owned)
    # The one place the customer-owned draw is worded, so all three policies say it
    # the same way. `first` is stated explicitly because the PRIORITY is the rule
    # the product owner asked for, and a planner cannot verify a rule they are not
    # told was applied.
    owned_clause = (
        f"{owned:g} of {line.quantity:g} drawn from this customer's OWN "
        f"customer-owned stock first (customer-owned inventory is consumed ahead of "
        f"company inventory)"
        if owned > 0
        else ""
    )

    if policy == AllocationPolicy.SOFT:
        if reserved_elsewhere > 0:
            label = line.product.description or line.product.id
            base = (
                f"Insufficient FREE on-hand inventory for requested ROS: "
                f"{reserved_elsewhere:g} of {label} in this Business Unit is "
                "hard-assigned to another customer's demand line, so it is not "
                "available to this line"
            )
            if owned_clause:
                base += f"; {owned_clause}"
            if drawn_from_pool > 0:
                base += (
                    f"; {drawn_from_pool:g} of {line.quantity:g} drawn from the "
                    f"unassigned remainder in ROS order, still short by "
                    f"{line.quantity - drawn_from_pool - owned:g}"
                )
            elif owned_clause:
                base += f", still short by {line.quantity - owned:g}"
            # Only claim an Oracle release would help when it actually would --
            # otherwise the material is short regardless and pointing at Oracle
            # would send a planner to argue about an assignment that cannot save
            # them. Same test the substitute path applies (see
            # `app.engines.substitution.find_candidates`, `release_would_close`).
            if (
                drawn_from_pool + owned + reserved_elsewhere >= line.quantity
                and standalone
            ):
                base += (
                    "; releasing it would close the gap -- "
                    f"{RECOMMENDED_ACTION[BLOCK_ORACLE_RELEASE]}"
                )
            return base

        if owned_clause:
            # A different sentence, not a decorated one. "Insufficient on-hand
            # inventory" is about company stock; this line's problem is that its own
            # material ran out too, and the lead clause has to say so or the reason
            # reads as though the upload had been ignored.
            base = (
                "Insufficient inventory for requested ROS even after customer-owned "
                f"stock was drawn first: {owned_clause}"
            )
        else:
            base = (
                "Insufficient on-hand inventory for requested ROS"
                if standalone
                else "Insufficient on-hand inventory"
            )
        if drawn_from_pool > 0:
            base += (
                f"; {drawn_from_pool:g} of {line.quantity:g} drawn from the pool in "
                f"ROS order, still short by "
                f"{line.quantity - drawn_from_pool - owned:g}"
            )
        elif owned_clause:
            base += f", still short by {line.quantity - owned:g}"
        return base

    if policy == AllocationPolicy.HARD:
        # The customer-owned clause comes FIRST here too. Under HARD it is doing
        # something the wording must not obscure: it is the ONE source that covers a
        # hard line with no Oracle assignment (see `app.engines.allocation`), so a
        # planner reading "no inventory assigned" alone would conclude, wrongly, that
        # their uploaded stock had been ignored on policy grounds.
        prefix = f"{owned_clause}; " if owned_clause else ""
        if assigned_qty <= 0:
            return (
                f"{prefix}No inventory assigned to this demand line; under hard "
                "allocation unassigned pool stock does not provide coverage"
            )
        return (
            f"{prefix}Only {assigned_qty:g} of {line.quantity:g} assigned to this "
            "demand line; hard allocation permits no top-up from the unassigned pool"
        )

    # HYBRID
    prefix = f"{owned_clause}; " if owned_clause else ""
    if assigned_qty <= 0:
        # Wording is unchanged when nothing was customer-owned, so the sentence a
        # planner already knows is not reworded for a case that did not occur.
        want = (
            f"the remaining {line.quantity - owned:g}" if owned > 0
            else f"{line.quantity:g}"
        )
        base = (
            f"{prefix}Nothing assigned to this demand line and the unassigned pool "
            f"cannot cover {want} at this ROS"
        )
        if drawn_from_pool > 0:
            base += (
                f" ({drawn_from_pool:g} drawn from the pool in ROS order, still "
                f"short by {line.quantity - drawn_from_pool - owned:g})"
            )
        return base

    capped = min(assigned_qty, line.quantity - owned)
    base = f"{prefix}Assigned {assigned_qty:g} of {line.quantity:g}"
    if drawn_from_pool > 0:
        return (
            f"{base}; the unassigned pool provided a further {drawn_from_pool:g} in "
            f"ROS order but the line is still short by "
            f"{line.quantity - owned - capped - drawn_from_pool:g} at this ROS"
        )
    return (
        f"{base}; unassigned pool cannot cover the remaining "
        f"{line.quantity - owned - capped:g} at this ROS"
    )


def _assignment_context(
    db: Session,
    policy: AllocationPolicy,
    included: list[DemandLine],
    customer: Customer,
    resolver: OverrideResolver = NO_OVERRIDES,
) -> tuple[dict[tuple[str, str], float], dict[str, float]]:
    """Load the Oracle-projected inventory assignments this pass needs.

    Returns:
      assigned_by_line     {(demand_line_id, product_id): qty} for `included`
      reserved_by_product  {product_id: total qty assigned WITHIN THIS SCOPE}

    SOFT: OWN assignments ignored, FOREIGN assignments carved out

    (C-09 resolved 2026-08-12: the product owner ruled the own-product path
    must deduct foreign hard assignments exactly as the substitute path does.
    That is what this function already implements; the regression tests are
    tests/test_allocation_policies.py::test_soft_is_constrained_by_another_customers_hard_assignment
    and its siblings.)
    ------------------------------------------------------------
    Under SOFT the two maps answer two DIFFERENT questions, and the distinction is
    the whole point:

      * `assigned_by_line` is `{}`. An assignment on one of THIS customer's own
        lines must not grant it coverage and must not be carved out of its own
        pool, because that is precisely what soft allocation means -- "do not tie
        my steel to specific wells of mine". Pooling is preserved exactly.
      * `reserved_by_product` carries assignments on demand lines of OTHER
        customers in this Business Unit, and this customer's own rows are excluded
        entirely. That steel belongs to somebody else's demand line, and quietly
        allocating it would be the platform overriding an Oracle fact -- the one
        thing it must never do. Before this, SOFT returned `({}, {})` and a soft
        line could be reported COVERED off steel hard-assigned to another
        customer, which is the same violation the substitute path already blocks,
        one level up.

    The exclusion of this customer's own rows is load-bearing arithmetic, not
    tidiness. `compute_customer_coverage` computes
    `reserved_elsewhere = reserved_by_product[P] - sum(own_assigned.values())`, and
    under SOFT `own_assigned` is all zeros because `assigned_by_line` is empty, so
    whatever this function puts in `reserved_by_product` is subtracted IN FULL.
    Returning the BU-wide total would therefore carve out this customer's own
    assignments too, breaking pooling in the very case it is supposed to protect.

    HARD and HYBRID are unchanged: `assigned_by_line` grants coverage, and
    `reserved_by_product` is the BU-wide total (their own rows included), which the
    subtraction above then removes again so only genuinely foreign reservations net
    off. That is what keeps "reserved stock is never free pool" true for them even
    when the reserving line stayed uncovered.

    Scenario ASSIGNMENT overrides are applied here, and only here
    ------------------------------------------------------------
    `resolver` (app.engines.overrides) may restate the quantity assigned to an
    in-pool line. When it does, the change is reflected in BOTH returned maps:
    `assigned_by_line` gets the new figure, and `reserved_by_product` is adjusted
    by the DELTA so the "reserved stock is never free pool" invariant survives --
    raising an assignment must take that quantity out of the unassigned pool, not
    conjure it from nowhere.

    Two guards worth naming. An override on a line OUTSIDE this pool is ignored
    (it is not this pass's business, and honouring it would let a scenario reach
    into another customer's reservations). And under SOFT this function returns
    before the resolver is ever consulted: an override may only restate an
    assignment on an IN-POOL line, and under SOFT an in-pool assignment changes
    nothing, so an assignment override on a soft customer correctly changes
    nothing. Foreign reservations stay exactly as Oracle projected them -- a
    scenario cannot release somebody else's assignment either.

    `reserved_by_product` is BU-SCOPED, not system-wide
    --------------------------------------------------
    It used to sum every InventoryAssignment row in the database. That was a
    cross-BU leak in the pessimistic direction: a reservation made against a
    demand line in a DIFFERENT Business Unit -- drawing on that BU's completely
    separate physical stock -- was netted off this BU's on-hand figure and could
    push a line to Uncovered for no physical reason. Rows are now restricted to
    demand lines owned by customers in this customer's inventory scope
    (`app.engines.inventory.scoped_customer_ids`: the whole BU -- which raises for
    an unmapped customer, since it has no pool). Reservations inside the scope
    still net off in full, which is what keeps "reserved stock is never free pool"
    true. Under SOFT the scope is narrowed one step further -- the BU MINUS this
    customer -- for the arithmetic reason given above.
    """
    soft = policy == AllocationPolicy.SOFT
    scope_ids = scoped_customer_ids(db, customer)
    line_ids = {line.id for line in included}
    assigned_by_line: dict[tuple[str, str], float] = defaultdict(float)
    reserved_by_product: dict[str, float] = defaultdict(float)
    query = (
        db.query(InventoryAssignment)
        .join(DemandLine, InventoryAssignment.demand_line_id == DemandLine.id)
        .join(Well, DemandLine.well_id == Well.id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id.in_(scope_ids))
    )
    if soft:
        # FOREIGN only. This is the own-vs-foreign distinction, expressed once, in
        # the query -- not as a subtraction afterwards, so there is no arithmetic
        # for a later edit to get half-right.
        query = query.filter(PlanningNode.customer_id != customer.id)
    for row in query.all():
        qty = max(0.0, row.quantity or 0.0)
        reserved_by_product[row.product_id] += qty
        if not soft and row.demand_line_id in line_ids:
            assigned_by_line[(row.demand_line_id, row.product_id)] += qty

    if soft:
        # No `assigned_by_line` (pooling), and nothing here for a scenario
        # assignment override to restate -- see the docstring.
        return {}, dict(reserved_by_product)

    for (line_id, product_id), new_qty in resolver.assignment_overrides().items():
        if line_id not in line_ids:
            continue
        old_qty = assigned_by_line.get((line_id, product_id), 0.0)
        assigned_by_line[(line_id, product_id)] = max(0.0, new_qty)
        reserved_by_product[product_id] = max(
            0.0, reserved_by_product.get(product_id, 0.0) - old_qty + max(0.0, new_qty)
        )

    return dict(assigned_by_line), dict(reserved_by_product)


def _substitute_target_ids(db: Session, product_ids: set[str]) -> set[str]:
    """Products reachable as a technical substitute from any of `product_ids`.

    Used to seed the substitution fall-through's availability map with
    BU-RESOLVED quantities, so a substitute is offered at the quantity THIS BU
    holds. It also means an unstocked substitute is refused loudly (see
    `app.engines.inventory`) rather than silently offered at somebody else's
    quantity, which is what the removed global scalar used to do -- most visibly
    under SOFT, where `reserved_by_product` is empty by design.
    """
    if not product_ids:
        return set()
    return {
        to_id
        for (to_id,) in db.query(TechnicalSubstitution.to_product_id).filter(
            TechnicalSubstitution.from_product_id.in_(sorted(product_ids))
        )
    }


def _hard_assigned_for_substitutes(
    db: Session,
    customer: Customer,
    product_ids: set[str],
) -> dict[str, float]:
    """{product_id: qty hard-assigned to a FOREIGN demand line in this BU} for
    SUBSTITUTE-TARGET products only.

    Why this exists separately from `_assignment_context`
    ----------------------------------------------------
    `_assignment_context` returns no `assigned_by_line` under SOFT by design, and
    it only ever covers products with a reservation in scope. This query answers
    the narrower substitute question for products this pool does not itself demand,
    where a hard assignment used to be invisible to a soft customer. Combined with
    the BU-resolved substitute seed, a soft customer's line could be reported
    COVERED_VIA_SUBSTITUTE off stock hard-assigned to somebody else's demand --
    i.e. the platform silently overriding a hard reservation, the one thing it
    must never do.

    The owner's ruling is narrow: the stock may be OFFERED as a candidate, but
    the platform may not take it -- "the user must go into Oracle
    themselves and release the hard assignment". So this query is deliberately
    narrow too. It is used ONLY
    for the substitution fall-through, for products this pool does not itself
    demand.

    WHOSE assignment counts, and why `max` (not a sum) is still correct
    ------------------------------------------------------------------
    This function applies the SAME own-vs-foreign rule as `_assignment_context`:
    under SOFT, this customer's own rows are excluded (pooling -- a soft customer
    is not blocked by its own assignment of a substitute to one of its own wells),
    and under HARD/HYBRID the whole BU counts.

    That agreement is what keeps the combination in
    `compute_customer_coverage` sound. The two figures are combined with `max`, and
    `max` is right precisely BECAUSE they now mean the same thing wherever they
    overlap:

      HARD/HYBRID  `reserved_by_product[P]` is the BU total and so is this; `max`
                   of two equal numbers excludes the steel exactly ONCE.
      SOFT         both are foreign-only; `max` of two equal numbers again
                   excludes it exactly once.

    Summing them would exclude the same quantity TWICE and manufacture a shortage
    out of arithmetic. The reason both are still consulted rather than one being
    deleted is coverage of the KEY SET, not of the quantity: `reserved_by_product`
    spans every product with a reservation in scope, this one spans every
    substitute-target product, and neither is a superset of the other.

    Scoped to the Business Unit for the same reason `_assignment_context` is: a
    reservation in another BU draws on another BU's physical stock and must not
    shrink this one.
    """
    if not product_ids:
        return {}
    scope_ids = scoped_customer_ids(db, customer)
    out: dict[str, float] = defaultdict(float)
    query = (
        db.query(InventoryAssignment)
        .join(DemandLine, InventoryAssignment.demand_line_id == DemandLine.id)
        .join(Well, DemandLine.well_id == Well.id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(
            PlanningNode.customer_id.in_(scope_ids),
            InventoryAssignment.product_id.in_(sorted(product_ids)),
        )
    )
    if customer.allocation_policy == AllocationPolicy.SOFT:
        query = query.filter(PlanningNode.customer_id != customer.id)
    for row in query.all():
        out[row.product_id] += max(0.0, row.quantity or 0.0)
    return dict(out)


def _customer_of(well: Well) -> Customer:
    return well.planning_node.customer


def _customer_wells(db: Session, customer: Customer) -> list[Well]:
    """Every well under `customer`, i.e. the whole inventory pool scope.

    PlanningNode carries `customer_id` on every node in the hierarchy, so this
    is a flat join -- no recursive walk of parent_id is needed.
    """
    return (
        db.query(Well)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id == customer.id)
        .all()
    )


def recompute_well(
    db: Session,
    well: Well,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
) -> "CustomerCoverage":
    """Recompute coverage for `well` -- the public, well-centric entry point.

    `status_filter` / `profile_filter` are passed straight through, `None` and all
    -- resolution happens once, in `compute_customer_coverage`. Resolving here as
    well would be a second reading of the setting on one code path and is exactly
    the kind of duplicate that drifts.

    Users think in wells, so this signature is the stable one and the callers
    (apply_revision, decide_approval, the seed) keep using it. It is now a thin
    wrapper: inventory cannot honestly be allocated one well at a time, so the
    call is widened to the well's whole CUSTOMER pool via `recompute_customer`
    and every well of that customer is re-rolled-up. Triggering on one well
    therefore still produces globally-correct results for all of them, and the
    result for THIS well is identical to what a pool-wide recompute would give.
    """
    return recompute_customer(
        db,
        _customer_of(well),
        status_filter=status_filter,
        profile_filter=profile_filter,
    )


@dataclass(frozen=True)
class LineCoverage:
    """One demand line's computed verdict, before anything is persisted.

    Values only -- no ORM instance, so a consumer cannot reach through it and
    mutate a mapped attribute. `quantity` and `ros_date` are the EFFECTIVE values
    the verdict was computed from, which differ from the persisted row when a
    scenario override is in play; carrying them here is what lets the scenario
    preview show its arithmetic.
    """

    demand_line_id: str
    well_id: str
    status: CoverageStatus
    reason: str | None
    fulfilled_by_product_id: str | None
    product_id: str
    quantity: float
    ros_date: datetime
    #: THE NET POSITION of this line (adversarial review 2026-09-06, F04).
    #:
    #: `quantity` is what the line demands; the three `drawn_*` figures are what
    #: the pass actually took for it, out of the SAME AllocationOutcome / substitute
    #: draw that produced `status`; `residual` is what nobody has steel for. A
    #: partially drawn Uncovered line has residual < quantity, and that residual
    #: -- not the whole line -- is what a mill order has to cover. MRP reads it;
    #: before this it re-ordered the whole line and double-counted the draw.
    #:
    #: `drawn_company` is pool + own assignment together: both are company steel.
    #: `drawn_substitute` is the whole line for CoveredViaSubstitute, which is
    #: exactly what the fall-through charges to the substitute's stock.
    drawn_customer_owned: float = 0.0
    drawn_company: float = 0.0
    drawn_substitute: float = 0.0
    residual: float = 0.0


@dataclass(frozen=True)
class CustomerCoverage:
    """The complete coverage answer for one customer pool -- computed, unwritten.

    An INTERNAL engine type, not an API shape: `included_views` holds
    `LineView`s, which wrap live ORM rows so the persisting caller can write
    against them and so MRP can be reused verbatim. Callers that hand results to
    the outside world (app.engines.scenario) must project this into their own
    frozen scalar dataclasses rather than pass it through.
    """

    customer_id: str
    #: {demand_line_id: LineCoverage} for every INCLUDED line.
    by_line: dict[str, LineCoverage]
    #: Lines a status/profile filter excluded -- evaluated by nothing, and whose
    #: persisted CoverageResult (if any) is stale misinformation. See below.
    excluded_line_ids: tuple[str, ...]
    #: {well_id: coverage_status value or None} for EVERY well of the customer.
    well_status: dict[str, str | None]
    #: The included lines as the engine saw them, for reuse by app.engines.mrp.
    included_views: tuple[LineView, ...]
    #: Every line of the pool, included or not, as the engine saw it.
    all_views: tuple[LineView, ...]
    #: PendingApproval load per substitute product -- the over-subscription
    #: surfacing that replaced the pending-substitute RESERVATION. One entry per
    #: substitute any line is pending on, over-subscribed or not, so a consumer
    #: can render the healthy case too. See app.engines.substitution
    #: .PendingSubstituteLoad.
    pending_substitute_load: tuple[PendingSubstituteLoad, ...] = ()
    #: {product_id: qty that exists in this BU but is hard-assigned to another
    #: demand line}, for substitute-target products. Handed to `find_candidates`
    #: so a caller outside this pass (the substitution-candidates route) can
    #: annotate candidates identically. See `_hard_assigned_for_substitutes`.
    hard_assigned_by_product: dict[str, float] = field(default_factory=dict)
    #: {demand_line_id: qty drawn from the shared UNASSIGNED pool of the line's own
    #: product}, straight from `app.engines.allocation.AllocationOutcome
    #: .consumed_from_pool`. Includes PARTIAL draws that did not cover the line.
    #:
    #: This is what "soft allocation" means in quantity terms -- steel taken from a
    #: pool nobody had earmarked -- and it is surfaced HERE rather than recomputed
    #: anywhere, because there is exactly one implementation of the allocation rules
    #: (see `compute_customer_coverage`). The Executive Dashboard's soft-allocation
    #: coverage block reads this map; it does not run allocation a second time.
    consumed_from_pool: dict[str, float] = field(default_factory=dict)
    #: {demand_line_id: qty drawn from the line's OWN Oracle assignment}, from
    #: `AllocationOutcome.consumed_from_assignment`. Empty under SOFT by
    #: construction: `_assignment_context` hands no assignments to a soft pass,
    #: which is exactly the pooling guarantee.
    consumed_from_assignment: dict[str, float] = field(default_factory=dict)
    #: {demand_line_id: qty drawn from the CUSTOMER'S OWN uploaded stock of the
    #: line's own product}, from `AllocationOutcome.consumed_from_customer_owned`.
    #: Drawn before either of the other two under every allocation policy.
    #:
    #: Surfaced here for the same reason the other two are -- there is exactly one
    #: implementation of the allocation rules and nothing may re-derive them -- and
    #: consumed by the Executive Dashboard's `from_customer_owned` channel and by the
    #: coverage reason text. Empty when no customer-owned stock exists, which is the
    #: ordinary state of a database that has not used the upload.
    consumed_from_customer_owned: dict[str, float] = field(default_factory=dict)


def compute_customer_coverage(
    db: Session,
    customer: Customer,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
    resolver: OverrideResolver = NO_OVERRIDES,
) -> CustomerCoverage:
    """THE coverage algorithm. Computes; does not write.

    This function is the single implementation of the coverage rules in the
    platform. `recompute_customer` is this plus persistence;
    `app.engines.scenario.preview` is this plus a `resolver` carrying a
    scenario's overrides. Nothing else may re-derive coverage.

    That split is the whole reason scenario preview can be trusted. A preview
    computed by a SECOND implementation would be a promise made by code that is
    not the code that keeps it -- it would drift, and it would drift silently,
    because a preview has no downstream consumer to notice it was wrong. Here,
    "preview and apply agree" is not a property maintained by tests, it is a
    property of there being one function.

    Reading values through `resolver`
    --------------------------------
    Every fact a scenario is allowed to change is read through
    `app.engines.overrides.OverrideResolver`: demand values via `resolver.view`,
    on-hand quantity via `resolver.on_hand`, assignments via
    `resolver.assigned` / `resolver.assignment_overrides`, and well-layer
    substitution approvals via `resolver.approval`. The OFFICIAL pass is not a
    bypass of that indirection -- it passes `NO_OVERRIDES`, the identity
    resolver, so both paths are the same instructions.

    It writes NOTHING, and must stay that way: `app.engines.scenario` depends on
    that, tests pin it, and a write introduced here would turn every scenario
    preview into a silent mutation of the official verdict.

    See `recompute_customer` for the full documentation of the rules themselves
    -- the two boundaries, the ROS ordering, the allocation policies and where
    the on-hand figure comes from.

    THE SCOPE IS RESOLVED HERE, AND ONLY HERE
    ----------------------------------------
    `None` for either filter means "the platform's CURRENT default", resolved from
    `app.engines.coverage_scope` against THIS session -- which is the persisted
    platform-wide setting when an administrator has adjusted it, and the shipped
    constant otherwise. It does not mean the constant.

    The resolution happens in this one function rather than in each of the three
    entry points, for the same reason there is one coverage implementation: two
    readings of the setting are two things to keep in step, and a
    `recompute_customer` that resolved its own would eventually persist verdicts
    under a scope `compute_customer_coverage` was not using.
    """
    if status_filter is None:
        status_filter = effective_status_filter(db)
    if profile_filter is None:
        profile_filter = effective_profile_filter(db)

    wells = _customer_wells(db, customer)

    # Demand lines are QUERIED rather than read off `well.demand_lines`. The
    # relationship is a cached collection: a well whose lines were loaded during
    # an earlier pass (when it had none) would keep reporting none, silently
    # dropping later-inserted demand out of the pool. A query cannot go stale
    # that way.
    pool_lines = (
        db.query(DemandLine)
        .join(Well, DemandLine.well_id == Well.id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id == customer.id)
        # Deterministic total order (ROS, then id as the tie-break). Without
        # it, the scarce customer-owned tier under HARD -- drawn in caller
        # order -- and exact ROS-date ties under SOFT/HYBRID were decided by
        # whatever order the database happened to return: two identical
        # recomputes could hand the last metre to different wells.
        .order_by(DemandLine.ros_date, DemandLine.id)
        .all()
    )

    # Every line is seen through the resolver, ALWAYS -- including on the
    # official path, where the resolver hands each one straight back. The engine
    # below therefore never touches a DemandLine attribute directly, so there is
    # no second reading of demand for an override to miss.
    views = [resolver.view(line) for line in pool_lines]

    # `view.status` is the effective demand status OF THIS LINE'S WELL (see
    # `app.engines.overrides.LineView`), so this one condition applies both
    # filters at their proper granularity: status by well, profile by line.
    #
    # Excluding a whole well therefore needs no special case, and -- more
    # importantly -- FREES ITS INVENTORY BY THE SAME ARITHMETIC that frees an
    # excluded line's. None of its lines reach `by_product` below, so none of them
    # is passed to `allocate_detailed`, so none of them consumes any of
    # `available`; the whole of that well's requirement stays on the shelf for the
    # wells that are in scope. Its `CoverageResult` rows are deleted by
    # `recompute_customer` (via `excluded_line_ids`) and it rolls up to
    # `coverage_status = None`, i.e. "not evaluated" -- never "covered".
    included: list[LineView] = []
    excluded: list[LineView] = []
    for view in views:
        if view.status in status_filter and view.profile in profile_filter:
            included.append(view)
        else:
            excluded.append(view)

    by_product: dict[str, list[LineView]] = defaultdict(list)
    for view in included:
        by_product[view.product_id].append(view)

    policy = customer.allocation_policy
    assigned_by_line, reserved_by_product = _assignment_context(
        db, policy, included, customer, resolver
    )

    # Every on-hand figure below is BU-resolved up front, in one place. The set
    # covers the products this pool demands, the products a technical
    # substitution could reach from them, anything carrying a reservation, and
    # anything an inventory override names -- i.e. every product whose quantity
    # this pass can possibly consult.
    bu_id = customer.business_unit_id
    demanded_ids = set(by_product)
    quantity_ids = (
        demanded_ids
        | _substitute_target_ids(db, demanded_ids)
        | set(reserved_by_product)
        | set(resolver.inventory_override_product_ids())
    )
    # The BU boundary is applied HERE, before the resolver is consulted, so an
    # inventory override can only ever restate a quantity that already belongs to
    # this customer's Business Unit. There is no argument a scenario could pass
    # that would make this read another BU's stock.
    #
    # `ownership_pool_map` is used rather than `on_hand_map`, and the difference is
    # the whole ownership tier: it returns an `OwnershipPool` per product, carrying
    # the COMPANY-owned quantity (Oracle's projection, BU-scoped, raising when
    # unknown) beside the CUSTOMER-owned quantity (this platform's own data, scoped
    # to the customer, never raising). Both rules are applied in that one function --
    # see `app.engines.inventory` -- so this pass cannot get the tier wrong and no
    # other consumer can get it differently.
    pools = ownership_pool_map(db, customer, quantity_ids)
    # Company tier only, and a scenario INVENTORY override may restate only this
    # one. `OwnershipPool.with_company_owned` is the sole mutator and there is
    # deliberately no counterpart for the customer tier: an INVENTORY override
    # restates an `InventoryOnHand` figure, which Oracle owns, and it must not be
    # able to invent (or delete) a customer's own property. See
    # `app.engines.overrides.OVERRIDE_FIELDS` for why customer-owned stock is not
    # an override target at all.
    on_hand = {
        product_id: resolver.on_hand(product_id, pool.company_owned)
        for product_id, pool in pools.items()
    }
    # {product_id: customer-owned quantity}. Additive to `on_hand`, never a
    # carve-out of it, and never netted against an assignment.
    customer_owned = {
        product_id: pool.customer_owned for product_id, pool in pools.items()
    }

    covered_by_line: dict[str, bool] = {}
    # Which product actually satisfied each line, for CoverageResult
    # .fulfilled_by_product_id. A line covered by its own product maps to that
    # product; a line covered via substitute maps to the substitute.
    fulfilled_by: dict[str, str] = {}
    # Free (unreserved, unconsumed) on-hand per product after the own-product
    # allocation, POOL-WIDE. Shared across the substitution fall-through below so
    # one line's substitute consumption is never double-counted by another line
    # in the same pass -- in any well of this customer -- and, under HARD/HYBRID,
    # so inventory earmarked for some other demand line is never offered up as a
    # substitute either.
    remaining_qty: dict[str, float] = {}
    # Quantity each line drew from the shared unassigned POOL of its own product
    # (its assignment is reported separately in the reason text). Under SOFT/HYBRID
    # this now includes PARTIAL draws that did not cover the line, which the reason
    # text has to state -- see `_shortage_phrase`.
    drawn_from_pool: dict[str, float] = {}
    # Quantity each line drew from its OWN Oracle assignment, kept beside the pool
    # draw so the two halves of "how was this line satisfied" come out of the SAME
    # AllocationOutcome. Reported on CustomerCoverage; nothing recomputes it.
    drawn_from_assignment: dict[str, float] = {}
    # Quantity each line drew from the CUSTOMER'S OWN uploaded stock, out of the same
    # AllocationOutcome as the other two. Drawn first under every policy.
    drawn_from_customer_owned: dict[str, float] = {}
    # {product_id: qty excluded from `remaining_qty` because it is hard-assigned
    # to another demand line}, per product, in ONE meaning for `find_candidates`.
    hard_assigned_by_product: dict[str, float] = {}
    for product_id, lines in by_product.items():
        own_assigned = {
            line.id: assigned_by_line.get((line.id, product_id), 0.0) for line in lines
        }
        # Stock assigned to demand lines OUTSIDE this customer's pool is not
        # available to this pool at all, so it comes off before allocating.
        #
        # Under SOFT `own_assigned` is all zeros (assignments do not grant a soft
        # line coverage), so this subtraction removes nothing and the whole of
        # `reserved_by_product[P]` nets off. That is exactly why
        # `_assignment_context` puts ONLY foreign assignments in it under SOFT --
        # the BU-wide total would carve out this customer's own assignments here
        # too and destroy pooling. Read the two together; neither is correct alone.
        reserved_elsewhere = max(
            0.0, reserved_by_product.get(product_id, 0.0) - sum(own_assigned.values())
        )
        # `on_hand[product_id]` is indexed, not `.get()`-with-a-default: every
        # demanded product is in `quantity_ids`, and `on_hand_map` either returns a
        # BU-resolved quantity for it or raises. A default here would be a second
        # policy for a missing quantity -- which is exactly the shim that was
        # removed.
        available = max(
            0.0,
            resolver.on_hand(product_id, on_hand[product_id]) - reserved_elsewhere,
        )

        outcome = allocate_detailed(
            policy,
            lines,
            available,
            own_assigned,
            # The customer's own stock, passed BESIDE `available` rather than added
            # into it. Adding it would make it indistinguishable from company steel
            # the moment it entered the function, and every downstream sentence and
            # dashboard channel depends on telling the two apart.
            customer_owned.get(product_id, 0.0),
        )
        covered_by_line.update(outcome.covered)
        for line_id, is_covered in outcome.covered.items():
            if is_covered:
                fulfilled_by[line_id] = product_id
            drawn_from_pool[line_id] = outcome.consumed_from_pool.get(line_id, 0.0)
            drawn_from_assignment[line_id] = outcome.consumed_from_assignment.get(
                line_id, 0.0
            )
            drawn_from_customer_owned[line_id] = (
                outcome.consumed_from_customer_owned.get(line_id, 0.0)
            )
        # Both residuals, because a SUBSTITUTE draw may legitimately come from
        # either: the unspent company pool, and the customer's own unspent stock of
        # this product. Both belong to THIS customer's pool and neither can reach
        # another customer -- the company residual because `find_candidates` is only
        # ever asked about this pool's lines, the customer-owned residual because it
        # is that customer's property and is excluded from sharing entirely.
        remaining_qty[product_id] = (
            outcome.remaining_pool + outcome.remaining_customer_owned
        )
        hard_assigned_by_product[product_id] = reserved_elsewhere

    # Products this pool does not demand directly can still be reached as
    # substitutes. Seed EVERY such product from the BU-resolved quantity, net of
    # assignment reservations for the same reason as above.
    #
    # Seeding all of them (not merely the ones carrying a reservation) is what
    # closed the last BU hole back when `find_candidates` had a global fallback
    # for anything left unseeded. That fallback is gone, so the seed is now also
    # the thing that keeps this pass answerable at all.
    #
    # The two figures are combined with `max`, never a sum: they carry the SAME
    # meaning (foreign-only under SOFT, BU-wide under HARD/HYBRID -- see
    # `_hard_assigned_for_substitutes`) and differ only in which products they
    # cover, so summing would exclude the same steel twice and invent a shortage.
    # `max` excludes it exactly once whichever map knows about it.
    substitute_only_ids = {pid for pid in on_hand if pid not in remaining_qty}
    hard_assigned_subs = _hard_assigned_for_substitutes(
        db, customer, substitute_only_ids
    )
    for product_id, resolved in on_hand.items():
        if product_id in remaining_qty:
            continue
        blocked = max(
            reserved_by_product.get(product_id, 0.0),
            hard_assigned_subs.get(product_id, 0.0),
        )
        hard_assigned_by_product[product_id] = blocked
        # Company stock net of reservations, PLUS whatever this customer owns of the
        # substitute itself. A customer that uploaded stock of a product it does not
        # directly demand may still use it to cover a line the product substitutes
        # for -- it is their property and no reservation can touch it, which is
        # precisely why `blocked` is subtracted only from the company tier.
        remaining_qty[product_id] = (
            max(0.0, resolved - blocked) + customer_owned.get(product_id, 0.0)
        )

    status_by_line: dict[str, CoverageStatus] = {}
    reason_by_line: dict[str, str | None] = {}

    for line in included:
        if covered_by_line.get(line.id, False):
            status_by_line[line.id] = CoverageStatus.COVERED
            reason_by_line[line.id] = None

    # Substitution fall-through: only lines their own product could not cover.
    # Earliest ROS first ACROSS THE WHOLE POOL so the scarce substitute stock
    # goes to the most urgent line regardless of which well it belongs to,
    # matching the pool allocation ordering used above. `remaining_qty` is
    # pool-scoped, so a substitute quantity spent for one well's line is gone for
    # every other well's line too.
    shortfall = sorted(
        (l for l in included if not covered_by_line.get(l.id, False)),
        key=lambda l: l.ros_date,
    )
    # {to_product_id: [LineView]} for lines that ended up PENDING_APPROVAL. A
    # pending substitute now consumes NOTHING (see below), so the scarcity it used
    # to hide has to be made visible after the pass, once every pending line is
    # known.
    pending_lines_by_product: dict[str, list[LineView]] = defaultdict(list)
    for line in shortfall:
        candidates = find_candidates(
            db,
            line,
            available_qty_by_product=remaining_qty,
            approval_override=resolver.approval,
            hard_assigned_by_product=hard_assigned_by_product,
        )

        usable = next((c for c in candidates if c.usable), None)
        if usable is not None:
            remaining_qty[usable.to_product_id] = usable.available_qty - line.quantity
            covered_by_line[line.id] = True
            fulfilled_by[line.id] = usable.to_product_id
            status_by_line[line.id] = CoverageStatus.COVERED_VIA_SUBSTITUTE
            label = usable.product.description or usable.product.id
            reason_by_line[line.id] = f"Covered via approved substitute {label}"
            continue

        # Technically valid + customer-allowed, awaiting (or never requested) a
        # well-level approval, and the substitute could actually close the gap.
        pending = next(
            (
                c
                for c in candidates
                if c.customer_allowed
                and c.approval_status in (None, SubstitutionApprovalStatus.PENDING)
                and c.available_qty >= c.required_qty
            ),
            None,
        )
        if pending is not None:
            # RESERVE NOTHING. A pending substitute holds no quantity back.
            #
            # The previous behaviour decremented `remaining_qty` here so that two
            # lines could not both be promised one quantity. The product owner has
            # REVERSED that: reserving is itself the platform creating a hard
            # reservation, and it must never do that --
            # "Hard allocation cannot be done on the OCTG Platform. On the OCTG
            # Platform everything is strictly soft."
            #
            # The bug that reservation was fixing is real, so it is not simply
            # reintroduced: the over-subscription is made VISIBLE instead, after
            # the pass, in both the reason text and the candidate payload. See
            # `pending_substitute_load` below.
            #
            # The stock is still NOT marked as fulfilling the line -- nothing has
            # been drawn, so fulfilled_by stays unset and MRP's runout keeps
            # charging this line to its own product.
            pending_lines_by_product[pending.to_product_id].append(line)
            label = pending.product.description or pending.product.id
            detail = (
                "approval not yet requested"
                if pending.approval_status is None
                else "approval request pending decision"
            )
            status_by_line[line.id] = CoverageStatus.PENDING_APPROVAL
            base_reason = f"Substitute {label} needs well-level approval ({detail})"
            # Append the approval-by-date clause -- reuses the SAME
            # order_dates/lead_time arithmetic `app.engines.substitution
            # .approval_by_date` exposes on the candidates payload, computed
            # directly here (rather than through that public wrapper) because
            # the CoverageResult row this pass is about to write does not exist
            # yet -- there is nothing for the wrapper's `applicable` check to
            # read. Composes with (never duplicates) the Oracle-release /
            # customer-owned clauses above: those are woven into `base_reason`
            # via `_shortage_phrase` upstream of the pending branch, and this
            # branch's own `base_reason` never mentions dates at all, so there
            # is nothing here for the appended clause to repeat.
            abd = _approval_by_date_for_product_ros(
                db, line.id, line.product, line.ros_date
            )
            if abd.available and abd.still_recoverable:
                base_reason += (
                    f"; approve by {abd.approval_by_date.isoformat()} or lose "
                    "mill recovery -- past that date even an immediate "
                    "primary-product order can no longer meet the ROS"
                )
            elif abd.available and not abd.still_recoverable:
                base_reason += (
                    "; mill recovery is ALREADY IMPOSSIBLE for the primary "
                    "product regardless of this approval's outcome -- even an "
                    f"order placed today ({abd.approval_by_date.isoformat()} has "
                    "already passed) cannot meet the ROS"
                )
            # When lead time is not modelled, `abd.available` is False and the
            # clause is silently omitted rather than guessed -- honest absence,
            # matching the pattern the rest of this reason string already
            # follows for other unresolvable figures.
            reason_by_line[line.id] = base_reason
            continue

        line_assigned = assigned_by_line.get((line.id, line.product_id), 0.0)
        inv_blocked = next((c for c in candidates if c.blocking_layer == BLOCK_INVENTORY), None)
        # A substitute blocked ONLY by somebody else's hard assignment is a
        # different message with a different action -- the steel exists.
        oracle_blocked = next(
            (c for c in candidates if c.blocking_layer == BLOCK_ORACLE_RELEASE), None
        )
        shortage = _shortage_phrase(
            policy,
            line,
            line_assigned,
            standalone=inv_blocked is None and oracle_blocked is None,
            drawn_from_pool=drawn_from_pool.get(line.id, 0.0),
            # The carve-out this line's OWN product suffered, which is what the
            # SOFT wording needs in order to name the assignment instead of
            # blaming the shelf. `hard_assigned_by_product` holds exactly the
            # `reserved_elsewhere` figure computed for the own-product allocation.
            reserved_elsewhere=hard_assigned_by_product.get(line.product_id, 0.0),
            drawn_from_customer_owned=drawn_from_customer_owned.get(line.id, 0.0),
        )
        if oracle_blocked is not None:
            label = oracle_blocked.product.description or oracle_blocked.product.id
            base_reason = (
                f"{shortage}; approved substitute {label} exists "
                f"({oracle_blocked.hard_assigned_qty:g} on hand) but is hard-assigned "
                "to another demand line. This platform never releases or overrides a "
                "hard reservation -- a user must remove the assignment in Oracle "
                "first, then re-sync"
            )
        elif inv_blocked is not None:
            label = inv_blocked.product.description or inv_blocked.product.id
            base_reason = (
                f"{shortage}; approved substitute {label} also has insufficient qty"
            )
        else:
            base_reason = shortage

        # Last resort: a mill order. If even ordering today cannot land the
        # material by ROS, the line is UNRECOVERABLE rather than UNCOVERED.
        # Products with no lead-time components are never judged unrecoverable
        # -- see app.engines.order_dates.
        #
        # `is_recoverable` reads the EFFECTIVE ros_date, so an ROS push-out in a
        # scenario changes recoverability here -- through the same physics
        # calculation the official verdict uses, not a scenario-specific one.
        if is_recoverable(db, line.product, line.ros_date):
            status_by_line[line.id] = CoverageStatus.UNCOVERED
            reason_by_line[line.id] = base_reason
        else:
            _ship, order_by, lead_months = order_feasibility(db, line.product, line.ros_date)
            status_by_line[line.id] = CoverageStatus.UNRECOVERABLE
            reason_by_line[line.id] = (
                f"{base_reason}; mill order needed {order_by.isoformat()} "
                f"({lead_months:g} month lead time) -- ROS cannot be met even if "
                "ordered today"
            )

    # ---- Over-subscription of pending substitutes (change 2) ----------------
    # A pending substitute reserves nothing, so several lines can now legitimately
    # be told "this substitute could close your gap once approved" about the same
    # steel. That is only honest if the planner can SEE when approving all of them
    # cannot all succeed, so the arithmetic is done once here, pool-wide, and
    # rendered in the reason text of EVERY affected line -- not just the losers,
    # because with nothing reserved there are no losers to single out.
    #
    # `available_qty` is `remaining_qty` as it stands AFTER the whole pass: every
    # real draw (own-product and approved-substitute) has already happened, and
    # pending lines took nothing, so this is the quantity actually up for grabs.
    pending_substitute_load: list[PendingSubstituteLoad] = []
    for to_product_id, lines_pending in sorted(pending_lines_by_product.items()):
        product = db.get(Product, to_product_id)
        load = PendingSubstituteLoad(
            to_product_id=to_product_id,
            product_description=(product.description if product is not None else None),
            pending_line_count=len(lines_pending),
            pending_line_ids=tuple(l.id for l in lines_pending),
            pending_required_qty=sum(l.quantity for l in lines_pending),
            available_qty=max(0.0, remaining_qty.get(to_product_id, 0.0)),
        )
        pending_substitute_load.append(load)
        note = load.note
        if note is None:
            continue
        for pending_line in lines_pending:
            reason_by_line[pending_line.id] = (
                f"{reason_by_line[pending_line.id]}; {note}"
            )

    def _net(view: LineView) -> tuple[float, float, float, float]:
        """(customer-owned, company, substitute, residual) for one line -- from
        the draws this pass recorded, never re-derived. See `LineCoverage`."""
        status = status_by_line[view.id]
        owned = drawn_from_customer_owned.get(view.id, 0.0)
        company = drawn_from_pool.get(view.id, 0.0) + drawn_from_assignment.get(
            view.id, 0.0
        )
        if status == CoverageStatus.COVERED_VIA_SUBSTITUTE:
            return owned, company, view.quantity, 0.0
        if status == CoverageStatus.COVERED:
            return owned, company, 0.0, 0.0
        return owned, company, 0.0, max(0.0, view.quantity - owned - company)

    by_line = {}
    for view in included:
        owned, company, substitute, residual = _net(view)
        by_line[view.id] = LineCoverage(
            demand_line_id=view.id,
            well_id=view.well_id,
            status=status_by_line[view.id],
            reason=reason_by_line[view.id],
            fulfilled_by_product_id=fulfilled_by.get(view.id),
            product_id=view.product_id,
            quantity=view.quantity,
            ros_date=view.ros_date,
            drawn_customer_owned=owned,
            drawn_company=company,
            drawn_substitute=substitute,
            residual=residual,
        )

    # Rollup rule: a well is Covered only if EVERY included line is Covered or
    # CoveredViaSubstitute. PendingApproval, Uncovered and Unrecoverable all mean
    # the well is not covered. A well with no included lines at all has no
    # coverage status.
    statuses_by_well: dict[str, list[CoverageStatus]] = defaultdict(list)
    for view in included:
        statuses_by_well[view.well_id].append(status_by_line[view.id])

    well_status: dict[str, str | None] = {}
    for pool_well in wells:
        well_statuses = statuses_by_well.get(pool_well.id, [])
        if not well_statuses:
            well_status[pool_well.id] = None
            continue
        all_ok = all(
            s in (CoverageStatus.COVERED, CoverageStatus.COVERED_VIA_SUBSTITUTE)
            for s in well_statuses
        )
        well_status[pool_well.id] = (
            CoverageStatus.COVERED.value if all_ok else CoverageStatus.UNCOVERED.value
        )

    return CustomerCoverage(
        customer_id=customer.id,
        by_line=by_line,
        excluded_line_ids=tuple(view.id for view in excluded),
        well_status=well_status,
        included_views=tuple(included),
        all_views=tuple(views),
        pending_substitute_load=tuple(pending_substitute_load),
        hard_assigned_by_product=dict(hard_assigned_by_product),
        consumed_from_pool=dict(drawn_from_pool),
        consumed_from_assignment=dict(drawn_from_assignment),
        consumed_from_customer_owned=dict(drawn_from_customer_owned),
    )


def recompute_customer(
    db: Session,
    customer: Customer,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
) -> CustomerCoverage:
    """Recompute CoverageResult for every included demand line of `customer`,
    then roll up coverage_status for each of its wells. Idempotent.

    The rules live in `compute_customer_coverage`; this function is that call
    plus persistence, and it is the ONLY thing in the platform that writes a
    coverage verdict. It always computes with NO overrides -- the official answer
    is the answer about the data as it actually is.

    Two boundaries: Business Unit ALWAYS, Customer BY DEFAULT
    --------------------------------------------------------
    Quantity scope is the BUSINESS UNIT. Every on-hand figure this pass uses is
    resolved through `app.engines.inventory.on_hand_map` for
    `customer.business_unit_id`, so two customers in DIFFERENT BUs read different
    numbers and cannot influence each other through any path -- not the own-product
    allocation, not the substitution fall-through's availability seed, and not the
    HARD/HYBRID `reserved_by_product` netting (which is scope-restricted in
    `_assignment_context`). A customer with NO BU mapped has no inventory pool at
    all, so this function RAISES `app.engines.inventory.InventoryScopeMissing`
    rather than returning a verdict; see app.models.customer.Customer for why a
    loud refusal beats the half-working middle state that used to exist.

    Demand scope is the CUSTOMER. Inventory is pooled across all of a customer's
    wells and NEVER further. Coverage for customer A must not shift because
    customer B's demand grew -- even inside the same BU -- because that would make
    a planner's own coverage unpredictable from data they can see. Planners who
    want to know whether a neighbour's surplus COULD have covered their shortfall
    ask app.engines.sharing.cross_customer_sharing, a read-only what-if that
    writes nothing and never leaves the BU.

    Allocation is ROS-ordered at the DEMAND LINE level across the whole pool
    -----------------------------------------------------------------------
    Every included line of every well is gathered first, grouped by product, and
    each product's pool is allocated in ONE pass (see app.engines.allocation,
    which sorts by ros_date under SOFT and HYBRID). This is what makes the
    result independent of the order wells happen to be processed in. Iterating
    wells and carrying the remainder forward would let well processing order
    decide who gets scarce steel: with 5000 on hand, well A needing 4000 at ROS
    Dec-1 and well B needing 4000 at ROS Sep-1, processing A first would cover A
    and starve B, whereas "Earliest ROS First" requires B to win. Only line-level
    ordering across the pool produces that.

    Allocation follows the owning customer's policy (SOFT / HARD / HYBRID). For
    HARD and HYBRID the Oracle-projected inventory assignments are loaded and
    passed through. SOFT ignores its OWN assignments -- that is what pooling means
    -- but is still constrained by assignments held against OTHER customers' demand
    lines in the same BU, because taking that steel would override an Oracle fact
    the platform does not own (see `_assignment_context`). Phase 3's semantics are
    preserved at pool scope: an assignment is a carve-out of on_hand and never
    additive, and quantity reserved to a line is never free pool -- quantities
    assigned to demand lines OUTSIDE this customer's pool are netted off before
    allocating.

    Where the on-hand figure comes from
    -----------------------------------
    `InventoryOnHand(business_unit_id, product_id, quantity)` for COMPANY-owned
    stock, and `CustomerOwnedInventory(customer_id, product_id, quantity)` for the
    customer's own. Resolution happens once, in
    `app.engines.inventory.ownership_pool_map`, and this engine reads no quantity by
    any other route. `Product.on_hand_qty` has been dropped and there is no
    fallback.

    THE TWO SOURCES ARE NOT INTERCHANGEABLE, and the ordering between them is the
    product owner's ruling: for the same product, CUSTOMER-OWNED INVENTORY IS
    CONSUMED FIRST, ahead of any company steel and ahead of any Oracle assignment,
    under all three allocation policies. See `app.engines.allocation` for the tier
    order and for why HARD grants coverage from customer-owned stock with no Oracle
    assignment (Oracle cannot assign material it does not know exists).

    Customer-owned stock is ADDITIVE to the company pool, never a carve-out of it,
    and it can never be offered to another customer -- it is that customer's
    property, a boundary tighter than the Business Unit boundary (see
    `app.engines.sharing`).

    A consequence worth stating: a product this pool demands but which has NO
    `InventoryOnHand` row for this BU makes the whole pass raise
    `InventoryRowMissing`. The quantity is unknown, and there is no honest partial
    verdict to return -- a pass that quietly treated it as 0 would report
    Uncovered as though it had measured something.

    Note what the BU figure means for two customers in the SAME BU: each is
    computed independently against the full BU quantity, so the sum of what the
    BU has promised its customers can exceed what the BU physically holds. That
    is deliberate -- customer separation is the point -- and it is exactly why the
    sharing analysis defines surplus as "what is left after EVERY customer in the
    BU has taken its own committed quantity", which can legitimately be nothing.
    """
    computed = compute_customer_coverage(
        db, customer, status_filter=status_filter, profile_filter=profile_filter
    )

    lines_by_id = {view.id: view.line for view in computed.all_views}

    for line_id, verdict in computed.by_line.items():
        existing = db.get(CoverageResult, line_id)
        if existing is None:
            db.add(
                CoverageResult(
                    demand_line_id=line_id,
                    status=verdict.status,
                    reason=verdict.reason,
                    fulfilled_by_product_id=verdict.fulfilled_by_product_id,
                    demand_quantity=verdict.quantity,
                    drawn_customer_owned=verdict.drawn_customer_owned,
                    drawn_company=verdict.drawn_company,
                    drawn_substitute=verdict.drawn_substitute,
                    residual=verdict.residual,
                )
            )
        else:
            existing.status = verdict.status
            existing.reason = verdict.reason
            existing.fulfilled_by_product_id = verdict.fulfilled_by_product_id
            existing.demand_quantity = verdict.quantity
            existing.drawn_customer_owned = verdict.drawn_customer_owned
            existing.drawn_company = verdict.drawn_company
            existing.drawn_substitute = verdict.drawn_substitute
            existing.residual = verdict.residual
            # Explicit, not onupdate: a re-verified verdict whose VALUE did not
            # change was still computed now, and an unchanged row is not dirty,
            # so onupdate would keep serving the first-ever stamp (the C-08
            # surfacing would then show weeks-old times on fresh verdicts).
            existing.computed_at = datetime.utcnow()

    # Coverage is DERIVED data, so a result for a line the engine no longer
    # evaluates is not merely stale, it is misinformation: revise a
    # Confirmed/Primary line down to Planned or Contingency and its last
    # PendingApproval verdict would otherwise sit in the table forever, still
    # being served by GET /wells/{id} and still listed on the Home Dashboard's
    # "Pending Approvals" card. Delete it. Absence of a row now means exactly one
    # thing -- "not currently evaluated" -- which MRP treats as unresolved rather
    # than as fine.
    for line_id in computed.excluded_line_ids:
        stale = db.get(CoverageResult, line_id)
        if stale is not None:
            db.delete(stale)
    db.flush()
    for line in lines_by_id.values():
        # Drop the cached relationship so callers holding these DemandLines read
        # the rows we just wrote (or, for excluded lines, the absence of the row
        # we just deleted) rather than a value cached before this pass.
        db.expire(line, ["coverage_result"])

    for pool_well in _customer_wells(db, customer):
        pool_well.coverage_status = computed.well_status.get(pool_well.id)

    db.flush()
    return computed


# ---------------------------------------------------------------------------
# The two sanctioned writers of demand
# ---------------------------------------------------------------------------
#
# WHY THERE ARE TWO FUNCTIONS AND NOT ONE
# ---------------------------------------
# `apply_revision` used to take a `status` argument alongside quantity, ROS and
# profile, because all four were columns on `DemandLine`. Status has moved to
# `Well.demand_status` (see `app.models.well.Well`), and the split below follows
# the data rather than fighting it:
#
#   apply_revision(db, line, quantity, ros_date, profile)
#       PER-LINE facts. One line's requirement changed: a different quantity, a
#       different date, or a move between Primary and Contingency. Exactly one
#       DemandRevision, on exactly the line that changed.
#
#   set_well_demand_status(db, well, status)
#       A WELL-LEVEL fact. Confirming a well confirms every line of it, so this
#       writes a DemandRevision to EVERY line of the well and one ImpactRecord per
#       line.
#
# The rejected alternatives, and why:
#
#   * Keep one entry point that still takes a status and fans out internally.
#     Rejected: the signature would then accept, per line, a value that is not a
#     property of a line, and every caller would have to pass one (or pass the
#     current one back, which is how a "status change" that changed nothing would
#     get recorded as history).
#   * Let a status change write a revision to ONE line only, the one being edited.
#     Rejected: the well's other lines' histories would then have gaps at exactly
#     the moments their status changed, and `app.engines.executive._state_as_of`
#     reconstructs past state per line -- it would report the old status for the
#     siblings forever.
#
# Both functions preserve the invariant stated in
# `app.models.demand._write_initial_revision`: a DemandRevision numbered
# `current_revision_no` always exists. Each appends `current_revision_no + 1` on
# every line it touches and bumps the line in the same breath, so the two can
# never disagree -- and a line NOT touched keeps the revision it already had.


def apply_revision(
    db: Session,
    demand_line: DemandLine,
    quantity: float,
    ros_date: datetime,
    profile: DemandProfile,
) -> ImpactRecord:
    """Revise ONE line's quantity, ROS and profile. Not its status.

    Writes a new DemandRevision, updates the DemandLine, recomputes coverage for
    its well (which widens to the whole customer pool) and records before/after
    impact.

    There is no `status` parameter, on purpose. Demand status belongs to the well;
    changing it is `set_well_demand_status`. The revision this writes still CARRIES
    the status -- the well's current one -- because a revision is a snapshot of the
    whole state at that moment, and a history in which status went momentarily
    unrecorded could not be reconstructed (see
    `app.models.demand.DemandRevision`).

    `ImpactRecord.status_before` / `status_after` are likewise both the well's
    current status, i.e. equal. That is honest rather than lazy: this operation did
    not change the status, and the Home Dashboard's "Demand Changes" card renders
    the pair as a transition -- an unchanged pair reads as "status untouched",
    which is exactly what happened.
    """
    well = demand_line.well
    status = well.demand_status

    before_qty = demand_line.quantity
    before_ros = demand_line.ros_date
    before_coverage = well.coverage_status

    next_rev_no = demand_line.current_revision_no + 1
    db.add(
        DemandRevision(
            demand_line_id=demand_line.id,
            revision_no=next_rev_no,
            quantity=quantity,
            ros_date=ros_date,
            status=status,
            profile=profile,
        )
    )
    demand_line.quantity = quantity
    demand_line.ros_date = ros_date
    demand_line.profile = profile
    demand_line.current_revision_no = next_rev_no
    db.flush()

    recompute_well(db, well)

    impact = ImpactRecord(
        demand_line_id=demand_line.id,
        well_id=well.id,
        quantity_before=before_qty,
        quantity_after=quantity,
        ros_date_before=before_ros,
        ros_date_after=ros_date,
        status_before=status.value,
        status_after=status.value,
        coverage_before=before_coverage,
        coverage_after=well.coverage_status,
    )
    db.add(impact)
    db.flush()
    return impact


@dataclass(frozen=True)
class WellStatusChange:
    """What `set_well_demand_status` did. Scalars only -- no ORM row escapes.

    `revision_ids` and `impact_record_ids` are parallel to `demand_line_ids`, one
    entry per line of the well, so a caller can prove the fan-out happened rather
    than trust that it did.
    """

    well_id: str
    well_name: str
    status_before: str
    status_after: str
    coverage_before: str | None
    coverage_after: str | None
    demand_line_ids: tuple[str, ...] = ()
    revision_ids: tuple[str, ...] = ()
    impact_record_ids: tuple[str, ...] = ()
    #: True when the requested status equalled the one already stored, in which
    #: case NOTHING was written -- see the docstring.
    unchanged: bool = False

    @property
    def revised_line_count(self) -> int:
        return len(self.demand_line_ids)


def set_well_demand_status(
    db: Session, well: Well, status: DemandStatus
) -> WellStatusChange:
    """Change a WELL's demand status, cascading a revision to every one of its lines.

    THE one sanctioned writer of `Well.demand_status`. Nothing else may assign it
    -- not an API handler, not the seed's later phases, not a scenario apply (which
    calls this) -- because assigning the column alone would move the well between
    coverage scopes while leaving every line's history claiming the old status.

    What it does, in order:

      1. reads the current status and coverage rollup for the before-picture;
      2. writes a DemandRevision to EVERY line of the well, each carrying that
         line's own current quantity / ROS / profile plus the NEW status, and bumps
         each line's `current_revision_no`;
      3. assigns `well.demand_status`;
      4. recomputes coverage ONCE for the whole customer pool -- the status filter
         reads this column, so the well has just entered or left scope and the
         inventory it was or was not consuming has to be reallocated across every
         other well of the customer;
      5. writes one ImpactRecord per line, so the Home Dashboard's "Demand Changes"
         card shows the status transition for each affected line.

    A NO-OP REQUEST WRITES NOTHING
    ------------------------------
    Setting the status to the value it already has returns `unchanged=True` and
    writes no revision, no impact record and no recompute. The alternative would
    append a revision to every line of the well recording a change of nothing,
    which is the same pollution `app.engines.scenario_apply` refuses for demand
    overrides that match the base plan, and it would push real history off the
    dashboard's 20-row card.

    A WELL WITH NO DEMAND LINES
    ---------------------------
    Its status still changes -- a well can legitimately be confirmed before its
    programme is entered -- and there is simply nothing to write a revision to. The
    returned tuples are empty and `revised_line_count` is 0. Coverage is still
    recomputed, because the well's rollup is `None` either way but the customer's
    pass is what says so.
    """
    before_status = well.demand_status
    if before_status == status:
        return WellStatusChange(
            well_id=well.id,
            well_name=well.name,
            status_before=before_status.value,
            status_after=before_status.value,
            coverage_before=well.coverage_status,
            coverage_after=well.coverage_status,
            unchanged=True,
        )

    before_coverage = well.coverage_status
    # Queried, not read off `well.demand_lines`: the relationship is a cached
    # collection and a well whose lines were loaded during an earlier pass (when it
    # had none) would silently miss the ones added since -- the same reason
    # `compute_customer_coverage` queries. Missing a line here would leave a gap in
    # exactly that line's history.
    lines = (
        db.query(DemandLine)
        .filter(DemandLine.well_id == well.id)
        .order_by(DemandLine.ros_date, DemandLine.id)
        .all()
    )

    revisions: list[DemandRevision] = []
    before_by_line = {line.id: line.quantity for line in lines}
    for line in lines:
        next_rev_no = line.current_revision_no + 1
        revision = DemandRevision(
            demand_line_id=line.id,
            revision_no=next_rev_no,
            quantity=line.quantity,
            ros_date=line.ros_date,
            status=status,
            profile=line.profile,
        )
        db.add(revision)
        revisions.append(revision)
        line.current_revision_no = next_rev_no

    well.demand_status = status
    db.flush()

    recompute_well(db, well)

    impacts: list[ImpactRecord] = []
    for line in lines:
        impact = ImpactRecord(
            demand_line_id=line.id,
            well_id=well.id,
            # Quantity and ROS did not move, and both are stated rather than left
            # NULL: the card shows the line the change landed on, and a row with no
            # quantity at all reads as a deletion.
            quantity_before=before_by_line[line.id],
            quantity_after=line.quantity,
            ros_date_before=line.ros_date,
            ros_date_after=line.ros_date,
            status_before=before_status.value,
            status_after=status.value,
            coverage_before=before_coverage,
            coverage_after=well.coverage_status,
        )
        db.add(impact)
        impacts.append(impact)
    db.flush()

    return WellStatusChange(
        well_id=well.id,
        well_name=well.name,
        status_before=before_status.value,
        status_after=status.value,
        coverage_before=before_coverage,
        coverage_after=well.coverage_status,
        demand_line_ids=tuple(line.id for line in lines),
        revision_ids=tuple(r.id for r in revisions),
        impact_record_ids=tuple(i.id for i in impacts),
    )
