"""Attribute-based lead time: the dimensions, the shared component, the
breakdown, and the incomplete-model contract.

What this file pins, and why each one is here
---------------------------------------------
THE DEFECT. Lead time used to be keyed by `grade_type` + a free-text component
name and summed over `grade_type` alone, so two products in the same grade family
but of different size, weight or connection were GUARANTEED the same lead time --
the size and connection dimensions were unrepresentable, not merely unpopulated.
`order_feasibility` therefore returned the same recommended order date for a
4-1/2 TBG and a 13-3/8 CSG, and because the same total feeds
`is_recoverable`, it could also produce the terminal UNRECOVERABLE verdict from a
lead time that was never about that product. The first two tests below make that
impossible to reintroduce.

THE SHARED COMPONENT. A Logistics row that applies to everything must be storable
ONCE. Under the old shape it had to be duplicated per grade_type, and a single
row with a sentinel grade_type would have been found by nothing -- both
`total_lead_time_months` and `transit_months` would have returned 0 for it and
`required_ship_date` would have collapsed to `== ROS`.

ONE RESOLVER. `transit_months` and `total_lead_time_months` must agree by
construction, because a drifted transit figure desynchronises
`required_ship_date` from `recommended_order_date`.

INCOMPLETE MEANS NOT MODELLED. Whether a partial component set is summed is a
decision, not an accident, so it gets a test that names it.
"""

from datetime import date, datetime, timedelta

import pytest

from app.engines.coverage import recompute_well
from app.engines.lead_time import (
    AmbiguousLeadTimeComponent,
    REQUIRED_DIMENSIONS,
    od_wt_key,
    resolve_lead_time,
    total_lead_time_months,
)
from app.engines.mrp import mrp_summary
from app.engines.order_dates import (
    is_recoverable,
    months_to_timedelta,
    order_feasibility,
    transit_months,
)
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    CoverageStatus,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    LeadTimeComponent,
    LeadTimeDimension,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
    ANY_ATTRIBUTE_VALUE,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _bu(db, name="LT BU"):
    bu = db.query(BusinessUnit).filter(BusinessUnit.name == name).one_or_none()
    if bu is None:
        bu = BusinessUnit(name=name)
        db.add(bu)
        db.flush()
    return bu


def _customer(db, name="LT Co"):
    customer = Customer(
        name=name, allocation_policy=AllocationPolicy.SOFT, business_unit_id=_bu(db).id
    )
    db.add(customer)
    db.flush()
    node = PlanningNode(customer_id=customer.id, node_type="Campaign", name="LT Campaign")
    db.add(node)
    db.flush()
    return customer, node


def _product(
    db,
    size="4-1/2",
    weight=12.6,
    grade_type="13CR",
    connection="VAM TOP",
    on_hand_qty=0.0,
    type_="TBG",
):
    product = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type=type_, size=size, weight=weight, grade=f"{grade_type}80",
        grade_type=grade_type, connection=connection,
        description=f"{type_} {size} {weight} {grade_type} {connection}",
    )
    db.add(product)
    db.flush()
    db.add(
        InventoryOnHand(
            business_unit_id=_bu(db).id, product_id=product.id,
            quantity=on_hand_qty, source_system="synthetic",
        )
    )
    db.flush()
    return product


def _component(db, dimension, attribute_value, months, label=None):
    row = LeadTimeComponent(
        dimension=dimension, attribute_value=attribute_value, months=months, label=label
    )
    db.add(row)
    db.flush()
    return row


def _complete_set(db, od_wt_months=3.0, grade_months=1.0, conn_months=0.5,
                  logistics_months=2.0, grade_type="13CR"):
    """Wildcard OD/WT + Connection, one Grade row, one shared Logistics row.

    Deliberately wildcard where a test is not varying that dimension, so the
    dimension under test is the ONLY difference between two products.
    """
    _component(db, LeadTimeDimension.OD_WT, ANY_ATTRIBUTE_VALUE, od_wt_months, "Ex-mill")
    _component(db, LeadTimeDimension.GRADE, grade_type, grade_months)
    _component(db, LeadTimeDimension.CONNECTION, ANY_ATTRIBUTE_VALUE, conn_months)
    _component(
        db, LeadTimeDimension.LOGISTICS, ANY_ATTRIBUTE_VALUE, logistics_months, "Sailing"
    )


