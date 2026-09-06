"""F09 -- the actor is recorded SERVER-SIDE from the authenticated user.

Before this, "who did it" was whatever text the request body carried
(`created_by`, `approved_by`) or nothing at all (substitution decisions, inline
inventory edits). Now every one of those writes records the authenticated user's
id and the free text is kept only as "on behalf of". A body that names someone
else changes the text and NOT the recorded actor.
"""

from datetime import timedelta

from app.models import (
    CompanyInventoryEdit,
    DemandImportRow,
    DemandLine,
    InventoryOnHand,
    Scenario,
    UserRole,
    WellSubstitutionApproval,
)
from tests.phase5_fixtures import NOW
from tests.test_auth import _auth, _mk_user, client_world, issue_token, real_auth  # noqa: F401
from tests.test_demand_import_conflicts import _approve, _decide, _stage

ROS = (NOW + timedelta(days=30)).date().isoformat()


def _as_admin(client, session_factory):
    uid = _mk_user(session_factory, email="actor@test", display_name="Real Actor", role=UserRole.ADMIN)
    client.headers.update(_auth(issue_token(uid)))
    return uid


def test_scenario_records_the_authenticated_creator_not_the_body(client_world):
    client, sf, w = client_world
    uid = _as_admin(client, sf)

    r = client.post(
        "/scenarios",
        json={"name": "S", "customer_id": w.acme_id, "created_by": "Somebody Else"},
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["created_by"] == "Somebody Else"  # on-behalf text, kept as typed
    assert body["created_by_user_id"] == uid
    assert body["created_by_user_name"] == "Real Actor"
    with sf() as db:
        assert db.get(Scenario, body["id"]).created_by_user_id == uid


def test_substitution_request_and_decision_record_the_actor(client_world):
    client, sf, w = client_world
    uid = _as_admin(client, sf)

    r = client.post(
        f"/demand-lines/{w.l2_id}/substitution-approvals",
        json={"from_product_id": w.p_b_id, "to_product_id": w.p_a_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["requested_by_user_id"] == uid
    assert r.json()["decided_by_user_id"] is None

    r = client.post(
        f"/substitution-approvals/{r.json()['id']}/decision", json={"approved": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["decided_by_user_id"] == uid
    assert r.json()["decided_by_user_name"] == "Real Actor"
    with sf() as db:
        row = db.get(WellSubstitutionApproval, r.json()["id"])
        assert (row.requested_by_user_id, row.decided_by_user_id) == (uid, uid)


def test_import_override_approval_records_the_actor_and_keeps_the_text(client_world):
    client, sf, w = client_world
    uid = _as_admin(client, sf)
    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Budgeted", "Primary"]])
    row_id = batch["rows"][0]["id"]
    _decide(client, batch["id"], row_id, "AcceptRevision")

    approved = _approve(client, batch["id"], row_id, by="Tanaka").json()
    assert approved["override_approved_by"] == "Tanaka"
    assert approved["override_approved_by_user_id"] == uid
    assert approved["override_approved_by_user_name"] == "Real Actor"

    withdrawn = _approve(client, batch["id"], row_id, approved=False, by=None).json()
    assert withdrawn["override_approved_by_user_id"] is None
    with sf() as db:
        assert db.get(DemandImportRow, row_id).override_approved_by_user_id is None


def test_inline_inventory_edits_are_logged_with_the_actor(client_world):
    client, sf, w = client_world
    uid = _as_admin(client, sf)
    with sf() as db:
        row = (
            db.query(InventoryOnHand)
            .filter(InventoryOnHand.product_id == w.p_a_id)
            .one()
        )
        row.source_system = "manual"
        db.commit()
        row_id = row.id

    r = client.patch(f"/company-inventory/on-hand/{row_id}", json={"quantity": 6100})
    assert r.status_code == 200, r.text
    r = client.delete(f"/company-inventory/on-hand/{row_id}")
    assert r.status_code == 200, r.text

    with sf() as db:
        edits = (
            db.query(CompanyInventoryEdit)
            .filter(CompanyInventoryEdit.row_id == row_id)
            .order_by(CompanyInventoryEdit.edited_at)
            .all()
        )
        assert [e.action for e in edits] == ["set", "delete"]
        assert {e.user_id for e in edits} == {uid}
        assert all(e.row_kind == "InventoryOnHand" for e in edits)
        assert '"quantity": 6000' in edits[0].before_json
        assert '"quantity": 6100' in edits[0].after_json
        assert edits[1].after_json is None
        assert edits[0].user.display_name == "Real Actor"


def test_scenario_apply_attributes_the_approvals_it_decides(client_world):
    client, sf, w = client_world
    uid = _as_admin(client, sf)
    r = client.post("/scenarios", json={"name": "S", "customer_id": w.acme_id})
    sid = r.json()["id"]
    r = client.post(
        f"/scenarios/{sid}/overrides",
        json={
            "target_kind": "SubstitutionApproval",
            "target_demand_line_id": w.l2_id,
            "target_to_product_id": w.p_a_id,
            "field_name": "approval_status",
            "value_text": "Approved",
        },
    )
    assert r.status_code in (200, 201), r.text
    r = client.post(f"/scenarios/{sid}/apply")
    assert r.status_code == 200, r.text
    with sf() as db:
        rows = db.query(WellSubstitutionApproval).all()
        assert len(rows) == 1
        assert (rows[0].requested_by_user_id, rows[0].decided_by_user_id) == (uid, uid)
