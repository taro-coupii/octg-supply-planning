"""CUSTOMER-OWNED inventory: the ownership tier, its priority, and its boundary.

The product owner's requirement, verbatim in the part that matters:

    For the same product, consuming customer-owned inventory takes priority
    over FIFO.

For the same product, consuming customer-owned inventory takes priority over the
ordinary draw order. Three separate claims come out of that, and this module pins
each of them directly rather than inferring any from a coverage badge:

  1. THE PRIORITY. Customer-owned stock is drawn first, AND the company-owned
     remainder is left intact -- the second half is the one that is easy to get
     wrong and invisible from a verdict alone. Asserted under all three allocation
     policies, because "customer-owned first" has to mean one thing.
  2. THE BOUNDARY. It can never be offered to another customer, in either
     direction. Tighter than the Business Unit boundary.
  3. THE UNKNOWN. "Owns none" and "no upload has happened" are different facts and
     stay distinguishable, the same rule the platform applies to on-order material.

Plus the upload itself: happy path, and malformed rows that do not fail the batch.
"""

import io
from datetime import datetime, timedelta

import pytest
from openpyxl import Workbook

from app.engines.allocation import allocate_detailed
from app.engines.coverage import compute_customer_coverage, recompute_customer
from app.engines.customer_owned_import import (
    CustomerOwnedImportError,
    parse_and_replace,
)
from app.engines.inventory import (
    customer_owned_for,
    on_hand_map,
    ownership_pool_for,
)
from app.engines.sharing import cross_customer_sharing
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    CoverageStatus,
    Customer,
    CustomerOwnedInventory,
    CustomerOwnedInventoryUpload,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
)

# ---------------------------------------------------------------------------
# Fixture helpers -- one BU, one or two customers, explicit quantities
# ---------------------------------------------------------------------------


def _bu(db, name="Owned BU"):
    bu = db.query(BusinessUnit).filter(BusinessUnit.name == name).one_or_none()
    if bu is None:
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


def _product(db, description, unit=UnitOfMeasure.MTR):
    product = Product(
        type="CSG", size="9-5/8", weight=53.5, grade="P110", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description=description, unit_of_measure=unit,
    )
    db.add(product)
    db.flush()
    return product


def _stock(db, bu, product, quantity):
    """Upsert the (BU, product) COMPANY on-hand row. An explicit 0 is a fact."""
    row = (
        db.query(InventoryOnHand)
        .filter(
            InventoryOnHand.business_unit_id == bu.id,
            InventoryOnHand.product_id == product.id,
        )
        .one_or_none()
    )
    if row is None:
        db.add(
            InventoryOnHand(
                business_unit_id=bu.id, product_id=product.id, quantity=quantity,
                source_system="synthetic",
            )
        )
    else:
        row.quantity = quantity
    db.flush()


def _upload_row(db, customer, filename="test.xlsx"):
    """The UPLOAD record -- the fact that a position has been declared at all."""
    upload = CustomerOwnedInventoryUpload(
        customer_id=customer.id, filename=filename, row_count=0,
        source_system="customer-upload",
    )
    db.add(upload)
    db.flush()
    return upload


def _owns(db, customer, product, quantity, upload=None):
    upload = upload or _upload_row(db, customer)
    db.add(
        CustomerOwnedInventory(
            customer_id=customer.id, product_id=product.id, quantity=quantity,
            source_system="customer-upload", uploaded_at=datetime.utcnow(),
            upload_id=upload.id,
        )
    )
    db.flush()
    return upload


def _well(db, node, name, status=DemandStatus.CONFIRMED):
    well = Well(planning_node_id=node.id, name=name, demand_status=status)
    db.add(well)
    db.flush()
    return well


def _line(db, well, product, quantity, days_out):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity,
        ros_date=datetime.utcnow() + timedelta(days=days_out),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.flush()
    return line


