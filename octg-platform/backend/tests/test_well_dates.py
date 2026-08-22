"""Per-well dates: earliest ROS, first runout, and the ordering built on them.

What the product owner asked for
--------------------------------
    "uncovered wells needs dates. earliest date for the well, and the date that
    well has first runout situation. and well should be shown in order of the
    earliest date for the well."

`app.engines.well_dates` documents the definition of "first runout date" in full,
including the projection-based reading it deliberately REJECTS. The two tests that
pin that choice -- `test_first_runout_is_not_the_products_runout_month` and
`test_a_policy_driven_shortage_still_reports_a_runout_date` -- are the most
important in this module: they are the counterexamples the decision rests on, so
if somebody later "reuses `mrp._runout_series`" here, they fail rather than
silently changing what the field means.
"""

from datetime import datetime, timedelta

from sqlalchemy import event

from app.engines.coverage import recompute_customer, recompute_well
from app.engines.mrp import by_item
from app.engines.well_dates import (
    SATISFIED_STATUSES,
    WellDates,
    sort_by_earliest_ros,
    well_dates,
)
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    CoverageStatus,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryAssignment,
    InventoryOnHand,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
)

# Comfortably outside any seeded lead time, so a shortfall resolves UNCOVERED
# rather than UNRECOVERABLE -- this module is about dates, not recoverability.
FAR = 400


def _bu(db):
    bu = db.query(BusinessUnit).filter(BusinessUnit.name == "Dates BU").one_or_none()
    if bu is None:
        bu = BusinessUnit(name="Dates BU")
        db.add(bu)
        db.flush()
    return bu


def _customer(db, policy=AllocationPolicy.SOFT, name="Dates Co"):
    customer = Customer(
        name=name, allocation_policy=policy, business_unit_id=_bu(db).id
    )
    db.add(customer)
    db.flush()
    node = PlanningNode(customer_id=customer.id, node_type="Project", name=f"{name} P")
    db.add(node)
    db.flush()
    return customer, node


def _product(db, on_hand, size="9-5/8", weight=53.5):
    product = Product(
        type="CSG", size=size, weight=weight, grade="P110", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description=f"CSG {size} {weight:g} P110 VAM 21 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.flush()
    db.add(
        InventoryOnHand(
            business_unit_id=_bu(db).id, product_id=product.id, quantity=on_hand,
            source_system="synthetic",
        )
    )
    db.flush()
    return product


def _well(db, node, name, demand_status=DemandStatus.CONFIRMED):
    """A well, WITH its demand status stated.

    Demand status is a property of the WELL now
    (`app.models.well.Well.demand_status`), so it is a fixture-construction argument
    here rather than something set per line. A test that wants an out-of-scope LINE
    has to give it an out-of-scope WELL -- which is the point: one well cannot hold
    two statuses.
    """
    well = Well(planning_node_id=node.id, name=name, demand_status=demand_status)
    db.add(well)
    db.flush()
    return well


def _line(
    db,
    well,
    product,
    quantity,
    days_out=FAR,
    profile=DemandProfile.PRIMARY,
):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity,
        ros_date=datetime.utcnow() + timedelta(days=days_out),
        profile=profile,
    )
    db.add(line)
    db.flush()
    return line


# ---------------------------------------------------------------------------
# Earliest ROS
# ---------------------------------------------------------------------------


def test_earliest_ros_is_the_minimum_over_in_scope_lines(db_session):
    """The earliest date the well needs steel at all."""
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=100_000)
    well = _well(db_session, node, "W-Ladder")
    _line(db_session, well, product, 1000, days_out=FAR + 60)
    early = _line(db_session, well, product, 1000, days_out=FAR)
    _line(db_session, well, product, 1000, days_out=FAR + 30)
    recompute_well(db_session, well)

    dates = well_dates(db_session, [well.id])[well.id]
    assert dates.earliest_ros_date == early.ros_date
    assert dates.in_scope_line_count == 3


