"""Per-request demand scope (status / profile) on Executive and Surplus.

Both endpoints accept `status` / `profile` query lists. The default serves the
stored official verdicts; anything else runs the whole computation inside
`app.engines.coverage_view.scoped_verdicts` -- a read-only recompute under the
requested scope that is rolled back afterwards. These tests pin the three
things that make that honest:

  1. the payload NAMES the scope it was computed under (`status_scope`,
     `profile_scope`, `scope_is_default`);
  2. widening the scope actually widens the figures (the recompute happened);
  3. the stored rows survive untouched (the rollback happened) -- a follow-up
     default request answers exactly as before.
"""

import pytest

from app.main import app
from app.models import CoverageResult
from tests.phase5_fixtures import build_client


@pytest.fixture()
def client_world():
    client, session_factory, world = build_client()
    try:
        yield client, session_factory, world
    finally:
        app.dependency_overrides.clear()


def _stored_verdicts(session_factory):
    db = session_factory()
    try:
        return sorted(
            (r.demand_line_id, r.status.value)
            for r in db.query(CoverageResult).all()
        )
    finally:
        db.close()


def test_executive_default_scope_is_labelled_default(client_world):
    client, _sf, _w = client_world
    body = client.get("/dashboard/executive").json()
    assert body["scope_is_default"] is True
    assert body["status_scope"] == ["Confirmed"]
    assert body["profile_scope"] == ["Contingency", "Primary"]


def test_executive_widened_scope_recomputes_and_rolls_back(client_world):
    client, session_factory, _w = client_world
    before = _stored_verdicts(session_factory)

    default = client.get("/dashboard/executive").json()
    widened = client.get(
        "/dashboard/executive",
        params={"status": ["Confirmed", "Budgeted", "Planned"]},
    ).json()

    assert widened["scope_is_default"] is False
    assert widened["status_scope"] == ["Budgeted", "Confirmed", "Planned"]

    # The Budgeted and Planned wells' demand joins the book, so the coverage
    # denominator grows -- proof the blocks were recomputed under the request,
    # not served from the Confirmed-only stored rows.
    assert (
        widened["coverage"]["total_quantity"]
        > default["coverage"]["total_quantity"]
    )

    # ...and nothing persisted: the stored official verdicts are untouched and
    # a follow-up default request answers exactly as before.
    assert _stored_verdicts(session_factory) == before
    again = client.get("/dashboard/executive").json()
    assert again["coverage"] == default["coverage"]


def test_executive_explicit_default_uses_stored_rows(client_world):
    client, _sf, _w = client_world
    body = client.get(
        "/dashboard/executive",
        params={"status": ["Confirmed"], "profile": ["Primary", "Contingency"]},
    ).json()
    assert body["scope_is_default"] is True


def test_surplus_scope_is_variable_and_labelled(client_world):
    client, session_factory, _w = client_world
    before = _stored_verdicts(session_factory)

    default = client.get("/analysis/surplus").json()
    assert default["scope_is_default"] is True

    widened = client.get(
        "/analysis/surplus",
        params={"status": ["Confirmed", "Budgeted", "Planned"]},
    ).json()
    assert widened["scope_is_default"] is False
    assert widened["status_scope"] == ["Budgeted", "Confirmed", "Planned"]

    # Widening the demand scope can only pull more stock into "allocated" --
    # total surplus+obsolete never grows.
    def idle(body):
        return sum(r["surplus"] + r["obsolete"] for r in body["rows"])

    assert idle(widened) <= idle(default)
    # The identity allocated + surplus + obsolete == on_hand holds per row
    # under ANY scope.
    for row in widened["rows"]:
        assert row["allocated"] + row["surplus"] + row["obsolete"] == pytest.approx(
            row["on_hand"]
        )

    assert _stored_verdicts(session_factory) == before


def test_unmapped_customer_no_longer_breaks_the_grid(client_world):
    """C-07: one customer without a Business Unit used to 409 the whole
    coverage grid for everyone on the non-default (recompute) path. It is now
    skipped and NAMED instead."""
    client, session_factory, _w = client_world
    db = session_factory()
    try:
        from app.models import Customer, PlanningNode, Well, DemandLine, Product
        from app.models import DemandStatus, DemandProfile
        from datetime import datetime, timedelta

        orphan = Customer(name="Orphan Oil", business_unit_id=None)
        db.add(orphan)
        db.flush()
        node = PlanningNode(
            customer_id=orphan.id, parent_id=None, node_type="Asset",
            name="Orphan Asset",
        )
        db.add(node)
        db.flush()
        well = Well(
            planning_node_id=node.id, name="ORPHAN-1",
            demand_status=DemandStatus.CONFIRMED,
        )
        db.add(well)
        db.flush()
        product = db.query(Product).first()
        db.add(DemandLine(
            well_id=well.id, product_id=product.id, quantity=100.0,
            ros_date=datetime.utcnow() + timedelta(days=30),
            profile=DemandProfile.PRIMARY,
        ))
        db.commit()
    finally:
        db.close()

    resp = client.get(
        "/coverage", params={"status": ["Confirmed", "Budgeted"]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filters"]["skipped_customers"] == ["Orphan Oil"]
    # C-08: the grid states how old its verdicts are.
    assert body["verdicts_computed_from"] is not None
    assert body["verdicts_computed_to"] is not None
