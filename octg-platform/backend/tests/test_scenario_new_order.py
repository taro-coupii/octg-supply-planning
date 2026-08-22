"""`PO_ARRIVAL.new_order` -- simulating a purchase order that DOES NOT EXIST.

The gap this closes
-------------------
`PO_ARRIVAL.arrival_date` lets a planner MOVE an existing on-order arrival. It
cannot express "what if we placed an emergency order for 5000 metres arriving next
March?", because every other override in the vocabulary RESTATES an existing value
and this one ASSERTS AN EVENT. `new_order` is that assertion: one row carrying both
a quantity and an arrival date, scoped to (Business Unit, product) exactly as a real
`InventoryOnOrder` row is.

What these tests are FOR, in order of how load-bearing they are
--------------------------------------------------------------
  1. It cannot be confused with editing a real PO -- by SHAPE, not by spelling.
     `test_a_new_order_row_and_an_arrival_date_row_are_not_interchangeable` and
     `test_both_fields_coexist_on_one_product_without_corrupting_each_other`.
  2. IT MOVES NO COVERAGE VERDICT, however large it is. Pinned with a
     leak-would-flip-the-verdict setup, the same way the arrival-date work pinned
     it: `test_a_hypothetical_order_moves_no_coverage_verdict`.
  3. It never becomes an `InventoryOnOrder` row and the preview writes nothing:
     `test_a_hypothetical_order_creates_no_inventory_on_order_row` and
     `test_preview_of_a_hypothetical_order_performs_no_writes`.
  4. Apply refuses it, with its OWN reason -- placing an order, not writing to a
     projection: `test_apply_refuses_a_hypothetical_order_for_its_own_reason`.

The fixtures are imported from `test_scenario_engine` rather than copied. That is
deliberate: these tests must reason against the SAME customer/BU/lead-time setup the
arrival-date tests do, or a difference between the two suites could come from the
scaffolding rather than from the override.
"""

from datetime import datetime, timedelta

import pytest

from app.engines.coverage import recompute_customer
from app.engines.mrp import on_order_runout
from app.engines.overrides import (
    SUPPLY_KINDS,
    OverrideError,
    ScenarioOverrides,
    validate,
)
from app.engines.scenario import apply_blockers, preview
from app.engines.scenario_apply import ScenarioNotApplicable, apply_to_base_plan
from app.models import (
    CoverageStatus,
    InventoryOnOrder,
    ScenarioOverride,
    ScenarioStatus,
    ScenarioTargetKind,
)
from tests.test_scenario_engine import (
    _customer,
    _default_bu,
    _lead_times,  # noqa: F401  -- autouse-style fixture pulled in for lead times
    _line,
    _on_order,
    _override,
    _po_arrival_override,
    _product,
    _scenario,
    _status_of,
    _well,
)

#: Far enough out that the demand line is RECOVERABLE, matching `_po_scenario`.
FAR_ROS_DAYS = 400


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _new_order_override(
    db, scenario, product, quantity, arrives_in_days, *, bu=None, note=None
):
    """One `PO_ARRIVAL.new_order` override: a hypothetical order.

    BOTH value columns are populated, and a Business Unit is named. That combination
    is what `validate` requires and what an `arrival_date` override may never have --
    see `app.engines.overrides.OVERRIDE_FIELDS`.
    """
    return _override(
        db,
        scenario,
        target_kind=ScenarioTargetKind.PO_ARRIVAL,
        field_name="new_order",
        target_product_id=product.id,
        target_business_unit_id=(bu if bu is not None else _default_bu(db)).id,
        value_number=quantity,
        value_date=datetime.utcnow() + timedelta(days=arrives_in_days),
        note=note,
    )


def _gap_scenario(db, *, on_hand=0.0, demand=4000, real_po=None, real_po_days=200):
    """A customer with a demand the plan cannot meet -- a gap to close.

    `on_hand=0` means the runout curve has nothing buffering it, so the month the
    balance goes negative responds sharply to incoming supply. That is what makes a
    hypothetical order's effect observable rather than absorbed by stock.

    `real_po` adds a REAL dated purchase order as well, so the additivity of a
    hypothetical order beside real ones can be tested rather than assumed.
    """
    product = _product(db, on_hand_qty=on_hand)
    customer, node = _customer(db)
    well = _well(db, node, "W-NewOrder")
    line = _line(db, well, product, quantity=demand)
    if real_po is not None:
        _on_order(db, product, real_po, arrives_in_days=real_po_days)
    recompute_customer(db, customer)
    scenario = _scenario(db, customer, "Emergency order")
    return product, customer, line, scenario


