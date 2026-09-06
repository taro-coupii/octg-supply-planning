"""Coverage engine: rollup, the status/profile filter, the Uncovered /
Unrecoverable boundary, and customer-scope inventory pooling.

Lead-time components are seeded by `_make_customer` ON PURPOSE. Without them
`total_lead_time_months` is 0 and `is_recoverable` short-circuits to True before
any date comparison happens, which meant every `== UNCOVERED` assertion in this
file used to pass even if the date arithmetic were completely wrong. With a
COMPLETE component set present (OD/WT 4.5 + Grade 0 + Connection 0 + Logistics 2
= 6.5 months) the arithmetic is genuinely exercised, so demand lines here sit
comfortably beyond the lead time unless a test is specifically about the
boundary.

The set has to be complete on all four dimensions, not just the two terms that
carry months: components are now attribute-keyed and an INCOMPLETE set resolves
to "not modelled" (total 0), which would restore exactly the false-pass the
paragraph above describes. The two zero-month rows are therefore load-bearing.
"""

from datetime import date, datetime, timedelta

import pytest

from app.engines.coverage import (
    DEFAULT_PROFILE_FILTER,
    DEFAULT_STATUS_FILTER,
    apply_revision,
    recompute_customer,
    recompute_well,
    set_well_demand_status,
)
from app.engines.order_dates import months_to_timedelta
from app.models import (
    ANY_ATTRIBUTE_VALUE,
    BusinessUnit,
    CoverageResult,
    CoverageStatus,
    Customer,
    DemandLine,
    DemandProfile,
    DemandRevision,
    DemandStatus,
    ImpactRecord,
    InventoryOnHand,
    LeadTimeComponent,
    LeadTimeDimension,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
    AllocationPolicy,
)

EX_MILL_MONTHS = 4.5
SAILING_MONTHS = 2.0
TOTAL_MONTHS = EX_MILL_MONTHS + SAILING_MONTHS
# 6.5 months expressed in the same days the engine uses.
TOTAL_LEAD_DAYS = months_to_timedelta(TOTAL_MONTHS).days

# Comfortably outside the lead time, so coverage tests that are not about the
# recoverability boundary resolve UNCOVERED rather than UNRECOVERABLE.
FAR_ROS_DAYS = 400


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
        .filter(BusinessUnit.name == "Test BU")
        .one_or_none()
    )
    if bu is None:
        bu = BusinessUnit(name="Test BU")
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


