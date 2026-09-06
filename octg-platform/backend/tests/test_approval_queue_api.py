"""GET /substitution-approvals -- the approval queue.

Pins the list shape (newest first, unit beside every quantity), the status
filter, and the degrade path: an approval whose demand line has been deleted
must render with nulls and raw-id fallbacks, never 500 the queue.
"""

from datetime import datetime, timedelta

import pytest

from app.main import app
from app.models import (
    DemandLine,
    SubstitutionApprovalStatus,
    WellSubstitutionApproval,
)
from tests.phase5_fixtures import build_client


@pytest.fixture()
def client_world():
    client, session_factory, world = build_client()
    try:
        yield client, session_factory, world
    finally:
        app.dependency_overrides.clear()


def _seed_approvals(session_factory):
    db = session_factory()
    try:
        lines = db.query(DemandLine).order_by(DemandLine.id).limit(2).all()
        products = sorted({l.product_id for l in db.query(DemandLine).all()})
        older = WellSubstitutionApproval(
            demand_line_id=lines[0].id,
            from_product_id=lines[0].product_id,
            to_product_id=products[-1],
            status=SubstitutionApprovalStatus.PENDING,
            requested_at=datetime.utcnow() - timedelta(days=2),
        )
        newer = WellSubstitutionApproval(
            demand_line_id=lines[1].id,
            from_product_id=lines[1].product_id,
            to_product_id=products[-1],
            status=SubstitutionApprovalStatus.APPROVED,
            requested_at=datetime.utcnow() - timedelta(days=1),
            decided_at=datetime.utcnow(),
        )
        db.add_all([older, newer])
        db.commit()
        return older.id, newer.id
    finally:
        db.close()


def test_queue_is_newest_first_with_units(client_world):
    client, session_factory, _w = client_world
    older_id, newer_id = _seed_approvals(session_factory)

    rows = client.get("/substitution-approvals").json()
    assert [r["approval_id"] for r in rows] == [newer_id, older_id]
    for r in rows:
        # Unit beside every quantity -- the platform-wide contract.
        assert (r["quantity"] is None) == (r["unit_of_measure"] is None)
        assert r["quantity"] is not None
        assert r["well_name"] is not None
        assert r["from_product_description"]
        assert r["to_product_description"]


def test_queue_status_filter(client_world):
    client, session_factory, _w = client_world
    older_id, newer_id = _seed_approvals(session_factory)

    pending = client.get(
        "/substitution-approvals", params={"status": "Pending"}
    ).json()
    assert [r["approval_id"] for r in pending] == [older_id]
    approved = client.get(
        "/substitution-approvals", params={"status": "Approved"}
    ).json()
    assert [r["approval_id"] for r in approved] == [newer_id]


def test_a_demand_line_with_an_open_approval_cannot_be_deleted(client_world):
    """An approval can no longer outlive its line, because the line cannot go.

    This test used to build an orphan by hand -- delete the line, keep the
    approval -- and assert the queue rendered it with nulls instead of 500ing.
    Since foreign keys are enforced on every connection (F10, 2026-09-06), that
    state cannot be constructed at all: the database refuses the delete while
    the approval still points at the line. The queue's null-tolerant rendering
    stays in place as belt-and-braces, but the guarantee is now upstream.
    """
    from sqlalchemy.exc import IntegrityError

    client, session_factory, _w = client_world
    older_id, _newer_id = _seed_approvals(session_factory)

    db = session_factory()
    try:
        approval = db.get(WellSubstitutionApproval, older_id)
        line = db.get(DemandLine, approval.demand_line_id)
        from app.models import CoverageResult, DemandRevision, ImpactRecord

        cr = db.get(CoverageResult, line.id)
        if cr is not None:
            db.delete(cr)
        for rev in db.query(DemandRevision).filter_by(demand_line_id=line.id):
            db.delete(rev)
        for imp in db.query(ImpactRecord).filter_by(demand_line_id=line.id):
            db.delete(imp)
        db.delete(line)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()

    resp = client.get("/substitution-approvals")
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["approval_id"] == older_id)
    assert row["well_id"] is not None, "the line survived, so the queue is whole"


def test_deciding_a_decided_approval_is_409(client_world):
    """A decision is final (2026-08-12 decision): re-deciding is refused.

    Coverage was already recomputed on top of the first verdict, so a silent
    re-decision would rewrite history under it. Reversal = raise a NEW
    approval request; the 409 body says so.
    """
    client, session_factory, _w = client_world
    pending_id, decided_id = _seed_approvals(session_factory)

    resp = client.post(
        f"/substitution-approvals/{decided_id}/decision",
        json={"approved": False},
    )
    assert resp.status_code == 409
    assert "already decided" in resp.json()["detail"]
    assert "new approval request" in resp.json()["detail"]

    # The stored verdict is untouched.
    db = session_factory()
    try:
        row = db.get(WellSubstitutionApproval, decided_id)
        assert row.status == SubstitutionApprovalStatus.APPROVED
    finally:
        db.close()

    # A pending one still decides normally.
    resp = client.post(
        f"/substitution-approvals/{pending_id}/decision",
        json={"approved": True},
    )
    assert resp.status_code == 200
