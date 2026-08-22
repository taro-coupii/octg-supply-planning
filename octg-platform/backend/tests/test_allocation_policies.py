"""Phase 3 -- HARD and HYBRID allocation policies.

The defining difference between the policies is what a line is allowed to draw
on, so every test here fixes the same physical stock and only varies the policy
and the assignment rows.
"""

from datetime import datetime, timedelta

from app.engines.allocation import allocate_detailed
from app.engines.coverage import recompute_well
from app.engines.substitution import BLOCK_ORACLE_RELEASE, RECOMMENDED_ACTION
from app.models import (
    BusinessUnit,
    CoverageStatus,
    Customer,
    CustomerSubstitutionRule,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryAssignment,
    InventoryOnHand,
    PlanningNode,
    Product,
    TechnicalSubstitution,
    UnitOfMeasure,
    Well,
    AllocationPolicy,
)


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
        .filter(BusinessUnit.name == "Alloc BU")
        .one_or_none()
    )
    if bu is None:
        bu = BusinessUnit(name="Alloc BU")
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


def _customer(db_session, policy, name="Alloc Test Co"):
    customer = Customer(
        name=name,
        allocation_policy=policy,
        business_unit_id=_default_bu(db_session).id,
    )
    db_session.add(customer)
    db_session.flush()
    node = PlanningNode(customer_id=customer.id, node_type="Campaign", name="Alloc Campaign")
    db_session.add(node)
    db_session.flush()
    return customer, node


def _product(db_session, on_hand_qty, grade="13CR80"):
    """Product + its on-hand row in the file's single BU. `on_hand_qty` means
    what it always did; it is simply stored where quantity lives."""
    product = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="TBG", size="4-1/2", weight=12.6, grade=grade, grade_type="13CR",
        connection="VAM TOP", description=f"TBG 4-1/2 12.6 {grade} VAM TOP",
    )
    db_session.add(product)
    db_session.flush()
    _stock(db_session, _default_bu(db_session), product, on_hand_qty)
    return product


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


def _line(db_session, well, product, quantity, days_out=400):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity,
        ros_date=datetime.utcnow() + timedelta(days=days_out),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(line)
    db_session.flush()
    return line


