"""Scenario PREVIEW -- read-only what-if. Phase 4.

The question this answers
------------------------
    "If these overrides were true, what would Coverage, MRP and Risk look like?"

Those three headings are the spec's, and they are the whole point. From the
discovery workshop:

    Scenarios are not strategic forecasts. Scenarios support active customer
    conversations. Users need to instantly see: Coverage Impact, MRP Impact,
    Risk Impact -- and then apply agreed changes to production data.

So this module produces a BEFORE and an AFTER of the same three things, and
nothing else. Applying is a separate module on purpose -- see below.

It writes NOTHING
-----------------
Same four layers as `app.engines.sharing`, deliberately copied rather than
reinvented:

  1. It never imports the persisting code path. `recompute_customer`,
     `apply_revision`, `CoverageResult` and `decide_approval` are all absent from
     this file's imports, so no edit here can reach a write without first adding
     an import -- which is a visible, reviewable act. This is why
     `apply_to_base_plan` lives in the SEPARATE module
     `app.engines.scenario_apply` instead of below: a module that legitimately
     writes cannot also be a module that structurally cannot.
  2. Every result is a frozen dataclass of scalars. No ORM instance and no
     `LineView` escapes, so a caller cannot mutate a mapped attribute it was
     handed, and FastAPI cannot lazily walk a relationship into a flush.
  3. The whole computation runs inside `Session.no_autoflush`, so no query issued
     mid-analysis can flush unrelated pending state as a side effect.
  4. `_assert_no_writes` compares the session's pending new/dirty/deleted sets
     before and after and raises if anything changed. A tripwire for a future
     edit, not a substitute for (1). Its own test injects a mutation and asserts
     it fires.

There is ONE implementation of the coverage rules
------------------------------------------------
This module does NOT re-derive coverage. It calls
`app.engines.coverage.compute_customer_coverage` twice -- once with
`NO_OVERRIDES` for the base, once with the scenario's `ScenarioOverrides` -- and
diffs the two answers. That function is the same code
`app.engines.coverage.recompute_customer` runs to produce the official verdict;
the official pass is just that call plus persistence.

The alternative -- a scenario-flavoured copy of the allocation ordering, the
substitution fall-through and the well rollup -- was rejected outright. A preview
has no downstream consumer to notice when it is wrong, so a forked copy would
drift silently and the drift would surface as a planner promising a customer
coverage the base plan then refuses to deliver. Here, "the preview and the real
thing agree" is not a property maintained by tests; it is a property of there
being one function. The overrides reach that function through
`app.engines.overrides`, a pure indirection layer.

MRP is reused the same way: `app.engines.mrp.recommendations_for_lines` is
called with the unresolved lines each pass produced, so the recommendation rows
in a preview are built by the same grouping and the same date arithmetic as the
ones on the MRP screen.

Every invariant of the official pass therefore still holds inside a preview,
because it is the official pass: line-level earliest-ROS ordering across the
customer's wells, the BU inventory boundary, HARD/HYBRID assignment semantics,
`is_recoverable` depending only on the physics, and the well rollup rule.

What "before" means
-------------------
The BEFORE column is the base plan RECOMPUTED, not the stored `CoverageResult`
rows. Those two agree whenever coverage is up to date, and when they do not, the
recomputed answer is the correct baseline: a diff against a stale stored verdict
would attribute someone else's drift to this scenario's overrides. It also makes
the promise checkable -- `apply_to_base_plan` recomputes too, so preview and
apply are comparing like with like.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.engines.coverage import (
    CustomerCoverage,
    compute_customer_coverage,
)
from app.engines.executive import quantity_by_unit
from app.engines.mrp import (
    UNRESOLVED_STATUSES,
    on_order_runout,
    recommendations_for_lines,
)
from app.engines.overrides import (
    NO_OVERRIDES,
    SUPPLY_KINDS,
    ScenarioOverrides,
)
from app.models import (
    CoverageStatus,
    PlanningNode,
    Product,
    ScenarioStatus,
    ScenarioTargetKind,
    Well,
)

#: Coverage outcomes that mean the demand is genuinely resolved.
_RESOLVED = (CoverageStatus.COVERED, CoverageStatus.COVERED_VIA_SUBSTITUTE)


# --------------------------------------------------------------------------
# Result shapes -- frozen dataclasses of scalars (layer 2)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LineCoverageChange:
    """One demand line, before -> after. `changed` is the headline."""

    demand_line_id: str
    well_id: str
    well_name: str
    product_id: str
    product_description: str | None
    #: Labels `quantity_before` and `quantity_after`. ONE value serves both, because
    #: a scenario can restate a quantity but never a product's unit -- there is no
    #: UoM override field in `app.engines.overrides.OVERRIDE_FIELDS` -- so the two
    #: figures are always in the same unit. None only when the product row could not
    #: be resolved, the same condition that makes `product_description` None.
    unit_of_measure: object
    status_before: str
    status_after: str
    reason_before: str | None
    reason_after: str | None
    quantity_before: float
    quantity_after: float
    ros_date_before: datetime
    ros_date_after: datetime
    #: True when the coverage STATUS differs. A line whose quantity an override
    #: changed without moving its status has changed=False and is still listed --
    #: "your override did not help" is an answer a planner needs.
    changed: bool
    #: True when this line's own values were overridden. A line can change status
    #: without being overridden at all: inventory is pooled and ROS-ordered, so
    #: pulling one line's ROS earlier can uncover a different line entirely. That
    #: second-order effect is the most valuable thing this screen shows.
    directly_overridden: bool


@dataclass(frozen=True)
class WellCoverageChange:
    """One well's rollup, before -> after."""

    well_id: str
    well_name: str
    status_before: str | None
    status_after: str | None
    changed: bool