def _line(db, well, product, quantity, days_out):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity,
        ros_date=datetime.utcnow() + timedelta(days=days_out),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.flush()
    return line


# --------------------------------------------------------------------------
# THE DEFECT: lead time must vary by OD/WT and by connection
# --------------------------------------------------------------------------


def test_same_grade_different_od_wt_gives_different_totals(db_session):
    """THE regression. Same grade family, same connection, different OD/WT.

    Under the old grade_type-keyed model these two products were guaranteed the
    same total and therefore the same recommended order date. A specific OD/WT row
    now beats the wildcard for the 13-3/8, and only the OD/WT term differs.
    """
    _complete_set(db_session)
    _component(db_session, LeadTimeDimension.OD_WT, "13-3/8 68", 6.0)

    small = _product(db_session, size="4-1/2", weight=12.6)
    large = _product(db_session, size="13-3/8", weight=68.0, type_="CSG")

    small_total = total_lead_time_months(db_session, small)
    large_total = total_lead_time_months(db_session, large)

    # 3.0 wildcard OD/WT + 1.0 grade + 0.5 connection + 2.0 logistics
    assert small_total == 6.5
    # 6.0 specific OD/WT + the same 3.5 of everything else
    assert large_total == 9.5
    assert small_total != large_total

    # And the DATE moves with it, which is the part that reaches a planner.
    ros = datetime.utcnow() + timedelta(days=600)
    _ship_s, order_small, _m = order_feasibility(db_session, small, ros)
    _ship_l, order_large, _m = order_feasibility(db_session, large, ros)
    assert order_large < order_small
    assert order_small - order_large == months_to_timedelta(9.5) - months_to_timedelta(6.5)


def test_od_wt_distinguishes_weight_not_only_size(db_session):
    """Wall thickness is half of "OD/WT" and must not be silently ignored.

    Two products of identical size, grade and connection, differing ONLY in
    weight. Keying OD/WT on `size` alone would collapse these two.
    """
    _complete_set(db_session, grade_type="Carbon")
    _component(db_session, LeadTimeDimension.OD_WT, "2-7/8 7.9", 3.5)

    thin = _product(db_session, size="2-7/8", weight=6.5, grade_type="Carbon")
    heavy = _product(db_session, size="2-7/8", weight=7.9, grade_type="Carbon")

    assert od_wt_key(thin) == "2-7/8 6.5"
    assert od_wt_key(heavy) == "2-7/8 7.9"
    assert total_lead_time_months(db_session, thin) == 6.5   # wildcard 3.0
    assert total_lead_time_months(db_session, heavy) == 7.0  # specific 3.5


def test_same_grade_different_connection_gives_different_totals(db_session):
    """Connection is a dimension of its own -- the spec lists it explicitly (at
    +0 months in the worked example, which is a VALUE, not an absence)."""
    _component(db_session, LeadTimeDimension.OD_WT, ANY_ATTRIBUTE_VALUE, 3.0)
    _component(db_session, LeadTimeDimension.GRADE, "Carbon", 0.5)
    _component(db_session, LeadTimeDimension.CONNECTION, "VAM TOP", 0.5)
    _component(db_session, LeadTimeDimension.CONNECTION, "VAM 21", 1.0)
    _component(db_session, LeadTimeDimension.LOGISTICS, ANY_ATTRIBUTE_VALUE, 2.0)

    vam_top = _product(db_session, grade_type="Carbon", connection="VAM TOP")
    vam_21 = _product(db_session, grade_type="Carbon", connection="VAM 21")

    assert total_lead_time_months(db_session, vam_top) == 6.0
    assert total_lead_time_months(db_session, vam_21) == 6.5


def test_zero_month_connection_is_a_value_not_an_absence(db_session):
    """The spec's own example has Connection at +0. A 0-month row must count as
    MODELLED -- it is a deliberate "this adds nothing" -- while a MISSING row is
    not modelled at all. Presence, never value, decides."""
    _complete_set(db_session, conn_months=0.0)
    product = _product(db_session)

    breakdown = resolve_lead_time(db_session, product)

    assert breakdown.modelled is True
    assert breakdown.total_months == 6.0
    assert any(c.dimension == "Connection" and c.months == 0.0 for c in breakdown.components)


# --------------------------------------------------------------------------
# The shared logistics component: stored ONCE, found by BOTH consumers
# --------------------------------------------------------------------------


