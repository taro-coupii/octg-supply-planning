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
    InventoryAssignment,
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


# --------------------------------------------------------------------------
# Two defects an adversarial review of this change found, each reproduced here
# before it was fixed.
# --------------------------------------------------------------------------


def test_an_assignment_override_restates_a_soft_reservation_rather_than_adding_to_it(
    db_session,
):
    """A no-op ASSIGNMENT override must move nobody.

    `assignment_overrides()` carries an ABSOLUTE quantity. Under SOFT the
    override used to be ADDED to the customer's pooled block while the stored row
    was already in it, so restating a line's existing 2,000 as 2,000 counted it
    twice, shrank the shared pool by 2,000 and flipped a NEIGHBOUR from Covered
    to Uncovered in the preview -- a scenario that changed nothing, changing
    somebody else's answer.
    """
    from app.engines.scenario import preview
    from app.models import InventoryAssignment, ScenarioTargetKind
    from tests.test_scenario_engine import _override, _scenario

    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 6000)
    a, node_a = _customer(db_session, name="Alpha", bu=bu, policy=AllocationPolicy.SOFT)
    b, node_b = _customer(db_session, name="Bravo", bu=bu, policy=AllocationPolicy.SOFT)
    line_a = _line(db_session, _well(db_session, node_a, "W-A"), product, quantity=3000, days_out=100)
    line_b = _line(db_session, _well(db_session, node_b, "W-B"), product, quantity=3000, days_out=200)
    db_session.add(
        InventoryAssignment(
            demand_line_id=line_a.id, product_id=product.id, quantity=2000,
            source_system="synthetic",
        )
    )
    db_session.flush()
    recompute_business_unit(db_session, bu)
    assert _verdict(db_session, line_a) == CoverageStatus.COVERED
    assert _verdict(db_session, line_b) == CoverageStatus.COVERED

    scenario = _scenario(db_session, a, "Restate the same assignment")
    _override(
        db_session, scenario,
        target_kind=ScenarioTargetKind.ASSIGNMENT,
        target_demand_line_id=line_a.id,
        target_product_id=product.id,
        field_name="quantity", value_number=2000,
    )
    by_line = {c.demand_line_id: c for c in preview(db_session, scenario).line_changes}
    assert by_line[line_b.id].status_after == "Covered"
    assert by_line[line_a.id].status_after == "Covered"
    assert all(not c.changed for c in by_line.values())


def test_over_subscription_counts_contested_steel_not_a_neighbours_property(db_session):
    """The pending-substitute figure describes the SHARED tier and nothing else.

    Three customers, each with a line pending approval on the same substitute,
    2,000 of that substitute on the company shelf and 4,000 privately owned by
    ONE of them. Adding every pending customer's private stock into one
    "available" number reported a genuine contest as healthy, using steel two of
    the three could never draw.
    """
    from app.models import (
        CustomerOwnedInventory, CustomerSubstitutionRule, TechnicalSubstitution,
    )

    bu = _bu(db_session)
    primary = _product(db_session, grade="13CR80")
    substitute = _product(db_session, grade="13CR110")
    _stock(db_session, bu, primary, 0)
    _stock(db_session, bu, substitute, 2000)
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    owners = []
    for name, days in (("Alpha", 100), ("Bravo", 200), ("Charlie", 300)):
        customer, node = _customer(db_session, name=name, bu=bu)
        _line(db_session, _well(db_session, node, f"W-{name}"), primary, quantity=2000, days_out=days)
        db_session.add(
            CustomerSubstitutionRule(
                customer_id=customer.id, from_product_id=primary.id,
                to_product_id=substitute.id, allowed=True,
            )
        )
        owners.append(customer)
    db_session.add(
        CustomerOwnedInventory(
            customer_id=owners[2].id, product_id=substitute.id, quantity=4000
        )
    )
    db_session.flush()

    computed = recompute_business_unit(db_session, bu)
    load = {l.to_product_id: l for l in computed.pending_substitute_load}[substitute.id]

    # Charlie's 4,000 covers its own line, so only Alpha and Bravo contest the
    # company's 2,000 -- and that contest is real.
    assert load.available_qty == 2000.0
    assert load.pending_required_qty == 4000.0
    assert load.over_subscribed is True
    assert load.shortfall == 2000.0


