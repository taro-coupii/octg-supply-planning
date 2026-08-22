"""Allocation engine -- Phase 3.

"Allocation Rules Vary By Business Model. There is no single allocation model."
Three policies, one per customer (see app.models.customer.AllocationPolicy):

  SOFT    Inventory is a pooled resource. Coverage is decided earliest-ROS-first
          against the whole on-hand quantity. Inventory assignments, if any
          exist, are IGNORED -- pooling is the entire point of soft allocation.

  HARD    Specific inventory is assigned to specific demand. A line is covered
          only if inventory has been explicitly assigned to it in sufficient
          quantity. Unassigned pool stock does NOT cover a hard-allocated line
          however much of it is sitting on the shelf, and there is no pool
          top-up for a partial assignment.

  HYBRID  ("most common" per the spec)
              Use Assigned Inventory First
                -> Remaining Inventory Pool
                -> Allocate by ROS
          A line draws on its own assignment first; any residual requirement
          then competes for the UNASSIGNED remainder of the pool on an
          earliest-ROS-first basis.

Assignment data is an Oracle-owned local projection -- see
app.models.inventory_assignment.InventoryAssignment.

PARTIAL CONSUMPTION BY EARLIEST ROS
-----------------------------------
Under SOFT and HYBRID a line draws whatever the pool can give it, in
earliest-ROS order, EVEN WHEN THAT DOES NOT COMPLETE THE LINE. This replaces the
former all-or-nothing rule, on the product owner's ruling. The worked example
they decided (unassigned pool 2000):

    Line A  ROS Sep-1  needs 5000, assigned 1000  -> shortfall 4000
    Line B  ROS Oct-1  needs 2000, assigned    0  -> shortfall 2000

    A draws its 1000 assignment AND all 2000 of the pool; it is still 3000
    short, so it stays UNCOVERED. B draws nothing and is UNCOVERED too.

Rationale: physically the earlier well gets the pipe. The steel goes onto the
earlier truck whether or not it is enough to finish that well, so a model in
which A leaves the pool untouched for B describes a warehouse nobody operates.

The previous rule optimised the COUNT of covered wells (never spend steel on a
line that cannot be completed, so a later line the remainder could finish is not
starved). The consequence of reversing it is therefore accepted and explicit:

    THE TOTAL NUMBER OF COVERED WELLS CAN DROP.

In the example above B would have been Covered under the old rule. That is the
trade-off the owner chose, not a bug -- see
tests/test_allocation_policies.py::test_partial_consumption_can_reduce_the_covered_well_count,
which asserts the decrease deliberately so nobody "fixes" it later.

HARD is deliberately NOT changed: coverage there comes from the line's own
assignment alone, with no pool top-up, so there is no shared pool for a partial
draw to come out of.

SOFT changed together with HYBRID on purpose. It is the identical physical
situation with no assignment involved, so leaving SOFT all-or-nothing would make
"soft allocation" mean two different things depending on the customer's policy.

Partial consumption is NOT coverage
-----------------------------------
Coverage semantics are untouched. Only a fully-satisfied line is covered; a
partially-drawn line is reported by the coverage engine as UNCOVERED (or falls
through to substitution / UNRECOVERABLE exactly as before). The change is about
which steel is spent, never about which verdict is reported.

Residual-pool accounting
------------------------
`allocate_detailed` returns the free pool left after the pass, which the
coverage engine hands to the substitution fall-through and
`app.engines.sharing` uses to define surplus. Three rules make that number
honest:

  * Assigned quantity is a CARVE-OUT of on_hand, never additive. The
    unassigned pool is `on_hand - total_assigned`.
  * Inventory reserved to a line is not free pool even when that line ended up
    uncovered -- it belongs to that line, so it is excluded from the residual
    under HARD and HYBRID whether it was consumed or not.
  * A PARTIAL draw is a real draw: it comes out of `remaining_pool` and is
    recorded in `consumed_from_pool`, exactly once. `remaining_pool` never goes
    negative and never double-counts, because every line's pool draw is
    `min(shortfall, pool)` and is subtracted from `pool` in the same step.

Under SOFT the residual is `on_hand - consumed`, where consumed now includes
partial draws.

CUSTOMER-OWNED STOCK IS DRAWN FIRST, UNDER EVERY POLICY
-------------------------------------------------------
The product owner's ruling:

    For the same product, consuming customer-owned inventory takes priority
    over FIFO.

For the same product, consuming CUSTOMER-OWNED inventory takes priority over the
ordinary draw order. Customer-owned stock is material the customer owns, absent
from Oracle entirely and uploaded into this platform -- see
`app.models.customer_owned_inventory.CustomerOwnedInventory`. The pool therefore
has an OWNERSHIP TIER, resolved in `app.engines.inventory.ownership_pool_map` (the
one place the Business Unit boundary is decided, so the one place the tier belongs
too), and every line draws:

    1. the customer's OWN steel        (customer-owned tier)
    2. then its own Oracle assignment  (HARD / HYBRID only)
    3. then the shared unassigned pool (SOFT / HYBRID only)

ONE tier order for all three policies. That is deliberate: "customer-owned first"
has to mean one thing, and a per-policy variation would make the sentence a planner
was given depend on a setting they cannot see.

What that means per policy, and why HARD is the interesting one
--------------------------------------------------------------
  SOFT    Unchanged in character: pooled, earliest-ROS-first, partial draws real.
          It now runs over TWO tiers, in ROS order, taking the customer's own steel
          before the company pool. Equivalently: sweep the customer-owned tier in
          ROS order, then sweep the company pool in ROS order -- both formulations
          give identical results, because both tiers are greedy and ROS-ordered, and
          the per-line form below is the one that reads as the physical story.

  HARD    A CUSTOMER-OWNED QUANTITY GRANTS COVERAGE WITH NO ORACLE ASSIGNMENT.
          Reasoned, not convenient. Hard allocation means "coverage comes only from
          steel physically earmarked for this line", and the earmarking lives in
          Oracle. ORACLE CANNOT ASSIGN INVENTORY IT DOES NOT KNOW ABOUT, and it does
          not know customer-owned material exists. So requiring an assignment would
          make customer-owned stock PERMANENTLY UNUSABLE under HARD -- a hard-policy
          customer could upload its own steel and be told it has none, forever, with
          no action available anywhere that would fix it. That is not a stricter
          policy, it is a dead end.

          The spirit of HARD is preserved rather than bent: what HARD refuses is a
          top-up from the SHARED pool, i.e. steel that is not this line's. The
          customer's own property is not shared with anybody -- it cannot even be
          offered to a neighbour (see `app.engines.sharing`) -- so drawing on it
          takes nothing from anyone and creates no contention for a reservation to
          resolve. HARD's no-pool-top-up rule is untouched: `remaining_pool` is
          still the unassigned company remainder and no hard line ever touches it.

          Lines still do not compete for the customer-owned tier in ROS order under
          HARD, for the same reason they do not compete for assignments: HARD is
          processed in list order because nothing is shared. The tier IS finite, so
          it is drawn in the order given and the remainder is reported; a HARD
          customer with more demand than owned steel gets whichever lines the caller
          passed first, which is exactly the existing HARD contract for a scarce
          assignment.

  HYBRID  Customer-owned, then assignment, then the unassigned pool -- position 1
          in the list above. It goes BEFORE the assignment, not between the
          assignment and the pool, because the ruling is about priority of
          CONSUMPTION and the cheapest steel to consume is the steel the customer
          already owns. Consuming an Oracle assignment first would burn a company
          reservation while the customer's own material sat on the dock.

Accounting invariants, all preserved
-----------------------------------
  * Customer-owned quantity is ADDITIVE to on-hand, never a carve-out of it. It is
    not in `InventoryOnHand` at all, so `_unassigned_pool` is unchanged and an
    assignment can never reserve customer-owned steel.
  * `remaining_pool` still means "free, unreserved COMPANY stock" and is untouched
    by a customer-owned draw. `remaining_customer_owned` is reported separately.
  * A customer-owned draw is a real draw: recorded once in
    `consumed_from_customer_owned`, subtracted from the tier in the same step,
    never negative and never double-counted -- the same three rules the pool draw
    already obeyed.
  * `drawn(line_id)` sums all THREE maps, so no caller can disagree about whether
    a customer-owned draw counts.

Scope of one call
-----------------
`allocate_detailed` is called ONCE PER PRODUCT for the whole customer pool, with
`lines` being every included demand line of every well under that customer. That
is deliberate and load-bearing: the earliest-ROS ordering inside `_allocate_soft`
and `_allocate_hybrid` is what makes coverage independent of the order wells are
processed in. Calling this per well and carrying the remainder forward would hand
scarce steel to whichever well happened to be processed first. See
app.engines.coverage.recompute_customer.
"""

