"""Cross-customer shared-inventory ANALYSIS -- read-only what-if.

The question this answers, and only this question
------------------------------------------------
    "Which of THIS customer's uncovered demand could be covered if inventory
     were shared across customers within the same Business Unit?"

The product owner's framing:

    Inventory is separated by Business Unit first -- always. It is separated by
    customer too as the default, but here we sometimes want to verify it as
    shared stock across the customer line (the uncovered-demand case).

So: BU separation is absolute; customer separation is the default; and the ONE
thing a planner may do across the customer line is *verify* -- for uncovered
demand -- whether a neighbour's stock would have helped.

It writes NOTHING
-----------------
This module is a projection, not a recalculation. The official verdict stays
customer-scoped and is produced solely by `app.engines.coverage.recompute_customer`.
Four separate things keep that true, deliberately layered:

  1. It never imports `recompute_customer`, `CoverageResult`, or anything else
     that persists. Coverage is re-derived here from the PURE
     `app.engines.allocation.allocate_detailed`, which takes values and returns
     values. The persisting code path is simply not reachable from this file.
  2. Every result is a frozen dataclass of scalars -- no ORM instances escape, so
     a caller cannot accidentally mutate a mapped attribute it was handed.
  3. The whole computation runs inside `Session.no_autoflush`, so no query issued
     mid-analysis can flush unrelated pending state as a side effect.
  4. `_assert_no_writes` compares the session's pending new/dirty/deleted sets
     before and after, and raises if the analysis added anything. That is a
     tripwire for a future edit, not a substitute for (1).

It never leaves the Business Unit
---------------------------------
Sharing is intra-BU by construction: the donor set is built by querying
`Customer.business_unit_id == customer.business_unit_id`, and every donor is
`assert`ed to match before its stock is considered. A customer with no BU mapped
(see app.models.customer.Customer) gets an empty, explained result -- it is
isolated, not universally compatible.

Surplus vs. committed: why we do not rob Peter to pay Paul
----------------------------------------------------------
The tempting-but-wrong answer is "take stock from customer B's covered line and
give it to customer A's uncovered line". That does not create coverage, it moves
it -- B's well flips to Uncovered and the BU is no better off, while the analysis
reports a win. So the only inventory offered here is GENUINE SURPLUS: what is
left in the BU after every customer in it has taken the quantity its own in-scope
demand is committed to.

"Committed" is deliberately wider than "consumed", because an assignment is
reserved to its line whether or not that line ended up covered (the same rule
`app.engines.allocation` enforces). Per customer c and product P:

    committed_c(P) = Σ over c's in-scope lines of P:
                        max(assigned to the line, quantity drawn)

    shareable(P)   = max(0, bu_on_hand(P) - Σ_{c in BU} committed_c(P))

That formula is now policy-BLIND, and the change matters. SOFT used to contribute
`drawn` only, on the reasoning that "no reservations exist under SOFT". True of a
soft customer's own coverage -- pooling means its assignments do not constrain it
-- and false here, because this module decides what a customer may give AWAY. A
soft customer's assigned steel is still hard-assigned in Oracle to one of its
specific demand lines, so counting it as surplus would offer a neighbour material
the platform would have to override an Oracle fact to move. Since
`committed_c(P) >= assigned_c(P)` for every c, it follows that
`shareable(P) <= bu_on_hand(P) - (total assigned in the BU)`: third-party
hard-assigned steel is never offered, as arithmetic rather than as a promise.

CUSTOMER-OWNED STOCK IS NEVER SHAREABLE, IN EITHER DIRECTION
-----------------------------------------------------------
This is a boundary TIGHTER than the Business Unit boundary, and it is the one
place in the platform where that sentence is true.

`app.models.customer_owned_inventory.CustomerOwnedInventory` holds material the
CUSTOMER owns. It is not ours. Offering it to a neighbour -- or offering a
neighbour's to this customer -- would have the platform propose transferring
property that is not the company's to move, which is a categorically worse error
than the Oracle-assignment override this module already refuses: an assignment is
at least our steel, wrongly promised.

The exclusion is STRUCTURAL, not a filter:

  * `shareable(P)` is computed from `bu_on_hand(P)`, and `bu_on_hand` comes from
    `app.engines.inventory.on_hand_map`, which reads `InventoryOnHand` and
    NOTHING ELSE. Customer-owned quantities live in a different table with a
    different scope, and no expression that produces `shareable` ever touches
    the function that reads it. There is no line to forget to write, because the
    quantity never enters the arithmetic in the first place.
  * `_customer_pass` DOES read customer-owned stock -- it must, or its `covered`
    would disagree with the official verdict and this analysis would offer to fix
    lines the customer's own steel already fixed. It keeps the two apart by
    counting ONLY company-owned draws into `committed`, and the customer-owned
    draw is available separately for the tripwire below.
  * `_assert_customer_owned_never_shared` re-derives the bound
    `shareable(P) <= bu_on_hand(P)` and raises if it is ever violated. That is the
    arithmetic signature of customer-owned stock having leaked in: the only way
    surplus can exceed what `InventoryOnHand` says the BU holds is if a quantity
    from somewhere else was added to it.

Note the second-order effect, which is correct and worth stating: a customer that
covers its lines from its OWN steel draws less company steel, so its
`committed_c(P)` falls and `shareable(P)` RISES. That is not a leak. The company
steel it did not need is genuinely free, and saying so is the honest answer -- what
must never happen is the customer's own material being counted into that figure.

Note `shareable` can be zero even when several customers each look comfortable.
Each customer's official pass is computed against the FULL BU quantity (that is
what customer separation means -- see recompute_customer), so the BU can have
promised more than it holds. When that is the case the honest answer is "no
surplus exists", and this analysis says so rather than manufacturing one.

Donor attribution is indicative, the pool is binding
---------------------------------------------------
`shareable(P)` is a single BU-level number; inventory carries a BU dimension, not
a customer one, so there is no ground truth for "whose 500 tonnes is it". Donors
are therefore reported as the customers that (a) actually have in-scope demand for
P -- so they have a real claim on that stock -- and (b) are committed to less than
the BU holds. They are listed largest-slack-first, each capped at its own slack,
until `shareable` is used up.

Condition (a) matters more than it looks. Without it, a customer that has no
demand for P at all scores the maximum possible slack (it consumed none of the BU
figure) and is reported as the largest donor of a product it has never ordered.
That is BU-level unallocated stock being mislabelled as a named customer's
generosity, and it would send a planner to phone the wrong operator. When no
donor qualifies, `contributions` is empty and the explanation says plainly that
the surplus is BU-level unallocated stock. The per-donor split is a hint about who
to call; the defensible quantity is always `shareable`.

Out of scope on purpose
-----------------------
  * Substitution. This asks about the line's OWN product; a line that its own
    product cannot cover but a substitute can has already been resolved by the
    coverage engine and is not uncovered.
  * Scenario planning (a later phase). Scenarios override demand and supply
    VALUES; this changes the POOLING SCOPE of the same values. Adjacent, not the
    same -- no scenario infrastructure is built or used here.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.engines.allocation import allocate_detailed
from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
)
from app.engines.inventory import customer_owned_map, on_hand_map
from app.models import (
    AllocationPolicy,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryAssignment,
    PlanningNode,
    UnitOfMeasure,
    Well,
)


@dataclass(frozen=True)
class SharingContribution:
    """One donor customer's indicative contribution to one uncovered line."""

    from_customer_id: str
    from_customer_name: str
    quantity: float
    #: Unit of the product this contribution is of. A contribution always concerns
    #: the single product of its parent `SharedLineOutcome`, so it is never mixed.
    unit_of_measure: UnitOfMeasure


