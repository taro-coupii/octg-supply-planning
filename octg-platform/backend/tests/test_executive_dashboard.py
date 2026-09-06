"""GET /dashboard/executive.

This screen goes to management, so the tests here are mostly about HONESTY:
a figure that cannot be computed must come back flagged unavailable with a
reason, never as a zero that reads as a measurement.
"""

from datetime import datetime, timedelta

import pytest

from app.engines.executive import _add_months
from app.main import app
from app.models import (
    CoverageResult,
    CoverageStatus,
    DemandLine,
    DemandProfile,
    DemandRevision,
    InventoryAssignment,
    InventoryOnOrder,
)
from tests.phase5_fixtures import NOW, build_client


@pytest.fixture()
def client_world():
    client, session_factory, world = build_client()
    try:
        yield client, session_factory, world
    finally:
        app.dependency_overrides.clear()


def _exec(client, **params):
    resp = client.get("/dashboard/executive", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


def test_coverage_is_measured_by_quantity_and_keeps_the_well_counts(client_world):
    """REPURPOSED from `test_coverage_percentage_counts_only_evaluated_wells`.

    That test pinned the well-count ratio AS THE HEADLINE, which is precisely what
    the product owner changed: "coverage should be based on quantity from lines not
    number of wells. while good to keep well situation for reference". So the
    headline assertion moves to the quantity ratio and every well-count assertion is
    KEPT VERBATIM against `well_coverage_pct` and the counts.

    That is not a weakening -- it is strictly more. The old test asserted one ratio
    and four counts; this one asserts two ratios, the same four counts, the two
    quantity totals and their units. The well-count behaviour that was pinned is
    still pinned, on the field it now lives on.

    The two measures genuinely disagree in this fixture (2/3 of wells, 60% of
    quantity), which is the whole reason the owner wanted the quantity one.
    """
    client, _sf, _w = client_world
    cov = _exec(client)["coverage"]
    assert cov["available"] is True

    # THE NEW PRIMARY MEASURE. In-scope demand is L1 5000 + L4 1000 (both Covered)
    # + L2 3000 (Uncovered) + L5 1000 (Unrecoverable) = 10000, of which 6000 is
    # covered.
    assert cov["total_quantity"] == 10000
    assert cov["covered_quantity"] == 6000
    assert cov["coverage_pct"] == pytest.approx(60.0)
    assert cov["in_scope_line_count"] == 4
    # Every fixture product is in metres, so the scalar totals are safe to render.
    assert cov["unit_of_measure"] == "Mtr"
    assert cov["total_quantities_by_unit"] == [{"unit_of_measure": "Mtr", "quantity": 10000}]

    # THE REFERENCE MEASURE, asserted exactly as it was before the change.
    assert cov["covered_well_count"] == 2       # WELL-1, WELL-3
    assert cov["uncovered_well_count"] == 1     # WELL-2
    assert cov["evaluated_well_count"] == 3
    # WELL-4 (Budgeted) and WELL-5 (Planned) -- two whole wells the status filter
    # leaves out, so neither is evaluated. See tests/phase5_fixtures.
    assert cov["unevaluated_well_count"] == 2
    assert cov["well_coverage_pct"] == pytest.approx(2 / 3 * 100)
    # And the two measures do NOT agree, which is the point.
    assert cov["coverage_pct"] != cov["well_coverage_pct"]


def test_quantity_coverage_snapshot_partitions_the_in_scope_book(client_world):
    """The snapshot must SUM to the total, or it is decoration rather than a figure.

    One headline percentage says how much is covered; the snapshot says what the
    rest actually IS, and the five statuses need five different actions. If the
    buckets did not partition the book exactly, a manager reading them would be
    unable to tell whether something had been dropped.
    """
    client, _sf, _w = client_world
    cov = _exec(client)["coverage"]
    by_status = {row["status"]: row for row in cov["by_status"]}

    # Every status has an entry, present at zero where nothing is in it -- a status
    # that vanished when empty would make the list look like changing data.
    assert set(by_status) == {
        "Covered",
        "CoveredViaSubstitute",
        "PendingApproval",
        "Uncovered",
        "Unrecoverable",
        "NotEvaluated",
    }
    assert by_status["Covered"]["quantity"] == 6000
    assert by_status["Covered"]["line_count"] == 2
    assert by_status["Covered"]["well_count"] == 2
    assert by_status["Covered"]["counts_as_covered"] is True
    assert by_status["Uncovered"]["quantity"] == 3000
    assert by_status["Unrecoverable"]["quantity"] == 1000
    assert by_status["CoveredViaSubstitute"]["quantity"] == 0
    assert by_status["NotEvaluated"]["quantity"] == 0

    # THE PARTITION, asserted on both the quantity and the line count.
    assert sum(row["quantity"] for row in cov["by_status"]) == cov["total_quantity"]
    assert (
        sum(row["line_count"] for row in cov["by_status"]) == cov["in_scope_line_count"]
    )
    # And the covered buckets sum to the covered quantity.
    assert (
        sum(row["quantity"] for row in cov["by_status"] if row["counts_as_covered"])
        == cov["covered_quantity"]
    )


def test_an_in_scope_line_with_no_verdict_is_NotEvaluated_and_not_covered(client_world):
    """A missing CoverageResult must never flatter the ratio.

    Deleting the verdicts leaves the demand in scope -- it is still 10000 metres
    somebody has committed to -- so the quantity denominator does NOT shrink. What
    changes is that none of it is known to be covered, which lands in `NotEvaluated`
    rather than in Uncovered (that would assert a verdict the engine never reached)
    and rather than disappearing (that would make 0 of 0 look like full coverage).
    """
    client, session_factory, _w = client_world
    db = session_factory()
    try:
        for cr in db.query(CoverageResult).all():
            db.delete(cr)
        db.commit()
    finally:
        db.close()

    cov = _exec(client)["coverage"]
    by_status = {row["status"]: row["quantity"] for row in cov["by_status"]}
    assert cov["total_quantity"] == 10000
    assert cov["covered_quantity"] == 0
    assert by_status["NotEvaluated"] == 10000
    assert by_status["Uncovered"] == 0
    # 0% here is a MEASUREMENT -- 10000 metres in scope and nothing known to be
    # covered -- so it is reported, unlike the empty-book case below.
    assert cov["coverage_pct"] == pytest.approx(0.0)


def test_coverage_is_unavailable_not_zero_when_no_demand_is_in_scope(client_world):
    """REPURPOSED from `test_coverage_is_unavailable_not_zero_when_nothing_is_evaluated`.

    The claim is unchanged and is the one that matters: a coverage percentage over
    an EMPTY set does not exist, and 0% would be a claim about nothing. What changed
    is how the set is emptied. The denominator used to be evaluated WELLS, so
    clearing the rollups emptied it; it is now in-scope demand QUANTITY, so the set
    is emptied by taking the wells out of the status filter -- which is the real
    condition under which there is no book to measure.

    The reason text is asserted just as strictly, including the "not 0%" phrase the
    old test pinned.
    """
    client, session_factory, _w = client_world
    db = session_factory()
    try:
        from app.models import DemandStatus, Well

        # Every well out of the default (Confirmed-only) status scope, so no line is
        # in scope at all. Assigned directly rather than through
        # `set_well_demand_status` because this test is about the dashboard's
        # arithmetic, not about the write path.
        for well in db.query(Well).all():
            well.demand_status = DemandStatus.PLANNED
            well.coverage_status = None
        for cr in db.query(CoverageResult).all():
            db.delete(cr)
        db.commit()
    finally:
        db.close()

    cov = _exec(client)["coverage"]
    assert cov["available"] is False
    assert cov["coverage_pct"] is None
    assert cov["well_coverage_pct"] is None
    assert cov["total_quantity"] == 0
    assert "not 0%" in cov["reason"]


def test_coverage_scoped_to_one_customer(client_world):
    client, _sf, w = client_world
    cov = _exec(client, customer_id=w.beta_id)["coverage"]
    assert cov["evaluated_well_count"] == 1
    assert cov["well_coverage_pct"] == 100.0
    # Beta holds only L4: 1000 metres, covered.
    assert cov["total_quantity"] == 1000
    assert cov["covered_quantity"] == 1000
    assert cov["coverage_pct"] == 100.0


# --------------------------------------------------------------------------
# Supply risk -- must REUSE the engine's Unrecoverable verdict
# --------------------------------------------------------------------------


def test_supply_risk_is_the_engines_unrecoverable_verdict(client_world):
    client, _sf, _w = client_world
    risk = _exec(client)["supply_risk"]
    assert risk["available"] is True
    assert risk["unrecoverable_line_count"] == 1
    assert risk["unrecoverable_quantity"] == 1000  # L5
    assert risk["affected_well_count"] == 1
    # 1000 of the 10000 in-scope units.
    assert risk["unrecoverable_pct_of_in_scope_demand"] == pytest.approx(10.0)
    assert "on order" in risk["note"]


def test_supply_risk_follows_the_stored_verdict_rather_than_re_deriving_it(
    client_world,
):
    """Flip one stored verdict to Unrecoverable WITHOUT touching lead times. If
    the dashboard re-derived recoverability from the calendar it would ignore the
    change; because it reads the engine's verdict, the number moves."""
    client, session_factory, w = client_world
    db = session_factory()
    try:
        cr = db.get(CoverageResult, w.l2_id)
        assert cr.status == CoverageStatus.UNCOVERED
        cr.status = CoverageStatus.UNRECOVERABLE
        db.commit()
    finally:
        db.close()

    risk = _exec(client)["supply_risk"]
    assert risk["unrecoverable_line_count"] == 2
    assert risk["unrecoverable_quantity"] == 4000  # L5 1000 + L2 3000
    assert risk["affected_well_count"] == 1


def test_supply_risk_ignores_a_coverage_row_that_outlived_its_scope(client_world):
    """An out-of-scope line must not inflate the most alarming figure on the
    dashboard, even if a CoverageResult row for it survives."""
    client, session_factory, w = client_world
    db = session_factory()
    try:
        db.add(
            CoverageResult(
                demand_line_id=w.l6_id,  # on WELL-4, Budgeted -- out of scope
                status=CoverageStatus.UNRECOVERABLE,
                reason="stale row from an older build",
            )
        )
        db.commit()
    finally:
        db.close()

    risk = _exec(client)["supply_risk"]
    assert risk["unrecoverable_line_count"] == 1
    assert risk["unrecoverable_quantity"] == 1000


# --------------------------------------------------------------------------
# Allocation
# --------------------------------------------------------------------------


def _channels(body):
    return {c["key"]: c for c in body["soft_allocation_coverage"]["channels"]}


def test_soft_allocation_coverage_is_available_with_no_oracle_assignment_at_all(
    client_world,
):
    """REPURPOSED from `test_allocation_is_unavailable_when_the_oracle_projection_is_empty`.

    That test pinned the old block's refusal: with no `InventoryAssignment` rows,
    "nothing is allocated" and "the feed has never run" were indistinguishable, so
    0% could not be reported. The product owner has replaced the question -- the
    block now reports SOFT allocation coverage, i.e. what this platform's own
    allocation achieved -- and for that question an empty assignment table is not an
    obstacle at all: a pool draw needs no assignment. So the block is AVAILABLE.

    That is not a weakening of the honesty rule, it is the same rule applied to the
    new figure, and this test now pins three things instead of one:

      * the figure is computed rather than refused, because it can be;
      * the ORACLE half of it is still explicitly `unavailable` -- no assignment
        row exists, and the block says so rather than reporting 0 as if measured;
      * the note disclaims Oracle hard allocation in words, so the confusion the
        owner reported cannot recur through the payload.
    """
    client, _sf, _w = client_world
    block = _exec(client)["soft_allocation_coverage"]

    assert block["available"] is True
    assert block["horizon_months"] == 12
    # In-scope demand inside 12 months: L1 5000 + L5 1000 + L4 1000 = 7000
    # (L2's ROS is +400d, outside the horizon).
    assert block["total_quantity"] == 7000

    channels = _channels(_exec(client))
    # Both fixture customers are SOFT, so every satisfied metre came from the shared
    # pool and NOTHING from an assignment. That is the pooling guarantee as a number.
    assert channels["from_own_assignment"]["quantity"] == 0.0
    assert block["assignment_source"] == "unavailable"
    # L1 5000 covered + L4 1000 covered, and L5 drew nothing (P-B holds 0).
    assert channels["from_shared_pool"]["quantity"] == 6000
    assert channels["not_satisfied"]["quantity"] == 1000
    assert channels["via_substitute"]["quantity"] == 0

    # The rename is in the payload, and the old key is gone so no screen can keep
    # reading it and calling it Oracle's allocation.
    assert "allocation" not in _exec(client)
    assert "soft allocation coverage" in block["note"].lower()
    assert "not Oracle" in block["note"] or "NOT Oracle" in block["note"]
    assert "not a reservation" in block["note"]


def test_soft_allocation_channels_sum_to_the_horizon_demand(client_world):
    """The four channels partition demand exactly, or they are not an accounting."""
    client, _sf, _w = client_world
    body = _exec(client)
    block = body["soft_allocation_coverage"]
    assert (
        sum(c["quantity"] for c in block["channels"]) == block["total_quantity"]
    )
    assert sum(c["pct"] for c in block["channels"]) == pytest.approx(100.0)


def test_soft_allocation_figures_come_from_the_coverage_pass_not_a_second_derivation(
    client_world,
):
    """The hard constraint: ONE implementation of the allocation rules.

    The dashboard's channel quantities are asserted against
    `compute_customer_coverage` directly -- the same read-only pass the coverage
    verdicts come from. If somebody ever re-derives allocation inside the executive
    engine, the two will drift and this fails.
    """
    from app.engines.coverage import compute_customer_coverage
    from app.models import Customer

    client, session_factory, w = client_world
    body = _exec(client, allocation_horizon_months=36)
    channels = _channels(body)

    db = session_factory()
    try:
        expected_pool = 0.0
        expected_assignment = 0.0
        for customer in db.query(Customer).all():
            computed = compute_customer_coverage(db, customer)
            for line_id, verdict in computed.by_line.items():
                expected_pool += computed.consumed_from_pool.get(line_id, 0.0)
                expected_assignment += computed.consumed_from_assignment.get(
                    line_id, 0.0
                )
    finally:
        db.close()

    # 36 months covers every in-scope line of this fixture, so the dashboard's
    # horizon and the coverage pass see the same book.
    assert channels["from_shared_pool"]["quantity"] == pytest.approx(expected_pool)
    assert channels["from_own_assignment"]["quantity"] == pytest.approx(
        expected_assignment
    )


def test_soft_allocation_reports_an_assignment_draw_for_a_hybrid_customer(client_world):
    """REPURPOSED from `test_allocation_computed_once_assignment_rows_exist` and
    `test_allocation_caps_over_assignment_at_the_line_quantity`.

    Both of those pinned the OLD block's arithmetic over `InventoryAssignment`
    (allocated = min(line quantity, assigned), summed). The assignment quantity is
    still reported -- as the `from_own_assignment` channel -- but it is now what the
    ALLOCATION ENGINE actually drew rather than a second reading of the same table,
    which is exactly the single-implementation constraint. The two properties those
    tests protected are both re-asserted here:

      * an assignment row shows up in the figures at all;
      * an OVER-assignment does not inflate anything -- the draw is capped at the
        line's own quantity, so 99999 assigned to a 5000-metre line contributes
        5000. (`app.engines.allocation._allocate_hybrid` caps it, and the cap is
        re-applied here against the line quantity, so the channel can never exceed
        the demand it explains.)

    A SOFT customer would ignore its own assignment entirely, which is why this test
    switches Acme to HYBRID -- under SOFT there is deliberately nothing to see.
    """
    client, session_factory, w = client_world
    db = session_factory()
    try:
        from app.engines.coverage import recompute_customer
        from app.models import AllocationPolicy, Customer

        acme = db.get(Customer, w.acme_id)
        acme.allocation_policy = AllocationPolicy.HYBRID
        db.add(
            InventoryAssignment(
                demand_line_id=w.l1_id,
                product_id=w.p_a_id,
                quantity=99999,
                source_system="synthetic",
            )
        )
        db.flush()
        recompute_customer(db, acme)
        db.commit()
    finally:
        db.close()

    body = _exec(client)
    channels = _channels(body)
    # Capped at L1's own 5000 -- an over-assignment is a data artefact, never 130%
    # of a line.
    assert channels["from_own_assignment"]["quantity"] == 5000
    assert channels["from_own_assignment"]["pct"] == pytest.approx(5000 / 7000 * 100)
    # Provenance of the assignment channel is still visible: demo data, not Oracle.
    assert body["soft_allocation_coverage"]["assignment_source"] == "synthetic"
    # And the partition still holds with an assignment in play.
    block = body["soft_allocation_coverage"]
    assert sum(c["quantity"] for c in block["channels"]) == block["total_quantity"]


def test_soft_allocation_horizon_selector(client_world):
    """REPURPOSED from `test_allocation_horizon_selector`. The selector is the same
    wire parameter with the same four values; only the block it drives changed."""
    client, _sf, _w = client_world
    # 24 months reaches L2's +400d ROS, so the book grows by 3000.
    block = _exec(client, allocation_horizon_months=24)["soft_allocation_coverage"]
    assert block["horizon_months"] == 24
    assert block["total_quantity"] == 10000

    for months in (12, 18, 24, 36):
        assert (
            _exec(client, allocation_horizon_months=months)[
                "soft_allocation_coverage"
            ]["horizon_months"]
            == months
        )


def test_invalid_allocation_horizon_is_a_400(client_world):
    client, _sf, _w = client_world
    resp = client.get(
        "/dashboard/executive", params={"allocation_horizon_months": 7}
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Demand trend -- the prior period is genuine or it is unavailable
# --------------------------------------------------------------------------


def test_demand_trend_current_totals(client_world):
    client, _sf, _w = client_world
    horizons = {h["months"]: h for h in _exec(client)["demand_trend"]["horizons"]}
    assert sorted(horizons) == [3, 6, 12, 24]
    # Within 3 months: L1 5000 (+30d) and L5 1000 (+20d).
    assert horizons[3]["current_total"] == 6000
    assert horizons[3]["current_line_count"] == 2
    assert horizons[6]["current_total"] == 6000
    # Within 12 months: adds L4 1000 (+200d). The Planned and Contingency lines are
    # excluded so the trend describes the same book of demand as the coverage figures.
    assert horizons[12]["current_total"] == 7000
    # Within 24 months: adds L2 3000 (+400d).
    assert horizons[24]["current_total"] == 10000



def _backdate_lines(session_factory, line_ids, when):
    """Make demand lines LOOK like rows that predate history being recorded.

    Every demand line created by this platform now carries a `created_at` and an
    initial revision (change 4), which is exactly what lets the prior-period figure
    accumulate. Rows that existed BEFORE that change have neither fact, and cannot
    be back-filled -- see the Alembic revision e5c2f81a4b90. There is no way to
    build such a row through the ORM any more, so the tests that are about that
    case construct it explicitly: a creation timestamp in the past, with no
    revision at or before the comparison date.
    """
    db = session_factory()
    try:
        for line_id in line_ids:
            line = db.get(DemandLine, line_id)
            line.created_at = when
            for rev in list(line.revisions):
                db.delete(rev)
        db.commit()
    finally:
        db.close()


def test_prior_period_is_unavailable_for_rows_that_predate_history(client_world):
    """A row with NO creation timestamp of its own and no revision at the
    comparison date cannot be reconstructed, and must be reported, not guessed.

    This is the same assertion the suite always made; what changed is how such a
    row comes about. It used to be every row, because nothing recorded creation.
    Now it is only rows written before change 4, so the test constructs one
    explicitly -- see `_backdate_lines`. The reason text is asserted just as
    strictly, including the new promise that the figure accumulates by itself.
    """
    client, session_factory, w = client_world
    _backdate_lines(
        session_factory,
        (w.l1_id, w.l2_id, w.l4_id, w.l5_id),
        NOW - timedelta(days=40 * 30),
    )
    for horizon in _exec(client)["demand_trend"]["horizons"]:
        previous = horizon["previous"]
        assert previous["available"] is False
        assert previous["value"] is None
        assert "neither a creation timestamp nor revision history" in previous["reason"]
        assert "would invent a trend" in previous["reason"]
        # The promise the owner was given is in the text, so it stays true.
        assert "accumulates and becomes available on its own" in previous["reason"]
        # And no derived percentage is fabricated from it either.
        assert horizon["change_pct"]["available"] is False
        assert horizon["change_pct"]["value"] is None
        # The comparison date is still stated, so the UI can explain itself.
        assert horizon["previous_as_of"] is not None
        assert "reconstructed from revision history" in horizon["definition"]


def test_created_at_makes_a_brand_new_book_comparable(client_world):
    """The accumulation property, asserted rather than promised.

    Every fixture line was created just now, through the ordinary ORM path, so each
    carries a `created_at` and an initial revision. Three months ago none of them
    existed -- and that is now a MEASURED fact, not an unknown. So the prior total
    is genuinely available and genuinely 0, which is what "unavailable now,
    accumulates going forward" has to mean in practice.
    """
    client, _sf, _w = client_world
    three = {h["months"]: h for h in _exec(client)["demand_trend"]["horizons"]}[3]
    assert three["previous"]["available"] is True
    assert three["previous"]["value"] == 0.0
    assert three["current_total"] == 6000
    # 0 -> 6000 has no defined percentage, and that is reported rather than faked.
    assert three["change_pct"]["available"] is False
    assert "undefined" in three["change_pct"]["reason"]


def test_prior_period_is_computed_once_history_covers_every_in_scope_line(
    client_world,
):
    """With a revision at or before the comparison date for every in-scope line,
    the prior figure becomes a real reconstruction.

    L1's historic ROS is placed 60 days in the past, so it falls inside the
    3-month horizon as seen from 3 months ago; the others keep their current ROS
    dates, which are in the future and therefore outside that window.
    """
    client, session_factory, w = client_world
    long_ago = NOW - timedelta(days=40 * 30)

    db = session_factory()
    try:
        for line_id, ros in (
            (w.l1_id, NOW - timedelta(days=60)),
            (w.l2_id, None),
            (w.l4_id, None),
            (w.l5_id, None),
        ):
            line = db.get(DemandLine, line_id)
            db.add(
                DemandRevision(
                    demand_line_id=line.id,
                    revision_no=1,
                    quantity=line.quantity,
                    ros_date=ros or line.ros_date,
                    status=line.well.demand_status,
                    profile=line.profile,
                    created_at=long_ago,
                )
            )
        db.commit()
    finally:
        db.close()

    horizons = {h["months"]: h for h in _exec(client)["demand_trend"]["horizons"]}
    three = horizons[3]
    assert three["previous"]["available"] is True
    assert three["previous"]["value"] == 5000
    assert three["current_total"] == 6000
    assert three["change_pct"]["available"] is True
    assert three["change_pct"]["value"] == pytest.approx(20.0)


def test_prior_period_zero_is_reported_as_measured_but_the_pct_is_not(client_world):
    """A genuine prior total of zero is a measurement and is reported as 0. The
    percentage CHANGE from zero is undefined, so that is unavailable instead of
    being rendered as an infinite or 0% growth."""
    client, session_factory, w = client_world
    long_ago = NOW - timedelta(days=40 * 30)
    db = session_factory()
    try:
        for line_id in (w.l1_id, w.l2_id, w.l4_id, w.l5_id):
            line = db.get(DemandLine, line_id)
            db.add(
                DemandRevision(
                    demand_line_id=line.id,
                    revision_no=1,
                    quantity=line.quantity,
                    ros_date=line.ros_date,  # all in the future -> nothing in window
                    status=line.well.demand_status,
                    profile=line.profile,
                    created_at=long_ago,
                )
            )
        db.commit()
    finally:
        db.close()

    three = {h["months"]: h for h in _exec(client)["demand_trend"]["horizons"]}[3]
    assert three["previous"]["available"] is True
    assert three["previous"]["value"] == 0.0
    assert three["change_pct"]["available"] is False
    assert "undefined" in three["change_pct"]["reason"]


def test_partial_history_still_yields_unavailable_rather_than_a_partial_total(
    client_world,
):
    """One line without history is enough to make the figure not genuine, because
    we cannot tell whether it existed at the comparison date."""
    client, session_factory, w = client_world
    long_ago = NOW - timedelta(days=40 * 30)
    # All four lines predate history recording; only ONE of them gets a revision.
    _backdate_lines(session_factory, (w.l1_id, w.l2_id, w.l4_id, w.l5_id), long_ago)
    db = session_factory()
    try:
        line = db.get(DemandLine, w.l1_id)
        db.add(
            DemandRevision(
                demand_line_id=line.id,
                revision_no=1,
                quantity=line.quantity,
                ros_date=NOW - timedelta(days=60),
                status=line.well.demand_status,
                profile=line.profile,
                created_at=long_ago,
            )
        )
        db.commit()
    finally:
        db.close()

    three = {h["months"]: h for h in _exec(client)["demand_trend"]["horizons"]}[3]
    assert three["previous"]["available"] is False
    assert "3 of 4 in-scope demand lines" in three["previous"]["reason"]


def test_revisions_written_after_the_comparison_date_do_not_count(client_world):
    """A revision created yesterday says nothing about the book 3 months ago."""
    client, session_factory, w = client_world
    # Backdated so the lines are not excluded by `created_at` instead -- the point
    # of this test is that a too-recent REVISION is not usable as historic state.
    _backdate_lines(
        session_factory,
        (w.l1_id, w.l2_id, w.l4_id, w.l5_id),
        NOW - timedelta(days=40 * 30),
    )
    db = session_factory()
    try:
        for line_id in (w.l1_id, w.l2_id, w.l4_id, w.l5_id):
            line = db.get(DemandLine, line_id)
            db.add(
                DemandRevision(
                    demand_line_id=line.id,
                    revision_no=1,
                    quantity=line.quantity,
                    ros_date=line.ros_date,
                    status=line.well.demand_status,
                    profile=line.profile,
                    created_at=NOW - timedelta(days=1),
                )
            )
        db.commit()
    finally:
        db.close()

    horizons = {h["months"]: h for h in _exec(client)["demand_trend"]["horizons"]}
    assert horizons[3]["previous"]["available"] is False
    assert horizons[24]["previous"]["available"] is False


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


def test_on_order_is_never_silently_reported_as_zero(client_world):
    """REPURPOSED, and NOT weakened.

    The original form of this test asserted `"on_order" not in str(body)` -- i.e.
    that the dashboard carried no on-order key at all. That absence was never the
    property worth protecting; it was the only way, at the time, to guarantee the
    property that WAS worth protecting: a figure nobody had measured must never
    appear as a 0. `on_order` had no storage behind it, so any key would have been
    a fabricated zero.

    There is now a real projection to read
    (`app.models.inventory_on_order.InventoryOnOrder`) and the owner asked for the
    data point, so the key exists. The assertion therefore moves to the property
    itself, which it can now state directly and more strictly than the absence ever
    did: with no projected row, the block is UNAVAILABLE with a reason, serves NO
    number, and says in words that the absence of a row is not a zero. Deleting the
    key would have been the weakening; keeping the guarantee is not.
    """
    client, _sf, _w = client_world
    body = _exec(client)
    joined = " ".join(body["notes"])
    # The provenance note is still on the envelope, in the same words.
    assert "on-order" in joined
    assert "oracle_integrated" in joined

    incoming = body["incoming_supply"]
    # The fixture seeds no purchase orders, so the quantity is UNKNOWN.
    assert incoming["available"] is False
    assert incoming["quantity"] is None, "an unmeasured on-order figure must not be 0"
    assert incoming["source"] == "unavailable"
    assert incoming["oracle_integrated"] is False
    assert "UNKNOWN" in incoming["reason"]
    assert "not reported as" in incoming["reason"]
    # Both fixture products are in scope and neither has a row.
    assert incoming["products_with_no_data"] == 2
    # And nothing in the coverage or risk figures has quietly netted on-order stock
    # against demand -- the note says so and the numbers agree with it.
    assert "never inside them" in incoming["note"]


def test_incoming_supply_distinguishes_nothing_on_order_from_no_data(client_world):
    """The two states, on the dashboard rather than only in the engine.

    An explicit zero-quantity row states "this Business Unit has nothing on order of
    this product", which is a fact and makes the block AVAILABLE at 0. The absence of
    any row is unknown and keeps it unavailable. If those ever collapse, the screen
    is back to publishing a zero nobody measured.
    """
    client, session_factory, w = client_world
    db = session_factory()
    try:
        # P-A: explicitly nothing on order. P-B: still no row at all.
        db.add(
            InventoryOnOrder(
                business_unit_id=w.bu_id,
                product_id=w.p_a_id,
                quantity=0,
                source_system="synthetic",
            )
        )
        db.commit()
    finally:
        db.close()

    incoming = _exec(client)["incoming_supply"]
    assert incoming["available"] is True
    assert incoming["quantity"] == 0.0
    assert incoming["products_with_nothing_on_order"] == 1
    # The other product is still UNKNOWN, and the block says how partial it is
    # rather than letting the 0 stand for both.
    assert incoming["products_with_no_data"] == 1
    assert "floor, not a complete answer" in incoming["reason"]
    # Provenance: seeded rows are not a live feed. Two independent flags.
    assert incoming["source"] == "synthetic"
    assert incoming["oracle_integrated"] is False


def test_incoming_supply_reports_the_expected_arrival_horizon(client_world):
    """The arrival horizon is cumulative and matches the demand-trend windows.

    Also pins that an UNDATED purchase order -- raised but not acknowledged -- is
    counted in the total but in no arrival window. Dating it to today would put
    unscheduled steel inside every horizon and make the incoming figure look
    imminent.
    """
    client, session_factory, w = client_world
    db = session_factory()
    try:
        db.add_all(
            [
                InventoryOnOrder(
                    business_unit_id=w.bu_id,
                    product_id=w.p_a_id,
                    quantity=1200,
                    expected_arrival_date=NOW + timedelta(days=40),
                    source_system="synthetic",
                ),
                InventoryOnOrder(
                    business_unit_id=w.bu_id,
                    product_id=w.p_b_id,
                    quantity=800,
                    expected_arrival_date=NOW + timedelta(days=300),
                    source_system="synthetic",
                ),
                # No promise date: real, and deliberately in no window.
                InventoryOnOrder(
                    business_unit_id=w.bu_id,
                    product_id=w.p_b_id,
                    quantity=500,
                    source_system="synthetic",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    incoming = _exec(client)["incoming_supply"]
    assert incoming["available"] is True
    assert incoming["quantity"] == 2500
    assert incoming["undated_quantity"] == 500
    assert incoming["row_count"] == 3

    horizons = {h["months"]: h for h in incoming["by_arrival_horizon"]}
    # Same windows as the demand trend, so the two can be read against each other.
    assert sorted(horizons) == [3, 6, 12, 24]
    assert horizons[3]["quantity"] == 1200          # the +40d row only
    assert horizons[6]["quantity"] == 1200
    assert horizons[12]["quantity"] == 2000         # + the +300d row
    assert horizons[24]["quantity"] == 2000         # cumulative, and no more arrives
    # The undated 500 is in the total and in NO window.
    assert horizons[24]["quantity"] + incoming["undated_quantity"] == incoming["quantity"]


def test_incoming_supply_never_crosses_the_business_unit_boundary(client_world):
    """A purchase order into another BU is another BU's steel, on every screen."""
    client, session_factory, w = client_world
    db = session_factory()
    try:
        from app.models import BusinessUnit

        other = BusinessUnit(name="BU-2")
        db.add(other)
        db.flush()
        db.add(
            InventoryOnOrder(
                business_unit_id=other.id,
                product_id=w.p_a_id,
                quantity=999_999,
                expected_arrival_date=NOW + timedelta(days=10),
                source_system="synthetic",
            )
        )
        db.commit()
    finally:
        db.close()

    # BU-1 scope: the other BU's order is invisible, so on-order is still unknown.
    incoming = _exec(client, business_unit_id=w.bu_id)["incoming_supply"]
    assert incoming["available"] is False
    assert incoming["quantity"] is None


# --------------------------------------------------------------------------
# Business Unit filter
# --------------------------------------------------------------------------


def test_business_unit_scope_is_stated_on_the_payload(client_world):
    client, _sf, w = client_world
    body = _exec(client, business_unit_id=w.bu_id)
    assert body["business_unit_id"] == w.bu_id
    assert body["business_unit_name"] == "BU-1"
    assert body["customer_id"] is None
    assert body["scope"]["label"] == "BU-1"
    assert body["scope"]["business_unit_id"] == w.bu_id
    # Both fixture customers sit in BU-1, so the BU-scoped figures equal the
    # unscoped ones -- and the payload still says which scope produced them, which
    # is the point.
    assert sorted(body["scope"]["customer_ids"]) == sorted([w.acme_id, w.beta_id])
    assert body["coverage"]["total_quantity"] == 10000
    assert "Scope: BU-1" in " ".join(body["notes"])


def test_customer_scope_states_its_customer_name(client_world):
    client, _sf, w = client_world
    body = _exec(client, customer_id=w.beta_id)
    assert body["customer_id"] == w.beta_id
    assert body["customer_name"] == "Beta"
    assert body["business_unit_id"] is None
    assert body["scope"]["customer_ids"] == [w.beta_id]


def test_the_business_unit_filter_cannot_leak_across_business_units(client_world):
    """The BU is a HARD boundary, so a second BU's demand must not appear in the
    first BU's figures -- in ANY block."""
    client, session_factory, w = client_world
    db = session_factory()
    try:
        from app.engines.coverage import recompute_customer
        from app.models import (
            AllocationPolicy,
            BusinessUnit,
            Customer,
            DemandStatus,
            InventoryOnHand,
            PlanningNode,
            Well,
        )

        bu2 = BusinessUnit(name="BU-2")
        db.add(bu2)
        db.flush()
        gamma = Customer(
            name="Gamma",
            business_unit_id=bu2.id,
            allocation_policy=AllocationPolicy.SOFT,
        )
        db.add(gamma)
        db.flush()
        node = PlanningNode(
            customer_id=gamma.id, node_type="Project", name="Project Gamma"
        )
        db.add(node)
        db.flush()
        well = Well(
            planning_node_id=node.id,
            name="WELL-G1",
            demand_status=DemandStatus.CONFIRMED,
        )
        db.add(well)
        db.flush()
        # BU-2 holds none of P-A, so this line is Uncovered inside BU-2 -- and its
        # 7777 must not reach BU-1's totals by any path.
        db.add(
            InventoryOnHand(business_unit_id=bu2.id, product_id=w.p_a_id, quantity=0)
        )
        db.add(
            DemandLine(
                well_id=well.id,
                product_id=w.p_a_id,
                quantity=7777,
                ros_date=NOW + timedelta(days=25),
                profile=DemandProfile.PRIMARY,
            )
        )
        db.flush()
        recompute_customer(db, gamma)
        db.commit()
        gamma_id = gamma.id
        bu2_id = bu2.id
    finally:
        db.close()

    # Unscoped: both BUs are in, so the new demand IS counted.
    unscoped = _exec(client)
    assert unscoped["coverage"]["total_quantity"] == 10000 + 7777

    # BU-1 scoped: not a single figure moves from the original world.
    bu1 = _exec(client, business_unit_id=w.bu_id)
    assert bu1["coverage"]["total_quantity"] == 10000
    assert bu1["coverage"]["covered_quantity"] == 6000
    assert bu1["soft_allocation_coverage"]["total_quantity"] == 7000
    assert {row["well_name"] for row in bu1["first_runout"]["wells"]} == {"WELL-2"}
    horizons = {h["months"]: h for h in bu1["demand_trend"]["horizons"]}
    assert horizons[3]["current_total"] == 6000

    # BU-2 scoped: only the new demand.
    bu2 = _exec(client, business_unit_id=bu2_id)
    assert bu2["coverage"]["total_quantity"] == 7777
    assert bu2["coverage"]["covered_quantity"] == 0
    assert {row["well_name"] for row in bu2["first_runout"]["wells"]} == {"WELL-G1"}
    assert bu2["scope"]["customer_ids"] == [gamma_id]


def test_a_customer_outside_the_named_business_unit_is_a_400_not_an_empty_result(
    client_world,
):
    """The contradiction decision, pinned with its reasoning.

    An empty 200 would be a MEASUREMENT -- no demand, no risk, no shortfall -- for a
    scope that cannot exist, and it would read as good news. A 400 states the one
    true thing about the request: it was impossible.
    """
    client, session_factory, w = client_world
    db = session_factory()
    try:
        from app.models import BusinessUnit

        other = BusinessUnit(name="BU-Elsewhere")
        db.add(other)
        db.commit()
        other_id = other.id
    finally:
        db.close()

    resp = client.get(
        "/dashboard/executive",
        params={"customer_id": w.acme_id, "business_unit_id": other_id},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "not to Business Unit" in detail
    assert "refused rather than answered" in detail

    # The consistent pair is fine.
    ok = _exec(client, customer_id=w.acme_id, business_unit_id=w.bu_id)
    assert ok["scope"]["label"] == "BU-1 / Acme"


def test_an_unknown_business_unit_is_a_404(client_world):
    client, _sf, _w = client_world
    resp = client.get(
        "/dashboard/executive", params={"business_unit_id": "no-such-bu"}
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# First runout, with customer and well detail
# --------------------------------------------------------------------------


def test_first_runout_detail_names_the_customer_and_the_shortfall(client_world):
    client, _sf, _w = client_world
    detail = _exec(client)["first_runout"]
    assert detail["available"] is True
    # WELL-2 is the only well with unsatisfied in-scope demand (L2 Uncovered,
    # L5 Unrecoverable). WELL-1 and WELL-3 are Covered; WELL-4 and WELL-5 have no
    # in-scope demand at all, so they have no first-runout date -- an answer, not a
    # gap.
    assert [row["well_name"] for row in detail["wells"]] == ["WELL-2"]
    (row,) = detail["wells"]
    assert row["customer_name"] == "Acme"
    assert row["customer_id"] is not None
    # L5's ROS (+20d) is earlier than L2's (+400d), so it drives the date.
    assert row["first_runout_date"].startswith(
        (NOW + timedelta(days=20)).date().isoformat()
    )
    # The shortfall is BOTH unsatisfied lines, which is exactly the set whose
    # earliest ROS produced the date.
    assert row["shortfall_quantity"] == 4000
    assert row["shortfall_line_count"] == 2
    assert row["unit_of_measure"] == "Mtr"
    assert detail["earliest_first_runout_date"] == row["first_runout_date"]


def test_first_runout_detail_is_sorted_and_reports_its_cap(client_world):
    """Sorted earliest-first, and the cap is DECLARED rather than applied silently.

    A management screen showing 10 of 40 shortfalls without saying so understates
    the problem fourfold, and the reader has no way to tell.
    """
    client, session_factory, w = client_world
    db = session_factory()
    try:
        from app.engines.coverage import recompute_customer
        from app.models import Customer, DemandStatus, PlanningNode, Well

        acme = db.get(Customer, w.acme_id)
        node = db.get(PlanningNode, w.alpha_id)
        # 12 more short wells, created in DESCENDING date order so a function that
        # forgot to sort would return them backwards.
        for index in range(12, 0, -1):
            well = Well(
                planning_node_id=node.id,
                name=f"WELL-S{index:02d}",
                demand_status=DemandStatus.CONFIRMED,
            )
            db.add(well)
            db.flush()
            db.add(
                DemandLine(
                    well_id=well.id,
                    product_id=w.p_b_id,  # P-B holds 0, so every one is short
                    quantity=100 + index,
                    ros_date=NOW + timedelta(days=500 + index),
                    profile=DemandProfile.PRIMARY,
                )
            )
        db.flush()
        recompute_customer(db, acme)
        db.commit()
    finally:
        db.close()

    detail = _exec(client)["first_runout"]
    assert detail["available"] is True
    # 13 short wells: WELL-2 plus the 12 new ones.
    assert detail["total_well_count"] == 13
    assert detail["cap"] == 10
    assert detail["returned_well_count"] == 10
    assert detail["omitted_well_count"] == 3
    assert detail["truncated"] is True
    assert "3 are omitted" in detail["reason"]

    # ASCENDING by date, and the cap kept the URGENT ones.
    dates = [row["first_runout_date"] for row in detail["wells"]]
    assert dates == sorted(dates)
    assert detail["wells"][0]["well_name"] == "WELL-2"   # +20d, the earliest
    assert detail["wells"][-1]["well_name"] == "WELL-S09"


def test_first_runout_is_an_answer_when_nothing_falls_short(client_world):
    """No shortfall is a real answer, distinguishable from an unavailable figure."""
    client, session_factory, _w = client_world
    db = session_factory()
    try:
        from app.models import CoverageStatus

        for cr in db.query(CoverageResult).all():
            cr.status = CoverageStatus.COVERED
        db.commit()
    finally:
        db.close()

    detail = _exec(client)["first_runout"]
    assert detail["available"] is True
    assert detail["wells"] == []
    assert detail["total_well_count"] == 0
    assert detail["earliest_first_runout_date"] is None
    assert "an answer, not a gap" in detail["reason"]


def test_unknown_customer_is_a_404(client_world):
    client, _sf, _w = client_world
    assert client.get(
        "/dashboard/executive", params={"customer_id": "nope"}
    ).status_code == 404


def test_add_months_clamps_the_day(client_world):
    assert _add_months(datetime(2027, 1, 31), 1) == datetime(2027, 2, 28)
    assert _add_months(datetime(2027, 3, 15), -3) == datetime(2026, 12, 15)
    assert _add_months(datetime(2027, 3, 15), 24) == datetime(2029, 3, 15)


def test_home_dashboard_still_works(client_world):
    """Guard against the executive route disturbing the existing card data."""
    client, _sf, _w = client_world
    body = client.get("/dashboard/home").json()
    assert {w["name"] for w in body["uncovered_wells"]} == {"WELL-2"}


def test_a_soft_customers_own_pooled_reservation_is_reported_as_reserved_not_as_shared_pool(
    client_world,
):
    """Owner ruling 2026-09-06 (D01 follow-up ②). Acme is SOFT and gets a 3,000
    Oracle assignment on L1. Soft allocation does not tie that steel to L1 -- it is
    pooled across Acme's own wells -- but it IS reserved to Acme and no neighbour can
    reach it, so the Executive block must not call it "shared unassigned pool".

      P-A 6000 on hand, 3000 assigned to Acme -> shared pool 3000.
      L1 (Acme, 5000): 3000 from its own pooled reservation + 2000 from the pool.
      L4 (Beta, 1000): 1000 from the pool.

    The reserved 3,000 lands in `from_own_assignment`; `from_shared_pool` is the
    3,000 of genuinely unassigned steel; the five channels still partition the
    7,000 of in-scope demand.
    """
    client, session_factory, w = client_world
    db = session_factory()
    try:
        from app.engines.coverage import compute_customer_coverage, recompute_customer
        from app.models import Customer

        acme = db.get(Customer, w.acme_id)
        db.add(
            InventoryAssignment(
                demand_line_id=w.l1_id,
                product_id=w.p_a_id,
                quantity=3000,
                source_system="synthetic",
            )
        )
        db.flush()
        recompute_customer(db, acme)
        db.commit()
        # The projection carries the reserved part separately, and the pool figure
        # still includes it (that is what "the pool" means to a SOFT pass).
        mine = compute_customer_coverage(db, acme)
        assert mine.consumed_from_assignment_block[w.l1_id] == 3000
        assert mine.consumed_from_pool[w.l1_id] == 5000
        # Under SOFT nothing is drawn line-by-line; the reservation is pooled.
        assert all(q == 0 for q in mine.consumed_from_assignment.values())
    finally:
        db.close()

    body = _exec(client)
    channels = _channels(body)
    assert channels["from_own_assignment"]["quantity"] == 3000
    assert channels["from_shared_pool"]["quantity"] == 3000
    assert channels["not_satisfied"]["quantity"] == 1000
    assert channels["from_customer_owned"]["quantity"] == 0
    assert channels["via_substitute"]["quantity"] == 0
    assert sum(c["quantity"] for c in channels.values()) == body[
        "soft_allocation_coverage"
    ]["total_quantity"] == 7000
    # And the label says what the channel now holds.
    assert "reserved to this customer" in channels["from_own_assignment"]["label"]
    assert "pooled across its own wells under SOFT" in channels["from_own_assignment"]["label"]
