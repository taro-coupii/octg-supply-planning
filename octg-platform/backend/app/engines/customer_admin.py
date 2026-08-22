"""Editing the Business Unit -> Customer hierarchy and a customer's allocation
policy, and reporting what the edit did.

`Customer.business_unit_id` and `Customer.allocation_policy` were seed-fixed. There
was no way to create a Business Unit, to map a customer into one, or to change the
model its coverage is judged under. This module is the part that is NOT plumbing:
deciding whether such an edit may proceed at all, and stating what it changed.

It is the deliberate sibling of `app.engines.lead_time_admin` and
`app.engines.substitution_admin` and follows their shape on purpose -- snapshot,
mutate, recompute, diff, SAVEPOINT-isolated so a failed recompute never leaves
half-written verdicts -- so the administrative surfaces cannot drift into reporting
their blast radius differently.

WHY THESE TWO FIELDS ARE WRITEABLE WHEN THE ADMIN SCREEN SAID THEY WERE NOT
==========================================================================
The screen said "Business Units, customers and allocation policies are maintained
upstream". That was true of the CUSTOMER MASTER -- who the customers are, and what
they are called -- and it stays true: nothing here creates or renames a customer.

It was never true of these two fields. Neither is an Oracle projection:

  `business_unit_id` is THIS PLATFORM'S mapping of a customer onto its inventory
  pool. `app.models.inventory_on_hand.InventoryOnHand` is keyed on
  (business_unit_id, product_id) and the BU rows are this platform's own; the FK is
  deliberately NULLABLE precisely so an operator can create a customer, see which
  screens refuse to compute, and FIX THE MAPPING -- see
  `app.models.customer.Customer`, which calls making it NOT NULL "a defensible
  follow-up". There was no way to perform that fix. The docstring described a
  workflow the API did not offer.

  `allocation_policy` is a commercial modelling choice ("Allocation Rules Vary By
  Business Model" -- see `app.engines.allocation`). Oracle holds no such column. It
  arrived by being typed into a seed script, which is not a system of record.

WHY THIS IS A BIGGER CHANGE THAN THE OTHER TWO ADMIN SURFACES
============================================================
A lead-time component or a substitution row changes an INPUT to a coverage pass. The
two fields here change WHICH POOL the pass reads and HOW it is divided:

  allocation_policy   rewrites the customer's ENTIRE coverage. SOFT pools the
                      on-hand quantity earliest-ROS-first; HARD grants coverage only
                      from a physical `InventoryAssignment` (with no pool top-up
                      however much steel is on the shelf); HYBRID is assignment then
                      pool. Every well of the customer must be re-derived, and
                      verdicts move a long way -- SOFT -> HARD turns covered demand
                      uncovered wherever no assignment exists.

  business_unit_id    changes the inventory pool ENTIRELY. Every
                      `InventoryOnHand` / `InventoryOnOrder` resolution, the
                      `InventoryAssignment` netting scope
                      (`app.engines.inventory.scoped_customer_ids`) and the
                      cross-customer sharing analysis all key on the BU. The
                      boundary is absolute and never crossed by any code path, so a
                      remap is not a relabelling: it is a different warehouse.

THE THREE REFUSALS, AND WHY EACH IS A REFUSAL RATHER THAN A REPORT
=================================================================
`substitution_admin` performs its write and REPORTS the customers it could not
re-derive. That is right there: a technical substitution is BU-agnostic, so one
Business Unit's missing inventory feed must not veto an engineering claim that
applies to every BU, and the customers concerned are ones the operator did not
mention and cannot fix from that screen.

Here the failing customer IS the subject of the edit, so the same reasoning inverts.
"The change you asked for leaves this customer with no computable coverage" is not a
footnote about somebody else; it is the outcome of the request.

  1. REMAP TO A BU WHERE THE CUSTOMER'S DEMAND CANNOT RESOLVE -> REFUSED, ROLLED
     BACK. `InventoryAssignment` and `CustomerOwnedInventory` do not travel with the
     customer, and the destination BU may hold no `InventoryOnHand` row for products
     this customer demands. `app.engines.inventory` refuses to invent a quantity, so
     the pass raises `InventoryRowMissing` and there is no verdict to store.

     Completing the remap and reporting the failure would leave the customer HALF
     MOVED: a `business_unit_id` asserting membership of a pool, and stored verdicts
     that were computed against the OLD pool and now describe nothing -- the
     "superseded assumption presented as current" defect the rest of this codebase
     refuses to ship. Worse, the state is not obviously broken from any screen: the
     Administration tree would show the customer neatly under its new BU.

     So the remap itself is rolled back and the refusal NAMES THE PRODUCTS that have
     no `InventoryOnHand` row in the destination, so the operator can have the feed
     loaded and retry. The engine's own message is passed through verbatim beside
     the list, because it names the BU and states the corrective action.

  2. REMAP AWAY FROM A BU TO NULL -> REFUSED, ALWAYS. Not a mapping correction: it
     is a de-configuration with no data-feed fix available to anybody. NULL means
     "not yet mapped", which is a BIRTH state -- see `app.models.customer.Customer`
     -- and a customer that HAS been mapped cannot honestly return to never having
     been. The consequence is total: `scoped_customer_ids`, `on_hand_map` and the
     HARD/HYBRID netting all raise `InventoryScopeMissing`, so every coverage read,
     every MRP figure and every dashboard tile for that customer 409s. Nothing about
     the feed could fix it, because there is no pool to feed.

     If a customer genuinely leaves, that is a customer-master decision, not an
     inventory-scope edit, and this platform does not own the customer master.

  3. A POLICY CHANGE ON A CUSTOMER WHOSE COVERAGE ALREADY CANNOT RESOLVE ->
     PERFORMED, AND THE FAILURE REPORTED. The opposite decision to (1), and the
     asymmetry is the point rather than an inconsistency.

     The set of products a pass must resolve is IDENTICAL under all three policies --
     `ownership_pool_map` is called for the demanded products, and the policy only
     decides how the resulting quantities are divided. So a policy change CANNOT
     cause an `InventoryRowMissing`; a failure after one was already there before it.
     Refusing would make `allocation_policy` permanently uneditable for exactly the
     customers whose configuration most needs attention -- including every unmapped
     customer, whose policy could then never be set correctly BEFORE mapping it. That
     is a dead end, and this codebase's standing objection to dead ends is stated in
     `app.engines.allocation` (why HARD grants coverage from customer-owned stock).

     `coverage_resolvable_before` and `unresolved_reason` carry the caveat, so no
     reader mistakes an empty `well_changes` for "nothing moved".

     The probe that establishes "already failing" is `compute_customer_coverage`,
     which WRITES NOTHING. It is the read-only half of `recompute_customer`, so the
     baseline costs no savepoint and cannot leave a trace.

ONE RECOMPUTE, WHETHER ONE FIELD CHANGED OR BOTH
================================================
`recomputes_performed` is on the report, and it is 1 for a combined change, not 2.
Both fields feed the SAME pass -- the BU decides the quantities `ownership_pool_map`
resolves, the policy decides how `allocate_detailed` divides them -- so one pass
after both mutations is not an optimisation, it is the only correct order. Two passes
would compute an intermediate state (new BU under the old policy) that the operator
never asked for and that would briefly be the stored answer.

ONLY THIS CUSTOMER IS RECOMPUTED, AND THAT IS PROVABLY ENOUGH
=============================================================
`lead_time_admin` and `substitution_admin` recompute EVERY customer because their
tables are customer-agnostic. These two fields are columns on one row, and
`recompute_customer` is customer-scoped by construction (see its docstring: demand
scope is the customer, and inventory is pooled across that customer's wells and never
further). Two customers in the same BU are each computed against the full BU
quantity, independently, so moving one in or out changes no other customer's stored
verdict.

The one thing a remap DOES change for a neighbour is the read-only
`app.engines.sharing` what-if, which defines surplus across a whole BU. It writes
nothing and is computed on demand, so there is nothing to repair -- the next read is
already correct.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    DemandLine,
    InventoryOnHand,
    PlanningNode,
    Product,
    Well,
)

#: Sentinel for "this field was not supplied". `None` is a REAL, meaningful value for
#: `business_unit_id` (it is the unmapped state), so a default of None could not be
#: distinguished from an explicit request to unmap -- which is refusal (2) above and
#: must not be reachable by omitting a field.
UNSET: Any = object()


@dataclass(frozen=True)
class WellCoverageChange:
    """One well whose coverage rollup moved. Scalars only.

    Same shape as `app.engines.lead_time_admin.WellCoverageChange` and
    `app.engines.substitution_admin.WellCoverageChange`, and serialised by the same
    `WellCoverageRollupChangeOut`, so the Administration screen renders one kind of
    before/after row whichever section produced it.
    """

    well_id: str
    well_name: str
    coverage_before: str | None
    coverage_after: str | None


class CustomerConfigRefused(Exception):
    """The requested configuration change was NOT made, and nothing was written.

    Carries a machine-readable `reason` plus the identifiers a client needs to render
    an actionable message without re-deriving anything. Raised only after the
    attempted mutation has been reverted in the session, so the customer is never
    left half-moved.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        destination_business_unit_id: str | None = None,
        missing_product_ids: tuple[str, ...] = (),
        engine_message: str | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.destination_business_unit_id = destination_business_unit_id
        self.missing_product_ids = missing_product_ids
        self.engine_message = engine_message