def _make_customer(db_session, policy=AllocationPolicy.SOFT, name="Test Co"):
    customer = Customer(
        name=name,
        allocation_policy=policy,
        business_unit_id=_default_bu(db_session).id,
    )
    db_session.add(customer)
    db_session.flush()

    node = PlanningNode(customer_id=customer.id, node_type="Project", name="Test Project")
    db_session.add(node)
    db_session.flush()

    # See module docstring: these are what make is_recoverable actually compute.
    # A COMPLETE set of four dimensions, because an incomplete set now resolves
    # to "not modelled" (total 0) instead of being partially summed -- which
    # would put this file straight back into the state its docstring warns
    # about, every `== UNCOVERED` passing for the wrong reason.
    if db_session.query(LeadTimeComponent).first() is None:
        db_session.add_all([
            LeadTimeComponent(
                dimension=LeadTimeDimension.OD_WT,
                attribute_value=ANY_ATTRIBUTE_VALUE,
                months=EX_MILL_MONTHS,
                label="Ex-mill",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.GRADE, attribute_value="13CR", months=0.0
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
    return customer, node


def _make_product(db_session, on_hand_qty: float, grade="13CR80"):
    """A product PLUS its on-hand row in the default BU.

    `on_hand_qty` keeps its name and its meaning -- how much of this exists
    for the customers under test -- but it is now written where quantity
    actually lives, as InventoryOnHand(default BU, product).
    """
    product = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="TBG", size="4-1/2", grade=grade, grade_type="13CR",
        connection="VAM TOP", description=f"TBG 4-1/2 {grade} VAM TOP",
    )
    db_session.add(product)
    db_session.flush()
    _stock(db_session, _default_bu(db_session), product, on_hand_qty)
    return product


def _make_well(
    db_session, on_hand_qty: float, demand_status=DemandStatus.CONFIRMED
):
    """One customer, one product, one well -- the original fixture shape.

    `demand_status` is a WELL argument now (see `_well`).
    """
    _customer, node = _make_customer(db_session)
    product = _make_product(db_session, on_hand_qty)
    well = Well(
        planning_node_id=node.id, name="Test Well", demand_status=demand_status
    )
    db_session.add(well)
    db_session.flush()
    return well, product


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


def _line(
    db_session,
    well,
    product,
    quantity,
    days_out=FAR_ROS_DAYS,
    profile=DemandProfile.PRIMARY,
):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity,
        ros_date=datetime.utcnow() + timedelta(days=days_out),
        profile=profile,
    )
    db_session.add(line)
    db_session.flush()
    return line


# --------------------------------------------------------------------------
# Rollup
# --------------------------------------------------------------------------


def test_well_covered_when_all_lines_covered(db_session):
    well, product = _make_well(db_session, on_hand_qty=10000)
    line = _line(db_session, well, product, quantity=5000)

    recompute_well(db_session, well)

    assert well.coverage_status == CoverageStatus.COVERED.value
    assert line.coverage_result.status == CoverageStatus.COVERED


def test_well_uncovered_when_one_line_uncovered(db_session):
    well, product = _make_well(db_session, on_hand_qty=3000)
    covered_line = _line(db_session, well, product, quantity=1000, days_out=FAR_ROS_DAYS)
    uncovered_line = _line(
        db_session, well, product, quantity=5000, days_out=FAR_ROS_DAYS + 10
    )

    recompute_well(db_session, well)

    assert well.coverage_status == CoverageStatus.UNCOVERED.value
    assert covered_line.coverage_result.status == CoverageStatus.COVERED
    assert uncovered_line.coverage_result.status == CoverageStatus.UNCOVERED


def test_revision_recomputes_coverage_and_records_impact(db_session):
    well, product = _make_well(db_session, on_hand_qty=3000)
    line = _line(db_session, well, product, quantity=2000)
    recompute_well(db_session, well)
    assert well.coverage_status == CoverageStatus.COVERED.value

    impact = apply_revision(
        db_session, line, quantity=5000,
        ros_date=line.ros_date, profile=DemandProfile.PRIMARY,
    )

    assert well.coverage_status == CoverageStatus.UNCOVERED.value
    assert impact.coverage_before == CoverageStatus.COVERED.value
    assert impact.coverage_after == CoverageStatus.UNCOVERED.value
    assert impact.quantity_before == 2000
    assert impact.quantity_after == 5000
    assert line.current_revision_no == 2
    # TWO revisions, not one: the line's INITIAL state was recorded as revision 1
    # when it was created (app.models.demand._write_initial_revision) and this
    # change is revision 2. The old `== 1` was counting the same single write --
    # asserting the full history instead is strictly more than it checked, and it
    # pins the invariant apply_revision relies on: a revision numbered
    # current_revision_no always exists.
    assert [r.revision_no for r in line.revisions] == [1, 2]
    assert line.revisions[0].quantity == 2000  # as created
    assert line.revisions[1].quantity == 5000  # as revised


# --------------------------------------------------------------------------
# Demand-line history is complete from creation (change 4)
# --------------------------------------------------------------------------


def test_demand_line_gets_a_created_at_on_creation(db_session):
    """Without this the demand book's past cannot be reconstructed at all -- see
    app.engines.executive._state_as_of."""
    before = datetime.utcnow()
    well, product = _make_well(db_session, on_hand_qty=1000)
    line = _line(db_session, well, product, quantity=100)
    db_session.refresh(line)

    assert line.created_at is not None
    # A real clock reading, not a placeholder.
    assert before - timedelta(seconds=5) <= line.created_at <= datetime.utcnow() + timedelta(seconds=5)


def test_initial_state_is_written_as_revision_1_at_creation(db_session):
    """Revision 1 carries the line AS CREATED, so "existed then and never changed"
    is distinguishable from "did not exist yet".

    The invariant: a DemandRevision numbered `current_revision_no` always exists.
    That is what app.engines.coverage.apply_revision already assumes when it
    appends `current_revision_no + 1`.

    The revision's STATUS comes from the line's WELL, which is where demand status
    lives -- so the well is created Budgeted here and the assertion below is
    unchanged. It also pins that the after-insert listener really reads the well
    rather than defaulting: a listener that wrote PLANNED would fail.
    """
    well, product = _make_well(
        db_session, on_hand_qty=1000, demand_status=DemandStatus.BUDGETED
    )
    ros = datetime.utcnow() + timedelta(days=FAR_ROS_DAYS)
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=250, ros_date=ros,
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(line)
    db_session.flush()
    db_session.refresh(line)

    (revision,) = line.revisions
    assert revision.revision_no == line.current_revision_no == 1
    assert revision.quantity == 250
    assert revision.ros_date == ros
    assert revision.status == DemandStatus.BUDGETED
    assert revision.profile == DemandProfile.PRIMARY
    assert revision.created_at is not None


def test_a_line_created_at_revision_zero_gets_no_initial_revision(db_session):
    """The import convention, preserved rather than duplicated.

    app.engines.demand_import.apply_batch creates a placeholder line at
    `current_revision_no = 0` and then calls apply_revision, so revision 1 carries
    the IMPORTED values instead of a zero-quantity placeholder. Under the invariant
    "a revision numbered current_revision_no exists", revision 0 means "no state
    committed yet" -- so nothing is written here and the numbering still ends up
    identical to the direct path.
    """
    well, product = _make_well(db_session, on_hand_qty=1000)
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=0.0,
        ros_date=datetime.utcnow() + timedelta(days=FAR_ROS_DAYS),
        profile=DemandProfile.PRIMARY,
        current_revision_no=0,
    )
    db_session.add(line)
    db_session.flush()

    assert line.revisions == []

    apply_revision(
        db_session, line, quantity=400, ros_date=line.ros_date,
        profile=DemandProfile.PRIMARY,
    )
    db_session.expire(line, ["revisions"])
    assert [r.revision_no for r in line.revisions] == [1]
    assert line.revisions[0].quantity == 400


# --------------------------------------------------------------------------
# Status / profile filter (Defect 4, plus the previously untested filter)
# --------------------------------------------------------------------------