from dataclasses import dataclass, field

from app.models import AllocationPolicy, DemandLine


@dataclass
class AllocationOutcome:
    """Result of allocating one product's on-hand quantity across every demand
    line for that product in one customer's pool."""

    covered: dict[str, bool] = field(default_factory=dict)
    # Free, unreserved on-hand quantity left over -- what a substitution
    # fall-through may safely draw on. Never negative.
    remaining_pool: float = 0.0
    # Quantity drawn from each line's own assignment, for explainability. Present
    # for PARTIALLY drawn lines too, not only covered ones.
    consumed_from_assignment: dict[str, float] = field(default_factory=dict)
    # Quantity drawn from the shared unassigned pool, per line. Present for
    # partial draws, which is what makes the trade-off auditable.
    consumed_from_pool: dict[str, float] = field(default_factory=dict)
    # Quantity drawn from the CUSTOMER-OWNED tier, per line -- the customer's own
    # property, drawn before anything else under every policy. Present for partial
    # draws, exactly like the other two maps.
    #
    # Reported rather than folded into `consumed_from_pool` because the Executive
    # Dashboard's `soft_allocation_coverage` channels and the coverage reason text
    # both have to say WHERE the steel came from: "we covered you off your own
    # material" and "we covered you off ours" are different sentences, and a planner
    # deciding what to order needs the difference.
    consumed_from_customer_owned: dict[str, float] = field(default_factory=dict)
    # What is left of the customer-owned tier after the pass. Kept separate from
    # `remaining_pool` because the two can never be spent on the same thing: the
    # residual company pool may be drawn by a SUBSTITUTE for any line of this
    # customer, while this residual is the customer's own property of THIS product.
    remaining_customer_owned: float = 0.0

    def drawn(self, line_id: str) -> float:
        """Total quantity this line actually consumed: customer-owned + assignment
        + pool.

        The one place the three maps are summed, so callers cannot disagree about
        whether a partial draw counts.
        """
        return (
            self.consumed_from_customer_owned.get(line_id, 0.0)
            + self.consumed_from_assignment.get(line_id, 0.0)
            + self.consumed_from_pool.get(line_id, 0.0)
        )

    @property
    def total_customer_owned_consumed(self) -> float:
        """Sum of every line's customer-owned draw.

        Exists so a consumer can assert the ownership split against the total drawn
        without re-summing a map it might sum differently.
        """
        return sum(self.consumed_from_customer_owned.values())


