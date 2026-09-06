"""Scenario Planning -- preview, the no-write guarantee, and apply-to-base-plan.

The single most important test in this file is
`test_apply_matches_what_the_preview_promised`. A preview has no downstream
consumer to notice when it is wrong, so if it and the real thing ever disagree
the symptom is a planner promising a customer coverage the base plan then refuses
to deliver. That test compares them field by field.

Lead-time components are seeded so `is_recoverable` genuinely computes rather
than short-circuiting on a zero total (same reasoning as
tests/test_coverage_engine.py). FAR_ROS_DAYS keeps a line comfortably outside the
6.5-month 13CR lead time so a shortfall resolves UNCOVERED; NEAR_ROS_DAYS puts it
inside, so the same shortfall resolves UNRECOVERABLE. Those two constants are
what the recoverability tests move demand between.
"""

from datetime import datetime, timedelta

import pytest

from app.engines.coverage import compute_customer_coverage, recompute_customer
from app.engines.overrides import (
    NO_OVERRIDES,
    OVERRIDE_ENUM_VALUES,
    OVERRIDE_FIELDS,
    SUPPLY_KINDS,
    UNMODELLED_KINDS,
    OverrideError,
    ScenarioOverrides,
)
from app.engines.mrp import on_order_runout
from app.engines.scenario import (
    ScenarioImmutable,
    assert_mutable,
    assert_target_in_scope,
    preview,
)
from app.engines.scenario_apply import ScenarioNotApplicable, apply_to_base_plan
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    CoverageResult,
    CoverageStatus,
    Customer,
    CustomerSubstitutionRule,
    DemandLine,
    DemandProfile,
    DemandRevision,
    DemandStatus,
    ImpactRecord,
    InventoryAssignment,
    InventoryOnHand,
    InventoryOnOrder,
    LeadTimeComponent,
    LeadTimeDimension,
    PlanningNode,
    Product,
    Scenario,
    ScenarioOverride,
    ScenarioStatus,
    ScenarioTargetKind,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    UnitOfMeasure,
    Well,
    ANY_ATTRIBUTE_VALUE,
)

FAR_ROS_DAYS = 400
NEAR_ROS_DAYS = 30


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _lead_times(db):
    """6.5 months of 13CR lead time, as a COMPLETE attribute component set.

    All four dimensions, because an incomplete set resolves to "not modelled"
    (total 0) instead of being partially summed -- see app.engines.lead_time. The
    NEAR_ROS_DAYS / FAR_ROS_DAYS boundary this file is built on only means
    anything while the total really is 6.5 months.
    """
    if db.query(LeadTimeComponent).first() is None:
        db.add_all([
            LeadTimeComponent(
                dimension=LeadTimeDimension.OD_WT,
                attribute_value=ANY_ATTRIBUTE_VALUE, months=4.5, label="Ex-mill",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.GRADE, attribute_value="13CR", months=0.0
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.CONNECTION,
                attribute_value=ANY_ATTRIBUTE_VALUE, months=0.0,
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.LOGISTICS,
                attribute_value=ANY_ATTRIBUTE_VALUE, months=2.0, label="Sailing",
            ),
        ])
        db.flush()


def _bu(db, name="North"):
    bu = BusinessUnit(name=name)
    db.add(bu)
    db.flush()
    return bu


def _default_bu(db):
    """The BU a fixture customer lands in when a test does not name one.

    Get-or-create, so every `_customer(db)` in this file shares one inventory
    pool. `bu=None` no longer means 'unmapped': an unmapped customer has no
    pool at all and `app.engines.inventory` raises for it, so it is not a
    shape a scenario test can be written against. Tests that need TWO pools
    still pass their own `_bu(db, name)` explicitly.
    """
    bu = (
        db.query(BusinessUnit)
        .filter(BusinessUnit.name == "Scenario BU")
        .one_or_none()
    )
    if bu is None:
        bu = BusinessUnit(name="Scenario BU")
        db.add(bu)
        db.flush()
    return bu


def _customer(db, name="Scenario Co", bu=None, policy=AllocationPolicy.SOFT):
    customer = Customer(
        name=name,
        allocation_policy=policy,
        business_unit_id=(bu if bu is not None else _default_bu(db)).id,
    )
    db.add(customer)
    db.flush()
    node = PlanningNode(
        customer_id=customer.id, node_type="Campaign", name=f"{name} Campaign"
    )
    db.add(node)
    db.flush()
    _lead_times(db)
    return customer, node


def _product(db, on_hand_qty=0.0, grade="13CR80"):
    """Product + its on-hand row in the DEFAULT BU.

    `on_hand_qty` keeps its meaning but is stored where quantity lives. Tests
    working with an explicit `_bu(...)` still call `_stock` for that BU; the
    default-BU row written here is inert for them, since resolution is always
    scoped to the customer's own BU.
    """
    product = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="TBG", size="4-1/2", weight=12.6, grade=grade, grade_type="13CR",
        connection="VAM TOP", description=f"TBG 4-1/2 12.6 {grade} VAM TOP",
    )
    db.add(product)
    db.flush()
    _stock(db, _default_bu(db), product, on_hand_qty)
    return product


def _stock(db, bu, product, quantity):
    """Upsert the (BU, product) on-hand row -- the ONLY place a quantity lives.

    Idempotent so a fixture may restate a quantity without tripping
    uq_inventory_on_hand_bu_product. An explicit quantity of 0 states "this BU
    holds none of it", which is a FACT; no row at all means the quantity is
    unknown and every resolver raises rather than guessing.
    """
    row = (
        db.query(InventoryOnHand)
        .filter(
            InventoryOnHand.business_unit_id == bu.id,
            InventoryOnHand.product_id == product.id,
        )
        .one_or_none()
    )
    if row is None:
        row = InventoryOnHand(
            business_unit_id=bu.id, product_id=product.id, quantity=quantity,
            source_system="synthetic",
        )
        db.add(row)
    else:
        row.quantity = quantity
    db.flush()
    return row


def _well(db, node, name, demand_status=DemandStatus.CONFIRMED):
    """A well, WITH its demand status stated.

    Demand status is a property of the WELL now
    (`app.models.well.Well.demand_status`), so it is a fixture-construction argument
    here rather than something set per line. A test that wants an out-of-scope LINE
    has to give it an out-of-scope WELL -- which is the point: one well cannot hold
    two statuses.
    """
    well = Well(planning_node_id=node.id, name=name, demand_status=demand_status)
    db.add(well)
    db.flush()
    return well


def _line(db, well, product, quantity, days_out=FAR_ROS_DAYS):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity,
        ros_date=datetime.utcnow() + timedelta(days=days_out),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.flush()
    return line


def _scenario(db, customer, name="What if"):
    scenario = Scenario(
        name=name, customer_id=customer.id, status=ScenarioStatus.DRAFT,
        description="test scenario", created_by="tester",
    )
    db.add(scenario)
    db.flush()
    return scenario


def _override(db, scenario, **kwargs):
    override = ScenarioOverride(scenario_id=scenario.id, **kwargs)
    db.add(override)
    db.flush()
    db.expire(scenario, ["overrides"])
    return override


def _demand_override(db, scenario, line, field, *, number=None, date=None, text=None):
    return _override(
        db, scenario,
        target_kind=ScenarioTargetKind.DEMAND_LINE,
        target_demand_line_id=line.id,
        field_name=field,
        value_number=number, value_date=date, value_text=text,
    )


def _well_status_override(db, scenario, well, status_text):
    """"What if this well were <status>?" -- the well-level demand override.

    `status` is no longer a DEMAND_LINE field: demand status is a property of the
    WELL (`app.models.well.Well.demand_status`), so the question is asked of a well
    and the resolver applies it to every line of that well.
    """
    return _override(
        db, scenario,
        target_kind=ScenarioTargetKind.WELL,
        target_well_id=well.id,
        field_name="demand_status",
        value_text=status_text,
    )


def _status_of(impact, line_id):
    (change,) = [c for c in impact.line_changes if c.demand_line_id == line_id]
    return change.status_before, change.status_after


def _well_change(impact, well_id):
    (change,) = [c for c in impact.well_changes if c.well_id == well_id]
    return change


# --------------------------------------------------------------------------
# Scenarios that flip coverage
# --------------------------------------------------------------------------


def test_quantity_override_flips_a_well_from_uncovered_to_covered(db_session):
    """The headline case: reduce the requested quantity to what is on the shelf
    and the well becomes Covered in the preview -- with the official verdict
    still saying Uncovered."""
    product = _product(db_session, on_hand_qty=5000)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Flip")
    line = _line(db_session, well, product, quantity=9000)

    recompute_customer(db_session, customer)
    assert line.coverage_result.status == CoverageStatus.UNCOVERED
    assert well.coverage_status == CoverageStatus.UNCOVERED.value

    scenario = _scenario(db_session, customer, "Reduce Hawk order to 5000")
    _demand_override(db_session, scenario, line, "quantity", number=5000)

    impact = preview(db_session, scenario)

    assert _status_of(impact, line.id) == ("Uncovered", "Covered")
    change = _well_change(impact, well.id)
    assert (change.status_before, change.status_after) == ("Uncovered", "Covered")
    assert change.changed is True
    assert impact.changed_well_count == 1
    assert impact.covered_wells_before == 0
    assert impact.covered_wells_after == 1

    # And the official verdict has not moved an inch.
    assert line.coverage_result.status == CoverageStatus.UNCOVERED
    assert well.coverage_status == CoverageStatus.UNCOVERED.value