@dataclass(frozen=True)
class CustomerConfigChange:
    """What one PATCH of a customer's configuration actually did."""

    customer_id: str
    customer_name: str

    business_unit_changed: bool
    business_unit_id_before: str | None
    business_unit_id_after: str | None
    business_unit_name_before: str | None
    business_unit_name_after: str | None

    allocation_policy_changed: bool
    allocation_policy_before: str
    allocation_policy_after: str

    #: How many of this customer's wells were VISITED. Without it a reader cannot tell
    #: "nothing moved" from "nothing was looked at" -- the same distinction
    #: `products_examined` keeps on the lead-time report and `wells_examined` on the
    #: substitution one.
    wells_examined: int
    well_changes: tuple[WellCoverageChange, ...]

    #: 1 for any real change, INCLUDING one that moved both fields at once; 0 when
    #: nothing changed. Reported rather than implied so "recomputed exactly once" is a
    #: checkable claim rather than a docstring promise.
    recomputes_performed: int

    #: Could this customer's coverage be resolved BEFORE the change? False means its
    #: inventory facts were already incomplete (or it had no Business Unit at all).
    coverage_resolvable_before: bool
    #: The engine's own refusal message when coverage STILL cannot be resolved after
    #: the change. Non-null is a caveat on every other number here: the stored verdicts
    #: were rolled back to exactly what they were, so they are now KNOWN to predate
    #: this change. Only ever non-null for a policy-only change -- a BU change that
    #: ends unresolvable is refused outright.
    unresolved_reason: str | None = None

    #: True when the payload asked for the values already in force. Nothing was
    #: written and nothing was recomputed.
    unchanged: bool = False


