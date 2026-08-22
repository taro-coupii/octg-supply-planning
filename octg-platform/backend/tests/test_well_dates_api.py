"""The well dates as the SCREENS receive them, and in the order they receive them.

`tests/test_well_dates.py` pins the engine's definitions. This module pins that the
three consumers the product owner named -- the Home Dashboard's uncovered-wells
card, the Coverage Workspace grid, and the well page -- actually serve those dates
and actually agree with each other.

"Agree" is the point, and is asserted directly
----------------------------------------------
The owner asked for the dates on Home AND on the Coverage view. Two screens each
computing "the earliest date for the well" for themselves is two screens that will
eventually disagree, and the one a planner happens to be looking at decides what
they believe. So `test_home_and_grid_and_well_page_agree_on_every_date` compares
all three payloads field by field rather than checking each against a constant.
"""

from datetime import datetime, timedelta

from tests.phase5_fixtures import NOW, build_client


def _iso(value):
    """Normalise a JSON datetime for comparison across payloads."""
    return None if value is None else value.replace("Z", "+00:00")


def _grid_rows(client, **params):
    response = client.get("/coverage", params=params)
    assert response.status_code == 200, response.text
    return {row["well_name"]: row for row in response.json()["rows"]}


# ---------------------------------------------------------------------------
# The Home Dashboard's uncovered-wells card
# ---------------------------------------------------------------------------


def test_home_uncovered_wells_carry_both_dates():
    """The card was unactionable without them: a list of names says nothing about
    WHEN each well bites, so one needed next month looked like one needed in two
    years."""
    client, _sf, w = build_client()

    body = client.get("/dashboard/home").json()
    uncovered = body["uncovered_wells"]
    assert uncovered, "the corpus must contain an uncovered well for this to mean anything"

    for row in uncovered:
        assert "earliest_ros_date" in row
        assert "first_runout_date" in row
        # An UNCOVERED well by definition has an unsatisfied line, so both dates
        # must be present. A null runout date on an uncovered well would be a
        # contradiction between two things the same engine computed.
        assert row["earliest_ros_date"] is not None
        assert row["first_runout_date"] is not None


def test_home_uncovered_wells_are_sorted_by_earliest_ros_ascending():
    """Sorted SERVER-side, exactly as asked: "well should be shown in order of the
    earliest date for the well".

    Server-side rather than left to the client, so every consumer -- this card, the
    grid, and anything added later -- presents the same order. A client-side sort
    would be a fourth opinion.
    """
    client, session_factory, w = build_client()

    # WELL-2 is the corpus's uncovered well. Add a second uncovered well with an
    # EARLIER ROS, so a server that returned rows in insertion or name order would
    # visibly get this wrong.
    from app.engines.coverage import recompute_customer
    from app.models import (
        Customer,
        DemandLine,
        DemandProfile,
        DemandStatus,
        PlanningNode,
        Well,
    )

    db = session_factory()
    try:
        node = db.get(PlanningNode, w.alpha_id)
        early_well = Well(planning_node_id=node.id, name="AAA-EARLY", demand_status=DemandStatus.CONFIRMED)
        db.add(early_well)
        db.flush()
        db.add(
            DemandLine(
                well_id=early_well.id, product_id=w.p_b_id, quantity=9999,
                ros_date=NOW + timedelta(days=300),
                profile=DemandProfile.PRIMARY,
            )
        )
        db.flush()
        recompute_customer(db, db.get(Customer, w.acme_id))
        db.commit()
    finally:
        db.close()

    rows = client.get("/dashboard/home").json()["uncovered_wells"]
    dates = [_iso(row["earliest_ros_date"]) for row in rows]
    assert dates == sorted(dates), (
        f"uncovered wells are not sorted by earliest ROS ascending: {dates}"
    )
    assert len(rows) >= 2, "the test needs at least two uncovered wells to order"


def test_home_demand_changes_label_their_quantities():
    """The Demand Changes card shows a before/after quantity, so it shows a unit
    and names the product -- the same defect, on the same dashboard."""
    client, session_factory, w = build_client()

    from app.engines.coverage import apply_revision
    from app.models import DemandLine, DemandProfile, DemandStatus

    db = session_factory()
    try:
        line = db.get(DemandLine, w.l1_id)
        apply_revision(
            db, line, quantity=4000, ros_date=line.ros_date,
            profile=DemandProfile.PRIMARY,
        )
        db.commit()
    finally:
        db.close()

    changes = client.get("/dashboard/home").json()["demand_changes"]
    assert changes, "the revision should have produced an impact record"
    change = changes[0]
    assert change["quantity_before"] == 5000
    assert change["quantity_after"] == 4000
    assert change["unit_of_measure"] == "Mtr"
    assert change["product_description"] == w.p_a_desc


def test_home_pending_approvals_label_their_quantities():
    """Same rule on the Pending Approvals card."""
    client, _sf, _w = build_client()
    body = client.get("/dashboard/home").json()
    for row in body["pending_approvals"]:
        assert row["unit_of_measure"] == "Mtr"
        assert "quantity" in row
        assert row["product_description"]


# ---------------------------------------------------------------------------
# The Coverage Workspace grid
# ---------------------------------------------------------------------------


def test_grid_rows_carry_both_dates_and_null_them_for_an_unevaluated_well():
    """WELL-4 has demand but none in scope, so it has no dates -- and no rollup.

    The two nulls agreeing is the assertion: a date on a well the engine never
    evaluated would be a claim it never made.
    """
    client, _sf, _w = build_client()
    rows = _grid_rows(client)

    assert rows["WELL-1"]["earliest_ros_date"] is not None
    assert rows["WELL-1"]["first_runout_date"] is None  # Covered
    assert rows["WELL-2"]["first_runout_date"] is not None  # Uncovered

    assert rows["WELL-4"]["coverage_status"] is None
    assert rows["WELL-4"]["earliest_ros_date"] is None
    assert rows["WELL-4"]["first_runout_date"] is None