def test_shared_logistics_component_is_stored_once_and_found_for_every_product(db_session):
    """ONE Logistics row, three products of different grade/size/connection.

    This is the minor finding: the old code filtered on `grade_type ==
    product.grade_type`, so a single shared sailing row could not be found at all
    and both the total and the transit leg would have silently lost it.
    """
    _component(db_session, LeadTimeDimension.OD_WT, ANY_ATTRIBUTE_VALUE, 3.0)
    _component(db_session, LeadTimeDimension.GRADE, "13CR", 1.0)
    _component(db_session, LeadTimeDimension.GRADE, "Carbon", 0.5)
    _component(db_session, LeadTimeDimension.CONNECTION, ANY_ATTRIBUTE_VALUE, 0.5)
    # Exactly one logistics row for the whole catalogue.
    _component(db_session, LeadTimeDimension.LOGISTICS, ANY_ATTRIBUTE_VALUE, 2.0, "Sailing")

    assert (
        db_session.query(LeadTimeComponent)
        .filter(LeadTimeComponent.dimension == LeadTimeDimension.LOGISTICS)
        .count()
        == 1
    )

    products = [
        _product(db_session, size="4-1/2", weight=12.6, grade_type="13CR"),
        _product(db_session, size="9-5/8", weight=53.5, grade_type="Carbon",
                 connection="VAM 21", type_="CSG"),
        _product(db_session, size="13-3/8", weight=68.0, grade_type="Carbon",
                 connection="VAM 21", type_="CSG"),
    ]

    for product in products:
        breakdown = resolve_lead_time(db_session, product)
        assert breakdown.modelled is True
        logistics = [c for c in breakdown.components if c.dimension == "Logistics"]
        assert len(logistics) == 1
        assert logistics[0].months == 2.0
        # It reports itself as shared, so the UI can say so rather than implying
        # a per-product figure.
        assert logistics[0].shared is True
        assert logistics[0].attribute_value == ANY_ATTRIBUTE_VALUE

        # BOTH consumers see it, and they agree.
        assert transit_months(db_session, product) == 2.0
        assert breakdown.transit_months == 2.0
        assert total_lead_time_months(db_session, product) >= 2.0


def test_transit_and_total_agree_and_the_two_dates_stay_consistent(db_session):
    """One resolver, two views. `required_ship_date` and
    `recommended_order_date` must be derivable from each other, i.e. order, spend
    (total - transit) at the mill, sail for transit, arrive exactly at ROS."""
    _complete_set(db_session, logistics_months=2.0)
    product = _product(db_session)
    ros = datetime.utcnow() + timedelta(days=600)

    ship_by, order_by, lead_months = order_feasibility(db_session, product, ros)

    assert transit_months(db_session, product) == 2.0
    assert lead_months == total_lead_time_months(db_session, product) == 6.5
    assert ship_by == ros.date() - months_to_timedelta(2.0)
    assert order_by == ros.date() - months_to_timedelta(6.5)
    assert ship_by == order_by + months_to_timedelta(6.5) - months_to_timedelta(2.0)


def test_a_specific_logistics_row_overrides_the_shared_one(db_session):
    """Most-specific-wins, so a global default plus exceptions is expressible --
    and the exception is what makes the wildcard safe to rely on."""
    _complete_set(db_session, logistics_months=2.0)
    _component(db_session, LeadTimeDimension.LOGISTICS, "13CR", 4.0, "Sailing (far mill)")

    product = _product(db_session, grade_type="13CR")

    assert transit_months(db_session, product) == 4.0
    assert total_lead_time_months(db_session, product) == 8.5
    logistics = [
        c for c in resolve_lead_time(db_session, product).components
        if c.dimension == "Logistics"
    ]
    assert logistics[0].shared is False
    assert logistics[0].attribute_value == "13CR"


# --------------------------------------------------------------------------
# The breakdown
# --------------------------------------------------------------------------


def test_breakdown_sums_exactly_to_the_total(db_session):
    """Transparency is the stated reason planners trust this model, so the
    published breakdown has to BE the arithmetic, not a decoration beside it."""
    _complete_set(db_session, od_wt_months=4.0, grade_months=2.0, conn_months=0.0,
                  logistics_months=2.0)
    product = _product(db_session)

    breakdown = resolve_lead_time(db_session, product)

    assert breakdown.total_months == 8.0  # the spec's worked example
    assert sum(c.months for c in breakdown.components) == breakdown.total_months
    assert [c.dimension for c in breakdown.components] == [
        d.value for d in REQUIRED_DIMENSIONS
    ]
    # Every term says which attribute value it matched on, so the row is
    # explainable without a second lookup.
    by_dimension = {c.dimension: c for c in breakdown.components}
    assert by_dimension["OD/WT"].matched_on == "4-1/2 12.6"
    assert by_dimension["Grade"].matched_on == "13CR"
    assert by_dimension["Connection"].matched_on == "VAM TOP"
    assert breakdown.missing_dimensions == ()
    assert breakdown.product_id == product.id