def _change_for(impact, product_id):
    matches = [c for c in impact.supply_runout_changes if c.product_id == product_id]
    assert len(matches) == 1, (
        f"expected exactly ONE runout comparison for {product_id}, got "
        f"{len(matches)} -- a product has one runout curve and reporting it twice "
        "would invite a planner to add two projections of it together"
    )
    return matches[0]


# --------------------------------------------------------------------------
# 1. The vocabulary, and why the two fields cannot be confused
# --------------------------------------------------------------------------


def test_a_new_order_row_and_an_arrival_date_row_are_not_interchangeable(db_session):
    """THE DESIGN CONSTRAINT, pinned at the validator.

    The whole reason `new_order` is its own field rather than a `quantity` field
    that means "new order when no real row matches" is that the two must be
    distinguishable by SHAPE, not by the state of `InventoryOnOrder` at read time.
    So each of the four cross-shaped rows below must be REFUSED:

      * a `new_order` missing its quantity, or missing its date -- half an event;
      * a `new_order` with no Business Unit -- an `arrival_date` row's shape;
      * an `arrival_date` that also carries a quantity -- a `new_order` row's shape.

    A reader that lost the field name could still tell the two apart, and a reader
    that guessed wrong gets an error instead of a wrong answer.
    """
    product = _product(db_session, on_hand_qty=0)
    customer, node = _customer(db_session)
    bu = _default_bu(db_session)
    scenario = _scenario(db_session, customer)

    def row(**kwargs):
        return ScenarioOverride(
            scenario_id=scenario.id,
            target_kind=ScenarioTargetKind.PO_ARRIVAL,
            target_product_id=product.id,
            **kwargs,
        )

    when = datetime.utcnow() + timedelta(days=90)

    # A new order with no quantity: nothing to project.
    with pytest.raises(OverrideError, match="value_number was missing"):
        validate(
            row(field_name="new_order", value_date=when, target_business_unit_id=bu.id),
            customer,
        )
    # A new order with no date: no month to project it at.
    with pytest.raises(OverrideError, match="value_date was missing"):
        validate(
            row(
                field_name="new_order",
                value_number=5000,
                target_business_unit_id=bu.id,
            ),
            customer,
        )
    # A new order with no Business Unit -- i.e. wearing an arrival_date row's shape.
    with pytest.raises(OverrideError, match="must state the Business Unit"):
        validate(
            row(field_name="new_order", value_number=5000, value_date=when), customer
        )
    # And the reverse: an arrival_date row may NOT carry a quantity, so it can never
    # be mistaken for a resize of a real PO.
    with pytest.raises(OverrideError, match="value_number must be empty"):
        validate(
            row(field_name="arrival_date", value_date=when, value_number=5000),
            customer,
        )

    # The well-formed row is accepted, so the refusals above are about shape rather
    # than the field being unusable.
    validate(
        row(
            field_name="new_order",
            value_number=5000,
            value_date=when,
            target_business_unit_id=bu.id,
        ),
        customer,
    )


def test_a_hypothetical_order_of_nothing_is_refused(db_session):
    """A zero or negative hypothetical order is not a what-if.

    This override only ever ADDS to the projection -- there is no real row for a
    negative quantity to subtract from -- so a non-positive quantity would be a row
    that sits in the scenario looking accounted-for while changing nothing.
    """
    product = _product(db_session, on_hand_qty=0)
    customer, node = _customer(db_session)
    bu = _default_bu(db_session)
    scenario = _scenario(db_session, customer)

    for bad in (0, -500):
        with pytest.raises(OverrideError, match="POSITIVE quantity"):
            validate(
                ScenarioOverride(
                    scenario_id=scenario.id,
                    target_kind=ScenarioTargetKind.PO_ARRIVAL,
                    field_name="new_order",
                    target_product_id=product.id,
                    target_business_unit_id=bu.id,
                    value_number=bad,
                    value_date=datetime.utcnow() + timedelta(days=90),
                ),
                customer,
            )


