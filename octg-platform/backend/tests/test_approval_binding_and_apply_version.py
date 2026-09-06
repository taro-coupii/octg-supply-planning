"""F07 -- an import override approval is bound to what it approved.
F08 -- a scenario apply must name the version it previewed.
"""

from datetime import timedelta

from app.models import DemandStatus, Scenario, Well
from tests.phase5_fixtures import NOW, build_client
from tests.test_demand_import_conflicts import (
    _approve,
    _decide,
    _revise_out_of_band,
    _stage,
    client_world,  # noqa: F401
)

ROS = (NOW + timedelta(days=30)).date().isoformat()


def test_well_status_change_after_approval_lapses_it(client_world):
    client, sf, w = client_world
    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Budgeted", "Primary"]])
    row_id = batch["rows"][0]["id"]
    _decide(client, batch["id"], row_id, "AcceptRevision")
    assert _approve(client, batch["id"], row_id).status_code == 200

    with sf() as db:
        db.get(Well, w.w1_id).demand_status = DemandStatus.PLANNED
        db.commit()

    row = client.get(f"/demand-imports/{batch['id']}").json()["rows"][0]
    assert row["override_approval_lapsed"] is True
    assert row["requires_override_approval"] is True
    assert client.post(f"/demand-imports/{batch['id']}/apply").status_code == 409


def test_an_approval_with_no_recorded_basis_is_lapsed(client_world):
    """A row approved before the basis column existed authorises nothing."""
    from app.models import DemandImportRow

    client, sf, w = client_world
    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Budgeted", "Primary"]])
    row_id = batch["rows"][0]["id"]
    _decide(client, batch["id"], row_id, "AcceptRevision")
    assert _approve(client, batch["id"], row_id).status_code == 200
    with sf() as db:
        db.get(DemandImportRow, row_id).override_approval_basis = None
        db.commit()
    row = client.get(f"/demand-imports/{batch['id']}").json()["rows"][0]
    assert row["override_approval_lapsed"] is True
    assert client.post(f"/demand-imports/{batch['id']}/apply").status_code == 409


def test_withdrawing_clears_the_basis(client_world):
    from app.models import DemandImportRow

    client, sf, w = client_world
    batch = _stage(client, [["WELL-1", w.p_a_desc, 6500, ROS, "Budgeted", "Primary"]])
    row_id = batch["rows"][0]["id"]
    _decide(client, batch["id"], row_id, "AcceptRevision")
    _approve(client, batch["id"], row_id)
    _approve(client, batch["id"], row_id, approved=False, by=None)
    with sf() as db:
        assert db.get(DemandImportRow, row_id).override_approval_basis is None


def _scenario_with_override(client, w):
    sid = client.post("/scenarios", json={"name": "V", "customer_id": w.acme_id}).json()["id"]
    r = client.post(
        f"/scenarios/{sid}/overrides",
        json={
            "target_kind": "DemandLine",
            "target_demand_line_id": w.l1_id,
            "field_name": "quantity",
            "value_number": 4000,
        },
    )
    assert r.status_code in (200, 201), r.text
    return sid


def test_apply_refuses_a_stale_version_and_writes_nothing():
    client, sf, w = build_client()
    sid = _scenario_with_override(client, w)
    detail = client.get(f"/scenarios/{sid}").json()
    assert detail["version"] == 2  # created (1) + one override
    preview = client.get(f"/scenarios/{sid}/preview").json()
    assert preview["scenario_version"] == 2

    # The scenario moves after the preview: another override.
    client.post(
        f"/scenarios/{sid}/overrides",
        json={
            "target_kind": "DemandLine",
            "target_demand_line_id": w.l1_id,
            "field_name": "quantity",
            "value_number": 3000,
        },
    )
    r = client.post(f"/scenarios/{sid}/apply", json={"expected_version": 2})
    assert r.status_code == 409
    assert "changed since" in r.json()["detail"]
    with sf() as db:
        assert db.get(Scenario, sid).status.value == "Draft"
        from app.models import DemandLine
        assert db.get(DemandLine, w.l1_id).quantity == 5000

    # Body is required: no version, no apply.
    assert client.post(f"/scenarios/{sid}/apply").status_code == 422

    fresh = client.get(f"/scenarios/{sid}/preview").json()["scenario_version"]
    r = client.post(f"/scenarios/{sid}/apply", json={"expected_version": fresh})
    assert r.status_code == 200, r.text


def test_metadata_and_override_removal_bump_the_version():
    client, sf, w = build_client()
    sid = _scenario_with_override(client, w)
    v = client.get(f"/scenarios/{sid}").json()["version"]
    client.patch(f"/scenarios/{sid}", json={"name": "renamed"})
    assert client.get(f"/scenarios/{sid}").json()["version"] == v + 1
    oid = client.get(f"/scenarios/{sid}").json()["overrides"][0]["id"]
    client.delete(f"/scenarios/{sid}/overrides/{oid}")
    assert client.get(f"/scenarios/{sid}").json()["version"] == v + 2
