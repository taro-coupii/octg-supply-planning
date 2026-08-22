"""GET /demand-lines -- the flat Demand List.

The load-bearing assertion in this file is
`test_out_of_scope_line_is_listed_with_no_coverage_status`. Everything else is
filtering and pagination; that one is the correctness rule the wells API already
enforces, and a regression there is the reported critical defect (stale
CoverageResult rows presented as current) reappearing on a new screen.
"""

from datetime import timedelta

import pytest
from sqlalchemy import event

from app.main import app
from app.models import (
    CoverageResult,
    CoverageStatus,
    DemandLine,
    DemandProfile,
    DemandStatus,
)
from tests.phase5_fixtures import NOW, build_client


@pytest.fixture()
def client_world():
    client, session_factory, world = build_client()
    try:
        yield client, session_factory, world
    finally:
        app.dependency_overrides.clear()


def _rows(client, **params):
    resp = client.get("/demand-lines", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_lists_every_line_with_denormalised_context(client_world):
    client, _sf, w = client_world
    body = _rows(client)
    assert body["total"] == 6
    assert body["returned"] == 6
    by_id = {r["id"]: r for r in body["rows"]}

    l1 = by_id[w.l1_id]
    assert l1["well_name"] == "WELL-1"
    # Full planning-node breadcrumb, not just the leaf.
    assert l1["planning_node_path"] == "Project Alpha / Pad A"
    assert l1["customer_id"] == w.acme_id
    assert l1["product_description"] == w.p_a_desc
    assert l1["quantity"] == 5000
    assert l1["status"] == "Confirmed"
    assert l1["profile"] == "Primary"
    assert l1["current_revision_no"] == 1
    assert l1["evaluated"] is True
    assert l1["coverage_status"] == "Covered"


def test_ordered_by_ros_date(client_world):
    client, _sf, _w = client_world
    dates = [r["ros_date"] for r in _rows(client)["rows"]]
    assert dates == sorted(dates)


# --------------------------------------------------------------------------
# The correctness rule: no coverage verdict for a line the engine does not
# evaluate. Same rule as app.api.wells._well_out.
# --------------------------------------------------------------------------


def test_out_of_scope_line_is_listed_with_no_coverage_status(client_world):
    client, _sf, w = client_world
    by_id = {r["id"]: r for r in _rows(client)["rows"]}

    planned = by_id[w.l3_id]  # on WELL-5, which is Planned
    assert planned["status"] == "Planned"
    assert planned["evaluated"] is False
    assert planned["coverage_status"] is None
    assert planned["coverage_reason"] is None

    # A SECOND out-of-scope well, at a DIFFERENT status, so the rule is shown not to
    # hinge on one status value.
    budgeted = by_id[w.l6_id]  # on WELL-4, which is Budgeted
    assert budgeted["status"] == "Budgeted"
    assert budgeted["evaluated"] is False
    assert budgeted["coverage_status"] is None
    assert budgeted["coverage_reason"] is None


def test_a_contingency_line_IS_evaluated_and_carries_a_verdict(client_world):
    """The complement of the test above, and new.

    Contingency is in the default profile scope now, so a Confirmed/Contingency line
    must come back EVALUATED with a real verdict. Without this, an implementation
    that withheld coverage from every non-Primary line would still satisfy every
    other assertion in this module -- which is exactly the bug the owner's change
    was made to remove.
    """
    client, session_factory, w = client_world

    db = session_factory()
    try:
        line = db.get(DemandLine, w.l4_id)  # WELL-3, Covered, Beta's only line
        line.profile = DemandProfile.CONTINGENCY
        db.commit()
        from app.engines.coverage import recompute_well
        recompute_well(db, line.well)
        db.commit()
    finally:
        db.close()

    row = next(r for r in _rows(client)["rows"] if r["id"] == w.l4_id)
    assert row["profile"] == "Contingency"
    assert row["evaluated"] is True
    assert row["coverage_status"] == "Covered"


def test_surviving_stale_coverage_row_is_never_served(client_world):
    """Belt-and-braces, mirroring test_wells_api: a CoverageResult row written by
    an older build can outlive its scope, and must not be presented as current."""
    client, session_factory, w = client_world

    db = session_factory()
    try:
        # Move a Covered line out of scope WITHOUT the engine, leaving its row.
        #
        # The move is now made on the WELL (`Well.demand_status`), because that is
        # where demand status lives -- and it is assigned DIRECTLY rather than through
        # `set_well_demand_status`, which is the whole point: bypassing the engine is
        # exactly what an older build (or a hand-edited row) looks like, and it stages
        # the identical situation this test is about -- a CoverageResult that has
        # outlived the scope of its line.
        line = db.get(DemandLine, w.l1_id)
        line.well.demand_status = DemandStatus.BUDGETED
        db.commit()
        assert db.get(CoverageResult, w.l1_id) is not None
    finally:
        db.close()

    row = next(r for r in _rows(client)["rows"] if r["id"] == w.l1_id)
    assert row["status"] == "Budgeted"
    assert row["evaluated"] is False
    assert row["coverage_status"] is None

    # And it cannot be reached via a coverage_status filter either -- an
    # out-of-scope line has no status, so it must match no status filter.
    ids = [r["id"] for r in _rows(client, coverage_status="Covered")["rows"]]
    assert w.l1_id not in ids


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def test_filter_by_customer(client_world):
    client, _sf, w = client_world
    body = _rows(client, customer_id=w.beta_id)
    assert [r["id"] for r in body["rows"]] == [w.l4_id]
    assert body["total"] == 1


def test_filter_by_well_and_product(client_world):
    client, _sf, w = client_world
    # WELL-1 carries L1 only: L3 moved to WELL-5 when demand status became a
    # property of the well (a Planned line cannot sit inside a Confirmed well).
    assert {r["id"] for r in _rows(client, well_id=w.w1_id)["rows"]} == {w.l1_id}
    assert {r["id"] for r in _rows(client, well_id=w.w5_id)["rows"]} == {w.l3_id}
    assert {r["id"] for r in _rows(client, product_id=w.p_b_id)["rows"]} == {
        w.l2_id,
        w.l5_id,
    }


def test_filter_by_status_and_profile(client_world):
    client, _sf, w = client_world
    assert {r["id"] for r in _rows(client, status="Planned")["rows"]} == {w.l3_id}
    assert {r["id"] for r in _rows(client, status="Budgeted")["rows"]} == {w.l6_id}
    # No Contingency demand in this corpus -- an empty result, asserted rather than
    # assumed, so the filter is shown to discriminate rather than to match everything.
    assert _rows(client, profile="Contingency")["rows"] == []
    # Repeated params are OR-ed within a field. Asserted as a UNION rather than as a
    # bare total, so the claim survives the corpus changing its status mix: the
    # OR-ed result must be exactly the two single-status results put together, and
    # strictly larger than either. A bare `== 6` happened to equal the whole corpus
    # and so could not tell OR from "no filter at all".
    planned = {r["id"] for r in _rows(client, status="Planned")["rows"]}
    confirmed = {r["id"] for r in _rows(client, status="Confirmed")["rows"]}
    both = {
        r["id"]
        for r in client.get(
            "/demand-lines?status=Planned&status=Confirmed"
        ).json()["rows"]
    }
    assert both == planned | confirmed
    assert len(both) > len(planned)
    assert len(both) > len(confirmed)
    # ...and it is NOT simply everything: the Budgeted line is excluded.
    assert w.l6_id not in both


def test_filter_by_coverage_status(client_world):
    client, _sf, w = client_world
    assert {r["id"] for r in _rows(client, coverage_status="Covered")["rows"]} == {
        w.l1_id,
        w.l4_id,
    }
    assert {r["id"] for r in _rows(client, coverage_status="Uncovered")["rows"]} == {
        w.l2_id
    }
    unrecoverable = _rows(client, coverage_status="Unrecoverable")["rows"]
    assert {r["id"] for r in unrecoverable} == {w.l5_id}
    assert "ROS cannot be met even if ordered today" in unrecoverable[0][
        "coverage_reason"
    ]


def test_filter_by_ros_range(client_world):
    client, _sf, w = client_world
    assert _rows(client, ros_from="2000-01-01", ros_to="2000-01-02")["rows"] == []

    # An upper bound 100 days out excludes the +200d and +400d lines.
    cutoff = (NOW + timedelta(days=100)).isoformat()
    assert {r["id"] for r in _rows(client, ros_to=cutoff)["rows"]} == {
        w.l1_id,
        w.l3_id,
        w.l5_id,
        w.l6_id,
    }
    # A lower bound at the same point keeps exactly those two.
    assert {r["id"] for r in _rows(client, ros_from=cutoff)["rows"]} == {
        w.l2_id,
        w.l4_id,
    }


def test_inverted_ros_range_is_a_400(client_world):
    client, _sf, _w = client_world
    resp = client.get(
        "/demand-lines", params={"ros_from": "2030-01-01", "ros_to": "2029-01-01"}
    )
    assert resp.status_code == 400


def test_pagination_reports_total_independently_of_the_page(client_world):
    client, _sf, _w = client_world
    page = _rows(client, limit=2, offset=2)
    assert page["total"] == 6
    assert page["returned"] == 2
    assert page["offset"] == 2
    first = _rows(client, limit=2, offset=0)
    assert {r["id"] for r in first["rows"]} & {r["id"] for r in page["rows"]} == set()


# --------------------------------------------------------------------------
# No N+1
# --------------------------------------------------------------------------


def test_no_n_plus_one_queries(client_world):
    """Serialising the list must not cost a query per row.

    Two statements are expected: one COUNT for `total`, one SELECT with the eager
    joins for the page. The bound is deliberately generous (<= 4) so the test
    pins the ORDER OF GROWTH rather than an exact plan -- what must never happen
    is the count scaling with the number of rows, which is what a lazy `well` /
    `product` / `coverage_result` load per row would do (6 rows x 3 relationships
    plus the planning node = ~24 extra statements here).
    """
    client, session_factory, _w = client_world
    counted: list[str] = []

    engine = session_factory.kw["bind"]

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counted.append(statement)

    try:
        body = _rows(client)
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert body["returned"] == 6
    assert len(counted) <= 4, "".join(f"\n---\n{s}" for s in counted)


def test_coverage_statuses_present_are_only_engine_written_values(client_world):
    client, _sf, _w = client_world
    served = {
        r["coverage_status"] for r in _rows(client)["rows"] if r["coverage_status"]
    }
    assert served <= {s.value for s in CoverageStatus}
