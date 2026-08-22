from datetime import date, datetime, timedelta

import pytest

from app.engines.coverage import recompute_well
from app.engines.mrp import by_item, mrp_summary, order_feasibility
from app.engines.order_dates import months_to_timedelta
from app.engines.substitution import decide_approval, request_approval
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    CoverageStatus,
    Customer,
    CustomerSubstitutionRule,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    LeadTimeComponent,
    LeadTimeDimension,
    PlanningNode,
    Product,
    TechnicalSubstitution,
    UnitOfMeasure,
    Well,
    ANY_ATTRIBUTE_VALUE,
)

# 13CR demo lead time: 4.5 ex-mill + 2 sailing = 6.5 months total, 2 of which
# are the shipping allowance stripped off the ROS date.
EX_MILL_MONTHS = 4.5
SAILING_MONTHS = 2.0
TOTAL_MONTHS = EX_MILL_MONTHS + SAILING_MONTHS


def _default_bu(db_session):
    """The one Business Unit these fixtures use, created on first request.

    Every customer built here is mapped to it and every product is stocked in it,
    so the fixtures describe ONE inventory pool -- which is what the tests in this
    file are about. On-hand quantity now exists only per (BU, product), and an
    unmapped customer has no pool at all (app.engines.inventory raises for it), so
    "customer with no BU" is no longer a usable fixture shape: it fails before any
    verdict is reached. The tests that are specifically ABOUT the boundary live in
    tests/test_business_unit_scope.py and build their BUs explicitly.
    """
    bu = (
        db_session.query(BusinessUnit)
        .filter(BusinessUnit.name == "MRP BU")
        .one_or_none()
    )
    if bu is None:
        bu = BusinessUnit(name="MRP BU")
        db_session.add(bu)
        db_session.flush()
    return bu


def _stock(db_session, bu, product, quantity):
    """Upsert the (BU, product) on-hand row.

    Idempotent so a fixture may restate a quantity without tripping
    uq_inventory_on_hand_bu_product. A quantity of 0 is written as an explicit 0
    row: "this BU holds none of it" is a fact, and it is not the same as no row,
    which means unknown and raises.
    """
    row = (
        db_session.query(InventoryOnHand)
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
        db_session.add(row)
    else:
        row.quantity = quantity
    db_session.flush()
    return row


def _customer(db_session, name="MRP Test Co"):
    customer = Customer(
        name=name,
        allocation_policy=AllocationPolicy.SOFT,
        business_unit_id=_default_bu(db_session).id,
    )
    db_session.add(customer)
    db_session.flush()
    node = PlanningNode(customer_id=customer.id, node_type="Campaign", name="MRP Campaign")
    db_session.add(node)
    db_session.flush()
    return customer, node


def _product(db_session, on_hand_qty=0.0, grade_type="13CR", grade="13CR80"):
    """Product + its on-hand row in the file's single BU.

    Writing the row even when `on_hand_qty` is 0 matters twice here:
    coverage refuses to resolve a (BU, product) pair with no row, and MRP's
    `by_item` refuses to draw a runout curve for a product with no row in ANY
    BU (unknown is not zero -- app.engines.inventory.total_on_hand_all_bus).
    """
    product = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="TBG", size="4-1/2", weight=12.6, grade=grade, grade_type=grade_type,
        connection="VAM TOP", description=f"TBG 4-1/2 12.6 {grade} VAM TOP",
    )
    db_session.add(product)
    db_session.flush()
    _stock(db_session, _default_bu(db_session), product, on_hand_qty)
    return product


def _lead_times(db_session, grade_type="13CR"):
    """A COMPLETE attribute component set totalling TOTAL_MONTHS for `grade_type`.

    All four dimensions are seeded because an incomplete set resolves to "not
    modelled" (total 0) rather than a partial sum -- see app.engines.lead_time.
    The Grade row is the only one keyed to `grade_type`, so a test that seeds one
    grade family and asks about a product in another still gets 0, exactly as it
    did when the whole table was keyed by grade_type.
    """
    db_session.add_all([
        LeadTimeComponent(
            dimension=LeadTimeDimension.OD_WT,
            attribute_value=ANY_ATTRIBUTE_VALUE,
            months=EX_MILL_MONTHS,
            label="Ex-mill",
        ),
        LeadTimeComponent(
            dimension=LeadTimeDimension.GRADE, attribute_value=grade_type, months=0.0
        ),
        LeadTimeComponent(
            dimension=LeadTimeDimension.CONNECTION,
            attribute_value=ANY_ATTRIBUTE_VALUE,
            months=0.0,
        ),
        LeadTimeComponent(
            dimension=LeadTimeDimension.LOGISTICS,
            attribute_value=ANY_ATTRIBUTE_VALUE,
            months=SAILING_MONTHS,
            label="Sailing",
        ),
    ])
    db_session.flush()