def test_over_assignment_cannot_manufacture_coverage(db_session):
    """Reservations are entitlements to company steel, not steel of their own.

    Oracle can hold more assignments than the Business Unit has metres, and a
    scenario can invent that state deliberately. Without the physical cap in
    `allocate_business_unit` each entitlement would be honoured in full and the
    BU would promise 8,000 out of 5,000 -- the exact thing D01 abolished, wearing
    a different hat.
    """
    from app.models import InventoryAssignment

    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 5000)
    a, node_a = _customer(db_session, name="Alpha", bu=bu, policy=AllocationPolicy.HARD)
    b, node_b = _customer(db_session, name="Bravo", bu=bu, policy=AllocationPolicy.HARD)
    line_a = _line(db_session, _well(db_session, node_a, "W-A"), product, quantity=4000, days_out=100)
    line_b = _line(db_session, _well(db_session, node_b, "W-B"), product, quantity=4000, days_out=200)
    for line in (line_a, line_b):
        db_session.add(
            InventoryAssignment(
                demand_line_id=line.id, product_id=product.id, quantity=4000,
                source_system="synthetic",
            )
        )
    db_session.flush()

    recompute_business_unit(db_session, bu)

    # 8,000 reserved against 5,000 held: the earlier line gets its 4,000 and the
    # later one is short, rather than both being told the steel is theirs.
    assert _verdict(db_session, line_a) == CoverageStatus.COVERED
    assert _verdict(db_session, line_b) != CoverageStatus.COVERED
    drawn = (
        line_a.coverage_result.drawn_company + line_b.coverage_result.drawn_company
    )
    assert drawn <= 5000


def test_a_soft_customers_own_reservation_is_named_in_its_reason_not_called_unassigned(
    db_session,
):
    """Owner ruling 2026-09-06: reserved steel is called reserved on the well
    screen exactly as on the Executive Dashboard. Alpha (SOFT) holds a 3,000
    reservation on its 5,000 line and Bravo (SOFT) a 2,000 one, so 1,000 of the
    6,000 is genuinely unassigned. Alpha's line draws its own 3,000 and the shared
    1,000 and is short 1,000 -- and its reason says which was which."""
    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 6000)
    a, node_a = _customer(db_session, name="Alpha", bu=bu, policy=AllocationPolicy.SOFT)
    b, node_b = _customer(db_session, name="Bravo", bu=bu, policy=AllocationPolicy.SOFT)
    line_a = _line(db_session, _well(db_session, node_a, "W-Alpha"), product, 5000, days_out=100)
    line_b = _line(db_session, _well(db_session, node_b, "W-Bravo"), product, 1000, days_out=200)
    for line, qty in ((line_a, 3000), (line_b, 2000)):
        db_session.add(
            InventoryAssignment(
                demand_line_id=line.id, product_id=product.id, quantity=qty,
                source_system="synthetic",
            )
        )
    db_session.flush()

    recompute_business_unit(db_session, bu)

    result = line_a.coverage_result
    assert result.status != CoverageStatus.COVERED
    assert (
        "3000 of 5000 drawn from this customer's own pooled Oracle reservation and "
        "1000 from the unassigned remainder in ROS order, still short by 1000"
    ) in result.reason
    assert "4000 of 5000" not in result.reason


def test_the_split_applies_when_only_this_customer_holds_a_reservation(db_session):
    """No neighbour reservation, so the plain SOFT sentence: the reserved part is
    still named, and the shared part is called the shared pool."""
    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 4000)
    a, node_a = _customer(db_session, name="Alpha", bu=bu, policy=AllocationPolicy.SOFT)
    line_a = _line(db_session, _well(db_session, node_a, "W-Alpha"), product, 5000, days_out=100)
    db_session.add(
        InventoryAssignment(
            demand_line_id=line_a.id, product_id=product.id, quantity=3000,
            source_system="synthetic",
        )
    )
    db_session.flush()

    recompute_business_unit(db_session, bu)

    reason = line_a.coverage_result.reason
    assert reason.startswith("Insufficient on-hand inventory for requested ROS")
    assert (
        "3000 of 5000 drawn from this customer's own pooled Oracle reservation and "
        "1000 from the shared pool in ROS order, still short by 1000"
    ) in reason
