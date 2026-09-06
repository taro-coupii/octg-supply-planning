from datetime import datetime, timedelta

from app.engines.coverage import recompute_well
from app.engines.order_dates import months_to_timedelta
from app.engines.substitution import (
    BLOCK_CUSTOMER,
    BLOCK_INVENTORY,
    BLOCK_WELL_APPROVAL,
    approval_by_date,
    decide_approval,
    find_candidates,
    request_approval,
)
from app.models import (
    ANY_ATTRIBUTE_VALUE,
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
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    UnitOfMeasure,
    Well,
    AllocationPolicy,
)


def _scenario(
    db_session,
    primary_on_hand: float = 1000,
    substitute_on_hand: float = 5000,
    line_qty: float = 4000,
):
    """Well with one CONFIRMED/PRIMARY line whose own product cannot cover it,
    plus a separate product available as a potential substitute."""
    # One Business Unit, because on-hand quantity exists only per
    # (BU, product) now -- an unmapped customer has no pool at all and
    # coverage raises for it (see app.engines.inventory).
    bu = BusinessUnit(name="Sub BU")
    db_session.add(bu)
    db_session.flush()
    customer = Customer(
        name="Sub Test Co",
        allocation_policy=AllocationPolicy.SOFT,
        business_unit_id=bu.id,
    )
    db_session.add(customer)
    db_session.flush()

    node = PlanningNode(customer_id=customer.id, node_type="Campaign", name="Sub Campaign")
    db_session.add(node)
    db_session.flush()

    primary = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="CSG", size="9-5/8", weight=53.5, grade="P110", grade_type="Carbon",
        connection="VAM 21", description="CSG 9-5/8 53.5 P110 VAM 21",
    )
    substitute = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="CSG", size="9-5/8", weight=58.4, grade="Q125", grade_type="Carbon",
        connection="VAM 21", description="CSG 9-5/8 58.4 Q125 VAM 21",
    )
    db_session.add_all([primary, substitute])
    db_session.flush()
    # The two quantities, written where quantity actually lives: one row per
    # (Business Unit, product). There is no unscoped column to put them on.
    db_session.add_all([
        InventoryOnHand(
            business_unit_id=bu.id, product_id=primary.id,
            quantity=primary_on_hand, source_system="synthetic",
        ),
        InventoryOnHand(
            business_unit_id=bu.id, product_id=substitute.id,
            quantity=substitute_on_hand, source_system="synthetic",
        ),
    ])
    db_session.flush()

    well = Well(planning_node_id=node.id, name="Well Sub-01", demand_status=DemandStatus.CONFIRMED)
    db_session.add(well)
    db_session.flush()

    line = DemandLine(
        well_id=well.id, product_id=primary.id, quantity=line_qty,
        ros_date=datetime.utcnow() + timedelta(days=20),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(line)
    db_session.flush()
    return customer, well, line, primary, substitute


def _add_technical(db_session, primary, substitute):
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.flush()


def _add_customer_rule(db_session, customer, primary, substitute, allowed=True):
    db_session.add(
        CustomerSubstitutionRule(
            customer_id=customer.id,
            from_product_id=primary.id,
            to_product_id=substitute.id,
            allowed=allowed,
        )
    )
    db_session.flush()


def test_all_three_layers_clear_gives_covered_via_substitute(db_session):
    customer, well, line, primary, substitute = _scenario(db_session)
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert substitute.description in line.coverage_result.reason
    assert well.coverage_status == CoverageStatus.COVERED.value

    candidate = find_candidates(db_session, line)[0]
    assert candidate.usable is True
    assert candidate.blocking_layer is None
    assert candidate.approval_status == SubstitutionApprovalStatus.APPROVED


def test_pending_well_approval_gives_pending_approval_and_well_not_covered(db_session):
    customer, well, line, primary, substitute = _scenario(db_session)
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    request_approval(db_session, line, primary.id, substitute.id)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.PENDING_APPROVAL
    assert well.coverage_status == CoverageStatus.UNCOVERED.value

    candidate = find_candidates(db_session, line)[0]
    assert candidate.usable is False
    assert candidate.blocking_layer == BLOCK_WELL_APPROVAL
    assert candidate.approval_status == SubstitutionApprovalStatus.PENDING


def test_never_requested_approval_also_gives_pending_approval(db_session):
    customer, well, line, primary, substitute = _scenario(db_session)
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.PENDING_APPROVAL

    candidate = find_candidates(db_session, line)[0]
    assert candidate.approval_status is None
    assert candidate.blocking_layer == BLOCK_WELL_APPROVAL


def test_customer_rule_disallowed_keeps_line_uncovered(db_session):
    customer, well, line, primary, substitute = _scenario(db_session)
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=False)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.UNCOVERED
    assert well.coverage_status == CoverageStatus.UNCOVERED.value

    candidate = find_candidates(db_session, line)[0]
    assert candidate.customer_allowed is False
    assert candidate.usable is False
    assert candidate.blocking_layer == BLOCK_CUSTOMER