def _well(db_session, node, name, demand_status=DemandStatus.CONFIRMED):
    """A well, WITH its demand status stated.

    Demand status is a property of the WELL now
    (`app.models.well.Well.demand_status`), so it is a fixture-construction argument
    here rather than something set per line. A test that wants an out-of-scope LINE
    has to give it an out-of-scope WELL -- which is the point: one well cannot hold
    two statuses.
    """
    well = Well(planning_node_id=node.id, name=name, demand_status=demand_status)
    db_session.add(well)
    db_session.flush()
    return well


def _line(db_session, well, product, quantity, days_out):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity,
        ros_date=datetime.utcnow() + timedelta(days=days_out),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(line)
    db_session.flush()
    return line


def test_lead_time_summed_from_components_drives_order_date(db_session):
    _customer(db_session)
    product = _product(db_session)
    _lead_times(db_session)
    ros = datetime.utcnow() + timedelta(days=500)

    ship_by, order_by, lead_months = order_feasibility(db_session, product, ros)

    assert lead_months == TOTAL_MONTHS
    # required_ship_date = ROS minus the Sailing/Shipping allowance only.
    assert ship_by == ros.date() - months_to_timedelta(SAILING_MONTHS)
    # recommended_order_date = ROS minus the FULL lead time. The full lead time
    # already contains the sailing leg, so it is subtracted from the ROS date and
    # NOT from the ship date -- the previous `ship_by - TOTAL_MONTHS` form
    # double-subtracted sailing (see app.engines.order_dates).
    assert order_by == ros.date() - months_to_timedelta(TOTAL_MONTHS)
    # Internal consistency: order, spend (total - transit) at the mill, sail for
    # transit, arrive exactly at ROS.
    assert ship_by == order_by + months_to_timedelta(
        TOTAL_MONTHS
    ) - months_to_timedelta(SAILING_MONTHS)


def test_past_order_date_resolves_unrecoverable(db_session):
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    _lead_times(db_session)
    well = _well(db_session, node, "Well Late-01")
    # ROS 30 days out but 6.5 months of lead time -> cannot be met even today.
    line = _line(db_session, well, product, quantity=1000, days_out=30)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.UNRECOVERABLE
    assert "cannot be met even if ordered today" in line.coverage_result.reason
    # Rollup unchanged: unrecoverable still means the well is not covered.
    assert well.coverage_status == CoverageStatus.UNCOVERED.value

    rows = mrp_summary(db_session)
    assert len(rows) == 1
    assert rows[0].unrecoverable is True
    assert rows[0].recommended_order_date < date.today()


def test_no_lead_time_components_never_unrecoverable(db_session):
    """Explicit guard: a grade_type with no components has total lead time 0,
    which means 'not modelled', not 'hopeless'."""
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    well = _well(db_session, node, "Well NoLT-01")
    line = _line(db_session, well, product, quantity=1000, days_out=10)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.UNCOVERED
    row = mrp_summary(db_session)[0]
    assert row.lead_time_months == 0
    assert row.unrecoverable is False
    assert "no lead-time components configured" in row.reason


def test_far_out_ros_is_recoverable_recommendation(db_session):
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    _lead_times(db_session)
    well = _well(db_session, node, "Well Early-01")
    _line(db_session, well, product, quantity=4000, days_out=500)

    recompute_well(db_session, well)

    row = mrp_summary(db_session)[0]
    assert row.unrecoverable is False
    assert row.quantity == 4000
    assert row.lead_time_months == TOTAL_MONTHS
    assert row.recommended_order_date > date.today()


def test_covered_line_produces_no_recommendation(db_session):
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=10000)
    _lead_times(db_session)
    well = _well(db_session, node, "Well Full-01")
    line = _line(db_session, well, product, quantity=5000, days_out=500)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.COVERED
    assert mrp_summary(db_session) == []
    assert by_item(db_session, product.id).recommendation is None