def test_mrp_row_carries_the_breakdown_that_explains_its_order_date(db_session):
    _customer_, node = _customer(db_session)
    _complete_set(db_session)
    product = _product(db_session, on_hand_qty=0)
    well = Well(planning_node_id=node.id, name="Well LT-Breakdown", demand_status=DemandStatus.CONFIRMED)
    db_session.add(well)
    db_session.flush()
    _line(db_session, well, product, quantity=1000, days_out=600)
    recompute_well(db_session, well)

    row = mrp_summary(db_session)[0]

    assert row.lead_time is not None
    assert row.lead_time.total_months == row.lead_time_months == 6.5
    assert sum(c.months for c in row.lead_time.components) == row.lead_time_months
    assert row.recommended_order_date == row.ros_date.date() - months_to_timedelta(6.5)


def test_by_item_exposes_the_breakdown_even_with_no_recommendation(db_session):
    """A fully covered product still has a lead time, and "how long to order
    more" is exactly the question a planner asks about covered stock."""
    from app.engines.mrp import by_item

    _customer_, node = _customer(db_session)
    _complete_set(db_session)
    product = _product(db_session, on_hand_qty=10000)
    well = Well(planning_node_id=node.id, name="Well LT-Covered", demand_status=DemandStatus.CONFIRMED)
    db_session.add(well)
    db_session.flush()
    _line(db_session, well, product, quantity=1000, days_out=600)
    recompute_well(db_session, well)

    analysis = by_item(db_session, product.id)

    assert analysis.recommendation is None
    assert analysis.lead_time is not None
    assert analysis.lead_time.total_months == 6.5


