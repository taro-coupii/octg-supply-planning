"""Business Unit inventory boundary + the cross-customer sharing ANALYSIS.

Two boundaries are under test and they behave differently on purpose:

  Business Unit  ALWAYS separates. Never crossed by anything, including the
                 analysis.
  Customer       Separates BY DEFAULT. The official verdict is customer-scoped
                 even for two customers in the same BU; only the read-only
                 what-if reasons across them.

There is no unscoped quantity anywhere any more. `InventoryOnHand(BU, product)`
is the only source: `Product.on_hand_qty` and the fallback that read it have both
been removed, so a (BU, product) pair with no row RAISES rather than resolving to
a number, and a customer with no BU has no pool to resolve against at all.
Sections 2, 8, 10 and 11 are where those rules are pinned; sections 2 and 8 hold
the tests that used to describe the removed fallback and now describe what
replaced it.

Lead-time components are seeded so `is_recoverable` genuinely computes rather
than short-circuiting on a zero total (same reasoning as
tests/test_coverage_engine.py). FAR_ROS_DAYS keeps every line comfortably
outside the 6.5-month 13CR lead time so shortfalls resolve UNCOVERED, not
UNRECOVERABLE.
"""

from datetime import datetime, timedelta

import pytest

from app.engines.coverage import recompute_customer, recompute_well
from app.engines.inventory import (
    InventoryNotScoped,
    InventoryRowMissing,
    InventoryScopeMissing,
)
from app.engines.sharing import cross_customer_sharing
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    CoverageResult,
    CoverageStatus,
    Customer,
    CustomerSubstitutionRule,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryAssignment,
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