def test_covered_via_substitute_produces_no_recommendation(db_session):
    customer, node = _customer(db_session)
    primary = _product(db_session, on_hand_qty=0, grade="13CR80")
    substitute = _product(db_session, on_hand_qty=9000, grade="13CR110")
    _lead_times(db_session)
    well = _well(db_session, node, "Well Sub-01")
    line = _line(db_session, well, primary, quantity=4000, days_out=500)

    db_session.add(TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id))
    db_session.add(
        CustomerSubstitutionRule(
            customer_id=customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db_session.flush()
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert mrp_summary(db_session) == []


def test_pending_approval_produces_no_recommendation(db_session):
    """Mill ordering is the LAST resort: while the customer still has an open
    approval decision, the engine must not pre-empt it with an order."""
    customer, node = _customer(db_session)
    primary = _product(db_session, on_hand_qty=0, grade="13CR80")
    substitute = _product(db_session, on_hand_qty=9000, grade="13CR110")
    _lead_times(db_session)
    well = _well(db_session, node, "Well Pend-01")
    line = _line(db_session, well, primary, quantity=4000, days_out=500)

    db_session.add(TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id))
    db_session.add(
        CustomerSubstitutionRule(
            customer_id=customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db_session.flush()
    request_approval(db_session, line, primary.id, substitute.id)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.PENDING_APPROVAL
    assert mrp_summary(db_session) == []


def test_runout_series_decreases_and_identifies_runout_month(db_session):
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=6000)
    _lead_times(db_session)
    well_a = _well(db_session, node, "Well Run-01")
    well_b = _well(db_session, node, "Well Run-02")
    # 4000 next month, then 5000 three months out -> crosses zero on the second.
    _line(db_session, well_a, product, quantity=4000, days_out=35)
    _line(db_session, well_b, product, quantity=5000, days_out=100)

    recompute_well(db_session, well_a)
    recompute_well(db_session, well_b)

    analysis = by_item(db_session, product.id)
    balances = [p.closing_balance for p in analysis.runout]
    # Monotonically non-increasing, and it ends below zero.
    assert all(b <= a for a, b in zip(balances, balances[1:]))
    assert balances[-1] < 0

    consuming = [p for p in analysis.runout if p.demand > 0]
    assert len(consuming) == 2
    assert consuming[0].closing_balance == 2000
    assert consuming[1].closing_balance == -3000
    assert analysis.runout_month == consuming[1].month
    # Tail months keep reporting a flat balance after runout.
    assert analysis.runout[-1].month > analysis.runout_month

    # Defect 1 regression -- this test used to assert the negative runout while
    # BOTH wells reported Covered and mrp_summary() returned [], i.e. it pinned
    # the runout half of a self-contradiction instead of catching it. The two
    # views must now agree: 6000 on hand cannot cover 4000 + 5000, so the
    # second well is short and that shortfall is visible in coverage AND in MRP.
    line_a, line_b = well_a.demand_lines[0], well_b.demand_lines[0]
    assert line_a.coverage_result.status == CoverageStatus.COVERED
    assert well_a.coverage_status == CoverageStatus.COVERED.value
    assert line_b.coverage_result.status in (
        CoverageStatus.UNCOVERED,
        CoverageStatus.UNRECOVERABLE,
    )
    assert well_b.coverage_status == CoverageStatus.UNCOVERED.value

    rows = mrp_summary(db_session)
    assert [r.quantity for r in rows] == [5000]
    assert rows[0].demand_line_ids == [line_b.id]


def test_runout_month_none_when_stock_covers_all_demand(db_session):
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=10000)
    well = _well(db_session, node, "Well Healthy-01")
    _line(db_session, well, product, quantity=4000, days_out=35)

    recompute_well(db_session, well)

    analysis = by_item(db_session, product.id)
    assert analysis.runout_month is None
    assert analysis.runout[-1].closing_balance == 6000


def test_by_item_lists_all_consuming_wells(db_session):
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=1000)
    _lead_times(db_session)
    wells = [_well(db_session, node, f"Well Item-{i:02d}") for i in range(1, 4)]
    for i, well in enumerate(wells):
        _line(db_session, well, product, quantity=2000, days_out=400 + i * 10)
        recompute_well(db_session, well)

    analysis = by_item(db_session, product.id)

    assert [l.well_name for l in analysis.demand_lines] == [w.name for w in wells]
    assert all(l.coverage_status is not None for l in analysis.demand_lines)
    # Inventory position: Oracle-owned fields are explicitly not integrated.
    assert analysis.inventory.on_hand == 1000
    assert analysis.inventory.assigned == 0
    # REPURPOSED, not relaxed. This line used to assert `on_order == 0`, which
    # pinned a hardcoded placeholder standing in for "we do not know". There is now
    # a real projection behind the figure
    # (app.models.inventory_on_order.InventoryOnOrder) and this product has NO row
    # in it, so the honest answer is UNKNOWN -- None, and never 0. The assertion is
    # STRONGER than it was: it now pins the distinction between "no data" and
    # "measured zero" that a constant 0 made unrepresentable.
    # `test_on_order_distinguishes_nothing_on_order_from_no_data` pins the other side.
    assert analysis.inventory.on_order is None
    assert analysis.inventory.on_order_source == "unavailable"
    # And `oracle_integrated` still means "the Oracle feed is live", which it is not.
    assert analysis.inventory.oracle_integrated is False


def test_on_order_distinguishes_nothing_on_order_from_no_data(db_session):
    """The two states a bare float cannot tell apart, asserted side by side.

    An explicit zero-quantity `InventoryOnOrder` row is a MEASUREMENT -- "this
    Business Unit has nothing on order of this product" -- and must read as 0. The
    ABSENCE of any row is unknown and must read as None. If those ever collapse into
    each other the dashboard is back to reporting a fabricated zero, which is the
    whole reason on-order was left out of it for so long.
    """
    from app.models import BusinessUnit, InventoryOnOrder

    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=1000)
    bu = db_session.query(BusinessUnit).first()

    # No row at all -> unknown.
    assert by_item(db_session, product.id).inventory.on_order is None

    # An explicit zero -> measured, and it is 0.
    db_session.add(
        InventoryOnOrder(
            business_unit_id=bu.id,
            product_id=product.id,
            quantity=0,
            source_system="synthetic",
        )
    )
    db_session.flush()
    position = by_item(db_session, product.id).inventory
    assert position.on_order == 0.0
    assert position.on_order_source == "synthetic"
    # Provenance stays TWO flags: a seeded projection is not a live integration.
    assert position.oracle_integrated is False

    # A real quantity, with a promise date.
    db_session.add(
        InventoryOnOrder(
            business_unit_id=bu.id,
            product_id=product.id,
            quantity=2500,
            expected_arrival_date=datetime.utcnow() + timedelta(days=90),
            source_system="synthetic",
        )
    )
    db_session.flush()
    position = by_item(db_session, product.id).inventory
    assert position.on_order == 2500.0
    assert position.on_order_earliest_arrival is not None
    # The undated part is reported separately, not dated to today.
    assert position.on_order_undated == 0.0


