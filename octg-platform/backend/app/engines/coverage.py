from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.engines.allocation import (
    allocate_business_unit,
    allocate_detailed,
    allocation_order,
)
# The two filters, and the resolver that decides which pair is actually in force.
#
# THE CONSTANTS ARE RE-EXPORTED FROM HERE ON PURPOSE. They were defined in this
# module for most of the platform's life and are imported from it by
# `app.api.wells`, `app.api.demand`, `app.api.dashboard`, `app.engines.mrp`,
# `app.engines.executive`, `app.engines.scenario`,
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
from app.engines.inventory import (
    InventoryNotScoped,
    customer_owned_map,
    on_hand_map,
    ownership_pool_map,
    scoped_customer_ids,
)
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
    BusinessUnit,
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


def _equal_ros_tie_note(
    line,
    *,
    rivals,
    policy: AllocationPolicy,
    customer_of_line: dict[str, str],
    customer_name_by_id: dict[str, str],
    drawn_from_pool: dict[str, float],
    drawn_from_block: dict[str, float],
    drawn_from_customer_owned: dict[str, float],
) -> str | None:
    """Where the steel went when this line lost an EQUAL-ROS contest, or None.

    `rivals` are the lines of the same product in the same pass. A rival counts
    only if it ranks ahead of `line` under `allocation_order` AT THE SAME ROS and
    actually drew from a tier this line could have reached: the shared pool (never
    reachable by a HARD line), or -- for a rival of the same customer -- that
    customer's own reservation block or customer-owned stock. A neighbour spending
    its own property is not a contest this line took part in, so it is not named.

    The sentence states the rule that decided the tie, and states it honestly:
    Primary before Contingency is a judgement the owner made; the line-id order
    after that is fixed and arbitrary, and the text says so rather than implying
    the winner was entered first or matters more.
    """
    owner_id = customer_of_line[line.id]
    rank = allocation_order(line)
    winners = []
    for other in rivals:
        if other.id == line.id or other.ros_date != line.ros_date:
            continue
        if allocation_order(other) >= rank:
            continue
        from_shared = max(
            0.0, drawn_from_pool.get(other.id, 0.0) - drawn_from_block.get(other.id, 0.0)
        )
        same_customer = customer_of_line[other.id] == owner_id
        reachable = (policy != AllocationPolicy.HARD and from_shared > 0) or (
            same_customer
            and (
                drawn_from_block.get(other.id, 0.0) > 0
                or drawn_from_customer_owned.get(other.id, 0.0) > 0
            )
        )
        if reachable:
            winners.append(other)
    if not winners:
        return None

    def _label(view) -> str:
        well = view.well.name if view.well is not None else view.well_id
        who = customer_name_by_id.get(customer_of_line[view.id], "another customer")
        return f"{well} ({who}, {view.profile.value})"

    names = ", ".join(_label(w) for w in winners)
    lead = f"this line's own product was drawn ahead of it at the same ROS by {names}"
    if line.profile == DemandProfile.CONTINGENCY and all(
        w.profile == DemandProfile.PRIMARY for w in winners
    ):
        return f"{lead}: at an equal ROS a Primary line is served before a Contingency line"
    return (
        f"{lead}, which ranks ahead of it under the fixed equal-ROS tie-break "
        "(Primary before Contingency, then line id -- a fixed order, not a "
        "judgement of priority)"
    )


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
    the platform may not take it -- "the user must go into Oracle themselves and
    release the hard assignment". So this query is deliberately narrow too. It is used ONLY
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


@dataclass(frozen=True)
class BusinessUnitCoverage:
    """The complete coverage answer for ONE BUSINESS UNIT -- computed, unwritten.

    THE UNIT OF ALLOCATION (product-owner ruling 2026-09-06, D01)
    ------------------------------------------------------------
    Until this existed the unit was the CUSTOMER: each customer's demand was
    allocated against the whole Business Unit's quantity independently, so a BU
    holding 6,000 could tell two customers needing 4,000 each that both were
    Covered. Every figure downstream inherited that: the executive coverage
    percentage, the surplus list, the MRP recommendation. The pool is now
    contested ONCE, across every customer in the Business Unit, so the sum of
    what the BU has promised can never exceed what it holds.

    `CustomerCoverage` did not go away -- `for_customer` projects this answer onto
    one customer and every existing consumer keeps its shape. What changed is that
    the projection is a VIEW OF A BU-WIDE PASS rather than a pass of its own, so
    two customers of one BU can no longer be handed contradictory answers.
    """

    business_unit_id: str
    customer_ids: tuple[str, ...]
    by_line: dict[str, LineCoverage]
    excluded_line_ids: tuple[str, ...]
    well_status: dict[str, str | None]
    included_views: tuple[LineView, ...]
    all_views: tuple[LineView, ...]
    #: {line_id: customer_id} for every line of the Business Unit, included or not.
    customer_of_line: dict[str, str]
    #: {customer_id: (well_id, ...)} for every well of the Business Unit.
    wells_by_customer: dict[str, tuple[str, ...]]
    pending_substitute_load: tuple[PendingSubstituteLoad, ...] = ()
    #: {customer_id: {product_id: qty}} -- per customer, because what is blocked
    #: depends on WHOSE lines are asking: a SOFT customer's own assignments are
    #: pooled across its own wells, everybody else's are reserved away from it.
    hard_assigned_by_product: dict[str, dict[str, float]] = field(default_factory=dict)
    consumed_from_pool: dict[str, float] = field(default_factory=dict)
    consumed_from_assignment: dict[str, float] = field(default_factory=dict)
    consumed_from_customer_owned: dict[str, float] = field(default_factory=dict)

    def for_customer(self, customer_id: str) -> CustomerCoverage:
        """This BU-wide answer, projected onto one customer.

        A projection, never a recomputation: every verdict here was decided in the
        single pass, so two customers of the same Business Unit read consistent
        numbers by construction rather than by agreement.
        """
        mine = {
            line_id: verdict
            for line_id, verdict in self.by_line.items()
            if self.customer_of_line.get(line_id) == customer_id
        }
        my_wells = set(self.wells_by_customer.get(customer_id, ()))
        return CustomerCoverage(
            customer_id=customer_id,
            by_line=mine,
            excluded_line_ids=tuple(
                line_id
                for line_id in self.excluded_line_ids
                if self.customer_of_line.get(line_id) == customer_id
            ),
            well_status={
                well_id: status
                for well_id, status in self.well_status.items()
                if well_id in my_wells
            },
            included_views=tuple(
                v for v in self.included_views
                if self.customer_of_line.get(v.id) == customer_id
            ),
            all_views=tuple(
                v for v in self.all_views
                if self.customer_of_line.get(v.id) == customer_id
            ),
            pending_substitute_load=tuple(
                load for load in self.pending_substitute_load
                if any(
                    self.customer_of_line.get(line_id) == customer_id
                    for line_id in load.pending_line_ids
                )
            ),
            hard_assigned_by_product=dict(
                self.hard_assigned_by_product.get(customer_id, {})
            ),
            consumed_from_pool={
                k: v for k, v in self.consumed_from_pool.items() if k in mine
            },
            consumed_from_assignment={
                k: v for k, v in self.consumed_from_assignment.items() if k in mine
            },
            consumed_from_customer_owned={
                k: v for k, v in self.consumed_from_customer_owned.items() if k in mine
            },
        )


