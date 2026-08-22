"""Three-layer substitution engine.

A substitute product is only usable for a demand line when it clears three
INDEPENDENT layers:

  1. Technical  -- engineering allows from_product -> to_product (directional,
     customer-agnostic).
  2. Customer   -- the owning customer generally permits that pair.
  3. Well       -- THIS specific demand line has an Approved approval record.

On top of the three layers the substitute must also physically exist in enough
quantity that is actually FREE. Two distinct inventory facts can stop it, and
they are reported separately because they have different recommended actions:

  * `blocking_layer == "insufficient-inventory"` -- the quantity simply is not
    there. Order it or transfer it.
  * `blocking_layer == "hard-assigned-elsewhere"` -- the quantity IS there, but
    it is hard-assigned to another demand line. This platform never creates,
    releases or overrides a hard reservation; hard assignments are read-only
    Oracle facts. So the substitute is still OFFERED as a candidate (hiding it
    would deny the planner a real option) but it can never become
    COVERED_VIA_SUBSTITUTE. The action names the Oracle step:
    the user must go into Oracle themselves and release the hard assignment.

`blocking_layer` is ONE value, so the layers -- though conceptually independent
-- are reported in a fixed PRIORITY order (see `find_candidates`). That tension
predates this module's current shape and is documented rather than deepened:
the order is chosen so the value always names the step the planner must take
FIRST, cheapest-and-most-fundamental permission question before the physical one.

    customer  >  well-approval  >  hard-assigned-elsewhere  >  insufficient-inventory

`hard-assigned-elsewhere` sits BELOW well-approval because a substitute nobody
is allowed to use is not worth an Oracle release request, and ABOVE
insufficient-inventory because it is the strictly more informative of the two
quantity answers: "the steel exists, it is just spoken for" tells the planner to
phone Oracle, whereas "insufficient-inventory" would send them to the mill for
material already sitting on the dock.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.engines.lead_time import resolve_lead_time
from app.engines.order_dates import is_recoverable, order_feasibility
from app.models import (
    CoverageResult,
    CoverageStatus,
    CustomerSubstitutionRule,
    DemandLine,
    Product,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    WellSubstitutionApproval,
)

# blocking_layer vocabulary, in the priority order documented above.
BLOCK_CUSTOMER = "customer"
BLOCK_WELL_APPROVAL = "well-approval"
BLOCK_ORACLE_RELEASE = "hard-assigned-elsewhere"
BLOCK_INVENTORY = "insufficient-inventory"

#: Priority order `find_candidates` reports in. Exported so a consumer (or a
#: test) can assert the order rather than rediscover it from the if/elif chain.
BLOCK_PRIORITY = (
    BLOCK_CUSTOMER,
    BLOCK_WELL_APPROVAL,
    BLOCK_ORACLE_RELEASE,
    BLOCK_INVENTORY,
)

#: The action a planner must take to clear each blocking layer. Deliberately
#: parallel in style: the well-approval wording ("Request customer approval") is
#: the pattern the Oracle-release action was modelled on.
RECOMMENDED_ACTION = {
    BLOCK_CUSTOMER: (
        "Agree the substitution with the customer and record a customer "
        "substitution rule permitting this pair"
    ),
    BLOCK_WELL_APPROVAL: "Request customer approval",
    BLOCK_ORACLE_RELEASE: (
        "Release the hard assignment in Oracle, then re-sync -- this platform "
        "cannot create, release or override a hard reservation, so the "
        "assignment has to be removed in Oracle by a user"
    ),
    BLOCK_INVENTORY: (
        "Order or transfer additional quantity of the substitute -- the required "
        "quantity does not exist in this Business Unit"
    ),
}


@dataclass(frozen=True)
class PendingSubstituteLoad:
    """How much PendingApproval demand is riding on ONE substitute product.

    A pending substitute reserves NOTHING (see
    `app.engines.coverage.compute_customer_coverage`): reserving would be this
    platform creating a hard reservation, which it must never do. The scarcity
    that reservation used to hide is therefore made VISIBLE instead -- through
    this record, which the coverage reason text and the substitution-candidate
    payload both render.
    """

    to_product_id: str
    product_description: str | None
    pending_line_count: int
    pending_line_ids: tuple[str, ...]
    pending_required_qty: float
    available_qty: float

    @property
    def over_subscribed(self) -> bool:
        return self.pending_required_qty > self.available_qty

    @property
    def shortfall(self) -> float:
        return max(0.0, self.pending_required_qty - self.available_qty)

    @property
    def note(self) -> str | None:
        """One sentence a planner can act on, or None when nothing is wrong."""
        if not self.over_subscribed:
            return None
        label = self.product_description or self.to_product_id
        return (
            f"OVER-SUBSCRIBED: {self.pending_line_count} demand lines are awaiting "
            f"approval for substitute {label}, together requiring "
            f"{self.pending_required_qty:g} against {self.available_qty:g} "
            f"available -- approving all of them cannot succeed (short by "
            f"{self.shortfall:g}). Decide which lines get the steel, or obtain "
            "more of it."
        )


@dataclass
class SubstitutionCandidate:
    """One technically-valid substitute for a demand line, annotated with the
    outcome of each layer. Importable by the API layer."""

    demand_line_id: str
    from_product_id: str
    to_product_id: str
    product: Product
    customer_allowed: bool
    approval_status: SubstitutionApprovalStatus | None
    approval_id: str | None
    #: FREE quantity -- what this line could actually draw on.
    available_qty: float
    required_qty: float
    usable: bool
    blocking_layer: str | None
    #: Quantity of this substitute that physically exists but is hard-assigned to
    #: another demand line, so it is excluded from `available_qty`. Non-zero is
    #: what distinguishes BLOCK_ORACLE_RELEASE from BLOCK_INVENTORY.
    hard_assigned_qty: float = 0.0
    #: What the planner should do about `blocking_layer`. None when nothing blocks.
    recommended_action: str | None = None
    #: Pending-approval over-subscription against THIS substitute, pool-wide.
    pending_line_count: int = 0
    pending_required_qty: float = 0.0
    over_subscribed: bool = False
    over_subscription_note: str | None = None


def _customer_id_for(demand_line: DemandLine) -> str:
    return demand_line.well.planning_node.customer_id


def find_candidates(
    db: Session,
    demand_line: DemandLine,
    available_qty_by_product: dict[str, float] | None = None,
    approval_override=None,
    hard_assigned_by_product: dict[str, float] | None = None,
    pending_load_by_product: dict[str, PendingSubstituteLoad] | None = None,
) -> list[SubstitutionCandidate]:
    """All technically-valid substitutes for `demand_line`, annotated per layer.

    `available_qty_by_product` lets a caller (e.g. the coverage engine mid-pass)
    override on-hand availability so substitute inventory already consumed by
    another line is not double-counted.

    `hard_assigned_by_product` is {product_id: quantity that physically exists in
    this Business Unit but is hard-assigned to a demand line other than this one,
    and is therefore NOT part of `available_qty_by_product`}. Exactly one meaning,
    so both callers agree: the coverage engine already nets assignment
    reservations out of the availability map it passes, and hands the same figure
    here. When the gap could be closed if -- and only if -- that quantity were
    released, the candidate is offered with `blocking_layer ==
    BLOCK_ORACLE_RELEASE` instead of the misleading `BLOCK_INVENTORY`. It is
    never usable: the platform does not override hard reservations.

    `pending_load_by_product` carries the pool-wide PendingApproval load on each
    substitute (see `PendingSubstituteLoad`) so the payload can show a planner
    that approving every pending line cannot all succeed. It is pool-wide
    information a single line cannot derive, so it is passed in rather than
    guessed; omitting it simply leaves those fields at their neutral defaults.

    When it is OMITTED the quantities are resolved here, BU-scoped, through
    `app.engines.inventory.on_hand_map` for the owning customer's Business Unit.
    That path used to read the global `Product.on_hand_qty`, which offered every
    caller without a map -- notably GET
    /demand-lines/{id}/substitution-candidates -- another BU's steel as this
    line's `available_qty`. There is no unscoped quantity left to read: an
    unmapped customer or a substitute with no row in this BU raises
    `InventoryNotScoped` rather than producing a BU-blind number.

    `approval_override` is an optional read-only hook with the signature
    ``(demand_line_id, from_product_id, to_product_id, persisted_status) ->
    status`` that supplies the WELL-LAYER approval status instead of the persisted
    WellSubstitutionApproval row. It exists so scenario planning can ask "what if
    the customer approved this substitute?" through THIS engine rather than a
    forked copy of it -- see app.engines.overrides. It is only ever a value
    substitution: nothing about the layer logic changes, the returned status is
    treated exactly as a persisted one would be, and no row is written or read
    differently. `approval_id` deliberately stays whatever the real row says (or
    None), so a caller can never mistake a hypothetical approval for a record it
    could act on.

    `demand_line` is duck-typed: an `app.engines.overrides.LineView` is accepted
    anywhere a DemandLine is, which is what lets scenario preview reuse this
    function with overridden quantities.
    """
    from_product_id = demand_line.product_id
    customer_id = _customer_id_for(demand_line)

    tech_rows = (
        db.query(TechnicalSubstitution)
        .filter(TechnicalSubstitution.from_product_id == from_product_id)
        .all()
    )
    if not tech_rows:
        return []

    to_ids = [t.to_product_id for t in tech_rows]

    # Whether WE resolved availability or the caller handed it to us decides
    # whether the hard-assigned quantity still has to be netted off: a
    # caller-supplied map has already excluded it (that is the documented meaning
    # of `hard_assigned_by_product`), whereas the raw BU figure resolved below has
    # not. Without this the same physical facts would answer differently depending
    # on which entry point asked.
    self_resolved_availability = available_qty_by_product is None

    if available_qty_by_product is None:
        # No caller-supplied availability, so resolve it here -- BU-scoped, via the
        # one resolver. Imported locally because app.engines.coverage imports this
        # module; a top-level import of inventory is fine today but the local one
        # keeps this file importable in isolation regardless of future edits there.
        # Resolved through `ownership_pool_map`, not `on_hand_map`, so this entry
        # point sees the SAME two ownership tiers the coverage pass does. A customer
        # that has uploaded stock of a substitute product may use it -- it is their
        # property, held for their own wells -- and a candidate list that showed only
        # company stock would tell them their own material was unavailable.
        # `.total` is asked for by name; see `app.engines.inventory.OwnershipPool`.
        from app.engines.inventory import ownership_pool_map

        customer = demand_line.well.planning_node.customer
        available_qty_by_product = {
            product_id: pool.total
            for product_id, pool in ownership_pool_map(
                db, customer, set(to_ids)
            ).items()
        }

    rules = {
        r.to_product_id: r
        for r in db.query(CustomerSubstitutionRule)
        .filter(
            CustomerSubstitutionRule.customer_id == customer_id,
            CustomerSubstitutionRule.from_product_id == from_product_id,
            CustomerSubstitutionRule.to_product_id.in_(to_ids),
        )
        .all()
    }

    # Newest first, but an APPROVED record always wins over a later Pending
    # re-request so re-requesting can never silently un-cover a line.
    approvals: dict[str, WellSubstitutionApproval] = {}
    for a in (
        db.query(WellSubstitutionApproval)
        .filter(
            WellSubstitutionApproval.demand_line_id == demand_line.id,
            WellSubstitutionApproval.from_product_id == from_product_id,
            WellSubstitutionApproval.to_product_id.in_(to_ids),
        )
        .order_by(WellSubstitutionApproval.requested_at.desc())
        .all()
    ):
        held = approvals.get(a.to_product_id)
        if held is None or (
            held.status != SubstitutionApprovalStatus.APPROVED
            and a.status == SubstitutionApprovalStatus.APPROVED
        ):
            approvals[a.to_product_id] = a

    candidates: list[SubstitutionCandidate] = []
    for tech in tech_rows:
        product = db.get(Product, tech.to_product_id)
        if product is None:
            continue

        rule = rules.get(tech.to_product_id)
        # No rule at all is treated as "not permitted": the customer layer is a
        # positive allow-list, so silence never grants permission.
        customer_allowed = bool(rule is not None and rule.allowed)

        approval = approvals.get(tech.to_product_id)
        approval_status = approval.status if approval is not None else None
        if approval_override is not None:
            approval_status = approval_override(
                demand_line.id, from_product_id, tech.to_product_id, approval_status
            )

        # `.get(..., 0.0)` is safe here and is not a reintroduced fallback: the map
        # is either the coverage engine's mid-pass availability (which seeds every
        # substitute target -- see coverage._substitute_target_ids) or the
        # BU-resolved map built above, which raises rather than omitting a stocked
        # product. A key genuinely absent from both means "no quantity available to
        # this line", which is what 0.0 says.
        hard_assigned = max(
            0.0, (hard_assigned_by_product or {}).get(product.id, 0.0)
        )
        available = available_qty_by_product.get(product.id, 0.0)
        if self_resolved_availability:
            available = max(0.0, available - hard_assigned)

        enough = available >= demand_line.quantity
        # Would releasing the hard assignment close the gap? Only then is naming
        # the Oracle step honest -- otherwise the material is short regardless and
        # the answer is insufficient-inventory.
        release_would_close = (available + hard_assigned) >= demand_line.quantity

        if not customer_allowed:
            blocking = BLOCK_CUSTOMER
        elif approval_status != SubstitutionApprovalStatus.APPROVED:
            blocking = BLOCK_WELL_APPROVAL
        elif enough:
            blocking = None
        elif hard_assigned > 0 and release_would_close:
            # OFFERED, never usable. The stock exists; the platform may not take
            # it, because doing so would override a hard reservation it does not
            # own. See the module docstring.
            blocking = BLOCK_ORACLE_RELEASE
        else:
            blocking = BLOCK_INVENTORY

        load = (pending_load_by_product or {}).get(product.id)

        candidates.append(
            SubstitutionCandidate(
                demand_line_id=demand_line.id,
                from_product_id=from_product_id,
                to_product_id=product.id,
                product=product,
                customer_allowed=customer_allowed,
                approval_status=approval_status,
                approval_id=approval.id if approval is not None else None,
                available_qty=available,
                required_qty=demand_line.quantity,
                usable=blocking is None,
                blocking_layer=blocking,
                hard_assigned_qty=hard_assigned,
                recommended_action=(
                    RECOMMENDED_ACTION.get(blocking) if blocking else None
                ),
                pending_line_count=load.pending_line_count if load else 0,
                pending_required_qty=load.pending_required_qty if load else 0.0,
                over_subscribed=bool(load and load.over_subscribed),
                over_subscription_note=load.note if load else None,
            )
        )

    return candidates


@dataclass(frozen=True)
class SubstitutionApprovalByDate:
    """The latest date a PendingApproval line can still be rejected and recovered
    by falling back to a mill order of its OWN (primary) product.

    Follows the codebase's existing "cannot answer" honesty pattern (see
    `MeasureOut` in app.schemas, `InventoryPositionOut`, `LeadTimeBreakdownOut`):
    `available=False` means DO NOT RENDER `approval_by_date`, render `reason`
    instead. There is deliberately no sentinel date.

    `applicable` decides the "line is not currently pursuing a substitution"
    edge case (see `approval_by_date` below). It is ALWAYS present rather than
    the field being absent/null on the payload -- a consistent shape is easier
    for a client to handle than a field that sometimes does not exist -- and
    `applicable=False` always implies `available=False`.

    `still_recoverable` is None whenever `available` is False (nothing to
    report), True when a mill order placed TODAY could still land the ROS
    (the ordinary "still has time" case, even though the WINDOW for a fresh
    order narrows as `approval_by_date` approaches), and False when the mill
    fallback is not merely narrowing but has become ALREADY IMPOSSIBLE --
    the ROS is inside the primary product's lead time even measured from
    today, so no decision speed can save it. `reason` states which case this
    is in words a planner does not have to infer from the flag.
    """

    demand_line_id: str
    applicable: bool
    available: bool
    approval_by_date: date | None
    still_recoverable: bool | None
    reason: str


def _approval_by_date_for_product_ros(
    db: Session,
    demand_line_id: str,
    product: Product,
    ros_date: datetime,
    today: date | None = None,
) -> SubstitutionApprovalByDate:
    """The date arithmetic itself, independent of whether a CoverageResult row
    has been written yet.

    Split out from `approval_by_date` so `app.engines.coverage` can call it
    WHILE it is still deciding the PendingApproval verdict for a line -- at
    that point in the pass there is no CoverageResult row to read `applicable`
    from, but the caller already knows the line is pending, so it hands this
    the product and ROS directly. `applicable` is always True here: the only
    caller that reaches this function without already knowing the line is
    pending is `approval_by_date`, which checks first.
    """
    breakdown = resolve_lead_time(db, product)
    if not breakdown.modelled:
        return SubstitutionApprovalByDate(
            demand_line_id=demand_line_id,
            applicable=True,
            available=False,
            approval_by_date=None,
            still_recoverable=None,
            reason=(
                "Primary product's lead time is not modelled ("
                f"{breakdown.note or 'missing ' + ', '.join(breakdown.missing_dimensions)}"
                ") -- an approval-by date would be a guess, not a calculation, "
                "so none is given"
            ),
        )

    _ship_by, order_by, lead_months = order_feasibility(db, product, ros_date)
    recoverable = is_recoverable(db, product, ros_date, today=today)
    ros_str = (ros_date.date() if isinstance(ros_date, datetime) else ros_date).isoformat()

    if recoverable:
        reason = (
            f"Reject by {order_by.isoformat()} at the latest -- a mill order of "
            f"the primary product placed on or before that date ({lead_months:g} "
            f"month lead time) can still land by ROS {ros_str}. Reject later than "
            "this and even an immediate primary-product order can no longer meet "
            "the ROS."
        )
    else:
        reason = (
            "Mill fallback is ALREADY IMPOSSIBLE, not merely narrowing: even a "
            f"primary-product order placed TODAY ({lead_months:g} month lead "
            f"time) cannot land by ROS {ros_str}. {order_by.isoformat()} is the "
            "date the arithmetic implies, but it has already passed -- rejecting "
            "this substitution now cannot be recovered by ordering the primary "
            "product, whatever is decided and whenever it is decided."
        )

    return SubstitutionApprovalByDate(
        demand_line_id=demand_line_id,
        applicable=True,
        available=True,
        approval_by_date=order_by,
        still_recoverable=recoverable,
        reason=reason,
    )


def approval_by_date(
    db: Session,
    demand_line: DemandLine,
    today: date | None = None,
) -> SubstitutionApprovalByDate:
    """Public entry point: the approval-by date for `demand_line`, or why there
    is none.

    "Currently pursuing a substitution" is read off the PERSISTED coverage
    verdict -- `CoverageResult.status == PENDING_APPROVAL` -- rather than
    re-derived from `find_candidates` here. `PENDING_APPROVAL` is written by
    `app.engines.coverage.compute_customer_coverage` for EXACTLY the case this
    feature is about (a technically-valid, customer-allowed substitute that
    could close the gap once approved, sitting on a Pending or unrequested
    well-layer approval -- see that module's pending-substitute branch), so
    reusing it is one definition instead of two that could disagree. A line
    with no candidate at all, or one already COVERED / COVERED_VIA_SUBSTITUTE
    / UNCOVERED / UNRECOVERABLE, is NOT applicable: there is no pending
    rejection decision hanging over it, so an approval-by date has nothing to
    answer.

    Raises nothing: an unevaluated line (no CoverageResult row) is treated the
    same as any other non-pending status -- not applicable -- because MRP's
    convention of surfacing a missing row as "unresolved" does not apply here;
    this feature answers a narrower question than "does this line have a
    verdict at all".
    """
    result = db.get(CoverageResult, demand_line.id)
    if result is None or result.status != CoverageStatus.PENDING_APPROVAL:
        return SubstitutionApprovalByDate(
            demand_line_id=demand_line.id,
            applicable=False,
            available=False,
            approval_by_date=None,
            still_recoverable=None,
            reason=(
                "This demand line is not currently pursuing a substitution "
                "(no PendingApproval coverage verdict), so there is no "
                "approval-by date to compute"
            ),
        )
    return _approval_by_date_for_product_ros(
        db, demand_line.id, demand_line.product, demand_line.ros_date, today=today
    )


def request_approval(
    db: Session,
    demand_line: DemandLine,
    from_product_id: str,
    to_product_id: str,
) -> WellSubstitutionApproval:
    """Create (or return the existing open) well-layer approval request.

    Requesting is allowed even when the technical/customer layers do not clear
    -- the layers are independent, and the request record is what the approver
    reviews. Re-requesting a pair that is already Pending or Approved returns
    that record rather than opening a duplicate; only a Rejected pair can be
    re-requested afresh.
    """
    existing = (
        db.query(WellSubstitutionApproval)
        .filter(
            WellSubstitutionApproval.demand_line_id == demand_line.id,
            WellSubstitutionApproval.from_product_id == from_product_id,
            WellSubstitutionApproval.to_product_id == to_product_id,
            WellSubstitutionApproval.status.in_(
                [SubstitutionApprovalStatus.PENDING, SubstitutionApprovalStatus.APPROVED]
            ),
        )
        .order_by(WellSubstitutionApproval.status)
        .first()
    )
    if existing is not None:
        return existing

    approval = WellSubstitutionApproval(
        demand_line_id=demand_line.id,
        from_product_id=from_product_id,
        to_product_id=to_product_id,
        status=SubstitutionApprovalStatus.PENDING,
        requested_at=datetime.utcnow(),
    )
    db.add(approval)
    db.flush()
    return approval


def decide_approval(db: Session, approval_id: str, approved: bool) -> WellSubstitutionApproval:
    """Approve or reject a well-layer approval request."""
    approval = db.get(WellSubstitutionApproval, approval_id)
    if approval is None:
        raise ValueError(f"WellSubstitutionApproval {approval_id} not found")
    approval.status = (
        SubstitutionApprovalStatus.APPROVED if approved else SubstitutionApprovalStatus.REJECTED
    )
    approval.decided_at = datetime.utcnow()
    db.flush()
    return approval
