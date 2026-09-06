"""D01 (product-owner ruling 2026-09-06): coverage is allocated across the whole
Business Unit, not customer by customer.

The defect this pins, reproduced against the shipped demo database before the
change: 10,045 metres of one tubing on the shelf, and four wells across two
customers each told "Covered" -- 14,050 metres promised out of 10,045. Each
customer was evaluated independently against the WHOLE BU quantity, so the sum
of the promises exceeded the steel.

After the change the same pool is contested once, earliest ROS first, across
every customer in the Business Unit. Ownership walls are unchanged: a customer's
own uploaded stock is drawable only by that customer's lines, and an Oracle
assignment is drawable only by the line it names.
"""

import pytest

from app.engines.coverage import recompute_business_unit
from app.models import (
    AllocationPolicy,
    CoverageStatus,
    CustomerOwnedInventory,
    InventoryOnHand,
)
from tests.test_scenario_engine import _bu, _customer, _line, _product, _stock, _well


def _verdict(db, line):
    db.expire(line, ["coverage_result"])
    return line.coverage_result.status


def test_two_customers_cannot_both_be_promised_the_same_steel(db_session):
    """THE D01 CASE. 6,000 on hand; A wants 4,000, B wants 4,000. Before this
    change both were Covered. Now the earlier ROS takes the steel and the later
    one is short by exactly what the Business Unit does not have."""
    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 6000)

    a, node_a = _customer(db_session, name="Alpha", bu=bu)
    b, node_b = _customer(db_session, name="Bravo", bu=bu)
    well_a = _well(db_session, node_a, "W-Alpha")
    well_b = _well(db_session, node_b, "W-Bravo")
    line_a = _line(db_session, well_a, product, quantity=4000, days_out=100)
    line_b = _line(db_session, well_b, product, quantity=4000, days_out=200)

    recompute_business_unit(db_session, bu)

    assert _verdict(db_session, line_a) == CoverageStatus.COVERED
    assert _verdict(db_session, line_b) != CoverageStatus.COVERED
    # And the arithmetic is stated, not merely implied: 2,000 of the 4,000 was
    # drawn, 2,000 has no steel behind it (F04's net position).
    result_b = line_b.coverage_result
    assert result_b.drawn_company == 2000
    assert result_b.residual == 2000


def test_the_earliest_ros_wins_across_the_customer_line(db_session):
    """Ordering is by ROS across the whole BU, not by customer. Swapping the two
    ROS dates swaps which customer is covered -- nothing about the customers
    themselves decides it."""
    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 6000)

    a, node_a = _customer(db_session, name="Alpha", bu=bu)
    b, node_b = _customer(db_session, name="Bravo", bu=bu)
    line_a = _line(db_session, _well(db_session, node_a, "W-A"), product, quantity=4000, days_out=300)
    line_b = _line(db_session, _well(db_session, node_b, "W-B"), product, quantity=4000, days_out=50)

    recompute_business_unit(db_session, bu)

    assert _verdict(db_session, line_b) == CoverageStatus.COVERED
    assert _verdict(db_session, line_a) != CoverageStatus.COVERED


def test_another_business_unit_is_untouched(db_session):
    """The BU stays an absolute boundary: the pool contested is one BU's."""
    bu1 = _bu(db_session, name="North")
    bu2 = _bu(db_session, name="South")
    product = _product(db_session)
    _stock(db_session, bu1, product, 6000)
    _stock(db_session, bu2, product, 6000)

    a, node_a = _customer(db_session, name="Alpha", bu=bu1)
    b, node_b = _customer(db_session, name="Bravo", bu=bu2)
    line_a = _line(db_session, _well(db_session, node_a, "W-A"), product, quantity=4000, days_out=100)
    line_b = _line(db_session, _well(db_session, node_b, "W-B"), product, quantity=4000, days_out=200)

    recompute_business_unit(db_session, bu1)
    recompute_business_unit(db_session, bu2)

    assert _verdict(db_session, line_a) == CoverageStatus.COVERED
    assert _verdict(db_session, line_b) == CoverageStatus.COVERED