@dataclass(frozen=True)
class MrpRowChange:
    """How one MRP recommendation row would change.

    Keyed on (product, recoverability) -- the same grouping key
    `app.engines.mrp` uses, so an "added" row here means exactly one more row on
    the MRP screen. `kind` is "added" | "removed" | "changed" | "unchanged".
    """

    kind: str
    product_id: str
    product_description: str | None
    #: One MRP row is one product, so one unit labels both quantities.
    unit_of_measure: object
    unrecoverable: bool
    quantity_before: float | None
    quantity_after: float | None
    ros_date_before: datetime | None
    ros_date_after: datetime | None
    recommended_order_date_before: date | None
    recommended_order_date_after: date | None
    reason_after: str | None


@dataclass(frozen=True)
class SupplyRunoutChange:
    """What moving ONE product's incoming-supply arrival does to its runout curve.

    The consequence of a `PO_ARRIVAL.arrival_date` override, and the reason that
    override is no longer in `app.engines.overrides.UNMODELLED_KINDS`.

    BOTH SERIES INCLUDE REAL ON-ORDER. `runout_month_before` is the runout month of
    an on-order-AWARE projection at the arrival dates Oracle actually promised;
    `runout_month_after` is the same projection with the override's shift applied.
    That symmetry is what makes the delta attributable to the drag and nothing
    else -- and it is why neither figure equals `ByItemAnalysis.runout_month`,
    which is on-hand-only by design (see `app.engines.mrp.OnOrderRunout`). A UI
    showing these must not present either as the By Item runout month.

    NOT A COVERAGE FIGURE, AND COVERAGE DID NOT MOVE. Coverage verdicts are decided
    from BU-scoped on-hand stock alone and this override is not visible to that
    pass at all (`app.engines.overrides.OverrideResolver.arrival_date` documents
    the deliberate absence of any coverage-side caller). If a planner needs the
    verdict to change, the question they are really asking is whether steel landing
    before ROS may cover a line -- an unmade coverage-rule decision, not this.
    """

    product_id: str
    product_description: str | None
    unit_of_measure: object
    #: Earliest DATED arrival before and after the shift. `arrival_before` is None
    #: when every on-order row for this product is undated, in which case there was
    #: no schedule position to shift and `shift_days` is 0.
    arrival_before: date | None
    arrival_after: date | None
    shift_days: int
    runout_month_before: str | None
    runout_month_after: str | None
    #: True when the shift actually moved the month the balance goes negative. False
    #: is a real and common answer -- pulling a PO in by a week inside the same
    #: month changes nothing, and a product with enough stock never runs out either
    #: way. The UI must say "no change" rather than implying the drag failed.
    runout_month_changed: bool
    #: Quantity folded in, and the part that could not be: an undated PO has no
    #: month to be projected at and is excluded from both series alike.
    on_order_dated_quantity: float
    on_order_undated_quantity: float
    #: The full curves, so the timeline can draw the before/after shift rather than
    #: only naming two months.
    runout_before: tuple = ()
    runout_after: tuple = ()

    # -- the HYPOTHETICAL half: purchase orders that do not exist -------------
    #
    # Set by `PO_ARRIVAL.new_order` overrides. Empty on a pure arrival-date shift,
    # which is why this dataclass grew rather than a second one being added: both
    # override fields change the same product's runout curve through the same
    # `on_order_runout` call, and reporting them in two tables would ask a planner to
    # add two projections of one product together in their head.
    #
    # THE `_after` CURVE INCLUDES THESE AND THE `_before` CURVE DOES NOT. That is the
    # asymmetry that makes the diff readable: `runout_before` is the plan as it
    # stands, `runout_after` is the plan with the planner's hypothesis in it.
    #: ((quantity, expected_arrival), ...) as asserted, earliest first.
    hypothetical_orders: tuple = ()
    #: Total invented quantity. NOT part of `on_order_dated_quantity`, which stays a
    #: sum of real Oracle-projected rows a planner can go and verify.
    hypothetical_quantity: float = 0.0

    # -- what MRP says about the gap this hypothesis is aimed at ---------------
    #
    # READ FROM THE MRP IMPACT, NOT RECOMPUTED, and deliberately NOT a claim that the
    # recommendation row moved -- it does not and must not. MRP recommendations are
    # derived from coverage verdicts, coverage is decided from on-hand stock alone,
    # and incoming supply (real or hypothetical) is not visible to that pass. So a
    # hypothetical order cannot delete an MRP row, and this platform will not pretend
    # it did.
    #
    # What CAN be answered honestly, and is: MRP has already computed how much of
    # this product it recommends ordering. Comparing the planner's hypothetical
    # quantity against that number says whether the order they just drew is SIZED to
    # cover the recommendation -- which is the actual decision ("is 5000 metres
    # enough?"). It is a comparison of two figures the preview already holds, not a
    # third projection.
    #: Total quantity MRP recommends ordering for this product in the AFTER pass,
    #: summed across its recoverable and unrecoverable rows. None when MRP recommends
    #: nothing for it -- there is no gap to close.
    mrp_recommended_quantity: float | None = None
    #: True when `hypothetical_quantity` >= `mrp_recommended_quantity`. Means "the
    #: order you drew is big enough to cover what MRP asks for", NOT "the MRP row
    #: disappeared".
    hypothetical_covers_recommendation: bool = False