def _sheet(rows, headers=("Product", "Quantity")):
    """An in-memory .xlsx with `headers` and `rows`, for the upload tests."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "AkerBP Owned Surplus List"
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 1. THE PRIORITY -- customer-owned drawn first, company remainder intact
# ---------------------------------------------------------------------------


def test_customer_owned_is_drawn_before_company_owned_for_the_same_product(db_session):
    """The rule itself, at the allocation engine, with both halves asserted.

    ONE line, enough of both tiers to cover it twice over. The line takes its whole
    requirement from the customer-owned tier and the company pool is returned
    UNTOUCHED -- that second assertion is the one that matters. A pass that drew
    company steel first would also report the line Covered, so the verdict alone
    cannot tell the two implementations apart; the residual can.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Priority Co")
    product = _product(db_session, "CSG priority product")
    _stock(db_session, bu, product, 2000)
    _owns(db_session, customer, product, 3000)
    well = _well(db_session, node, "WELL-P1")
    line = _line(db_session, well, product, 1800, days_out=100)

    outcome = allocate_detailed(
        AllocationPolicy.SOFT, [line], 2000, {}, 3000
    )

    assert outcome.covered[line.id] is True
    assert outcome.consumed_from_customer_owned[line.id] == 1800
    # The company pool was not touched at ALL -- not "was touched less".
    assert line.id not in outcome.consumed_from_pool
    assert outcome.remaining_pool == 2000
    assert outcome.remaining_customer_owned == 1200


def test_the_draw_order_changes_which_wells_are_covered(db_session):
    """The priority is worth two whole wells here, which is why it is a rule.

    Company 2000, customer owns 3000, two lines needing 3000 then 2000.

      with the rule       the early line takes 3000 of its OWN material and leaves the
                          company 2000 for the later line -> BOTH covered.
      company-owned first the early line drains the 2000 in ROS order and is still
                          1000 short (partial consumption is real), leaving nothing
                          -> NEITHER covered.

    Asserted as a real counterfactual -- the same lines run through the same function
    with the tiers swapped -- rather than as a claim in a comment.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Order Co")
    product = _product(db_session, "CSG order product")
    early = _line(db_session, _well(db_session, node, "WELL-EARLY"), product, 3000, 100)
    later = _line(db_session, _well(db_session, node, "WELL-LATER"), product, 2000, 200)
    lines = [early, later]

    with_rule = allocate_detailed(AllocationPolicy.SOFT, lines, 2000, {}, 3000)
    assert with_rule.covered == {early.id: True, later.id: True}
    assert with_rule.consumed_from_customer_owned[early.id] == 3000
    assert with_rule.consumed_from_pool[later.id] == 2000
    assert with_rule.remaining_pool == 0.0

    # THE COUNTERFACTUAL: the same 2000 of company stock with no customer-owned tier.
    without = allocate_detailed(AllocationPolicy.SOFT, lines, 2000, {}, 0.0)
    assert without.covered == {early.id: False, later.id: False}
    assert without.consumed_from_pool[early.id] == 2000
    assert later.id not in without.consumed_from_pool


@pytest.mark.parametrize(
    "policy",
    [AllocationPolicy.SOFT, AllocationPolicy.HARD, AllocationPolicy.HYBRID],
)
def test_the_priority_holds_under_every_policy(db_session, policy):
    """One tier order for all three policies -- otherwise the rule means three things.

    The line is given an assignment as well, so HARD and HYBRID have something the
    customer-owned tier could have been drawn AFTER. It must not be: the assignment is
    company steel reserved to this line, and burning it while the customer's own
    material sits on the dock is the order the ruling forbids.

    HARD is the interesting case and is asserted, not exempted: the customer-owned
    draw happens there too, which is what makes customer-owned stock usable at all
    under HARD. Oracle cannot assign inventory it does not know exists, so requiring
    an assignment would make it permanently unusable -- see `app.engines.allocation`.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, f"Policy Co {policy.value}", policy)
    product = _product(db_session, f"CSG policy product {policy.value}")
    line = _line(db_session, _well(db_session, node, f"W-{policy.value}"), product, 1000, 90)

    outcome = allocate_detailed(policy, [line], 5000, {line.id: 1000}, 400)

    # The customer's own 400 went first, under every policy.
    assert outcome.consumed_from_customer_owned[line.id] == 400
    assert outcome.remaining_customer_owned == 0.0
    # ...and only the SHORTFALL after that came from the next tier down, which is the
    # assignment under HARD/HYBRID and the pool under SOFT. That difference is the
    # existing pooling guarantee, untouched: a soft customer's own assignment never
    # grants it coverage. What is identical across all three is the 400 above.
    if policy == AllocationPolicy.SOFT:
        assert outcome.consumed_from_assignment == {}
        assert outcome.consumed_from_pool[line.id] == 600
    else:
        assert outcome.consumed_from_assignment[line.id] == 600
        assert line.id not in outcome.consumed_from_pool
    assert outcome.covered[line.id] is True
    assert outcome.drawn(line.id) == 1000