def customer_wells(db: Session, customer: Customer) -> list[Well]:
    """Every well under `customer` -- the whole inventory pool scope.

    The same flat join `app.engines.coverage._customer_wells` performs, repeated here
    rather than imported because that name is private to the coverage engine and this
    module must not reach into it. PlanningNode carries `customer_id` on every node, so
    no recursive walk of `parent_id` is needed.
    """
    return (
        db.query(Well)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id == customer.id)
        .all()
    )


def demanded_product_ids(db: Session, customer: Customer) -> list[str]:
    """Every product this customer's demand lines name, in any status or profile.

    Deliberately UNFILTERED by the current coverage scope. The question being asked is
    "can this customer be administered into this Business Unit", and a product that is
    out of scope today is in scope the moment somebody Confirms a well -- so a
    destination BU missing its row is a gap the operator wants to see now, not after
    the next status change turns it into a 424.
    """
    return sorted(
        {
            product_id
            for (product_id,) in db.query(DemandLine.product_id)
            .join(Well, DemandLine.well_id == Well.id)
            .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .filter(PlanningNode.customer_id == customer.id)
            .distinct()
        }
    )


def products_without_on_hand_row(
    db: Session, business_unit_id: str, product_ids: list[str]
) -> list[str]:
    """Which of `product_ids` have NO `InventoryOnHand` row in `business_unit_id`.

    The structured half of the remap refusal. It answers exactly the question the
    operator has to act on -- which (BU, product) rows the inventory feed still owes --
    and it is computed from the same table `app.engines.inventory.on_hand_map` reads,
    so the list and the engine's refusal cannot disagree about what is absent.
    """
    if not product_ids:
        return []
    present = {
        row_id
        for (row_id,) in db.query(InventoryOnHand.product_id).filter(
            InventoryOnHand.business_unit_id == business_unit_id,
            InventoryOnHand.product_id.in_(product_ids),
        )
    }
    return [pid for pid in product_ids if pid not in present]