@dataclass(frozen=True)
class RiskImpact:
    """Change in UNRECOVERABLE demand -- demand that a mill order cannot save.

    The sharpest number on the screen. UNRECOVERABLE means "ROS cannot be met
    even if we order today", so a line entering this set is a commitment the
    supply chain can no longer keep and a line leaving it is a genuine rescue.
    Both directions matter: an ROS push-out is the classic way to rescue one, and
    an ROS pull-in is the classic way to create one by accident.
    """

    unrecoverable_lines_before: int
    unrecoverable_lines_after: int
    #: CROSS-PRODUCT TOTALS. These sum unrecoverable demand over whatever mix of
    #: products the scenario's pool contains, so they follow the same rule as the
    #: Executive Dashboard (`app.engines.executive.quantity_by_unit`): the scalar is
    #: safe only when `unit_of_measure` is non-null, and `quantities_by_unit_*` is
    #: the answer that is always correct. Entries are never added together.
    unrecoverable_quantity_before: float
    unrecoverable_quantity_after: float
    unit_of_measure: object = None
    quantities_by_unit_before: tuple = ()
    quantities_by_unit_after: tuple = ()
    #: Lines that the scenario would push into UNRECOVERABLE.
    became_unrecoverable: tuple[str, ...] = ()
    #: Lines the scenario would rescue out of UNRECOVERABLE.
    no_longer_unrecoverable: tuple[str, ...] = ()

    @property
    def quantity_delta(self) -> float:
        return self.unrecoverable_quantity_after - self.unrecoverable_quantity_before


@dataclass(frozen=True)
class ScenarioImpact:
    """Read-only what-if result. Nothing here has been persisted.

    `is_what_if` is always True and is serialised deliberately: the frontend must
    be able to label this panel unmistakably without inferring anything, exactly
    as the cross-customer sharing panel is labelled. A number from this object
    must never be presented as the coverage verdict.
    """

    scenario_id: str
    scenario_name: str
    scenario_status: str
    customer_id: str
    customer_name: str
    business_unit_id: str | None
    business_unit_name: str | None

    override_count: int = 0
    #: Overrides that were RECORDED but could not be modelled, counted and explained
    #: in `notes` rather than silently dropped.
    #:
    #: ALWAYS 0 TODAY: `app.engines.overrides.UNMODELLED_KINDS` is now empty, since
    #: PO_ARRIVAL -- its only ever member -- became modelled (its effect is in
    #: `supply_runout_changes`). The field and the note machinery are KEPT rather
    #: than deleted: the vocabulary is the place new override kinds arrive, and the
    #: next one that outruns the engines must be reported loudly rather than
    #: quietly ignored, which is the failure mode this field was added to prevent.
    unmodelled_override_count: int = 0

    line_changes: tuple[LineCoverageChange, ...] = ()
    well_changes: tuple[WellCoverageChange, ...] = ()
    mrp_changes: tuple[MrpRowChange, ...] = ()
    #: Runout effect of PO_ARRIVAL overrides. One entry per overridden product.
    #: Empty when the scenario restates no arrival date -- which is most scenarios.
    supply_runout_changes: tuple[SupplyRunoutChange, ...] = ()
    risk: RiskImpact | None = None

    covered_lines_before: int = 0
    covered_lines_after: int = 0
    covered_wells_before: int = 0
    covered_wells_after: int = 0
    changed_line_count: int = 0
    changed_well_count: int = 0

    #: Whether `apply_to_base_plan` would accept this scenario, and if not why.
    #: Computed here so the editor can disable the button and SAY why, instead of
    #: letting a planner discover it by pressing it.
    applicable: bool = True
    apply_blockers: tuple[str, ...] = ()

    is_what_if: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# Lifecycle guards (pure -- no writes, hence they live here)
# --------------------------------------------------------------------------


class ScenarioImmutable(RuntimeError):
    """Raised on any attempt to change or re-apply an APPLIED scenario."""


def assert_mutable(scenario) -> None:
    """Refuse to modify an applied scenario.

    An applied scenario is the record of what was agreed with the customer and
    what was consequently written into production data -- see
    app.models.scenario.Scenario for the full reasoning. Editing it would make
    it describe overrides that were never applied while still claiming an
    `applied_at`, and re-applying it would append a second DemandRevision and a
    second ImpactRecord recording a change of nothing.

    Enforced in code, in one place, called by every mutating path.
    """
    if scenario.status == ScenarioStatus.APPLIED:
        applied = (
            scenario.applied_at.isoformat() if scenario.applied_at else "an earlier run"
        )
        raise ScenarioImmutable(
            f"Scenario {scenario.name!r} was applied to the base plan at {applied} "
            "and is now an immutable record of what was agreed. It cannot be "
            "edited or re-applied. Create a new scenario to revisit the "
            "agreement -- that keeps both the original and the revision visible."
        )