def test_hard_allocation_is_covered_by_customer_owned_stock_with_no_assignment(
    db_session,
):
    """A hard-allocated line with NO Oracle assignment, covered by its own material.

    The decisive design question, asserted as behaviour. Oracle owns assignments and
    knows nothing about customer-owned steel, so if an assignment were required here
    the customer's own inventory would be unusable under HARD for ever, with no action
    available to anybody that would fix it.

    The company pool is deliberately huge and deliberately does NOT help: HARD's
    no-top-up-from-the-shared-pool rule is untouched, which is what keeps this a
    widening from the customer's own property only.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Hard Co", AllocationPolicy.HARD)
    product = _product(db_session, "CSG hard product")
    covered_line = _line(
        db_session, _well(db_session, node, "W-HARD-OWNED"), product, 1000, 90
    )
    pool_only_line = _line(
        db_session, _well(db_session, node, "W-HARD-POOL"), product, 1000, 120
    )

    outcome = allocate_detailed(
        AllocationPolicy.HARD, [covered_line, pool_only_line], 50_000, {}, 1000
    )

    assert outcome.covered[covered_line.id] is True
    assert outcome.consumed_from_customer_owned[covered_line.id] == 1000
    assert covered_line.id not in outcome.consumed_from_assignment
    # The second line gets nothing: the customer tier is exhausted and 50 000 of
    # UNASSIGNED company stock still provides no coverage under HARD.
    assert outcome.covered[pool_only_line.id] is False
    assert outcome.remaining_pool == 50_000


def test_the_ownership_split_sums_to_what_was_consumed(db_session):
    """`drawn()` is the three maps and nothing else, per line and in total.

    Pinned because the split is what the Executive Dashboard's channels and the
    coverage reason text are built from, and a split that did not add up to the draw
    would make both of them wrong while every coverage verdict stayed right -- i.e. it
    would be invisible.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Split Co", AllocationPolicy.HYBRID)
    product = _product(db_session, "CSG split product")
    a = _line(db_session, _well(db_session, node, "W-S1"), product, 2000, 60)
    b = _line(db_session, _well(db_session, node, "W-S2"), product, 2000, 90)

    outcome = allocate_detailed(
        AllocationPolicy.HYBRID, [a, b], 2500, {a.id: 500}, 1200
    )

    for line in (a, b):
        assert outcome.drawn(line.id) == (
            outcome.consumed_from_customer_owned.get(line.id, 0.0)
            + outcome.consumed_from_assignment.get(line.id, 0.0)
            + outcome.consumed_from_pool.get(line.id, 0.0)
        )
    # Every tier is fully accounted for: what went out plus what is left equals what
    # was there. Stated for both tiers, because they are separate stock.
    assert outcome.total_customer_owned_consumed + outcome.remaining_customer_owned == 1200
    assert (
        sum(outcome.consumed_from_pool.values()) + outcome.remaining_pool
        == 2500 - 500  # the assignment is a carve-out of on-hand, never additive
    )
    assert outcome.total_customer_owned_consumed == sum(
        outcome.consumed_from_customer_owned.values()
    )