def test_lead_time_endpoint_answers_for_a_product_with_no_inventory_row(db_session):
    """The route exists precisely because `by_item` refuses (correctly) when a
    product is stocked nowhere -- the lead time is knowable regardless."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    _complete_set(db_session)
    product = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="CSG", size="9-5/8", weight=53.5, grade="P110", grade_type="13CR",
        connection="VAM TOP", description="CSG 9-5/8 53.5 P110 VAM TOP",
    )
    db_session.add(product)
    db_session.flush()  # NO InventoryOnHand row anywhere.

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        client = TestClient(app)
        response = client.get(f"/mrp/lead-time/{product.id}")
        assert response.status_code == 200
        body = response.json()
        assert body["modelled"] is True
        assert body["total_months"] == 6.5
        assert body["transit_months"] == 2.0
        assert sum(c["months"] for c in body["components"]) == 6.5
        assert client.get("/mrp/lead-time/nope").status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


# --------------------------------------------------------------------------
# Not modelled: nothing at all, and the PARTIAL-match decision
# --------------------------------------------------------------------------


def test_no_components_at_all_is_zero_and_never_unrecoverable(db_session):
    """The existing contract (pinned by tests/test_mrp_engine.py too): total 0
    means "not modelled", NOT "instant"."""
    product = _product(db_session, on_hand_qty=0)
    ros = datetime.utcnow() + timedelta(days=1)

    breakdown = resolve_lead_time(db_session, product)

    assert breakdown.total_months == 0.0
    assert breakdown.modelled is False
    assert breakdown.components == ()
    assert set(breakdown.missing_dimensions) == {d.value for d in REQUIRED_DIMENSIONS}
    assert "NOT MODELLED" in breakdown.note
    assert is_recoverable(db_session, product, ros) is True


def test_a_partial_component_set_is_not_modelled_and_is_never_partially_summed(db_session):
    """DECISION: an INCOMPLETE component set is reported as NOT MODELLED (total
    0), exactly like no components at all. It is NEVER partially summed.

    Grade (+2) and Logistics (+2) are configured here; OD/WT and Connection are
    not. Summing what matched would publish a 4-month lead time for a product
    whose real lead time is unknown and certainly longer -- a confidently wrong
    `recommended_order_date`, and a confidently wrong `is_recoverable` verdict
    computed from months that are known to be missing. Admitting the model is
    incomplete is recoverable; a wrong date that looks right is not.

    The matched months are still REPORTED (`matched_months`, and each component),
    because naming what is configured and what is missing is what makes the gap
    fixable -- but they are deliberately not the total.
    """
    _component(db_session, LeadTimeDimension.GRADE, "13CR", 2.0)
    _component(db_session, LeadTimeDimension.LOGISTICS, ANY_ATTRIBUTE_VALUE, 2.0)
    product = _product(db_session, on_hand_qty=0)

    breakdown = resolve_lead_time(db_session, product)

    assert breakdown.modelled is False
    assert breakdown.total_months == 0.0
    assert breakdown.matched_months == 4.0  # reported, NOT totalled
    assert len(breakdown.components) == 2
    assert set(breakdown.missing_dimensions) == {"OD/WT", "Connection"}
    assert "NOT MODELLED" in breakdown.note
    assert "OD/WT" in breakdown.note and "Connection" in breakdown.note

    # Consequences of the decision, stated: no partial total anywhere...
    assert total_lead_time_months(db_session, product) == 0.0
    # ...transit is 0 too, so the two published dates stay consistent with each
    # other rather than one being real and the other a placeholder...
    assert transit_months(db_session, product) == 0.0
    assert breakdown.transit_months == 0.0
    # ...and an incomplete model can never manufacture an UNRECOVERABLE verdict.
    assert is_recoverable(db_session, product, datetime.utcnow() + timedelta(days=1))


def test_mrp_reason_names_the_missing_dimensions(db_session):
    """The incomplete case has to be actionable: "configure OD/WT, Connection"
    can be fixed, "no lead time" cannot."""
    _customer_, node = _customer(db_session)
    _component(db_session, LeadTimeDimension.GRADE, "13CR", 2.0)
    _component(db_session, LeadTimeDimension.LOGISTICS, ANY_ATTRIBUTE_VALUE, 2.0)
    product = _product(db_session, on_hand_qty=0)
    well = Well(planning_node_id=node.id, name="Well LT-Partial", demand_status=DemandStatus.CONFIRMED)
    db_session.add(well)
    db_session.flush()
    _line(db_session, well, product, quantity=1000, days_out=10)
    recompute_well(db_session, well)

    row = mrp_summary(db_session)[0]

    assert row.lead_time_months == 0
    assert row.unrecoverable is False
    assert "no lead-time components configured" in row.reason
    assert "OD/WT" in row.reason and "Connection" in row.reason
    assert "NOT MODELLED" in row.reason
    assert row.lead_time is not None and row.lead_time.modelled is False


def test_the_database_refuses_two_rows_for_one_attribute_value(db_session):
    """First line of defence: the unique constraint. Two rows claiming the same
    (dimension, attribute value) would make the published breakdown a coin
    toss."""
    from sqlalchemy.exc import IntegrityError

    _complete_set(db_session)
    db_session.add(
        LeadTimeComponent(
            dimension=LeadTimeDimension.GRADE, attribute_value="13CR", months=99.0
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_the_resolver_also_refuses_rather_than_silently_picking_a_winner(db_session):
    """Second line of defence, for duplicates the constraint cannot see: a
    database created BEFORE the unique constraint existed (the constraint arrived
    with the same migration as this shape, so any hand-loaded or pre-existing
    sqlite file is in that category). Refusing beats picking, because which row
    won would be invisible in the breakdown -- and the breakdown is the reason
    planners trust the number.

    Driven through a stub session rather than the real one, because the real one
    cannot be made to hold two such rows: the constraint rejects them on flush
    (the test above) and an unflushed row is not visible to a query at all. The
    stub is the only way to exercise the guard, and the guard is worth having --
    it is the difference between an unexplainable number and a loud refusal.
    """
    rows = [
        LeadTimeComponent(
            id="row-a", dimension=LeadTimeDimension.GRADE, attribute_value="13CR",
            months=1.0,
        ),
        LeadTimeComponent(
            id="row-b", dimension=LeadTimeDimension.GRADE, attribute_value="13CR",
            months=99.0,
        ),
    ]

    class _StubQuery:
        def all(self):
            return rows

    class _StubSession:
        def query(self, *_args):
            return _StubQuery()

    product = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="TBG", size="4-1/2", weight=12.6, grade="13CR80", grade_type="13CR",
        connection="VAM TOP",
    )

    with pytest.raises(AmbiguousLeadTimeComponent) as excinfo:
        resolve_lead_time(_StubSession(), product)
    assert "Grade" in str(excinfo.value)
    assert "row-a" in str(excinfo.value) and "row-b" in str(excinfo.value)


def test_a_product_whose_od_wt_is_unlisted_is_not_modelled(db_session):
    """No wildcard on OD/WT means an unlisted size/weight is honestly unknown
    rather than inheriting somebody else's mill queue."""
    _component(db_session, LeadTimeDimension.OD_WT, "4-1/2 12.6", 3.0)
    _component(db_session, LeadTimeDimension.GRADE, "13CR", 1.0)
    _component(db_session, LeadTimeDimension.CONNECTION, ANY_ATTRIBUTE_VALUE, 0.5)
    _component(db_session, LeadTimeDimension.LOGISTICS, ANY_ATTRIBUTE_VALUE, 2.0)

    listed = _product(db_session, size="4-1/2", weight=12.6)
    unlisted = _product(db_session, size="7", weight=29.0, type_="CSG")

    assert total_lead_time_months(db_session, listed) == 6.5
    unresolved = resolve_lead_time(db_session, unlisted)
    assert unresolved.modelled is False
    assert unresolved.missing_dimensions == ("OD/WT",)