def assert_target_in_scope(db: Session, scenario, override) -> None:
    """Refuse an override that points outside the scenario's own customer.

    Covers both demand targets: a DEMAND_LINE override's line and a WELL
    override's well.

    A scenario is scoped to ONE customer because coverage is computed at that
    scope. An override naming another customer's demand line would be silently
    ignored by the preview (the pool query never sees that line) while sitting in
    the scenario looking effective -- and `apply_to_base_plan` would then happily
    write a revision to a line the preview never modelled, which is the exact
    shape of a preview that lies.

    Read-only, and called before an override is persisted. The shape and
    Business-Unit rules are `app.engines.overrides.validate`'s job; this is the
    one rule that needs a database lookup, which is why it is here and not there.
    """
    from app.models import DemandLine  # local: keeps the model import surface small

    well_id = override.target_well_id
    if well_id is not None:
        # A WELL override (demand status). Same rule, same reason: a status change
        # on another customer's well would be invisible to this scenario's preview
        # -- the pool query never sees that well -- while `apply_to_base_plan`
        # would happily cascade revisions to every line of it.
        well_owner = (
            db.query(PlanningNode.customer_id)
            .join(Well, Well.planning_node_id == PlanningNode.id)
            .filter(Well.id == well_id)
            .scalar()
        )
        if well_owner is None:
            raise LookupError(f"Well {well_id!r} does not exist.")
        if well_owner != scenario.customer_id:
            raise ValueError(
                f"Well {well_id!r} belongs to a different customer than scenario "
                f"{scenario.name!r}. A scenario may only override its own "
                "customer's plan -- coverage is computed per customer, so an "
                "override on someone else's well could be neither previewed nor "
                "honestly applied."
            )

    line_id = override.target_demand_line_id
    if line_id is None:
        return

    owner = (
        db.query(PlanningNode.customer_id)
        .join(Well, Well.planning_node_id == PlanningNode.id)
        .join(DemandLine, DemandLine.well_id == Well.id)
        .filter(DemandLine.id == line_id)
        .scalar()
    )
    if owner is None:
        raise LookupError(f"Demand line {line_id!r} does not exist.")
    if owner != scenario.customer_id:
        raise ValueError(
            f"Demand line {line_id!r} belongs to a different customer than "
            f"scenario {scenario.name!r}. A scenario may only override its own "
            "customer's plan -- coverage is computed per customer, so an override "
            "on someone else's line could be neither previewed nor honestly "
            "applied."
        )


def apply_blockers(scenario) -> tuple[str, ...]:
    """Why `apply_to_base_plan` would refuse this scenario. Empty means it would
    proceed.

    Pure, so the preview can report it and the editor can render it before the
    planner commits to anything. The authority on refusal is still
    `app.engines.scenario_apply.apply_to_base_plan`, which calls this.
    """
    blockers: list[str] = []

    if scenario.status == ScenarioStatus.APPLIED:
        blockers.append(
            "This scenario has already been applied. An applied scenario is an "
            "immutable record and cannot be applied again."
        )

    supply = [o for o in scenario.overrides if o.target_kind in SUPPLY_KINDS]
    if supply:
        kinds = sorted({o.target_kind.value for o in supply})
        blockers.append(
            f"{len(supply)} supply override(s) ({', '.join(kinds)}) cannot be "
            "written to the base plan: InventoryOnHand, InventoryAssignment and "
            "purchase orders are all owned by Oracle, and this platform holds "
            "read-only projections of them. See the notes on the preview for what "
            "to do instead."
        )

    # A SECOND, DISTINCT REFUSAL FOR HYPOTHETICAL NEW ORDERS.
    #
    # Kept separate from the blocker above rather than folded into it, because the
    # reason is genuinely a different one and a planner is entitled to the right
    # reason. Above: "we hold a read-only COPY of a row Oracle owns, so writing to it
    # would be reverted by the next sync." Here there is NO ROW AT ALL, in either
    # system -- the override invents a purchase order with no Oracle counterpart, so
    # "applying" it could not mean updating a projection. It could only mean PLACING
    # AN ORDER.
    #
    # And placing an order is not this platform's act to perform. Per the spec, MRP
    # here is a RECOMMENDATION process and mill ordering is the last resort -- a
    # decision made by people, against lead times, mill slots and commercial terms
    # this platform does not model. A button that silently turned a planner's
    # what-if sketch into a procurement commitment would be the most consequential
    # unreviewed write in the system.
    #
    # So the refusal is not a limitation being apologised for; it is the correct
    # relationship between a what-if and a purchase. The scenario remains exactly
    # what it should be: the argument a planner takes INTO the ordering conversation.
    new_orders = [
        o
        for o in scenario.overrides
        if o.target_kind == ScenarioTargetKind.PO_ARRIVAL
        and o.field_name == "new_order"
    ]
    if new_orders:
        blockers.append(
            f"{len(new_orders)} hypothetical new-order override(s) cannot be applied, "
            "and this is a stronger refusal than the one above rather than the same "
            "one: there is no purchase order anywhere to update. The override asserts "
            "an order that does not exist in Oracle or here, so applying it would not "
            "be writing to a read-only projection -- it would be PLACING AN ORDER. "
            "Mill ordering is a human, last-resort decision made against lead times, "
            "mill capacity and commercial terms this platform does not model, and it "
            "is not something a saved what-if may trigger. The projection above is the "
            "answer to take into that conversation; raise the order itself through the "
            "ordering process, then re-preview against the synced purchase order."
        )

    return tuple(blockers)


# --------------------------------------------------------------------------
# Write tripwire (layer 4)
# --------------------------------------------------------------------------


def _pending_snapshot(db: Session) -> tuple[int, int, int]:
    return (len(db.new), len(db.dirty), len(db.deleted))


