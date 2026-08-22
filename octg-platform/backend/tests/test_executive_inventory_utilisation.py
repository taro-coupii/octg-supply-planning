"""`app.engines.executive.inventory_utilisation` -- the new Executive Dashboard
block. Of company-owned steel standing in the yard, how much is tied to
in-window demand and how much is idle.

THE DENOMINATOR IS INVENTORY, NOT DEMAND -- the opposite of
`soft_allocation_coverage`. This is demand NETTING against on-hand stock, never
a return of the old `InventoryAssignment`-based allocation block.
"""

from datetime import datetime, timedelta

import pytest

from app.engines.executive import ALLOCATION_HORIZONS, inventory_utilisation
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    CustomerOwnedInventory,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    InventoryOnOrder,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
)
from app.models.customer import Customer

NOW = datetime.utcnow()


def _bu(db, name="BU"):
    bu = BusinessUnit(name=name)
    db.add(bu)
    db.flush()
    return bu


def _customer(db, bu, name, policy=AllocationPolicy.SOFT):
    customer = Customer(name=name, allocation_policy=policy, business_unit_id=bu.id)
    db.add(customer)
    db.flush()
    node = PlanningNode(
        customer_id=customer.id, node_type="Campaign", name=f"{name} campaign"
    )
    db.add(node)
    db.flush()
    return customer, node


def _product(db, description, unit=UnitOfMeasure.MTR, weight=53.5):
    product = Product(
        type="CSG", size="9-5/8", weight=weight, grade="P110", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description=description, unit_of_measure=unit,
    )
    db.add(product)
    db.flush()
    return product


def _on_hand(db, bu, product, quantity, source_system="synthetic"):
    row = InventoryOnHand(
        business_unit_id=bu.id, product_id=product.id, quantity=quantity,
        source_system=source_system,
    )
    db.add(row)
    db.flush()
    return row


def _well(db, node, name, status=DemandStatus.CONFIRMED):
    well = Well(planning_node_id=node.id, name=name, demand_status=status)
    db.add(well)
    db.flush()
    return well


def _line(db, well, product, quantity, days_out=30):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity,
        ros_date=NOW + timedelta(days=days_out),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.flush()
    return line


def _customer_owned(db, customer, product, quantity):
    row = CustomerOwnedInventory(
        customer_id=customer.id, product_id=product.id, quantity=quantity,
        source_system="customer-upload",
    )
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# Core netting arithmetic
# ---------------------------------------------------------------------------