def _product_labels(db: Session, product_ids: list[str]) -> list[str]:
    """`"description (id=...)"` for each id, so a refusal is readable.

    Descriptions are joined here rather than left to the client for the reason stated
    throughout this API: a planner cannot act on a bare uuid.
    """
    if not product_ids:
        return []
    rows = {p.id: p for p in db.query(Product).filter(Product.id.in_(product_ids))}
    out = []
    for pid in product_ids:
        product = rows.get(pid)
        label = (product.description if product is not None else None) or pid
        out.append(f"{label} (id={pid})")
    return out


def _bu_name(db: Session, business_unit_id: str | None) -> str | None:
    if business_unit_id is None:
        return None
    unit = db.get(BusinessUnit, business_unit_id)
    return unit.name if unit is not None else None


def apply_customer_config_change(
    db: Session,
    customer: Customer,
    *,
    business_unit_id: Any = UNSET,
    allocation_policy: Any = UNSET,
) -> CustomerConfigChange:
    """Change `customer`'s Business Unit and/or allocation policy, then report.

    In order: probe whether coverage resolves TODAY (read-only), snapshot this
    customer's stored well rollups, apply both mutations, flush, recompute the customer
    ONCE inside a savepoint, and return the wells that moved.

    Flushes; does not commit. The caller owns the transaction, so a failure after this
    point rolls back the recompute along with the edit rather than leaving the mapping
    changed and verdicts half-rewritten.

    Raises `CustomerConfigRefused` -- after reverting the attempted mutation in this
    session -- for the two refusals the module docstring states: unmapping a mapped
    customer, and a remap whose destination cannot resolve this customer's demand.

    Callers must still pass a `business_unit_id` that EXISTS; validating it is the
    API layer's job, so the refusal can be a 400 naming the field rather than a
    foreign-key `IntegrityError`.
    """
    bu_before = customer.business_unit_id
    policy_before = customer.allocation_policy
    policy_before_value = getattr(policy_before, "value", str(policy_before))

    bu_after = bu_before if business_unit_id is UNSET else business_unit_id
    policy_after = policy_before if allocation_policy is UNSET else allocation_policy
    policy_after_value = getattr(policy_after, "value", str(policy_after))

    bu_changed = bu_after != bu_before
    policy_changed = policy_after != policy_before

    name_before = _bu_name(db, bu_before)
    name_after = name_before if not bu_changed else _bu_name(db, bu_after)

    if not bu_changed and not policy_changed:
        # A no-op is answered as a no-op rather than recomputed. Recomputing anyway
        # would be defensible repair work, but it would make an idempotent PATCH
        # indistinguishable from a real change in the report -- and a screen that
        # showed well movements for a save the operator knows changed nothing would
        # teach them to distrust the diff.
        return CustomerConfigChange(
            customer_id=customer.id,
            customer_name=customer.name,
            business_unit_changed=False,
            business_unit_id_before=bu_before,
            business_unit_id_after=bu_after,
            business_unit_name_before=name_before,
            business_unit_name_after=name_after,
            allocation_policy_changed=False,
            allocation_policy_before=policy_before_value,
            allocation_policy_after=policy_after_value,
            wells_examined=0,
            well_changes=(),
            recomputes_performed=0,
            coverage_resolvable_before=True,
            unchanged=True,
        )

    # Refusal (2): unmapping a customer that HAS a Business Unit. Checked before
    # anything is touched -- there is no destination to probe, and the outcome does not
    # depend on any data.
    if bu_changed and bu_after is None:
        raise CustomerConfigRefused(
            f"Customer {customer.name!r} is mapped to Business Unit "
            f"{name_before or bu_before!r} and cannot be un-mapped from this endpoint. "
            "A null business_unit_id means 'not yet mapped to a Business Unit', which "
            "is a state a customer is CREATED in, not one it can be returned to: "
            "on-hand inventory is held per (Business Unit, product), so an unmapped "
            "customer has NO INVENTORY POOL and every coverage verdict, MRP figure and "
            "dashboard tile for it fails with 409 inventory_scope_missing. Unlike a "
            "missing inventory row, nothing anybody could load would fix that -- there "
            "would be no pool to load it into. Remap the customer to a DIFFERENT "
            "Business Unit if the mapping is wrong; if the customer has genuinely left, "
            "that is a customer-master decision, which this platform does not own. "
            "Nothing was saved.",
            reason="unmap_refused",
        )

    # Read-only baseline. `compute_customer_coverage` is the half of
    # `recompute_customer` that writes nothing, so this costs no savepoint and can
    # leave no trace. It is what separates "your change broke this customer" from
    # "this customer was already unresolvable", which is the whole basis of the
    # asymmetry between refusals (1) and (3).
    from app.engines.coverage import compute_customer_coverage
    from app.engines.inventory import InventoryRowMissing, InventoryScopeMissing

    resolvable_before = True
    try:
        compute_customer_coverage(db, customer)
    except (InventoryRowMissing, InventoryScopeMissing):
        resolvable_before = False

    wells = customer_wells(db, customer)
    coverage_before = {well.id: (well.name, well.coverage_status) for well in wells}

    customer.business_unit_id = bu_after
    customer.allocation_policy = policy_after
    db.flush()

    from app.engines.coverage import recompute_customer

    unresolved_reason: str | None = None
    try:
        # SAVEPOINT. `recompute_customer` deletes and rewrites `CoverageResult` rows as
        # it goes, so a failure part-way through would otherwise leave this customer's
        # stored verdicts half-erased -- strictly worse than the ones it was replacing.
        # The savepoint makes the recompute all-or-nothing, which is what lets the
        # outcome be either a clean refusal or a reported caveat rather than damage.
        with db.begin_nested():
            # No explicit filters: the platform's CURRENT coverage scope is what the
            # official verdict must be computed under. See app.engines.coverage_scope.
            recompute_customer(db, customer)
    except (InventoryRowMissing, InventoryScopeMissing) as exc:
        if bu_changed:
            # Refusal (1). Put the row back exactly as it was, in this same session,
            # BEFORE raising -- the caller will roll the transaction back too, but a
            # half-moved customer must not be observable even from inside this request
            # (the API layer reads the customer again to build its response).
            customer.business_unit_id = bu_before
            customer.allocation_policy = policy_before
            db.flush()

            missing = (
                products_without_on_hand_row(
                    db, bu_after, demanded_product_ids(db, customer)
                )
                if bu_after is not None
                else []
            )
            labels = _product_labels(db, missing)
            if labels:
                gap = (
                    f"{len(labels)} product(s) this customer demands have NO "
                    f"InventoryOnHand row in {name_after or bu_after!r}: "
                    + ", ".join(labels)
                    + ". Load those (Business Unit, product) rows from the Oracle "
                    "inventory projection and retry the remap."
                )
            else:
                # The engine can also raise over a SUBSTITUTE target, which is not a
                # product this customer's demand lines name. Saying "no products are
                # missing" would then contradict the refusal, so the engine's own
                # message -- which names what it could not resolve -- is deferred to
                # instead of being second-guessed.
                gap = (
                    "Every product this customer's demand lines name does have an "
                    "InventoryOnHand row in the destination, so the unresolved "
                    "quantity is one the coverage pass reached indirectly (a "
                    "substitution target, for instance). The engine's own message "
                    "below names it."
                )
            raise CustomerConfigRefused(
                f"Remapping customer {customer.name!r} from Business Unit "
                f"{name_before or bu_before or 'none'!r} to "
                f"{name_after or bu_after!r} was REFUSED and rolled back: its coverage "
                "cannot be computed in the destination Business Unit. A Business Unit "
                "is an absolute inventory boundary, and nothing follows the customer "
                "across it -- InventoryOnHand is keyed on (Business Unit, product), and "
                "InventoryAssignment rows belong to the demand lines they name in the "
                "BU that holds the steel. Demand that resolved cleanly under the old "
                f"pool therefore has no quantity to be judged against under the new "
                f"one. {gap} The remap was rolled back rather than completed-and-"
                "reported, because a customer sitting under a Business Unit whose "
                "inventory cannot answer for its demand looks correctly configured on "
                "every screen while every coverage read for it fails. Nothing was "
                f"saved. Engine detail: {exc}",
                reason="remap_unresolvable",
                destination_business_unit_id=bu_after,
                missing_product_ids=tuple(missing),
                engine_message=str(exc),
            ) from exc
        # Refusal (3) is not a refusal: a policy-only change cannot have caused this,
        # because the set of products a pass must resolve is identical under all three
        # policies. The change stands and the failure is REPORTED -- the alternative
        # makes `allocation_policy` permanently uneditable for exactly the customers
        # whose configuration needs attention most. See the module docstring.
        unresolved_reason = str(exc)

    db.flush()

    well_changes: list[WellCoverageChange] = []
    for well in customer_wells(db, customer):
        name, was = coverage_before.get(well.id, (well.name, None))
        if was != well.coverage_status:
            well_changes.append(
                WellCoverageChange(
                    well_id=well.id,
                    well_name=name,
                    coverage_before=was,
                    coverage_after=well.coverage_status,
                )
            )
    well_changes.sort(key=lambda c: c.well_name)

    return CustomerConfigChange(
        customer_id=customer.id,
        customer_name=customer.name,
        business_unit_changed=bu_changed,
        business_unit_id_before=bu_before,
        business_unit_id_after=bu_after,
        business_unit_name_before=name_before,
        business_unit_name_after=name_after,
        allocation_policy_changed=policy_changed,
        allocation_policy_before=policy_before_value,
        allocation_policy_after=policy_after_value,
        wells_examined=len(wells),
        well_changes=tuple(well_changes),
        # ONE pass, whether one field moved or both. See the module docstring.
        recomputes_performed=1,
        coverage_resolvable_before=resolvable_before,
        unresolved_reason=unresolved_reason,
    )