def _assert_no_writes(db: Session, before: tuple[int, int, int]) -> None:
    """Fail loudly if the preview left anything pending in the session.

    Layer 4 of the no-write guarantee (see module docstring). If a future edit
    introduces a write here, this raises during the preview rather than letting a
    what-if quietly become the official coverage answer on the next commit.
    """
    after = _pending_snapshot(db)
    if after != before:
        raise AssertionError(
            "scenario preview must not modify the session: pending "
            f"(new, dirty, deleted) went from {before} to {after}. The preview is "
            "a read-only projection -- the official coverage answer is written "
            "only by app.engines.coverage.recompute_customer, and a scenario "
            "reaches production data only through "
            "app.engines.scenario_apply.apply_to_base_plan."
        )


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def preview(
    db: Session,
    scenario,
    # `None` for either means the platform's CURRENT coverage scope. Passed
    # STRAIGHT THROUGH to `compute_customer_coverage`, which is the one place the
    # scope is resolved -- and that matters more here than anywhere else, because a
    # preview whose two passes resolved the scope separately could diff a base plan
    # against a scenario evaluated under a different include-set and attribute the
    # difference to the scenario.
    status_filter: set | None = None,
    profile_filter: set | None = None,
    today: date | None = None,
) -> ScenarioImpact:
    """Coverage, MRP and Risk impact of `scenario`. Writes nothing.

    Computes the customer's coverage twice through the ONE coverage
    implementation -- once as the data actually is, once with the scenario's
    overrides resolved in -- and diffs the two. See the module docstring for the
    four mechanisms that make the no-write property structural rather than
    hopeful.
    """
    before_pending = _pending_snapshot(db)

    with db.no_autoflush:
        customer = scenario.customer
        bu = customer.business_unit

        resolver = ScenarioOverrides(scenario.overrides, customer)

        base = compute_customer_coverage(
            db, customer, status_filter, profile_filter, NO_OVERRIDES
        )
        after = compute_customer_coverage(
            db, customer, status_filter, profile_filter, resolver
        )

        well_names = _well_names(db, customer.id)
        line_changes = _line_changes(base, after, well_names, resolver)
        well_changes = _well_changes(base, after, well_names)
        mrp_changes = _mrp_changes(db, base, after, today=today)
        # `mrp_changes` is passed IN rather than recomputed inside: the MRP-impact
        # computation must happen exactly once per preview, and the runout comparison
        # only READS its result to size a hypothetical order against it.
        supply_runout_changes = _supply_runout_changes(
            db, resolver, mrp_changes, today=today
        )
        risk = _risk(base, after)

        notes = _notes(scenario, resolver, bu)
        blockers = apply_blockers(scenario)

        impact = ScenarioImpact(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            scenario_status=scenario.status.value,
            customer_id=customer.id,
            customer_name=customer.name,
            business_unit_id=customer.business_unit_id,
            business_unit_name=bu.name if bu is not None else None,
            override_count=len(scenario.overrides),
            unmodelled_override_count=len(resolver.unmodelled),
            line_changes=line_changes,
            well_changes=well_changes,
            mrp_changes=mrp_changes,
            supply_runout_changes=supply_runout_changes,
            risk=risk,
            covered_lines_before=_covered_line_count(base),
            covered_lines_after=_covered_line_count(after),
            covered_wells_before=_covered_well_count(base),
            covered_wells_after=_covered_well_count(after),
            changed_line_count=sum(1 for c in line_changes if c.changed),
            changed_well_count=sum(1 for c in well_changes if c.changed),
            applicable=not blockers,
            apply_blockers=blockers,
            notes=notes,
        )

    _assert_no_writes(db, before_pending)
    return impact


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------


def _well_names(db: Session, customer_id: str) -> dict[str, str]:
    return {
        well.id: well.name
        for well in db.query(Well)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id == customer_id)
    }


def _line_changes(
    base: CustomerCoverage,
    after: CustomerCoverage,
    well_names: dict[str, str],
    resolver: ScenarioOverrides,
) -> tuple[LineCoverageChange, ...]:
    """Every line the two passes have an opinion about, before -> after.

    The union of both key sets, not just the base's. A WELL status override that
    flips a well from Planned to Confirmed brings ALL of that well's lines INTO
    scope that the base pass never evaluated, and vice versa -- both are real
    coverage impacts and neither may be dropped just because one side lacks a row.
    (Since the move of demand status onto `Well`, this arrives a whole well at a
    time rather than a line at a time, which is the point of the change.)
    """
    products = {
        view.id: view.product for view in (*base.included_views, *after.included_views)
    }
    directly = resolver.overridden_line_ids()
    # A WELL status override names every line of that well, so each of them is
    # directly overridden even though none is named by id. Without this the editor
    # would show a whole well's lines flipping in and out of scope with nothing
    # marked as the cause.
    directly_wells = resolver.overridden_well_ids()

    out: list[LineCoverageChange] = []
    for line_id in sorted(set(base.by_line) | set(after.by_line)):
        b = base.by_line.get(line_id)
        a = after.by_line.get(line_id)
        product = products.get(line_id)

        # "NotEvaluated" is a real, distinct outcome, not a missing value: it is
        # what the coverage engine means by the ABSENCE of a CoverageResult row
        # (see app.models.coverage.CoverageResult). Rendering it as a status
        # rather than a blank is what makes "this override took the line out of
        # scope entirely" legible.
        status_before = b.status.value if b else "NotEvaluated"
        status_after = a.status.value if a else "NotEvaluated"
        ref = a or b

        out.append(
            LineCoverageChange(
                demand_line_id=line_id,
                well_id=ref.well_id,
                well_name=well_names.get(ref.well_id, ref.well_id),
                product_id=ref.product_id,
                product_description=product.description if product else None,
                unit_of_measure=product.unit_of_measure if product else None,
                status_before=status_before,
                status_after=status_after,
                reason_before=b.reason if b else None,
                reason_after=a.reason if a else None,
                quantity_before=b.quantity if b else (a.quantity if a else 0.0),
                quantity_after=a.quantity if a else (b.quantity if b else 0.0),
                ros_date_before=b.ros_date if b else a.ros_date,
                ros_date_after=a.ros_date if a else b.ros_date,
                changed=status_before != status_after,
                directly_overridden=(
                    line_id in directly or ref.well_id in directly_wells
                ),
            )
        )
    return tuple(out)


