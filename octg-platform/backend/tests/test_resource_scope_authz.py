"""A planner cannot reach another Business Unit's resources by ANY spelling.

Adversarial review 2026-09-06, F01. Authorization used to look only at a
customer_id in the query string or path. Everything else -- a well id in the
path, a customer_id in a JSON body, an inventory row id -- authenticated and
went through. These tests are the matrix the review asked for: own BU vs other
BU, read / write / list / export, and the id arriving by path, query or body.

They run the real token -> user -> BU chain: the admin override cannot test a
boundary that admins are exempt from.
"""

from datetime import datetime, timedelta

import pytest

from app.auth.tokens import issue_token
from app.models import (
    AllocationPolicy, BusinessUnit, Customer, DemandLine, DemandProfile, DemandStatus,
    InventoryOnHand, PlanningNode, Scenario, ScenarioStatus, UserRole, Well,
)
from tests.test_auth import _auth, _mk_user, client_world, real_auth  # noqa: F401


def _foreign_world(session_factory, product_id):
    """A second BU with a customer, a well, a line, an on-hand row and a scenario."""
    with session_factory() as db:
        bu = BusinessUnit(name="BU-2"); db.add(bu); db.flush()
        cust = Customer(name="Foreign Co", business_unit_id=bu.id, allocation_policy=AllocationPolicy.SOFT)
        db.add(cust); db.flush()
        node = PlanningNode(customer_id=cust.id, node_type="Project", name="Foreign Project"); db.add(node); db.flush()
        well = Well(name="FOREIGN-1", planning_node_id=node.id, demand_status=DemandStatus.CONFIRMED); db.add(well); db.flush()
        line = DemandLine(well_id=well.id, product_id=product_id, quantity=100.0,
                          ros_date=datetime.utcnow() + timedelta(days=200), profile=DemandProfile.PRIMARY)
        db.add(line)
        oh = InventoryOnHand(business_unit_id=bu.id, product_id=product_id, quantity=500.0); db.add(oh)
        sc = Scenario(name="Foreign what-if", customer_id=cust.id, status=ScenarioStatus.DRAFT, created_by="x"); db.add(sc)
        db.commit()
        return dict(bu=bu.id, customer=cust.id, well=well.id, line=line.id, on_hand=oh.id, scenario=sc.id)


@pytest.fixture()
def planner_and_foreign(client_world):
    client, sf, w = client_world
    f = _foreign_world(sf, w.p_a_id)
    uid = _mk_user(sf, email="planner@test", role=UserRole.PLANNER, business_unit_id=w.bu_id)
    return client, sf, w, f, _auth(issue_token(uid))


def _rev(qty=150):
    return {"quantity": qty, "ros_date": (datetime.utcnow() + timedelta(days=200)).isoformat(), "profile": "Primary"}


# --- path ids -----------------------------------------------------------------

def test_a_foreign_well_by_path_is_refused_and_an_own_well_is_not(planner_and_foreign):
    client, sf, w, f, h = planner_and_foreign
    assert client.get(f"/wells/{f['well']}", headers=h).status_code == 403
    assert client.get(f"/wells/{w.w1_id}", headers=h).status_code == 200


def test_a_foreign_demand_line_cannot_be_read_or_revised(planner_and_foreign):
    client, sf, w, f, h = planner_and_foreign
    assert client.get(f"/demand-lines/{f['line']}/substitution-candidates", headers=h).status_code == 403
    r = client.post(f"/demand-lines/{f['line']}/revisions", json=_rev(), headers=h)
    assert r.status_code == 403, r.text
    with sf() as db:
        assert db.get(DemandLine, f["line"]).quantity == 100.0, "zero writes on refusal"
    assert client.post(f"/demand-lines/{w.l1_id}/revisions", json=_rev(6000), headers=h).status_code == 200


def test_a_foreign_scenario_is_invisible_and_immutable(planner_and_foreign):
    client, sf, w, f, h = planner_and_foreign
    assert client.get(f"/scenarios/{f['scenario']}", headers=h).status_code == 403
    assert client.patch(f"/scenarios/{f['scenario']}", json={"name": "hijack"}, headers=h).status_code == 403
    assert client.get(f"/scenarios/{f['scenario']}/preview", headers=h).status_code == 403
    assert client.post(
        f"/scenarios/{f['scenario']}/apply", json={"expected_version": 1}, headers=h
    ).status_code == 403