def test_missing_technical_substitution_keeps_line_uncovered(db_session):
    customer, well, line, primary, substitute = _scenario(db_session)
    # Layers 2 and 3 clear but engineering never approved the pair.
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.UNCOVERED
    assert well.coverage_status == CoverageStatus.UNCOVERED.value
    assert find_candidates(db_session, line) == []


def test_insufficient_substitute_inventory_keeps_line_uncovered(db_session):
    customer, well, line, primary, substitute = _scenario(
        db_session, substitute_on_hand=500
    )
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.UNCOVERED
    assert well.coverage_status == CoverageStatus.UNCOVERED.value

    candidate = find_candidates(db_session, line)[0]
    assert candidate.usable is False
    assert candidate.blocking_layer == BLOCK_INVENTORY
    assert candidate.available_qty == 500
    assert candidate.required_qty == 4000


def test_rejected_approval_keeps_line_uncovered(db_session):
    customer, well, line, primary, substitute = _scenario(db_session)
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=False)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.UNCOVERED
    candidate = find_candidates(db_session, line)[0]
    assert candidate.approval_status == SubstitutionApprovalStatus.REJECTED
    assert candidate.blocking_layer == BLOCK_WELL_APPROVAL


def test_technical_substitution_is_directional(db_session):
    customer, well, line, primary, substitute = _scenario(db_session)
    # Only substitute -> primary is registered, so primary has no candidates.
    db_session.add(
        TechnicalSubstitution(from_product_id=substitute.id, to_product_id=primary.id)
    )
    db_session.flush()
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)

    assert find_candidates(db_session, line) == []