def test_grid_is_sorted_by_earliest_ros_ascending():
    """The same server-side order as Home, by the same helper."""
    client, _sf, _w = build_client()
    rows = list(client.get("/coverage").json()["rows"])

    dated = [_iso(r["earliest_ros_date"]) for r in rows if r["earliest_ros_date"]]
    assert dated == sorted(dated)
    # Wells with no date sort LAST, never first.
    undated_positions = [
        index for index, r in enumerate(rows) if r["earliest_ros_date"] is None
    ]
    if undated_positions:
        assert min(undated_positions) >= len(dated)


def test_grid_dates_follow_the_REQUESTED_filters_not_the_stored_defaults():
    """Under non-default filters the dates are recomputed with the verdicts.

    This is the mismatch that would otherwise be invisible: the grid recomputes
    verdicts read-only for the filters asked for, then rolls back. A caller computing
    dates AFTER that rollback would read the DEFAULT-filter verdicts and pair
    default-filter dates with projected rollups on the same row. So the dates are
    harvested inside the projection (`CoverageProjection.well_dates`), and this test
    is what pins that.

    WELL-4's only demand is Budgeted, so under the defaults it has no dates at all;
    widen the status filter to include Budgeted and it must acquire both a rollup and
    an earliest ROS in the SAME response.
    """
    client, _sf, _w = build_client()

    default_row = _grid_rows(client)["WELL-4"]
    assert default_row["earliest_ros_date"] is None

    body = client.get(
        "/coverage", params={"status": ["Confirmed", "Budgeted"], "profile": "Primary"}
    ).json()
    assert body["filters"]["recomputed_read_only"] is True
    widened = {row["well_name"]: row for row in body["rows"]}["WELL-4"]

    assert widened["evaluated"] is True
    assert widened["earliest_ros_date"] is not None, (
        "the grid served default-filter dates beside projected verdicts -- the dates "
        "must come from the same projection as the rollups"
    )
    assert widened["in_scope_line_count"] == 1


def test_a_projection_does_not_persist_its_dates_either():
    """The read-only guarantee still holds with the dates in the projection.

    A non-default request recomputes inside the caller's transaction and rolls back.
    Asserted here because `well_dates` is now called INSIDE that window, and a query
    issued there must not leave anything behind.
    """
    client, _sf, _w = build_client()

    client.get("/coverage", params={"status": ["Confirmed", "Budgeted"]})

    after = _grid_rows(client)
    assert after["WELL-4"]["coverage_status"] is None
    assert after["WELL-4"]["earliest_ros_date"] is None
    assert after["WELL-1"]["coverage_status"] == "Covered"


# ---------------------------------------------------------------------------
# The well page, and the agreement between all three
# ---------------------------------------------------------------------------


def test_well_page_carries_both_dates_and_labels_every_line():
    client, _sf, w = build_client()
    body = client.get(f"/wells/{w.w2_id}").json()

    assert body["earliest_ros_date"] is not None
    assert body["first_runout_date"] is not None  # WELL-2 is Uncovered
    assert body["demand_lines"], "WELL-2 has demand"
    for line in body["demand_lines"]:
        assert line["unit_of_measure"] == "Mtr"
        assert line["product_description"]
        assert line["product_description"] != line["product_id"]

    # Lines come back in ROS order, so the casing programme reads as a ladder.
    ros = [line["ros_date"] for line in body["demand_lines"]]
    assert ros == sorted(ros)


def test_home_and_grid_and_well_page_agree_on_every_date():
    """One definition, three screens, identical answers.

    Compared payload against payload rather than each against a constant: a constant
    would be satisfied by three screens that were all wrong in the same way, and the
    risk being guarded here is divergence.
    """
    client, _sf, _w = build_client()

    grid = _grid_rows(client)
    home = {row["name"]: row for row in client.get("/dashboard/home").json()["uncovered_wells"]}
    listing = {row["name"]: row for row in client.get("/wells").json()}

    for well_name, grid_row in grid.items():
        page = client.get(f"/wells/{grid_row['well_id']}").json()
        for field in ("earliest_ros_date", "first_runout_date"):
            assert _iso(page[field]) == _iso(grid_row[field]), (
                f"{well_name}: the well page and the coverage grid disagree on {field}"
            )
            assert _iso(listing[well_name][field]) == _iso(grid_row[field]), (
                f"{well_name}: GET /wells and the coverage grid disagree on {field}"
            )
            if well_name in home:
                assert _iso(home[well_name][field]) == _iso(grid_row[field]), (
                    f"{well_name}: the Home Dashboard and the coverage grid "
                    f"disagree on {field}"
                )


def test_wells_listing_is_sorted_and_filterable_by_rollup():
    """GET /wells is ordered by earliest ROS too, and can serve the uncovered set.

    The filter exists so the Home card and this route return the SAME rows in the
    SAME order rather than two subtly different lists.
    """
    client, _sf, _w = build_client()

    listing = client.get("/wells").json()
    dated = [_iso(r["earliest_ros_date"]) for r in listing if r["earliest_ros_date"]]
    assert dated == sorted(dated)

    uncovered = client.get("/wells", params={"coverage_status": "Uncovered"}).json()
    home = client.get("/dashboard/home").json()["uncovered_wells"]
    assert [r["name"] for r in uncovered] == [r["name"] for r in home]