def test_a_hypothetical_order_cannot_be_placed_by_another_business_unit(db_session):
    """The BU boundary is not crossed by a hypothesis either.

    A hypothetical order records WHO WOULD PLACE IT, and a scenario may not record
    an intention outside its own customer's Business Unit -- the same rule an
    INVENTORY override gets, and for the same reason.
    """
    from tests.test_scenario_engine import _bu

    product = _product(db_session, on_hand_qty=0)
    customer, node = _customer(db_session)
    other = _bu(db_session, name="Gulf-Hypothetical")
    scenario = _scenario(db_session, customer)

    with pytest.raises(OverrideError, match="hypothetical new-order override"):
        validate(
            ScenarioOverride(
                scenario_id=scenario.id,
                target_kind=ScenarioTargetKind.PO_ARRIVAL,
                field_name="new_order",
                target_product_id=product.id,
                target_business_unit_id=other.id,
                value_number=5000,
                value_date=datetime.utcnow() + timedelta(days=90),
            ),
            customer,
        )


def test_the_resolver_keeps_the_two_fields_in_separate_members(db_session):
    """Structural separation, asserted at the resolver rather than inferred.

    `arrival_date` resolves through `_po_arrival` into `on_order_runout`'s
    `arrival_override`; `new_order` resolves through `_new_orders` into its
    `hypothetical_orders`. Neither can reach the other's code path, which is what
    makes "cannot be confused" a property of the code rather than of the naming.
    """
    product, customer, line, scenario = _gap_scenario(db_session, real_po=1000)
    _po_arrival_override(db_session, scenario, product, arrives_in_days=50)
    _new_order_override(db_session, scenario, product, 5000, arrives_in_days=90)

    resolver = ScenarioOverrides(scenario.overrides, customer)

    # The arrival-date side sees ONLY the date, and does not see the quantity.
    assert resolver.po_arrival_product_ids() == frozenset({product.id})
    assert resolver.arrival_date(product.id, None) is not None
    # The new-order side sees ONLY the (quantity, date) pair.
    assert resolver.new_order_product_ids() == frozenset({product.id})
    orders = resolver.hypothetical_orders(product.id)
    assert len(orders) == 1
    assert orders[0][0] == 5000
    # A product with no new-order override reports none, not the arrival date.
    other = _product(db_session, on_hand_qty=0, grade="L80")
    assert resolver.hypothetical_orders(other.id) == ()


def test_several_hypothetical_orders_on_one_product_are_all_kept(db_session):
    """"2000 in March and 3000 in June" is one coherent question, not two rivals.

    `arrival_date` is a dict keyed by product because it restates ONE fact and a
    second row would be ambiguous. A hypothetical order is an EVENT, so the resolver
    keeps a LIST -- collapsing them would silently discard the planner's second
    order, and the total supply modelled would be wrong.
    """
    product, customer, line, scenario = _gap_scenario(db_session)
    _new_order_override(db_session, scenario, product, 2000, arrives_in_days=90)
    _new_order_override(db_session, scenario, product, 3000, arrives_in_days=180)

    resolver = ScenarioOverrides(scenario.overrides, customer)
    orders = resolver.hypothetical_orders(product.id)

    assert [q for q, _d in orders] == [2000, 3000], (
        "both hypothetical orders must survive, earliest first -- one of them being "
        "dropped would understate the supply the planner asked about"
    )
    change = _change_for(preview(db_session, scenario), product.id)
    assert change.hypothetical_quantity == 5000


# --------------------------------------------------------------------------
# 2. It lands in the runout projection, at the right month, additively
# --------------------------------------------------------------------------