def test_substitute_inventory_not_double_counted_across_lines(db_session):
    """Two shortfall lines competing for one substitute pool: the earlier-ROS
    line wins, the later one must not re-spend the same qty."""
    customer, well, line_a, primary, substitute = _scenario(
        db_session, substitute_on_hand=5000, line_qty=4000
    )
    line_b = DemandLine(
        well_id=well.id, product_id=primary.id, quantity=4000,
        ros_date=line_a.ros_date + timedelta(days=10),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(line_b)
    db_session.flush()

    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    for line in (line_a, line_b):
        approval = request_approval(db_session, line, primary.id, substitute.id)
        decide_approval(db_session, approval.id, approved=True)

    recompute_well(db_session, well)

    assert line_a.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert line_b.coverage_result.status == CoverageStatus.UNCOVERED
    assert well.coverage_status == CoverageStatus.UNCOVERED.value


def test_re_request_after_approval_does_not_uncover_line(db_session):
    customer, well, line, primary, substitute = _scenario(db_session)
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    again = request_approval(db_session, line, primary.id, substitute.id)
    assert again.id == approval.id
    assert again.status == SubstitutionApprovalStatus.APPROVED

    recompute_well(db_session, well)
    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE


def test_request_approval_is_idempotent_while_pending(db_session):
    customer, well, line, primary, substitute = _scenario(db_session)
    first = request_approval(db_session, line, primary.id, substitute.id)
    second = request_approval(db_session, line, primary.id, substitute.id)
    assert first.id == second.id
    assert second.status == SubstitutionApprovalStatus.PENDING
    assert second.decided_at is None


# --------------------------------------------------------------------------
# PendingApproval must not over-promise one substitute quantity -- and, since the
# owner reversed the reservation, must SAY SO instead of reserving.
# --------------------------------------------------------------------------


def test_pending_approval_over_subscription_is_visible_and_reserves_nothing(db_session):
    """Two shortfall lines, one substitute pool big enough for only ONE of them.

    This test exists because of a real defect: two lines both reporting
    PendingApproval against one quantity, so approving both silently reverted one
    to Uncovered. It was originally fixed by RESERVING the quantity for the
    earliest-ROS line. The product owner has reversed that fix -- reserving is the
    platform creating a hard reservation, which it must never do
    (「OCTG Platform上でハード割り当てはできない」).

    So the test now pins the replacement, not the removal. It asserts MORE than
    before, and nothing it used to assert has been dropped:

      * the substitute quantity is NOT consumed by a pending line (new -- the
        behaviour the owner asked for, previously the opposite);
      * BOTH lines are told the substitute could close their gap, because with
        nothing reserved that is the truth for both (changed verdict);
      * the over-subscription is VISIBLE on every affected line -- the count, the
        combined requirement, the available quantity and the shortfall -- so a
        planner can see up front that approving all of them cannot succeed. This
        is what keeps the original defect fixed: the silent revert is no longer
        silent.
      * still nothing is drawn: fulfilled_by_product_id stays None (unchanged).
    """
    customer, well, line_a, primary, substitute = _scenario(
        db_session, primary_on_hand=0, substitute_on_hand=5000, line_qty=4000
    )
    line_b = DemandLine(
        well_id=well.id, product_id=primary.id, quantity=4000,
        ros_date=line_a.ros_date + timedelta(days=10),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(line_b)
    db_session.flush()

    # Layers 1 and 2 clear for both lines; layer 3 is never requested, which is
    # the PendingApproval path.
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)

    computed = recompute_well(db_session, well)

    # A pending substitute holds nothing back, so BOTH lines are pending.
    assert line_a.coverage_result.status == CoverageStatus.PENDING_APPROVAL
    assert line_b.coverage_result.status == CoverageStatus.PENDING_APPROVAL

    # ...and the over-subscription is stated on BOTH of them, with the arithmetic.
    for line in (line_a, line_b):
        reason = line.coverage_result.reason
        assert "OVER-SUBSCRIBED" in reason
        assert "2 demand lines" in reason
        assert "8000" in reason  # combined requirement
        assert "5000" in reason  # available
        assert "short by 3000" in reason

    # The engine's structured view of the same fact, for the API payload.
    (load,) = computed.pending_substitute_load
    assert load.to_product_id == substitute.id
    assert load.pending_line_count == 2
    assert set(load.pending_line_ids) == {line_a.id, line_b.id}
    assert load.pending_required_qty == 8000
    assert load.available_qty == 5000
    assert load.over_subscribed is True
    assert load.shortfall == 3000

    # Nothing has actually been drawn for either line.
    assert line_a.coverage_result.fulfilled_by_product_id is None
    assert line_b.coverage_result.fulfilled_by_product_id is None


def test_pending_approval_still_granted_when_the_pool_covers_both(db_session):
    """Complement: no false scarcity is manufactured. With enough substitute stock
    for both lines, both keep their PendingApproval and NOTHING is flagged."""
    customer, well, line_a, primary, substitute = _scenario(
        db_session, primary_on_hand=0, substitute_on_hand=8000, line_qty=4000
    )
    line_b = DemandLine(
        well_id=well.id, product_id=primary.id, quantity=4000,
        ros_date=line_a.ros_date + timedelta(days=10),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(line_b)
    db_session.flush()
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)

    computed = recompute_well(db_session, well)

    assert line_a.coverage_result.status == CoverageStatus.PENDING_APPROVAL
    assert line_b.coverage_result.status == CoverageStatus.PENDING_APPROVAL

    (load,) = computed.pending_substitute_load
    assert load.pending_required_qty == 8000
    assert load.available_qty == 8000
    assert load.over_subscribed is False
    assert load.note is None
    for line in (line_a, line_b):
        assert "OVER-SUBSCRIBED" not in line.coverage_result.reason


def test_pending_substitute_consumes_nothing_so_a_later_approved_line_can_use_it(
    db_session,
):
    """A pending substitute holds NOTHING back -- stated as a consequence.

    Line A (earlier ROS) is pending approval. Line B (later ROS) is fully approved.
    There is only enough substitute stock for one of them. Under the reversed rule
    the pending line reserves nothing, so B's APPROVED claim on real steel wins and
    B is covered via substitute. The old reserving behaviour would have handed the
    quantity to A -- which had no approval and therefore no right to it -- and left
    B uncovered.
    """
    customer, well, line_a, primary, substitute = _scenario(
        db_session, primary_on_hand=0, substitute_on_hand=5000, line_qty=4000
    )
    line_b = DemandLine(
        well_id=well.id, product_id=primary.id, quantity=4000,
        ros_date=line_a.ros_date + timedelta(days=10),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(line_b)
    db_session.flush()
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    # Only B gets a real approval.
    approval = request_approval(db_session, line_b, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_well(db_session, well)

    assert line_a.coverage_result.status == CoverageStatus.PENDING_APPROVAL
    assert line_b.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert line_b.coverage_result.fulfilled_by_product_id == substitute.id
    # A promised nothing and drew nothing.
    assert line_a.coverage_result.fulfilled_by_product_id is None


# --------------------------------------------------------------------------
# Hard-assigned stock is a candidate, but blocked pending an Oracle release
# --------------------------------------------------------------------------


def _assign_away(db_session, customer, substitute, quantity, days_out=500):
    """Hard-assign every unit of `substitute` to ANOTHER CUSTOMER's demand line.

    Another customer in the SAME Business Unit, which is the situation the owner
    ruled on: the steel is on our BU's shelf, and it is spoken for by somebody
    else's demand. A different customer (rather than another well of our own) is
    what makes the assignment genuinely foreign -- our own pool would simply
    consume it as ordinary demand.
    """
    from app.models import InventoryAssignment

    other_customer = Customer(
        name=f"Other Claimant {quantity:g}",
        allocation_policy=AllocationPolicy.HARD,
        business_unit_id=customer.business_unit_id,
    )
    db_session.add(other_customer)
    db_session.flush()
    other_node = PlanningNode(
        customer_id=other_customer.id, node_type="Campaign", name="Other Campaign"
    )
    db_session.add(other_node)
    db_session.flush()
    other_well = Well(planning_node_id=other_node.id, name="Well Other-Claim", demand_status=DemandStatus.CONFIRMED)
    db_session.add(other_well)
    db_session.flush()
    other_line = DemandLine(
        well_id=other_well.id, product_id=substitute.id, quantity=quantity,
        ros_date=datetime.utcnow() + timedelta(days=days_out),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(other_line)
    db_session.flush()
    db_session.add(
        InventoryAssignment(
            demand_line_id=other_line.id, product_id=substitute.id,
            quantity=quantity, source_system="synthetic",
        )
    )
    db_session.flush()
    return other_line


def test_soft_customer_is_offered_a_hard_assigned_substitute_but_blocked(db_session):
    """The change-3 hole, closed. Customer is SOFT, so assignments used to be
    invisible to it -- and it was reported COVERED_VIA_SUBSTITUTE off stock
    hard-assigned to somebody else's demand line, i.e. the platform silently
    overriding a hard reservation.

    The owner's ruling: OFFER it as a candidate, never take it. So the candidate is
    present, its blocking layer names the Oracle release, and the coverage verdict
    is never COVERED_VIA_SUBSTITUTE.
    """
    from app.engines.substitution import BLOCK_ORACLE_RELEASE

    customer, well, line, primary, substitute = _scenario(
        db_session, primary_on_hand=0, substitute_on_hand=9000, line_qty=4000
    )
    assert customer.allocation_policy == AllocationPolicy.SOFT
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    # Baseline: with the substitute stock free, it covers the line.
    recompute_well(db_session, well)
    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE

    # Now every unit is hard-assigned to another demand line.
    _assign_away(db_session, customer, substitute, 9000)

    computed = recompute_well(db_session, well)

    # NEVER covered via substitute -- the platform may not override the reservation.
    assert line.coverage_result.status != CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert line.coverage_result.status in (
        CoverageStatus.UNCOVERED,
        CoverageStatus.UNRECOVERABLE,
    )
    reason = line.coverage_result.reason
    assert "hard-assigned to another demand line" in reason
    assert "Oracle" in reason

    # ...but the candidate is OFFERED, not hidden, with the Oracle action on it.
    candidates = find_candidates(
        db_session,
        line,
        hard_assigned_by_product=computed.hard_assigned_by_product,
    )
    (candidate,) = candidates
    assert candidate.to_product_id == substitute.id
    assert candidate.usable is False
    assert candidate.blocking_layer == BLOCK_ORACLE_RELEASE
    assert candidate.hard_assigned_qty == 9000
    assert "Oracle" in candidate.recommended_action
    # The technical and customer layers are still reported as clear -- this is an
    # inventory-ownership block, not a permission one.
    assert candidate.customer_allowed is True
    assert candidate.approval_status == SubstitutionApprovalStatus.APPROVED


def test_genuinely_absent_substitute_stock_is_still_insufficient_inventory(db_session):
    """The new layer must not swallow the old one. With NOTHING assigned anywhere,
    a short substitute is still `insufficient-inventory` -- the action is to obtain
    material, not to phone Oracle about an assignment that does not exist."""
    from app.engines.substitution import BLOCK_ORACLE_RELEASE

    customer, well, line, primary, substitute = _scenario(
        db_session, primary_on_hand=0, substitute_on_hand=500, line_qty=4000
    )
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_well(db_session, well)

    (candidate,) = find_candidates(db_session, line)
    assert candidate.blocking_layer == BLOCK_INVENTORY
    assert candidate.blocking_layer != BLOCK_ORACLE_RELEASE
    assert candidate.hard_assigned_qty == 0.0
    assert "Order or transfer" in candidate.recommended_action


def test_a_release_that_would_not_close_the_gap_is_not_reported_as_releasable(
    db_session,
):
    """Naming the Oracle step is only honest when releasing would actually help.

    1000 of the substitute exists and ALL of it is assigned elsewhere, against a
    4000 requirement. Releasing every unit still leaves the line 3000 short, so the
    answer is insufficient-inventory -- sending the planner to Oracle would waste
    their time and still not cover the well.
    """
    from app.engines.substitution import BLOCK_ORACLE_RELEASE

    customer, well, line, primary, substitute = _scenario(
        db_session, primary_on_hand=0, substitute_on_hand=1000, line_qty=4000
    )
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)
    _assign_away(db_session, customer, substitute, 1000)

    computed = recompute_well(db_session, well)

    (candidate,) = find_candidates(
        db_session,
        line,
        hard_assigned_by_product=computed.hard_assigned_by_product,
    )
    assert candidate.blocking_layer == BLOCK_INVENTORY
    assert candidate.blocking_layer != BLOCK_ORACLE_RELEASE


def test_blocking_layer_priority_order_is_pinned(db_session):
    """The layers are conceptually independent but `blocking_layer` is one value,
    so the reported order is a real decision -- pinned here and explained in
    app.engines.substitution's module docstring."""
    from app.engines.substitution import (
        BLOCK_ORACLE_RELEASE,
        BLOCK_PRIORITY,
    )

    assert BLOCK_PRIORITY == (
        BLOCK_CUSTOMER,
        BLOCK_WELL_APPROVAL,
        BLOCK_ORACLE_RELEASE,
        BLOCK_INVENTORY,
    )

    # A substitute that is hard-assigned elsewhere AND lacks customer permission
    # reports the CUSTOMER layer: no point asking Oracle to free steel nobody is
    # allowed to use.
    customer, well, line, primary, substitute = _scenario(
        db_session, primary_on_hand=0, substitute_on_hand=9000, line_qty=4000
    )
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=False)
    _assign_away(db_session, customer, substitute, 9000)

    computed = recompute_well(db_session, well)
    (candidate,) = find_candidates(
        db_session,
        line,
        hard_assigned_by_product=computed.hard_assigned_by_product,
    )
    assert candidate.blocking_layer == BLOCK_CUSTOMER

    # Permit it, and the next layer down is well-approval -- still not Oracle.
    rule = db_session.query(CustomerSubstitutionRule).one()
    rule.allowed = True
    db_session.flush()
    computed = recompute_well(db_session, well)
    (candidate,) = find_candidates(
        db_session,
        line,
        hard_assigned_by_product=computed.hard_assigned_by_product,
    )
    assert candidate.blocking_layer == BLOCK_WELL_APPROVAL

    # Approve it, and only now does the Oracle release surface.
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)
    computed = recompute_well(db_session, well)
    (candidate,) = find_candidates(
        db_session,
        line,
        hard_assigned_by_product=computed.hard_assigned_by_product,
    )
    assert candidate.blocking_layer == BLOCK_ORACLE_RELEASE


def test_substitute_pool_is_not_double_spent_across_wells(db_session):
    """Defect 1's substitution half: the residual substitute pool is scoped to the
    CUSTOMER, so a substitute quantity spent for one well's line is gone for every
    other well's line too -- not just for other lines in the same well."""
    customer, well_a, line_a, primary, substitute = _scenario(
        db_session, primary_on_hand=0, substitute_on_hand=5000, line_qty=4000
    )
    well_b = Well(planning_node_id=well_a.planning_node_id, name="Well Sub-02", demand_status=DemandStatus.CONFIRMED)
    db_session.add(well_b)
    db_session.flush()
    line_b = DemandLine(
        well_id=well_b.id, product_id=primary.id, quantity=4000,
        ros_date=line_a.ros_date + timedelta(days=10),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(line_b)
    db_session.flush()

    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    for line in (line_a, line_b):
        approval = request_approval(db_session, line, primary.id, substitute.id)
        decide_approval(db_session, approval.id, approved=True)

    # Trigger on well B -- the later-ROS one. Earliest ROS must still win.
    recompute_well(db_session, well_b)

    assert line_a.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert line_a.coverage_result.fulfilled_by_product_id == substitute.id
    assert line_b.coverage_result.status != CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert well_a.coverage_status == CoverageStatus.COVERED.value
    assert well_b.coverage_status == CoverageStatus.UNCOVERED.value


# ---------------------------------------------------------------------------
# Phase 3: substitution approval-by date
# ---------------------------------------------------------------------------


def _seed_lead_time(db_session, months):
    """A complete, single-dimension-driven lead time totalling `months`, so
    `order_feasibility` has something other than 0/not-modelled to compute."""
    db_session.add_all([
        LeadTimeComponent(
            dimension=LeadTimeDimension.OD_WT,
            attribute_value=ANY_ATTRIBUTE_VALUE,
            months=months,
        ),
        LeadTimeComponent(
            dimension=LeadTimeDimension.GRADE,
            attribute_value=ANY_ATTRIBUTE_VALUE,
            months=0.0,
        ),
        LeadTimeComponent(
            dimension=LeadTimeDimension.CONNECTION,
            attribute_value=ANY_ATTRIBUTE_VALUE,
            months=0.0,
        ),
        LeadTimeComponent(
            dimension=LeadTimeDimension.LOGISTICS,
            attribute_value=ANY_ATTRIBUTE_VALUE,
            months=0.0,
        ),
    ])
    db_session.flush()


def _pending(db_session, line, primary, substitute, customer):
    """Put `line` into PendingApproval: technical + customer clear, well-layer
    request left open (never decided)."""
    _add_technical(db_session, primary, substitute)
    _add_customer_rule(db_session, customer, primary, substitute, allowed=True)
    request_approval(db_session, line, primary.id, substitute.id)


def test_approval_by_date_computed_from_primary_products_lead_time(db_session):
    """The ordinary case: ROS is comfortably outside the primary product's lead
    time, so a rejection could still be recovered by ordering the primary."""
    customer, well, line, primary, substitute = _scenario(db_session)
    # `_scenario`'s default ROS (+20 days) is too tight for any nonzero lead
    # time to leave room to still be recoverable -- push it out for this case.
    line.ros_date = datetime.utcnow() + timedelta(days=365)
    db_session.flush()
    _seed_lead_time(db_session, months=4.0)
    _pending(db_session, line, primary, substitute, customer)
    recompute_well(db_session, well)
    assert line.coverage_result.status == CoverageStatus.PENDING_APPROVAL

    result = approval_by_date(db_session, line)

    assert result.applicable is True
    assert result.available is True
    assert result.still_recoverable is True
    # Computed via the SAME order_dates.order_feasibility as MRP -- not a
    # second calculation.
    from app.engines.order_dates import order_feasibility

    _ship_by, expected_order_by, expected_months = order_feasibility(
        db_session, primary, line.ros_date
    )
    assert result.approval_by_date == expected_order_by
    assert expected_months == 4.0
    assert str(expected_order_by.isoformat()) in result.reason


def test_approval_by_date_unmodelled_lead_time_is_unavailable_not_a_guess(
    db_session,
):
    """No lead-time components at all for the primary product -> unavailable,
    never a fabricated date."""
    customer, well, line, primary, substitute = _scenario(db_session)
    # Deliberately no `_seed_lead_time` call: the primary product's lead time
    # is not modelled.
    _pending(db_session, line, primary, substitute, customer)
    recompute_well(db_session, well)
    assert line.coverage_result.status == CoverageStatus.PENDING_APPROVAL

    result = approval_by_date(db_session, line)

    assert result.applicable is True
    assert result.available is False
    assert result.approval_by_date is None
    assert result.still_recoverable is None
    assert "not modelled" in result.reason


def test_approval_by_date_already_past_recoverable_window_says_so_distinctly(
    db_session,
):
    """ROS is INSIDE the primary product's lead time even measured from today --
    mill fallback is already impossible, and the message must say that plainly,
    not merely "still has time" with an early date."""
    customer, well, line, primary, substitute = _scenario(db_session)
    # 24 months lead time against a line only 20 days out -- unrecoverable by
    # the primary product regardless of the substitution's own outcome.
    _seed_lead_time(db_session, months=24.0)
    _pending(db_session, line, primary, substitute, customer)
    recompute_well(db_session, well)
    assert line.coverage_result.status == CoverageStatus.PENDING_APPROVAL

    result = approval_by_date(db_session, line)

    assert result.applicable is True
    assert result.available is True
    assert result.still_recoverable is False
    assert "ALREADY IMPOSSIBLE" in result.reason
    # Distinct from the "still has time" wording used in the recoverable case.
    assert "Reject by" not in result.reason


def test_approval_by_date_not_applicable_when_line_is_not_pending(db_session):
    """A line with no substitution pursuit at all (COVERED by its own product)
    reports available=False with applicable=False, not a fabricated date."""
    customer, well, line, primary, substitute = _scenario(
        db_session, primary_on_hand=10_000
    )
    _seed_lead_time(db_session, months=4.0)
    # No technical/customer/approval rows at all -- the line's own product
    # covers it outright.
    recompute_well(db_session, well)
    assert line.coverage_result.status == CoverageStatus.COVERED

    result = approval_by_date(db_session, line)

    assert result.applicable is False
    assert result.available is False
    assert result.approval_by_date is None
    assert "not currently pursuing a substitution" in result.reason


def test_approval_by_date_also_not_applicable_for_uncovered_or_unrecoverable(
    db_session,
):
    """UNCOVERED (own product short, no usable/pending substitute at all) is
    likewise not "pursuing a substitution" -- no candidate ever reached the
    well-layer approval step."""
    customer, well, line, primary, substitute = _scenario(db_session)
    _seed_lead_time(db_session, months=4.0)
    # No technical substitution at all: `find_candidates` returns [] entirely
    # and the line falls straight to UNCOVERED/UNRECOVERABLE.
    recompute_well(db_session, well)
    assert line.coverage_result.status in (
        CoverageStatus.UNCOVERED,
        CoverageStatus.UNRECOVERABLE,
    )

    result = approval_by_date(db_session, line)
    assert result.applicable is False
    assert result.available is False


def test_approval_by_date_exposed_on_the_candidates_payload(db_session):
    """The API-level exposure: every row of the candidates list carries the
    SAME line-level approval-by date, available flag and reason."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    customer, well, line, primary, substitute = _scenario(db_session)
    _seed_lead_time(db_session, months=4.0)
    _pending(db_session, line, primary, substitute, customer)
    recompute_well(db_session, well)

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        client = TestClient(app)
        resp = client.get(f"/demand-lines/{line.id}/substitution-candidates")
        assert resp.status_code == 200
        rows = resp.json()
        assert rows, "expected at least one candidate"
        for row in rows:
            assert row["approval_by_date_available"] is True
            assert row["approval_by_date"] is not None
            # `still_recoverable` decides whether a client should render the
            # ordinary "reject by DATE" case or the more urgent "mill recovery is
            # ALREADY IMPOSSIBLE" case. It was computed by the engine but never
            # reached the wire until this assertion was added -- a frontend pass
            # discovered the gap by curling the live endpoint and finding the
            # field absent even though `app.engines.substitution` always sets it.
            assert row["still_recoverable"] is not None
            assert row["approval_by_date_reason"]
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_pending_approval_reason_composes_the_approval_by_date_clause(db_session):
    """`compute_customer_coverage`'s PendingApproval reason text carries the
    approve-by-date clause, composed onto (not duplicating) the base sentence."""
    customer, well, line, primary, substitute = _scenario(db_session)
    line.ros_date = datetime.utcnow() + timedelta(days=365)
    db_session.flush()
    _seed_lead_time(db_session, months=4.0)
    _pending(db_session, line, primary, substitute, customer)
    recompute_well(db_session, well)

    reason = line.coverage_result.reason
    assert "needs well-level approval" in reason
    assert "approve by" in reason
    assert "lose mill recovery" in reason

    from app.engines.order_dates import order_feasibility

    _ship_by, order_by, _months = order_feasibility(db_session, primary, line.ros_date)
    assert order_by.isoformat() in reason