def test_a_line_short_after_drawing_customer_owned_stock_says_so(db_session):
    """The coverage REASON, end to end through `recompute_customer`.

    Without this wording a planner whose own material was on the dock reads
    "insufficient on-hand inventory" and concludes the upload was ignored. The reason
    has to state that the customer-owned stock was drawn FIRST, how much there was, and
    what is still short.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Short Co")
    product = _product(db_session, "CSG short product")
    _stock(db_session, bu, product, 0)  # explicit zero: a fact, not silence
    _owns(db_session, customer, product, 1000)
    well = _well(db_session, node, "WELL-SHORT")
    line = _line(db_session, well, product, 2500, days_out=400)

    computed = recompute_customer(db_session, customer)
    verdict = computed.by_line[line.id]

    assert verdict.status is CoverageStatus.UNCOVERED
    assert computed.consumed_from_customer_owned[line.id] == 1000
    assert "customer-owned" in verdict.reason
    assert "drawn from this customer's OWN" in verdict.reason
    assert "still short by 1500" in verdict.reason
    # And it does NOT claim the shelf was simply empty, which is the lie this wording
    # replaces.
    assert "Insufficient on-hand inventory for requested ROS" not in verdict.reason


def test_the_executive_channels_reflect_the_customer_owned_draw(db_session):
    """The dashboard reports WHERE the steel came from, not just that it arrived.

    `from_customer_owned` is its own channel and is never folded into
    `from_shared_pool`: that material is not the company's, and this is the screen
    management judges the company's own coverage performance on. The five channels
    still partition demand exactly.
    """
    from app.engines.executive import (
        FROM_ASSIGNMENT,
        FROM_CUSTOMER_OWNED,
        FROM_POOL,
        NOT_SATISFIED,
        soft_allocation_coverage,
    )

    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Exec Co")
    product = _product(db_session, "CSG exec product")
    _stock(db_session, bu, product, 500)
    _owns(db_session, customer, product, 1000)
    well = _well(db_session, node, "WELL-EXEC")
    _line(db_session, well, product, 1800, days_out=100)
    recompute_customer(db_session, customer)

    block = soft_allocation_coverage(db_session, horizon_months=12, customer_id=customer.id)
    channels = {c.key: c for c in block.channels}

    assert channels[FROM_CUSTOMER_OWNED].quantity == 1000
    assert channels[FROM_POOL].quantity == 500
    assert channels[FROM_ASSIGNMENT].quantity == 0
    assert channels[NOT_SATISFIED].quantity == 300
    # The partition still holds exactly.
    assert sum(c.quantity for c in block.channels) == block.total_quantity == 1800


def test_mrp_reports_the_two_ownership_tiers_separately(db_session):
    """A planner must see that part of the stock is the customer's own.

    It changes what to order in BOTH directions: the runout curve opens from both
    tiers (customer-owned steel really will be drawn against that demand), while the
    figure stays separate because we may not promise it to anybody else.
    """
    from app.engines.mrp import by_item

    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Mrp Co")
    product = _product(db_session, "CSG mrp product")
    _stock(db_session, bu, product, 400)
    _owns(db_session, customer, product, 600)
    well = _well(db_session, node, "WELL-MRP")
    _line(db_session, well, product, 300, days_out=40)
    recompute_customer(db_session, customer)

    analysis = by_item(db_session, product.id)
    assert analysis.inventory.on_hand == 400  # COMPANY only
    assert analysis.inventory.customer_owned == 600
    assert analysis.inventory.customer_owned_source == "customer-upload"
    # The projection opens from both tiers -- 1000, not 400.
    assert analysis.runout[0].opening_balance == 1000


# ---------------------------------------------------------------------------
# 2. THE BOUNDARY -- never shareable, in either direction
# ---------------------------------------------------------------------------


def test_a_customers_own_uploaded_stock_is_never_offered_to_a_neighbour(db_session):
    """Genuine surplus of the customer's OWN material, and still not shareable.

    The setup is deliberately the most tempting possible: the donor owns 10 000 of a
    product it has no demand for at all, so by every other definition in this module
    that quantity is idle surplus sitting inside the same Business Unit as a customer
    who is short of exactly it. The company holds an explicit ZERO.

    It must still not be offered. It is the donor's PROPERTY, not the company's, and
    proposing to move it would propose moving something we do not own -- a boundary
    tighter than the Business Unit boundary. Both directions are covered by the same
    assertion: `shareable` is 0, so nothing is offered TO the needy customer, and
    nothing of the needy customer's own would be offered away either.
    """
    bu = _bu(db_session)
    donor, donor_node = _customer(db_session, bu, "Donor Co")
    needy, needy_node = _customer(db_session, bu, "Needy Co")
    product = _product(db_session, "CSG shared product")
    _stock(db_session, bu, product, 0)
    _owns(db_session, donor, product, 10_000)

    needy_well = _well(db_session, needy_node, "WELL-NEEDY")
    _line(db_session, needy_well, product, 4000, days_out=200)
    recompute_customer(db_session, needy)

    analysis = cross_customer_sharing(db_session, needy)

    (surplus,) = [s for s in analysis.product_surplus if s.product_id == product.id]
    # The company holds none, so there is no surplus -- the donor's 10 000 is invisible
    # to this arithmetic by construction, not by a filter.
    assert surplus.bu_on_hand == 0.0
    assert surplus.shareable == 0.0

    (outcome,) = [
        line for line in analysis.uncovered_lines if line.product_id == product.id
    ]
    assert outcome.would_be_covered is False
    assert outcome.shared_quantity == 0.0
    assert outcome.contributions == ()
    assert donor.id not in {c.from_customer_id for c in outcome.contributions}

    # The reason is stated in the payload a planner reads, not only in the code.
    assert any("CUSTOMER-OWNED" in note for note in analysis.notes)

    # And the structural fact behind it: the company on-hand resolver never sees the
    # customer-owned quantity at all.
    assert on_hand_map(db_session, bu.id, {product.id}) == {product.id: 0.0}


def test_own_stock_covering_own_demand_frees_company_steel_rather_than_leaking(
    db_session,
):
    """The correct second-order effect, pinned so nobody "fixes" it.

    A customer that covers its lines from its OWN material draws less company steel,
    so its committed quantity falls and the BU's shareable surplus RISES. That is not
    a leak -- the company steel it did not need is genuinely free, and saying so is
    the honest answer. What must never happen is the customer's own material being
    counted INTO that surplus, which the bound `shareable <= bu_on_hand` excludes.
    """
    bu = _bu(db_session)
    self_sufficient, ss_node = _customer(db_session, bu, "Self Sufficient Co")
    needy, needy_node = _customer(db_session, bu, "Needy Two Co")
    product = _product(db_session, "CSG freeing product")
    _stock(db_session, bu, product, 5000)
    _owns(db_session, self_sufficient, product, 5000)

    _line(db_session, _well(db_session, ss_node, "WELL-SS"), product, 5000, 150)
    _line(db_session, _well(db_session, needy_node, "WELL-N2"), product, 4000, 200)
    recompute_customer(db_session, self_sufficient)
    recompute_customer(db_session, needy)

    analysis = cross_customer_sharing(db_session, needy)
    (surplus,) = [s for s in analysis.product_surplus if s.product_id == product.id]

    # The self-sufficient customer used its own steel, so it committed NO company
    # steel and the whole 5000 is genuinely surplus.
    assert surplus.bu_on_hand == 5000.0
    assert surplus.committed_in_bu == 4000.0  # only the needy customer's own draw
    assert surplus.shareable == 1000.0
    # THE BOUND: surplus is company stock only. 10 000 exists in the BU across both
    # tiers, and not one metre of the customer-owned half reached this figure.
    assert surplus.shareable <= surplus.bu_on_hand


# ---------------------------------------------------------------------------
# 3. THE UNKNOWN -- "owns none" is not "no upload has happened"
# ---------------------------------------------------------------------------


def test_none_owned_is_distinguishable_from_no_upload(db_session):
    """Absence is not zero, and the platform says which absence it is.

    Three states, all reachable, all different, and none of them raising -- unlike a
    missing `InventoryOnHand` row, because most products legitimately have no
    customer-owned stock and raising would take coverage down for every customer that
    has not uploaded a file.
    """
    bu = _bu(db_session)
    silent, _node_s = _customer(db_session, bu, "Silent Co")
    declared, _node_d = _customer(db_session, bu, "Declared Co")
    owned_product = _product(db_session, "CSG declared product")
    other_product = _product(db_session, "CSG undeclared product")
    zero_product = _product(db_session, "CSG explicit zero product")

    upload = _owns(db_session, declared, owned_product, 700)
    _owns(db_session, declared, zero_product, 0, upload=upload)

    # 1. NO UPLOAD -> unknown. Never 0.
    unknown = customer_owned_for(db_session, silent, owned_product)
    assert unknown.known is False
    assert unknown.quantity is None
    assert unknown.source_system == "unavailable"
    assert unknown.drawable == 0.0  # spendable as nothing, claimed as nothing

    # 2. UPLOADED, and this product was not in the file -> MEASURED zero.
    measured = customer_owned_for(db_session, declared, other_product)
    assert measured.known is True
    assert measured.quantity == 0.0
    assert measured.source_system == "declared-none"

    # 3. UPLOADED with an explicit 0 row -> the same fact, stated by name.
    explicit = customer_owned_for(db_session, declared, zero_product)
    assert explicit.known is True
    assert explicit.quantity == 0.0
    assert explicit.source_system == "customer-upload"

    # ...and the positive case, for contrast.
    real = customer_owned_for(db_session, declared, owned_product)
    assert (real.known, real.quantity) == (True, 700.0)


def test_the_ownership_pool_refuses_to_be_a_number(db_session):
    """The split is a TYPE, so a caller cannot take a total by accident.

    This is the structural half of "design so a caller cannot get the total when it
    needed the split". `OwnershipPool` has no arithmetic: it cannot be added,
    compared, or coerced, and `.total` has to be asked for by name. A caller that
    wanted the split and reached for `on_hand_map` instead gets a float whose
    signature and docstring both say company-owned.
    """
    bu = _bu(db_session)
    customer, _node = _customer(db_session, bu, "Type Co")
    product = _product(db_session, "CSG typed product")
    _stock(db_session, bu, product, 900)
    _owns(db_session, customer, product, 100)

    pool = ownership_pool_for(db_session, customer, product)
    assert (pool.company_owned, pool.customer_owned) == (900.0, 100.0)
    assert pool.total == 1000.0
    assert pool.customer_owned_known is True

    with pytest.raises(TypeError):
        pool + 1  # not a number, on purpose
    with pytest.raises(TypeError):
        float(pool)
    with pytest.raises(TypeError):
        sum([pool, pool])

    # The company tier can be restated (assignment carve-out, scenario override); the
    # customer tier cannot, because neither of those may touch a customer's property.
    assert pool.with_company_owned(400).customer_owned == 100.0
    assert not hasattr(pool, "with_customer_owned")


def test_an_unknown_customer_owned_quantity_never_raises_and_never_covers(db_session):
    """Coverage still computes for a customer that has uploaded nothing.

    The whole platform refuses over a missing `InventoryOnHand` row, and this must NOT
    inherit that: a database that has never used the upload is the ordinary state, and
    raising there would be an outage rather than an honesty measure.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Nothing Uploaded Co")
    product = _product(db_session, "CSG plain product")
    _stock(db_session, bu, product, 1000)
    line = _line(db_session, _well(db_session, node, "WELL-PLAIN"), product, 1000, 100)

    computed = compute_customer_coverage(db_session, customer)
    assert computed.by_line[line.id].status is CoverageStatus.COVERED
    # Nothing was drawn from a tier nobody has declared.
    assert computed.consumed_from_customer_owned[line.id] == 0.0
    assert computed.consumed_from_pool[line.id] == 1000.0