def test_a_foreign_inventory_row_cannot_be_edited(planner_and_foreign):
    client, sf, w, f, h = planner_and_foreign
    r = client.patch(f"/company-inventory/on-hand/{f['on_hand']}", json={"quantity": 1}, headers=h)
    assert r.status_code == 403, r.text
    with sf() as db:
        assert db.get(InventoryOnHand, f["on_hand"]).quantity == 500.0
    assert client.delete(f"/company-inventory/on-hand/{f['on_hand']}", headers=h).status_code == 403


def test_a_foreign_customer_by_path_is_refused(planner_and_foreign):
    client, sf, w, f, h = planner_and_foreign
    assert client.get(f"/customers/{f['customer']}", headers=h).status_code == 403
    assert client.get(f"/customer-owned-inventory/{f['customer']}", headers=h).status_code == 403
    assert client.get(f"/customer-owned-inventory/{f['customer']}/template", headers=h).status_code == 403


# --- query ids ----------------------------------------------------------------

def test_foreign_ids_in_the_query_string_are_refused(planner_and_foreign):
    client, sf, w, f, h = planner_and_foreign
    assert client.get("/coverage", params={"customer_id": f["customer"]}, headers=h).status_code == 403
    assert client.get("/company-inventory", params={"business_unit_id": f["bu"]}, headers=h).status_code == 403
    assert client.get("/analysis/surplus", params={"business_unit_id": f["bu"]}, headers=h).status_code == 403
    assert client.get("/dashboard/executive", params={"business_unit_id": f["bu"]}, headers=h).status_code == 403
    assert client.get("/mrp/export", params={"customer_id": f["customer"]}, headers=h).status_code == 403
    assert client.get("/demand-imports/template", params={"customer_id": f["customer"]}, headers=h).status_code == 403


# --- body ids -----------------------------------------------------------------

def test_a_foreign_customer_in_a_json_body_is_refused_with_zero_writes(planner_and_foreign):
    client, sf, w, f, h = planner_and_foreign
    with sf() as db:
        before = db.query(Scenario).count()
    r = client.post("/scenarios", json={"name": "x", "customer_id": f["customer"], "created_by": "p"}, headers=h)
    assert r.status_code == 403, r.text
    with sf() as db:
        assert db.query(Scenario).count() == before
    assert client.post("/scenarios", json={"name": "ok", "customer_id": w.acme_id, "created_by": "p"}, headers=h).status_code == 201


def test_a_foreign_bu_in_a_json_body_is_refused(planner_and_foreign):
    client, sf, w, f, h = planner_and_foreign
    r = client.post("/company-inventory/on-hand", json={"business_unit_id": f["bu"], "product_id": w.p_b_id, "quantity": 1}, headers=h)
    assert r.status_code == 403, r.text


def test_an_own_scenario_cannot_target_a_foreign_line(planner_and_foreign):
    """The batch ruling in miniature: an override that reaches across the wall
    refuses the whole request, even though the scenario itself is ours."""
    client, sf, w, f, h = planner_and_foreign
    sc = client.post("/scenarios", json={"name": "mine", "customer_id": w.acme_id, "created_by": "p"}, headers=h).json()
    r = client.post(f"/scenarios/{sc['id']}/overrides", json={
        "target_kind": "demand_line", "field_name": "quantity",
        "target_demand_line_id": f["line"], "value_number": 5,
    }, headers=h)
    assert r.status_code == 403, r.text
    assert client.get(f"/scenarios/{sc['id']}", headers=h).json()["overrides"] == []


# --- the honest 404 and the admin -----------------------------------------------

def test_an_unknown_id_is_still_a_404_not_a_403(planner_and_foreign):
    client, sf, w, f, h = planner_and_foreign
    assert client.get("/wells/does-not-exist", headers=h).status_code == 404


def test_an_admin_crosses_freely(client_world):
    client, sf, w = client_world
    f = _foreign_world(sf, w.p_a_id)
    uid = _mk_user(sf, email="admin@test", role=UserRole.ADMIN, business_unit_id=None)
    h = _auth(issue_token(uid))
    assert client.get(f"/wells/{f['well']}", headers=h).status_code == 200
    assert client.patch(f"/company-inventory/on-hand/{f['on_hand']}", json={"quantity": 1}, headers=h).status_code == 200