def _well_changes(
    base: CustomerCoverage,
    after: CustomerCoverage,
    well_names: dict[str, str],
) -> tuple[WellCoverageChange, ...]:
    out: list[WellCoverageChange] = []
    for well_id in sorted(set(base.well_status) | set(after.well_status)):
        before = base.well_status.get(well_id)
        now = after.well_status.get(well_id)
        out.append(
            WellCoverageChange(
                well_id=well_id,
                well_name=well_names.get(well_id, well_id),
                status_before=before,
                status_after=now,
                changed=before != now,
            )
        )
    return tuple(out)


def _unresolved_views(computed: CustomerCoverage) -> list:
    """Included lines this pass left genuinely unresolved -- MRP's input.

    Exactly `app.engines.mrp`'s definition (UNCOVERED or UNRECOVERABLE), read
    from the pass's own verdict rather than from the stored CoverageResult. MRP's
    "no row at all means unevaluated" case cannot arise here: every included line
    has a computed verdict by construction.
    """
    return [
        view
        for view in computed.included_views
        if computed.by_line[view.id].status in UNRESOLVED_STATUSES
    ]


def _mrp_changes(
    db: Session,
    base: CustomerCoverage,
    after: CustomerCoverage,
    today: date | None = None,
) -> tuple[MrpRowChange, ...]:
    """How the MRP recommendation rows would change.

    Built by calling `app.engines.mrp.recommendations_for_lines` -- the same
    grouping, the same lead-time arithmetic and the same reason wording the MRP
    screen shows -- once per pass, then diffing on MRP's own (product,
    recoverability) key. Rows that are byte-identical are reported as
    "unchanged" and kept, so the panel can show the whole order book rather than
    a delta floating in space.
    """
    before_rows = {
        (r.product_id, r.unrecoverable): r
        for r in recommendations_for_lines(db, _unresolved_views(base), today=today)
    }
    after_rows = {
        (r.product_id, r.unrecoverable): r
        for r in recommendations_for_lines(db, _unresolved_views(after), today=today)
    }

    out: list[MrpRowChange] = []
    for key in sorted(set(before_rows) | set(after_rows)):
        b = before_rows.get(key)
        a = after_rows.get(key)
        if b is None:
            kind = "added"
        elif a is None:
            kind = "removed"
        elif (
            b.quantity != a.quantity
            or b.ros_date != a.ros_date
            or b.recommended_order_date != a.recommended_order_date
        ):
            kind = "changed"
        else:
            kind = "unchanged"

        ref = a or b
        out.append(
            MrpRowChange(
                kind=kind,
                product_id=ref.product_id,
                product_description=ref.product_description,
                unit_of_measure=ref.unit_of_measure,
                unrecoverable=ref.unrecoverable,
                quantity_before=b.quantity if b else None,
                quantity_after=a.quantity if a else None,
                ros_date_before=b.ros_date if b else None,
                ros_date_after=a.ros_date if a else None,
                recommended_order_date_before=b.recommended_order_date if b else None,
                recommended_order_date_after=a.recommended_order_date if a else None,
                reason_after=a.reason if a else None,
            )
        )
    return tuple(out)


def _mrp_recommended_by_product(
    mrp_changes: tuple[MrpRowChange, ...],
) -> dict[str, float]:
    """{product_id: total quantity MRP recommends ordering in the AFTER pass}.

    Summed across the recoverable and unrecoverable rows MRP splits a product into,
    because "how much of this do we need to order" is one question and the split is
    about whether the order can still land in time, not about how much.

    A pure read of `_mrp_changes`'s own output -- no second call to
    `recommendations_for_lines`. Rows MRP dropped in the after pass (`quantity_after
    is None`) contribute nothing, which is correct: there is no recommendation left
    to size against.
    """
    totals: dict[str, float] = {}
    for row in mrp_changes:
        if row.quantity_after is None:
            continue
        totals[row.product_id] = totals.get(row.product_id, 0.0) + row.quantity_after
    return totals