def test_default_filters_are_confirmed_only_and_primary_plus_contingency():
    """Pin the filter itself, so changing it is a deliberate act.

    REPURPOSED to the new values, and the INTENT is unchanged -- that intent is the
    whole reason this test exists. The product owner has moved the default scope
    ("demand status = confirmed only, profile = primary and contingency"), so this
    test now pins THAT, and moving it again will once more require a deliberate edit
    here rather than a quiet one-word diff in the engine.

    Not a weakening: the assertion is exactly as strict as before -- two exact set
    equalities, no membership checks, no subsets. What changed is the value being
    pinned, which is the only thing that could change when the specification does.

    Both halves are named because they moved in OPPOSITE directions and the pair is
    coherent only when read together:
      * Budgeted LEAVES scope -- money set aside is not a commitment to drill, and
        it must not out-compete confirmed demand for steel.
      * Contingency ENTERS scope -- a contingency string is steel the well may
        genuinely need, and planning as though it never will is how a contingency
        arrives unreserved.
    """
    assert DEFAULT_STATUS_FILTER == {DemandStatus.CONFIRMED}
    assert DEFAULT_PROFILE_FILTER == {DemandProfile.PRIMARY, DemandProfile.CONTINGENCY}

    # Stated as explicit exclusions too, because these two are the moves that
    # changed and a reader of a failure needs to see which direction broke.
    assert DemandStatus.BUDGETED not in DEFAULT_STATUS_FILTER
    assert DemandStatus.PLANNED not in DEFAULT_STATUS_FILTER
    assert DemandProfile.CONTINGENCY in DEFAULT_PROFILE_FILTER


def test_planned_well_is_excluded_and_does_not_affect_another_wells_rollup(db_session):
    """A Planned WELL is out of scope: it neither consumes inventory nor drags any
    rollup down, and none of its lines gets a CoverageResult at all.

    REPURPOSED, not weakened. The excluded line used to sit on the SAME well as the
    in-scope one, which is the state that stopped existing when demand status became
    a property of the well -- a well cannot be Confirmed and Planned at once. The
    exclusion is now expressed where it lives, on the well.

    Strictly MORE is asserted than before. The old test checked that an excluded line
    got no verdict and did not drag its own well's rollup down; this checks that AND
    that the excluded well itself rolls up to None (unevaluated -- never "covered",
    which would be a claim the engine never made), which the single-well shape could
    not express.
    """
    _customer, node = _make_customer(db_session)
    product = _make_product(db_session, on_hand_qty=3000)
    confirmed_well = _well(db_session, node, "W-Confirmed")
    planned_well = _well(
        db_session, node, "W-Planned", demand_status=DemandStatus.PLANNED
    )
    confirmed = _line(db_session, confirmed_well, product, quantity=2000)
    planned = _line(
        db_session, planned_well, product, quantity=999999,
        days_out=FAR_ROS_DAYS - 20,
    )

    recompute_well(db_session, confirmed_well)

    # Excluded despite its enormous quantity and EARLIER ROS -- it never
    # competed for the pool.
    assert confirmed.coverage_result.status == CoverageStatus.COVERED
    assert planned.coverage_result is None
    assert confirmed_well.coverage_status == CoverageStatus.COVERED.value
    # And the excluded well is UNEVALUATED, not covered and not uncovered.
    assert planned_well.coverage_status is None


def test_contingency_line_is_INCLUDED_and_competes_for_inventory(db_session):
    """Contingency is IN scope now, and this test pins the CONSEQUENCE, not the flag.

    REPURPOSED. It used to assert the exact opposite -- that a Contingency line was
    excluded, got no verdict, and could not drag the rollup down -- because
    Contingency sat outside `DEFAULT_PROFILE_FILTER`. The owner has moved it inside,
    so the old assertion pinned behaviour that no longer exists.

    This is not a weakening, and deliberately asserts MORE than the old test did.
    The old test only had to show that an excluded line was inert. An included line
    has to be shown to do three separate things, and all three are checked here:
    it gets its own verdict, it CONSUMES INVENTORY that the other lines then cannot
    have, and it can drag the well rollup down. The middle one is the substantive
    half of the owner's change -- a contingency string that consumed nothing was the
    defect -- and no test asserted it before, because before this it was false.

    The 999999 quantity is kept from the original fixture on purpose: it was chosen
    to make an exclusion bug obvious, and it now makes the inclusion equally
    obvious in the other direction.
    """
    well, product = _make_well(db_session, on_hand_qty=3000)
    primary = _line(db_session, well, product, quantity=2000)
    contingency = _line(
        db_session, well, product, quantity=999999,
        days_out=FAR_ROS_DAYS - 20, profile=DemandProfile.CONTINGENCY,
    )

    recompute_well(db_session, well)

    # 1. It is evaluated -- there IS a verdict.
    assert contingency.coverage_result is not None
    assert contingency.coverage_result.status == CoverageStatus.UNCOVERED
    # 2. It CONSUMED the pool. Its ROS is earlier, so under ROS-ordered allocation it
    #    draws first and partially consumes all 3000 -- leaving the Primary line,
    #    which was Covered when contingency was out of scope, with nothing.
    assert primary.coverage_result.status == CoverageStatus.UNCOVERED
    # 3. It drags the rollup down.
    assert well.coverage_status == CoverageStatus.UNCOVERED.value