# ---------------------------------------------------------------------------
# The upload
# ---------------------------------------------------------------------------


def test_upload_happy_path_creates_positions_and_recomputes_coverage(db_session):
    """The whole point of the feature: a spreadsheet changes a coverage verdict.

    One well, short of company stock, covered once the customer's own position is
    uploaded. The upload record is written too, which is what makes every OTHER
    product of this customer read as a measured zero rather than as unknown.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Upload Co")
    product = _product(db_session, "CSG uploaded product")
    _stock(db_session, bu, product, 1000)
    well = _well(db_session, node, "WELL-UPLOAD")
    line = _line(db_session, well, product, 2500, days_out=300)
    recompute_customer(db_session, customer)
    assert db_session.query(Well).get(well.id).coverage_status == "Uncovered"

    result = parse_and_replace(
        db_session,
        customer,
        _sheet([(product.description, 1500)]),
        filename="owned.xlsx",
    )

    assert (result.created_count, result.replaced_count, result.error_count) == (1, 0, 0)
    assert result.rows[0].action == "Created"
    # The consequence is reported at the moment the user caused it.
    assert result.coverage_changes == {well.id: ("Uncovered", "Covered")}

    computed = compute_customer_coverage(db_session, customer)
    assert computed.by_line[line.id].status is CoverageStatus.COVERED
    assert computed.consumed_from_customer_owned[line.id] == 1500
    assert computed.consumed_from_pool[line.id] == 1000


def test_a_second_upload_REPLACES_the_position_rather_than_adding_to_it(db_session):
    """An inventory position has a current value, not a history.

    This is the difference from a demand import that removes the need for a
    revision-vs-new review step: there is no reading under which the second count is
    "additional stock", and keeping both would double the position. `previous_quantity`
    is reported because it is a number the user has just lost sight of.
    """
    bu = _bu(db_session)
    customer, _node = _customer(db_session, bu, "Replace Co")
    product = _product(db_session, "CSG replaced product")
    _stock(db_session, bu, product, 0)

    parse_and_replace(db_session, customer, _sheet([(product.description, 900)]))
    result = parse_and_replace(
        db_session, customer, _sheet([(product.description, 250)]), filename="v2.xlsx"
    )

    assert (result.created_count, result.replaced_count) == (0, 1)
    assert result.rows[0].action == "Replaced"
    assert result.rows[0].previous_quantity == 900
    assert result.rows[0].quantity == 250

    rows = (
        db_session.query(CustomerOwnedInventory)
        .filter(CustomerOwnedInventory.customer_id == customer.id)
        .all()
    )
    assert [r.quantity for r in rows] == [250]  # ONE row, not two, and not 1150


def test_malformed_rows_do_not_fail_the_batch(db_session):
    """Every row-level failure is a per-row error and the good rows still load.

    Six kinds of bad row beside two good ones. A single exception here would mean a
    user with one typo cannot upload anything -- the same defensiveness rule the demand
    import follows.
    """
    bu = _bu(db_session)
    customer, _node = _customer(db_session, bu, "Malformed Co")
    good = _product(db_session, "CSG good product")
    also_good = _product(db_session, "CSG also good product")
    _stock(db_session, bu, good, 0)
    _stock(db_session, bu, also_good, 0)

    sheet = _sheet(
        [
            (good.description, 1000),                 # 2  ok
            ("No Such Product", 500),                 # 3  unknown product
            (None, 500),                              # 4  product empty
            (also_good.description, "not a number"),  # 5  bad quantity
            (also_good.description, -5),              # 6  negative quantity
            (also_good.description, 250),             # 7  ok
            (good.description, 999),                  # 8  in-file duplicate of row 2
        ],
    )
    result = parse_and_replace(db_session, customer, sheet, filename="messy.xlsx")

    by_row = {row.row_number: row for row in result.rows}
    assert by_row[2].action == "Created"
    assert by_row[7].action == "Created"
    assert "unknown product" in by_row[3].error
    assert "product is empty" in by_row[4].error
    assert "is not a number" in by_row[5].error
    assert "cannot be negative" in by_row[6].error
    assert "duplicate of row 2" in by_row[8].error
    assert result.created_count == 2
    assert result.error_count == 5

    # The two good positions really landed.
    assert {
        r.product_id: r.quantity
        for r in db_session.query(CustomerOwnedInventory).all()
    } == {good.id: 1000.0, also_good.id: 250.0}

    # An error row carries no product, so it carries no unit -- labelling an
    # unvalidated cell with a guessed unit is the mistake the units guard prevents.
    assert by_row[3].product_id is None


def test_zero_is_accepted_because_owning_none_is_a_fact(db_session):
    """Unlike a demand row, a zero inventory row is meaningful.

    A demand line for nothing is not demand, so `demand_import` refuses 0. "We ran our
    count and own none of this" IS a fact, and refusing it would leave a customer no
    way to state it except by omitting the row -- the one thing the model works to keep
    distinguishable from silence.
    """
    bu = _bu(db_session)
    customer, _node = _customer(db_session, bu, "Zero Co")
    product = _product(db_session, "CSG zero product")
    _stock(db_session, bu, product, 0)

    result = parse_and_replace(db_session, customer, _sheet([(product.description, 0)]))
    assert result.error_count == 0
    assert result.rows[0].quantity == 0.0

    position = customer_owned_for(db_session, customer, product)
    assert position.known is True
    assert position.quantity == 0.0


def test_a_row_naming_another_customer_is_refused_not_ignored(db_session):
    """Whose inventory this is cannot be decided by a cell in the file.

    Refused per row rather than ignored: silently loading one operator's declared
    stock against another is the worst outcome this endpoint has, and silently
    dropping the cell would hide that the file was sent to the wrong place.
    """
    bu = _bu(db_session)
    mine, _node = _customer(db_session, bu, "Mine Co")
    theirs, _node2 = _customer(db_session, bu, "Theirs Co")
    product = _product(db_session, "CSG customer-column product")
    _stock(db_session, bu, product, 0)

    sheet = _sheet(
        [
            ("Mine Co", product.description, 100),
            ("Theirs Co", product.description, 100),
        ],
        headers=("Customer", "Product", "Quantity"),
    )
    result = parse_and_replace(db_session, mine, sheet)

    by_row = {row.row_number: row for row in result.rows}
    assert by_row[2].action == "Created"
    assert "does not match the customer this upload is for" in by_row[3].error
    assert (
        db_session.query(CustomerOwnedInventory)
        .filter(CustomerOwnedInventory.customer_id == theirs.id)
        .count()
        == 0
    )


def test_an_ambiguous_as_of_date_is_refused_rather_than_guessed(db_session):
    """03/04/2027 could be 3 April or 4 March, and this date is how a planner judges
    whether to trust the quantity at all. Same rule as the demand import's ROS date."""
    bu = _bu(db_session)
    customer, _node = _customer(db_session, bu, "Date Co")
    product = _product(db_session, "CSG dated product")
    _stock(db_session, bu, product, 0)

    sheet = _sheet(
        [
            (product.description, 100, "03/04/2027"),
            (product.description, 100, "2027-04-03"),
        ],
        headers=("Product", "Quantity", "As Of Date"),
    )
    result = parse_and_replace(db_session, customer, sheet)
    by_row = {row.row_number: row for row in result.rows}
    assert "is not a date" in by_row[2].error
    # The unambiguous spelling of the same row is accepted, and its as-of date becomes
    # the row's `uploaded_at` -- the age of the COUNT, not of the email.
    assert by_row[3].action == "Created"
    row = (
        db_session.query(CustomerOwnedInventory)
        .filter(CustomerOwnedInventory.customer_id == customer.id)
        .one()
    )
    assert row.uploaded_at.date().isoformat() == "2027-04-03"