def test_ros_override_changes_recoverability(db_session):
    """An ROS push-out is the classic rescue: the same shortfall that is
    UNRECOVERABLE at a near ROS is merely UNCOVERED once the date moves outside
    the lead time. Recoverability must follow the OVERRIDDEN date, through the
    same physics the official pass uses."""
    product = _product(db_session, on_hand_qty=0)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Ros")
    line = _line(db_session, well, product, quantity=1000, days_out=NEAR_ROS_DAYS)

    recompute_customer(db_session, customer)
    assert line.coverage_result.status == CoverageStatus.UNRECOVERABLE

    scenario = _scenario(db_session, customer, "Push ROS out 400 days")
    _demand_override(
        db_session, scenario, line, "ros_date",
        date=datetime.utcnow() + timedelta(days=FAR_ROS_DAYS),
    )

    impact = preview(db_session, scenario)

    assert _status_of(impact, line.id) == ("Unrecoverable", "Uncovered")
    assert impact.risk.unrecoverable_lines_before == 1
    assert impact.risk.unrecoverable_lines_after == 0
    assert impact.risk.no_longer_unrecoverable == (line.id,)
    assert impact.risk.unrecoverable_quantity_before == 1000
    assert impact.risk.unrecoverable_quantity_after == 0


def test_ros_pull_in_creates_unrecoverable_risk(db_session):
    """The other direction, which is the one that bites: pulling an ROS forward
    into the lead time turns orderable demand into demand no mill order can
    save. The risk panel must show it."""
    product = _product(db_session, on_hand_qty=0)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Pull")
    line = _line(db_session, well, product, quantity=2500, days_out=FAR_ROS_DAYS)

    recompute_customer(db_session, customer)
    assert line.coverage_result.status == CoverageStatus.UNCOVERED

    scenario = _scenario(db_session, customer, "Rig available 30 days out")
    _demand_override(
        db_session, scenario, line, "ros_date",
        date=datetime.utcnow() + timedelta(days=NEAR_ROS_DAYS),
    )

    impact = preview(db_session, scenario)

    assert _status_of(impact, line.id) == ("Uncovered", "Unrecoverable")
    assert impact.risk.became_unrecoverable == (line.id,)
    assert impact.risk.unrecoverable_quantity_after == 2500


def test_status_override_brings_a_planned_well_into_scope(db_session):
    """"What if this demand becomes confirmed?" -- the spec's own first example.

    A Planned WELL is outside the coverage engine's status filter, so the base pass
    has NO verdict for any of its lines. The preview must report that as the distinct
    outcome "NotEvaluated" rather than as a blank, and must show the consequence of
    confirming it: here it consumes the pool and uncovers the other well.

    REPURPOSED to a WELL override, and the fixture already had one line per well, so
    nothing about the arithmetic moved. The override kind changed because
    `(DemandLine, status)` no longer exists -- demand status is a property of the
    well, and a per-line status override would let a preview model a well at two
    statuses, which is the state this change made unrepresentable.
    """
    product = _product(db_session, on_hand_qty=5000)
    customer, node = _customer(db_session)
    well_a = _well(db_session, node, "W-Confirmed")
    well_b = _well(db_session, node, "W-Planned", demand_status=DemandStatus.PLANNED)
    line_a = _line(db_session, well_a, product, quantity=4000, days_out=FAR_ROS_DAYS + 10)
    line_b = _line(db_session, well_b, product, quantity=3000, days_out=FAR_ROS_DAYS)

    recompute_customer(db_session, customer)
    assert line_a.coverage_result.status == CoverageStatus.COVERED
    assert db_session.get(CoverageResult, line_b.id) is None

    scenario = _scenario(db_session, customer, "Confirm the Planned well")
    _well_status_override(db_session, scenario, well_b, "Confirmed")

    impact = preview(db_session, scenario)

    # The newly in-scope line has the earlier ROS, so under earliest-ROS-first it
    # takes the pool and the previously covered line loses it.
    assert _status_of(impact, line_b.id) == ("NotEvaluated", "Covered")
    assert _status_of(impact, line_a.id) == ("Covered", "Uncovered")

    # That second flip is a line nobody overrode -- the second-order effect that
    # makes the screen worth having.
    (a_change,) = [c for c in impact.line_changes if c.demand_line_id == line_a.id]
    assert a_change.directly_overridden is False
    (b_change,) = [c for c in impact.line_changes if c.demand_line_id == line_b.id]
    assert b_change.directly_overridden is True


def test_status_override_takes_a_whole_well_out_of_scope(db_session):
    """The reverse move: taking a WELL out of scope frees its inventory.

    REPURPOSED twice, and never weakened. It first made the move by overriding
    `profile` to Contingency (which stopped excluding when the owner widened the
    profile default), then by overriding a LINE's `status` to Budgeted, and now by
    overriding the WELL's demand status -- because that is where demand status
    lives, and a per-line status override would model a well at two statuses.

    The BEHAVIOUR under test is unchanged and is still exactly what it was: demand
    the filters exclude is not evaluated, gets no verdict, and releases the
    inventory it was consuming to the demand that remains. The companion test below
    pins the other half of the owner's earlier change: Contingency IS in scope.
    """
    product = _product(db_session, on_hand_qty=5000)
    customer, node = _customer(db_session)
    well_a = _well(db_session, node, "W-Early")
    well_b = _well(db_session, node, "W-Late")
    line_a = _line(db_session, well_a, product, quantity=4000, days_out=FAR_ROS_DAYS)
    line_b = _line(db_session, well_b, product, quantity=4000, days_out=FAR_ROS_DAYS + 10)

    recompute_customer(db_session, customer)
    assert line_b.coverage_result.status == CoverageStatus.UNCOVERED

    scenario = _scenario(db_session, customer, "Deprioritise the early well")
    _well_status_override(db_session, scenario, well_a, "Budgeted")

    impact = preview(db_session, scenario)

    assert _status_of(impact, line_a.id) == ("Covered", "NotEvaluated")
    assert _status_of(impact, line_b.id) == ("Uncovered", "Covered")


def test_contingency_override_keeps_a_line_in_scope(db_session):
    """Contingency is IN the default profile scope, so this override frees NOTHING.

    The other half of the owner's default-filter change, and the reason the test
    above had to move to a different field. A contingency string is steel the well
    may genuinely need, so it now competes for inventory like any other in-scope
    line -- and a planner who deprioritises a line to Contingency hoping to release
    its steel must be told, honestly, that it did not.

    Asserted as the exact SAME fixture as the test above with only the override
    field changed, so the pair reads as the contrast it is: identical demand,
    identical inventory, one override excludes and the other does not.
    """
    product = _product(db_session, on_hand_qty=5000)
    customer, node = _customer(db_session)
    well_a = _well(db_session, node, "W-Early")
    well_b = _well(db_session, node, "W-Late")
    line_a = _line(db_session, well_a, product, quantity=4000, days_out=FAR_ROS_DAYS)
    line_b = _line(db_session, well_b, product, quantity=4000, days_out=FAR_ROS_DAYS + 10)

    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Call the early well contingency")
    _demand_override(db_session, scenario, line_a, "profile", text="Contingency")

    impact = preview(db_session, scenario)

    # Still evaluated, still Covered, still consuming its 4000 -- so line_b is still
    # short. Nothing moved, which is the answer.
    assert _status_of(impact, line_a.id) == ("Covered", "Covered")
    assert _status_of(impact, line_b.id) == ("Uncovered", "Uncovered")