def test_summary_aggregates_per_product_and_uses_earliest_ros(db_session):
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    _lead_times(db_session)
    well_a = _well(db_session, node, "Well Agg-01")
    well_b = _well(db_session, node, "Well Agg-02")
    early = _line(db_session, well_a, product, quantity=3000, days_out=400)
    _line(db_session, well_b, product, quantity=2000, days_out=500)
    recompute_well(db_session, well_a)
    recompute_well(db_session, well_b)

    rows = mrp_summary(db_session)

    assert len(rows) == 1
    assert rows[0].quantity == 5000
    assert rows[0].ros_date == early.ros_date
    assert len(rows[0].demand_line_ids) == 2


def test_recoverable_and_unrecoverable_demand_split_into_separate_rows(db_session):
    """An early unrecoverable line must not drag its product's still-orderable
    demand into the UNRECOVERABLE bucket."""
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    _lead_times(db_session)
    well_late = _well(db_session, node, "Well Split-Late")
    well_ok = _well(db_session, node, "Well Split-OK")
    _line(db_session, well_late, product, quantity=1000, days_out=30)
    _line(db_session, well_ok, product, quantity=4000, days_out=500)
    recompute_well(db_session, well_late)
    recompute_well(db_session, well_ok)

    rows = mrp_summary(db_session)

    assert len(rows) == 2
    # Sorted by order date: the unrecoverable one is the more urgent.
    assert rows[0].unrecoverable is True and rows[0].quantity == 1000
    assert rows[1].unrecoverable is False and rows[1].quantity == 4000

    analysis = by_item(db_session, product.id)
    assert len(analysis.recommendations) == 2
    # `recommendation` is the headline row: the most urgent one, i.e. the same
    # escalation row mrp_summary put first. Asserted field by field -- the old
    # `is rows[0] or ...quantity == 1000` form was near-tautological because the
    # two objects are built by separate calls and can never be identical.
    headline = analysis.recommendation
    assert headline is not None
    assert headline.quantity == 1000
    assert headline.unrecoverable is True
    assert headline.product_id == rows[0].product_id
    assert headline.ros_date == rows[0].ros_date
    assert headline.recommended_order_date == rows[0].recommended_order_date
    assert headline.demand_line_ids == rows[0].demand_line_ids
    # And it really is the earliest of the two rows it was chosen from.
    assert headline.recommended_order_date == min(
        r.recommended_order_date for r in analysis.recommendations
    )
    assert {r.quantity for r in analysis.recommendations} == {1000, 4000}


def test_summary_filters_by_customer(db_session):
    _c1, node1 = _customer(db_session, name="Cust One")
    _c2, node2 = _customer(db_session, name="Cust Two")
    product = _product(db_session, on_hand_qty=0)
    _lead_times(db_session)
    well_1 = _well(db_session, node1, "Well C1-01")
    well_2 = _well(db_session, node2, "Well C2-01")
    _line(db_session, well_1, product, quantity=1000, days_out=400)
    _line(db_session, well_2, product, quantity=7000, days_out=400)
    recompute_well(db_session, well_1)
    recompute_well(db_session, well_2)

    assert len(mrp_summary(db_session)) == 1
    assert mrp_summary(db_session, customer_id=node1.customer_id)[0].quantity == 1000
    assert mrp_summary(db_session, customer_id=node2.customer_id)[0].quantity == 7000


# --------------------------------------------------------------------------
# Defect 2 -- the grouping key and the verdict must come from ONE live
# evaluation, never from a stored status mixed with a live recomputation.
# --------------------------------------------------------------------------