def test_a_hypothetical_order_lands_in_the_runout_at_its_own_month(db_session):
    """THE LOAD-BEARING NEW BEHAVIOUR, with a real PO present too.

    The product has a REAL 1000-unit PO landing at day 200 and a demand of 4000, so
    the plan runs out at the ROS month. A hypothetical 6000-unit order landing at day
    90 closes that gap: the balance no longer goes negative.

    Both halves are checked, because either alone would pass a broken
    implementation:
      * the hypothetical quantity appears in `incoming_by_month` at ITS OWN month,
        not the real PO's month;
      * the real PO's quantity is STILL THERE -- the hypothetical order is added
        BESIDE it, not instead of it.
    """
    product, customer, line, scenario = _gap_scenario(
        db_session, demand=4000, real_po=1000, real_po_days=200
    )
    before = _change_for(
        preview(
            db_session,
            _seed_arrival_only(db_session, scenario, product),
        ),
        product.id,
    )
    # The baseline really is short: 1000 on order against 4000 of demand.
    assert before.runout_month_before == line.ros_date.strftime("%Y-%m")

    scenario2 = _scenario(db_session, customer, "Emergency 6000")
    order = _new_order_override(db_session, scenario2, product, 6000, arrives_in_days=90)
    impact = preview(db_session, scenario2)
    change = _change_for(impact, product.id)

    # The gap closes.
    assert change.runout_month_before == line.ros_date.strftime("%Y-%m")
    assert change.runout_month_after is None
    assert change.runout_month_changed is True

    # ADDITIVITY, checked at the engine so the month keys are visible: the real PO's
    # month and the hypothetical order's month are BOTH populated, and the real
    # quantity is untouched.
    arrival = order.value_date.date()
    real_month = (datetime.utcnow() + timedelta(days=200)).strftime("%Y-%m")
    hypo_month = arrival.strftime("%Y-%m")
    runout = on_order_runout(
        db_session, product.id, hypothetical_orders=[(6000.0, arrival)]
    )
    assert runout.hypothetical_by_month == {hypo_month: 6000.0}
    assert runout.incoming_by_month[hypo_month] == pytest.approx(
        6000.0 + (1000.0 if hypo_month == real_month else 0.0)
    )
    assert runout.incoming_by_month[real_month] == pytest.approx(
        1000.0 + (6000.0 if hypo_month == real_month else 0.0)
    ), "the real purchase order must still be in the projection, not replaced"
    assert sum(runout.incoming_by_month.values()) == pytest.approx(7000.0)

    # And the reported figures keep real and invented apart.
    assert change.hypothetical_quantity == 6000
    assert change.on_order_dated_quantity == 1000, (
        "the real on-order total must stay a sum of real rows a planner can verify "
        "in Oracle -- merging an invented quantity into it would make the two "
        "indistinguishable in the field most likely to be quoted as fact"
    )
    assert [q for q, _d in change.hypothetical_orders] == [6000]


def _seed_arrival_only(db, scenario, product):
    """A sibling scenario carrying only an arrival_date override.

    Used to obtain the BASELINE runout comparison for the same product without a
    hypothetical order in it, so the two can be compared like for like.
    """
    _po_arrival_override(db, scenario, product, arrives_in_days=200)
    return scenario


def test_the_real_on_order_total_never_absorbs_the_invented_quantity(db_session):
    """`on_order_total` is a fact; `hypothetical_total` is a hypothesis.

    Pinned at the engine as well as at the preview, because this is the one field a
    planner is most likely to read as "how much is coming" and act on.
    """
    product, customer, line, scenario = _gap_scenario(db_session, real_po=1000)

    runout = on_order_runout(
        db_session,
        product.id,
        hypothetical_orders=[(6000.0, (datetime.utcnow() + timedelta(days=90)).date())],
    )

    assert runout.on_order_total == 1000, "real rows only"
    assert runout.hypothetical_total == 6000
    # And a caller that passed nothing gets zero, not a missing attribute.
    plain = on_order_runout(db_session, product.id)
    assert plain.hypothetical_total == 0.0
    assert plain.hypothetical_by_month == {}


def test_a_hypothetical_order_does_not_move_the_promised_earliest_arrival(db_session):
    """An invented order is not an "expected arrival" -- nobody expects it.

    `earliest_arrival` is the field the Executive Dashboard and
    `InventoryPosition.on_order_earliest_arrival` call Oracle's promise, and it is
    what `_supply_runout_changes` diffs to report `shift_days`. A hypothetical order
    landing before every real PO must therefore leave both alone, or it would read as
    though a delivery had been pulled in.
    """
    product, customer, line, scenario = _gap_scenario(
        db_session, real_po=1000, real_po_days=300
    )
    _new_order_override(db_session, scenario, product, 6000, arrives_in_days=10)

    change = _change_for(preview(db_session, scenario), product.id)

    assert change.arrival_before == change.arrival_after, (
        "a hypothetical order moved the promised arrival date -- it has no promised "
        "date of its own and must not overwrite Oracle's"
    )
    assert change.shift_days == 0


