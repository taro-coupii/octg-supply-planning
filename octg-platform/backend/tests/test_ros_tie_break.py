"""Equal-ROS tie-break (product-owner ruling 2026-09-06): at the same ROS a
Primary line is served before a Contingency line, and two lines of the same
profile are ordered by line id -- a fixed order, not a judgement. The line that
lost such a tie is told so in its reason text.

Until this ruling the tie fell to the caller's order, which was the line id by
way of the query -- stable, but nothing a planner could explain to a customer.
"""

from datetime import datetime, timedelta

from app.engines.allocation import allocate_business_unit, allocation_order
from app.engines.coverage import recompute_business_unit
from app.engines.substitution import decide_approval, request_approval
from app.models import (
    AllocationPolicy,
    CoverageStatus,
    CustomerSubstitutionRule,
    InventoryAssignment,
    TechnicalSubstitution,
)
from tests.test_scenario_engine import _bu, _customer, _line, _product, _stock, _well

ROS = datetime(2027, 3, 1)
LOW_ID = "00000000-0000-4000-8000-000000000001"
HIGH_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


def _verdict(db, line):
    db.expire(line, ["coverage_result"])
    return line.coverage_result


def _two_customers(db, bu, policy_a=AllocationPolicy.SOFT, policy_b=AllocationPolicy.SOFT):
    a, node_a = _customer(db, name="Alpha", bu=bu, policy=policy_a)
    b, node_b = _customer(db, name="Bravo", bu=bu, policy=policy_b)
    return a, node_a, b, node_b


def test_the_order_key_is_ros_then_primary_then_id(db_session):
    """The key itself, so the rule is stated once and read off directly."""
    bu = _bu(db_session)
    product = _product(db_session)
    _, node = _customer(db_session, bu=bu)
    well = _well(db_session, node, "W-KEY")
    later = _line(db_session, well, product, 1, ros_date=ROS + timedelta(days=1), line_id=LOW_ID)
    contingency = _line(
        db_session, well, product, 1, ros_date=ROS,
        profile="Contingency", line_id="00000000-0000-4000-8000-000000000002",
    )
    primary_high = _line(db_session, well, product, 1, ros_date=ROS, line_id=HIGH_ID)
    primary_low = _line(
        db_session, well, product, 1, ros_date=ROS,
        line_id="10000000-0000-4000-8000-000000000000",
    )

    ranked = sorted([later, contingency, primary_high, primary_low], key=allocation_order)
    assert ranked == [primary_low, primary_high, contingency, later]


def test_at_an_equal_ros_the_primary_line_beats_the_contingency_line(db_session):
    """4,000 on the shelf, two lines of 4,000 due the same instant across two
    customers. The Contingency line has the LOWER id -- which under the old
    tie-break would have handed it the steel -- and it still loses."""
    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 4000)
    _, node_a, _, node_b = _two_customers(db_session, bu)
    contingency = _line(
        db_session, _well(db_session, node_a, "W-Backup"), product, 4000,
        ros_date=ROS, profile="Contingency", line_id=LOW_ID,
    )
    primary = _line(
        db_session, _well(db_session, node_b, "W-Main"), product, 4000,
        ros_date=ROS, line_id=HIGH_ID,
    )

    recompute_business_unit(db_session, bu)

    assert _verdict(db_session, primary).status == CoverageStatus.COVERED
    lost = _verdict(db_session, contingency)
    assert lost.status != CoverageStatus.COVERED
    assert lost.drawn_company == 0
    # And the loser is TOLD why, naming the well that took the steel and the
    # rule that decided it.
    assert "drawn ahead of it at the same ROS by W-Main (Bravo, Primary)" in lost.reason
    assert "a Primary line is served before a Contingency line" in lost.reason


def test_at_an_equal_ros_and_profile_the_lower_line_id_wins_whatever_the_insert_order(
    db_session,
):
    """Two Primary lines, same instant. The HIGHER id is inserted first and
    would win a 'first row wins' contest; the lower id wins the fixed one. The
    reason says the order is fixed, not a judgement."""
    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 4000)
    _, node_a, _, node_b = _two_customers(db_session, bu)
    high = _line(
        db_session, _well(db_session, node_a, "W-High"), product, 4000,
        ros_date=ROS, line_id=HIGH_ID,
    )
    low = _line(
        db_session, _well(db_session, node_b, "W-Low"), product, 4000,
        ros_date=ROS, line_id=LOW_ID,
    )

    recompute_business_unit(db_session, bu)

    assert _verdict(db_session, low).status == CoverageStatus.COVERED
    lost = _verdict(db_session, high)
    assert lost.status != CoverageStatus.COVERED
    assert "drawn ahead of it at the same ROS by W-Low (Bravo, Primary)" in lost.reason
    assert "a fixed order, not a judgement of priority" in lost.reason


def test_a_partial_tie_loss_states_both_the_draw_and_the_tie(db_session):
    """6,000 on the shelf, Primary 4,000 and Contingency 4,000 at the same
    instant: the Contingency line gets the 2,000 that is left and its reason
    carries both facts -- the partial draw and who took the rest."""
    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 6000)
    _, node_a, _, node_b = _two_customers(db_session, bu)
    contingency = _line(
        db_session, _well(db_session, node_a, "W-Backup"), product, 4000,
        ros_date=ROS, profile="Contingency", line_id=LOW_ID,
    )
    _line(
        db_session, _well(db_session, node_b, "W-Main"), product, 4000,
        ros_date=ROS, line_id=HIGH_ID,
    )

    recompute_business_unit(db_session, bu)

    lost = _verdict(db_session, contingency)
    assert lost.status != CoverageStatus.COVERED
    assert lost.drawn_company == 2000
    assert "2000 of 4000 drawn from the pool in ROS order, still short by 2000" in lost.reason
    assert "drawn ahead of it at the same ROS by W-Main (Bravo, Primary)" in lost.reason