def test_a_modest_contingency_line_is_covered_like_any_other(db_session):
    """The healthy side of inclusion: contingency demand that fits is just Covered.

    Added alongside the test above so "Contingency is in scope" is pinned in both
    directions. Without this, an engine bug that marked every contingency line
    Uncovered regardless of stock would still satisfy the test above.
    """
    well, product = _make_well(db_session, on_hand_qty=3000)
    primary = _line(db_session, well, product, quantity=2000)
    contingency = _line(
        db_session, well, product, quantity=1000,
        days_out=FAR_ROS_DAYS + 20, profile=DemandProfile.CONTINGENCY,
    )

    recompute_well(db_session, well)

    assert primary.coverage_result.status == CoverageStatus.COVERED
    assert contingency.coverage_result.status == CoverageStatus.COVERED
    assert well.coverage_status == CoverageStatus.COVERED.value


def test_budgeted_line_is_EXCLUDED_and_does_not_affect_rollup(db_session):
    """Budgeted is OUT of scope now -- the filter is 'Confirmed only'.

    REPURPOSED, and the mirror image of the Contingency test above. It used to
    assert that a Budgeted line WAS included and could turn a well Uncovered; the
    owner has taken Budgeted out of the default scope, so it now asserts the
    exclusion -- and, as with any excluded line, that means no verdict at all rather
    than a stale one (`app.models.coverage.CoverageResult`).

    Strictly more is checked than before: the old test asserted one line's verdict,
    while this asserts BOTH that the Budgeted demand is inert AND that the inventory
    it used to consume is now available to the Confirmed line -- which is the point of
    excluding it. The 5000-against-1000 shape is retained from the original, so the
    line would unmistakably have been Uncovered had it stayed in scope.

    REPURPOSED again since: the Budgeted demand now sits on a Budgeted WELL, because
    a well cannot hold two statuses. The claim is the same one, and the excluded
    well's own null rollup is pinned too.
    """
    _customer, node = _make_customer(db_session)
    product = _make_product(db_session, on_hand_qty=1000)
    budgeted_well = _well(
        db_session, node, "W-Budgeted", demand_status=DemandStatus.BUDGETED
    )
    confirmed_well = _well(db_session, node, "W-Confirmed")
    budgeted = _line(
        db_session, budgeted_well, product, quantity=5000,
        days_out=FAR_ROS_DAYS - 20,
    )
    confirmed = _line(db_session, confirmed_well, product, quantity=800)

    recompute_well(db_session, confirmed_well)

    # Not evaluated: no verdict, not even though it would obviously have failed.
    assert budgeted.coverage_result is None
    assert budgeted_well.coverage_status is None
    # And its 5000 no longer eats the pool, despite the earlier ROS.
    assert confirmed.coverage_result.status == CoverageStatus.COVERED
    assert confirmed_well.coverage_status == CoverageStatus.COVERED.value


def test_well_with_no_included_lines_has_no_coverage_status(db_session):
    """A well whose every line is out of scope is UNEVALUATED, not covered.

    REPURPOSED. It once used two out-of-scope STATUSES on one well's two lines,
    which is no longer expressible -- a well has one status. The exclusion is now the
    well's, and the well is Planned. The claim under test -- that no included lines
    means no rollup -- is untouched, and it is now demonstrated on the case that
    actually occurs: a whole well the status filter leaves out.

    The complementary case (the whole well in scope, but every line excluded by
    PROFILE) cannot be built any more -- both profiles are in the default scope
    since the owner's change -- and it was already gone before this change for that
    reason.
    """
    _customer, node = _make_customer(db_session)
    product = _make_product(db_session, on_hand_qty=3000)
    well = _well(db_session, node, "W-Planned", demand_status=DemandStatus.PLANNED)
    _line(db_session, well, product, quantity=2000)
    _line(db_session, well, product, quantity=2000)

    recompute_customer(db_session, _customer)

    assert well.coverage_status is None


def test_moving_a_well_to_planned_deletes_every_lines_stale_coverage_result(db_session):
    """Defect 4: coverage is derived data, so a result for a line the engine no
    longer evaluates must not linger in the table pretending to be authoritative
    -- GET /wells/{id} and the Home Dashboard read those rows directly.

    REPURPOSED, and strictly stronger. The move out of scope is now made at well
    level (`set_well_demand_status`), because that is the only way it can be made --
    and so the deletion has to happen for EVERY line of the well, which this asserts
    with two lines rather than the original one. The original claim (a superseded
    verdict is deleted, not left behind) is unchanged.
    """
    well, product = _make_well(db_session, on_hand_qty=1000)
    line = _line(db_session, well, product, quantity=5000)
    second = _line(db_session, well, product, quantity=10, days_out=FAR_ROS_DAYS + 5)
    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.UNCOVERED
    assert db_session.get(CoverageResult, line.id) is not None
    assert db_session.get(CoverageResult, second.id) is not None

    # The planner downgrades the WELL to Planned -- out of scope, all of it.
    set_well_demand_status(db_session, well, DemandStatus.PLANNED)

    assert db_session.get(CoverageResult, line.id) is None
    assert db_session.get(CoverageResult, second.id) is None
    assert line.coverage_result is None
    assert well.coverage_status is None