def test_substitution_approval_override_gives_covered_via_substitute(db_session):
    """"What if the customer approved this substitute?"

    Layers 1 and 2 clear and the substitute physically exists, so the official
    verdict is PendingApproval. Overriding the well-layer approval to Approved
    must produce CoveredViaSubstitute -- via the real substitution engine, with
    the real three-layer logic, not a shortcut.
    """
    bu = _bu(db_session)
    primary = _product(db_session, on_hand_qty=0, grade="13CR80")
    substitute = _product(db_session, on_hand_qty=0, grade="13CR110")
    _stock(db_session, bu, primary, 0)
    _stock(db_session, bu, substitute, 9000)

    customer, node = _customer(db_session, bu=bu)
    well = _well(db_session, node, "W-Sub")
    line = _line(db_session, well, primary, quantity=4000)

    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.add(
        CustomerSubstitutionRule(
            customer_id=customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db_session.flush()

    recompute_customer(db_session, customer)
    assert line.coverage_result.status == CoverageStatus.PENDING_APPROVAL

    scenario = _scenario(db_session, customer, "Customer approves the 110 substitute")
    _override(
        db_session, scenario,
        target_kind=ScenarioTargetKind.SUBSTITUTION_APPROVAL,
        target_demand_line_id=line.id,
        target_from_product_id=primary.id,
        target_to_product_id=substitute.id,
        field_name="approval_status",
        value_text="Approved",
    )

    impact = preview(db_session, scenario)

    assert _status_of(impact, line.id) == ("PendingApproval", "CoveredViaSubstitute")
    assert _well_change(impact, well.id).status_after == "Covered"
    # Official verdict untouched.
    assert line.coverage_result.status == CoverageStatus.PENDING_APPROVAL


def test_inventory_override_covers_a_shortfall(db_session):
    """A supply-side what-if: more stock arrives in OUR Business Unit."""
    bu = _bu(db_session)
    product = _product(db_session, on_hand_qty=0)
    _stock(db_session, bu, product, 1000)

    customer, node = _customer(db_session, bu=bu)
    well = _well(db_session, node, "W-Inv")
    line = _line(db_session, well, product, quantity=4000)

    recompute_customer(db_session, customer)
    assert line.coverage_result.status == CoverageStatus.UNCOVERED

    scenario = _scenario(db_session, customer, "Extra 3000 lands early")
    _override(
        db_session, scenario,
        target_kind=ScenarioTargetKind.INVENTORY,
        target_product_id=product.id,
        target_business_unit_id=bu.id,
        field_name="quantity",
        value_number=4000,
    )

    impact = preview(db_session, scenario)
    assert _status_of(impact, line.id) == ("Uncovered", "Covered")


def test_assignment_override_covers_a_hard_allocated_line(db_session):
    """HARD allocation semantics must survive an override: coverage still comes
    from the assignment alone, so raising the assignment is what covers the line
    -- and the raised quantity is taken out of the unassigned pool, not invented.
    """
    bu = _bu(db_session)
    product = _product(db_session, on_hand_qty=0)
    _stock(db_session, bu, product, 12000)

    customer, node = _customer(db_session, bu=bu, policy=AllocationPolicy.HARD)
    well = _well(db_session, node, "W-Hard")
    line = _line(db_session, well, product, quantity=5000)
    db_session.add(
        InventoryAssignment(
            demand_line_id=line.id, product_id=product.id, quantity=1000,
            source_system="synthetic",
        )
    )
    db_session.flush()

    recompute_customer(db_session, customer)
    assert line.coverage_result.status == CoverageStatus.UNCOVERED

    scenario = _scenario(db_session, customer, "Assign the full 5000")
    _override(
        db_session, scenario,
        target_kind=ScenarioTargetKind.ASSIGNMENT,
        target_demand_line_id=line.id,
        target_product_id=product.id,
        field_name="quantity",
        value_number=5000,
    )

    impact = preview(db_session, scenario)
    assert _status_of(impact, line.id) == ("Uncovered", "Covered")


def test_assignment_override_does_nothing_under_soft_allocation(db_session):
    """SOFT ignores assignment rows entirely -- that is what pooling means -- so
    an assignment override on a soft customer must change nothing. Preserving
    that is how we know the override layer did not quietly re-open a door the
    allocation policies closed."""
    bu = _bu(db_session)
    product = _product(db_session, on_hand_qty=0)
    _stock(db_session, bu, product, 1000)

    customer, node = _customer(db_session, bu=bu, policy=AllocationPolicy.SOFT)
    well = _well(db_session, node, "W-Soft")
    line = _line(db_session, well, product, quantity=4000)
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Assign 4000 (soft: irrelevant)")
    _override(
        db_session, scenario,
        target_kind=ScenarioTargetKind.ASSIGNMENT,
        target_demand_line_id=line.id,
        target_product_id=product.id,
        field_name="quantity",
        value_number=4000,
    )

    impact = preview(db_session, scenario)
    assert _status_of(impact, line.id) == ("Uncovered", "Uncovered")
    assert impact.changed_line_count == 0


# --------------------------------------------------------------------------
# MRP impact
# --------------------------------------------------------------------------


def test_preview_reports_mrp_rows_removed_and_changed(db_session):
    """Coverage impact is not the whole story: the order book moves too.

    Two uncovered lines of the same product produce ONE aggregated MRP row.
    Covering one of them must leave a "changed" row with a smaller quantity;
    covering both must remove the row entirely.
    """
    product = _product(db_session, on_hand_qty=0)
    customer, node = _customer(db_session)
    well_a = _well(db_session, node, "W-A")
    well_b = _well(db_session, node, "W-B")
    line_a = _line(db_session, well_a, product, quantity=1000)
    _line(db_session, well_b, product, quantity=2000, days_out=FAR_ROS_DAYS + 10)
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Drop A to zero")
    _demand_override(db_session, scenario, line_a, "quantity", number=0)

    impact = preview(db_session, scenario)

    (row,) = impact.mrp_changes
    assert row.product_id == product.id
    assert row.kind == "changed"
    assert row.quantity_before == 3000
    # The zero-quantity line is covered by the empty pool (0 <= 0), so only B's
    # 2000 remains unresolved.
    assert row.quantity_after == 2000


def test_preview_reports_an_mrp_row_being_removed(db_session):
    product = _product(db_session, on_hand_qty=1000)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Only")
    line = _line(db_session, well, product, quantity=4000)
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Fit within stock")
    _demand_override(db_session, scenario, line, "quantity", number=1000)

    impact = preview(db_session, scenario)
    (row,) = impact.mrp_changes
    assert row.kind == "removed"
    # 4000 demanded, 1000 drawn from the pool: the NET shortfall (F04) is 3000.
    assert row.quantity_before == 3000
    assert row.quantity_after is None


# --------------------------------------------------------------------------
# The no-write guarantee
# --------------------------------------------------------------------------


def _coverage_snapshot(db):
    """Every persisted coverage fact, as plain comparable values."""
    rows = {
        r.demand_line_id: (r.status, r.reason, r.fulfilled_by_product_id)
        for r in db.query(CoverageResult).all()
    }
    wells = {w.id: w.coverage_status for w in db.query(Well).all()}
    return rows, wells


def _mixed_scenario(db):
    """One customer, several wells, and a scenario touching all three families."""
    bu = _bu(db)
    primary = _product(db, on_hand_qty=0, grade="13CR80")
    substitute = _product(db, on_hand_qty=0, grade="13CR110")
    _stock(db, bu, primary, 5000)
    _stock(db, bu, substitute, 9000)

    customer, node = _customer(db, bu=bu)
    well_a = _well(db, node, "W-Mix-A")
    well_b = _well(db, node, "W-Mix-B")
    line_a = _line(db, well_a, primary, quantity=9000)
    line_b = _line(db, well_b, primary, quantity=4000, days_out=NEAR_ROS_DAYS)

    db.add(TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id))
    db.add(
        CustomerSubstitutionRule(
            customer_id=customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db.flush()
    recompute_customer(db, customer)

    scenario = _scenario(db, customer, "Mixed")
    _demand_override(db, scenario, line_a, "quantity", number=5000)
    _override(
        db, scenario,
        target_kind=ScenarioTargetKind.INVENTORY,
        target_product_id=primary.id, target_business_unit_id=bu.id,
        field_name="quantity", value_number=6000,
    )
    _override(
        db, scenario,
        target_kind=ScenarioTargetKind.SUBSTITUTION_APPROVAL,
        target_demand_line_id=line_b.id,
        target_from_product_id=primary.id, target_to_product_id=substitute.id,
        field_name="approval_status", value_text="Approved",
    )
    db.flush()
    return {
        "customer": customer, "scenario": scenario,
        "line_a": line_a, "line_b": line_b,
        "well_a": well_a, "well_b": well_b, "bu": bu,
        "primary": primary, "substitute": substitute,
    }


def test_preview_performs_no_writes(db_session):
    """CoverageResult rows and Well.coverage_status must be byte-identical before
    and after -- including after an explicit flush, so nothing was merely QUEUED
    for a later commit to pick up."""
    s = _mixed_scenario(db_session)
    db_session.flush()

    before = _coverage_snapshot(db_session)
    before_counts = (
        len(db_session.new), len(db_session.dirty), len(db_session.deleted)
    )
    before_rows = db_session.query(CoverageResult).count()

    for _ in range(3):  # repeated calls must be side-effect free too
        preview(db_session, s["scenario"])

    assert _coverage_snapshot(db_session) == before
    assert (
        len(db_session.new), len(db_session.dirty), len(db_session.deleted)
    ) == before_counts
    assert db_session.query(CoverageResult).count() == before_rows

    # And nothing was queued that a later commit could flush.
    db_session.flush()
    assert _coverage_snapshot(db_session) == before

    # No DemandRevision or ImpactRecord either -- a preview is not a revision.
    # (Creation revisions exist for the fixture's lines; a preview must add none.)
    assert _applied_revisions(db_session) == []
    assert db_session.query(ImpactRecord).count() == 0
    # And no approval row was conjured for the hypothetical approval.
    assert s["line_b"].coverage_result.status != CoverageStatus.COVERED_VIA_SUBSTITUTE


def test_preview_does_not_mutate_the_demand_line_it_overrides(db_session):
    """The obvious wrong implementation is "mutate, compute, roll back". It would
    make the row dirty, so any later commit on the same Session would persist a
    what-if as fact. LineView exists so that never happens."""
    s = _mixed_scenario(db_session)
    line = s["line_a"]
    original = (
        line.quantity, line.ros_date, line.well.demand_status, line.profile
    )

    impact = preview(db_session, s["scenario"])
    assert impact.override_count == 3

    assert (
        line.quantity, line.ros_date, line.well.demand_status, line.profile
    ) == original
    assert line not in db_session.dirty


def test_preview_write_tripwire_fires(db_session):
    """The layer-4 guard is real, not decorative: if anything ever does leave a
    pending change in the session, the preview raises instead of letting a GET
    quietly overwrite the official answer."""
    from app.engines import scenario as scenario_engine

    s = _mixed_scenario(db_session)
    original = scenario_engine._line_changes

    def _sneaky_write(base, after, well_names, resolver):
        # Exactly the kind of edit the tripwire exists to catch.
        s["line_a"].quantity = 1.0
        return original(base, after, well_names, resolver)

    scenario_engine._line_changes = _sneaky_write
    try:
        with pytest.raises(AssertionError, match="must not modify the session"):
            preview(db_session, s["scenario"])
    finally:
        scenario_engine._line_changes = original


def test_official_pass_and_preview_share_one_implementation(db_session):
    """A scenario with NO overrides must produce an 'after' identical to the
    recomputed base, line for line.

    This is the structural check behind "one implementation": the official path
    is `compute_customer_coverage` with the identity resolver, so an empty
    scenario cannot possibly differ. If this ever fails, the two paths have
    diverged and every other assertion in this file is worthless.
    """
    s = _mixed_scenario(db_session)
    empty = _scenario(db_session, s["customer"], "No overrides")

    impact = preview(db_session, empty)
    assert impact.changed_line_count == 0
    assert impact.changed_well_count == 0
    assert all(not c.changed for c in impact.well_changes)
    assert any("no overrides yet" in n for n in impact.notes)

    official = compute_customer_coverage(
        db_session, s["customer"], resolver=NO_OVERRIDES
    )
    for change in impact.line_changes:
        assert change.status_after == official.by_line[change.demand_line_id].status.value


# --------------------------------------------------------------------------
# The Business Unit boundary
# --------------------------------------------------------------------------


def test_inventory_override_cannot_name_another_business_unit(db_session):
    """A scenario is not an exception to the BU boundary. An inventory override
    naming a foreign BU is REFUSED, not clamped -- a clamped override would sit in
    the scenario looking as though it had been taken into account."""
    bu_north = _bu(db_session, "North")
    bu_gulf = _bu(db_session, "Gulf")
    product = _product(db_session, on_hand_qty=0)
    _stock(db_session, bu_north, product, 1000)
    _stock(db_session, bu_gulf, product, 50000)

    customer, node = _customer(db_session, bu=bu_north)
    well = _well(db_session, node, "W-Bounded")
    line = _line(db_session, well, product, quantity=4000)
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Borrow from Gulf")
    bad = ScenarioOverride(
        scenario_id=scenario.id,
        target_kind=ScenarioTargetKind.INVENTORY,
        target_product_id=product.id,
        target_business_unit_id=bu_gulf.id,
        field_name="quantity",
        value_number=50000,
    )

    with pytest.raises(OverrideError, match="hard inventory boundary"):
        ScenarioOverrides([bad], customer)

    # Belt and braces: even if such a row were somehow persisted, the preview
    # refuses to run rather than computing with it silently dropped.
    db_session.add(bad)
    db_session.flush()
    db_session.expire(scenario, ["overrides"])
    with pytest.raises(OverrideError):
        preview(db_session, scenario)

    # The official verdict is still the honest one: BU Gulf's 50000 never helped.
    assert line.coverage_result.status == CoverageStatus.UNCOVERED


def test_inventory_override_does_not_leak_into_another_bu(db_session):
    """The other half of the boundary: an override raising OUR BU's quantity must
    not move a customer in a DIFFERENT BU, whose coverage the preview never
    recomputes at all."""
    bu_north = _bu(db_session, "North")
    bu_gulf = _bu(db_session, "Gulf")
    product = _product(db_session, on_hand_qty=0)
    _stock(db_session, bu_north, product, 1000)
    _stock(db_session, bu_gulf, product, 1000)

    north_co, north_node = _customer(db_session, "North Co", bu_north)
    gulf_co, gulf_node = _customer(db_session, "Gulf Co", bu_gulf)
    north_line = _line(
        db_session, _well(db_session, north_node, "W-N"), product, 4000
    )
    gulf_line = _line(db_session, _well(db_session, gulf_node, "W-G"), product, 4000)
    recompute_customer(db_session, north_co)
    recompute_customer(db_session, gulf_co)

    scenario = _scenario(db_session, north_co, "North gets more steel")
    _override(
        db_session, scenario,
        target_kind=ScenarioTargetKind.INVENTORY,
        target_product_id=product.id, target_business_unit_id=bu_north.id,
        field_name="quantity", value_number=9000,
    )

    impact = preview(db_session, scenario)

    assert _status_of(impact, north_line.id) == ("Uncovered", "Covered")
    # Gulf's line is not even mentioned -- the preview is scoped to one customer.
    assert all(c.demand_line_id != gulf_line.id for c in impact.line_changes)
    assert gulf_line.coverage_result.status == CoverageStatus.UNCOVERED


def test_override_cannot_target_another_customers_demand_line(db_session):
    """A scenario belongs to one customer. An override on someone else's line
    would be silently ignored by the preview and then written for real by apply
    -- the exact shape of a preview that lies. Refused at creation."""
    from app.engines.scenario import assert_target_in_scope

    bu = _bu(db_session)
    product = _product(db_session, on_hand_qty=1000)
    mine, my_node = _customer(db_session, "Mine", bu)
    theirs, their_node = _customer(db_session, "Theirs", bu)
    their_line = _line(
        db_session, _well(db_session, their_node, "W-Theirs"), product, 4000
    )
    _line(db_session, _well(db_session, my_node, "W-Mine"), product, 500)

    scenario = _scenario(db_session, mine, "Reach across")
    bad = ScenarioOverride(
        scenario_id=scenario.id,
        target_kind=ScenarioTargetKind.DEMAND_LINE,
        target_demand_line_id=their_line.id,
        field_name="quantity", value_number=1,
    )
    with pytest.raises(ValueError, match="belongs to a different customer"):
        assert_target_in_scope(db_session, scenario, bad)


def _applied_revisions(db):
    """DemandRevision rows written by an APPLY, i.e. everything past revision 1.

    Every demand line now records its initial state as revision 1 the moment it is
    created (app.models.demand._write_initial_revision), so a bare
    ``query(DemandRevision).count()`` no longer means "revisions this operation
    wrote" -- it counts creation history too. Filtering to ``revision_no > 1``
    restores the exact original meaning: apply_to_base_plan appends
    ``current_revision_no + 1``, so anything it writes is numbered 2 or higher.
    Nothing here is relaxed to an inequality or a tolerance.
    """
    return (
        db.query(DemandRevision)
        .filter(DemandRevision.revision_no > 1)
        .order_by(DemandRevision.revision_no)
        .all()
    )


# --------------------------------------------------------------------------
# Apply to base plan
# --------------------------------------------------------------------------


def test_apply_creates_revisions_and_impact_records(db_session):
    product = _product(db_session, on_hand_qty=5000)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Apply")
    line = _line(db_session, well, product, quantity=9000)
    recompute_customer(db_session, customer)
    assert line.current_revision_no == 1

    scenario = _scenario(db_session, customer, "Agreed: reduce to 5000")
    _demand_override(db_session, scenario, line, "quantity", number=5000)

    result = apply_to_base_plan(db_session, scenario)

    revisions = _applied_revisions(db_session)
    assert len(revisions) == 1
    assert revisions[0].demand_line_id == line.id
    assert revisions[0].quantity == 5000
    assert revisions[0].revision_no == 2
    assert line.current_revision_no == 2

    impacts = db_session.query(ImpactRecord).all()
    assert len(impacts) == 1
    assert impacts[0].quantity_before == 9000
    assert impacts[0].quantity_after == 5000
    assert impacts[0].coverage_before == "Uncovered"
    assert impacts[0].coverage_after == "Covered"
    assert result.impact_record_ids == (impacts[0].id,)
    assert result.demand_revision_ids == (revisions[0].id,)

    assert line.coverage_result.status == CoverageStatus.COVERED
    assert well.coverage_status == CoverageStatus.COVERED.value
    assert scenario.status == ScenarioStatus.APPLIED
    assert scenario.applied_at is not None


def test_apply_writes_one_revision_per_line_not_per_override(db_session):
    """A DemandRevision is a snapshot of all four fields, not a diff of one. Two
    overrides on one line must therefore produce ONE revision carrying both --
    two would record an intermediate state the plan was never in."""
    product = _product(db_session, on_hand_qty=5000)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Two")
    line = _line(db_session, well, product, quantity=9000)
    recompute_customer(db_session, customer)

    new_ros = datetime.utcnow() + timedelta(days=FAR_ROS_DAYS + 60)
    scenario = _scenario(db_session, customer, "Reduce and push out")
    _demand_override(db_session, scenario, line, "quantity", number=4000)
    _demand_override(db_session, scenario, line, "ros_date", date=new_ros)

    apply_to_base_plan(db_session, scenario)

    revisions = _applied_revisions(db_session)
    assert len(revisions) == 1
    assert revisions[0].quantity == 4000
    assert revisions[0].ros_date == new_ros


def test_apply_matches_what_the_preview_promised(db_session):
    """THE test. Preview and apply must agree, line for line and well for well.

    A preview has no downstream consumer to notice when it is wrong, so a
    divergence here surfaces as a planner promising a customer coverage the base
    plan then refuses to deliver. The scenario deliberately mixes a demand
    override with a substitution-approval override and involves second-order
    effects (a pooled, ROS-ordered product) so the comparison is not trivial.
    """
    bu = _bu(db_session)
    primary = _product(db_session, on_hand_qty=0, grade="13CR80")
    substitute = _product(db_session, on_hand_qty=0, grade="13CR110")
    _stock(db_session, bu, primary, 5000)
    _stock(db_session, bu, substitute, 9000)

    customer, node = _customer(db_session, bu=bu)
    well_a = _well(db_session, node, "W-P-A")
    well_b = _well(db_session, node, "W-P-B")
    well_c = _well(db_session, node, "W-P-C")
    line_a = _line(db_session, well_a, primary, quantity=9000, days_out=FAR_ROS_DAYS)
    line_b = _line(db_session, well_b, primary, quantity=4000, days_out=FAR_ROS_DAYS + 5)
    _line(db_session, well_c, primary, quantity=2000, days_out=FAR_ROS_DAYS + 10)

    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.add(
        CustomerSubstitutionRule(
            customer_id=customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db_session.flush()
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Agreed package")
    _demand_override(db_session, scenario, line_a, "quantity", number=3000)
    _override(
        db_session, scenario,
        target_kind=ScenarioTargetKind.SUBSTITUTION_APPROVAL,
        target_demand_line_id=line_b.id,
        target_from_product_id=primary.id, target_to_product_id=substitute.id,
        field_name="approval_status", value_text="Approved",
    )

    promised = preview(db_session, scenario)
    promised_lines = {
        c.demand_line_id: c.status_after
        for c in promised.line_changes
        if c.status_after != "NotEvaluated"
    }
    promised_wells = {c.well_id: c.status_after for c in promised.well_changes}

    result = apply_to_base_plan(db_session, scenario)

    assert dict(result.line_status_after) == promised_lines
    assert dict(result.well_status_after) == promised_wells

    # And the same again read straight off the persisted rows, so this is not a
    # comparison of two in-memory objects that happen to share a bug.
    persisted_lines = {
        r.demand_line_id: r.status.value for r in db_session.query(CoverageResult).all()
    }
    assert persisted_lines == promised_lines
    persisted_wells = {
        w.id: w.coverage_status
        for w in db_session.query(Well).all()
    }
    assert persisted_wells == promised_wells


def test_apply_decides_a_substitution_approval_that_did_not_exist(db_session):
    """Overriding an approval for a pair nobody had even requested is the common
    case. Applying must create the request through the normal engine and then
    decide it -- not fabricate a row."""
    bu = _bu(db_session)
    primary = _product(db_session, on_hand_qty=0, grade="13CR80")
    substitute = _product(db_session, on_hand_qty=0, grade="13CR110")
    _stock(db_session, bu, primary, 0)
    _stock(db_session, bu, substitute, 9000)

    customer, node = _customer(db_session, bu=bu)
    well = _well(db_session, node, "W-SubApply")
    line = _line(db_session, well, primary, quantity=4000)
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.add(
        CustomerSubstitutionRule(
            customer_id=customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db_session.flush()
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Customer approved on the call")
    _override(
        db_session, scenario,
        target_kind=ScenarioTargetKind.SUBSTITUTION_APPROVAL,
        target_demand_line_id=line.id,
        target_to_product_id=substitute.id,
        field_name="approval_status", value_text="Approved",
    )

    result = apply_to_base_plan(db_session, scenario)

    from app.models import WellSubstitutionApproval

    approvals = db_session.query(WellSubstitutionApproval).all()
    assert len(approvals) == 1
    assert approvals[0].status == SubstitutionApprovalStatus.APPROVED
    assert approvals[0].decided_at is not None
    assert result.decided_approval_ids == (approvals[0].id,)
    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE


def test_apply_refuses_supply_overrides_and_writes_nothing(db_session):
    """The Oracle system boundary, enforced.

    InventoryOnHand is a read-only projection of Oracle-owned data. Applying a
    scenario value into it would be authoritative for coverage until the next sync
    silently reverted it -- so the apply is refused, ALL of it, and the base plan
    is left exactly as it was.
    """
    bu = _bu(db_session)
    product = _product(db_session, on_hand_qty=0)
    _stock(db_session, bu, product, 1000)
    customer, node = _customer(db_session, bu=bu)
    well = _well(db_session, node, "W-Oracle")
    line = _line(db_session, well, product, quantity=4000)
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "More stock plus a demand cut")
    _override(
        db_session, scenario,
        target_kind=ScenarioTargetKind.INVENTORY,
        target_product_id=product.id, target_business_unit_id=bu.id,
        field_name="quantity", value_number=9000,
    )
    _demand_override(db_session, scenario, line, "quantity", number=900)

    # Previewing it is fine and expected -- that is the whole point of asking.
    impact = preview(db_session, scenario)
    assert impact.applicable is False
    assert any("Oracle" in b for b in impact.apply_blockers)
    assert any("read-only projections" in n for n in impact.notes)

    with pytest.raises(ScenarioNotApplicable, match="owned by Oracle"):
        apply_to_base_plan(db_session, scenario)

    # ALL-OR-NOTHING: the demand half was not applied either.
    assert _applied_revisions(db_session) == []
    assert db_session.query(ImpactRecord).count() == 0
    assert line.quantity == 4000
    assert scenario.status == ScenarioStatus.DRAFT
    assert scenario.applied_at is None
    # Named by (BU, product) rather than "the only row", because quantity now
    # exists per pair and this world holds more than one row.
    assert (
        db_session.query(InventoryOnHand)
        .filter(
            InventoryOnHand.business_unit_id == bu.id,
            InventoryOnHand.product_id == product.id,
        )
        .one()
        .quantity
        == 1000
    )


def _on_order(db, product, quantity, arrives_in_days=None, bu=None):
    """One row of the read-only Oracle purchase-order projection.

    `arrives_in_days=None` writes a row with NO promised arrival date -- a real
    state for a PO raised but not acknowledged, and the case a shift must NOT
    invent a date for.
    """
    row = InventoryOnOrder(
        business_unit_id=(bu if bu is not None else _default_bu(db)).id,
        product_id=product.id,
        quantity=quantity,
        expected_arrival_date=(
            datetime.utcnow() + timedelta(days=arrives_in_days)
            if arrives_in_days is not None
            else None
        ),
        source_system="synthetic",
    )
    db.add(row)
    db.flush()
    return row


def _po_arrival_override(db, scenario, product, arrives_in_days):
    return _override(
        db, scenario,
        target_kind=ScenarioTargetKind.PO_ARRIVAL,
        target_product_id=product.id,
        field_name="arrival_date",
        value_date=datetime.utcnow() + timedelta(days=arrives_in_days),
    )


def _po_scenario(db, *, po_days=200, on_hand=0.0, quantity=4000):
    """A customer whose only demand is met by ONE dated purchase order.

    on_hand=0 makes the on-order row the ONLY thing between the demand and a
    negative balance, so the runout month responds to its arrival date sharply
    instead of being buffered by stock. That is what makes the shift observable.
    """
    product = _product(db, on_hand_qty=on_hand)
    customer, node = _customer(db)
    well = _well(db, node, "W-Po")
    line = _line(db, well, product, quantity=quantity)
    _on_order(db, product, quantity + 1000, arrives_in_days=po_days)
    recompute_customer(db, customer)
    scenario = _scenario(db, customer, "Mill delivery moves")
    return product, customer, line, scenario


def test_po_arrival_is_no_longer_in_unmodelled_kinds(db_session):
    """The vocabulary must not still advertise this kind as unmodellable.

    REPURPOSED, not relaxed, from
    `test_po_arrival_override_is_recorded_but_reported_as_unmodelled`, which pinned
    the OPPOSITE and did so correctly for the platform as it then was. The stated
    reason PO_ARRIVAL was unmodelled, quoted from `ScenarioTargetKind`, was that
    "no engine reads incoming supply when deciding coverage [...] Moving a promised
    date would move a number on a report and no verdict anywhere."

    That first clause is STILL TRUE and the tests below pin it: coverage does not
    move. What changed is the inference drawn from it. The runout PROJECTION is a
    decision figure the MRP screen already draws, moving it requires no
    coverage-rule change, and it plainly does respond to an arrival date. So the
    override is modelled there and nowhere else. The old test's load-bearing
    assertions -- coverage unchanged, apply refused -- are not dropped; they are
    pinned harder, and separately, below.
    """
    assert ScenarioTargetKind.PO_ARRIVAL not in UNMODELLED_KINDS
    # Still previewable-but-not-applicable: InventoryOnOrder is a read-only Oracle
    # projection, so becoming modellable never made it writable.
    assert ScenarioTargetKind.PO_ARRIVAL in SUPPLY_KINDS


def test_po_arrival_override_shifts_the_runout_projection(db_session):
    """The load-bearing new behaviour: the preview's runout curve responds.

    The PO arrives 200 days out, comfortably before the 400-day ROS, so the
    on-order-aware projection never goes negative. Pushing that arrival past the
    ROS month makes the balance go negative at the ROS month -- a runout month
    appearing where there was none.
    """
    product, customer, line, scenario = _po_scenario(db_session, po_days=200)

    _po_arrival_override(db_session, scenario, product, arrives_in_days=500)
    impact = preview(db_session, scenario)

    assert impact.unmodelled_override_count == 0, (
        "PO_ARRIVAL is modelled now; counting it as unmodelled would tell the "
        "planner the figures ignored their override when they did not"
    )
    assert len(impact.supply_runout_changes) == 1
    change = impact.supply_runout_changes[0]
    assert change.product_id == product.id
    # Before: the PO lands ahead of demand, so the balance never goes negative.
    assert change.runout_month_before is None
    # After: it lands too late, so the ROS month runs out.
    assert change.runout_month_after == line.ros_date.strftime("%Y-%m")
    assert change.runout_month_changed is True
    assert change.shift_days == 300
    # The curve itself is reported, not just the two month labels.
    assert change.runout_before and change.runout_after
    assert min(p.closing_balance for p in change.runout_before) >= 0
    assert min(p.closing_balance for p in change.runout_after) < 0


def test_pulling_a_po_in_can_remove_a_runout_month(db_session):
    """The other direction, which is the one planners actually ask for.

    A PO landing AFTER the ROS leaves the ROS month short; pulling it in ahead of
    the ROS removes the runout entirely. Pinned separately from the push-out case
    because a sign error would pass one and fail the other.
    """
    product, customer, line, scenario = _po_scenario(db_session, po_days=500)

    _po_arrival_override(db_session, scenario, product, arrives_in_days=200)
    impact = preview(db_session, scenario)

    change = impact.supply_runout_changes[0]
    assert change.runout_month_before == line.ros_date.strftime("%Y-%m")
    assert change.runout_month_after is None
    assert change.runout_month_changed is True
    assert change.shift_days == -300


def test_po_arrival_override_does_not_move_any_coverage_verdict(db_session):
    """THE CONSTRAINT. Coverage is decided from on-hand stock alone.

    `app.engines.executive.ON_ORDER_NOTE` states the policy: no coverage figure
    "nets material already on order against demand -- coverage is decided from
    on-hand stock alone. Read the incoming_supply block beside them, never inside
    them." Making the arrival date modellable must not have quietly breached that,
    so every coverage-shaped number in the impact is pinned unchanged here.

    The setup is chosen so a breach WOULD show: on-hand is 0 and the line is
    UNCOVERED while 5000 is on order. If on-order leaked into coverage the line
    would flip to COVERED, which is exactly the wrong answer to ship.
    """
    product, customer, line, scenario = _po_scenario(db_session, po_days=200)
    _po_arrival_override(db_session, scenario, product, arrives_in_days=10)

    impact = preview(db_session, scenario)

    assert impact.supply_runout_changes, "the override must genuinely be modelled"
    # Coverage: identical before and after, line by line.
    assert impact.changed_line_count == 0
    assert impact.changed_well_count == 0
    assert impact.covered_lines_before == impact.covered_lines_after
    assert impact.covered_wells_before == impact.covered_wells_after
    for change in impact.line_changes:
        assert change.status_before == change.status_after, (
            "an on-order arrival date moved a coverage verdict -- coverage must be "
            "decided from on-hand stock alone"
        )
        assert change.changed is False
    # And the verdict really was a shortfall, so there was something to flip.
    assert _status_of(impact, line.id) == (
        CoverageStatus.UNCOVERED.value,
        CoverageStatus.UNCOVERED.value,
    )
    # Risk is derived from coverage, so it must be still too.
    assert (
        impact.risk.unrecoverable_lines_before
        == impact.risk.unrecoverable_lines_after
    )


def test_po_arrival_override_does_not_move_mrp_recommendation_rows(db_session):
    """MRP rows derive from coverage verdicts, so they cannot move either.

    Asserted rather than assumed: `mrp_changes` is built from each pass's
    unresolved lines, and if on-order had leaked into coverage the recommendation
    would vanish. Every row must read "unchanged". The preview's note says so in
    words; this pins that the words are true.
    """
    product, customer, line, scenario = _po_scenario(db_session, po_days=500)
    _po_arrival_override(db_session, scenario, product, arrives_in_days=30)

    impact = preview(db_session, scenario)

    assert impact.mrp_changes, "the shortfall must produce an MRP row to compare"
    for row in impact.mrp_changes:
        assert row.kind == "unchanged", (
            f"MRP row for {row.product_id} became {row.kind!r}: an on-order arrival "
            "date must not move a recommendation, which is derived from coverage"
        )


def test_the_note_says_what_moved_and_what_did_not(db_session):
    """An honest preview must distinguish "modelled" from "modelled everywhere".

    The old note said the override was NOT taken into account. That sentence would
    now be false, so it is replaced -- but the half of it that is still true (no
    coverage verdict moves) has to survive in substance, or a planner reads a moved
    runout month as a moved coverage answer.
    """
    product, customer, line, scenario = _po_scenario(db_session, po_days=200)
    _po_arrival_override(db_session, scenario, product, arrives_in_days=500)

    notes = preview(db_session, scenario).notes

    assert any("IS modelled" in n for n in notes)
    assert any("DOES NOT MOVE ANY COVERAGE VERDICT" in n for n in notes)
    assert any("on-hand stock alone" in n for n in notes)
    # The old "was NOT taken into account" wording must be gone: it is now a lie.
    assert not any("was NOT taken into account" in n for n in notes)


def test_an_undated_purchase_order_is_not_shifted_onto_a_date(db_session):
    """A PO with no promised date has no schedule position to move.

    Inventing one from the override would fabricate supply arriving in a month,
    the same class of error as reading a missing on-order row as 0. It stays
    counted in the on-order total and excluded from both projections.
    """
    product = _product(db_session, on_hand_qty=0)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Undated")
    line = _line(db_session, well, product, quantity=4000)
    _on_order(db_session, product, 7000, arrives_in_days=None)
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Mill promises a date")
    _po_arrival_override(db_session, scenario, product, arrives_in_days=100)

    change = preview(db_session, scenario).supply_runout_changes[0]

    assert change.on_order_undated_quantity == 7000
    assert change.on_order_dated_quantity == 0
    # No dated row, so nothing to shift and nothing to project.
    assert change.arrival_before is None
    assert change.arrival_after is None
    assert change.shift_days == 0
    assert change.runout_month_changed is False
    # The demand is still unmet on both curves -- undated steel did NOT rescue it.
    assert change.runout_month_after == line.ros_date.strftime("%Y-%m")


def test_a_delivery_schedule_shifts_uniformly_rather_than_collapsing(db_session):
    """Several POs for one (BU, product) are normal -- that is a delivery schedule.

    The override restates the EARLIEST arrival and every other dated row moves by
    the SAME offset, so the schedule keeps its shape. Collapsing all rows onto the
    one overridden date would double the supply available in that month, which is
    the specific error this pins against: the two arrival months must stay two
    distinct months, 200 days apart, after the shift.
    """
    product = _product(db_session, on_hand_qty=0)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Sched")
    _line(db_session, well, product, quantity=4000)
    _on_order(db_session, product, 1000, arrives_in_days=100)
    _on_order(db_session, product, 1000, arrives_in_days=300)
    recompute_customer(db_session, customer)

    base = on_order_runout(db_session, product.id)
    assert sum(base.incoming_by_month.values()) == 2000
    assert len(base.incoming_by_month) == 2, base.incoming_by_month

    shifted = on_order_runout(
        db_session,
        product.id,
        arrival_override=(datetime.utcnow() + timedelta(days=130)).date(),
    )
    # Same total, still in TWO months -- not merged onto the overridden date.
    assert sum(shifted.incoming_by_month.values()) == 2000
    assert len(shifted.incoming_by_month) == 2, shifted.incoming_by_month
    assert shifted.earliest_arrival == base.earliest_arrival + timedelta(days=30)

    # And the scenario preview reports that same uniform 30-day offset.
    scenario = _scenario(db_session, customer, "Whole schedule slips")
    _po_arrival_override(db_session, scenario, product, arrives_in_days=130)
    change = preview(db_session, scenario).supply_runout_changes[0]
    assert change.shift_days == 30
    assert change.on_order_dated_quantity == 2000


def test_po_arrival_override_is_refused_at_apply_time(db_session):
    """Previewable, never applicable -- the identical refusal INVENTORY gets.

    `InventoryOnOrder` is a read-only projection of Oracle's purchase orders,
    exactly as `InventoryOnHand` is of Oracle's stock. Modelling the override in a
    projection did not make the underlying row writable, and the refusal wording is
    the SHARED supply-override one rather than a special case, so the two cannot
    drift apart.
    """
    product, customer, line, scenario = _po_scenario(db_session, po_days=200)
    _po_arrival_override(db_session, scenario, product, arrives_in_days=500)

    impact = preview(db_session, scenario)
    assert impact.applicable is False
    assert any(
        "cannot be written to the base plan" in b and "owned by Oracle" in b
        for b in impact.apply_blockers
    ), impact.apply_blockers
    assert any("PoArrival" in b for b in impact.apply_blockers), impact.apply_blockers

    with pytest.raises(ScenarioNotApplicable):
        apply_to_base_plan(db_session, scenario)

    # And the refusal did not half-happen: the scenario is still a draft.
    assert scenario.status == ScenarioStatus.DRAFT


def test_previewing_a_po_arrival_override_writes_nothing(db_session):
    """The four-layer no-write guarantee, re-verified for the NEW code path.

    `_supply_runout_changes` is the first thing added to `preview` that queries a
    table the coverage pass never touches, so the guarantee is re-checked here
    rather than assumed to have been inherited. `preview` itself raises via
    `_assert_no_writes` if the session's pending sets move; this pins that the
    Oracle-owned arrival date and the stored coverage verdict on disk are untouched
    too.
    """
    product, customer, line, scenario = _po_scenario(db_session, po_days=200)
    _po_arrival_override(db_session, scenario, product, arrives_in_days=500)

    before_arrival = (
        db_session.query(InventoryOnOrder)
        .filter(InventoryOnOrder.product_id == product.id)
        .one()
        .expected_arrival_date
    )
    status_before = db_session.get(CoverageResult, line.id).status

    preview(db_session, scenario)  # raises via _assert_no_writes if anything pended

    db_session.expire_all()
    assert (
        db_session.query(InventoryOnOrder)
        .filter(InventoryOnOrder.product_id == product.id)
        .one()
        .expected_arrival_date
        == before_arrival
    ), "the preview restated an Oracle-owned arrival date on disk"
    assert db_session.get(CoverageResult, line.id).status == status_before


def test_an_applied_scenario_cannot_be_reapplied_or_mutated(db_session):
    """Applied is terminal. Re-applying would append a second revision recording
    a change of nothing; editing would make the row describe overrides that were
    never applied while still claiming an applied_at."""
    product = _product(db_session, on_hand_qty=5000)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Once")
    line = _line(db_session, well, product, quantity=9000)
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Apply once")
    _demand_override(db_session, scenario, line, "quantity", number=5000)
    apply_to_base_plan(db_session, scenario)

    revisions_after_first = len(_applied_revisions(db_session))

    with pytest.raises(ScenarioImmutable, match="immutable record"):
        apply_to_base_plan(db_session, scenario)
    with pytest.raises(ScenarioImmutable):
        assert_mutable(scenario)

    assert len(_applied_revisions(db_session)) == revisions_after_first
    assert db_session.query(ImpactRecord).count() == 1


def test_apply_skips_an_override_that_matches_the_current_value(db_session):
    """An override whose value already equals the base plan must not manufacture a
    revision recording a change of nothing."""
    product = _product(db_session, on_hand_qty=5000)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Noop")
    line = _line(db_session, well, product, quantity=9000)
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Change nothing")
    _demand_override(db_session, scenario, line, "quantity", number=9000)

    result = apply_to_base_plan(db_session, scenario)

    assert _applied_revisions(db_session) == []
    assert result.revised_demand_line_ids == ()
    assert any("match the values already in the base plan" in n for n in result.notes)
    # It still counts as applied: the agreement was "leave it alone".
    assert scenario.status == ScenarioStatus.APPLIED


# --------------------------------------------------------------------------
# Override validation
# --------------------------------------------------------------------------


def test_validate_rejects_a_field_that_does_not_belong_to_the_target(db_session):
    customer, _node = _customer(db_session)
    scenario = _scenario(db_session, customer)
    bad = ScenarioOverride(
        scenario_id=scenario.id,
        target_kind=ScenarioTargetKind.INVENTORY,
        target_product_id="p", target_business_unit_id=None,
        field_name="ros_date", value_date=datetime.utcnow(),
    )
    with pytest.raises(OverrideError, match="cannot be overridden"):
        ScenarioOverrides([bad], customer)


def test_validate_rejects_a_wrongly_typed_value(db_session):
    customer, _node = _customer(db_session)
    scenario = _scenario(db_session, customer)
    bad = ScenarioOverride(
        scenario_id=scenario.id,
        target_kind=ScenarioTargetKind.DEMAND_LINE,
        target_demand_line_id="line",
        field_name="quantity", value_text="lots",
    )
    with pytest.raises(OverrideError, match="needs a number value"):
        ScenarioOverrides([bad], customer)


def test_validate_rejects_a_bad_enum_value(db_session):
    """REPURPOSED to the WELL target, because `status` is no longer a DEMAND_LINE
    field at all -- demand status is a property of the well.

    Not a weakening: the assertion is the same one (an illegal enum VALUE is
    refused by name), on the field that now carries that enum. The complementary
    case -- an illegal FIELD on a target kind -- is asserted by the test below,
    which is new and which pins that `status` on a DemandLine is now rejected
    outright rather than silently ignored.
    """
    customer, _node = _customer(db_session)
    scenario = _scenario(db_session, customer)
    bad = ScenarioOverride(
        scenario_id=scenario.id,
        target_kind=ScenarioTargetKind.WELL,
        target_well_id="well",
        field_name="demand_status", value_text="Maybe",
    )
    with pytest.raises(OverrideError, match="not a valid"):
        ScenarioOverrides([bad], customer)


def test_status_is_no_longer_a_demand_line_field(db_session):
    """A per-line status override is REFUSED, not ignored.

    Demand status is a property of the well, so an override that named a LINE would
    be asking the preview to model one well at two statuses. `validate` is re-run
    when a scenario is read, so a row like this -- written by an older build --
    fails loudly instead of quietly changing nothing.
    """
    customer, _node = _customer(db_session)
    scenario = _scenario(db_session, customer)
    bad = ScenarioOverride(
        scenario_id=scenario.id,
        target_kind=ScenarioTargetKind.DEMAND_LINE,
        target_demand_line_id="line",
        field_name="status", value_text="Confirmed",
    )
    with pytest.raises(OverrideError, match="cannot be overridden on a DemandLine"):
        ScenarioOverrides([bad], customer)


def test_validate_rejects_a_negative_quantity(db_session):
    customer, _node = _customer(db_session)
    scenario = _scenario(db_session, customer)
    bad = ScenarioOverride(
        scenario_id=scenario.id,
        target_kind=ScenarioTargetKind.DEMAND_LINE,
        target_demand_line_id="line",
        field_name="quantity", value_number=-5,
    )
    with pytest.raises(OverrideError, match="cannot be negative"):
        ScenarioOverrides([bad], customer)


# --------------------------------------------------------------------------
# API routes
# --------------------------------------------------------------------------


def _client(db_session):
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app), app


def test_scenario_routes_end_to_end(db_session):
    """Create -> add override -> preview -> apply, over HTTP.

    Also pins that the PREVIEW route leaves the persisted coverage answer
    untouched, exactly as the cross-customer sharing route does.
    """
    product = _product(db_session, on_hand_qty=5000)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Http")
    line = _line(db_session, well, product, quantity=9000)
    recompute_customer(db_session, customer)
    db_session.flush()

    client, app = _client(db_session)
    try:
        vocab = client.get("/scenarios/override-fields")
        assert vocab.status_code == 200
        assert "quantity" in vocab.json()["fields"]["DemandLine"]
        assert "Inventory" in vocab.json()["supply_kinds"]

        created = client.post(
            "/scenarios",
            json={
                "name": "HTTP scenario",
                "customer_id": customer.id,
                "description": "via the API",
                "created_by": "tester",
            },
        )
        assert created.status_code == 201
        scenario_id = created.json()["id"]
        assert created.json()["status"] == "Draft"

        before = _coverage_snapshot(db_session)

        added = client.post(
            f"/scenarios/{scenario_id}/overrides",
            json={
                "target_kind": "DemandLine",
                "field_name": "quantity",
                "target_demand_line_id": line.id,
                "value_number": 5000,
            },
        )
        assert added.status_code == 201
        override_id = added.json()["id"]

        listed = client.get("/scenarios")
        assert listed.status_code == 200
        (row,) = listed.json()
        assert row["override_count"] == 1
        assert row["customer_name"] == customer.name
        assert row["coverage_delta_wells"] == 1

        got = client.get(f"/scenarios/{scenario_id}")
        assert len(got.json()["overrides"]) == 1

        prev = client.get(f"/scenarios/{scenario_id}/preview")
        assert prev.status_code == 200
        body = prev.json()
        assert body["is_what_if"] is True
        assert body["applicable"] is True
        assert body["changed_well_count"] == 1
        (well_change,) = [w for w in body["well_changes"] if w["changed"]]
        assert (well_change["status_before"], well_change["status_after"]) == (
            "Uncovered", "Covered",
        )
        # The GET wrote nothing.
        assert _coverage_snapshot(db_session) == before

        patched = client.patch(
            f"/scenarios/{scenario_id}", json={"status": "Review", "name": "Renamed"}
        )
        assert patched.status_code == 200
        assert patched.json()["status"] == "Review"
        assert patched.json()["name"] == "Renamed"

        # Cannot fake Applied through the metadata route.
        assert client.patch(
            f"/scenarios/{scenario_id}", json={"status": "Applied"}
        ).status_code == 400

        applied = client.post(f"/scenarios/{scenario_id}/apply")
        assert applied.status_code == 200
        assert len(applied.json()["impact_record_ids"]) == 1
        assert dict(
            (a, b) for a, b in applied.json()["well_status_after"]
        )[well.id] == "Covered"

        # Applied is terminal, over HTTP too.
        assert client.post(f"/scenarios/{scenario_id}/apply").status_code == 409
        assert client.patch(
            f"/scenarios/{scenario_id}", json={"name": "nope"}
        ).status_code == 409
        assert client.delete(
            f"/scenarios/{scenario_id}/overrides/{override_id}"
        ).status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_override_route_refuses_a_cross_bu_inventory_override(db_session):
    bu_north = _bu(db_session, "North")
    bu_gulf = _bu(db_session, "Gulf")
    product = _product(db_session, on_hand_qty=0)
    _stock(db_session, bu_north, product, 1000)
    customer, node = _customer(db_session, bu=bu_north)
    _line(db_session, _well(db_session, node, "W-X"), product, 4000)
    scenario = _scenario(db_session, customer)
    db_session.flush()

    client, app = _client(db_session)
    try:
        resp = client.post(
            f"/scenarios/{scenario.id}/overrides",
            json={
                "target_kind": "Inventory",
                "field_name": "quantity",
                "target_product_id": product.id,
                "target_business_unit_id": bu_gulf.id,
                "value_number": 50000,
            },
        )
        assert resp.status_code == 400
        assert "hard inventory boundary" in resp.json()["detail"]
        assert db_session.query(ScenarioOverride).count() == 0
    finally:
        app.dependency_overrides.clear()


def test_override_can_be_removed(db_session):
    product = _product(db_session, on_hand_qty=5000)
    customer, node = _customer(db_session)
    line = _line(db_session, _well(db_session, node, "W-Del"), product, 9000)
    recompute_customer(db_session, customer)
    scenario = _scenario(db_session, customer)
    db_session.flush()

    client, app = _client(db_session)
    try:
        added = client.post(
            f"/scenarios/{scenario.id}/overrides",
            json={
                "target_kind": "DemandLine",
                "field_name": "quantity",
                "target_demand_line_id": line.id,
                "value_number": 5000,
            },
        )
        override_id = added.json()["id"]
        assert client.get(f"/scenarios/{scenario.id}/preview").json()[
            "changed_well_count"
        ] == 1

        assert client.delete(
            f"/scenarios/{scenario.id}/overrides/{override_id}"
        ).status_code == 204
        assert client.get(f"/scenarios/{scenario.id}/preview").json()[
            "changed_well_count"
        ] == 0
        assert client.delete(
            f"/scenarios/{scenario.id}/overrides/{override_id}"
        ).status_code == 404
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# WELL demand-status overrides: previewable AND applicable
# --------------------------------------------------------------------------


def test_a_well_status_override_is_applied_through_the_well_level_writer(db_session):
    """Applying "confirm this well" cascades a revision to EVERY line of the well.

    `apply_to_base_plan` goes through `app.engines.coverage.set_well_demand_status`
    rather than assigning `well.demand_status`, for the same reason demand goes
    through `apply_revision`: the column is the well's authority, and moving it
    without writing the history would leave every line of that well claiming a status
    the well no longer has.
    """
    product = _product(db_session, on_hand_qty=100_000)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-ToConfirm", demand_status=DemandStatus.PLANNED)
    a = _line(db_session, well, product, quantity=1000, days_out=FAR_ROS_DAYS)
    b = _line(db_session, well, product, quantity=2000, days_out=FAR_ROS_DAYS + 10)
    recompute_customer(db_session, customer)
    assert well.coverage_status is None

    scenario = _scenario(db_session, customer, "Confirm W-ToConfirm")
    _well_status_override(db_session, scenario, well, "Confirmed")

    impact = preview(db_session, scenario)
    assert _status_of(impact, a.id) == ("NotEvaluated", "Covered")
    assert _status_of(impact, b.id) == ("NotEvaluated", "Covered")
    # Neither line was named by id -- the WELL was -- and both must still be shown as
    # directly overridden, or the editor would render a whole well moving with nothing
    # marked as the cause.
    for change in impact.line_changes:
        assert change.directly_overridden is True
    assert impact.applicable is True, impact.apply_blockers

    result = apply_to_base_plan(db_session, scenario)

    assert well.demand_status == DemandStatus.CONFIRMED
    assert well.coverage_status == CoverageStatus.COVERED.value
    # One revision and one impact record per line of the well.
    assert len(result.demand_revision_ids) == 2
    assert len(result.impact_record_ids) == 2
    for line in (a, b):
        db_session.expire(line, ["revisions"])
        assert line.current_revision_no == 2
        assert line.revisions[-1].status == DemandStatus.CONFIRMED
    assert any("demand status Planned -> Confirmed" in n for n in result.notes)


def test_a_well_status_override_matching_the_base_plan_writes_nothing(db_session):
    """Same rule the demand overrides follow: an override that changes nothing must
    not append history recording a change of nothing."""
    product = _product(db_session, on_hand_qty=100_000)
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Already")
    line = _line(db_session, well, product, quantity=1000)
    recompute_customer(db_session, customer)

    scenario = _scenario(db_session, customer, "Confirm what is already confirmed")
    _well_status_override(db_session, scenario, well, "Confirmed")

    result = apply_to_base_plan(db_session, scenario)

    assert result.demand_revision_ids == ()
    assert line.current_revision_no == 1
    assert any("already is" in n for n in result.notes)


def test_a_well_status_override_naming_another_customers_well_is_refused(db_session):
    """The scenario/customer boundary, extended to the new target kind.

    A scenario may only override its own customer's plan: coverage is computed per
    customer, so an override on somebody else's well would be invisible to the
    preview while `apply_to_base_plan` cascaded revisions through it.
    """
    product = _product(db_session, on_hand_qty=1000)
    customer, node = _customer(db_session)
    other, other_node = _customer(db_session, name="Someone Else")
    foreign_well = _well(db_session, other_node, "W-Foreign")
    _line(db_session, foreign_well, product, quantity=100)

    scenario = _scenario(db_session, customer, "Reach across")
    override = ScenarioOverride(
        scenario_id=scenario.id,
        target_kind=ScenarioTargetKind.WELL,
        target_well_id=foreign_well.id,
        field_name="demand_status",
        value_text="Budgeted",
    )
    with pytest.raises(ValueError, match="belongs to a different customer"):
        assert_target_in_scope(db_session, scenario, override)


def test_a_well_override_may_not_also_name_a_demand_line(db_session):
    """Naming a line alongside the well would suggest the other lines were
    unaffected, which is the impression the column move exists to remove."""
    customer, _node = _customer(db_session)
    scenario = _scenario(db_session, customer)
    bad = ScenarioOverride(
        scenario_id=scenario.id,
        target_kind=ScenarioTargetKind.WELL,
        target_well_id="well",
        target_demand_line_id="line",
        field_name="demand_status",
        value_text="Confirmed",
    )
    with pytest.raises(OverrideError, match="must not also name a demand line"):
        ScenarioOverrides([bad], customer)


def test_the_override_vocabulary_lists_demand_status_on_WELL_only(db_session):
    """The published vocabulary is what the editor's field pickers are built from, so
    it must not still advertise a per-line status."""
    assert OVERRIDE_FIELDS[ScenarioTargetKind.WELL] == {"demand_status": "text"}
    assert "status" not in OVERRIDE_FIELDS[ScenarioTargetKind.DEMAND_LINE]
    assert "demand_status" not in OVERRIDE_FIELDS[ScenarioTargetKind.DEMAND_LINE]
    assert OVERRIDE_ENUM_VALUES[(ScenarioTargetKind.WELL, "demand_status")] == (
        "Planned",
        "Budgeted",
        "Confirmed",
    )
    # A WELL override is NOT a supply override, so it can be applied. Recording it as
    # unmodelled (the PO_ARRIVAL treatment) was the alternative and was rejected: the
    # question is perfectly modellable at well level.
    assert ScenarioTargetKind.WELL not in SUPPLY_KINDS
    assert ScenarioTargetKind.WELL not in UNMODELLED_KINDS