def test_no_tie_note_when_the_loser_was_simply_later(db_session):
    """A later ROS is not a tie. The reason must not claim one."""
    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 4000)
    _, node_a, _, node_b = _two_customers(db_session, bu)
    _line(db_session, _well(db_session, node_a, "W-Early"), product, 4000, ros_date=ROS)
    late = _line(
        db_session, _well(db_session, node_b, "W-Late"), product, 4000,
        ros_date=ROS + timedelta(days=1),
    )

    recompute_business_unit(db_session, bu)

    lost = _verdict(db_session, late)
    assert lost.status != CoverageStatus.COVERED
    assert "at the same ROS" not in lost.reason


def test_a_hard_line_is_not_told_it_lost_a_pool_contest_it_was_never_in(db_session):
    """A HARD line never draws the shared pool, so a neighbour's pool draw at the
    same instant is not a tie it lost. Its reason stays about its assignment."""
    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 4000)
    _, node_a, _, node_b = _two_customers(
        db_session, bu, policy_a=AllocationPolicy.HARD, policy_b=AllocationPolicy.SOFT
    )
    hard = _line(
        db_session, _well(db_session, node_a, "W-Hard"), product, 4000,
        ros_date=ROS, profile="Contingency", line_id=HIGH_ID,
    )
    _line(
        db_session, _well(db_session, node_b, "W-Soft"), product, 4000,
        ros_date=ROS, line_id=LOW_ID,
    )

    recompute_business_unit(db_session, bu)

    lost = _verdict(db_session, hard)
    assert lost.status != CoverageStatus.COVERED
    assert "No inventory assigned to this demand line" in lost.reason
    assert "at the same ROS" not in lost.reason


def test_a_neighbour_spending_its_own_property_is_not_named_as_a_tie_winner(db_session):
    """Bravo's own uploaded stock covers Bravo's line at the same instant Alpha's
    line goes short of company stock. Alpha never had a claim on that steel, so
    the winner is not named; the shelf was simply empty for Alpha."""
    from app.models import CustomerOwnedInventory

    bu = _bu(db_session)
    product = _product(db_session)
    _stock(db_session, bu, product, 0)
    a, node_a, b, node_b = _two_customers(db_session, bu)
    db_session.add(
        CustomerOwnedInventory(customer_id=b.id, product_id=product.id, quantity=4000)
    )
    db_session.flush()
    alpha = _line(
        db_session, _well(db_session, node_a, "W-Alpha"), product, 4000,
        ros_date=ROS, line_id=HIGH_ID,
    )
    bravo = _line(
        db_session, _well(db_session, node_b, "W-Bravo"), product, 4000,
        ros_date=ROS, line_id=LOW_ID,
    )

    recompute_business_unit(db_session, bu)

    assert _verdict(db_session, bravo).status == CoverageStatus.COVERED
    lost = _verdict(db_session, alpha)
    assert lost.status != CoverageStatus.COVERED
    assert "at the same ROS" not in lost.reason


def test_the_substitution_fall_through_uses_the_same_tie_break(db_session):
    """Two lines short of their own product at the same instant; one approved
    substitute with stock for exactly one of them. The Primary line -- despite the
    higher id -- gets the substitute, the Contingency line stays short."""
    bu = _bu(db_session)
    p = _product(db_session, grade="13CR80")
    q = _product(db_session, grade="13CR110")
    _stock(db_session, bu, p, 0)
    _stock(db_session, bu, q, 4000)
    a, node_a, b, node_b = _two_customers(db_session, bu)
    db_session.add(TechnicalSubstitution(from_product_id=p.id, to_product_id=q.id))
    for customer in (a, b):
        db_session.add(
            CustomerSubstitutionRule(
                customer_id=customer.id, from_product_id=p.id, to_product_id=q.id,
                allowed=True,
            )
        )
    db_session.flush()
    contingency = _line(
        db_session, _well(db_session, node_a, "W-Backup"), p, 4000,
        ros_date=ROS, profile="Contingency", line_id=LOW_ID,
    )
    primary = _line(
        db_session, _well(db_session, node_b, "W-Main"), p, 4000,
        ros_date=ROS, line_id=HIGH_ID,
    )
    for line in (contingency, primary):
        approval = request_approval(db_session, line, p.id, q.id)
        decide_approval(db_session, approval.id, approved=True)

    recompute_business_unit(db_session, bu)

    assert _verdict(db_session, primary).status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert _verdict(db_session, contingency).status != CoverageStatus.COVERED_VIA_SUBSTITUTE


def test_the_allocator_ranks_by_the_key_not_by_the_order_it_is_handed(db_session):
    """Straight at the engine: lines passed Contingency-first, same instant, one
    unit of stock. The Primary line is covered."""
    bu = _bu(db_session)
    product = _product(db_session)
    _, node = _customer(db_session, bu=bu)
    well = _well(db_session, node, "W-ENGINE")
    contingency = _line(
        db_session, well, product, 1, ros_date=ROS, profile="Contingency", line_id=LOW_ID
    )
    primary = _line(db_session, well, product, 1, ros_date=ROS, line_id=HIGH_ID)
    customer = "c"

    outcome = allocate_business_unit(
        lines=[contingency, primary],
        company_on_hand=1,
        total_assigned=0,
        assigned_by_line={},
        assignment_block_by_customer={customer: 0.0},
        customer_owned_by_customer={customer: 0.0},
        policy_by_customer={customer: AllocationPolicy.SOFT},
        customer_of_line={contingency.id: customer, primary.id: customer},
    )

    assert outcome.covered == {primary.id: True, contingency.id: False}