@dataclass(frozen=True)
class SharedLineOutcome:
    """What BU-level sharing would (or would not) do for one uncovered line."""

    demand_line_id: str
    well_id: str
    well_name: str
    product_id: str
    product_description: str | None
    quantity: float
    #: Labels `quantity`, `shared_quantity` AND `shortfall` -- all three are
    #: quantities of `product_id`, so one value covers all of them.
    unit_of_measure: UnitOfMeasure
    ros_date: object  # datetime; kept loose so no ORM type leaks into the schema
    official_status: str
    would_be_covered: bool
    shared_quantity: float
    shortfall: float
    contributions: tuple[SharingContribution, ...] = ()
    explanation: str = ""


@dataclass(frozen=True)
class ProductSurplus:
    """BU-level surplus position for one product the analysis looked at."""

    product_id: str
    product_description: str | None
    bu_on_hand: float
    committed_in_bu: float
    shareable: float
    #: Labels all three figures above. This model is PER PRODUCT, which is why the
    #: sharing analysis needs no cross-unit aggregate anywhere: `shareable` is
    #: deliberately never rolled up into a single headline number.
    #:
    #: None only when the product row has vanished from the catalogue, the same
    #: condition that makes `product_description` None -- there is then no unit to
    #: state, and inventing one would label a quantity with a guess.
    unit_of_measure: UnitOfMeasure | None = None