def test_earliest_ros_ignores_out_of_scope_wells(db_session):
    """Demand on an out-of-scope WELL is not demand the engine is planning, so it
    sets no date -- neither its own well's nor anybody else's.

    REPURPOSED, not weakened. The out-of-scope lines used to sit on the same well as
    the in-scope one; demand status is a property of the WELL now, so they sit on
    wells of their own at those statuses. Both halves of the original claim are still
    asserted -- the in-scope well reports its own earliest ROS and a count of 1 -- and
    a third is added: the excluded wells report NO dates at all, which is the
    assertion that fails if the filter is dropped from the query (they would report
    FAR-100 and FAR-200).
    """
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=100_000)
    well = _well(db_session, node, "W-Filtered")
    budgeted = _well(
        db_session, node, "W-Budgeted", demand_status=DemandStatus.BUDGETED
    )
    planned = _well(db_session, node, "W-Planned", demand_status=DemandStatus.PLANNED)
    confirmed = _line(db_session, well, product, 1000, days_out=FAR)
    # Both EARLIER than the in-scope line, so an implementation that ignored the
    # filter would visibly return one of their dates.
    _line(db_session, budgeted, product, 1000, days_out=FAR - 100)
    _line(db_session, planned, product, 1000, days_out=FAR - 200)
    recompute_customer(db_session, customer)

    dates = well_dates(db_session, [well.id, budgeted.id, planned.id])
    assert dates[well.id].earliest_ros_date == confirmed.ros_date
    assert dates[well.id].in_scope_line_count == 1
    for excluded in (budgeted, planned):
        assert dates[excluded.id].earliest_ros_date is None
        assert dates[excluded.id].first_runout_date is None
        assert dates[excluded.id].in_scope_line_count == 0


def test_a_contingency_line_DOES_set_the_earliest_ros(db_session):
    """Contingency is in the default profile scope, so it counts.

    Pinned here as well as in the coverage tests, because these dates are computed
    from their own query and could easily have been given their own (stale) filter.
    """
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=100_000)
    well = _well(db_session, node, "W-Contingency")
    _line(db_session, well, product, 1000, days_out=FAR)
    contingency = _line(
        db_session, well, product, 1000, days_out=FAR - 50,
        profile=DemandProfile.CONTINGENCY,
    )
    recompute_well(db_session, well)

    assert well_dates(db_session, [well.id])[well.id].earliest_ros_date == (
        contingency.ros_date
    )


def test_a_well_with_no_in_scope_demand_has_no_dates_at_all(db_session):
    """Both dates null, and the well is still PRESENT in the result.

    Every requested well gets an entry, so no caller has to invent the empty case
    (and no two callers can invent it differently).
    """
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=100_000)
    # PLANNED, so its demand is out of scope -- the well-level way to say what this
    # test has always said.
    well = _well(db_session, node, "W-Empty", demand_status=DemandStatus.PLANNED)
    _line(db_session, well, product, 1000)
    recompute_customer(db_session, customer)

    dates = well_dates(db_session, [well.id])
    assert well.id in dates
    assert dates[well.id].earliest_ros_date is None
    assert dates[well.id].first_runout_date is None
    assert dates[well.id].in_scope_line_count == 0
    # Which agrees with the rollup: unevaluated, neither covered nor uncovered.
    assert well.coverage_status is None


def test_a_well_with_no_demand_whatsoever_is_still_returned(db_session):
    customer, node = _customer(db_session)
    well = _well(db_session, node, "W-Bare")
    dates = well_dates(db_session, [well.id])
    assert dates[well.id] == WellDates(
        well_id=well.id,
        earliest_ros_date=None,
        first_runout_date=None,
        in_scope_line_count=0,
    )


# ---------------------------------------------------------------------------
# First runout date -- the definition, and the readings it rejects
# ---------------------------------------------------------------------------


def test_a_fully_covered_well_has_NO_runout_date(db_session):
    """None means "no shortage". It is an ANSWER, not a gap, and not a sentinel.

    A far-future sentinel date was the tempting alternative and is exactly what this
    forbids: a sentinel sorts and renders like a fact, so a well with no problem
    would appear on a dates column as though it had one scheduled.
    """
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=100_000)
    well = _well(db_session, node, "W-Fine")
    _line(db_session, well, product, 1000)
    _line(db_session, well, product, 2000, days_out=FAR + 30)
    recompute_well(db_session, well)

    dates = well_dates(db_session, [well.id])[well.id]
    assert well.coverage_status == CoverageStatus.COVERED.value
    assert dates.earliest_ros_date is not None
    assert dates.first_runout_date is None
    assert dates.has_shortage is False


