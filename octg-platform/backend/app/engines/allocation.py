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
`app.engines.surplus` uses to define surplus. Three rules make that number
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
          offered to a neighbour -- so drawing on it
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
    not in `InventoryOnHand` at all, so the unassigned pool is unchanged and an
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
    # BU-WIDE PASS ONLY (see `allocate_business_unit`). `remaining_pool` above is
    # the ONE shared unassigned pool of the whole Business Unit; these two say what
    # is left of each customer's PRIVATE tiers, which the substitution fall-through
    # must offer to that customer's lines and to no others. Empty for the
    # single-customer helpers, whose residuals are the scalars above.
    remaining_customer_owned_by_customer: dict[str, float] = field(default_factory=dict)
    remaining_assignment_block_by_customer: dict[str, float] = field(
        default_factory=dict
    )
    # The part of `consumed_from_pool` that came from a SOFT customer's own pooled
    # assignment block rather than from the shared Business-Unit pool. Reported
    # separately for ONE reason: when a substitute later takes the line and its
    # own-product draw is released (C-17), each quantity has to go back to the tier
    # it came from -- a reservation returned to the shared pool would be a
    # reservation the platform had quietly released.
    consumed_from_assignment_block: dict[str, float] = field(default_factory=dict)

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
    """Full allocation outcome for ONE customer's lines of ONE product.

    A THIN ADAPTER over `allocate_business_unit`, which is the single
    implementation of the allocation rules. It was three separate policy
    functions until the Business Unit became the unit of allocation (D01); once
    production stopped calling them they were a second implementation that only
    the tests exercised, free to drift away from the rules actually being run.
    The signature is kept because "one customer, one product" is a genuinely
    useful thing to state directly, and because it is what the policy tests are
    written against.

    `on_hand_qty` is the COMPANY-OWNED quantity available to this pool (already
    net of any foreign assignment carve-out); the assignments in
    `assigned_qty_by_line` are a carve-out OF it, not additional steel.
    `customer_owned_qty` is what the customer itself owns of the same product,
    drawn FIRST under every policy -- see the module docstring, including why HARD
    grants coverage from it with no Oracle assignment.
    """
    assigned = assigned_qty_by_line or {}
    customer = "__one__"
    outcome = allocate_business_unit(
        lines=lines,
        company_on_hand=on_hand_qty,
        total_assigned=sum(max(0.0, assigned.get(line.id, 0.0)) for line in lines),
        assigned_by_line={
            line.id: max(0.0, assigned.get(line.id, 0.0)) for line in lines
        },
        # Empty: the SOFT block exists so a soft customer's reservations stay out
        # of a NEIGHBOUR's reach, and there is no neighbour here. Under SOFT the
        # caller passes no assignments at all, which is the same statement.
        assignment_block_by_customer={customer: 0.0},
        customer_owned_by_customer={customer: max(0.0, customer_owned_qty)},
        policy_by_customer={customer: policy},
        customer_of_line={line.id: customer for line in lines},
    )
    outcome.remaining_customer_owned = (
        outcome.remaining_customer_owned_by_customer.get(customer, 0.0)
    )
    return outcome