def test_every_on_hand_row_is_reported_whether_or_not_anything_demands_it(db_session):
    """The denominator is the YARD, not the order book.

    REGRESSION. The first implementation built its product list from the demand
    in the window, so a product with stock and no demand never appeared at all --
    and `tied + not_tied` silently came out lower than the Business Unit's real
    on-hand total. Against seeded data 58 500 Mtr of the North Sea BU's stock was
    invisible at a 12-month horizon.

    That is the worst possible direction for this bug to point: the stock a
    report of idle stock drops is, by construction, the most idle stock there is.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    wanted = _product(db_session, "Wanted casing")
    ignored = _product(db_session, "Nobody wants this")
    well = _well(db_session, node, "Well 1")
    _line(db_session, well, wanted, quantity=3000, days_out=30)

    _on_hand(db_session, bu, wanted, quantity=5000)
    _on_hand(db_session, bu, ignored, quantity=7000)  # demanded by nothing

    by_horizon = {
        months: inventory_utilisation(
            db_session, business_unit_id=bu.id, horizon_months=months, now=NOW
        )
        for months in (12, 18, 24, 36)
    }

    for months, result in by_horizon.items():
        reported = {row.product_id: row for row in result.products}
        assert set(reported) == {wanted.id, ignored.id}, (
            f"horizon {months}: every on-hand row must be reported, "
            "including ones nothing is asking for"
        )
        assert reported[ignored.id].tied == 0
        assert reported[ignored.id].not_tied == 7000

        # The invariant the original bug broke: the two halves must add back up
        # to the steel actually standing in the yard.
        total = sum(row.on_hand_quantity for row in result.products)
        assert total == 12000, f"horizon {months}: on-hand total must be 12 000"
        assert (
            sum(row.tied for row in result.products)
            + sum(row.not_tied for row in result.products)
            == total
        )

    # A yard position cannot depend on how far ahead you look.
    totals = {
        months: sum(row.on_hand_quantity for row in result.products)
        for months, result in by_horizon.items()
    }
    assert len(set(totals.values())) == 1, f"on-hand total moved with horizon: {totals}"


def test_tied_plus_not_tied_equals_on_hand(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    product = _product(db_session, "Casing A")
    well = _well(db_session, node, "Well 1")
    _line(db_session, well, product, quantity=3000, days_out=30)
    _on_hand(db_session, bu, product, quantity=5000)

    result = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=12, now=NOW
    )

    assert result.available is True
    assert len(result.products) == 1
    row = result.products[0]
    assert row.on_hand_quantity == 5000
    assert row.demand_in_window == 3000
    assert row.tied == 3000
    assert row.not_tied == 2000
    assert row.tied + row.not_tied == row.on_hand_quantity


def test_demand_beyond_horizon_does_not_tie_and_shortening_the_horizon_moves_it(
    db_session,
):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    product = _product(db_session, "Casing A")
    well = _well(db_session, node, "Well 1")
    # Inside a 24-month horizon but OUTSIDE a 12-month one.
    _line(db_session, well, product, quantity=4000, days_out=500)
    _on_hand(db_session, bu, product, quantity=5000)

    short = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=12, now=NOW
    )
    long = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=24, now=NOW
    )

    # 12-month horizon: the demand is outside the window, so NOTHING is tied --
    # but the 5 000 on the ground has not stopped existing. It is 100% IDLE, and
    # that is the answer, not an absence of one. Reporting `available=False` here
    # would hide the single situation this block exists to surface.
    assert short.available is True
    assert len(short.products) == 1
    idle = short.products[0]
    assert idle.on_hand_quantity == 5000
    assert idle.demand_in_window == 0
    assert idle.tied == 0
    assert idle.not_tied == 5000
    assert "idle" in (short.reason or "")

    # 24-month horizon: the same on-hand is now tied by the same demand.
    assert long.available is True
    row = long.products[0]
    assert row.tied == 4000
    assert row.not_tied == 1000

    # The yard position is the same in both windows. The horizon moves stock
    # BETWEEN tied and not_tied; it can never change how much steel is held.
    assert idle.on_hand_quantity == row.on_hand_quantity
    assert idle.tied + idle.not_tied == row.tied + row.not_tied


def test_customer_owned_inventory_reduces_tied(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    product = _product(db_session, "Casing A")
    well = _well(db_session, node, "Well 1")
    _line(db_session, well, product, quantity=3000, days_out=30)
    _on_hand(db_session, bu, product, quantity=5000)
    _customer_owned(db_session, customer, product, quantity=1000)

    result = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=12, now=NOW
    )

    row = result.products[0]
    # demand 3000 - customer_owned 1000 = 2000 tied (not 3000).
    assert row.customer_owned_quantity == 1000
    assert row.tied == 2000
    assert row.not_tied == 3000
    assert row.tied + row.not_tied == row.on_hand_quantity


def test_customer_owned_fully_covering_demand_ties_nothing(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    product = _product(db_session, "Casing A")
    well = _well(db_session, node, "Well 1")
    _line(db_session, well, product, quantity=1000, days_out=30)
    _on_hand(db_session, bu, product, quantity=5000)
    _customer_owned(db_session, customer, product, quantity=5000)

    result = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=12, now=NOW
    )
    row = result.products[0]
    assert row.tied == 0
    assert row.not_tied == 5000


# ---------------------------------------------------------------------------
# Unknown position
# ---------------------------------------------------------------------------


def test_product_with_no_on_hand_row_is_unknown_not_zero(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    product = _product(db_session, "Casing Unknown")
    well = _well(db_session, node, "Well 1")
    _line(db_session, well, product, quantity=1000, days_out=30)
    # Deliberately NO InventoryOnHand row for this product.

    result = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=12, now=NOW
    )

    assert result.unknown_position_count == 1
    assert result.unknown_position[0].product_id == product.id
    # In NEITHER tied nor not_tied -- it must not appear in the netted products.
    assert all(p.product_id != product.id for p in result.products)


# ---------------------------------------------------------------------------
# Business Unit boundary
# ---------------------------------------------------------------------------


def test_demand_in_another_business_unit_does_not_tie_this_bus_inventory(db_session):
    bu_a = _bu(db_session, "BU A")
    bu_b = _bu(db_session, "BU B")
    customer_a, node_a = _customer(db_session, bu_a, "Cust A")
    customer_b, node_b = _customer(db_session, bu_b, "Cust B")
    product = _product(db_session, "Shared SKU")

    well_b = _well(db_session, node_b, "Well B1")
    _line(db_session, well_b, product, quantity=9000, days_out=30)  # huge demand in BU B

    # BU A holds stock of the SAME product but has NO demand for it in scope.
    _on_hand(db_session, bu_a, product, quantity=5000)
    # BU B has demand but no on-hand row -> unknown for BU B.

    result = inventory_utilisation(
        db_session, business_unit_id=bu_a.id, horizon_months=12, now=NOW
    )

    # BU B's demand must not reach across the boundary and tie BU A's steel --
    # but BU A's steel is still REPORTED, as fully idle. The BU boundary decides
    # what may be netted, never whether a position is visible at all: a Business
    # Unit sitting on stock nobody in it wants is the most actionable finding
    # this block can produce.
    assert result.available is True
    assert len(result.products) == 1
    row = result.products[0]
    assert row.business_unit_id == bu_a.id
    assert row.on_hand_quantity == 5000
    assert row.demand_in_window == 0  # BU B's 9 000 did NOT cross the boundary
    assert row.tied == 0
    assert row.not_tied == 5000


# ---------------------------------------------------------------------------
# Metric-tonnes headline and unconvertible quantities
# ---------------------------------------------------------------------------


def test_pc_product_is_reported_unconvertible_not_dropped(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    pc_product = _product(db_session, "Float Collar", unit=UnitOfMeasure.PC, weight=None)
    well = _well(db_session, node, "Well 1")
    _line(db_session, well, pc_product, quantity=10, days_out=30)
    _on_hand(db_session, bu, pc_product, quantity=20)

    result = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=12, now=NOW
    )

    # The PC product is netted normally (tied/not_tied are native-unit facts,
    # unaffected by convertibility) ...
    assert len(result.products) == 1
    assert result.products[0].tied == 10
    assert result.products[0].not_tied == 10

    # ... but it must show up as EXCLUDED from the metric-tonnes headline, named
    # with a reason, never silently absorbed or dropped.
    assert any(u.product_id == pc_product.id and u.side == "tied" for u in result.unconvertible)
    assert any(
        u.product_id == pc_product.id and u.side == "not_tied" for u in result.unconvertible
    )
    # Nothing else contributed, so both totals are fully unconvertible.
    assert result.tied_tonnes.available is False
    assert result.not_tied_tonnes.available is False


def test_mixed_convertible_and_unconvertible_gives_a_partial_tonnes_total(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    mtr_product = _product(db_session, "Casing MTR", unit=UnitOfMeasure.MTR, weight=53.5)
    pc_product = _product(db_session, "Float Collar", unit=UnitOfMeasure.PC, weight=None)
    well = _well(db_session, node, "Well 1")
    _line(db_session, well, mtr_product, quantity=1000, days_out=30)
    _line(db_session, well, pc_product, quantity=10, days_out=30)
    _on_hand(db_session, bu, mtr_product, quantity=1000)
    _on_hand(db_session, bu, pc_product, quantity=20)

    result = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=12, now=NOW
    )

    # tied side: mtr fully tied (1000 == demand), so it CAN convert -> partial total.
    assert result.tied_tonnes.available is True
    assert result.tied_tonnes.value is not None
    assert result.tied_tonnes.value > 0
    assert result.tied_tonnes.reason is not None  # says something was excluded
    assert any(u.side == "tied" and u.product_id == pc_product.id for u in result.unconvertible)


def test_no_conversion_boundary_leak_into_native_breakdown(db_session):
    """`tied_by_unit` / `not_tied_by_unit` stay in NATIVE units regardless of
    convertibility -- the metric-tonnes conversion never touches them."""
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    product = _product(db_session, "Casing A", unit=UnitOfMeasure.MTR)
    well = _well(db_session, node, "Well 1")
    _line(db_session, well, product, quantity=1000, days_out=30)
    _on_hand(db_session, bu, product, quantity=1000)

    result = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=12, now=NOW
    )
    assert {q.unit_of_measure for q in result.tied_by_unit} <= {UnitOfMeasure.MTR}
    assert {q.unit_of_measure for q in result.not_tied_by_unit} <= {UnitOfMeasure.MTR}


def test_horizon_must_be_a_legal_allocation_horizon(db_session):
    bu = _bu(db_session)
    with pytest.raises(ValueError):
        inventory_utilisation(db_session, business_unit_id=bu.id, horizon_months=7)
    # Sanity: every legal value is accepted without raising.
    for months in ALLOCATION_HORIZONS:
        inventory_utilisation(db_session, business_unit_id=bu.id, horizon_months=months)


# ---------------------------------------------------------------------------
# Overdue demand (product-owner decision, 2026-08-12): counted AND labelled
# ---------------------------------------------------------------------------


def test_overdue_demand_counts_into_the_tie_and_is_labelled(db_session):
    """A line whose ROS date has already passed still needs steel.

    600 overdue + 400 forward against 2000 on hand: tied is 1000 (both count),
    and the overdue part is reported as its own bucket rather than silently
    blended into demand_in_window.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    product = _product(db_session, "Casing A")
    well = _well(db_session, node, "Well 1")
    _line(db_session, well, product, quantity=600, days_out=-45)
    _line(db_session, well, product, quantity=400, days_out=30)
    _on_hand(db_session, bu, product, quantity=2000)

    result = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=12, now=NOW
    )
    row = next(p for p in result.products if p.product_id == product.id)
    assert row.demand_in_window == 1000
    assert row.demand_overdue == 600
    assert row.tied == 1000
    assert row.not_tied == 1000


def test_forward_only_demand_reports_zero_overdue(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Cust A")
    product = _product(db_session, "Casing A")
    well = _well(db_session, node, "Well 1")
    _line(db_session, well, product, quantity=500, days_out=60)
    _on_hand(db_session, bu, product, quantity=1000)

    result = inventory_utilisation(
        db_session, business_unit_id=bu.id, horizon_months=12, now=NOW
    )
    row = next(p for p in result.products if p.product_id == product.id)
    assert row.demand_overdue == 0.0
    assert row.demand_in_window == 500