def test_first_runout_is_the_earliest_UNSATISFIED_lines_ros(db_session):
    """Not the earliest line -- the earliest line that was let down.

    The well's first two lines are Covered; the third is not. The answer must be the
    third line's ROS, so an implementation that just returned `earliest_ros_date`
    when a well was uncovered would fail here.
    """
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=3000)
    well = _well(db_session, node, "W-Short")
    _line(db_session, well, product, 1000, days_out=FAR)
    _line(db_session, well, product, 2000, days_out=FAR + 30)
    short = _line(db_session, well, product, 5000, days_out=FAR + 60)
    recompute_well(db_session, well)

    dates = well_dates(db_session, [well.id])[well.id]
    assert short.coverage_result.status == CoverageStatus.UNCOVERED
    assert dates.first_runout_date == short.ros_date
    # ...and it is strictly later than the well's earliest ROS, which is the whole
    # reason the two dates are separate fields.
    assert dates.first_runout_date > dates.earliest_ros_date


def test_a_pending_approval_line_counts_as_a_shortage(db_session):
    """Nothing has been DRAWN for a PendingApproval line, so the well is short.

    `SATISFIED_STATUSES` is exactly Covered and CoveredViaSubstitute. PendingApproval
    is a state in which the platform has promised nothing and consumed nothing, so
    treating it as satisfied would tell a planner the well is fine while it waits for
    a decision that may never come.
    """
    assert SATISFIED_STATUSES == {"Covered", "CoveredViaSubstitute"}


def test_an_unevaluated_line_counts_as_a_shortage(db_session):
    """A line with NO CoverageResult contributes its ROS, never silence.

    Same convention as `app.engines.mrp._unresolved`: a missing verdict means the
    engine has not evaluated the line, and "not evaluated" must never be reported as
    "fine". The line is created and deliberately NOT recomputed.
    """
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=100_000)
    well = _well(db_session, node, "W-Unevaluated")
    line = _line(db_session, well, product, 1000)

    assert line.coverage_result is None
    dates = well_dates(db_session, [well.id])[well.id]
    assert dates.first_runout_date == line.ros_date


def test_first_runout_is_not_the_products_runout_month(db_session):
    """A COVERED well gets NO runout date even when its product's curve goes negative.

    The counterexample that rejects the projection-based reading, direction one.
    `mrp._runout_series` buckets the demand of EVERY well charged to a product, so a
    pool exhausted by a LATER well makes the series close negative -- for every
    consumer of that product, including this well, whose own line was satisfied out
    of the same pool because its ROS came first.

    Reporting that month here would state that a fully covered well "has a problem
    from month M", which is false, and would put it on any screen sorted by trouble.
    """
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=3000)
    covered_well = _well(db_session, node, "W-First")
    hungry_well = _well(db_session, node, "W-Second")
    _line(db_session, covered_well, product, 3000, days_out=FAR)
    _line(db_session, hungry_well, product, 9000, days_out=FAR + 40)
    recompute_customer(db_session, customer)

    assert covered_well.coverage_status == CoverageStatus.COVERED.value

    # The PRODUCT genuinely runs out -- the projection this test rejects is real.
    analysis = by_item(db_session, product.id)
    assert analysis.runout_month is not None

    dates = well_dates(db_session)
    assert dates[covered_well.id].first_runout_date is None, (
        "a covered well must not inherit its product's runout month, which is "
        "driven by another well's demand"
    )
    # ...while the well that is actually short does get a date.
    assert dates[hungry_well.id].first_runout_date is not None