@dataclass(frozen=True)
class CrossCustomerSharingAnalysis:
    """Read-only what-if result. Nothing here has been persisted."""

    customer_id: str
    customer_name: str
    business_unit_id: str | None
    business_unit_name: str | None
    # Customers whose surplus was eligible to be considered. Always intra-BU.
    donor_customer_ids: tuple[str, ...] = ()
    uncovered_lines: tuple[SharedLineOutcome, ...] = ()
    product_surplus: tuple[ProductSurplus, ...] = ()
    covered_by_sharing_count: int = 0
    still_uncovered_count: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# Pure per-customer re-derivation (no writes, no CoverageResult involvement)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _CustomerPass:
    """One customer's own-product allocation outcome, recomputed in memory.

    `covered` mirrors what `recompute_customer` would decide from own-product
    stock alone. It intentionally does NOT model the substitution fall-through:
    the lines this analysis is about are lines that fell all the way through, and
    a line that a substitute rescued is not uncovered.
    """

    covered: dict[str, bool]
    # {product_id: quantity of COMPANY-OWNED stock this customer is committed to} --
    # see module docstring. Customer-owned draws are deliberately NOT in here: this
    # figure exists only to shrink `shareable`, which is a company-stock figure, and
    # adding a quantity the company does not own would make the subtraction
    # dimensionally wrong in the worst direction (it would shrink the company surplus
    # by the customer's own property).
    committed: dict[str, float]
    # {product_id: quantity drawn from the customer's OWN stock}. Reported so the
    # tripwire and the tests can see that it was read and kept out of `committed`,
    # rather than having to infer it from a number that does not contain it.
    customer_owned_drawn: dict[str, float]