def allocate(
    policy: AllocationPolicy,
    lines: list[DemandLine],
    on_hand_qty: float,
    assigned_qty_by_line: dict[str, float] | None = None,
    customer_owned_qty: float = 0.0,
) -> dict[str, bool]:
    """Return {demand_line_id: is_covered} for one product's on-hand pool.

    `assigned_qty_by_line` is optional and additive: omitting it keeps the
    original two-argument call working, and it is ignored entirely under SOFT.
    HARD and HYBRID with no assignments passed simply see every line as
    unassigned (so HARD covers nothing, and HYBRID degrades to pure ROS
    ordering over the whole pool).

    `customer_owned_qty` is likewise optional and defaults to 0.0, so a caller that
    knows nothing about ownership tiers keeps the previous behaviour exactly. It is
    ADDITIVE to `on_hand_qty` rather than part of it -- see the module docstring.

    Callers needing the residual pool or the ownership/assignment/pool split should
    use `allocate_detailed`; this thin wrapper exists so existing call sites and
    tests keep the exact same contract.
    """
    return allocate_detailed(
        policy, lines, on_hand_qty, assigned_qty_by_line, customer_owned_qty
    ).covered


def allocate_detailed(
    policy: AllocationPolicy,
    lines: list[DemandLine],
    on_hand_qty: float,
    assigned_qty_by_line: dict[str, float] | None = None,
    customer_owned_qty: float = 0.0,
) -> AllocationOutcome:
    """Full allocation outcome: coverage flags plus residual accounting.

    `on_hand_qty` is the COMPANY-OWNED quantity available to this pool (already net
    of any foreign assignment carve-out). `customer_owned_qty` is what the customer
    itself owns of the same product, drawn FIRST under every policy -- see the
    module docstring, including why HARD grants coverage from it with no Oracle
    assignment.
    """
    assigned = assigned_qty_by_line or {}
    customer_owned = max(0.0, customer_owned_qty)

    if policy == AllocationPolicy.SOFT:
        return _allocate_soft(lines, on_hand_qty, customer_owned)
    if policy == AllocationPolicy.HARD:
        return _allocate_hard(lines, on_hand_qty, assigned, customer_owned)
    if policy == AllocationPolicy.HYBRID:
        return _allocate_hybrid(lines, on_hand_qty, assigned, customer_owned)
    raise ValueError(f"Unknown allocation policy: {policy!r}")