def parse_allocation_policy(raw: str) -> AllocationPolicy:
    """`raw` as an `AllocationPolicy`, or raise `ValueError` naming the closed set.

    Deliberately not delegated to Pydantic, for the reason
    `app.api.admin._parse_dimension` states: the API layer turns this into a 400 whose
    message names the three permitted values and says what each one MEANS for the
    coverage verdict. Pydantic's generic 422 would tell an operator that a policy is
    invalid without telling them that choosing HARD can turn covered demand uncovered.
    """
    text = (raw or "").strip()
    by_value = {policy.value: policy for policy in AllocationPolicy}
    if text in by_value:
        return by_value[text]
    by_lower = {value.lower(): member for value, member in by_value.items()}
    if text.lower() in by_lower:
        return by_lower[text.lower()]
    raise ValueError(
        f"{raw!r} is not an allocation policy. It must be one of "
        f"{sorted(by_value)}. The set is CLOSED because each value names a DIFFERENT "
        "coverage rule implemented in app.engines.allocation -- 'soft' pools the "
        "Business Unit's unassigned on-hand stock earliest-ROS-first, 'hard' grants "
        "coverage only from inventory physically assigned to the demand line (with no "
        "pool top-up, however much steel is on the shelf), and 'hybrid' draws the "
        "assignment first and the pool for the remainder. A value outside the set has "
        "no rule behind it, so there would be nothing to compute. Nothing was saved."
    )