def _included_lines(
    db: Session,
    customer_id: str,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
) -> list[DemandLine]:
    """The same lines `recompute_customer` would evaluate, for one customer.

    Filters are threaded through rather than hardcoded so the analysis always
    asks about exactly the demand the official pass evaluated -- an analysis run
    against a different scope than the verdict it is explaining would be
    meaningless.
    """
    # `None` means the platform's CURRENT coverage scope (see
    # `app.engines.coverage_scope`), resolved per call. It is NOT the shipped
    # constant: this analysis exists to explain the official verdict, so it has to
    # ask about exactly the demand the official pass evaluated -- which is whatever
    # scope is in force, not whatever scope shipped.
    status_filter = set(status_filter or effective_status_filter(db))
    profile_filter = set(profile_filter or effective_profile_filter(db))
    lines = (
        db.query(DemandLine)
        .join(Well, DemandLine.well_id == Well.id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(
            PlanningNode.customer_id == customer_id,
            # Status selects WELLS -- see `app.models.well.Well.demand_status`.
            # Expressed in SQL rather than in a loop below so the two filters stay
            # visibly at different granularities.
            Well.demand_status.in_(sorted(status_filter, key=lambda s: s.value)),
        )
        .all()
    )
    return [line for line in lines if line.profile in profile_filter]


def _assigned_by_line(
    db: Session, policy: AllocationPolicy, lines: list[DemandLine]
) -> dict[tuple[str, str], float]:
    """{(line_id, product_id): assigned qty} for `lines`, for EVERY policy.

    It used to return `{}` under SOFT, mirroring the old
    `app.engines.coverage._assignment_context`. It no longer may, and the reason is
    a boundary crossing rather than a policy question.

    Inside a soft customer's own pass an assignment on its own line is correctly
    ignored -- pooling. But this module does something the coverage engine never
    does: it computes what a customer would give AWAY. A soft customer's assigned
    steel is still hard-assigned in Oracle to one of its specific demand lines, so
    offering it to a NEIGHBOUR as surplus would be the platform overriding that
    assignment -- the same violation the coverage engine now refuses one level up,
    reintroduced through the analysis path. `policy` is therefore no longer
    consulted: whose steel it is does not depend on how its owner likes to allocate.

    Note this does NOT give a soft line coverage here either: `allocate_detailed`
    discards the assignment map under SOFT (see app.engines.allocation), so these
    figures only ever reach `committed`.
    """
    if not lines:
        return {}
    line_ids = [line.id for line in lines]
    out: dict[tuple[str, str], float] = {}
    for row in (
        db.query(InventoryAssignment)
        .filter(InventoryAssignment.demand_line_id.in_(line_ids))
        .all()
    ):
        key = (row.demand_line_id, row.product_id)
        out[key] = out.get(key, 0.0) + max(0.0, row.quantity or 0.0)
    return out


def _official_status(line: DemandLine) -> str:
    """The line's ACTUAL stored coverage verdict, verbatim.

    Display-only fix (product-owner decision, 2026-08-12): this panel used to
    hardcode "Uncovered (customer-scoped)" on every row, which misrepresented
    lines whose official verdict is PendingApproval or Unrecoverable. The
    panel selects lines by the re-derived "not covered" test, so several
    verdicts can appear here -- each row now names its own.
    """
    cr = line.coverage_result
    return cr.status.value if cr is not None else "Not evaluated"


def _reserved_in_bu(
    db: Session, business_unit_id: str, product_ids: set[str]
) -> dict[str, float]:
    """{product_id: qty hard-assigned to ANY demand line in this Business Unit}.

    The BU-wide figure, deliberately: each customer's pass turns it into its own
    "reserved to somebody else" number by subtracting that customer's own
    assignments, which is the identical arithmetic
    `app.engines.coverage.compute_customer_coverage` performs. Computing it once
    here keeps the two engines agreeing by construction rather than by comment.

    BU-scoped for the usual reason -- a reservation in another BU draws on another
    BU's physical stock and must not shrink this one.
    """
    if not product_ids:
        return {}
    out: dict[str, float] = {}
    for row in (
        db.query(InventoryAssignment)
        .join(DemandLine, InventoryAssignment.demand_line_id == DemandLine.id)
        .join(Well, DemandLine.well_id == Well.id)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .join(Customer, PlanningNode.customer_id == Customer.id)
        .filter(
            Customer.business_unit_id == business_unit_id,
            InventoryAssignment.product_id.in_(sorted(product_ids)),
        )
        .all()
    ):
        out[row.product_id] = out.get(row.product_id, 0.0) + max(
            0.0, row.quantity or 0.0
        )
    return out


def _customer_pass(
    db: Session,
    customer: Customer,
    bu_on_hand: dict[str, float],
    reserved_in_bu: dict[str, float] | None = None,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
) -> _CustomerPass:
    """Re-derive one customer's own-product coverage and committed quantities.

    Pure with respect to the database: it reads, then hands values to
    `allocate_detailed`. It does not touch CoverageResult in either direction --
    neither reading the stored verdict (which can be stale) nor writing one.
    """
    lines = _included_lines(db, customer.id, status_filter, profile_filter)
    policy = customer.allocation_policy
    assigned = _assigned_by_line(db, policy, lines)

    by_product: dict[str, list[DemandLine]] = {}
    for line in lines:
        by_product.setdefault(line.product_id, []).append(line)

    covered: dict[str, bool] = {}
    committed: dict[str, float] = {}
    customer_owned_drawn: dict[str, float] = {}

    # The customer's OWN stock, read so that `covered` agrees with the official
    # verdict (a line its own steel covered is not uncovered and must not be offered
    # sharing). It is deliberately kept in its own variable and never merged into
    # `bu_on_hand`, which is the company figure `shareable` is computed from.
    owned = customer_owned_map(db, customer, set(by_product))

    for product_id, product_lines in by_product.items():
        own_assigned = {
            line.id: assigned.get((line.id, product_id), 0.0) for line in product_lines
        }
        # Stock hard-assigned to a demand line of ANOTHER customer in this BU is
        # not available to this customer at all, so it comes off before allocating
        # -- byte for byte the subtraction `compute_customer_coverage` performs.
        # Without it this re-derivation would call a line Covered that the official
        # pass calls Uncovered, and the analysis would silently omit that line from
        # the very shortfall it exists to explain.
        reserved_elsewhere = max(
            0.0,
            (reserved_in_bu or {}).get(product_id, 0.0)
            - sum(own_assigned.values()),
        )
        available = max(0.0, bu_on_hand.get(product_id, 0.0) - reserved_elsewhere)
        owned_qty = owned[product_id].drawable if product_id in owned else 0.0
        outcome = allocate_detailed(
            policy, product_lines, available, own_assigned, owned_qty
        )
        covered.update(outcome.covered)
        customer_owned_drawn[product_id] = outcome.total_customer_owned_consumed

        total = 0.0
        for line in product_lines:
            # COMPANY-owned draws only. `consumed_from_customer_owned` is excluded on
            # purpose and is the one thing this sum must not contain -- see the
            # `committed` field comment.
            drawn = outcome.consumed_from_assignment.get(
                line.id, 0.0
            ) + outcome.consumed_from_pool.get(line.id, 0.0)
            # An assignment stays reserved to its line even when the line ended up
            # uncovered, so it is committed either way -- and now for EVERY policy.
            # A soft customer's own assignment does not constrain the soft customer
            # (pooling), but it absolutely constrains what may be offered to a
            # NEIGHBOUR: handing that steel to another customer as "surplus" would
            # override the Oracle assignment, which is precisely what this platform
            # never does. Since `committed` exists only to shrink `shareable`, the
            # honest figure here is the wider one for all three policies.
            #
            # `max` (not a sum) because a covered line's draw already INCLUDES the
            # part it took from its own assignment. The consequence worth stating:
            # Σ_c committed_c(P) >= Σ_c assigned_c(P), so
            # `shareable(P) <= on_hand(P) - total assigned(P)` -- third-party
            # hard-assigned steel can never be offered, by arithmetic.
            total += max(own_assigned.get(line.id, 0.0), drawn)
        committed[product_id] = total

    return _CustomerPass(
        covered=covered,
        committed=committed,
        customer_owned_drawn=customer_owned_drawn,
    )


def _assert_customer_owned_never_shared(
    product_id: str,
    bu_on_hand_qty: float,
    committed_total: float,
    shareable: float,
) -> None:
    """Tripwire: surplus can never exceed what `InventoryOnHand` says the BU holds.

    `shareable = max(0, bu_on_hand - committed)` with `committed >= 0`, so this bound
    holds by construction TODAY. It is asserted anyway, because it is the exact
    arithmetic signature of the failure this module must never have: the only way
    surplus can exceed the company's own on-hand figure is if a quantity from
    somewhere else was added to it, and the only other inventory quantity in the
    platform is a CUSTOMER'S OWN PROPERTY.

    So this is not a check that the formula was typed correctly. It is a check that
    nobody has "helpfully" widened `bu_on_hand` to include customer-owned stock -- a
    change that would look like an improvement (more surplus found!) and would have
    the platform proposing to move steel the company does not own. It raises rather
    than clamping, for the same reason `_assert_no_writes` does: a silently corrected
    what-if is one nobody finds out was wrong.
    """
    if shareable > max(0.0, bu_on_hand_qty) + 1e-9:
        raise AssertionError(
            f"shareable surplus of product {product_id!r} is {shareable:g}, which "
            f"exceeds the Business Unit's own on-hand quantity {bu_on_hand_qty:g} "
            f"(committed {committed_total:g}). Surplus is COMPANY stock only. The "
            "likely cause is customer-owned inventory having been added into "
            "bu_on_hand -- that material is the CUSTOMER'S PROPERTY and can never be "
            "offered to another customer, in either direction. Resolve company "
            "on-hand through app.engines.inventory.on_hand_map, which reads "
            "InventoryOnHand and nothing else."
        )


# --------------------------------------------------------------------------
# Write tripwire
# --------------------------------------------------------------------------


def _pending_snapshot(db: Session) -> tuple[int, int, int]:
    return (len(db.new), len(db.dirty), len(db.deleted))


def _assert_no_writes(db: Session, before: tuple[int, int, int]) -> None:
    """Fail loudly if the analysis left anything pending in the session.

    Layer 4 of the no-write guarantee (see module docstring). If a future edit
    introduces a write here, this raises during the analysis rather than letting
    a what-if quietly overwrite the official coverage answer on the next commit.
    """
    after = _pending_snapshot(db)
    if after != before:
        raise AssertionError(
            "cross_customer_sharing must not modify the session: pending "
            f"(new, dirty, deleted) went from {before} to {after}. This analysis "
            "is a read-only projection -- the official coverage answer is written "
            "only by app.engines.coverage.recompute_customer."
        )


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def cross_customer_sharing(
    db: Session,
    customer: Customer,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
) -> CrossCustomerSharingAnalysis:
    """Would BU-level inventory sharing cover `customer`'s uncovered demand?

    Read-only. Writes nothing, commits nothing, and never crosses the Business
    Unit boundary. See the module docstring for the surplus definition and for
    the four mechanisms that make the no-write property structural.
    """
    before = _pending_snapshot(db)
    notes: list[str] = []

    with db.no_autoflush:
        bu_id = customer.business_unit_id

        if bu_id is None:
            # Conservative failure mode. An unmapped customer is ISOLATED, so
            # there is no BU whose surplus it could legitimately draw on -- and
            # crucially it must not be pooled with every OTHER unmapped customer.
            return CrossCustomerSharingAnalysis(
                customer_id=customer.id,
                customer_name=customer.name,
                business_unit_id=None,
                business_unit_name=None,
                notes=(
                    f"Customer '{customer.name}' is not mapped to a Business "
                    "Unit. Inventory sharing is only ever evaluated within one "
                    "BU, so no sharing is offered. Map the customer to a "
                    "Business Unit to run this analysis.",
                ),
            )

        bu = customer.business_unit
        peers = (
            db.query(Customer)
            .filter(Customer.business_unit_id == bu_id)
            .order_by(Customer.name)
            .all()
        )
        # Belt and braces: the query above is the boundary, this is the assertion
        # the spec asks for in code rather than only in prose.
        for peer in peers:
            assert peer.business_unit_id == bu_id, (
                "cross-BU leak: donor set must contain only customers of BU "
                f"{bu_id!r}, got {peer.id!r} in {peer.business_unit_id!r}"
            )
        donors = [p for p in peers if p.id != customer.id]

        # Products in play: everything this customer demands, so the surplus
        # position is computed for exactly the products its shortfall needs.
        own_lines = _included_lines(db, customer.id, status_filter, profile_filter)
        product_ids = {line.product_id for line in own_lines}
        bu_on_hand = on_hand_map(db, bu_id, product_ids)
        reserved_in_bu = _reserved_in_bu(db, bu_id, product_ids)

        # Every customer in the BU is re-derived, including this one: the surplus
        # definition subtracts EVERY customer's own commitment, so a peer whose
        # demand is silently omitted would make the surplus look larger than it is.
        #
        # `bu_on_hand` is keyed only on the products THIS customer demands, so a
        # peer's lines for any other product see available=0 in its pass. That is
        # harmless by construction: only `committed[product_id]` for product_ids
        # in `product_ids` is ever read below, and those keys are all present.
        # Nothing in a peer's pass is reported to the caller.
        own_pass = _customer_pass(
            db, customer, bu_on_hand, reserved_in_bu, status_filter, profile_filter
        )
        peer_passes = {
            p.id: (
                own_pass
                if p.id == customer.id
                else _customer_pass(
                    db, p, bu_on_hand, reserved_in_bu, status_filter, profile_filter
                )
            )
            for p in peers
        }

        # ---- surplus per product -------------------------------------------
        surplus: dict[str, float] = {}
        surplus_rows: list[ProductSurplus] = []
        # {product_id: [(slack, customer)]} for indicative donor attribution.
        slack_by_product: dict[str, list[tuple[float, Customer]]] = {}

        products_by_id = {line.product_id: line.product for line in own_lines}

        for product_id in sorted(product_ids):
            on_hand_qty = bu_on_hand.get(product_id, 0.0)
            committed_total = sum(
                peer_passes[p.id].committed.get(product_id, 0.0) for p in peers
            )
            shareable = max(0.0, on_hand_qty - committed_total)
            _assert_customer_owned_never_shared(
                product_id, on_hand_qty, committed_total, shareable
            )
            surplus[product_id] = shareable

            slack: list[tuple[float, Customer]] = []
            for donor in donors:
                donor_committed = peer_passes[donor.id].committed
                # A donor is only credited for a product it actually HAS in-scope
                # demand for. Without this gate a customer that has never heard of
                # the product came out as the biggest donor of all -- its
                # "unconsumed" quantity was the entire BU figure, simply because it
                # consumed none of it. That is not a customer sharing its stock,
                # it is BU-level unallocated inventory being mislabelled, and it
                # would send a planner to phone the wrong operator.
                if product_id not in donor_committed:
                    continue
                donor_slack = max(0.0, on_hand_qty - donor_committed[product_id])
                if donor_slack > 0:
                    slack.append((donor_slack, donor))
            slack.sort(key=lambda pair: (-pair[0], pair[1].name))
            slack_by_product[product_id] = slack

            product = products_by_id.get(product_id)
            surplus_rows.append(
                ProductSurplus(
                    product_id=product_id,
                    product_description=product.description if product else None,
                    unit_of_measure=(
                        product.unit_of_measure if product is not None else None
                    ),
                    bu_on_hand=on_hand_qty,
                    committed_in_bu=committed_total,
                    shareable=shareable,
                )
            )

        # ---- allocate the surplus to this customer's uncovered lines --------
        # Earliest ROS first, matching the coverage engine's ordering rule so the
        # what-if answers the same question in the same order the real engine
        # would have.
        uncovered = sorted(
            (line for line in own_lines if not own_pass.covered.get(line.id, False)),
            key=lambda line: line.ros_date,
        )

        outcomes: list[SharedLineOutcome] = []
        covered_count = 0
        for line in uncovered:
            pool = surplus.get(line.product_id, 0.0)
            product = line.product

            if line.quantity <= pool:
                taken = line.quantity
                surplus[line.product_id] = pool - taken
                contributions = _attribute(
                    taken,
                    slack_by_product.get(line.product_id, []),
                    unit_of_measure=product.unit_of_measure,
                )
                covered_count += 1
                bu_label = bu.name if bu is not None else bu_id
                if contributions:
                    sources = ", ".join(
                        f"{c.from_customer_name} ({c.quantity:g})"
                        for c in contributions
                    )
                    explanation = (
                        f"Coverable by sharing: {taken:g} of surplus "
                        f"{product.description or product.id} exists within Business "
                        f"Unit '{bu_label}' after every customer's own committed "
                        f"demand. Indicative source(s): {sources}."
                    )
                else:
                    explanation = (
                        f"Coverable within the Business Unit: {taken:g} of "
                        f"{product.description or product.id} is unallocated in "
                        f"Business Unit '{bu_label}'. No OTHER customer in the BU "
                        "has demand for this product, so the surplus is BU-level "
                        "unallocated stock rather than a transfer from a named "
                        "customer."
                    )
                outcomes.append(
                    SharedLineOutcome(
                        demand_line_id=line.id,
                        well_id=line.well_id,
                        well_name=line.well.name,
                        product_id=line.product_id,
                        product_description=product.description,
                        quantity=line.quantity,
                        unit_of_measure=product.unit_of_measure,
                        ros_date=line.ros_date,
                        official_status=_official_status(line),
                        would_be_covered=True,
                        shared_quantity=taken,
                        shortfall=0.0,
                        contributions=contributions,
                        explanation=explanation,
                    )
                )
                continue

            # Not coverable. Partial surplus is NOT consumed here.
            #
            # NOTE the tension, rather than leaving a rationale that no longer
            # matches the engine: the reasoning below ("spending it would deny it to
            # MVP-COMPROMISE[C-10]: sharing analysis is all-or-nothing per line,
            #     unlike production coverage's ROS-ordered partial consumption.
            #     WHY: partial sharing needs a product-owner ruling on splits.
            #     REMOVE: align with the allocation engine's partial draw.
            # a later line it could genuinely close") is exactly the all-or-nothing
            # argument the product owner REVERSED for real allocation -- see
            # app.engines.allocation. It is deliberately left alone because this is
            # a read-only what-if about a hypothetical pooling scope, not a claim
            # about which steel physically moves: nothing is drawn, no
            # `remaining_pool` is published, and the question a planner asks here is
            # "could sharing close this line", to which a partial answer is no.
            # Whether the what-if should mirror the new rule is an OPEN QUESTION for
            # the owner; changing it was not part of the substitute-search ruling.
            shortfall = line.quantity - pool
            outcomes.append(
                SharedLineOutcome(
                    demand_line_id=line.id,
                    well_id=line.well_id,
                    well_name=line.well.name,
                    product_id=line.product_id,
                    product_description=product.description,
                    quantity=line.quantity,
                    unit_of_measure=product.unit_of_measure,
                    ros_date=line.ros_date,
                    official_status=_official_status(line),
                    would_be_covered=False,
                    shared_quantity=0.0,
                    shortfall=shortfall,
                    contributions=(),
                    explanation=(
                        f"Not coverable by sharing: needs {line.quantity:g} but only "
                        f"{pool:g} of {product.description or product.id} is genuine "
                        f"surplus within Business Unit '{bu.name if bu else bu_id}' "
                        f"(short by {shortfall:g}). Stock already committed to "
                        "another customer's own demand is not offered -- moving it "
                        "would simply uncover that customer instead."
                    ),
                )
            )

        if not donors:
            notes.append(
                f"Business Unit '{bu.name if bu else bu_id}' contains no other "
                "customer, so there is no cross-customer sharing to evaluate. Any "
                "surplus reported is this customer's own unconsumed stock."
            )
        notes.append(
            "Read-only what-if. The official coverage verdict remains "
            "customer-scoped and is unchanged by this analysis."
        )
        notes.append(
            "Sharing is evaluated within one Business Unit only. Stock held in "
            "any other BU is never offered, whatever its quantity."
        )
        notes.append(
            "CUSTOMER-OWNED inventory is excluded from every surplus figure here, in "
            "both directions: it cannot be offered away, and another customer's "
            "customer-owned stock is never offered to you. It is that customer's "
            "property, not the company's, so proposing to move it would propose "
            "moving something we do not own -- a boundary tighter than the Business "
            "Unit boundary. It IS taken into account when deciding which of your "
            "lines are uncovered in the first place, because your own material "
            "covers your own demand first."
        )

        result = CrossCustomerSharingAnalysis(
            customer_id=customer.id,
            customer_name=customer.name,
            business_unit_id=bu_id,
            business_unit_name=bu.name if bu is not None else None,
            donor_customer_ids=tuple(d.id for d in donors),
            uncovered_lines=tuple(outcomes),
            product_surplus=tuple(surplus_rows),
            covered_by_sharing_count=covered_count,
            still_uncovered_count=len(outcomes) - covered_count,
            notes=tuple(notes),
        )

    _assert_no_writes(db, before)
    return result


def _attribute(
    quantity: float,
    slack: list[tuple[float, Customer]],
    unit_of_measure: UnitOfMeasure = UnitOfMeasure.MTR,
) -> tuple[SharingContribution, ...]:
    """Split `quantity` across donors, largest slack first, each capped at its own.

    Indicative only -- inventory carries a BU dimension, not a customer one, so
    there is no ground truth for whose tonnage this is. See the module docstring;
    the binding number is the BU-level shareable pool, not this split.
    """
    out: list[SharingContribution] = []
    remaining = quantity
    for donor_slack, donor in slack:
        if remaining <= 0:
            break
        take = min(donor_slack, remaining)
        if take <= 0:
            continue
        out.append(
            SharingContribution(
                from_customer_id=donor.id,
                from_customer_name=donor.name,
                quantity=take,
                unit_of_measure=unit_of_measure,
            )
        )
        remaining -= take
    return tuple(out)