def test_moving_a_well_to_budgeted_also_deletes_the_coverage_result(db_session):
    """A SECOND status out of scope, so the deletion is not tied to one value.

    Kept as a separate test from the Planned one above rather than merged, for the
    original reason: two different values of the same column both leaving scope is
    what shows the engine keys on the FILTER and not on one magic status.
    """
    well, product = _make_well(db_session, on_hand_qty=1000)
    line = _line(db_session, well, product, quantity=5000)
    recompute_well(db_session, well)
    assert db_session.get(CoverageResult, line.id) is not None

    set_well_demand_status(db_session, well, DemandStatus.BUDGETED)

    assert db_session.get(CoverageResult, line.id) is None


def test_revising_a_line_to_contingency_KEEPS_its_coverage_result(db_session):
    """The complement, which is new: Contingency stays in scope, so the row stays.

    Added because "the verdict is deleted when a line leaves scope" is only half a
    rule -- without this, an engine that deleted verdicts on every revision would
    pass every deletion test in this file. It also pins the owner's profile change at
    the revision path specifically, which is where a planner would actually make it.
    """
    well, product = _make_well(db_session, on_hand_qty=1000)
    line = _line(db_session, well, product, quantity=5000)
    recompute_well(db_session, well)

    apply_revision(
        db_session, line, quantity=5000, ros_date=line.ros_date,
        profile=DemandProfile.CONTINGENCY,
    )

    result = db_session.get(CoverageResult, line.id)
    assert result is not None
    assert result.status == CoverageStatus.UNCOVERED
    # And the well's status was untouched by a line-level revision -- it has no
    # business changing it.
    assert well.demand_status == DemandStatus.CONFIRMED


def test_excluding_a_whole_well_frees_the_inventory_it_used_to_consume(db_session):
    """The filter runs BEFORE allocation, so dropping a WELL out of scope releases
    its whole share of the pool to the wells that remain.

    REPURPOSED, not weakened. The original made the move on a line and asserted that
    the sibling line in the same well picked up the freed steel. Status is a
    well-level fact now, so the move is a well-level move -- and the freed steel goes
    to a DIFFERENT well, which is a strictly wider claim: it proves the pool
    arithmetic is pool-wide rather than well-local, which the single-well shape could
    not distinguish.
    """
    customer, node = _make_customer(db_session)
    product = _make_product(db_session, on_hand_qty=5000)
    early_well = _well(db_session, node, "Well Free-Early")
    late_well = _well(db_session, node, "Well Free-Late")
    early = _line(db_session, early_well, product, quantity=4000, days_out=FAR_ROS_DAYS)
    late = _line(
        db_session, late_well, product, quantity=4000, days_out=FAR_ROS_DAYS + 30
    )

    recompute_customer(db_session, customer)
    assert early.coverage_result.status == CoverageStatus.COVERED
    assert late.coverage_result.status == CoverageStatus.UNCOVERED
    assert late_well.coverage_status == CoverageStatus.UNCOVERED.value

    set_well_demand_status(db_session, early_well, DemandStatus.PLANNED)

    # The excluded well: no verdict for its line, and an unevaluated rollup.
    assert db_session.get(CoverageResult, early.id) is None
    assert early_well.coverage_status is None
    # Its 4000 went to the OTHER well, which now completes.
    assert late.coverage_result.status == CoverageStatus.COVERED
    assert late_well.coverage_status == CoverageStatus.COVERED.value


# --------------------------------------------------------------------------
# Demand status is a WELL-level fact (set_well_demand_status)
# --------------------------------------------------------------------------


def test_a_line_cannot_carry_a_status_of_its_own(db_session):
    """The old invalid state is UNREPRESENTABLE, not merely disallowed.

    Constructing a `DemandLine(status=...)` is a TypeError, because there is no such
    column -- and every line of a well necessarily reports its well's status because
    that is the only place the value exists. This is asserted rather than assumed: a
    future convenience property forwarding `line.status` would re-create exactly the
    impression the move was made to remove, and the coverage engine's filter would
    once again look line-scoped.
    """
    well, product = _make_well(db_session, on_hand_qty=1000)
    a = _line(db_session, well, product, quantity=100)
    b = _line(db_session, well, product, quantity=200, profile=DemandProfile.CONTINGENCY)

    assert not hasattr(a, "status")
    with pytest.raises(TypeError):
        DemandLine(
            well_id=well.id, product_id=product.id, quantity=1,
            ros_date=well.demand_lines[0].ros_date,
            status=DemandStatus.PLANNED, profile=DemandProfile.PRIMARY,
        )

    # One status, shared by construction -- both lines, and both profiles.
    assert a.well.demand_status is b.well.demand_status is well.demand_status