def test_time_drift_does_not_drag_orderable_demand_into_the_unrecoverable_row(db_session):
    """Coverage is only recomputed on revisions and approval decisions, so a
    stored UNCOVERED status ages while the calendar moves.

    13CR lead time is 6.5 months. Line A's ROS is 9 months out and line B's is
    20 months out, so coverage today marks BOTH merely UNCOVERED. Evaluate MRP
    120 days later: A can no longer be met by a mill order, B comfortably can.

    The old grouping key read `result.status == UNRECOVERABLE` from the STORED
    row -- still UNCOVERED for both -- so A and B landed in one group, and the
    live `order_by < today` check then stamped the whole 5000 row unrecoverable.
    That reported B's 4000 tonnes of perfectly orderable demand as hopeless.
    """
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    _lead_times(db_session)
    well = _well(db_session, node, "Well Drift-01")
    line_a = _line(db_session, well, product, quantity=1000, days_out=274)   # ~9 mo
    line_b = _line(db_session, well, product, quantity=4000, days_out=609)   # ~20 mo

    recompute_well(db_session, well)

    # Both are merely UNCOVERED as of today -- neither stored row says
    # UNRECOVERABLE, which is exactly the drift condition.
    assert line_a.coverage_result.status == CoverageStatus.UNCOVERED
    assert line_b.coverage_result.status == CoverageStatus.UNCOVERED

    later = date.today() + timedelta(days=120)
    rows = mrp_summary(db_session, today=later)

    assert len(rows) == 2
    by_qty = {r.quantity: r for r in rows}
    assert set(by_qty) == {1000, 4000}
    # Each line lands in the bucket its OWN feasibility dictates.
    assert by_qty[1000].unrecoverable is True
    assert by_qty[1000].demand_line_ids == [line_a.id]
    assert by_qty[4000].unrecoverable is False
    assert by_qty[4000].demand_line_ids == [line_b.id]
    # The specific failure: no row may declare 5000 unrecoverable.
    assert not any(r.unrecoverable and r.quantity == 5000 for r in rows)
    # The row flag and the row's own reason text agree.
    assert "UNRECOVERABLE" in by_qty[1000].reason
    assert "UNRECOVERABLE" not in by_qty[4000].reason
    assert "order by" in by_qty[4000].reason


# --------------------------------------------------------------------------
# Defect 5 -- runout charges demand to the product that SATISFIED it
# --------------------------------------------------------------------------


def test_substituted_demand_is_charged_to_the_substitute_not_the_primary(db_session):
    """Defect 5, both halves at once.

    The 4000 was drawn from the SUBSTITUTE, so:
      * by_item(substitute) must show the draw -- otherwise its 9000 looks
        entirely free and a planner commits the same steel a second time;
      * by_item(primary) must NOT be charged for demand that is already covered
        -- otherwise its zero stock shows an immediate runout for nothing.
    """
    customer, node = _customer(db_session)
    primary = _product(db_session, on_hand_qty=0, grade="13CR80")
    substitute = _product(db_session, on_hand_qty=9000, grade="13CR110")
    _lead_times(db_session)
    well = _well(db_session, node, "Well Attr-01")
    line = _line(db_session, well, primary, quantity=4000, days_out=500)

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
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    # Coverage persisted WHICH product satisfied the line; MRP reads this rather
    # than re-deriving any substitution decision of its own.
    assert line.coverage_result.fulfilled_by_product_id == substitute.id

    sub_analysis = by_item(db_session, substitute.id)
    assert [l.demand_line_id for l in sub_analysis.demand_lines] == [line.id]

    # The listed line ordered the PRIMARY, not the product whose page this is.
    # Without these fields the page silently invites the wrong reading -- that
    # every row it lists ordered the page's own product -- and it is wrong exactly
    # on the substitute pages where a planner is deciding whether steel is free.
    listed = sub_analysis.demand_lines[0]
    assert listed.product_id == primary.id
    assert listed.product_id != substitute.id
    assert listed.product_description == primary.description

    consuming = [p for p in sub_analysis.runout if p.demand > 0]
    assert len(consuming) == 1
    assert consuming[0].demand == 4000
    # 9000 on hand less the 4000 actually drawn -- NOT a flat 9000.
    assert sub_analysis.runout[-1].closing_balance == 5000
    assert sub_analysis.runout_month is None

    primary_analysis = by_item(db_session, primary.id)
    assert primary_analysis.demand_lines == []
    assert all(p.demand == 0 for p in primary_analysis.runout)
    # No phantom runout against 0 on hand for demand that is already covered.
    assert primary_analysis.runout_month is None
    assert primary_analysis.recommendation is None
    assert mrp_summary(db_session) == []


def test_unresolved_demand_stays_charged_to_its_own_product(db_session):
    """The attribution fallback: PendingApproval / Uncovered / Unrecoverable have
    drawn nothing, so they remain an open commitment against their OWN product
    and must still push that product's runout negative."""
    customer, node = _customer(db_session)
    primary = _product(db_session, on_hand_qty=1000, grade="13CR80")
    substitute = _product(db_session, on_hand_qty=9000, grade="13CR110")
    _lead_times(db_session)
    well = _well(db_session, node, "Well Pend-Attr")
    line = _line(db_session, well, primary, quantity=4000, days_out=500)
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
    request_approval(db_session, line, primary.id, substitute.id)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.PENDING_APPROVAL
    assert line.coverage_result.fulfilled_by_product_id is None

    primary_analysis = by_item(db_session, primary.id)
    assert [l.demand_line_id for l in primary_analysis.demand_lines] == [line.id]
    assert primary_analysis.runout[-1].closing_balance == -3000

    # The substitute has NOT been drawn on -- the approval has not been granted.
    sub_analysis = by_item(db_session, substitute.id)
    assert sub_analysis.demand_lines == []
    assert sub_analysis.runout[-1].closing_balance == 9000