FAR_ROS_DAYS = 400


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _lead_times(db_session):
    """6.5 months of 13CR lead time, as a COMPLETE attribute component set.

    All four dimensions, because an incomplete set resolves to "not modelled"
    (total 0) instead of being partially summed -- see app.engines.lead_time. The
    two zero-month rows carry no time but they are what make the model complete.
    """
    if db_session.query(LeadTimeComponent).first() is None:
        db_session.add_all([
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
        db_session.flush()


def _bu(db_session, name):
    bu = BusinessUnit(name=name)
    db_session.add(bu)
    db_session.flush()
    return bu


def _customer(db_session, name, bu=None, policy=AllocationPolicy.SOFT):
    customer = Customer(
        name=name,
        allocation_policy=policy,
        business_unit_id=bu.id if bu is not None else None,
    )
    db_session.add(customer)
    db_session.flush()
    node = PlanningNode(
        customer_id=customer.id, node_type="Campaign", name=f"{name} Campaign"
    )
    db_session.add(node)
    db_session.flush()
    _lead_times(db_session)
    return customer, node


def _product(db_session, grade="13CR80"):
    """A catalogue entry ONLY -- no quantity.

    Quantity is not a property of a product; it belongs to a (Business Unit,
    product) pair and is written by `_stock`. That is the whole point of this
    file, so the fixture no longer lets a test hand a product a bare number.
    """
    product = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="TBG", size="4-1/2", weight=12.6, grade=grade, grade_type="13CR",
        connection="VAM TOP", description=f"TBG 4-1/2 12.6 {grade} VAM TOP",
    )
    db_session.add(product)
    db_session.flush()
    return product


def _stock(db_session, bu, product, quantity):
    """Upsert the (BU, product) on-hand row -- the ONLY place a quantity lives.

    Idempotent so a fixture may restate a quantity without tripping
    uq_inventory_on_hand_bu_product. An explicit quantity of 0 states "this BU
    holds none of it", which is a FACT; no row at all means the quantity is
    unknown and every resolver raises rather than guessing.
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


def _line(db_session, well, product, quantity, days_out=FAR_ROS_DAYS):
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
# 1. The BU boundary in the coverage engine
# --------------------------------------------------------------------------


def test_two_bus_see_only_their_own_quantity_of_one_product(db_session):
    """ONE product, TWO BUs, two different quantities.

    The catalogue entry is shared -- that is what a product master is -- but the
    tonnage is not. BU North holds 6000 and covers its 5000; BU Gulf holds 1000
    and cannot. Neither verdict may move when the other's does.
    """
    bu_north = _bu(db_session, "North")
    bu_gulf = _bu(db_session, "Gulf")
    product = _product(db_session)
    _stock(db_session, bu_north, product, 6000)
    _stock(db_session, bu_gulf, product, 1000)

    c_north, node_north = _customer(db_session, "North Co", bu_north)
    c_gulf, node_gulf = _customer(db_session, "Gulf Co", bu_gulf)
    well_north = _well(db_session, node_north, "Well North-01")
    well_gulf = _well(db_session, node_gulf, "Well Gulf-01")
    line_north = _line(db_session, well_north, product, quantity=5000)
    line_gulf = _line(db_session, well_gulf, product, quantity=5000)

    recompute_customer(db_session, c_north)
    recompute_customer(db_session, c_gulf)

    assert _status(line_north) == CoverageStatus.COVERED
    assert _status(line_gulf) == CoverageStatus.UNCOVERED

    # Recomputing either side again changes nothing about the other.
    recompute_customer(db_session, c_north)
    assert _status(line_gulf) == CoverageStatus.UNCOVERED
    recompute_customer(db_session, c_gulf)
    assert _status(line_north) == CoverageStatus.COVERED


def test_demand_in_another_bu_never_changes_this_bus_coverage(db_session):
    """A neighbouring BU piling on demand with an EARLIER ROS is irrelevant."""
    bu_north = _bu(db_session, "North")
    bu_gulf = _bu(db_session, "Gulf")
    product = _product(db_session)
    _stock(db_session, bu_north, product, 6000)
    _stock(db_session, bu_gulf, product, 6000)

    c_north, node_north = _customer(db_session, "North Co", bu_north)
    c_gulf, node_gulf = _customer(db_session, "Gulf Co", bu_gulf)
    line_north = _line(
        db_session, _well(db_session, node_north, "W-N"), product, quantity=5000
    )
    recompute_customer(db_session, c_north)
    assert _status(line_north) == CoverageStatus.COVERED

    _line(
        db_session, _well(db_session, node_gulf, "W-G"), product,
        quantity=6000, days_out=FAR_ROS_DAYS - 100,
    )
    recompute_customer(db_session, c_gulf)
    recompute_customer(db_session, c_north)

    assert _status(line_north) == CoverageStatus.COVERED


def test_official_coverage_is_still_customer_scoped_inside_one_bu(db_session):
    """THE crucial 'default is still separated' test.

    Two customers, ONE BU, one product, 6000 on hand. Customer B demands 6000
    with an EARLIER ROS than customer A's 5000. If the pool had silently widened
    to the BU, A would lose. It must not: the customer boundary is the default
    and only the read-only what-if is allowed to look past it.
    """
    bu = _bu(db_session, "Shared BU")
    product = _product(db_session)
    _stock(db_session, bu, product, 6000)

    c_a, node_a = _customer(db_session, "Cust A", bu)
    c_b, node_b = _customer(db_session, "Cust B", bu)
    line_a = _line(db_session, _well(db_session, node_a, "W-A"), product, quantity=5000)
    recompute_customer(db_session, c_a)
    assert _status(line_a) == CoverageStatus.COVERED

    line_b = _line(
        db_session, _well(db_session, node_b, "W-B"), product,
        quantity=6000, days_out=FAR_ROS_DAYS - 100,
    )
    recompute_customer(db_session, c_b)
    recompute_customer(db_session, c_a)

    # Both are Covered against the same 6000 -- the BU has over-promised, and
    # that is the documented consequence of customer separation, not a bug.
    assert _status(line_b) == CoverageStatus.COVERED
    assert _status(line_a) == CoverageStatus.COVERED


# --------------------------------------------------------------------------
# 2. InventoryOnHand is the ONLY source -- there is nothing to fall back to
#
# These three tests used to be about PRECEDENCE between the BU row and the legacy
# unscoped `Product.on_hand_qty`. That column is gone, so precedence is not a
# thing any more and the tests now pin the rules that replaced it: the row's value
# decides, an explicit 0 is a real answer, and a MISSING row raises instead of
# being answered by anything at all.
# --------------------------------------------------------------------------


def test_the_bu_row_value_is_what_decides_coverage(db_session):
    """The BU holds 1000 against a 5000 requirement, so the line is Uncovered.

    Formerly `test_bu_row_takes_precedence_over_product_on_hand_qty`, where the
    product ALSO carried a global 99999 that had to lose. There is no competing
    number to beat now, so what is left to assert is the half that always
    mattered: the verdict is computed from this BU's row and from nothing else.
    """
    bu = _bu(db_session, "Row Value BU")
    product = _product(db_session)
    _stock(db_session, bu, product, 1000)

    customer, node = _customer(db_session, "Prec Co", bu)
    line = _line(db_session, _well(db_session, node, "W-P"), product, quantity=5000)

    recompute_customer(db_session, customer)

    assert _status(line) == CoverageStatus.UNCOVERED

    # And the row is genuinely the input: raise it and the same line is Covered.
    _stock(db_session, bu, product, 6000)
    recompute_customer(db_session, customer)
    assert _status(line) == CoverageStatus.COVERED


def test_a_missing_bu_row_raises_instead_of_falling_back(db_session):
    """THE test this file's section 2 exists for now.

    Formerly `test_product_on_hand_qty_is_the_fallback_when_no_bu_row_exists`,
    which asserted that a product with a global 8000 and no BU row came out
    COVERED. That is the leak: the 8000 belonged to no BU, so every BU read it as
    its own. The fallback is removed, and the replacement assertion is strictly
    STRONGER than the one it replaces -- it pins that the engine refuses to
    produce a verdict at all rather than producing a plausible wrong one.

    Note what is NOT asserted: that the line comes out Uncovered. Treating the
    missing row as 0 would be just as much of an invention as treating it as
    8000, and it would be the more dangerous one because it looks like a
    measurement.
    """
    bu = _bu(db_session, "No Row BU")
    product = _product(db_session)  # deliberately NOT stocked in `bu`

    customer, node = _customer(db_session, "Fall Co", bu)
    line = _line(db_session, _well(db_session, node, "W-F"), product, quantity=5000)

    with pytest.raises(InventoryRowMissing) as excinfo:
        recompute_customer(db_session, customer)

    message = str(excinfo.value)
    assert product.description in message
    assert bu.id in message
    # The exception is catchable as the general kind an API handler catches.
    assert isinstance(excinfo.value, InventoryNotScoped)
    assert excinfo.value.business_unit_id == bu.id
    assert excinfo.value.product_id == product.id
    # No verdict was invented on the way out.
    assert db_session.get(CoverageResult, line.id) is None

    # Control: state the quantity -- even as 0 -- and the engine answers again.
    _stock(db_session, bu, product, 0)
    recompute_customer(db_session, customer)
    assert _status(line) == CoverageStatus.UNCOVERED


def test_a_zero_bu_row_is_a_real_answer_not_a_missing_one(db_session):
    """An explicit 0 means "this BU holds none of it", which is a FACT, and it
    must not be conflated with the silence of no row at all.

    Formerly `test_a_zero_bu_row_still_wins_over_a_healthy_legacy_scalar`. The
    scalar it had to win over is gone; the distinction the test protects -- 0 is
    measured, absent is unknown -- is the one that survived, and it is now the
    load-bearing one, since the two outcomes differ (a verdict vs. an exception).
    """
    bu = _bu(db_session, "Empty BU")
    product = _product(db_session)
    _stock(db_session, bu, product, 0)

    customer, node = _customer(db_session, "Empty Co", bu)
    line = _line(db_session, _well(db_session, node, "W-E"), product, quantity=100)

    recompute_customer(db_session, customer)

    assert _status(line) == CoverageStatus.UNCOVERED


# --------------------------------------------------------------------------
# 3. Substitution fall-through respects the BU boundary
# --------------------------------------------------------------------------


def test_substitute_cannot_be_drawn_from_another_bu(db_session):
    """A fully approved substitute with 9000 in ANOTHER BU and 0 in ours.

    Every permission layer clears, so if the line came out covered it could only
    be off the back of the other BU's steel. Under SOFT this is the sharp case:
    soft allocation ignores assignment rows, so `reserved_by_product` is empty
    and the old code fell straight back to the global `Product.on_hand_qty`.
    """
    from app.engines.substitution import decide_approval, request_approval

    bu_ours = _bu(db_session, "Ours")
    bu_theirs = _bu(db_session, "Theirs")
    primary = _product(db_session, grade="13CR80")
    # Global scalar is generous; our BU holds none of it, theirs holds plenty.
    substitute = _product(db_session, grade="13CR110")
    _stock(db_session, bu_ours, primary, 0)
    _stock(db_session, bu_ours, substitute, 0)
    _stock(db_session, bu_theirs, substitute, 9000)

    customer, node = _customer(db_session, "Ours Co", bu_ours)
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
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_customer(db_session, customer)

    assert _status(line) != CoverageStatus.COVERED_VIA_SUBSTITUTE
    assert _status(line) == CoverageStatus.UNCOVERED

    # Control: put the stock in OUR BU and the very same setup covers it, so the
    # test is failing for the boundary and not for a broken substitution path.
    db_session.query(InventoryOnHand).filter(
        InventoryOnHand.business_unit_id == bu_ours.id,
        InventoryOnHand.product_id == substitute.id,
    ).one().quantity = 9000
    db_session.flush()
    recompute_customer(db_session, customer)
    assert _status(line) == CoverageStatus.COVERED_VIA_SUBSTITUTE


# --------------------------------------------------------------------------
# 4. HARD / HYBRID assignments respect the BU boundary
# --------------------------------------------------------------------------


def test_hard_assignment_in_another_bu_does_not_shrink_this_pool(db_session):
    """A reservation made in another BU draws on that BU's separate stock, so it
    must not be netted off ours.

    Our BU holds 4000 and all 4000 is assigned to our line -> Covered. A foreign
    BU reserves 4000 of the same product for its own line. Ours stays Covered.
    """
    bu_ours = _bu(db_session, "Ours")
    bu_theirs = _bu(db_session, "Theirs")
    product = _product(db_session)
    _stock(db_session, bu_ours, product, 4000)
    _stock(db_session, bu_theirs, product, 4000)

    c_ours, node_ours = _customer(
        db_session, "Ours Co", bu_ours, AllocationPolicy.HARD
    )
    c_theirs, node_theirs = _customer(
        db_session, "Theirs Co", bu_theirs, AllocationPolicy.HARD
    )

    our_line = _line(db_session, _well(db_session, node_ours, "W-O"), product, 4000)
    _assign(db_session, our_line, product, 4000)
    recompute_customer(db_session, c_ours)
    assert _status(our_line) == CoverageStatus.COVERED

    their_line = _line(
        db_session, _well(db_session, node_theirs, "W-T"), product, 4000
    )
    _assign(db_session, their_line, product, 4000)
    recompute_customer(db_session, c_theirs)
    recompute_customer(db_session, c_ours)

    assert _status(their_line) == CoverageStatus.COVERED
    assert _status(our_line) == CoverageStatus.COVERED


def test_foreign_bu_reservation_does_not_shrink_our_unassigned_pool(db_session):
    """The netting direction the HARD test above cannot reach.

    HARD coverage depends only on the assignment, so netting errors are invisible
    to it. HYBRID's pool top-up is where a cross-BU reservation actually does
    damage: our BU holds 4000, our line needs 4000 with 1500 assigned, so it
    needs 2500 from our own 2500 unassigned pool -> Covered. A foreign BU then
    reserves 4000 of the same product against its own separate stock. Netting
    that off system-wide (as the engine used to) would zero our pool and flip us
    to Uncovered for no physical reason at all.
    """
    bu_ours = _bu(db_session, "Ours")
    bu_theirs = _bu(db_session, "Theirs")
    product = _product(db_session)
    _stock(db_session, bu_ours, product, 4000)
    _stock(db_session, bu_theirs, product, 4000)

    c_ours, node_ours = _customer(
        db_session, "Ours Co", bu_ours, AllocationPolicy.HYBRID
    )
    c_theirs, node_theirs = _customer(
        db_session, "Theirs Co", bu_theirs, AllocationPolicy.HYBRID
    )

    our_line = _line(db_session, _well(db_session, node_ours, "W-O"), product, 4000)
    _assign(db_session, our_line, product, 1500)
    recompute_customer(db_session, c_ours)
    assert _status(our_line) == CoverageStatus.COVERED

    their_line = _line(
        db_session, _well(db_session, node_theirs, "W-T"), product, 4000
    )
    _assign(db_session, their_line, product, 4000)
    recompute_customer(db_session, c_theirs)
    recompute_customer(db_session, c_ours)

    assert _status(our_line) == CoverageStatus.COVERED


def test_hybrid_pool_top_up_cannot_reach_into_another_bu(db_session):
    """HYBRID's unassigned-pool top-up is bounded by the BU figure.

    2500 assigned against a 4000 requirement. Our BU holds exactly 2500, so
    there is no unassigned pool at all; the neighbouring BU's 10000 is not a
    pool we may top up from.
    """
    bu_ours = _bu(db_session, "Ours")
    bu_theirs = _bu(db_session, "Theirs")
    product = _product(db_session)
    _stock(db_session, bu_ours, product, 2500)
    _stock(db_session, bu_theirs, product, 10000)

    customer, node = _customer(
        db_session, "Hyb Co", bu_ours, AllocationPolicy.HYBRID
    )
    line = _line(db_session, _well(db_session, node, "W-H"), product, quantity=4000)
    _assign(db_session, line, product, 2500)

    recompute_customer(db_session, customer)

    assert _status(line) == CoverageStatus.UNCOVERED
    assert "Assigned 2500 of 4000" in line.coverage_result.reason


def test_hard_reservation_inside_the_same_bu_still_nets_off(db_session):
    """The scoping must not become a hole: a reservation made by ANOTHER
    CUSTOMER IN THE SAME BU draws on the same physical stock, so it is still
    netted off -- 'reserved stock is never free pool' survives."""
    bu = _bu(db_session, "One BU")
    primary = _product(db_session, grade="13CR80")
    substitute = _product(db_session, grade="13CR110")
    _stock(db_session, bu, primary, 0)
    _stock(db_session, bu, substitute, 4000)

    from app.engines.substitution import decide_approval, request_approval

    c_a, node_a = _customer(db_session, "Cust A", bu, AllocationPolicy.HARD)
    c_b, node_b = _customer(db_session, "Cust B", bu, AllocationPolicy.HARD)

    line_a = _line(db_session, _well(db_session, node_a, "W-A"), primary, 4000)
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.add(
        CustomerSubstitutionRule(
            customer_id=c_a.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db_session.flush()
    approval = request_approval(db_session, line_a, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    recompute_customer(db_session, c_a)
    assert _status(line_a) == CoverageStatus.COVERED_VIA_SUBSTITUTE

    # Customer B, same BU, reserves every unit of the substitute.
    line_b = _line(db_session, _well(db_session, node_b, "W-B"), substitute, 4000)
    _assign(db_session, line_b, substitute, 4000)

    recompute_customer(db_session, c_a)

    assert _status(line_a) != CoverageStatus.COVERED_VIA_SUBSTITUTE


# --------------------------------------------------------------------------
# 5. Sharing analysis -- scenario fixture
# --------------------------------------------------------------------------


def _sharing_scenario(db_session):
    """One BU with a needy HARD customer and a SOFT customer holding surplus,
    plus a second BU stuffed with the same product.

    BU North holds 9000. Soft Co consumes 2000 (Covered) -> 7000 surplus.
    Hard Co needs 5000 with NOTHING assigned -> Uncovered under hard allocation
    even though the steel is on the shelf. 5000 <= 7000, so sharing WOULD cover
    it.

    BU Gulf holds 50000 of the identical product and must never be offered.
    """
    bu_north = _bu(db_session, "North")
    bu_gulf = _bu(db_session, "Gulf")
    product = _product(db_session)
    _stock(db_session, bu_north, product, 9000)
    _stock(db_session, bu_gulf, product, 50000)

    soft_co, soft_node = _customer(
        db_session, "Soft Co", bu_north, AllocationPolicy.SOFT
    )
    hard_co, hard_node = _customer(
        db_session, "Hard Co", bu_north, AllocationPolicy.HARD
    )
    gulf_co, gulf_node = _customer(
        db_session, "Gulf Co", bu_gulf, AllocationPolicy.SOFT
    )

    surplus_line = _line(
        db_session, _well(db_session, soft_node, "W-Surplus"), product, 2000
    )
    needy_line = _line(
        db_session, _well(db_session, hard_node, "W-Needy"), product,
        5000, days_out=FAR_ROS_DAYS + 10,
    )
    gulf_line = _line(
        db_session, _well(db_session, gulf_node, "W-Gulf"), product, 1000
    )

    for customer in (soft_co, hard_co, gulf_co):
        recompute_customer(db_session, customer)

    assert _status(surplus_line) == CoverageStatus.COVERED
    assert _status(needy_line) == CoverageStatus.UNCOVERED

    return {
        "bu_north": bu_north, "bu_gulf": bu_gulf, "product": product,
        "soft_co": soft_co, "hard_co": hard_co, "gulf_co": gulf_co,
        "surplus_line": surplus_line, "needy_line": needy_line,
        "gulf_line": gulf_line,
    }


# --------------------------------------------------------------------------
# 6. Sharing analysis -- behaviour
# --------------------------------------------------------------------------


def test_sharing_analysis_finds_surplus_within_the_bu(db_session):
    s = _sharing_scenario(db_session)

    result = cross_customer_sharing(db_session, s["hard_co"])

    assert result.business_unit_id == s["bu_north"].id
    assert result.donor_customer_ids == (s["soft_co"].id,)
    assert len(result.uncovered_lines) == 1

    outcome = result.uncovered_lines[0]
    assert outcome.demand_line_id == s["needy_line"].id
    assert outcome.would_be_covered is True
    assert outcome.shared_quantity == 5000
    assert outcome.shortfall == 0
    assert [c.from_customer_name for c in outcome.contributions] == ["Soft Co"]
    assert outcome.contributions[0].quantity == 5000
    assert result.covered_by_sharing_count == 1
    assert result.still_uncovered_count == 0

    # And the surplus arithmetic is reported, not just the verdict.
    (surplus,) = result.product_surplus
    assert surplus.bu_on_hand == 9000
    assert surplus.committed_in_bu == 2000  # Soft Co's covered 2000; Hard Co 0
    assert surplus.shareable == 7000


def test_sharing_analysis_never_offers_inventory_from_another_bu(db_session):
    """BU Gulf holds 50000 of the identical product. Not one unit is offered."""
    s = _sharing_scenario(db_session)

    # Make BU North genuinely short so the ONLY way to cover is to cross the BU.
    db_session.query(InventoryOnHand).filter(
        InventoryOnHand.business_unit_id == s["bu_north"].id
    ).one().quantity = 1000
    db_session.flush()
    recompute_customer(db_session, s["soft_co"])
    recompute_customer(db_session, s["hard_co"])

    result = cross_customer_sharing(db_session, s["hard_co"])

    (outcome,) = result.uncovered_lines
    assert outcome.would_be_covered is False
    assert outcome.shared_quantity == 0
    (surplus,) = result.product_surplus
    # 1000 on hand, all of it consumed by Soft Co's covered 1000... it demanded
    # 2000, which 1000 cannot cover, so Soft Co commits nothing and 1000 is
    # spare. Either way the number is bounded by BU North, never by BU Gulf.
    assert surplus.bu_on_hand == 1000
    assert surplus.shareable <= 1000
    assert s["gulf_co"].id not in result.donor_customer_ids

    # Symmetrically, BU Gulf is not offered BU North's stock either.
    gulf_result = cross_customer_sharing(db_session, s["gulf_co"])
    assert gulf_result.business_unit_id == s["bu_gulf"].id
    assert gulf_result.donor_customer_ids == ()


def test_sharing_analysis_only_offers_genuine_surplus_never_committed_stock(
    db_session,
):
    """Robbing Peter to pay Paul is refused.

    Soft Co's demand is raised to 8000 (still Covered -- 8000 <= 9000), leaving
    only 1000 of genuine surplus. Hard Co's 5000 must NOT be reported coverable:
    the only way to find 5000 would be to take stock off Soft Co's covered line,
    which would uncover Soft Co and leave the BU no better off.
    """
    s = _sharing_scenario(db_session)
    s["surplus_line"].quantity = 8000
    db_session.flush()
    recompute_customer(db_session, s["soft_co"])
    assert _status(s["surplus_line"]) == CoverageStatus.COVERED

    result = cross_customer_sharing(db_session, s["hard_co"])

    (surplus,) = result.product_surplus
    assert surplus.committed_in_bu == 8000
    assert surplus.shareable == 1000

    (outcome,) = result.uncovered_lines
    assert outcome.would_be_covered is False
    assert outcome.shared_quantity == 0
    assert outcome.shortfall == 4000
    assert outcome.contributions == ()
    assert "not offered" in outcome.explanation
    assert result.still_uncovered_count == 1

    # Soft Co's official verdict is untouched by having been asked about.
    assert _status(s["surplus_line"]) == CoverageStatus.COVERED


def test_hard_assignment_reserved_to_an_uncovered_line_is_not_surplus(db_session):
    """Committed is wider than consumed.

    Under HARD an assignment stays reserved to its line even when that line
    ended up UNCOVERED (the rule app.engines.allocation enforces). Soft Co is
    replaced here by a hard customer holding 6000 reserved against a 9000
    requirement it cannot meet: it draws nothing, but that 6000 is still its
    steel and must not be handed to somebody else.
    """
    bu = _bu(db_session, "Reserve BU")
    product = _product(db_session)
    _stock(db_session, bu, product, 9000)

    holder, holder_node = _customer(
        db_session, "Holder Co", bu, AllocationPolicy.HARD
    )
    needy, needy_node = _customer(db_session, "Needy Co", bu, AllocationPolicy.HARD)

    held_line = _line(
        db_session, _well(db_session, holder_node, "W-Held"), product, 9000
    )
    _assign(db_session, held_line, product, 6000)
    needy_line = _line(
        db_session, _well(db_session, needy_node, "W-Needy"), product, 5000
    )
    recompute_customer(db_session, holder)
    recompute_customer(db_session, needy)

    # Partial assignment, no pool top-up under HARD -> the holder is uncovered
    # and has consumed nothing at all.
    assert _status(held_line) == CoverageStatus.UNCOVERED
    assert _status(needy_line) == CoverageStatus.UNCOVERED

    result = cross_customer_sharing(db_session, needy)

    (surplus,) = result.product_surplus
    # 6000 reserved to the holder's line, so only 3000 is genuine surplus --
    # NOT the 9000 a consumption-only definition would have offered.
    assert surplus.committed_in_bu == 6000
    assert surplus.shareable == 3000

    (outcome,) = result.uncovered_lines
    assert outcome.would_be_covered is False
    assert outcome.shortfall == 2000


def test_sharing_never_offers_a_soft_customers_hard_assigned_steel(db_session):
    """A SOFT donor's hard assignment is not surplus either.

    `committed` used to be `drawn` only under SOFT, on the reasoning that "no
    reservations exist under SOFT". That is true of the soft customer's OWN
    coverage -- pooling means its assignments do not constrain it -- and false of
    what it may give away. The assignment is a read-only Oracle fact tying that
    steel to one specific demand line; offering it to a neighbour would have the
    platform override the reservation through the analysis path, which is exactly
    the hole the coverage engine now closes one level up.

        BU holds 5000. Soft Donor demands 1000 (Covered) but holds 4000
        hard-assigned in Oracle. Needy Co needs 2000.

    Genuine surplus is 5000 - max(4000, 1000) = 1000, so the 2000 line is NOT
    coverable. On the old rule committed was 1000, surplus looked like 4000, and
    the analysis would have offered somebody else's reserved steel.
    """
    bu = _bu(db_session, "Soft Reserve BU")
    product = _product(db_session)
    _stock(db_session, bu, product, 5000)

    donor, donor_node = _customer(
        db_session, "Soft Donor Co", bu, AllocationPolicy.SOFT
    )
    needy, needy_node = _customer(db_session, "Needy Co", bu, AllocationPolicy.HARD)

    donor_line = _line(
        db_session, _well(db_session, donor_node, "W-SoftDonor"), product, 1000
    )
    _assign(db_session, donor_line, product, 4000)
    needy_line = _line(
        db_session, _well(db_session, needy_node, "W-Needy"), product, 2000
    )
    recompute_customer(db_session, donor)
    recompute_customer(db_session, needy)

    # Pooling is intact: the soft donor's own assignment did not constrain it, and
    # its 1000 line is Covered out of the 5000 the BU holds.
    assert _status(donor_line) == CoverageStatus.COVERED
    assert _status(needy_line) == CoverageStatus.UNCOVERED

    result = cross_customer_sharing(db_session, needy)

    (surplus,) = result.product_surplus
    assert surplus.bu_on_hand == 5000
    assert surplus.committed_in_bu == 4000
    assert surplus.shareable == 1000

    (outcome,) = result.uncovered_lines
    assert outcome.would_be_covered is False
    assert outcome.shared_quantity == 0
    assert outcome.shortfall == 1000
    assert result.covered_by_sharing_count == 0


def test_a_customer_with_no_demand_for_the_product_is_not_credited_as_donor(
    db_session,
):
    """Slack alone does not make somebody a donor.

    A customer that has never ordered this product has consumed none of the BU
    figure, so a naive `bu_on_hand - committed` scores it the MAXIMUM possible
    slack and reports it as the biggest donor of a product it has never heard of.
    The surplus is real, but it is BU-level unallocated stock -- not a named
    customer's generosity -- and mislabelling it would send a planner to phone the
    wrong operator.
    """
    bu = _bu(db_session, "Attrib BU")
    needed = _product(db_session, grade="13CR80")
    unrelated = _product(db_session, grade="13CR110")
    _stock(db_session, bu, needed, 9000)
    _stock(db_session, bu, unrelated, 9000)

    # Bystander demands only the UNRELATED product, so it has no claim on
    # `needed` at all.
    bystander, bystander_node = _customer(
        db_session, "Bystander Co", bu, AllocationPolicy.SOFT
    )
    needy, needy_node = _customer(db_session, "Needy Co", bu, AllocationPolicy.HARD)

    _line(db_session, _well(db_session, bystander_node, "W-By"), unrelated, 1000)
    needy_line = _line(
        db_session, _well(db_session, needy_node, "W-Needy"), needed, 5000
    )
    recompute_customer(db_session, bystander)
    recompute_customer(db_session, needy)
    assert _status(needy_line) == CoverageStatus.UNCOVERED

    result = cross_customer_sharing(db_session, needy)

    (outcome,) = result.uncovered_lines
    # The surplus is genuinely there, so sharing DOES resolve the line...
    assert outcome.would_be_covered is True
    assert outcome.shared_quantity == 5000
    # ...but it is not attributed to the bystander.
    assert outcome.contributions == ()
    assert "BU-level unallocated stock" in outcome.explanation
    assert "Bystander" not in outcome.explanation


def test_sharing_analysis_ignores_lines_that_are_already_covered(db_session):
    """Scoped to UNCOVERED demand. A Covered / CoveredViaSubstitute line is not
    the planner's question and must not appear."""
    s = _sharing_scenario(db_session)

    result = cross_customer_sharing(db_session, s["soft_co"])

    # Soft Co's only line is Covered, so there is nothing to analyse.
    assert result.uncovered_lines == ()
    assert result.covered_by_sharing_count == 0
    assert result.still_uncovered_count == 0


def test_sharing_analysis_orders_uncovered_lines_by_earliest_ros(db_session):
    """Scarce surplus goes to the most urgent line, matching the coverage
    engine's earliest-ROS-first rule -- the late line is the one that loses."""
    bu = _bu(db_session, "Order BU")
    product = _product(db_session)
    _stock(db_session, bu, product, 5000)

    donor, donor_node = _customer(db_session, "Donor Co", bu, AllocationPolicy.SOFT)
    needy, needy_node = _customer(db_session, "Needy Co", bu, AllocationPolicy.HARD)

    # Donor consumes nothing, so all 5000 is surplus.
    late = _line(
        db_session, _well(db_session, needy_node, "W-Late"), product,
        4000, days_out=FAR_ROS_DAYS + 90,
    )
    early = _line(
        db_session, _well(db_session, needy_node, "W-Early"), product,
        4000, days_out=FAR_ROS_DAYS,
    )
    recompute_customer(db_session, donor)
    recompute_customer(db_session, needy)
    assert _status(early) == CoverageStatus.UNCOVERED
    assert _status(late) == CoverageStatus.UNCOVERED

    result = cross_customer_sharing(db_session, needy)

    by_line = {o.demand_line_id: o for o in result.uncovered_lines}
    assert by_line[early.id].would_be_covered is True
    assert by_line[late.id].would_be_covered is False
    assert result.uncovered_lines[0].demand_line_id == early.id


# --------------------------------------------------------------------------
# 7. Sharing analysis -- the no-write guarantee
# --------------------------------------------------------------------------


def _coverage_snapshot(db_session):
    """Every persisted coverage fact, as plain comparable values."""
    rows = {
        r.demand_line_id: (r.status, r.reason, r.fulfilled_by_product_id)
        for r in db_session.query(CoverageResult).all()
    }
    wells = {w.id: w.coverage_status for w in db_session.query(Well).all()}
    return rows, wells


def test_sharing_analysis_performs_no_writes(db_session):
    """CoverageResult rows and Well.coverage_status must be identical before and
    after -- the analysis is a projection, never a recalculation."""
    s = _sharing_scenario(db_session)
    db_session.flush()

    before = _coverage_snapshot(db_session)
    before_counts = (
        len(db_session.new), len(db_session.dirty), len(db_session.deleted)
    )

    for customer in (s["hard_co"], s["soft_co"], s["gulf_co"]):
        cross_customer_sharing(db_session, customer)

    after = _coverage_snapshot(db_session)
    assert after == before
    assert (
        len(db_session.new), len(db_session.dirty), len(db_session.deleted)
    ) == before_counts

    # And nothing was queued that a later commit could flush.
    db_session.flush()
    assert _coverage_snapshot(db_session) == before


def test_sharing_analysis_does_not_create_coverage_rows_for_uncovered_lines(
    db_session,
):
    """Specifically: the line the analysis says WOULD be covered keeps its
    official Uncovered verdict, and no extra row appears anywhere."""
    s = _sharing_scenario(db_session)
    row_count_before = db_session.query(CoverageResult).count()

    result = cross_customer_sharing(db_session, s["hard_co"])
    assert result.uncovered_lines[0].would_be_covered is True

    assert db_session.query(CoverageResult).count() == row_count_before
    assert _status(s["needy_line"]) == CoverageStatus.UNCOVERED
    assert s["needy_line"].well.coverage_status == CoverageStatus.UNCOVERED.value


def test_sharing_analysis_write_tripwire_fires(db_session):
    """The layer-4 guard is real, not decorative: if anything ever does leave a
    pending change in the session, the analysis raises instead of letting a GET
    quietly overwrite the official answer."""
    from app.engines import sharing

    s = _sharing_scenario(db_session)

    original = sharing._customer_pass

    def _sneaky_write(db, customer, bu_on_hand, *args, **kwargs):
        # Exactly the kind of edit the tripwire exists to catch.
        customer.name = customer.name + " (mutated)"
        return original(db, customer, bu_on_hand, *args, **kwargs)

    sharing._customer_pass = _sneaky_write
    try:
        with pytest.raises(AssertionError, match="must not modify the session"):
            cross_customer_sharing(db_session, s["hard_co"])
    finally:
        sharing._customer_pass = original


# --------------------------------------------------------------------------
# 8. Customers with no Business Unit -- no pool, so no verdict
#
# Both tests below used to pin the "unmapped customer is ISOLATED" middle state:
# it read the legacy unscoped `Product.on_hand_qty` and was judged against it, as
# a scope of one. With that column gone there is no quantity in the schema that is
# not BU-scoped, so the state those tests described is not merely unwanted, it is
# impossible by construction -- there is nothing left for such a customer to be
# judged against.
#
# They are therefore REPURPOSED to pin the behaviour that replaced it. This is not
# a weakening: each still asserts that an unmapped customer never draws on
# anybody's stock and is never anybody's donor -- the original point -- and adds
# that the attempt now FAILS LOUDLY instead of quietly succeeding against a global
# figure. Silence was the weaker guarantee; an exception is the stronger one.
# --------------------------------------------------------------------------


def test_customer_with_no_bu_cannot_have_coverage_computed_at_all(db_session):
    """Formerly `test_customer_with_no_bu_does_not_pool_with_other_customers`.

    Two UNMAPPED customers, one product with real stock in a real BU. The original
    test asserted that neither orphan is offered the other's stock and that
    neither is a candidate donor. Both still hold -- and the first one now holds
    for a much better reason: an orphan does not get a pooled-with-nobody verdict,
    it gets no verdict, because on-hand exists only per (BU, product) and it is in
    no BU.

    What the ORIGINAL test could not have caught, and this one does: back when the
    orphans fell through to a global scalar, both of them read THE SAME number as
    their own. They did not pool, but they did both spend the same steel. That is
    the leak, one layer up, and the only way to close it is to refuse.
    """
    bu = _bu(db_session, "Somebody Else's BU")
    product = _product(db_session)
    _stock(db_session, bu, product, 9000)

    orphan_a, node_a = _customer(db_session, "Orphan A", None, AllocationPolicy.HARD)
    orphan_b, node_b = _customer(db_session, "Orphan B", None, AllocationPolicy.SOFT)

    line_a = _line(db_session, _well(db_session, node_a, "W-OA"), product, 5000)
    _line(db_session, _well(db_session, node_b, "W-OB"), product, 1000)

    # Neither orphan can be judged -- not against the other's demand, and above
    # all not against that 9000, which belongs to a BU neither of them is in.
    for orphan in (orphan_a, orphan_b):
        with pytest.raises(InventoryScopeMissing) as excinfo:
            recompute_customer(db_session, orphan)
        assert "not mapped to a Business Unit" in str(excinfo.value)
        assert isinstance(excinfo.value, InventoryNotScoped)

    # No verdict was written for either of them on the way out.
    assert db_session.query(CoverageResult).count() == 0
    assert db_session.get(CoverageResult, line_a.id) is None

    # The read-only sharing analysis still ANSWERS rather than raising -- its
    # honest answer is "there is no BU to share within", and it says so. It offers
    # nothing and names nobody, which is the original assertion, unchanged.
    result = cross_customer_sharing(db_session, orphan_a)

    assert result.business_unit_id is None
    assert result.donor_customer_ids == ()
    assert result.uncovered_lines == ()
    assert result.product_surplus == ()
    assert orphan_b.id not in result.donor_customer_ids
    assert any("not mapped to a Business Unit" in n for n in result.notes)


def test_unmapped_customer_is_not_a_donor_for_a_mapped_one(db_session):
    """The isolation is symmetric, and now it is loud on the orphan's own side.

    Original assertions kept verbatim: the MAPPED customer's sharing analysis
    offers no donors and does not cover its line, so the orphan's demand and
    whatever it might have been holding are invisible to it. Added: the orphan
    cannot be given a verdict of its own either, so there is no longer a state in
    which it looks half-planned. The BU's own 1000 is unaffected by any of it.
    """
    bu = _bu(db_session, "Mapped BU")
    product = _product(db_session)
    _stock(db_session, bu, product, 1000)

    mapped, mapped_node = _customer(db_session, "Mapped Co", bu, AllocationPolicy.SOFT)
    orphan, orphan_node = _customer(db_session, "Orphan Co", None, AllocationPolicy.SOFT)

    line = _line(db_session, _well(db_session, mapped_node, "W-M"), product, 5000)
    orphan_line = _line(
        db_session, _well(db_session, orphan_node, "W-O"), product, 100
    )
    recompute_customer(db_session, mapped)
    assert _status(line) == CoverageStatus.UNCOVERED

    with pytest.raises(InventoryScopeMissing):
        recompute_customer(db_session, orphan)
    assert db_session.get(CoverageResult, orphan_line.id) is None

    result = cross_customer_sharing(db_session, mapped)

    assert result.donor_customer_ids == ()
    (outcome,) = result.uncovered_lines
    assert outcome.would_be_covered is False
    # The mapped customer's own verdict is untouched by the orphan's existence.
    assert _status(line) == CoverageStatus.UNCOVERED


def test_recompute_well_entry_point_still_resolves_the_bu_quantity(db_session):
    """`recompute_well` stays the public entry point and must go through the same
    BU resolution -- not a second, BU-blind path."""
    bu = _bu(db_session, "Entry BU")
    product = _product(db_session)
    _stock(db_session, bu, product, 1000)

    _customer_obj, node = _customer(db_session, "Entry Co", bu)
    well = _well(db_session, node, "W-Entry")
    line = _line(db_session, well, product, quantity=5000)

    recompute_well(db_session, well)

    assert _status(line) == CoverageStatus.UNCOVERED
    assert well.coverage_status == CoverageStatus.UNCOVERED.value


# --------------------------------------------------------------------------
# 9. API route
# --------------------------------------------------------------------------


def test_cross_customer_sharing_route(db_session):
    """GET /analysis/cross-customer-sharing serialises the analysis and, being a
    read-only projection, leaves the persisted coverage answer untouched."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    s = _sharing_scenario(db_session)
    db_session.flush()
    before = _coverage_snapshot(db_session)

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)
        resp = client.get(
            "/analysis/cross-customer-sharing", params={"customer_id": s["hard_co"].id}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["business_unit_name"] == "North"
        assert body["donor_customer_ids"] == [s["soft_co"].id]
        assert body["covered_by_sharing_count"] == 1
        (line,) = body["uncovered_lines"]
        assert line["would_be_covered"] is True
        assert line["shared_quantity"] == 5000
        assert line["contributions"][0]["from_customer_name"] == "Soft Co"
        (surplus,) = body["product_surplus"]
        assert surplus["shareable"] == 7000

        missing = client.get(
            "/analysis/cross-customer-sharing", params={"customer_id": "nope"}
        )
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()

    # The GET wrote nothing.
    assert _coverage_snapshot(db_session) == before


# --------------------------------------------------------------------------
# 10. The closed leak, pinned directly
#
# Everything above tests the engines. This section tests the RULES themselves --
# `app.engines.inventory` in isolation, the shape of the Product model, and the
# HTTP contract -- so a regression is caught at its source rather than by
# whichever screen happens to notice first.
# --------------------------------------------------------------------------


def test_product_has_no_on_hand_qty_attribute(db_session):
    """The column is GONE, not merely unread.

    An unread column is still a second source of truth: the next person who needs
    a quantity finds it sitting there, uses it, and the leak is back with no code
    review to stop it. Absence is the only durable guarantee, so it is asserted.
    """
    product = _product(db_session)

    assert not hasattr(product, "on_hand_qty")
    assert "on_hand_qty" not in {c.name for c in Product.__table__.columns}


def test_on_hand_for_raises_when_the_bu_row_is_missing(db_session):
    """A missing row is UNKNOWN, and unknown is never returned as a number."""
    from app.engines.inventory import on_hand_for, on_hand_map

    bu = _bu(db_session, "Unit BU")
    product = _product(db_session)

    with pytest.raises(InventoryRowMissing) as excinfo:
        on_hand_for(db_session, bu.id, product)
    assert product.description in str(excinfo.value)
    assert bu.id in str(excinfo.value)

    # Bulk resolution obeys the same rule -- one raising path, not two policies.
    with pytest.raises(InventoryRowMissing):
        on_hand_map(db_session, bu.id, {product.id})

    _stock(db_session, bu, product, 4200)
    assert on_hand_for(db_session, bu.id, product) == 4200
    assert on_hand_map(db_session, bu.id, {product.id}) == {product.id: 4200}


def test_on_hand_map_names_every_missing_product_at_once(db_session):
    """One round trip tells the operator everything that has to be loaded."""
    from app.engines.inventory import on_hand_map

    bu = _bu(db_session, "Multi BU")
    a = _product(db_session, grade="13CR80")
    b = _product(db_session, grade="13CR110")
    _stock(db_session, bu, a, 100)

    with pytest.raises(InventoryRowMissing) as excinfo:
        on_hand_map(db_session, bu.id, {a.id, b.id})
    assert b.id in str(excinfo.value)


def test_resolution_raises_for_a_missing_business_unit(db_session):
    """No BU means no pool. Every entry point says so the same way."""
    from app.engines.inventory import on_hand_for, on_hand_map, scoped_customer_ids

    product = _product(db_session)
    orphan, _node = _customer(db_session, "Unit Orphan", None)

    with pytest.raises(InventoryScopeMissing):
        on_hand_for(db_session, None, product)
    with pytest.raises(InventoryScopeMissing):
        on_hand_map(db_session, None, {product.id})
    with pytest.raises(InventoryScopeMissing):
        scoped_customer_ids(db_session, orphan)

    # Asking for NOTHING is not an unresolved quantity, so it does not raise.
    assert on_hand_map(db_session, None, set()) == {}


def test_scope_key_is_gone(db_session):
    """`scope_key` returned ("customer", id) for an unmapped customer, encoding
    the removed "scope of one" behaviour. It had no callers and could never again
    be reached correctly, so the whole helper was deleted rather than left as a
    trap for the next reader.
    """
    from app.engines import inventory

    assert not hasattr(inventory, "scope_key")
    assert not hasattr(inventory, "SCOPE_CUSTOMER")


def test_one_product_two_bus_with_different_quantities_get_independent_verdicts(
    db_session,
):
    """THE case that would have caught the original defect.

    Every product in the old seed happened to be demanded from exactly ONE
    Business Unit, so a resolver that ignored the BU dimension still produced
    correct-looking screens -- the leak was latent. This shape makes it impossible
    to hide: ONE product, TWO BUs, DIFFERENT quantities, the SAME requirement, and
    the two answers must disagree.

        BU Rich   7000 on hand, demands 6000 -> Covered
        BU Poor   2000 on hand, demands 6000 -> Uncovered

    Neither answer is reachable from the other's number, so any single shared
    quantity -- a global column, a cached figure, a `.get(pid, default)` -- would
    have to make one of these two verdicts flip.
    """
    bu_rich = _bu(db_session, "Rich BU")
    bu_poor = _bu(db_session, "Poor BU")
    product = _product(db_session)
    _stock(db_session, bu_rich, product, 7000)
    _stock(db_session, bu_poor, product, 2000)

    rich_co, rich_node = _customer(db_session, "Rich Co", bu_rich)
    poor_co, poor_node = _customer(db_session, "Poor Co", bu_poor)
    rich_line = _line(
        db_session, _well(db_session, rich_node, "W-Rich"), product, 6000
    )
    poor_line = _line(
        db_session, _well(db_session, poor_node, "W-Poor"), product, 6000
    )

    recompute_customer(db_session, rich_co)
    recompute_customer(db_session, poor_co)

    assert _status(rich_line) == CoverageStatus.COVERED
    assert _status(poor_line) == CoverageStatus.UNCOVERED

    # Independence in BOTH directions, each verdict moving only with its own row.
    # Emptying the rich BU must not rescue the poor one...
    _stock(db_session, bu_rich, product, 0)
    recompute_customer(db_session, rich_co)
    recompute_customer(db_session, poor_co)
    assert _status(rich_line) == CoverageStatus.UNCOVERED
    assert _status(poor_line) == CoverageStatus.UNCOVERED

    # ...and filling the poor BU must not depend on the rich one at all.
    _stock(db_session, bu_poor, product, 6000)
    recompute_customer(db_session, poor_co)
    recompute_customer(db_session, rich_co)
    assert _status(poor_line) == CoverageStatus.COVERED
    assert _status(rich_line) == CoverageStatus.UNCOVERED


# --------------------------------------------------------------------------
# 11. The HTTP contract for an unresolvable quantity
#
# An unresolved quantity must reach the caller as an EXPLAINED 4xx, not an opaque
# traceback, and the two causes get different codes because they are different
# problems with different owners. See app.main for the full justification.
# --------------------------------------------------------------------------


def _http(db_session):
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app, raise_server_exceptions=False), app


def _revise(client, line):
    """POST a no-op revision -- a route that genuinely has to resolve a quantity,
    since it recomputes coverage before responding."""
    return client.post(
        "/demand-lines/%s/revisions" % line.id,
        json={
            "quantity": line.quantity,
            "ros_date": line.ros_date.isoformat(),
            # No "status": it is a property of the WELL now, and this route refuses
            # one with a 400 that points at PUT /wells/{id}/demand-status. Sending it
            # here would mask the 409/424 these tests are about behind that 400.
            "profile": "Primary",
        },
    )


def test_unmapped_customer_surfaces_as_409_with_an_explanation(db_session):
    """409 Conflict: the request was fine, the stored customer record is not."""
    product = _product(db_session)
    orphan, node = _customer(db_session, "HTTP Orphan", None)
    line = _line(db_session, _well(db_session, node, "W-409"), product, 5000)

    client, app = _http(db_session)
    try:
        resp = _revise(client, line)
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "inventory_scope_missing"
        assert "not mapped to a Business Unit" in body["detail"]
        assert "Map the customer" in body["detail"]
    finally:
        app.dependency_overrides.clear()


def test_missing_inventory_row_surfaces_as_424_with_an_explanation(db_session):
    """424 Failed Dependency: the Oracle-projected row this answer needs is
    absent. Not the caller's doing (a 400 would misattribute it), not a
    malfunction of this service (5xx), and every resource named in the URL exists
    (404 would be a lie)."""
    bu = _bu(db_session, "HTTP BU")
    product = _product(db_session)  # deliberately unstocked
    customer, node = _customer(db_session, "HTTP Co", bu)
    line = _line(db_session, _well(db_session, node, "W-424"), product, 5000)

    client, app = _http(db_session)
    try:
        resp = _revise(client, line)
        assert resp.status_code == 424
        body = resp.json()
        assert body["error"] == "inventory_row_missing"
        assert body["business_unit_id"] == bu.id
        assert body["product_id"] == product.id
        assert "UNKNOWN" in body["detail"]
    finally:
        app.dependency_overrides.clear()


def test_substitution_candidates_route_is_bu_scoped(db_session):
    """GET /demand-lines/{id}/substitution-candidates passes no availability map,
    so it used to read the global `Product.on_hand_qty` and report another BU's
    steel as this line's `available_qty`. It now resolves through the same
    BU-scoped resolver as everything else, and the product payload it embeds
    carries no quantity at all.
    """
    from app.engines.substitution import decide_approval, request_approval

    bu_ours = _bu(db_session, "Route Ours")
    bu_theirs = _bu(db_session, "Route Theirs")
    primary = _product(db_session, grade="13CR80")
    substitute = _product(db_session, grade="13CR110")
    _stock(db_session, bu_ours, primary, 0)
    _stock(db_session, bu_ours, substitute, 1000)
    # Plenty of the same substitute next door. It must not reach the payload.
    _stock(db_session, bu_theirs, substitute, 90000)

    customer, node = _customer(db_session, "Route Co", bu_ours)
    line = _line(db_session, _well(db_session, node, "W-Route"), primary, 4000)
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

    client, app = _http(db_session)
    try:
        resp = client.get("/demand-lines/%s/substitution-candidates" % line.id)
        assert resp.status_code == 200
        (candidate,) = resp.json()
        # OUR 1000 -- not their 90000, and not a bare 0.
        assert candidate["available_qty"] == 1000
        assert candidate["usable"] is False
        assert candidate["blocking_layer"] == "insufficient-inventory"
        assert "on_hand_qty" not in candidate["product"]
    finally:
        app.dependency_overrides.clear()


def test_sharing_official_status_is_the_stored_verdict_not_a_hardcoded_label(
    db_session,
):
    """Display-only fix (2026-08-12): each row names its ACTUAL verdict.

    The panel used to print "Uncovered (customer-scoped)" on every row, which
    misrepresented lines whose stored verdict was PendingApproval or
    Unrecoverable. The label now comes verbatim from the line's CoverageResult.
    """
    s = _sharing_scenario(db_session)
    result = cross_customer_sharing(db_session, s["hard_co"])
    outcome = result.uncovered_lines[0]
    stored = s["needy_line"].coverage_result
    assert stored is not None
    assert outcome.official_status == stored.status.value
    assert "(customer-scoped)" not in outcome.official_status