def test_well_status_change_writes_a_revision_and_an_impact_to_every_line(db_session):
    """A status change is a WELL operation that fans out to every line of the well.

    This is how the functional spec's two statements are both kept: it lists "Demand
    Status Changes" among what the revision model records, and it says revisions
    occur at demand-line level. The well is the authority; each line's history stays
    complete.
    """
    well, product = _make_well(db_session, on_hand_qty=100_000)
    lines = [
        _line(db_session, well, product, quantity=100, days_out=FAR_ROS_DAYS),
        _line(db_session, well, product, quantity=200, days_out=FAR_ROS_DAYS + 10),
        _line(
            db_session, well, product, quantity=300, days_out=FAR_ROS_DAYS + 20,
            profile=DemandProfile.CONTINGENCY,
        ),
    ]
    recompute_well(db_session, well)
    assert all(l.current_revision_no == 1 for l in lines)

    change = set_well_demand_status(db_session, well, DemandStatus.BUDGETED)

    assert well.demand_status == DemandStatus.BUDGETED
    assert change.status_before == "Confirmed"
    assert change.status_after == "Budgeted"
    assert change.unchanged is False
    assert set(change.demand_line_ids) == {l.id for l in lines}
    assert change.revised_line_count == 3
    assert len(change.revision_ids) == 3
    assert len(change.impact_record_ids) == 3

    for line in lines:
        db_session.expire(line, ["revisions"])
        # The invariant: a revision numbered current_revision_no always exists.
        assert line.current_revision_no == 2
        assert [r.revision_no for r in line.revisions] == [1, 2]
        latest = line.revisions[-1]
        assert latest.status == DemandStatus.BUDGETED
        # Its OWN quantity / ROS / profile, unchanged -- a revision is a snapshot of
        # the whole state, not of the one field that moved.
        assert latest.quantity == line.quantity
        assert latest.ros_date == line.ros_date
        assert latest.profile == line.profile

    impacts = (
        db_session.query(ImpactRecord)
        .filter(ImpactRecord.well_id == well.id)
        .all()
    )
    assert {i.demand_line_id for i in impacts} == {l.id for l in lines}
    for impact in impacts:
        assert impact.status_before == "Confirmed"
        assert impact.status_after == "Budgeted"
        # The well left scope, so its rollup went from Covered to unevaluated.
        assert impact.coverage_before == CoverageStatus.COVERED.value
        assert impact.coverage_after is None


def test_setting_the_status_a_well_already_has_writes_nothing(db_session):
    """A no-op request must not append a revision recording a change of nothing.

    The Home Dashboard's "Demand Changes" card shows 20 rows; a status change that
    fans out to every line of a well would push real history off it. Same reason
    `app.engines.scenario_apply` declines to revise a line whose override equals the
    base plan.
    """
    well, product = _make_well(db_session, on_hand_qty=100_000)
    line = _line(db_session, well, product, quantity=100)
    recompute_well(db_session, well)
    revisions_before = db_session.query(DemandRevision).count()
    impacts_before = db_session.query(ImpactRecord).count()

    change = set_well_demand_status(db_session, well, DemandStatus.CONFIRMED)

    assert change.unchanged is True
    assert change.revision_ids == ()
    assert change.impact_record_ids == ()
    assert change.revised_line_count == 0
    assert db_session.query(DemandRevision).count() == revisions_before
    assert db_session.query(ImpactRecord).count() == impacts_before
    assert line.current_revision_no == 1


def test_a_line_revision_does_not_touch_the_wells_status(db_session):
    """`apply_revision` takes no status, and its revision snapshots the well's.

    So the two writers cannot fight: a quantity change records the status that was
    true at the time (which is what makes the history reconstructable) without
    claiming to have changed it.
    """
    well, product = _make_well(
        db_session, on_hand_qty=100_000, demand_status=DemandStatus.CONFIRMED
    )
    line = _line(db_session, well, product, quantity=100)
    recompute_well(db_session, well)

    impact = apply_revision(
        db_session, line, quantity=250, ros_date=line.ros_date,
        profile=DemandProfile.PRIMARY,
    )

    assert well.demand_status == DemandStatus.CONFIRMED
    db_session.expire(line, ["revisions"])
    assert line.revisions[-1].status == DemandStatus.CONFIRMED
    assert line.revisions[-1].quantity == 250
    # Reported as unchanged rather than left NULL: nothing happened to the status,
    # and the dashboard renders the pair as a transition.
    assert impact.status_before == impact.status_after == "Confirmed"


def test_confirming_a_well_makes_its_whole_programme_compete_for_inventory(db_session):
    """The reverse of the exclusion test, and the case a planner actually asks about.

    Confirming a well brings ALL of its demand into the pool at once -- Primary and
    Contingency alike -- which is what makes the status filter's effect legible and
    what makes the pool arithmetic symmetric with excluding a well.
    """
    customer, node = _make_customer(db_session)
    product = _make_product(db_session, on_hand_qty=5000)
    incumbent = _well(db_session, node, "W-Incumbent")
    candidate = _well(
        db_session, node, "W-Candidate", demand_status=DemandStatus.PLANNED
    )
    held = _line(
        db_session, incumbent, product, quantity=4000, days_out=FAR_ROS_DAYS + 30
    )
    # Both profiles, both earlier than the incumbent, so confirming the well starves
    # it -- and it takes BOTH lines to do so (2000 + 2500 > 5000 - 4000).
    primary = _line(db_session, candidate, product, quantity=2000, days_out=FAR_ROS_DAYS)
    contingency = _line(
        db_session, candidate, product, quantity=2500, days_out=FAR_ROS_DAYS + 5,
        profile=DemandProfile.CONTINGENCY,
    )

    recompute_customer(db_session, customer)
    assert held.coverage_result.status == CoverageStatus.COVERED
    assert db_session.get(CoverageResult, primary.id) is None
    assert db_session.get(CoverageResult, contingency.id) is None

    set_well_demand_status(db_session, candidate, DemandStatus.CONFIRMED)

    # Both profiles are now evaluated and both drew on the pool.
    assert primary.coverage_result.status == CoverageStatus.COVERED
    assert contingency.coverage_result.status == CoverageStatus.COVERED
    # ...leaving 500 of the 5000, so the incumbent's 4000 no longer completes.
    assert held.coverage_result.status == CoverageStatus.UNCOVERED
    assert incumbent.coverage_status == CoverageStatus.UNCOVERED.value
    assert candidate.coverage_status == CoverageStatus.COVERED.value