def test_both_fields_coexist_on_one_product_without_corrupting_each_other(db_session):
    """The two overrides are NOT mutually exclusive, and both apply.

    "The mill pulled our deliveries in AND we placed an emergency order" is one
    question. This pins that asserting a hypothetical order does not corrupt the
    arrival-date override on the same product, and vice versa: the real PO still
    shifts by the stated offset (`shift_days` is unchanged by the presence of the new
    order) and the hypothetical quantity is still folded in.
    """
    product, customer, line, scenario = _gap_scenario(
        db_session, real_po=1000, real_po_days=300
    )
    # Arrival-date only, to establish what the shift alone reports.
    _po_arrival_override(db_session, scenario, product, arrives_in_days=100)
    shift_alone = _change_for(preview(db_session, scenario), product.id)
    assert shift_alone.shift_days == -200
    assert shift_alone.hypothetical_quantity == 0.0

    # Now add the hypothetical order to the SAME scenario and the SAME product.
    _new_order_override(db_session, scenario, product, 6000, arrives_in_days=60)
    both = _change_for(preview(db_session, scenario), product.id)

    assert both.shift_days == -200, (
        "the arrival-date override was corrupted by the new-order override on the "
        "same product -- they resolve through different resolver members and must "
        "not interfere"
    )
    assert both.arrival_after == shift_alone.arrival_after
    assert both.hypothetical_quantity == 6000
    assert both.on_order_dated_quantity == 1000
    # One product, one runout comparison, even with two overrides on it.
    assert len(preview(db_session, scenario).supply_runout_changes) == 1


# --------------------------------------------------------------------------
# 3. THE CONSTRAINT: coverage does not move, and nothing is written
# --------------------------------------------------------------------------


def test_a_hypothetical_order_moves_no_coverage_verdict(db_session):
    """THE POLICY. Coverage is decided from on-hand stock alone.

    Set up so a leak WOULD FLIP THE VERDICT and could not be missed: on-hand is 0,
    the line demands 4000 and is UNCOVERED, and the hypothetical order is 50000 --
    more than ten times the demand, landing well before ROS. If incoming supply
    leaked into the coverage pass this line would read COVERED, which is exactly the
    wrong answer to ship.

    `app.engines.executive.ON_ORDER_NOTE` states the rule: no coverage figure "nets
    material already on order against demand". A hypothetical order is incoming
    supply, so it is bound by the same rule -- and being the planner's own invention
    makes it MORE important, not less, that it cannot manufacture a Covered verdict.
    """
    product, customer, line, scenario = _gap_scenario(db_session, demand=4000)
    _new_order_override(db_session, scenario, product, 50000, arrives_in_days=30)

    impact = preview(db_session, scenario)

    # It genuinely was modelled -- otherwise this test would pass vacuously.
    assert impact.supply_runout_changes
    assert _change_for(impact, product.id).hypothetical_quantity == 50000

    assert impact.changed_line_count == 0
    assert impact.changed_well_count == 0
    assert impact.covered_lines_before == impact.covered_lines_after
    assert impact.covered_wells_before == impact.covered_wells_after
    for change in impact.line_changes:
        assert change.status_before == change.status_after, (
            "a HYPOTHETICAL purchase order moved a coverage verdict -- coverage is "
            "decided from on-hand stock alone, and this order does not exist"
        )
        assert change.changed is False
    # And the verdict really was a shortfall, so there was something to flip.
    assert _status_of(impact, line.id) == (
        CoverageStatus.UNCOVERED.value,
        CoverageStatus.UNCOVERED.value,
    )
    # Risk derives from coverage, so it must be still too.
    assert (
        impact.risk.unrecoverable_lines_before
        == impact.risk.unrecoverable_lines_after
    )


def test_a_hypothetical_order_does_not_move_mrp_recommendation_rows(db_session):
    """MRP rows derive from coverage verdicts, so they cannot move either.

    The counterpart to the arrival-date test of the same name, and the honest limit
    on the "closes the gap" claim: the runout curve absorbs the order, and the MRP
    recommendation row does NOT disappear. Reporting it as removed would require
    on-order to reach coverage, which is the policy this platform has not changed.
    """
    product, customer, line, scenario = _gap_scenario(db_session, demand=4000)
    _new_order_override(db_session, scenario, product, 50000, arrives_in_days=30)

    impact = preview(db_session, scenario)

    assert impact.mrp_changes, "the shortfall must produce an MRP row to compare"
    for row in impact.mrp_changes:
        assert row.kind == "unchanged", (
            f"MRP row for {row.product_id} became {row.kind!r}: a hypothetical order "
            "must not move a recommendation, which is derived from coverage"
        )