def allocate_business_unit(
    lines,
    company_on_hand: float,
    total_assigned: float,
    assigned_by_line: dict[str, float],
    assignment_block_by_customer: dict[str, float],
    customer_owned_by_customer: dict[str, float],
    policy_by_customer: dict[str, AllocationPolicy],
    customer_of_line: dict[str, str],
) -> AllocationOutcome:
    """Allocate ONE product's Business-Unit stock across the in-scope demand of
    EVERY customer in that Business Unit, in one pass.

    THE CHANGE THIS FUNCTION IS (product-owner ruling 2026-09-06, D01)
    -----------------------------------------------------------------
    Until this existed, each customer was allocated against the WHOLE Business
    Unit quantity independently, so two customers could each be told the same
    6,000 metres were theirs and the sum of the promises exceeded the steel. The
    unassigned pool is now contested ONCE, earliest-ROS-first, across the whole
    Business Unit -- so what the BU has promised can never exceed what it holds.

    Nothing else about allocation changes. The three tiers, their order, the
    partial-consumption rule and each policy's meaning are exactly as the
    per-customer helpers above implement them, and the ownership walls are kept by
    construction rather than by convention:

      1. CUSTOMER-OWNED (`customer_owned_by_customer`) -- keyed by customer, so a
         line can only ever draw its own customer's property. Never shared.
      2. ORACLE ASSIGNMENT -- keyed by LINE for HARD/HYBRID, so an assignment is
         drawable only by the line it names. For a SOFT customer it is keyed by
         CUSTOMER instead (`assignment_block_by_customer`): soft allocation means
         "do not tie my steel to specific wells of MINE", and that is exactly what
         the per-customer pass did (it ignored its own assignments and left that
         quantity in its own pool) -- but the steel still belongs to that customer
         and no neighbour may take it.
      3. THE SHARED UNASSIGNED POOL -- `company_on_hand` less EVERY assignment in
         the Business Unit (`total_assigned`, including assignments on lines this
         pass does not evaluate: reserved steel is never free pool). This is the
         one tier that crosses the customer line, and the only one that does.

    HARD lines never touch tier 3, exactly as before, and their assignment is
    recorded as drawn only when it covers the line's whole remaining requirement --
    also as before. HYBRID and SOFT draw partially and the quantity is genuinely
    spent, which is what makes the trade-off auditable.

    ORDERING: earliest ROS first across the WHOLE Business Unit, ties broken by the
    caller's order (`app.engines.coverage` sorts by `(ros_date, id)`, so the total
    order is deterministic and does not depend on which customer a line belongs
    to). The owner rejected a customer-priority ordering; urgency decides.
    """
    shared_pool = max(0.0, company_on_hand - max(0.0, total_assigned))
    owned = {c: max(0.0, q) for c, q in customer_owned_by_customer.items()}
    block = {c: max(0.0, q) for c, q in assignment_block_by_customer.items()}
    # WHAT PHYSICALLY EXISTS, spent alongside the entitlements above.
    #
    # A reservation is an ENTITLEMENT to company steel, not steel of its own, and
    # Oracle can hold more of them than the Business Unit has metres -- a scenario
    # assignment override can invent the same state deliberately. Without this
    # counter the entitlements would each be honoured in full and the Business Unit
    # would promise what it does not hold, which is the very thing this pass
    # exists to prevent (D01). Customer-owned steel is NOT counted here: it is the
    # customer's own property and is not part of `company_on_hand` at all.
    physical = max(0.0, company_on_hand)
    outcome = AllocationOutcome(remaining_pool=shared_pool)

    for line in sorted(lines, key=lambda l: l.ros_date):
        customer_id = customer_of_line[line.id]
        policy = policy_by_customer[customer_id]
        need = line.quantity

        from_owned = min(need, owned.get(customer_id, 0.0))
        if from_owned > 0:
            outcome.consumed_from_customer_owned[line.id] = from_owned
            owned[customer_id] -= from_owned
            need -= from_owned

        if policy == AllocationPolicy.SOFT:
            # The customer's own assignments, pooled across its own lines. Recorded
            # as a POOL draw because that is what the per-customer pass called it:
            # under SOFT this quantity was simply part of the customer's pool, and
            # the reason text and the Executive channels are written against that
            # meaning.
            from_block = min(need, block.get(customer_id, 0.0), physical)
            if from_block > 0:
                outcome.consumed_from_pool[line.id] = from_block
                outcome.consumed_from_assignment_block[line.id] = from_block
                block[customer_id] -= from_block
                physical -= from_block
                need -= from_block
        else:
            available = min(max(0.0, assigned_by_line.get(line.id, 0.0)), physical)
            if policy == AllocationPolicy.HARD:
                # No partial record: an assignment that cannot close the line is
                # still reserved to it, but nothing has been drawn. Unchanged.
                if available >= need and need > 0:
                    outcome.consumed_from_assignment[line.id] = need
                    physical -= need
                    need = 0.0
            else:
                from_assignment = min(available, need)
                if from_assignment > 0:
                    outcome.consumed_from_assignment[line.id] = from_assignment
                    physical -= from_assignment
                    need -= from_assignment

        if policy != AllocationPolicy.HARD and need > 0:
            from_pool = min(need, shared_pool, physical)
            if from_pool > 0:
                physical -= from_pool
                outcome.consumed_from_pool[line.id] = (
                    outcome.consumed_from_pool.get(line.id, 0.0) + from_pool
                )
                shared_pool -= from_pool
                need -= from_pool

        outcome.covered[line.id] = need <= 0

    outcome.remaining_pool = max(0.0, shared_pool)
    outcome.remaining_customer_owned_by_customer = {
        c: max(0.0, q) for c, q in owned.items()
    }
    outcome.remaining_assignment_block_by_customer = {
        c: max(0.0, q) for c, q in block.items()
    }
    outcome.remaining_customer_owned = sum(
        outcome.remaining_customer_owned_by_customer.values()
    )
    return outcome