def test_a_well_with_no_demand_lines_can_still_change_status(db_session):
    """A well can legitimately be confirmed before its programme is entered.

    Nothing to write a revision to, so nothing is written -- and that is reported as
    an empty fan-out rather than as a failure.
    """
    _customer, node = _make_customer(db_session)
    well = _well(db_session, node, "W-Bare", demand_status=DemandStatus.PLANNED)

    change = set_well_demand_status(db_session, well, DemandStatus.CONFIRMED)

    assert well.demand_status == DemandStatus.CONFIRMED
    assert change.unchanged is False
    assert change.revised_line_count == 0
    assert change.revision_ids == ()
    assert change.impact_record_ids == ()
    # Still unevaluated: a well with no demand has no rollup either way.
    assert well.coverage_status is None


# --------------------------------------------------------------------------
# Uncovered / Unrecoverable boundary (Defect 3)
# --------------------------------------------------------------------------


def test_ros_inside_the_old_sailing_double_count_window_is_uncovered(db_session):
    """Defect 3 regression: the ~2-month false-UNRECOVERABLE window.

    13CR total lead time is 6.5 months (198 days). The old recommended order date
    subtracted the 2-month sailing leg TWICE (ros - 198 - 61 = ros - 259 days)
    and `is_recoverable` was derived from it, so any ROS between 199 and 259 days
    out was declared UNRECOVERABLE -- "ROS cannot be met even if ordered today" --
    when ordering today would in fact land the steel about 2 months EARLY.

    230 days sits squarely inside that window. It must be UNCOVERED.
    """
    assert 199 <= 230 <= 259, "the test date must lie inside the old false window"

    well, product = _make_well(db_session, on_hand_qty=0)
    line = _line(db_session, well, product, quantity=1000, days_out=230)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.UNCOVERED
    assert "cannot be met even if ordered today" not in (
        line.coverage_result.reason or ""
    )


def test_ros_just_outside_the_lead_time_is_uncovered_not_unrecoverable(db_session):
    """The true boundary, from the physics only: today + 6.5 months <= ROS."""
    well, product = _make_well(db_session, on_hand_qty=0)
    line = _line(db_session, well, product, quantity=1000, days_out=TOTAL_LEAD_DAYS + 2)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.UNCOVERED


def test_ros_just_inside_the_lead_time_is_unrecoverable(db_session):
    """The other side of the same boundary -- steel ordered today cannot arrive
    in time, so the terminal claim is genuinely true."""
    well, product = _make_well(db_session, on_hand_qty=0)
    line = _line(db_session, well, product, quantity=1000, days_out=TOTAL_LEAD_DAYS - 2)

    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.UNRECOVERABLE
    assert "cannot be met even if ordered today" in line.coverage_result.reason
    # And the claim is arithmetically true, not merely asserted.
    assert date.today() + months_to_timedelta(TOTAL_MONTHS) > line.ros_date.date()


# --------------------------------------------------------------------------
# Customer-scope pooling (Defect 1)
# --------------------------------------------------------------------------


def test_inventory_is_pooled_across_wells_of_one_customer(db_session):
    """Defect 1: 6000 on hand cannot cover 4000 + 5000 across two wells.

    Before the fix each well was allocated the FULL on-hand quantity against its
    own lines, so both wells reported Covered for 9000 tonnes of demand against
    6000 tonnes of steel.
    """
    _customer, node = _make_customer(db_session)
    product = _make_product(db_session, on_hand_qty=6000)
    well_a = _well(db_session, node, "Well Pool-A")
    well_b = _well(db_session, node, "Well Pool-B")
    line_a = _line(db_session, well_a, product, quantity=4000, days_out=FAR_ROS_DAYS)
    line_b = _line(db_session, well_b, product, quantity=5000, days_out=FAR_ROS_DAYS + 50)

    recompute_well(db_session, well_a)

    # Earliest ROS first: A takes 4000, leaving 2000 -- not enough for B.
    assert line_a.coverage_result.status == CoverageStatus.COVERED
    assert line_b.coverage_result.status == CoverageStatus.UNCOVERED
    assert well_a.coverage_status == CoverageStatus.COVERED.value
    assert well_b.coverage_status == CoverageStatus.UNCOVERED.value


def test_single_well_trigger_writes_results_for_every_well_of_the_customer(db_session):
    """recompute_well stays the public entry point, but a single-well trigger
    must still produce globally-correct results for the whole pool -- otherwise
    the well that was not triggered keeps an over-optimistic verdict."""
    _customer, node = _make_customer(db_session)
    product = _make_product(db_session, on_hand_qty=6000)
    well_a = _well(db_session, node, "Well Trigger-A")
    well_b = _well(db_session, node, "Well Trigger-B")
    line_a = _line(db_session, well_a, product, quantity=4000, days_out=FAR_ROS_DAYS)
    line_b = _line(db_session, well_b, product, quantity=5000, days_out=FAR_ROS_DAYS + 50)

    # Trigger on B only. A must still be evaluated and B must still lose.
    recompute_well(db_session, well_b)

    assert line_a.coverage_result is not None
    assert line_a.coverage_result.status == CoverageStatus.COVERED
    assert line_b.coverage_result.status == CoverageStatus.UNCOVERED
    assert well_a.coverage_status == CoverageStatus.COVERED.value