def _unassigned_pool(lines: list[DemandLine], on_hand_qty: float, assigned: dict[str, float]) -> float:
    """The part of on_hand that is not reserved to any of these lines.

    Assignments are a carve-out of on_hand, so this can only shrink it. Clamped
    at zero because a projection that over-assigns relative to on-hand is an
    upstream (Oracle) data problem, and pretending the pool is negative would
    make coverage results nonsensical rather than merely pessimistic.
    """
    total_assigned = sum(max(0.0, assigned.get(line.id, 0.0)) for line in lines)
    return max(0.0, on_hand_qty - total_assigned)


def _allocate_soft(
    lines: list[DemandLine], on_hand_qty: float, customer_owned_qty: float = 0.0
) -> AllocationOutcome:
    """Earliest-ROS-first greedy allocation over TWO ownership tiers.

    PARTIAL CONSUMPTION: a line takes `min(quantity, what both tiers can give)`. It
    is covered only if that equals its whole requirement, but the steel is drawn
    either way -- see the module docstring for the ruling and the accepted
    consequence.

    OWNERSHIP PRIORITY: each line drains the customer-owned tier before touching the
    company pool. Because both tiers are swept in the same earliest-ROS order, this
    is identical to "exhaust customer-owned across every line, then exhaust the
    pool"; the per-line form is written because it is the physical story -- the
    truck is loaded from the customer's own pipe first.
    """
    owned = max(0.0, customer_owned_qty)
    remaining = on_hand_qty
    outcome = AllocationOutcome(
        remaining_pool=on_hand_qty, remaining_customer_owned=owned
    )
    for line in sorted(lines, key=lambda l: l.ros_date):
        from_owned = min(line.quantity, owned)
        if from_owned > 0:
            outcome.consumed_from_customer_owned[line.id] = from_owned
            owned -= from_owned

        shortfall = line.quantity - from_owned
        from_pool = min(shortfall, remaining) if shortfall > 0 else 0.0
        if from_pool > 0:
            outcome.consumed_from_pool[line.id] = from_pool
            remaining -= from_pool

        outcome.covered[line.id] = (from_owned + from_pool) >= line.quantity
    outcome.remaining_pool = max(0.0, remaining)
    outcome.remaining_customer_owned = max(0.0, owned)
    return outcome


