import datetime

from app.models import (
    BookingStatus,
    BusinessUnit,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    InventoryOnOrder,
    Product,
    UnitOfMeasure,
    Well,
)


def _seed(db):
    bu = BusinessUnit(name="BU1")
    db.add(bu)
    db.flush()
    customer = Customer(name="Cust1", business_unit_id=bu.id)
    db.add(customer)
    db.flush()
    well = Well(customer_id=customer.id, name="Well1", demand_status=DemandStatus.CONFIRMED)
    db.add(well)
    db.flush()
    product = Product(name="Prod1", unit_of_measure=UnitOfMeasure.MTR, weight_kg=10.0)
    db.add(product)
    db.flush()
    line = DemandLine(
        well_id=well.id,
        product_id=product.id,
        quantity=100.0,
        unit=UnitOfMeasure.MTR,
        ros_date=datetime.date(2026, 9, 1),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.add(
        InventoryOnHand(business_unit_id=bu.id, product_id=product.id, quantity=50.0, unit=UnitOfMeasure.MTR)
    )
    po = InventoryOnOrder(
        business_unit_id=bu.id,
        product_id=product.id,
        quantity=20.0,
        unit=UnitOfMeasure.MTR,
        expected_date=datetime.date(2026, 9, 1),
        booking_status=BookingStatus.BOOKED,
    )
    db.add(po)
    db.commit()
    return {"bu": bu, "customer": customer, "well": well, "product": product, "line": line, "po": po}


def _create_scenario(client, name="S1"):
    resp = client.post("/scenarios", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_and_list_scenarios(client, db):
    _seed(db)
    scenario = _create_scenario(client)
    assert scenario["status"] == "Draft"
    resp = client.get("/scenarios")
    assert resp.status_code == 200
    assert any(s["id"] == scenario["id"] for s in resp.json())


def test_get_scenario_detail_resolves_names_not_uuids(client, db):
    seed = _seed(db)
    scenario = _create_scenario(client)
    resp = client.post(
        f"/scenarios/{scenario['id']}/overrides",
        json={"kind": "quantity", "target_id": seed["line"].id, "payload": {"value": 55.0}},
    )
    assert resp.status_code == 201, resp.text
    override = resp.json()
    assert override["target_name"] == "Well1 / Prod1"

    detail = client.get(f"/scenarios/{scenario['id']}").json()
    assert len(detail["overrides"]) == 1
    assert detail["overrides"][0]["target_name"] == "Well1 / Prod1"
    # No bare UUID should appear as the display name.
    assert detail["overrides"][0]["target_name"] != seed["line"].id


def test_override_target_must_exist(client, db):
    _seed(db)
    scenario = _create_scenario(client)
    resp = client.post(
        f"/scenarios/{scenario['id']}/overrides",
        json={"kind": "quantity", "target_id": "does-not-exist", "payload": {"value": 5.0}},
    )
    assert resp.status_code == 422


def test_override_payload_shape_validated(client, db):
    seed = _seed(db)
    scenario = _create_scenario(client)
    resp = client.post(
        f"/scenarios/{scenario['id']}/overrides",
        json={"kind": "quantity", "target_id": seed["line"].id, "payload": {"value": "not-a-number"}},
    )
    assert resp.status_code == 422


def test_add_and_delete_override_draft_only(client, db):
    seed = _seed(db)
    scenario = _create_scenario(client)
    resp = client.post(
        f"/scenarios/{scenario['id']}/overrides",
        json={"kind": "quantity", "target_id": seed["line"].id, "payload": {"value": 42.0}},
    )
    override_id = resp.json()["id"]

    del_resp = client.delete(f"/scenarios/{scenario['id']}/overrides/{override_id}")
    assert del_resp.status_code == 204

    detail = client.get(f"/scenarios/{scenario['id']}").json()
    assert detail["overrides"] == []


def test_override_add_and_delete_rejected_when_applied(client, db):
    seed = _seed(db)
    scenario = _create_scenario(client)
    resp = client.post(
        f"/scenarios/{scenario['id']}/overrides",
        json={"kind": "quantity", "target_id": seed["line"].id, "payload": {"value": 42.0}},
    )
    override_id = resp.json()["id"]

    apply_resp = client.post(f"/scenarios/{scenario['id']}/apply")
    assert apply_resp.status_code == 200, apply_resp.text

    add_resp = client.post(
        f"/scenarios/{scenario['id']}/overrides",
        json={"kind": "quantity", "target_id": seed["line"].id, "payload": {"value": 1.0}},
    )
    assert add_resp.status_code == 409

    del_resp = client.delete(f"/scenarios/{scenario['id']}/overrides/{override_id}")
    assert del_resp.status_code == 409


def test_preview_reflects_quantity_override(client, db):
    seed = _seed(db)
    scenario = _create_scenario(client)
    client.post(
        f"/scenarios/{scenario['id']}/overrides",
        json={"kind": "quantity", "target_id": seed["line"].id, "payload": {"value": 10.0}},
    )
    resp = client.get(f"/scenarios/{scenario['id']}/preview?sections=coverage")
    assert resp.status_code == 200
    body = resp.json()
    assert "coverage" in body
    assert body["coverage"]["before"] != body["coverage"]["after"]


def test_apply_mixed_supply_override_is_422_and_atomic(client, db):
    seed = _seed(db)
    scenario = _create_scenario(client)
    client.post(
        f"/scenarios/{scenario['id']}/overrides",
        json={"kind": "quantity", "target_id": seed["line"].id, "payload": {"value": 5.0}},
    )
    client.post(
        f"/scenarios/{scenario['id']}/overrides",
        json={"kind": "po_arrival", "target_id": seed["po"].id, "payload": {"value": "2026-10-01"}},
    )

    resp = client.post(f"/scenarios/{scenario['id']}/apply")
    assert resp.status_code == 422
    assert len(resp.json()["detail"]["rejected"]) == 1

    db.expire_all()
    reloaded_line = db.get(DemandLine, seed["line"].id)
    assert reloaded_line.quantity == 100.0
    reloaded_po = db.get(InventoryOnOrder, seed["po"].id)
    assert reloaded_po.expected_date == datetime.date(2026, 9, 1)

    scenario_check = client.get(f"/scenarios/{scenario['id']}").json()
    assert scenario_check["status"] == "Draft"


def test_apply_happy_path_transitions_and_reapply_409(client, db):
    seed = _seed(db)
    scenario = _create_scenario(client)
    client.post(
        f"/scenarios/{scenario['id']}/overrides",
        json={"kind": "quantity", "target_id": seed["line"].id, "payload": {"value": 33.0}},
    )

    resp = client.post(f"/scenarios/{scenario['id']}/apply")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["applied_overrides"]) == 1
    assert body["rejected"] == []

    db.expire_all()
    reloaded_line = db.get(DemandLine, seed["line"].id)
    assert reloaded_line.quantity == 33.0

    scenario_check = client.get(f"/scenarios/{scenario['id']}").json()
    assert scenario_check["status"] == "Applied"
    assert scenario_check["applied_at"] is not None

    reapply_resp = client.post(f"/scenarios/{scenario['id']}/apply")
    assert reapply_resp.status_code == 409


def test_invariant_6_no_delete_route_on_scenario(client, db):
    _seed(db)
    scenario = _create_scenario(client)
    resp = client.delete(f"/scenarios/{scenario['id']}")
    assert resp.status_code == 405