# --------------------------------------------------------------------------
# Defect 6 -- a line with no CoverageResult must not vanish
# --------------------------------------------------------------------------


def test_never_evaluated_line_surfaces_in_mrp_instead_of_vanishing(db_session):
    """A CONFIRMED/PRIMARY line on a well coverage has never run for used to be
    treated exactly like a covered one: no recommendation, no warning. Missing
    means 'not yet evaluated', never 'fine'."""
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    _lead_times(db_session)
    well = _well(db_session, node, "Well Unevaluated-01")
    line = _line(db_session, well, product, quantity=3000, days_out=500)

    # Deliberately NO recompute_well call.
    assert line.coverage_result is None

    rows = mrp_summary(db_session)

    assert len(rows) == 1
    assert rows[0].quantity == 3000
    assert rows[0].demand_line_ids == [line.id]
    assert rows[0].unrecoverable is False
    # And it fails LOUD -- the reason names the missing evaluation.
    assert "NO coverage result yet" in rows[0].reason

    analysis = by_item(db_session, product.id)
    assert analysis.recommendation is not None
    assert analysis.demand_lines[0].coverage_status is None


def test_evaluating_the_well_clears_the_never_evaluated_note(db_session):
    """Complement to the above: once coverage has actually run, the row stops
    claiming the line is unevaluated."""
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    _lead_times(db_session)
    well = _well(db_session, node, "Well Unevaluated-02")
    _line(db_session, well, product, quantity=3000, days_out=500)

    assert "NO coverage result yet" in mrp_summary(db_session)[0].reason

    recompute_well(db_session, well)

    row = mrp_summary(db_session)[0]
    assert row.quantity == 3000
    assert "NO coverage result yet" not in row.reason


def test_by_item_on_hand_is_the_sum_of_the_inventory_on_hand_rows(db_session):
    """MRP is system-wide, so its on-hand figure is the ALL-BU total.

    That is a deliberate choice, not a leftover of the dropped global column: both
    `mrp_summary(customer_id=None)` and `by_item` aggregate demand across every
    customer of every BU, so the honest supply counterpart is what the company
    holds in total. It is a procurement number and never a coverage number --
    coverage is resolved per BU by `on_hand_for` / `on_hand_map`, which is what the
    Business Unit tests pin.
    """
    from app.models import BusinessUnit, InventoryOnHand

    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=1000)  # row in the file's MRP BU
    _lead_times(db_session)

    other_bu = BusinessUnit(name="Second BU")
    db_session.add(other_bu)
    db_session.flush()
    db_session.add(
        InventoryOnHand(
            business_unit_id=other_bu.id, product_id=product.id, quantity=250,
            source_system="synthetic",
        )
    )
    db_session.flush()

    analysis = by_item(db_session, product.id)

    assert analysis.inventory.on_hand == 1250
    # And the runout curve opens from that same total, not from either BU alone.
    assert analysis.runout[0].opening_balance == 1250


def test_by_item_raises_when_the_product_has_no_inventory_row_anywhere(db_session):
    """No row in ANY Business Unit means the quantity is UNKNOWN.

    The old code read `Product.on_hand_qty`, which was NOT NULL with a default of
    0, so "nobody has told us" and "we hold none" were the same number and a
    runout curve opened confidently at zero. With the column gone, absence is
    absence: `by_item` refuses rather than drawing a chart from a value nobody
    supplied. The refusal happens BEFORE any other figure is computed, so no
    partially-real report escapes.
    """
    from app.engines.inventory import InventoryRowMissing
    from app.models import InventoryOnHand

    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    _lead_times(db_session)
    well = _well(db_session, node, "Well NoRow-01")
    _line(db_session, well, product, quantity=5000, days_out=400)

    # Remove the row the fixture wrote, leaving the product stocked NOWHERE.
    db_session.query(InventoryOnHand).filter(
        InventoryOnHand.product_id == product.id
    ).delete()
    db_session.flush()

    with pytest.raises(InventoryRowMissing) as excinfo:
        by_item(db_session, product.id)
    assert product.description in str(excinfo.value)
    assert "UNKNOWN" in str(excinfo.value)

    # An explicit 0 is a different thing entirely, and it is accepted.
    db_session.add(
        InventoryOnHand(
            business_unit_id=_default_bu(db_session).id, product_id=product.id,
            quantity=0, source_system="synthetic",
        )
    )
    db_session.flush()
    assert by_item(db_session, product.id).inventory.on_hand == 0


# ---------------------------------------------------------------------------
# Phase 3: runout-with-recommended-order series
# ---------------------------------------------------------------------------