def test_customer_owned_stock_is_never_taken_by_a_neighbour(db_session):
    """The ownership wall survives the pooling. Bravo's own uploaded steel covers
    Bravo even though Alpha's line is earlier and short."""
    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 0)

    a, node_a = _customer(db_session, name="Alpha", bu=bu)
    b, node_b = _customer(db_session, name="Bravo", bu=bu)
    db_session.add(
        CustomerOwnedInventory(customer_id=b.id, product_id=product.id, quantity=4000)
    )
    db_session.flush()
    line_a = _line(db_session, _well(db_session, node_a, "W-A"), product, quantity=4000, days_out=50)
    line_b = _line(db_session, _well(db_session, node_b, "W-B"), product, quantity=4000, days_out=300)

    recompute_business_unit(db_session, bu)

    assert _verdict(db_session, line_a) != CoverageStatus.COVERED
    assert _verdict(db_session, line_b) == CoverageStatus.COVERED
    assert line_b.coverage_result.drawn_customer_owned == 4000


# --------------------------------------------------------------------------
# The preview follows the allocation. A scenario belonging to one customer moves
# its neighbours' wells, so the preview has to show them -- otherwise apply keeps
# a promise the planner was never shown.
# --------------------------------------------------------------------------


def test_a_scenario_preview_shows_the_knock_on_onto_a_neighbour(db_session):
    from app.engines.scenario import preview
    from tests.test_scenario_engine import _demand_override, _scenario

    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 6000)

    a, node_a = _customer(db_session, name="Alpha", bu=bu)
    b, node_b = _customer(db_session, name="Bravo", bu=bu)
    line_a = _line(db_session, _well(db_session, node_a, "W-Alpha"), product, quantity=4000, days_out=100)
    line_b = _line(db_session, _well(db_session, node_b, "W-Bravo"), product, quantity=4000, days_out=200)
    recompute_business_unit(db_session, bu)
    assert _verdict(db_session, line_b) != CoverageStatus.COVERED

    # Alpha's planner asks "what if we did not need this at all?". The 4,000 they
    # release is what covers Bravo -- a customer this scenario never names.
    scenario = _scenario(db_session, a, "Alpha stands down")
    _demand_override(db_session, scenario, line_a, "quantity", number=1)

    impact = preview(db_session, scenario)

    by_line = {c.demand_line_id: c for c in impact.line_changes}
    assert set(by_line) == {line_a.id, line_b.id}
    neighbour = by_line[line_b.id]
    assert neighbour.changed is True
    assert neighbour.status_before == "Uncovered"
    assert neighbour.status_after == "Covered"
    # And it is attributed, not anonymous.
    assert neighbour.customer_id == b.id
    assert neighbour.customer_name == "Bravo"
    assert neighbour.directly_overridden is False
    assert by_line[line_a.id].customer_name == "Alpha"

    # The well rollup carries the same attribution.
    moved = [w for w in impact.well_changes if w.changed]
    assert {w.customer_name for w in moved} == {"Bravo"}


def test_apply_keeps_the_promise_the_preview_made_across_the_business_unit(db_session):
    from app.engines.scenario import preview
    from app.engines.scenario_apply import apply_to_base_plan
    from tests.test_scenario_engine import _demand_override, _scenario

    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 6000)
    a, node_a = _customer(db_session, name="Alpha", bu=bu)
    b, node_b = _customer(db_session, name="Bravo", bu=bu)
    line_a = _line(db_session, _well(db_session, node_a, "W-Alpha"), product, quantity=4000, days_out=100)
    line_b = _line(db_session, _well(db_session, node_b, "W-Bravo"), product, quantity=4000, days_out=200)
    recompute_business_unit(db_session, bu)

    scenario = _scenario(db_session, a, "Alpha stands down")
    _demand_override(db_session, scenario, line_a, "quantity", number=1)
    promised = {c.demand_line_id: c.status_after for c in preview(db_session, scenario).line_changes}

    result = apply_to_base_plan(db_session, scenario)

    assert dict(result.line_status_after) == promised
    assert _verdict(db_session, line_b) == CoverageStatus.COVERED