def _business_unit_customers(db: Session, business_unit_id: str) -> list[Customer]:
    """Every customer of the Business Unit, in a deterministic order."""
    return (
        db.query(Customer)
        .filter(Customer.business_unit_id == business_unit_id)
        .order_by(Customer.id)
        .all()
    )


def _assignment_context_bu(
    db: Session,
    included: list[LineView],
    policy_by_customer: dict[str, AllocationPolicy],
    customer_of_line: dict[str, str],
    business_unit_id: str,
    resolver: OverrideResolver = NO_OVERRIDES,
) -> tuple[dict[tuple[str, str], float], dict[str, float], dict[tuple[str, str], float]]:
    """The Oracle-projected assignments of one Business Unit, in three maps.

    Returns:
      assigned_by_line   {(demand_line_id, product_id): qty} -- the line's OWN
                         reservation, for HARD and HYBRID lines only.
      total_by_product   {product_id: qty} -- EVERY assignment row in this Business
                         Unit, including rows on demand lines this pass does not
                         evaluate. Reserved steel is never free pool, whether or not
                         the line it is reserved for is in scope.
      block_by_customer  {(customer_id, product_id): qty} -- a SOFT customer's OWN
                         assignments, pooled across its own lines.

    WHY THE THIRD MAP EXISTS
    ------------------------
    The per-customer pass expressed the SOFT rule as an exclusion: it ignored the
    customer's own assignments entirely and carved out only the neighbours'. That
    worked while each customer was allocated on its own, and it has no meaning once
    every customer is allocated together -- "ignore my own assignments" would drop
    that steel into the shared pool, where a neighbour could take it, and the
    platform would have released an Oracle reservation.

    So the rule is expressed POSITIVELY instead, and says the same thing: every
    assignment comes out of the shared pool (`total_by_product`), and a SOFT
    customer gets its own back as a private block usable by any of ITS lines
    (`block_by_customer`). A soft customer's availability is therefore
    `on_hand - total + own`, which is exactly `on_hand - foreign` -- the figure the
    per-customer pass computed. Nothing about SOFT changed; it is merely stated in
    a form that survives the pool being shared.

    Scenario ASSIGNMENT overrides are applied here, and only here. An override on a
    line outside this Business Unit is ignored -- a scenario cannot reach into
    another BU's reservations -- and one on an in-scope line moves BOTH the map that
    grants it and the total that carves it out of the shared pool, so raising an
    assignment takes that quantity from the pool rather than conjuring it.
    """
    line_ids = {view.id for view in included}
    assigned_by_line: dict[tuple[str, str], float] = defaultdict(float)
    total_by_product: dict[str, float] = defaultdict(float)
    block_by_customer: dict[tuple[str, str], float] = defaultdict(float)
    # What each SOFT line contributes to its customer's block. Kept because a
    # scenario override RESTATES that line's assignment rather than adding to it,
    # and under SOFT the line's own figure is otherwise invisible -- it has been
    # summed into the customer's pooled block and cannot be subtracted back out.
    soft_line_assigned: dict[tuple[str, str], float] = defaultdict(float)

    rows = (
        db.query(InventoryAssignment, PlanningNode.customer_id)
        .join(DemandLine, InventoryAssignment.demand_line_id == DemandLine.id)
        .join(Well, DemandLine.well_id == Well.id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .join(Customer, PlanningNode.customer_id == Customer.id)
        .filter(Customer.business_unit_id == business_unit_id)
        .all()
    )
    for row, owner_id in rows:
        qty = max(0.0, row.quantity or 0.0)
        total_by_product[row.product_id] += qty
        if policy_by_customer.get(owner_id) == AllocationPolicy.SOFT:
            block_by_customer[(owner_id, row.product_id)] += qty
            soft_line_assigned[(row.demand_line_id, row.product_id)] += qty
        elif row.demand_line_id in line_ids:
            assigned_by_line[(row.demand_line_id, row.product_id)] += qty

    for (line_id, product_id), new_qty in resolver.assignment_overrides().items():
        if line_id not in line_ids:
            continue
        owner_id = customer_of_line.get(line_id)
        new_qty = max(0.0, new_qty)
        if policy_by_customer.get(owner_id) == AllocationPolicy.SOFT:
            # RESTATES this line's reservation, exactly as it does for the other
            # policies -- `assignment_overrides()` carries an absolute quantity,
            # not a delta. The line's old figure comes back out of the customer's
            # pooled block and the new one goes in, so the customer's own
            # availability is unchanged (block and total move together) and the
            # difference is taken from, or returned to, the shared pool. Adding
            # without subtracting counted the stored row twice: a no-op override
            # then shrank the shared pool and moved a NEIGHBOUR's verdict.
            old_qty = soft_line_assigned.get((line_id, product_id), 0.0)
            block_by_customer[(owner_id, product_id)] = max(
                0.0,
                block_by_customer.get((owner_id, product_id), 0.0) - old_qty + new_qty,
            )
        else:
            old_qty = assigned_by_line.get((line_id, product_id), 0.0)
            assigned_by_line[(line_id, product_id)] = new_qty
        total_by_product[product_id] = max(
            0.0, total_by_product.get(product_id, 0.0) - old_qty + new_qty
        )

    return dict(assigned_by_line), dict(total_by_product), dict(block_by_customer)


def compute_business_unit_coverage(
    db: Session,
    business_unit,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
    resolver: OverrideResolver = NO_OVERRIDES,
) -> BusinessUnitCoverage:
    """THE coverage algorithm. Computes; does not write.

    This function is the single implementation of the coverage rules in the
    platform. `recompute_business_unit` is this plus persistence;
    `app.engines.scenario.preview` is this plus a `resolver` carrying a scenario's
    overrides; `compute_customer_coverage` is this plus a projection onto one
    customer. Nothing else may re-derive coverage.

    That split is the whole reason scenario preview can be trusted. A preview
    computed by a SECOND implementation would be a promise made by code that is not
    the code that keeps it -- it would drift, and it would drift silently, because a
    preview has no downstream consumer to notice it was wrong. Here, "preview and
    apply agree" is not a property maintained by tests, it is a property of there
    being one function.

    THE SCOPE OF THE POOL IS THE BUSINESS UNIT
    ------------------------------------------
    Every in-scope demand line of every customer in `business_unit` is allocated in
    ONE pass, earliest ROS first, against the Business Unit's stock. Two customers
    can no longer both be promised the same steel (D01). The ownership walls inside
    that pool are unchanged and are enforced by construction in
    `app.engines.allocation.allocate_business_unit`:

      * customer-owned material is drawable only by its own customer's lines;
      * an Oracle assignment is drawable only by the line it names (HARD/HYBRID) or,
        for a SOFT customer, only by that customer's own lines;
      * only genuinely unassigned company stock is shared, and the Business Unit
        boundary is never crossed.

    Reading values through `resolver`
    --------------------------------
    Every fact a scenario is allowed to change is read through
    `app.engines.overrides.OverrideResolver`: demand values via `resolver.view`,
    on-hand quantity via `resolver.on_hand`, assignments via
    `resolver.assignment_overrides`, and well-layer substitution approvals via
    `resolver.approval`. The OFFICIAL pass is not a bypass of that indirection --
    it passes `NO_OVERRIDES`, the identity resolver, so both paths are the same
    instructions.

    It writes NOTHING, and must stay that way: `app.engines.scenario` depends on
    that, tests pin it, and a write introduced here would turn every scenario
    preview into a silent mutation of the official verdict.

    THE FILTERS ARE RESOLVED HERE, AND ONLY HERE
    -------------------------------------------
    `None` for either filter means "the platform's CURRENT default", resolved from
    `app.engines.coverage_scope` against THIS session -- which is the persisted
    platform-wide setting when an administrator has adjusted it, and the shipped
    constant otherwise. It does not mean the constant. Resolving it in one function
    rather than at each entry point is the same rule as having one implementation:
    two readings of the setting are two things to keep in step.
    """
    if status_filter is None:
        status_filter = effective_status_filter(db)
    if profile_filter is None:
        profile_filter = effective_profile_filter(db)

    bu_id = business_unit.id
    customers = _business_unit_customers(db, bu_id)
    customer_ids = [c.id for c in customers]
    policy_by_customer = {c.id: c.allocation_policy for c in customers}

    # Wells and their owning customer in one query -- the rollup needs every well of
    # the Business Unit, including wells with no in-scope demand (they roll up to
    # "not evaluated", never to "covered").
    well_rows = (
        db.query(Well, PlanningNode.customer_id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id.in_(customer_ids))
        .order_by(Well.id)
        .all()
        if customer_ids
        else []
    )
    wells = [well for well, _owner in well_rows]
    customer_of_well = {well.id: owner for well, owner in well_rows}
    wells_by_customer: dict[str, list[str]] = defaultdict(list)
    for well, owner in well_rows:
        wells_by_customer[owner].append(well.id)

    # Demand lines are QUERIED rather than read off `well.demand_lines`. The
    # relationship is a cached collection: a well whose lines were loaded during an
    # earlier pass (when it had none) would keep reporting none, silently dropping
    # later-inserted demand out of the pool. A query cannot go stale that way.
    #
    # Queried in (ROS, id) order for a stable read; the order that DECIDES every
    # contest is `app.engines.allocation.allocation_order` (ROS, then Primary before
    # Contingency, then id), applied explicitly by every sort below. Without a total
    # order the contest between two customers' equally urgent lines would be decided
    # by whatever order the database happened to return, and two identical
    # recomputes could hand the last metre to different customers.
    pool_lines = (
        db.query(DemandLine)
        .join(Well, DemandLine.well_id == Well.id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id.in_(customer_ids))
        .order_by(DemandLine.ros_date, DemandLine.id)
        .all()
        if customer_ids
        else []
    )

    # Every line is seen through the resolver, ALWAYS -- including on the official
    # path, where the resolver hands each one straight back. The engine below
    # therefore never touches a DemandLine attribute directly, so there is no second
    # reading of demand for an override to miss.
    views = [resolver.view(line) for line in pool_lines]
    customer_of_line = {
        view.id: customer_of_well[view.well_id] for view in views
    }

    # `view.status` is the effective demand status OF THIS LINE'S WELL (see
    # `app.engines.overrides.LineView`), so this one condition applies both filters
    # at their proper granularity: status by well, profile by line.
    #
    # Excluding a whole well therefore needs no special case, and -- more
    # importantly -- FREES ITS INVENTORY BY THE SAME ARITHMETIC that frees an
    # excluded line's. None of its lines reach `by_product` below, so none of them
    # is passed to the allocator, so none of them consumes any of the pool; the
    # whole of that well's requirement stays on the shelf for the wells that are in
    # scope. Its `CoverageResult` rows are deleted by `recompute_business_unit` (via
    # `excluded_line_ids`) and it rolls up to `coverage_status = None`, i.e. "not
    # evaluated" -- never "covered".
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

    assigned_by_line, total_assigned, assignment_block = _assignment_context_bu(
        db, included, policy_by_customer, customer_of_line, bu_id, resolver
    )

    # Every on-hand figure below is BU-resolved up front, in one place. The set
    # covers the products this Business Unit demands, the products a technical
    # substitution could reach from them, anything carrying a reservation, and
    # anything an inventory override names -- i.e. every product whose quantity this
    # pass can possibly consult.
    demanded_ids = set(by_product)
    quantity_ids = (
        demanded_ids
        | _substitute_target_ids(db, demanded_ids)
        | set(total_assigned)
        | set(resolver.inventory_override_product_ids())
    )
    # The BU boundary is applied HERE, before the resolver is consulted, so an
    # inventory override can only ever restate a quantity that already belongs to
    # this Business Unit. There is no argument a scenario could pass that would make
    # this read another BU's stock. A demanded product with no `InventoryOnHand` row
    # in this BU makes the whole call raise: unknown is not zero.
    company = on_hand_map(db, bu_id, quantity_ids)
    # Company tier only, and a scenario INVENTORY override may restate only this
    # one. There is deliberately no counterpart for the customer tier: an INVENTORY
    # override restates an `InventoryOnHand` figure, which Oracle owns, and it must
    # not be able to invent (or delete) a customer's own property. See
    # `app.engines.overrides.OVERRIDE_FIELDS`.
    on_hand = {
        product_id: resolver.on_hand(product_id, qty)
        for product_id, qty in company.items()
    }
    # {(customer_id, product_id): customer-owned quantity}. Additive to `on_hand`,
    # never a carve-out of it, and never netted against an assignment. Scoped to the
    # customer because one customer's property can never satisfy another's demand --
    # which is exactly why it is keyed by customer here rather than summed.
    customer_owned: dict[tuple[str, str], float] = {}
    for customer in customers:
        for product_id, position in customer_owned_map(
            db, customer, set(company)
        ).items():
            customer_owned[(customer.id, product_id)] = position.drawable

    covered_by_line: dict[str, bool] = {}
    # Which product actually satisfied each line, for CoverageResult
    # .fulfilled_by_product_id. A line covered by its own product maps to that
    # product; a line covered via substitute maps to the substitute.
    fulfilled_by: dict[str, str] = {}
    # THE THREE RESIDUAL TIERS after the own-product allocation, which the
    # substitution fall-through draws on. They are kept apart rather than summed
    # because they can never be spent on the same thing: `remaining_shared` is the
    # Business Unit's unassigned company steel and any line may reach it;
    # `remaining_owned` is one customer's property and `remaining_block` one SOFT
    # customer's reservations, and only that customer's lines may reach either.
    remaining_shared: dict[str, float] = {}
    remaining_owned: dict[tuple[str, str], float] = {}
    remaining_block: dict[tuple[str, str], float] = {}
    # {(customer_id, product_id): qty of that product reserved to demand lines this
    # customer's lines cannot draw on}. One meaning, for `find_candidates` and for
    # the reason text.
    blocked_by_customer: dict[tuple[str, str], float] = {}
    # Quantity each line drew from the shared unassigned POOL of its own product --
    # which for a SOFT customer includes its own pooled assignment block, exactly as
    # the per-customer pass reported it. Includes PARTIAL draws that did not cover
    # the line, which the reason text has to state -- see `_shortage_phrase`.
    drawn_from_pool: dict[str, float] = {}
    # The part of `drawn_from_pool` that came from a SOFT customer's own assignment
    # block. Tracked so that releasing a partial draw (C-17, below) can put each
    # quantity back where it came from instead of donating a reservation to the
    # shared pool.
    drawn_from_block: dict[str, float] = {}
    # Quantity each line drew from its OWN Oracle assignment, kept beside the pool
    # draw so the two halves of "how was this line satisfied" come out of the SAME
    # AllocationOutcome. Reported on the result; nothing recomputes it.
    drawn_from_assignment: dict[str, float] = {}
    # Quantity each line drew from its customer's OWN uploaded stock, out of the
    # same AllocationOutcome as the other two. Drawn first under every policy.
    drawn_from_customer_owned: dict[str, float] = {}

    lines_by_customer_product: dict[tuple[str, str], list[LineView]] = defaultdict(list)
    for view in included:
        lines_by_customer_product[(customer_of_line[view.id], view.product_id)].append(view)

    def _usable_assigned(customer_id: str, product_id: str) -> float:
        """How much of this product's reserved stock THIS customer's in-scope lines
        may actually draw on: its own pooled block under SOFT, the sum of its own
        lines' entitlements otherwise."""
        return assignment_block.get((customer_id, product_id), 0.0) + sum(
            assigned_by_line.get((line.id, product_id), 0.0)
            for line in lines_by_customer_product.get((customer_id, product_id), [])
        )

    for product_id, lines in by_product.items():
        outcome = allocate_business_unit(
            lines=lines,
            company_on_hand=on_hand[product_id],
            total_assigned=total_assigned.get(product_id, 0.0),
            assigned_by_line={
                line.id: assigned_by_line.get((line.id, product_id), 0.0)
                for line in lines
            },
            assignment_block_by_customer={
                c: assignment_block.get((c, product_id), 0.0) for c in customer_ids
            },
            customer_owned_by_customer={
                c: customer_owned.get((c, product_id), 0.0) for c in customer_ids
            },
            policy_by_customer=policy_by_customer,
            customer_of_line=customer_of_line,
        )
        covered_by_line.update(outcome.covered)
        for line_id, is_covered in outcome.covered.items():
            if is_covered:
                fulfilled_by[line_id] = product_id
            drawn_from_pool[line_id] = outcome.consumed_from_pool.get(line_id, 0.0)
            drawn_from_block[line_id] = outcome.consumed_from_assignment_block.get(
                line_id, 0.0
            )
            drawn_from_assignment[line_id] = outcome.consumed_from_assignment.get(
                line_id, 0.0
            )
            drawn_from_customer_owned[line_id] = (
                outcome.consumed_from_customer_owned.get(line_id, 0.0)
            )
        remaining_shared[product_id] = outcome.remaining_pool
        for c in customer_ids:
            remaining_owned[(c, product_id)] = (
                outcome.remaining_customer_owned_by_customer.get(c, 0.0)
            )
            remaining_block[(c, product_id)] = (
                outcome.remaining_assignment_block_by_customer.get(c, 0.0)
            )
            blocked_by_customer[(c, product_id)] = max(
                0.0,
                total_assigned.get(product_id, 0.0) - _usable_assigned(c, product_id),
            )

    # Products this Business Unit does not demand directly can still be reached as
    # SUBSTITUTE targets, so their residual tiers are seeded here on exactly the
    # same rules -- shared company steel net of every reservation, plus whatever
    # each customer privately holds of it.
    for product_id, resolved in on_hand.items():
        if product_id in remaining_shared:
            continue
        remaining_shared[product_id] = max(
            0.0, resolved - total_assigned.get(product_id, 0.0)
        )
        for c in customer_ids:
            remaining_owned[(c, product_id)] = customer_owned.get((c, product_id), 0.0)
            remaining_block[(c, product_id)] = assignment_block.get(
                (c, product_id), 0.0
            )
            blocked_by_customer[(c, product_id)] = max(
                0.0,
                total_assigned.get(product_id, 0.0) - _usable_assigned(c, product_id),
            )

    def _availability_for(customer_id: str) -> dict[str, float]:
        """What one customer's lines could actually draw of each product, right now:
        the shared residual plus that customer's own private residuals."""
        return {
            product_id: max(
                0.0,
                shared
                + remaining_owned.get((customer_id, product_id), 0.0)
                + remaining_block.get((customer_id, product_id), 0.0),
            )
            for product_id, shared in remaining_shared.items()
        }

    def _draw(customer_id: str, product_id: str, quantity: float) -> None:
        """Take `quantity` of `product_id` for `customer_id`, in ownership order --
        its own stock first, then its own reservations, then the shared pool."""
        need = quantity
        for store, key in (
            (remaining_owned, (customer_id, product_id)),
            (remaining_block, (customer_id, product_id)),
        ):
            take = min(need, store.get(key, 0.0))
            if take > 0:
                store[key] = store.get(key, 0.0) - take
                need -= take
        if need > 0:
            remaining_shared[product_id] = max(
                0.0, remaining_shared.get(product_id, 0.0) - need
            )

    status_by_line: dict[str, CoverageStatus] = {}
    reason_by_line: dict[str, str | None] = {}

    for line in included:
        if covered_by_line.get(line.id, False):
            status_by_line[line.id] = CoverageStatus.COVERED
            reason_by_line[line.id] = None

    # ---- Equal-ROS ties, stated for the line that lost them -------------------
    # At an identical ROS the contest is decided by `allocation_order` (Primary
    # before Contingency, then line id -- owner ruling 2026-09-06). The loser's
    # reason has to say so: "insufficient inventory" alone leaves a planner unable
    # to explain why the well next door, due the very same day, got the steel.
    #
    # Only a winner that drew from a tier THIS line could reach is named -- the
    # shared pool for a non-HARD loser, or the loser's own customer's private
    # tiers -- because a neighbour spending its own property was never a contest
    # this line was in. Computed HERE, before the fall-through below, because a
    # winner later rescued by a substitute has its own-product draw released
    # (C-17) and the record of who took the steel at allocation time would be gone.
    customer_name_by_id = {c.id: c.name for c in customers}
    tie_note_by_line: dict[str, str] = {}
    for line in included:
        if covered_by_line.get(line.id, False):
            continue
        note = _equal_ros_tie_note(
            line,
            rivals=by_product.get(line.product_id, []),
            policy=policy_by_customer[customer_of_line[line.id]],
            customer_of_line=customer_of_line,
            customer_name_by_id=customer_name_by_id,
            drawn_from_pool=drawn_from_pool,
            drawn_from_block=drawn_from_block,
            drawn_from_customer_owned=drawn_from_customer_owned,
        )
        if note is not None:
            tie_note_by_line[line.id] = note

    # Substitution fall-through: only lines their own product could not cover.
    # `allocation_order` ACROSS THE WHOLE BUSINESS UNIT -- earliest ROS first, then
    # Primary before Contingency, then id -- so the scarce substitute stock goes to
    # the most urgent line regardless of which customer it belongs to, and a line's
    # rank is the same one the allocation above used.
    shortfall = sorted(
        (l for l in included if not covered_by_line.get(l.id, False)),
        key=allocation_order,
    )
    # {to_product_id: [LineView]} for lines that ended up PENDING_APPROVAL. A pending
    # substitute now consumes NOTHING (see below), so the scarcity it used to hide
    # has to be made visible after the pass, once every pending line is known.
    pending_lines_by_product: dict[str, list[LineView]] = defaultdict(list)
    for line in shortfall:
        owner_id = customer_of_line[line.id]
        candidates = find_candidates(
            db,
            line,
            available_qty_by_product=_availability_for(owner_id),
            approval_override=resolver.approval,
            hard_assigned_by_product={
                product_id: blocked_by_customer.get((owner_id, product_id), 0.0)
                for product_id in remaining_shared
            },
        )

        usable = next((c for c in candidates if c.usable), None)
        if usable is not None:
            # The substitute covers the WHOLE line, and whatever the line had
            # already drawn from its own product goes BACK (owner ruling
            # 2026-09-06, C-17): a line is satisfied by one product, not a mixture,
            # and the released steel is then available to later draws in this pass.
            # Each quantity returns to the tier it came from -- a SOFT customer's
            # reservation block is NOT donated to the shared pool, because that
            # would release an Oracle reservation. An assignment draw is released
            # from the line but returns nowhere: it is that line's own reservation
            # and cannot serve anyone else.
            from_block = drawn_from_block.get(line.id, 0.0)
            from_shared = max(0.0, drawn_from_pool.get(line.id, 0.0) - from_block)
            from_owned = drawn_from_customer_owned.get(line.id, 0.0)
            if from_owned > 0:
                remaining_owned[(owner_id, line.product_id)] = (
                    remaining_owned.get((owner_id, line.product_id), 0.0) + from_owned
                )
            if from_block > 0:
                remaining_block[(owner_id, line.product_id)] = (
                    remaining_block.get((owner_id, line.product_id), 0.0) + from_block
                )
            if from_shared > 0:
                remaining_shared[line.product_id] = (
                    remaining_shared.get(line.product_id, 0.0) + from_shared
                )
            drawn_from_pool[line.id] = 0.0
            drawn_from_block[line.id] = 0.0
            drawn_from_customer_owned[line.id] = 0.0
            drawn_from_assignment[line.id] = 0.0

            _draw(owner_id, usable.to_product_id, line.quantity)
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
            # RESERVE NOTHING. A pending substitute holds no quantity back: reserving
            # is itself the platform creating a hard reservation, and it must never
            # do that -- "hard allocation cannot be done on the OCTG Platform; on the
            # OCTG Platform everything is strictly soft". The over-subscription that
            # reservation used to hide is made VISIBLE instead, after the pass, in
            # both the reason text and the candidate payload (`pending_substitute_load`).
            #
            # The stock is still NOT marked as fulfilling the line -- nothing has
            # been drawn, so fulfilled_by stays unset and MRP's runout keeps charging
            # this line to its own product.
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
            # directly here (rather than through that public wrapper) because the
            # CoverageResult row this pass is about to write does not exist yet --
            # there is nothing for the wrapper's `applicable` check to read.
            # Composes with (never duplicates) the Oracle-release / customer-owned
            # clauses: those are woven in via `_shortage_phrase` downstream of the
            # pending branch, and this branch's own `base_reason` never mentions
            # dates at all, so there is nothing here for the appended clause to
            # repeat.
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
            # matching the pattern the rest of this reason string already follows.
            if line.id in tie_note_by_line:
                base_reason += f"; {tie_note_by_line[line.id]}"
            reason_by_line[line.id] = base_reason
            continue

        policy = policy_by_customer[owner_id]
        line_assigned = assigned_by_line.get((line.id, line.product_id), 0.0)
        inv_blocked = next(
            (c for c in candidates if c.blocking_layer == BLOCK_INVENTORY), None
        )
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
            # The carve-out this line's OWN product suffered, which is what the SOFT
            # wording needs in order to name the reservation instead of blaming the
            # shelf.
            reserved_elsewhere=blocked_by_customer.get(
                (owner_id, line.product_id), 0.0
            ),
            drawn_from_customer_owned=drawn_from_customer_owned.get(line.id, 0.0),
        )
        if line.id in tie_note_by_line:
            shortage = f"{shortage}; {tie_note_by_line[line.id]}"
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

        # Last resort: a mill order. If even ordering today cannot land the material
        # by ROS, the line is UNRECOVERABLE rather than UNCOVERED. Products with no
        # lead-time components are never judged unrecoverable -- see
        # app.engines.order_dates.
        #
        # `is_recoverable` reads the EFFECTIVE ros_date, so an ROS push-out in a
        # scenario changes recoverability here -- through the same physics
        # calculation the official verdict uses, not a scenario-specific one.
        if is_recoverable(db, line.product, line.ros_date):
            status_by_line[line.id] = CoverageStatus.UNCOVERED
            reason_by_line[line.id] = base_reason
        else:
            _ship, order_by, lead_months = order_feasibility(
                db, line.product, line.ros_date
            )
            status_by_line[line.id] = CoverageStatus.UNRECOVERABLE
            reason_by_line[line.id] = (
                f"{base_reason}; mill order needed {order_by.isoformat()} "
                f"({lead_months:g} month lead time) -- ROS cannot be met even if "
                "ordered today"
            )

    # ---- Over-subscription of pending substitutes ---------------------------
    # A pending substitute reserves nothing, so several lines can legitimately be
    # told "this substitute could close your gap once approved" about the same
    # steel. That is only honest if the planner can SEE when approving all of them
    # cannot all succeed, so the arithmetic is done once here, Business-Unit-wide,
    # and rendered in the reason text of EVERY affected line -- not just the losers,
    # because with nothing reserved there are no losers to single out.
    #
    # `available_qty` is the residual as it stands AFTER the whole pass -- the shared
    # remainder plus the private remainders of exactly the customers whose lines are
    # pending on this substitute, since no other customer's stock could reach them.
    pending_substitute_load: list[PendingSubstituteLoad] = []
    for to_product_id, lines_pending in sorted(pending_lines_by_product.items()):
        product = db.get(Product, to_product_id)
        # A private residual is NOT contested: only its own customer's lines can
        # reach it. So it is spent here on that customer's own pending lines, and
        # what is left over is the claim on the SHARED tier -- the one quantity
        # several customers can be promised at once, and therefore the only one an
        # over-subscription figure may be about. Summing every pending customer's
        # private stock into a single "available" number reported a real three-way
        # contest as healthy, using steel two of the three could never draw.
        private = {
            owner: remaining_owned.get((owner, to_product_id), 0.0)
            + remaining_block.get((owner, to_product_id), 0.0)
            for owner in {customer_of_line[l.id] for l in lines_pending}
        }
        contested = 0.0
        for pending_line in sorted(lines_pending, key=allocation_order):
            owner = customer_of_line[pending_line.id]
            from_private = min(pending_line.quantity, private.get(owner, 0.0))
            private[owner] = private.get(owner, 0.0) - from_private
            contested += max(0.0, pending_line.quantity - from_private)
        load = PendingSubstituteLoad(
            to_product_id=to_product_id,
            product_description=(product.description if product is not None else None),
            pending_line_count=len(lines_pending),
            pending_line_ids=tuple(l.id for l in lines_pending),
            pending_required_qty=contested,
            available_qty=max(0.0, remaining_shared.get(to_product_id, 0.0)),
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
        """(customer-owned, company, substitute, residual) for one line -- from the
        draws this pass recorded, never re-derived. See `LineCoverage`."""
        status = status_by_line[view.id]
        owned = drawn_from_customer_owned.get(view.id, 0.0)
        company = drawn_from_pool.get(view.id, 0.0) + drawn_from_assignment.get(
            view.id, 0.0
        )
        if status == CoverageStatus.COVERED_VIA_SUBSTITUTE:
            # Own-product draws were released when the substitute took the line
            # (C-17), so `owned`/`company` are 0 here by construction.
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
    # CoveredViaSubstitute. PendingApproval, Uncovered and Unrecoverable all mean the
    # well is not covered. A well with no included lines at all has no coverage
    # status.
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

    hard_assigned_by_product: dict[str, dict[str, float]] = defaultdict(dict)
    for (owner_id, product_id), qty in blocked_by_customer.items():
        hard_assigned_by_product[owner_id][product_id] = qty

    return BusinessUnitCoverage(
        business_unit_id=bu_id,
        customer_ids=tuple(customer_ids),
        by_line=by_line,
        excluded_line_ids=tuple(view.id for view in excluded),
        well_status=well_status,
        included_views=tuple(included),
        all_views=tuple(views),
        customer_of_line=dict(customer_of_line),
        wells_by_customer={c: tuple(ws) for c, ws in wells_by_customer.items()},
        pending_substitute_load=tuple(pending_substitute_load),
        hard_assigned_by_product={k: dict(v) for k, v in hard_assigned_by_product.items()},
        consumed_from_pool=dict(drawn_from_pool),
        consumed_from_assignment=dict(drawn_from_assignment),
        consumed_from_customer_owned=dict(drawn_from_customer_owned),
    )


def compute_customer_coverage(
    db: Session,
    customer: Customer,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
    resolver: OverrideResolver = NO_OVERRIDES,
) -> CustomerCoverage:
    """One customer's view of its Business Unit's coverage answer.

    A PROJECTION of `compute_business_unit_coverage`, not a pass of its own. Since
    the ruling of 2026-09-06 (D01) the unit of allocation is the Business Unit, so a
    per-customer answer computed in isolation would be exactly the figure that
    ruling abolished -- one that quietly promised the same steel twice. Every
    existing consumer keeps this signature and this shape; what it now returns is a
    slice of a consistent whole.

    Writes nothing. Raises `app.engines.inventory.InventoryScopeMissing` for a
    customer with no Business Unit -- there is no pool, so there is no verdict --
    exactly as before, and only when that customer actually has demand to judge.
    """
    # No Business Unit means no inventory scope, so there is no pool to allocate
    # and no honest verdict to return -- with or without demand. Raises
    # `InventoryScopeMissing`, exactly as the per-customer pass did (it reached the
    # same refusal through `_assignment_context`), and the callers that isolate a
    # customer they cannot evaluate keep catching the same exception.
    scoped_customer_ids(db, customer)

    # Resolved by id, never through `customer.business_unit`: a caller that has
    # just re-pointed `business_unit_id` (the Administration remap) still holds the
    # OLD BusinessUnit on that relationship until the instance is expired, and a
    # pass computed against it would judge the customer in the pool it just left.
    computed = compute_business_unit_coverage(
        db,
        db.get(BusinessUnit, customer.business_unit_id),
        status_filter=status_filter,
        profile_filter=profile_filter,
        resolver=resolver,
    )
    return computed.for_customer(customer.id)


def _persist_coverage(
    db: Session,
    by_line: dict,
    excluded_line_ids,
    all_views,
    well_status: dict,
) -> None:
    """Write one computed coverage answer down. The ONLY place a verdict is
    persisted.

    Split out of `recompute_customer` when the unit of allocation became the
    Business Unit (D01): the same rows have to be written whether the caller asked
    for a whole BU or -- in the one remaining case, a customer with no Business
    Unit -- for a single customer, and two copies of the write would eventually
    disagree about the deletion of stale rows.
    """
    lines_by_id = {view.id: view.line for view in all_views}

    for line_id, verdict in by_line.items():
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
    for line_id in excluded_line_ids:
        stale = db.get(CoverageResult, line_id)
        if stale is not None:
            db.delete(stale)
    db.flush()
    for line in lines_by_id.values():
        # Drop the cached relationship so callers holding these DemandLines read
        # the rows we just wrote (or, for excluded lines, the absence of the row
        # we just deleted) rather than a value cached before this pass.
        db.expire(line, ["coverage_result"])

    if well_status:
        for pool_well in (
            db.query(Well).filter(Well.id.in_(list(well_status))).all()
        ):
            pool_well.coverage_status = well_status.get(pool_well.id)

    db.flush()


def recompute_business_unit(
    db: Session,
    business_unit,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
) -> BusinessUnitCoverage:
    """Recompute and persist coverage for EVERY customer of `business_unit`.

    THE writer of coverage verdicts since the unit of allocation became the
    Business Unit (product-owner ruling 2026-09-06, D01). The rules live in
    `compute_business_unit_coverage`; this is that call plus persistence. It always
    computes with NO overrides -- the official answer is the answer about the data
    as it actually is.

    Idempotent, and necessarily WHOLE: a pass that wrote one customer's verdicts
    and left its neighbours' alone would leave the Business Unit describing a
    division of steel that no single pass ever computed, which is precisely the
    incoherence D01 abolished.
    """
    computed = compute_business_unit_coverage(
        db,
        business_unit,
        status_filter=status_filter,
        profile_filter=profile_filter,
    )
    _persist_coverage(
        db,
        computed.by_line,
        computed.excluded_line_ids,
        computed.all_views,
        computed.well_status,
    )
    return computed


@dataclass(frozen=True)
class BusinessUnitRecomputeFailure:
    """One Business Unit whose pass could not be computed, and who it affects."""

    business_unit_id: str | None
    business_unit_name: str | None
    customer_ids: tuple[str, ...]
    customer_names: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class RecomputeSweep:
    """The outcome of recomputing several Business Units in one transaction."""

    recomputed_customer_ids: tuple[str, ...] = ()
    recomputed_line_count: int = 0
    failures: tuple[BusinessUnitRecomputeFailure, ...] = ()


def recompute_all_business_units(
    db: Session,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
    customers: list[Customer] | None = None,
    use_savepoints: bool = True,
) -> RecomputeSweep:
    """Recompute every Business Unit touched by `customers` (all of them by default).

    ONE pass per Business Unit, not one per customer. Since the unit of allocation
    became the BU (D01) a per-customer loop would recompute the same rows once for
    every customer in the BU -- identical work, identical result, N times over --
    and the administrative actions that trigger a full sweep (a lead-time edit, a
    substitution master-data change, a coverage-scope change, the manual recompute
    button) all used to be written that way.

    FAILURE IS ISOLATED PER BUSINESS UNIT, which is the honest unit now: an
    unknown on-hand quantity makes the whole pool unmeasurable, so every customer
    sharing that pool is affected and all of them are NAMED. A customer with no
    Business Unit has no pool at all and is reported the same way. One BU's
    incomplete data must never veto another's, which is the C-07 stance kept at
    its new granularity.

    Each BU's pass runs in its own SAVEPOINT so a failure part-way through cannot
    leave that BU's stored verdicts half-erased -- strictly worse than the ones
    they were replacing. `use_savepoints=False` turns that off for the ONE caller
    that must not use them: `app.engines.coverage_view` recomputes read-only and
    rolls the whole transaction back, and under pysqlite a SAVEPOINT RELEASE can
    behave as a commit -- which would turn that projection into a persist. That
    caller accepts a half-written pool for the duration of one response precisely
    because it discards everything at the end.
    """
    if customers is None:
        customers = db.query(Customer).order_by(Customer.name).all()

    # Expanded from the requested customers to EVERY customer of each Business Unit
    # they belong to. Naming one customer recomputes its whole pool -- that is what
    # allocating a Business Unit means -- and the report has to say so rather than
    # claim a narrower blast radius than the write actually had.
    by_bu: dict[str | None, list[Customer]] = defaultdict(list)
    for bu_id in {c.business_unit_id for c in customers}:
        if bu_id is None:
            by_bu[None] = [c for c in customers if c.business_unit_id is None]
        else:
            by_bu[bu_id] = _business_unit_customers(db, bu_id)

    recomputed: list[str] = []
    line_count = 0
    failures: list[BusinessUnitRecomputeFailure] = []

    for bu_id, members in by_bu.items():
        business_unit = db.get(BusinessUnit, bu_id) if bu_id is not None else None
        try:
            if business_unit is None:
                # No pool, so no verdict -- the same refusal a single unmapped
                # customer gets, raised here so it is reported rather than thrown.
                scoped_customer_ids(db, members[0])
                continue
            if use_savepoints:
                with db.begin_nested():
                    computed = recompute_business_unit(
                        db,
                        business_unit,
                        status_filter=status_filter,
                        profile_filter=profile_filter,
                    )
            else:
                computed = recompute_business_unit(
                    db,
                    business_unit,
                    status_filter=status_filter,
                    profile_filter=profile_filter,
                )
            recomputed.extend(c.id for c in members)
            line_count += len(computed.by_line)
        except InventoryNotScoped as exc:
            failures.append(
                BusinessUnitRecomputeFailure(
                    business_unit_id=bu_id,
                    business_unit_name=(
                        business_unit.name if business_unit is not None else None
                    ),
                    customer_ids=tuple(c.id for c in members),
                    customer_names=tuple(c.name for c in members),
                    reason=str(exc),
                )
            )

    return RecomputeSweep(
        recomputed_customer_ids=tuple(recomputed),
        recomputed_line_count=line_count,
        failures=tuple(failures),
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

    Demand scope is the BUSINESS UNIT (product-owner ruling 2026-09-06, D01).
    Every in-scope line of every customer in the BU is allocated together, so
    coverage for customer A DOES shift when customer B's demand grows -- and must,
    because they draw on one shelf and the alternative was promising the same
    steel twice. What is still true, and is now enforced tier by tier rather than
    by keeping the customers apart: a customer's own uploaded stock and an Oracle
    assignment are never drawn by anybody else.

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
    property, a boundary tighter than the Business Unit boundary and the one wall
    that survived the pool being shared (D01).

    A consequence worth stating: a product this pool demands but which has NO
    `InventoryOnHand` row for this BU makes the whole pass raise
    `InventoryRowMissing`. The quantity is unknown, and there is no honest partial
    verdict to return -- a pass that quietly treated it as 0 would report
    Uncovered as though it had measured something.

    Note what the BU figure means for two customers in the SAME BU: it is divided
    between them once, earliest ROS first, so the sum of what the BU has promised
    can never exceed what it physically holds. That was not true before D01, and
    the demo database was the proof -- 49,240 metres promised twice.
    """
    if customer.business_unit_id is None:
        # No Business Unit means no pool: there is nothing to allocate and nothing
        # to share. `compute_customer_coverage` raises for such a customer the
        # moment it has demand to judge, exactly as before; a customer with nothing
        # in scope is written down as unevaluated.
        computed = compute_customer_coverage(
            db, customer, status_filter=status_filter, profile_filter=profile_filter
        )
        _persist_coverage(
            db,
            computed.by_line,
            computed.excluded_line_ids,
            computed.all_views,
            computed.well_status,
        )
        return computed

    # The whole Business Unit, always. Recomputing this customer alone would leave
    # its neighbours holding verdicts decided against a pool this pass has just
    # re-divided -- the stale-neighbour defect F05 closed for assignments, and now
    # a property of every recompute rather than of the callers remembering.
    return recompute_business_unit(
        db,
        db.get(BusinessUnit, customer.business_unit_id),
        status_filter=status_filter,
        profile_filter=profile_filter,
    ).for_customer(customer.id)


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
