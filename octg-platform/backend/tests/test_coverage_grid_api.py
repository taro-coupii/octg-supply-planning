"""GET /coverage -- the cross-well Coverage Workspace grid.

The interesting half of this file is the non-default-filter behaviour. The design
decision under test: a request with filters other than the platform defaults is
answered by a READ-ONLY recompute for exactly those filters, is labelled as a
projection, and persists nothing. The alternative -- serving stored default-filter
rows under a non-default label -- is what these tests exist to make impossible.
"""

import pytest

from app.main import app
from app.models import CoverageResult, Well
from tests.phase5_fixtures import build_client


@pytest.fixture()
def client_world():
    client, session_factory, world = build_client()
    try:
        yield client, session_factory, world
    finally:
        app.dependency_overrides.clear()


def _grid(client, **params):
    resp = client.get("/coverage", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_name(body):
    return {row["well_name"]: row for row in body["rows"]}


# --------------------------------------------------------------------------
# Default filters -- the official stored verdicts
# --------------------------------------------------------------------------


def test_default_filters_serve_stored_verdicts_and_say_so(client_world):
    client, _sf, _w = client_world
    body = _grid(client)
    assert body["filters"]["are_default"] is True
    assert body["filters"]["recomputed_read_only"] is False
    # The platform defaults as the API PUBLISHES them, sorted. Repurposed to the
    # owner's new scope (Confirmed only; Primary + Contingency). The claim is
    # unchanged -- "an unfiltered request reports the defaults it actually used" --
    # only the values moved, and they moved because the specification did.
    assert body["filters"]["status"] == ["Confirmed"]
    assert body["filters"]["profile"] == ["Contingency", "Primary"]
    # FIVE wells now: WELL-5 was added to carry the Planned line that used to sit
    # inside WELL-1. See tests/phase5_fixtures.
    assert body["well_count"] == 5


def test_counts_and_rollups(client_world):
    client, _sf, _w = client_world
    rows = _by_name(_grid(client))

    # WELL-1: one in-scope line, covered. (Its Planned companion line lives on
    # WELL-5 now -- status is a well-level fact.)
    assert rows["WELL-1"]["in_scope_line_count"] == 1
    assert rows["WELL-1"]["covered_line_count"] == 1
    assert rows["WELL-1"]["coverage_status"] == "Covered"
    assert rows["WELL-1"]["evaluated"] is True

    # WELL-2: two in-scope lines, neither covered (Uncovered + Unrecoverable).
    assert rows["WELL-2"]["in_scope_line_count"] == 2
    assert rows["WELL-2"]["covered_line_count"] == 0
    assert rows["WELL-2"]["coverage_status"] == "Uncovered"

    assert rows["WELL-3"]["in_scope_line_count"] == 1
    assert rows["WELL-3"]["coverage_status"] == "Covered"

    # WELL-4 (Budgeted) and WELL-5 (Planned) both have demand and neither is in
    # scope, because the STATUS FILTER SELECTS WELLS. Nothing in scope, so the
    # engine reached no verdict: not covered, not uncovered -- unevaluated.
    for name in ("WELL-4", "WELL-5"):
        assert rows[name]["in_scope_line_count"] == 0
        assert rows[name]["covered_line_count"] == 0
        assert rows[name]["coverage_status"] is None
        assert rows[name]["evaluated"] is False


def test_planning_node_path_and_customer(client_world):
    client, _sf, w = client_world
    rows = _by_name(_grid(client))
    assert rows["WELL-1"]["planning_node_path"] == "Project Alpha / Pad A"
    assert rows["WELL-1"]["customer_name"] == "Acme"
    assert rows["WELL-1"]["customer_id"] == w.acme_id
    assert rows["WELL-2"]["planning_node_path"] == "Project Alpha"
    assert rows["WELL-3"]["customer_name"] == "Beta"


def test_filter_by_customer(client_world):
    client, _sf, w = client_world
    body = _grid(client, customer_id=w.beta_id)
    assert [r["well_name"] for r in body["rows"]] == ["WELL-3"]
    assert body["well_count"] == 1


def test_filter_by_rollup_coverage_status(client_world):
    client, _sf, _w = client_world
    assert {r["well_name"] for r in _grid(client, coverage_status="Covered")["rows"]} == {
        "WELL-1",
        "WELL-3",
    }
    assert {
        r["well_name"] for r in _grid(client, coverage_status="Uncovered")["rows"]
    } == {"WELL-2"}
    assert {
        r["well_name"] for r in _grid(client, coverage_status="Unevaluated")["rows"]
    } == {"WELL-4", "WELL-5"}


def test_unknown_rollup_filter_value_is_a_400(client_world):
    client, _sf, _w = client_world
    resp = client.get("/coverage", params={"coverage_status": "PendingApproval"})
    assert resp.status_code == 400
    assert "well rollup" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Non-default filters -- recomputed read-only, labelled, and not persisted
# --------------------------------------------------------------------------


def test_non_default_filters_change_the_answer_not_just_the_view(client_world):
    """Widening the status filter to include Planned pulls the WHOLE of WELL-5 into
    the P-A pool. L3's ROS (+10d) beats L1's (+30d), so under earliest-ROS-first L3
    takes 2000 of the 6000 on hand and WELL-1's 5000-unit line is left 4000 --
    WELL-1 flips from Covered to Uncovered.

    This is the whole reason the endpoint recomputes: the stored rows would have
    reported WELL-1 as Covered and WELL-5 as unevaluated with an in-scope count of
    0, which is not the answer to the question that was asked.

    REPURPOSED, not weakened. The claim is identical -- "a filter change moves a
    well that was in scope all along, so a projection cannot be served from stored
    rows" -- and WELL-1 still flips Covered -> Uncovered. What changed is that the
    line doing the starving lives on its own well now, because demand status is a
    property of the WELL. MORE is asserted than before: every well's counts are
    pinned, and the newly-in-scope well's arrival is pinned as a WHOLE WELL (all of
    its lines at once), which is the readability the change was made for.
    """
    client, _sf, _w = client_world

    default_rows = _by_name(_grid(client))
    assert default_rows["WELL-1"]["coverage_status"] == "Covered"
    assert default_rows["WELL-5"]["evaluated"] is False
    assert default_rows["WELL-5"]["in_scope_line_count"] == 0

    body = _grid(
        client,
        status=["Confirmed", "Budgeted", "Planned"],
        profile=["Primary"],
    )
    assert body["filters"]["are_default"] is False
    assert body["filters"]["recomputed_read_only"] is True
    assert "PROJECTION" in body["filters"]["explanation"]

    rows = _by_name(body)
    # WELL-1 was in scope under BOTH filter sets and its verdict still moved.
    assert rows["WELL-1"]["in_scope_line_count"] == 1
    assert rows["WELL-1"]["covered_line_count"] == 0
    assert rows["WELL-1"]["coverage_status"] == "Uncovered"
    # WELL-5 arrived whole: its one line, evaluated, and it won the race.
    assert rows["WELL-5"]["in_scope_line_count"] == 1
    assert rows["WELL-5"]["covered_line_count"] == 1
    assert rows["WELL-5"]["coverage_status"] == "Covered"
    # WELL-4 arrived whole too, and got nothing -- the pool was gone.
    assert rows["WELL-4"]["in_scope_line_count"] == 1
    assert rows["WELL-4"]["coverage_status"] == "Uncovered"


def test_non_default_filters_persist_nothing(client_world):
    """The projection must not become the official verdict."""
    client, session_factory, w = client_world

    db = session_factory()
    try:
        before_rows = {
            cr.demand_line_id: cr.status.value
            for cr in db.query(CoverageResult).all()
        }
        before_wells = {
            well.id: well.coverage_status for well in db.query(Well).all()
        }
    finally:
        db.close()

    _grid(client, status=["Confirmed", "Budgeted", "Planned"], profile=["Primary"])

    db = session_factory()
    try:
        after_rows = {
            cr.demand_line_id: cr.status.value
            for cr in db.query(CoverageResult).all()
        }
        after_wells = {well.id: well.coverage_status for well in db.query(Well).all()}
        # In particular: no row was created for the Planned line the projection
        # evaluated, and WELL-1 is still officially Covered.
        assert w.l3_id not in after_rows
        assert after_rows == before_rows
        assert after_wells == before_wells
        assert after_wells[w.w1_id] == "Covered"
    finally:
        db.close()


def test_budgeted_status_toggle_brings_well_4_into_scope(client_world):
    """WELL-4's only demand is Budgeted/Primary. Under the defaults it is
    unevaluated; toggling Budgeted on gives it a real verdict.

    REPURPOSED to the status toggle. The behaviour under test is identical -- a
    toggle that widens scope turns an UNEVALUATED well into an evaluated one, which
    is the whole reason the grid must recompute rather than serve stored rows. Only
    the toggle changed, because Contingency is now IN the defaults (so toggling it
    widens nothing) while Budgeted is now OUT of them.
    """
    client, _sf, _w = client_world
    body = _grid(client, status=["Confirmed", "Budgeted"], profile=["Primary"])
    assert body["filters"]["recomputed_read_only"] is True
    rows = _by_name(body)
    assert rows["WELL-4"]["in_scope_line_count"] == 1
    assert rows["WELL-4"]["evaluated"] is True
    assert rows["WELL-4"]["coverage_status"] in ("Covered", "Uncovered")


def test_explicitly_passing_the_defaults_is_not_treated_as_non_default(client_world):
    """Sending the default filters explicitly must take the stored-rows fast path
    -- otherwise the Coverage Workspace would recompute on every page load simply
    because its toggles are populated."""
    client, _sf, _w = client_world
    body = _grid(
        client, status=["Confirmed"], profile=["Primary", "Contingency"]
    )
    assert body["filters"]["are_default"] is True
    assert body["filters"]["recomputed_read_only"] is False
    assert _by_name(body)["WELL-1"]["coverage_status"] == "Covered"


def test_projection_leaves_the_session_usable(client_world):
    """A recompute-and-rollback must not poison the request session for the next
    call -- including a write."""
    client, _sf, w = client_world
    _grid(client, status=["Planned"], profile=["Primary"])

    body = _grid(client)
    assert _by_name(body)["WELL-1"]["coverage_status"] == "Covered"

    resp = client.get("/demand-lines")
    assert resp.status_code == 200
    resp = client.get(f"/wells/{w.w1_id}")
    assert resp.status_code == 200
    assert resp.json()["coverage_status"] == "Covered"


def test_grid_row_carries_the_wells_demand_status_so_an_unevaluated_row_explains_itself(
    client_world,
):
    """An unevaluated row must name its CAUSE, not just its consequence.

    The status filter selects whole wells, so the commonest reason a row has no
    verdict is that its demand status sits outside the requested scope. Serving
    `coverage_status: null` without the demand status states the consequence and
    withholds the cause, leaving a planner unable to tell "this well is out of
    scope" from "the engine failed on this well" -- two situations with completely
    different responses.
    """
    client, _sf, _w = client_world
    rows = _grid(client)["rows"]
    assert rows, "fixture produced no wells"

    # Present on every row, evaluated or not -- it is an input, so it exists
    # regardless of whether a verdict was reached.
    assert all(row["demand_status"] for row in rows)

    unevaluated = [row for row in rows if not row["evaluated"]]
    assert unevaluated, "fixture no longer contains an out-of-scope well"
    for row in unevaluated:
        assert row["coverage_status"] is None
        # The cause, stated: not the status the default filter admits.
        assert row["demand_status"] != "Confirmed"

    # And the complement, so the assertion above cannot pass by the field simply
    # never being "Confirmed".
    evaluated = [row for row in rows if row["evaluated"]]
    assert evaluated
    assert all(row["demand_status"] == "Confirmed" for row in evaluated)