def _supply_runout_changes(
    db: Session,
    resolver: ScenarioOverrides,
    mrp_changes: tuple[MrpRowChange, ...] = (),
    today: date | None = None,
) -> tuple[SupplyRunoutChange, ...]:
    """The runout effect of every PO_ARRIVAL override on this scenario.

    Covers BOTH of the kind's fields, in ONE comparison per product:

      * `arrival_date` -- the real schedule moves. Passed as `arrival_override`.
      * `new_order`    -- a purchase order that does not exist is added beside the
                          real ones. Passed as `hypothetical_orders`.

    Calls `app.engines.mrp.on_order_runout` TWICE per affected product -- once with
    neither, once with whichever this scenario asserts -- and diffs the runout month.
    Both calls go through the same function, which itself goes through the same
    `_runout_series` month-walk every other runout curve in the platform uses, so
    there is no second projection implementation to drift. In particular the
    hypothetical quantity is injected through `_runout_series`'s existing
    `incoming_by_month` hook rather than through a forked month-walk.

    ONE ENTRY PER PRODUCT even when both fields are set on it, because there is one
    runout curve for a product and reporting it twice would invite a planner to add
    two projections of the same product together.

    Empty when the scenario has neither field, which is the common case and costs two
    `frozenset` checks rather than any query.
    """
    product_ids = sorted(
        resolver.po_arrival_product_ids() | resolver.new_order_product_ids()
    )
    if not product_ids:
        return ()

    recommended = _mrp_recommended_by_product(mrp_changes)

    out: list[SupplyRunoutChange] = []
    for product_id in product_ids:
        product = db.get(Product, product_id)
        if product is None:
            # An override naming a deleted product. Skipped rather than raised: the
            # rest of the preview is still a valid answer, and `validate` already
            # guaranteed the row was well-formed when it was written.
            continue

        # BEFORE: no override of either kind. The plan as it stands.
        base = on_order_runout(db, product_id, arrival_override=None, today=today)
        restated = resolver.arrival_date(product_id, base.earliest_arrival)
        hypothetical = resolver.hypothetical_orders(product_id)
        # AFTER: the real schedule shifted (if asked) AND the hypothetical orders
        # added beside it (if asserted). The two compose -- see `on_order_runout`.
        after = on_order_runout(
            db,
            product_id,
            arrival_override=restated,
            today=today,
            hypothetical_orders=hypothetical,
        )

        # Computed from REAL dated arrivals on both sides, which `on_order_runout`
        # guarantees `earliest_arrival` still is. So a scenario that only asserts a
        # hypothetical order reports `shift_days == 0` and identical before/after
        # arrival dates -- correct, because it moved no promised delivery.
        shift = 0
        if base.earliest_arrival is not None and after.earliest_arrival is not None:
            shift = (after.earliest_arrival - base.earliest_arrival).days

        hypothetical_quantity = after.hypothetical_total
        product_recommended = recommended.get(product_id)

        out.append(
            SupplyRunoutChange(
                product_id=product_id,
                product_description=product.description,
                unit_of_measure=product.unit_of_measure,
                arrival_before=base.earliest_arrival,
                arrival_after=after.earliest_arrival,
                shift_days=shift,
                runout_month_before=base.runout_month,
                runout_month_after=after.runout_month,
                runout_month_changed=base.runout_month != after.runout_month,
                on_order_dated_quantity=sum(base.incoming_by_month.values()),
                on_order_undated_quantity=base.undated_quantity,
                runout_before=tuple(base.runout),
                runout_after=tuple(after.runout),
                hypothetical_orders=hypothetical,
                hypothetical_quantity=hypothetical_quantity,
                mrp_recommended_quantity=product_recommended,
                hypothetical_covers_recommendation=(
                    product_recommended is not None
                    and hypothetical_quantity > 0
                    and hypothetical_quantity >= product_recommended
                ),
            )
        )
    return tuple(out)


def _risk(base: CustomerCoverage, after: CustomerCoverage) -> RiskImpact:
    def unrecoverable(computed):
        return {
            line_id: verdict
            for line_id, verdict in computed.by_line.items()
            if verdict.status == CoverageStatus.UNRECOVERABLE
        }

    b = unrecoverable(base)
    a = unrecoverable(after)

    # Units of the LINES' own products, taken from the views the two passes actually
    # saw. Unrecoverable demand is per line and can span products, so the totals
    # above are cross-product aggregates and get the same treatment the Executive
    # Dashboard gives its own: a per-unit breakdown that is always right, and a
    # scalar that is only labelled when there is one unit to label it with.
    units = {
        view.id: view.product.unit_of_measure
        for view in (*base.included_views, *after.included_views)
    }
    by_unit_before, unit_before = quantity_by_unit(
        (units[line_id], verdict.quantity)
        for line_id, verdict in b.items()
        if line_id in units
    )
    by_unit_after, unit_after = quantity_by_unit(
        (units[line_id], verdict.quantity)
        for line_id, verdict in a.items()
        if line_id in units
    )
    # A single scalar unit is only claimed when BOTH sides agree on it -- otherwise
    # `quantity_delta` would be a difference between totals in different units, which
    # is exactly the arithmetic the rule forbids. Two empty sides give None, which is
    # correct: nothing was summed, so there is no unit to claim.
    single = unit_before if unit_before == unit_after else None

    return RiskImpact(
        unrecoverable_lines_before=len(b),
        unrecoverable_lines_after=len(a),
        unrecoverable_quantity_before=sum(v.quantity for v in b.values()),
        unrecoverable_quantity_after=sum(v.quantity for v in a.values()),
        unit_of_measure=single,
        quantities_by_unit_before=by_unit_before,
        quantities_by_unit_after=by_unit_after,
        became_unrecoverable=tuple(sorted(set(a) - set(b))),
        no_longer_unrecoverable=tuple(sorted(set(b) - set(a))),
    )


def _covered_line_count(computed: CustomerCoverage) -> int:
    return sum(1 for v in computed.by_line.values() if v.status in _RESOLVED)