def _assign(db_session, line, product, quantity):
    row = InventoryAssignment(
        demand_line_id=line.id, product_id=product.id, quantity=quantity,
        source_system="synthetic",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _status(line):
    return line.coverage_result.status


# --------------------------------------------------------------------------
# HARD
# --------------------------------------------------------------------------


def test_hard_sufficient_assignment_is_covered(db_session):
    _c, node = _customer(db_session, AllocationPolicy.HARD)
    product = _product(db_session, on_hand_qty=10000)
    well = _well(db_session, node, "Well Hard-Covered")
    line = _line(db_session, well, product, quantity=4000)
    _assign(db_session, line, product, 4000)

    recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.COVERED
    assert well.coverage_status == CoverageStatus.COVERED.value


def test_hard_no_assignment_is_uncovered_despite_ample_pool(db_session):
    """THE defining hard-allocation case: 10000 on the shelf, none of it assigned
    to this line, so the line is not covered."""
    _c, node = _customer(db_session, AllocationPolicy.HARD)
    product = _product(db_session, on_hand_qty=10000)
    well = _well(db_session, node, "Well Hard-Unassigned")
    line = _line(db_session, well, product, quantity=4000)

    recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.UNCOVERED
    assert well.coverage_status == CoverageStatus.UNCOVERED.value
    # The reason must name the assignment, not blame the shelf -- there are
    # 10000 units on hand.
    assert "No inventory assigned to this demand line" in line.coverage_result.reason
    # Sanity: the very same data under SOFT would have been covered.
    node.customer.allocation_policy = AllocationPolicy.SOFT
    db_session.flush()
    recompute_well(db_session, well)
    assert _status(line) == CoverageStatus.COVERED


def test_hard_partial_assignment_gets_no_pool_top_up(db_session):
    _c, node = _customer(db_session, AllocationPolicy.HARD)
    product = _product(db_session, on_hand_qty=10000)
    well = _well(db_session, node, "Well Hard-Partial")
    line = _line(db_session, well, product, quantity=4000)
    _assign(db_session, line, product, 2500)

    recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.UNCOVERED
    assert "no top-up from the unassigned pool" in line.coverage_result.reason


# --------------------------------------------------------------------------
# HYBRID
# --------------------------------------------------------------------------


def test_hybrid_assignment_alone_sufficient_is_covered(db_session):
    _c, node = _customer(db_session, AllocationPolicy.HYBRID)
    product = _product(db_session, on_hand_qty=4000)
    well = _well(db_session, node, "Well Hybrid-Assigned")
    line = _line(db_session, well, product, quantity=4000)
    _assign(db_session, line, product, 4000)

    recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.COVERED


def test_hybrid_partial_assignment_tops_up_from_pool(db_session):
    """Assigned first, then the unassigned remainder of the pool."""
    _c, node = _customer(db_session, AllocationPolicy.HYBRID)
    # 2500 assigned + 1500 unassigned pool = exactly the 4000 required.
    product = _product(db_session, on_hand_qty=4000)
    well = _well(db_session, node, "Well Hybrid-TopUp")
    line = _line(db_session, well, product, quantity=4000)
    _assign(db_session, line, product, 2500)

    recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.COVERED


def test_hybrid_partial_assignment_with_insufficient_pool_is_uncovered(db_session):
    """Partial cover is still NOT cover -- but the pool IS now drawn.

    This test used to pin the all-or-nothing rule: with a 1500 shortfall against a
    1000 pool, nothing was drawn at all. The owner reversed that, so it now pins
    the replacement. The verdict assertion is unchanged (UNCOVERED, and the reason
    still names the assignment), and two assertions are ADDED: that the available
    1000 was actually consumed, and that the reason says so. Nothing was relaxed --
    the test checks strictly more of the engine's behaviour than before.
    """
    _c, node = _customer(db_session, AllocationPolicy.HYBRID)
    # 2500 assigned leaves only 1000 unassigned against a 1500 shortfall.
    product = _product(db_session, on_hand_qty=3500)
    well = _well(db_session, node, "Well Hybrid-Short")
    line = _line(db_session, well, product, quantity=4000)
    _assign(db_session, line, product, 2500)

    recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.UNCOVERED
    assert "Assigned 2500 of 4000" in line.coverage_result.reason
    # The 1000 that existed was drawn in ROS order even though it cannot finish
    # the line, and the reason states the residual shortfall honestly.
    reason = line.coverage_result.reason
    assert "provided a further 1000" in reason
    assert "still short by 500" in reason


def test_hybrid_residual_pool_goes_to_earliest_ros(db_session):
    _c, node = _customer(db_session, AllocationPolicy.HYBRID)
    # Each line has 1000 assigned; 1000 unassigned is left for a 1000 shortfall
    # that BOTH lines have. Only the earlier ROS can win it.
    product = _product(db_session, on_hand_qty=3000)
    well = _well(db_session, node, "Well Hybrid-Compete")
    early = _line(db_session, well, product, quantity=2000, days_out=380)
    late = _line(db_session, well, product, quantity=2000, days_out=420)
    _assign(db_session, early, product, 1000)
    _assign(db_session, late, product, 1000)

    recompute_well(db_session, well)

    assert _status(early) == CoverageStatus.COVERED
    assert _status(late) == CoverageStatus.UNCOVERED
    assert well.coverage_status == CoverageStatus.UNCOVERED.value


def test_hybrid_no_assignments_behaves_like_pooled_ros_allocation(db_session):
    """A hybrid customer with nothing assigned still gets ROS-ordered access to
    the whole pool -- hybrid degrades gracefully, it does not degrade to hard."""
    _c, node = _customer(db_session, AllocationPolicy.HYBRID)
    product = _product(db_session, on_hand_qty=3000)
    well = _well(db_session, node, "Well Hybrid-Nothing")
    early = _line(db_session, well, product, quantity=2000, days_out=380)
    late = _line(db_session, well, product, quantity=2000, days_out=420)

    recompute_well(db_session, well)

    assert _status(early) == CoverageStatus.COVERED
    assert _status(late) == CoverageStatus.UNCOVERED


# --------------------------------------------------------------------------
# Partial consumption by earliest ROS (the reversed all-or-nothing rule)
# --------------------------------------------------------------------------


def test_hybrid_owners_worked_example_earlier_ros_draws_pool_it_cannot_complete(
    db_session,
):
    """The exact case the product owner ruled on. Unassigned pool 2000:

        Line A  ROS Sep-1  needs 5000, assigned 1000  -> shortfall 4000
        Line B  ROS Oct-1  needs 2000, assigned    0  -> shortfall 2000

    Ruling: A consumes the pool in ROS order and B goes short. A stays Uncovered
    (still 3000 short) but the steel is drawn -- physically the earlier well gets
    the pipe.
    """
    _c, node = _customer(db_session, AllocationPolicy.HYBRID)
    # 3000 on hand, 1000 of it assigned to A -> unassigned pool is 2000.
    product = _product(db_session, on_hand_qty=3000)
    well_a = _well(db_session, node, "Well Sep-1")
    well_b = _well(db_session, node, "Well Oct-1")
    line_a = _line(db_session, well_a, product, quantity=5000, days_out=380)
    line_b = _line(db_session, well_b, product, quantity=2000, days_out=410)
    _assign(db_session, line_a, product, 1000)

    outcome = allocate_detailed(
        AllocationPolicy.HYBRID, [line_a, line_b], 3000, {line_a.id: 1000}
    )

    # A drew everything it could -- its own 1000 plus the whole 2000 pool.
    assert outcome.consumed_from_assignment[line_a.id] == 1000
    assert outcome.consumed_from_pool[line_a.id] == 2000
    assert outcome.drawn(line_a.id) == 3000
    # ...and is STILL uncovered, 3000 short of its 5000.
    assert outcome.covered[line_a.id] is False

    # B is short: the pool it could have used entirely is gone.
    assert outcome.covered[line_b.id] is False
    assert outcome.drawn(line_b.id) == 0.0

    # Nothing left, nothing negative, nothing double-counted.
    assert outcome.remaining_pool == 0.0

    # And the same answer through the persisting engine, end to end.
    recompute_well(db_session, well_a)
    assert _status(line_a) == CoverageStatus.UNCOVERED
    assert _status(line_b) == CoverageStatus.UNCOVERED


def test_soft_partial_consumption_draws_what_it_can_in_ros_order(db_session):
    """SOFT behaves identically -- the same physical situation with no assignment.

    Leaving SOFT all-or-nothing would make "soft allocation" mean two different
    things depending on the customer's policy.
    """
    _c, node = _customer(db_session, AllocationPolicy.SOFT)
    product = _product(db_session, on_hand_qty=2000)
    well = _well(db_session, node, "Well Soft-Partial")
    early = _line(db_session, well, product, quantity=5000, days_out=380)
    late = _line(db_session, well, product, quantity=2000, days_out=410)

    outcome = allocate_detailed(AllocationPolicy.SOFT, [early, late], 2000)

    assert outcome.consumed_from_pool[early.id] == 2000
    assert outcome.covered[early.id] is False
    assert outcome.drawn(late.id) == 0.0
    assert outcome.covered[late.id] is False
    assert outcome.remaining_pool == 0.0

    recompute_well(db_session, well)
    assert _status(early) == CoverageStatus.UNCOVERED
    assert _status(late) == CoverageStatus.UNCOVERED
    # The reason states where the steel went, so a planner is not left guessing.
    assert "2000 of 5000 drawn from the pool" in early.coverage_result.reason
    assert "still short by 3000" in early.coverage_result.reason


def test_remaining_pool_is_exact_after_a_chain_of_partial_draws(db_session):
    """`remaining_pool` never goes negative and never double-counts.

    Three lines, one 2500 pool, each taking `min(shortfall, pool)` in ROS order.
    The residual is what the substitution fall-through and
    app.engines.sharing's surplus both depend on, so it is asserted exactly.
    """
    _c, node = _customer(db_session, AllocationPolicy.SOFT)
    product = _product(db_session, on_hand_qty=2500)
    well = _well(db_session, node, "Well Chain")
    first = _line(db_session, well, product, quantity=1000, days_out=370)
    second = _line(db_session, well, product, quantity=4000, days_out=390)
    third = _line(db_session, well, product, quantity=1000, days_out=410)

    outcome = allocate_detailed(AllocationPolicy.SOFT, [first, second, third], 2500)

    assert outcome.consumed_from_pool[first.id] == 1000   # covered outright
    assert outcome.consumed_from_pool[second.id] == 1500  # partial, drains the rest
    assert third.id not in outcome.consumed_from_pool     # nothing left
    assert outcome.covered == {first.id: True, second.id: False, third.id: False}
    # Exactly the pool, spent once: 1000 + 1500 + 0 == 2500.
    total_drawn = sum(outcome.consumed_from_pool.values())
    assert total_drawn == 2500
    assert outcome.remaining_pool == 0.0
    assert outcome.remaining_pool >= 0.0

    # A pool larger than total demand leaves the honest remainder.
    roomy = allocate_detailed(AllocationPolicy.SOFT, [first, second, third], 10000)
    assert roomy.remaining_pool == 10000 - (1000 + 4000 + 1000)


def test_partial_consumption_can_reduce_the_covered_well_count(db_session):
    """THE ACCEPTED TRADE-OFF, asserted so nobody "fixes" it later.

    Under the old all-or-nothing rule the earlier line drew nothing, so the later
    line was Covered and the customer had ONE covered well. Under the owner's
    ruling the earlier line drains the pool it cannot complete with, and the
    covered-well count drops to ZERO.

    This is a deliberate consequence of the ruling, not a regression. If this test
    starts failing because the count went back up, the all-or-nothing rule has been
    reintroduced -- read app.engines.allocation's module docstring before changing
    anything here.
    """
    _c, node = _customer(db_session, AllocationPolicy.SOFT, name="Trade Off Co")
    product = _product(db_session, on_hand_qty=2000)
    hungry_well = _well(db_session, node, "Well Hungry")
    modest_well = _well(db_session, node, "Well Modest")
    hungry = _line(db_session, hungry_well, product, quantity=5000, days_out=380)
    modest = _line(db_session, modest_well, product, quantity=2000, days_out=410)

    recompute_well(db_session, hungry_well)

    # The modest well could have been covered outright by the 2000 on hand.
    assert modest.quantity == 2000
    assert _status(modest) == CoverageStatus.UNCOVERED
    assert _status(hungry) == CoverageStatus.UNCOVERED
    covered_wells = sum(
        1
        for well in (hungry_well, modest_well)
        if well.coverage_status == CoverageStatus.COVERED.value
    )
    assert covered_wells == 0, (
        "the earlier-ROS line must consume the pool it cannot complete with, even "
        "though that costs a covered well -- see app.engines.allocation"
    )


def test_hard_is_untouched_by_partial_consumption(db_session):
    """HARD gets no pool top-up, partial or otherwise."""
    _c, node = _customer(db_session, AllocationPolicy.HARD)
    product = _product(db_session, on_hand_qty=10000)
    well = _well(db_session, node, "Well Hard-NoPartial")
    line = _line(db_session, well, product, quantity=4000)
    _assign(db_session, line, product, 2500)

    outcome = allocate_detailed(
        AllocationPolicy.HARD, [line], 10000, {line.id: 2500}
    )

    assert outcome.covered[line.id] is False
    # Nothing drawn: not the pool (hard allocation has no top-up) and not the
    # assignment (it was never spent, so it stays reserved).
    assert outcome.drawn(line.id) == 0.0
    # 10000 on hand less the 2500 reserved to this line = 7500 free pool, which
    # remains available to the substitution fall-through and to nothing else.
    assert outcome.remaining_pool == 7500


# --------------------------------------------------------------------------
# SOFT regression guard + residual-pool accounting
# --------------------------------------------------------------------------


def test_soft_ignores_assignment_rows(db_session):
    """Regression guard, NARROWED to the fact that is still true: soft allocation
    pools inventory, so an assignment on one of THIS CUSTOMER'S OWN lines -- any of
    them, including a different well -- must not change its coverage.

    This test always described the own-assignment case (both wells hang off the
    same customer's node), and that case is unchanged: "do not tie my steel to
    specific wells of mine" is exactly what a soft customer is asking for. What
    changed is the FOREIGN case, which this test never covered and which
    `test_soft_is_constrained_by_another_customers_hard_assignment` below now pins.
    Split rather than edited, so both facts are asserted independently and neither
    can be lost to a future "simplification": nothing here was relaxed, and the
    engine is now pinned in strictly more places than before.
    """
    customer, node = _customer(db_session, AllocationPolicy.SOFT)
    product = _product(db_session, on_hand_qty=6000)
    well = _well(db_session, node, "Well Soft-01")
    other_well = _well(db_session, node, "Well Soft-02")
    line = _line(db_session, well, product, quantity=5000)
    other_line = _line(db_session, other_well, product, quantity=5000, days_out=500)
    # 6000 of 6000 nominally reserved -- to ANOTHER WELL OF THE SAME CUSTOMER, so
    # it is this customer's own steel and pooling still applies in full.
    _assign(db_session, other_line, product, 6000)
    assert other_line.well.planning_node.customer_id == customer.id

    recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.COVERED
    assert well.coverage_status == CoverageStatus.COVERED.value


# --------------------------------------------------------------------------
# SOFT vs. ANOTHER customer's hard assignment
#
# The platform never creates, releases or overrides a hard reservation: hard
# assignments are read-only Oracle facts. A soft customer's own-product allocation
# used to ignore assignment rows outright, so a soft line could be reported COVERED
# off steel hard-assigned to somebody ELSE's demand line -- the same violation the
# substitute path blocks, one level up. The distinction these tests fix is own vs.
# foreign, not SOFT vs. anything.
# --------------------------------------------------------------------------


def _foreign_claim(db_session, product, quantity, bu=None, name="Foreign Claimant"):
    """Hard-assign `quantity` of `product` to ANOTHER CUSTOMER's demand line.

    Another CUSTOMER, deliberately -- another well of our own would just be
    ordinary pooled demand (see `test_soft_ignores_assignment_rows`). `bu` defaults
    to the file's single Business Unit; pass a different one to check that the
    carve-out is still bounded by the BU.
    """
    claimant = Customer(
        name=name,
        allocation_policy=AllocationPolicy.HARD,
        business_unit_id=(bu or _default_bu(db_session)).id,
    )
    db_session.add(claimant)
    db_session.flush()
    node = PlanningNode(
        customer_id=claimant.id, node_type="Campaign", name=f"{name} Campaign"
    )
    db_session.add(node)
    db_session.flush()
    well = _well(db_session, node, f"Well {name}")
    line = _line(db_session, well, product, quantity=quantity, days_out=500)
    _assign(db_session, line, product, quantity)
    return claimant, line


def test_soft_is_constrained_by_another_customers_hard_assignment(db_session):
    """A SOFT line short ONLY because another customer holds a hard assignment.

    6000 on the shelf in this BU and a 5000 requirement: comfortably covered on the
    old rule. Every one of those 6000 is hard-assigned to a demand line belonging to
    a DIFFERENT customer in the same BU, so none of it is free, and the platform may
    not take it -- doing so would override an Oracle fact it does not own.
    """
    _c, node = _customer(db_session, AllocationPolicy.SOFT, name="Soft Claimee")
    product = _product(db_session, on_hand_qty=6000)
    well = _well(db_session, node, "Well Soft-Foreign")
    line = _line(db_session, well, product, quantity=5000)

    # Control: with nothing assigned anywhere, this is Covered.
    recompute_well(db_session, well)
    assert _status(line) == CoverageStatus.COVERED

    _foreign_claim(db_session, product, 6000)
    recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.UNCOVERED
    assert well.coverage_status == CoverageStatus.UNCOVERED.value

    reason = line.coverage_result.reason
    # The steel is ON THE SHELF, so blaming the shelf would be the same lie HARD's
    # wording exists to avoid. The reason names the assignment...
    assert "Insufficient on-hand inventory" not in reason
    assert "hard-assigned to another customer's demand line" in reason
    assert "6000" in reason
    # ...and, because releasing it genuinely would close the 5000 gap, points at
    # Oracle using the SAME action text the substitute path uses.
    assert RECOMMENDED_ACTION[BLOCK_ORACLE_RELEASE] in reason


def test_soft_reason_omits_the_oracle_action_when_release_would_not_help(db_session):
    """Naming Oracle is only honest when the release would actually close the gap.

    4000 reserved to another customer against a 9000 requirement: even releasing
    every unit leaves the line 5000 short, so sending a planner to argue about the
    assignment would waste the trip. The assignment is still NAMED -- it is a real
    part of the answer -- but no action is claimed for it.
    """
    _c, node = _customer(db_session, AllocationPolicy.SOFT, name="Soft Hopeless")
    product = _product(db_session, on_hand_qty=4000)
    well = _well(db_session, node, "Well Soft-Hopeless")
    line = _line(db_session, well, product, quantity=9000)
    _foreign_claim(db_session, product, 4000)

    recompute_well(db_session, well)

    reason = line.coverage_result.reason
    assert _status(line) == CoverageStatus.UNCOVERED
    assert "hard-assigned to another customer's demand line" in reason
    assert RECOMMENDED_ACTION[BLOCK_ORACLE_RELEASE] not in reason


def test_soft_partial_carve_out_leaves_the_unassigned_remainder_usable(db_session):
    """The carve-out is a subtraction, not a switch.

    6000 on hand, 2000 hard-assigned to another customer -> 4000 genuinely free.
    A 4000 line is still Covered (the reservation took only what it owns) and a
    5000 line is not, drawing the 4000 it can and saying so.
    """
    _c, node = _customer(db_session, AllocationPolicy.SOFT, name="Soft Partial Carve")
    product = _product(db_session, on_hand_qty=6000)
    fits = _well(db_session, node, "Well Soft-Fits")
    line_fits = _line(db_session, fits, product, quantity=4000)
    _foreign_claim(db_session, product, 2000)

    recompute_well(db_session, fits)
    assert _status(line_fits) == CoverageStatus.COVERED

    # Same pool, a bigger requirement: 4000 free against 5000 needed.
    line_fits.quantity = 5000
    db_session.flush()
    recompute_well(db_session, fits)
    assert _status(line_fits) == CoverageStatus.UNCOVERED
    reason = line_fits.coverage_result.reason
    assert "2000 of" in reason and "hard-assigned to another customer" in reason
    assert "4000 of 5000 drawn from the unassigned remainder" in reason
    assert "still short by 1000" in reason


def test_soft_is_not_constrained_by_a_foreign_assignment_in_another_bu(db_session):
    """The BU boundary still bounds the carve-out.

    A reservation made against a demand line in a DIFFERENT Business Unit draws on
    that BU's completely separate physical stock. Netting it off here would be a
    cross-BU leak in the pessimistic direction -- a line pushed Uncovered for no
    physical reason -- which is the bug `_assignment_context`'s scope restriction
    was introduced to fix. Widening SOFT's carve-out must not reopen it.
    """
    other_bu = BusinessUnit(name="Alloc BU Elsewhere")
    db_session.add(other_bu)
    db_session.flush()

    _c, node = _customer(db_session, AllocationPolicy.SOFT, name="Soft Cross BU")
    product = _product(db_session, on_hand_qty=6000)
    well = _well(db_session, node, "Well Soft-CrossBU")
    line = _line(db_session, well, product, quantity=5000)
    # Same product, same quantity, ANOTHER BU's customer.
    _foreign_claim(
        db_session, product, 6000, bu=other_bu, name="Elsewhere Claimant"
    )

    recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.COVERED
    # Control: the identical claim inside OUR BU does bite, so the test is passing
    # for the boundary and not because the carve-out is broken.
    _foreign_claim(db_session, product, 6000, name="Same BU Claimant")
    recompute_well(db_session, well)
    assert _status(line) == CoverageStatus.UNCOVERED


def test_foreign_assignment_is_not_excluded_twice_for_a_soft_substitute(db_session):
    """No double-exclusion between `reserved_by_product` and
    `_hard_assigned_for_substitutes`.

    Both maps now carry a foreign assignment on a substitute-target product under
    SOFT, and `compute_customer_coverage` combines them with `max`. If it ever
    summed them instead, the 3000 below would be netted off twice -- 5000 - 6000 ->
    0 free -- and the line would be reported short of material that is physically
    free. The margins are chosen so the two answers differ:

        substitute 5000 on hand, 3000 hard-assigned to another customer
        -> 2000 genuinely free, and the line needs exactly 2000.
    """
    customer, node = _customer(db_session, AllocationPolicy.SOFT, name="Soft Sub Once")
    primary = _product(db_session, on_hand_qty=0, grade="13CR80")
    substitute = _product(db_session, on_hand_qty=5000, grade="13CR110")

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

    well = _well(db_session, node, "Well Soft-SubOnce")
    line = _line(db_session, well, primary, quantity=2000)
    from app.engines.substitution import decide_approval, request_approval

    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    _foreign_claim(db_session, substitute, 3000, name="Sub Claimant")

    computed = recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.COVERED_VIA_SUBSTITUTE
    # Excluded exactly once: 3000, not 6000.
    assert computed.hard_assigned_by_product[substitute.id] == 3000


def test_assigned_inventory_is_not_reoffered_to_the_substitution_fallthrough(db_session):
    """Under HARD the substitute's stock is fully assigned to another customer's
    demand line, so it is NOT free pool -- the shortfall line must not be
    covered via substitute off the back of inventory somebody else owns."""
    customer, node = _customer(db_session, AllocationPolicy.HARD, name="Hard Sub Co")
    primary = _product(db_session, on_hand_qty=0, grade="13CR80")
    substitute = _product(db_session, on_hand_qty=9000, grade="13CR110")

    well = _well(db_session, node, "Well Hard-Sub")
    line = _line(db_session, well, primary, quantity=4000)

    # A fully approved substitution path -- only inventory availability is in
    # question here.
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
    from app.engines.substitution import decide_approval, request_approval

    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    # First: with the substitute stock free, the substitute closes the gap.
    recompute_well(db_session, well)
    assert _status(line) == CoverageStatus.COVERED_VIA_SUBSTITUTE

    # Now assign every unit of the substitute to a foreign demand line.
    foreign_well = _well(db_session, node, "Well Hard-Foreign")
    foreign_line = _line(db_session, foreign_well, substitute, quantity=9000, days_out=500)
    _assign(db_session, foreign_line, substitute, 9000)

    recompute_well(db_session, well)

    assert _status(line) != CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert _status(line) in (CoverageStatus.UNCOVERED, CoverageStatus.UNRECOVERABLE)


def test_own_assignment_consumed_once_is_not_reused_as_substitute_stock(db_session):
    """The quantity a hybrid line drew from the pool must be gone for the
    substitution fall-through in the same pass."""
    customer, node = _customer(db_session, AllocationPolicy.HYBRID, name="Hybrid Sub Co")
    primary = _product(db_session, on_hand_qty=2000, grade="13CR80")
    substitute = _product(db_session, on_hand_qty=2000, grade="13CR110")

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

    well = _well(db_session, node, "Well Hybrid-SubPool")
    # Two lines of 2000 each. Line A is covered outright by primary stock; line
    # B falls through to the substitute, whose 2000 is itself half assigned to
    # line A of a different product -- so only 2000 - 0 = 2000 is free and B is
    # covered. Assign 1500 of the substitute away and B must fail.
    line_a = _line(db_session, well, primary, quantity=2000, days_out=380)
    line_b = _line(db_session, well, primary, quantity=2000, days_out=420)
    from app.engines.substitution import decide_approval, request_approval

    approval = request_approval(db_session, line_b, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_well(db_session, well)
    assert _status(line_a) == CoverageStatus.COVERED
    assert _status(line_b) == CoverageStatus.COVERED_VIA_SUBSTITUTE

    _assign(db_session, line_a, substitute, 1500)
    recompute_well(db_session, well)

    # line_a still covered by its own primary stock; only 500 of the substitute
    # is free now, so line_b can no longer be covered via substitute.
    assert _status(line_a) == CoverageStatus.COVERED
    assert _status(line_b) != CoverageStatus.COVERED_VIA_SUBSTITUTE