def test_with_order_series_injects_recommended_qty_on_the_correct_arrival_date(
    db_session,
):
    """The recoverable recommendation's quantity lands exactly at
    recommended_order_date + lead time, and only on the SECOND series."""
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=1000)
    _lead_times(db_session)
    well = _well(db_session, node, "Well Order-Series")
    line = _line(db_session, well, product, quantity=5000, days_out=400)
    recompute_well(db_session, well)

    analysis = by_item(db_session, product.id)
    (rec,) = analysis.recommendations
    assert rec.product_id == product.id
    assert rec.unrecoverable is False

    arrival = rec.recommended_order_date + months_to_timedelta(rec.lead_time_months)
    arrival_key = f"{arrival.year:04d}-{arrival.month:02d}"

    with_order_point = next(
        p for p in analysis.runout_with_recommended_order if p.month == arrival_key
    )
    # The BASELINE series never saw this injection -- additive, not merged.
    # The injected quantity is exactly the gap between the two series'
    # OPENING balances in the arrival month (the incoming supply is added to
    # the opening balance before that month's demand is subtracted -- see
    # `app.engines.mrp._runout_series`).
    baseline_point = next(p for p in analysis.runout if p.month == arrival_key)
    assert with_order_point.opening_balance - baseline_point.opening_balance == (
        pytest.approx(rec.quantity)
    )

    # And the shortfall the baseline shows is resolved (or improved) once the
    # order lands: the "with order" balance at/after arrival must never be
    # worse than the baseline's.
    for a, b in zip(analysis.runout, analysis.runout_with_recommended_order):
        assert b.closing_balance >= a.closing_balance - 1e-6


def test_with_order_series_is_additive_to_real_on_order_baseline(db_session):
    """The hypothetical series and the baseline both start from the SAME opening
    position (on-hand + customer-owned); the injected quantity is layered on top
    of whatever the baseline already does, never replacing it.

    `_runout_series` does not fold real `InventoryOnOrder` rows into either
    series today (a documented pre-existing gap -- see `app.engines.mrp.by_item`).
    This test pins that both series still open from the identical starting
    balance, so a future fix that adds real on-order to the baseline only has to
    touch one function and both series inherit it consistently.
    """
    from app.models import BusinessUnit, InventoryOnOrder

    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=1000)
    _lead_times(db_session)
    well = _well(db_session, node, "Well Order-Series-Additive")
    _line(db_session, well, product, quantity=5000, days_out=400)
    recompute_well(db_session, well)

    bu = db_session.query(BusinessUnit).first()
    db_session.add(
        InventoryOnOrder(
            business_unit_id=bu.id, product_id=product.id, quantity=200,
            source_system="synthetic",
        )
    )
    db_session.flush()

    analysis = by_item(db_session, product.id)
    # Real on-order is reported separately (InventoryPosition.on_order) and is
    # NOT folded into either runout series's opening balance.
    assert analysis.inventory.on_order == 200.0
    assert analysis.runout[0].opening_balance == analysis.inventory.on_hand
    assert (
        analysis.runout_with_recommended_order[0].opening_balance
        == analysis.inventory.on_hand
    )


def test_with_order_series_uses_only_the_recoverable_portion(db_session):
    """An UNRECOVERABLE recommendation must NOT be injected as incoming supply.

    Injecting it would show steel arriving to save a shortfall the engine has
    already determined a mill order cannot save even if placed today -- a less
    honest picture than simply leaving it as unmet demand on both series.
    """
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=0)
    _lead_times(db_session)
    well = _well(db_session, node, "Well Unrecoverable-Order")
    # ROS inside the lead time window -> UNRECOVERABLE.
    _line(db_session, well, product, quantity=3000, days_out=30)
    recompute_well(db_session, well)

    analysis = by_item(db_session, product.id)
    (rec,) = analysis.recommendations
    assert rec.unrecoverable is True

    # Nothing injected: the two series must be identical.
    assert [p.closing_balance for p in analysis.runout] == [
        p.closing_balance for p in analysis.runout_with_recommended_order
    ]
    assert analysis.runout_month == analysis.runout_month_with_recommended_order


def test_with_order_runout_month_out_carries_a_unit(db_session):
    """Every point of the new series is labelled -- the units guard's rule."""
    _customer_, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=1000)
    _lead_times(db_session)
    well = _well(db_session, node, "Well Order-Series-Units")
    _line(db_session, well, product, quantity=5000, days_out=400)
    recompute_well(db_session, well)

    analysis = by_item(db_session, product.id)
    assert analysis.runout_with_recommended_order, "no points to label"
    assert {
        p.unit_of_measure for p in analysis.runout_with_recommended_order
    } == {UnitOfMeasure.MTR}


# ---------------------------------------------------------------------------
# 2026-08-11 By Item enrichment: profile split and ownership-split balances
# ---------------------------------------------------------------------------


