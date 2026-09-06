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


def test_the_pool_is_the_business_unit_and_it_cannot_over_promise(db_session):
    """THE 'one Business Unit, one division of the steel' test.

    Two customers, ONE BU, one product, 6000 on hand. Customer B demands 6000 with
    an EARLIER ROS than customer A's 5000, so B takes the shelf and A is left with
    nothing.

    This test asserted the OPPOSITE until 2026-09-06. Coverage was computed per
    customer against the whole Business Unit quantity, so both were reported
    Covered off the same 6000 and the BU had promised 11,000 it did not have. The
    product owner ruled that out (D01): the pool is contested once, earliest ROS
    first, across every customer in the Business Unit.

    Note what did NOT change: A's verdict moves because of B's demand, which is
    the whole point, but nothing here crosses a BUSINESS UNIT boundary -- that one
    is still absolute, and `test_a_second_bu_is_invisible_to_the_first` pins it.
    """
    bu = _bu(db_session, "Shared BU")
    product = _product(db_session)
    _stock(db_session, bu, product, 6000)

    c_a, node_a = _customer(db_session, "Cust A", bu)
    c_b, node_b = _customer(db_session, "Cust B", bu)
    line_a = _line(db_session, _well(db_session, node_a, "W-A"), product, quantity=5000)
    recompute_customer(db_session, c_a)
    # Alone in the Business Unit, A is covered -- there is nobody to compete with.
    assert _status(line_a) == CoverageStatus.COVERED

    line_b = _line(
        db_session, _well(db_session, node_b, "W-B"), product,
        quantity=6000, days_out=FAR_ROS_DAYS - 100,
    )
    # Recomputing EITHER customer now re-divides the whole Business Unit, so one
    # call is enough and a second cannot change the answer.
    recompute_customer(db_session, c_b)

    assert _status(line_b) == CoverageStatus.COVERED
    assert _status(line_a) == CoverageStatus.UNCOVERED
    # The arithmetic is stated, not implied: A drew nothing, and the whole 5000 is
    # what a mill order would have to cover.
    assert line_a.coverage_result.drawn_company == 0
    assert line_a.coverage_result.residual == 5000

    recompute_customer(db_session, c_a)
    assert _status(line_b) == CoverageStatus.COVERED
    assert _status(line_a) == CoverageStatus.UNCOVERED


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


# --------------------------------------------------------------------------
# 6. Sharing analysis -- behaviour
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 7. Sharing analysis -- the no-write guarantee
# --------------------------------------------------------------------------


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