def test_pool_is_allocated_by_line_level_ros_not_by_well_processing_order(db_session):
    """Defect 1's subtle half: 'Earliest ROS First' is a LINE-level rule.

    5000 on hand. Well A needs 4000 at a LATE ROS; well B needs 4000 at an EARLY
    ROS. Naive well-by-well carry-forward, triggered on A, would cover A and
    starve B -- letting well processing order decide who gets scarce steel. The
    spec requires B to win, and the answer must be identical whichever well is
    triggered.
    """
    _customer, node = _make_customer(db_session)
    product = _make_product(db_session, on_hand_qty=5000)
    well_a = _well(db_session, node, "Well Order-A")
    well_b = _well(db_session, node, "Well Order-B")
    late = _line(db_session, well_a, product, quantity=4000, days_out=FAR_ROS_DAYS + 90)
    early = _line(db_session, well_b, product, quantity=4000, days_out=FAR_ROS_DAYS)

    # Trigger on the LATE well first -- the order that used to pick the winner.
    recompute_well(db_session, well_a)

    assert early.coverage_result.status == CoverageStatus.COVERED
    assert late.coverage_result.status == CoverageStatus.UNCOVERED
    assert well_b.coverage_status == CoverageStatus.COVERED.value
    assert well_a.coverage_status == CoverageStatus.UNCOVERED.value

    # Idempotent and trigger-independent: recomputing from the other well, or
    # from the customer directly, gives exactly the same answer.
    recompute_well(db_session, well_b)
    assert early.coverage_result.status == CoverageStatus.COVERED
    assert late.coverage_result.status == CoverageStatus.UNCOVERED

    recompute_customer(db_session, node.customer)
    assert early.coverage_result.status == CoverageStatus.COVERED
    assert late.coverage_result.status == CoverageStatus.UNCOVERED


def test_pooling_spans_the_business_unit_and_the_earliest_ros_wins(db_session):
    """Pool scope is the BUSINESS UNIT (product-owner ruling 2026-09-06, D01).

    This test asserted the opposite -- that a neighbour's demand could never move
    your coverage -- and that separation is exactly what let one Business Unit
    promise the same steel to two customers. Urgency decides now: the earlier ROS
    takes the shelf whoever owns it, and the loser is short by what the Business
    Unit genuinely does not have.

    Two things a reader should not mistake this for. It is NOT a widening of the
    BUSINESS UNIT boundary, which is still absolute. And it does not touch the
    ownership walls inside the pool: a customer's own uploaded stock and an Oracle
    assignment stay private -- see tests/test_bu_wide_allocation.py.
    """
    _c1, node1 = _make_customer(db_session, name="Cust One")
    _c2, node2 = _make_customer(db_session, name="Cust Two")
    product = _make_product(db_session, on_hand_qty=5000)

    well_1 = _well(db_session, node1, "Well Sep-1")
    line_1 = _line(db_session, well_1, product, quantity=4000)
    recompute_well(db_session, well_1)
    assert line_1.coverage_result.status == CoverageStatus.COVERED

    # A second customer piles 4000 of demand onto the same product with an EARLIER
    # ROS. It is more urgent, so it takes the steel.
    well_2 = _well(db_session, node2, "Well Sep-2")
    line_2 = _line(db_session, well_2, product, quantity=4000, days_out=FAR_ROS_DAYS - 50)
    recompute_well(db_session, well_2)

    assert line_2.coverage_result.status == CoverageStatus.COVERED
    # One recompute re-divided the whole Business Unit, so Cust One's stored
    # verdict is already correct -- it does not wait for a recompute of its own.
    assert line_1.coverage_result.status == CoverageStatus.UNCOVERED
    # 1000 of the 5000 was left and it was genuinely drawn, so only 3000 needs
    # ordering (the F04 net position).
    assert line_1.coverage_result.drawn_company == 1000
    assert line_1.coverage_result.residual == 3000

    recompute_well(db_session, well_1)
    assert line_1.coverage_result.status == CoverageStatus.UNCOVERED


def test_recompute_advances_computed_at(db_session):
    """C-08: computed_at must report the LAST computation, not the first --
    a verdict recomputed now was computed now, whether or not its value moved."""
    from datetime import datetime, timedelta

    from app.engines.coverage import recompute_customer
    from app.models import CoverageResult

    well, product = _make_well(db_session, 500)
    _line(db_session, well, product, 100)
    customer = well.planning_node.customer
    recompute_customer(db_session, customer)
    db_session.flush()
    row = (
        db_session.query(CoverageResult).first()
    )
    assert row is not None
    # Backdate the stamp, recompute, and require it to advance.
    row.computed_at = datetime.utcnow() - timedelta(days=30)
    db_session.flush()
    stale = row.computed_at
    recompute_customer(db_session, customer)
    db_session.flush()
    assert row.computed_at > stale