def test_product_with_no_weight_keys_on_size_alone(db_session):
    """Honest key for the information present -- and it must be the SAME key the
    seeder would write, or nothing would ever match it."""
    _component(db_session, LeadTimeDimension.OD_WT, "9-5/8", 4.0)
    _component(db_session, LeadTimeDimension.GRADE, "Carbon", 0.5)
    _component(db_session, LeadTimeDimension.CONNECTION, ANY_ATTRIBUTE_VALUE, 0.0)
    _component(db_session, LeadTimeDimension.LOGISTICS, ANY_ATTRIBUTE_VALUE, 2.0)

    product = _product(db_session, size="9-5/8", weight=None, grade_type="Carbon",
                       type_="CSG")

    assert od_wt_key(product) == "9-5/8"
    assert total_lead_time_months(db_session, product) == 6.5


# --------------------------------------------------------------------------
# End to end: a lead-time change moves dates and moves verdicts
# --------------------------------------------------------------------------


def test_lead_time_change_moves_the_order_date_and_the_coverage_verdict(db_session):
    """The whole reason this defect was major: the total reaches
    `recommended_order_date` AND the terminal UNRECOVERABLE claim.

    One product, one uncovered line at ROS +200 days. With a 4.5-month lead time
    steel ordered today still arrives, so the line is UNCOVERED with a future
    order date. Lengthen ONE component -- the OD/WT term -- and the same line
    becomes UNRECOVERABLE with an order date in the past. Nothing else changes.
    """
    _customer_, node = _customer(db_session)
    _complete_set(db_session, od_wt_months=2.0, grade_months=0.5, conn_months=0.0,
                  logistics_months=2.0)
    product = _product(db_session, on_hand_qty=0)
    well = Well(planning_node_id=node.id, name="Well LT-E2E", demand_status=DemandStatus.CONFIRMED)
    db_session.add(well)
    db_session.flush()
    line = _line(db_session, well, product, quantity=1000, days_out=200)

    recompute_well(db_session, well)
    assert line.coverage_result.status == CoverageStatus.UNCOVERED
    before = mrp_summary(db_session)[0]
    assert before.lead_time_months == 4.5
    assert before.unrecoverable is False
    assert before.recommended_order_date > date.today()

    # Same product, same demand: only the OD/WT component's months change.
    od_wt_row = (
        db_session.query(LeadTimeComponent)
        .filter(LeadTimeComponent.dimension == LeadTimeDimension.OD_WT)
        .one()
    )
    od_wt_row.months = 10.0
    db_session.flush()

    recompute_well(db_session, well)
    assert line.coverage_result.status == CoverageStatus.UNRECOVERABLE
    after = mrp_summary(db_session)[0]
    assert after.lead_time_months == 12.5
    assert after.unrecoverable is True
    assert after.recommended_order_date < before.recommended_order_date
    assert after.recommended_order_date < date.today()
    # And the breakdown on the row explains the new date, term by term.
    assert sum(c.months for c in after.lead_time.components) == 12.5