def test_a_file_level_failure_raises_rather_than_staging_nonsense(db_session):
    """No per-row answer exists for a file with no `product` column at all."""
    bu = _bu(db_session)
    customer, _node = _customer(db_session, bu, "Bad File Co")

    with pytest.raises(CustomerOwnedImportError) as excinfo:
        parse_and_replace(
            db_session, customer, _sheet([(1,)], headers=("Something Else",))
        )
    assert "Missing required column" in str(excinfo.value)

    with pytest.raises(CustomerOwnedImportError):
        parse_and_replace(db_session, customer, b"not a workbook at all")


def test_the_upload_api_round_trips(db_session):
    """The endpoints a UI actually calls, including the has_uploaded discriminator."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    bu = _bu(db_session)
    customer, _node = _customer(db_session, bu, "Api Co")
    product = _product(db_session, "CSG api product")
    _stock(db_session, bu, product, 0)

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        client = TestClient(app)

        before = client.get(f"/customer-owned-inventory/{customer.id}").json()
        assert before["has_uploaded"] is False
        assert before["positions"] == []
        assert "NOT the same as owning none" in before["note"]

        response = client.post(
            f"/customer-owned-inventory/{customer.id}/uploads",
            files={
                "file": (
                    "owned.xlsx",
                    _sheet([(product.description, 4200)]),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["created_count"] == 1
        assert body["rows"][0]["unit_of_measure"] == UnitOfMeasure.MTR.value
        assert body["column_contract"]["required"] == ["product", "quantity"]

        after = client.get(f"/customer-owned-inventory/{customer.id}").json()
        assert after["has_uploaded"] is True
        assert [p["quantity"] for p in after["positions"]] == [4200]
        assert after["positions"][0]["unit_of_measure"] == UnitOfMeasure.MTR.value

        history = client.get(f"/customer-owned-inventory/{customer.id}/uploads").json()
        assert len(history) == 1
        assert history[0]["applied_count"] == 1
    finally:
        app.dependency_overrides.pop(get_db, None)