def test_mrp_impact_reports_whether_the_order_is_big_enough_to_close_the_gap(db_session):
    """What CAN be said honestly about the gap, and is.

    MRP has already computed how much of this product it recommends ordering. The
    preview compares the planner's hypothetical quantity against that figure, so the
    actual decision ("is 5000 metres enough?") is answered -- as a SIZING CHECK, not
    as a claim that the recommendation row went away.

    Both directions are pinned, because a flag that was always true would pass a
    one-sided test.
    """
    product, customer, line, scenario = _gap_scenario(db_session, demand=4000)
    _new_order_override(db_session, scenario, product, 50000, arrives_in_days=30)

    change = _change_for(preview(db_session, scenario), product.id)

    assert change.mrp_recommended_quantity is not None, (
        "the setup must actually produce an MRP recommendation to size against"
    )
    assert change.mrp_recommended_quantity > 0
    assert change.hypothetical_covers_recommendation is True

    # An order too small to cover the recommendation reports False, and the runout
    # curve still shows partial relief rather than nothing.
    small = _scenario(db_session, customer, "Token order")
    _new_order_override(db_session, small, product, 10, arrives_in_days=30)
    small_change = _change_for(preview(db_session, small), product.id)
    assert small_change.hypothetical_covers_recommendation is False
    assert small_change.hypothetical_quantity == 10


def test_the_mrp_impact_comparison_reuses_the_preview_mrp_rows(db_session):
    """The sizing figure is READ from `mrp_changes`, not recomputed.

    Pinned by construction: the recommended quantity reported on the runout change
    must equal the sum of the after-pass quantities in `impact.mrp_changes` for that
    product. If a second MRP computation were introduced, the two could differ.
    """
    product, customer, line, scenario = _gap_scenario(db_session, demand=4000)
    _new_order_override(db_session, scenario, product, 5000, arrives_in_days=30)

    impact = preview(db_session, scenario)
    change = _change_for(impact, product.id)

    from_mrp_rows = sum(
        row.quantity_after
        for row in impact.mrp_changes
        if row.product_id == product.id and row.quantity_after is not None
    )
    assert change.mrp_recommended_quantity == pytest.approx(from_mrp_rows)


def test_a_hypothetical_order_creates_no_inventory_on_order_row(db_session):
    """It is a projection input, not a row. There is no writer for one.

    The specific error this pins against is a synthetic on-order row sitting beside
    Oracle-projected ones, indistinguishable, claiming a promise nobody made.
    """
    product, customer, line, scenario = _gap_scenario(db_session, real_po=1000)
    before = db_session.query(InventoryOnOrder).count()

    _new_order_override(db_session, scenario, product, 6000, arrives_in_days=90)
    impact = preview(db_session, scenario)
    assert _change_for(impact, product.id).hypothetical_quantity == 6000

    after_rows = db_session.query(InventoryOnOrder).all()
    assert len(after_rows) == before, (
        "previewing a hypothetical order created an InventoryOnOrder row -- this "
        "platform holds Oracle's purchase orders as a read-only projection and must "
        "never fabricate one"
    )
    # And nothing calls itself a scenario-authored purchase order either.
    assert all(r.quantity != 6000 for r in after_rows)


def test_preview_of_a_hypothetical_order_performs_no_writes(db_session):
    """The four-layer no-write guarantee holds on this new path too.

    Layer 4 (`_assert_no_writes`) would raise inside `preview` if the session grew
    anything pending, so a passing preview is itself part of the assertion. The
    session's pending sets are compared explicitly as well, and the scenario's own
    row must be untouched.
    """
    product, customer, line, scenario = _gap_scenario(db_session, real_po=1000)
    _new_order_override(db_session, scenario, product, 6000, arrives_in_days=90)
    db_session.flush()

    before = (
        len(db_session.new),
        len(db_session.dirty),
        len(db_session.deleted),
    )
    on_order_before = db_session.query(InventoryOnOrder).count()
    override_before = db_session.query(ScenarioOverride).count()

    impact = preview(db_session, scenario)  # layer 4 raises if this path writes

    assert impact.supply_runout_changes
    assert (
        len(db_session.new),
        len(db_session.dirty),
        len(db_session.deleted),
    ) == before
    assert db_session.query(InventoryOnOrder).count() == on_order_before
    assert db_session.query(ScenarioOverride).count() == override_before
    assert scenario.status == ScenarioStatus.DRAFT
    assert scenario.applied_at is None