def test_a_policy_driven_shortage_still_reports_a_runout_date(db_session):
    """An UNCOVERED well gets a date even when its product's curve NEVER goes negative.

    The counterexample that rejects the projection-based reading, direction two --
    and the seeded demo case is Well Merlin-05.

    Under HARD allocation a line is covered only from inventory assigned to it, so
    this well is Uncovered beside plentiful unassigned stock. Total demand never
    exceeds what the Business Unit holds, so the monthly balance never closes
    negative and the projection reading would return None -- "no shortage" -- for a
    well the coverage engine has declared Uncovered. A quantity curve cannot see
    assignments, approvals, substitution or BU scope, which is why the verdict is
    the authority here and ROS supplies only the timing.
    """
    customer, node = _customer(db_session, policy=AllocationPolicy.HARD, name="Hard Co")
    product = _product(db_session, on_hand=50_000)
    well = _well(db_session, node, "W-Hard")
    line = _line(db_session, well, product, 5000)
    recompute_well(db_session, well)

    assert well.coverage_status == CoverageStatus.UNCOVERED.value
    # The steel is there in abundance: the projection never dips below zero.
    analysis = by_item(db_session, product.id)
    assert analysis.runout_month is None
    assert all(point.closing_balance >= 0 for point in analysis.runout)

    dates = well_dates(db_session, [well.id])[well.id]
    assert dates.first_runout_date == line.ros_date, (
        "a shortage created by the allocation policy is still a shortage; a monthly "
        "balance curve cannot see it, which is why it is not the instrument used"
    )


def test_assigning_the_stock_clears_the_runout_date(db_session):
    """The complement: fix the cause and the date goes away.

    Guards against a trivially-passing implementation that returned the earliest ROS
    for every well regardless of coverage.
    """
    customer, node = _customer(db_session, policy=AllocationPolicy.HARD, name="Hard2 Co")
    product = _product(db_session, on_hand=50_000)
    well = _well(db_session, node, "W-Hard2")
    line = _line(db_session, well, product, 5000)
    db_session.add(
        InventoryAssignment(
            demand_line_id=line.id, product_id=product.id, quantity=5000,
            source_system="synthetic",
        )
    )
    db_session.flush()
    recompute_well(db_session, well)

    assert well.coverage_status == CoverageStatus.COVERED.value
    dates = well_dates(db_session, [well.id])[well.id]
    assert dates.earliest_ros_date == line.ros_date
    assert dates.first_runout_date is None


def test_a_substituted_line_is_satisfied_and_sets_no_runout_date(db_session):
    """CoveredViaSubstitute means steel was genuinely drawn, so there is no shortage."""
    from app.engines.substitution import decide_approval, request_approval
    from app.models import CustomerSubstitutionRule, TechnicalSubstitution

    customer, node = _customer(db_session, name="Sub Co")
    primary = _product(db_session, on_hand=0, size="7", weight=29.0)
    substitute = _product(db_session, on_hand=9000, size="7", weight=32.0)
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
    well = _well(db_session, node, "W-Sub")
    line = _line(db_session, well, primary, 4000)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)
    recompute_well(db_session, well)

    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    dates = well_dates(db_session, [well.id])[well.id]
    assert dates.first_runout_date is None


def test_filters_are_honoured_so_a_projection_gets_its_own_dates(db_session):
    """Passing non-default filters changes both dates, as it must.

    The Coverage Workspace recomputes verdicts for the filters actually requested, so
    the dates beside those verdicts have to answer the same question. Serving
    default-filter dates next to projected verdicts is the mismatch
    `filters.recomputed_read_only` exists to rule out.
    """
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=100_000)
    well = _well(db_session, node, "W-Projected")
    planned_well = _well(
        db_session, node, "W-Projected-Planned", demand_status=DemandStatus.PLANNED
    )
    confirmed = _line(db_session, well, product, 1000, days_out=FAR)
    planned = _line(db_session, planned_well, product, 1000, days_out=FAR - 100)
    recompute_customer(db_session, customer)

    default = well_dates(db_session, [well.id, planned_well.id])
    assert default[well.id].earliest_ros_date == confirmed.ros_date
    assert default[planned_well.id].earliest_ros_date is None
    assert default[planned_well.id].in_scope_line_count == 0

    widened = well_dates(
        db_session,
        [well.id, planned_well.id],
        status_filter={DemandStatus.CONFIRMED, DemandStatus.PLANNED},
    )
    # The Planned WELL arrives whole, with its own date.
    assert widened[planned_well.id].earliest_ros_date == planned.ros_date
    assert widened[planned_well.id].in_scope_line_count == 1
    # And the Confirmed well is unaffected -- widening the status filter moves whole
    # wells, so it cannot reach inside one that was already in scope.
    assert widened[well.id].earliest_ros_date == confirmed.ros_date
    assert widened[well.id].in_scope_line_count == 1


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_sort_by_earliest_ros_is_ascending_with_no_dates_last(db_session):
    """The ONE ordering rule, so every screen agrees."""

    class Row:
        def __init__(self, name, ros):
            self.well_name = name
            self.earliest_ros_date = ros

    now = datetime(2026, 1, 1)
    rows = [
        Row("late", now + timedelta(days=90)),
        Row("nodate", None),
        Row("early", now),
        Row("middle", now + timedelta(days=30)),
    ]
    assert [r.well_name for r in sort_by_earliest_ros(rows)] == [
        "early",
        "middle",
        "late",
        # A well with no date cannot be urgent, so it sorts LAST -- None is not the
        # beginning of time.
        "nodate",
    ]