def test_runout_points_split_demand_by_profile_and_balance_by_ownership():
    from datetime import date, datetime, timedelta

    from app.engines.mrp import _runout_series
    from app.models import DemandLine, DemandProfile

    today = date(2026, 8, 15)

    class _L:
        def __init__(self, qty, days, profile):
            self.quantity = qty
            self.ros_date = datetime(2026, 8, 15) + timedelta(days=days)
            self.profile = profile

    lines = [
        _L(300, 10, DemandProfile.PRIMARY),
        _L(200, 12, DemandProfile.CONTINGENCY),
        _L(400, 45, DemandProfile.PRIMARY),
    ]
    series, runout = _runout_series(
        900, lines, today=today, customer_owned_opening=350
    )
    first, second = series[0], series[1]
    # Profile split partitions the month's demand exactly.
    assert first.demand == 500
    assert first.demand_primary == 300 and first.demand_contingency == 200
    # Ownership: the 350 customer-owned depletes first (500 drawn in month 1).
    assert first.closing_customer_owned == 0
    assert first.closing_company == first.closing_balance == 400
    # Month 2: all remaining draw is company steel; the two tiers always sum
    # to the closing balance while it is non-negative.
    assert second.closing_customer_owned + second.closing_company == second.closing_balance
    assert runout is None


def test_with_order_series_keeps_the_customer_owned_split(db_session):
    """The with-order curve opens with the SAME ownership split as the baseline.

    REGRESSION (2026-08-12 decision): `runout_with_recommended_order` was built
    without `customer_owned_opening`, so its whole opening balance was reported
    as company steel and its customer/company decomposition contradicted the
    baseline curve's in the same payload.
    """
    from app.models import CustomerOwnedInventory

    customer, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=1000)
    _lead_times(db_session)
    db_session.add(
        CustomerOwnedInventory(
            customer_id=customer.id,
            product_id=product.id,
            quantity=400,
            source_system="customer-upload",
        )
    )
    well = _well(db_session, node, "WELL-SPLIT")
    _line(db_session, well, product, quantity=2000, days_out=200)
    recompute_well(db_session, well)
    db_session.commit()

    analysis = by_item(db_session, product.id)
    base_first = analysis.runout[0]
    with_first = analysis.runout_with_recommended_order[0]
    # Baseline split holds by contract; the with-order series must agree in
    # its first month (any injected supply is company steel, never customer).
    assert base_first.closing_customer_owned == with_first.closing_customer_owned
    for point in analysis.runout_with_recommended_order:
        if point.closing_balance >= 0:
            assert point.closing_customer_owned + point.closing_company == (
                point.closing_balance
            )


def test_ledger_splits_on_order_from_recommended_and_carries_openings(db_session):
    """The monthly ledger (2026-08-12 rework): full plan, honest columns.

    - opening balance == previous month's closing balance (plus that month's
      arrivals), with the ownership split carried over;
    - REAL on-order arrivals and the RECOMMENDED order's arrival land in
      separate incoming columns -- a promise is not a suggestion;
    - undated on-order quantity is reported beside the ledger, never netted.
    """
    from app.models import BusinessUnit, CustomerOwnedInventory, InventoryOnOrder

    customer, node = _customer(db_session)
    product = _product(db_session, on_hand_qty=1000)
    _lead_times(db_session)
    db_session.add(
        CustomerOwnedInventory(
            customer_id=customer.id,
            product_id=product.id,
            quantity=300,
            source_system="customer-upload",
        )
    )
    bu = db_session.query(BusinessUnit).first()
    today = date.today()
    # A dated PO landing in month 2 of the walk, and an undated one.
    from app.engines.mrp import _next_month as nm
    y2, m2 = nm(today.year, today.month)
    db_session.add(
        InventoryOnOrder(
            business_unit_id=bu.id, product_id=product.id, quantity=400,
            expected_arrival_date=date(y2, m2, 15), source_system="synthetic",
        )
    )
    db_session.add(
        InventoryOnOrder(
            business_unit_id=bu.id, product_id=product.id, quantity=250,
            expected_arrival_date=None, source_system="synthetic",
        )
    )
    well = _well(db_session, node, "WELL-LEDGER")
    _line(db_session, well, product, quantity=2000, days_out=200)
    recompute_well(db_session, well)
    db_session.commit()

    analysis = by_item(db_session, product.id)
    assert analysis.ledger, "ledger series must exist"
    assert analysis.ledger_undated_on_order == 250

    month2_key = f"{y2:04d}-{m2:02d}"
    p2 = next(p for p in analysis.ledger if p.month == month2_key)
    assert p2.incoming_on_order == 400
    # Openings carry: each month's opening == previous closing + this month's
    # incoming, and the customer tier never grows from incoming supply.
    for prev, cur in zip(analysis.ledger, analysis.ledger[1:]):
        assert cur.opening_balance == pytest.approx(
            prev.closing_balance + cur.incoming_on_order + cur.incoming_recommended
        )
        assert cur.opening_customer_owned == pytest.approx(
            prev.closing_customer_owned
        )
    # The recommendation (2000 demanded vs 1300 held + 400 on order) must land
    # in the recommended column somewhere, never in on_order.
    assert any(p.incoming_recommended > 0 for p in analysis.ledger)
    total_on_order_in_ledger = sum(p.incoming_on_order for p in analysis.ledger)
    assert total_on_order_in_ledger == 400