def _covered_well_count(computed: CustomerCoverage) -> int:
    return sum(
        1
        for status in computed.well_status.values()
        if status == CoverageStatus.COVERED.value
    )


def _notes(scenario, resolver: ScenarioOverrides, bu) -> tuple[str, ...]:
    notes = [
        "WHAT-IF ONLY. Nothing in this result has been persisted. The official "
        "coverage verdict is unchanged and is written only by the coverage engine "
        "-- it changes when, and only when, this scenario is applied to the base "
        "plan.",
    ]

    if not scenario.overrides:
        notes.append(
            "This scenario has no overrides yet, so the 'after' column is "
            "identical to the base plan by construction."
        )
    elif not resolver.active:
        notes.append(
            "None of this scenario's overrides can affect the numbers, so the "
            "'after' column equals the base plan. See the note(s) below."
        )

    for override in resolver.unmodelled:
        notes.append(
            f"An override of {override.target_kind.value}.{override.field_name} is "
            "recorded on this scenario but was NOT taken into account, because "
            "nothing in this platform reads the thing it describes. The question is "
            "captured for the customer conversation, but no figure above is "
            "affected by it. Do not read an unchanged preview as 'that change "
            "would not help'."
        )
        break

    if resolver.po_arrival_product_ids():
        notes.append(
            "A PO ARRIVAL override is recorded and IS modelled -- but only where it "
            "honestly reaches. It shifts this product's incoming-supply arrival "
            "dates and moves the RUNOUT PROJECTION accordingly; see "
            "`supply_runout_changes`, which reports the runout month before and "
            "after. Both of those figures come from an on-order-AWARE projection, so "
            "neither equals the By Item runout month, which is on-hand-only by "
            "design. "
            "IT DOES NOT MOVE ANY COVERAGE VERDICT, and no Covered/Uncovered figure "
            "above reflects it: coverage is decided from on-hand stock alone, and "
            "incoming supply is read beside that verdict, never inside it. Whether "
            "steel landing before ROS may COVER a line is a coverage-rule question "
            "this platform has not answered, and a scenario override is not the "
            "place to answer it. Nor does it move the MRP recommendation rows, "
            "which are derived from those same coverage verdicts. "
            "The arrival date restated is the EARLIEST dated arrival; any further "
            "purchase orders for this product shift by the same number of days, so "
            "the delivery schedule keeps its shape. Purchase orders with no promised "
            "date are not shifted onto one and are excluded from both projections."
        )

    if resolver.new_order_product_ids():
        notes.append(
            "A HYPOTHETICAL NEW ORDER is recorded on this scenario -- a purchase order "
            "that DOES NOT EXIST, which you have asked the platform to imagine. It is "
            "added to the RUNOUT PROJECTION at its stated month, additively beside "
            "whatever real purchase orders that product already has, and the effect is "
            "reported in `supply_runout_changes` (`hypothetical_orders` names the "
            "quantity and date; `hypothetical_quantity` is the invented total, kept "
            "OUT of the real on-order figures so the two never blur). "
            "NO InventoryOnOrder ROW WAS CREATED and none ever will be -- this "
            "platform holds Oracle's purchase orders as a read-only projection and "
            "does not fabricate rows in it. "
            "IT MOVES NO COVERAGE VERDICT. Coverage is decided from on-hand stock "
            "alone, so however large the hypothetical order is, no Covered/Uncovered "
            "figure above responds to it, and neither do the MRP recommendation rows "
            "derived from those verdicts. What IS reported is whether the quantity you "
            "chose is big enough to cover what MRP already recommends ordering for the "
            "product (`mrp_recommended_quantity` / "
            "`hypothetical_covers_recommendation`) -- that is a comparison of two "
            "figures, not a claim that a recommendation row disappeared. "
            "AND IT CAN NEVER BE APPLIED: applying it would mean placing an order, "
            "and mill ordering is a human last-resort decision this platform "
            "recommends but does not execute."
        )

    supply = [o for o in scenario.overrides if o.target_kind in SUPPLY_KINDS]
    if supply:
        notes.append(
            "This scenario contains supply overrides. They ARE modelled in the "
            "figures above (that is the point of asking), but they CANNOT be "
            "applied to the base plan: on-hand inventory, inventory assignments "
            "and purchase orders are owned by Oracle and this platform holds only "
            "read-only projections of them. Writing a scenario value into a "
            "projection would be silently reverted by the next Oracle sync while "
            "the planner believed the change had been recorded. Raise the agreed "
            "supply change in Oracle, then re-preview against the synced data."
        )

    if bu is not None:
        notes.append(
            f"Every quantity above was resolved inside Business Unit "
            f"'{bu.name}'. A scenario cannot reach across the Business Unit "
            "boundary; an inventory override naming another BU is refused when it "
            "is created."
        )
    else:
        # Unreachable in practice: an unmapped customer has no inventory pool, so
        # the preview's coverage pass raises InventoryScopeMissing before these
        # notes are assembled. Kept, and worded honestly, so that if a future
        # code path DOES produce a note set for such a customer the note does not
        # describe the removed legacy fallback.
        notes.append(
            f"Customer '{scenario.customer.name}' is not mapped to a Business "
            "Unit. On-hand inventory exists only per (Business Unit, product), so "
            "there is no pool to resolve any quantity against and no coverage can "
            "be computed for this customer at all -- map it to a Business Unit."
        )

    return tuple(notes)