def test_ties_break_on_name_so_the_order_is_total(db_session):
    """Two wells due the same day must not swap places between requests."""

    class Row:
        def __init__(self, name, ros):
            self.well_name = name
            self.earliest_ros_date = ros

    same = datetime(2026, 6, 1)
    rows = [Row("Zulu", same), Row("Alpha", same), Row("Mike", same)]
    assert [r.well_name for r in sort_by_earliest_ros(rows)] == ["Alpha", "Mike", "Zulu"]
    # Idempotent -- sorting an already-sorted list changes nothing.
    once = sort_by_earliest_ros(rows)
    assert [r.well_name for r in sort_by_earliest_ros(once)] == [
        r.well_name for r in once
    ]


# ---------------------------------------------------------------------------
# Batching -- the performance guarantee, asserted rather than hoped for
# ---------------------------------------------------------------------------


def test_well_dates_costs_a_constant_number_of_queries(db_session):
    """The cost must NOT grow with the number of wells.

    The Home Dashboard and the coverage grid both list every well, so this is the
    difference between two queries and several hundred -- and, had the rejected
    projection reading been used, between two queries and N full passes over the
    whole demand book.

    Asserted as an exact query COUNT and asserted to be IDENTICAL for 3 wells and
    for 30. A ceiling alone would pass for an implementation that grew slowly;
    equality is what pins "independent of N".
    """
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=1_000_000)

    def count_queries(well_ids):
        statements = []

        def before_cursor_execute(conn, cursor, statement, *args):
            statements.append(statement)

        engine = db_session.get_bind()
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            well_dates(db_session, well_ids)
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)
        return len(statements)

    small = []
    for index in range(3):
        well = _well(db_session, node, f"W-Small-{index}")
        _line(db_session, well, product, 100, days_out=FAR + index)
        small.append(well.id)
    recompute_customer(db_session, customer)
    small_cost = count_queries(small)

    large = list(small)
    for index in range(27):
        well = _well(db_session, node, f"W-Large-{index}")
        _line(db_session, well, product, 100, days_out=FAR + index)
        large.append(well.id)
    recompute_customer(db_session, customer)
    large_cost = count_queries(large)

    assert small_cost == large_cost, (
        f"well_dates issued {small_cost} queries for 3 wells and {large_cost} for "
        f"{len(large)} -- the cost must not scale with the number of wells. A "
        "per-well lookup (or, worse, a per-well runout projection) has been "
        "reintroduced."
    )
    # The documented cost: one query for the wells, one for their lines + verdicts.
    assert small_cost == 2, (
        f"expected exactly 2 queries (wells, then lines outer-joined to verdicts); "
        f"got {small_cost}"
    )


def test_the_whole_platform_answer_is_also_two_queries(db_session):
    """`well_ids=None` (every well) must not be the slow path either.

    That is the shape both the Home Dashboard and the coverage grid actually use.
    """
    customer, node = _customer(db_session)
    product = _product(db_session, on_hand=1_000_000)
    for index in range(20):
        well = _well(db_session, node, f"W-All-{index}")
        _line(db_session, well, product, 100, days_out=FAR + index)
    recompute_customer(db_session, customer)

    statements = []

    def before_cursor_execute(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        result = well_dates(db_session)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert len(result) == 20
    assert len(statements) == 2