# --------------------------------------------------------------------------
# 4. Apply refuses it, for its own reason
# --------------------------------------------------------------------------


def test_apply_refuses_a_hypothetical_order_for_its_own_reason(db_session):
    """Two blockers, and the second is a STRONGER point than the first.

    The shared supply refusal is "we hold a read-only COPY of a row Oracle owns".
    Here there is no row in either system, so applying could not mean updating a
    projection -- it could only mean PLACING AN ORDER, which per the spec is a human
    last-resort decision this platform recommends but does not execute.

    The wording is pinned because a planner reading only the generic reason would be
    told something true but not the actual reason, and might conclude the block is a
    sync-timing inconvenience rather than a deliberate boundary.
    """
    product, customer, line, scenario = _gap_scenario(db_session)
    _new_order_override(db_session, scenario, product, 6000, arrives_in_days=90)

    blockers = apply_blockers(scenario)
    joined = " ".join(blockers)

    assert ScenarioTargetKind.PO_ARRIVAL in SUPPLY_KINDS
    # The generic supply refusal still fires -- the kind is still Oracle-owned.
    assert any("owned by Oracle" in b for b in blockers)
    # And the distinct one, naming the real reason.
    assert "PLACING AN ORDER" in joined
    assert "no purchase order anywhere to update" in joined
    assert "Mill ordering is a human, last-resort decision" in joined

    impact = preview(db_session, scenario)
    assert impact.applicable is False
    assert impact.apply_blockers == blockers

    # And the refusal is enforced, not merely advertised.
    with pytest.raises(ScenarioNotApplicable):
        apply_to_base_plan(db_session, scenario)
    assert scenario.applied_at is None
    assert scenario.status == ScenarioStatus.DRAFT


def test_the_note_explains_the_hypothesis_and_its_limits(db_session):
    """The preview must say what a hypothetical order did AND what it did not.

    A planner who sees a runout month vanish will otherwise assume coverage
    responded, or that an order has somehow been recorded. Both would be wrong, and
    the note is the only place the preview can say so in words.
    """
    product, customer, line, scenario = _gap_scenario(db_session)
    _new_order_override(db_session, scenario, product, 6000, arrives_in_days=90)

    notes = preview(db_session, scenario).notes

    assert any("DOES NOT EXIST" in n for n in notes)
    assert any("No InventoryOnOrder ROW WAS CREATED".upper() in n.upper() for n in notes)
    assert any("MOVES NO COVERAGE VERDICT" in n for n in notes)
    assert any("CAN NEVER BE APPLIED" in n for n in notes)
    assert any("mill ordering is a human last-resort decision" in n for n in notes)
    # It must not CLAIM an MRP row moved. The note mentions the idea only to deny
    # it, so the check is that the denial is present -- banning the phrase outright
    # would forbid the very sentence that prevents the misreading.
    assert any(
        "not a claim that a recommendation row disappeared" in n for n in notes
    )
    assert any("neither do the MRP recommendation rows" in n for n in notes)


def test_a_scenario_with_no_new_order_reports_none_of_this(db_session):
    """The common case pays nothing and claims nothing.

    A scenario with only an arrival-date override must not sprout a hypothetical
    order note, a non-zero hypothetical quantity, or the new apply blocker -- that
    would tell a planner their scenario invents supply when it does not.
    """
    product, customer, line, scenario = _gap_scenario(db_session, real_po=1000)
    _po_arrival_override(db_session, scenario, product, arrives_in_days=90)

    impact = preview(db_session, scenario)
    change = _change_for(impact, product.id)

    assert change.hypothetical_quantity == 0.0
    assert change.hypothetical_orders == ()
    assert change.hypothetical_covers_recommendation is False
    assert not any("DOES NOT EXIST" in n for n in impact.notes)
    assert not any("PLACING AN ORDER" in b for b in impact.apply_blockers)