def _allocate_hard(
    lines: list[DemandLine],
    on_hand_qty: float,
    assigned: dict[str, float],
    customer_owned_qty: float = 0.0,
) -> AllocationOutcome:
    """Coverage from the physical assignment, plus the customer's OWN steel first.

    No pool top-up, unchanged: unassigned COMPANY stock still does not cover a
    hard-allocated line however much of it is on the shelf.

    ROS order remains irrelevant to the assignment: lines do not compete for it,
    because each can only ever draw on inventory already earmarked for it. The
    CUSTOMER-OWNED tier is finite and therefore drawn in the order the caller passed
    the lines -- which is the existing HARD contract for a scarce reservation, not a
    new rule. HARD deliberately has no ROS-ordered competition to inherit.

    WHY CUSTOMER-OWNED STOCK COVERS A HARD LINE WITH NO ORACLE ASSIGNMENT: Oracle
    cannot assign inventory it does not know about, and it does not know
    customer-owned material exists at all, so requiring an assignment would make
    that material permanently unusable under HARD -- with no action available to
    anybody that would ever fix it. The full argument, including why this does not
    weaken HARD, is in the module docstring.

    UNCHANGED by the partial-consumption ruling for the ASSIGNMENT: the ruling is
    about which line wins a SHARED pool, and a hard-allocated line never touches the
    shared pool. A customer-owned draw IS recorded partially, because the tier is
    genuinely finite and genuinely spent -- the quantity is gone and a later line
    will find it missing, which is exactly the condition that made partial recording
    mandatory for the pool.
    """
    owned = max(0.0, customer_owned_qty)
    outcome = AllocationOutcome(remaining_customer_owned=owned)
    for line in lines:
        from_owned = min(line.quantity, owned)
        if from_owned > 0:
            outcome.consumed_from_customer_owned[line.id] = from_owned
            owned -= from_owned

        shortfall = line.quantity - from_owned
        available = max(0.0, assigned.get(line.id, 0.0))
        covered = available >= shortfall
        outcome.covered[line.id] = covered
        if covered and shortfall > 0:
            outcome.consumed_from_assignment[line.id] = shortfall
    # Everything assigned stays reserved to its line whether or not it covered
    # it; only genuinely unassigned stock is free for the fall-through. The
    # customer-owned tier is NOT part of this -- it is not company stock and cannot
    # be offered as a substitute to any other customer.
    outcome.remaining_pool = _unassigned_pool(lines, on_hand_qty, assigned)
    outcome.remaining_customer_owned = max(0.0, owned)
    return outcome


def _allocate_hybrid(
    lines: list[DemandLine],
    on_hand_qty: float,
    assigned: dict[str, float],
    customer_owned_qty: float = 0.0,
) -> AllocationOutcome:
    """Customer-owned first, then the assignment, then the unassigned remainder.

    PARTIAL CONSUMPTION: the pool draw is `min(shortfall, pool)`, taken in
    earliest-ROS order, and it happens whether or not it completes the line. See
    the module docstring for the owner's worked example, the rationale, and the
    accepted consequence that the covered-well count can fall.

    The customer-owned tier sits BEFORE the assignment rather than between it and
    the pool: the ruling is about priority of consumption, and burning an Oracle
    reservation while the customer's own material sits on the dock is exactly the
    order it exists to forbid.
    """
    outcome = AllocationOutcome()
    pool = _unassigned_pool(lines, on_hand_qty, assigned)
    owned = max(0.0, customer_owned_qty)

    for line in sorted(lines, key=lambda l: l.ros_date):
        from_owned = min(line.quantity, owned)
        if from_owned > 0:
            outcome.consumed_from_customer_owned[line.id] = from_owned
            owned -= from_owned

        from_assignment = min(
            max(0.0, assigned.get(line.id, 0.0)), line.quantity - from_owned
        )
        if from_assignment > 0:
            # Recorded even for a line that ends up uncovered: the assignment is
            # this line's own steel and it is drawn on regardless.
            outcome.consumed_from_assignment[line.id] = from_assignment

        shortfall = line.quantity - from_owned - from_assignment
        from_pool = min(shortfall, pool) if shortfall > 0 else 0.0
        if from_pool > 0:
            pool -= from_pool
            outcome.consumed_from_pool[line.id] = from_pool

        outcome.covered[line.id] = (
            from_owned + from_assignment + from_pool
        ) >= line.quantity

    outcome.remaining_pool = max(0.0, pool)
    outcome.remaining_customer_owned = max(0.0, owned)
    return outcome
