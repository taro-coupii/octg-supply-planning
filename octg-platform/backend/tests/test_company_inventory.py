from app.models import (
    BookingStatus,
    BusinessUnit,
    Customer,
    InventoryAssignment,
    InventoryOnHand,
    InventoryOnOrder,
    Product,
    UnitOfMeasure,
)


def _fixtures(db):
    bu = BusinessUnit(name="BU")
    p = Product(name="P", unit_of_measure=UnitOfMeasure.PC)
    c = Customer(name="C")
    db.add_all([bu, p, c])
    db.commit()
    return bu, p, c


def test_get_company_inventory_shape(client, db):
    bu, p, c = _fixtures(db)
    db.add(InventoryOnHand(business_unit_id=bu.id, product_id=p.id, quantity=10, unit=UnitOfMeasure.PC))
    db.add(
        InventoryAssignment(
            business_unit_id=bu.id, product_id=p.id, customer_id=c.id, quantity=5, unit=UnitOfMeasure.PC
        )
    )
    db.add(
        InventoryOnOrder(
            business_unit_id=bu.id,
            product_id=p.id,
            quantity=3,
            unit=UnitOfMeasure.PC,
            expected_date=None,
            booking_status=BookingStatus.POED,
        )
    )
    db.commit()

    resp = client.get("/company-inventory")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"on_hand", "assignments", "on_order"}
    assert len(body["on_hand"]) == 1
    assert body["on_hand"][0]["unit"] == "PC"
    assert len(body["assignments"]) == 1
    assert body["assignments"][0]["unit"] == "PC"
    assert len(body["on_order"]) == 1
    assert body["on_order"][0]["unit"] == "PC"


def test_on_order_undated_row_returns_null_expected_date(client, db):
    bu, p, c = _fixtures(db)
    db.add(
        InventoryOnOrder(
            business_unit_id=bu.id,
            product_id=p.id,
            quantity=3,
            unit=UnitOfMeasure.PC,
            expected_date=None,
            booking_status=BookingStatus.BOOKED,
        )
    )
    db.commit()

    resp = client.get("/company-inventory")
    row = resp.json()["on_order"][0]
    assert row["expected_date"] is None


def test_post_on_hand_creates_manual_row(client, db):
    bu, p, c = _fixtures(db)

    resp = client.post(
        "/company-inventory/on-hand",
        json={"business_unit_id": bu.id, "product_id": p.id, "quantity": 7, "unit": "PC"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["quantity"] == 7
    assert body["source_system"] == "manual"

    resp = client.get("/company-inventory")
    assert len(resp.json()["on_hand"]) == 1


def test_patch_manual_on_hand_row(client, db):
    bu, p, c = _fixtures(db)
    row = InventoryOnHand(
        business_unit_id=bu.id, product_id=p.id, quantity=10, unit=UnitOfMeasure.PC, source_system="manual"
    )
    db.add(row)
    db.commit()

    resp = client.patch(f"/company-inventory/on-hand/{row.id}", json={"quantity": 20, "unit": "PC"})
    assert resp.status_code == 200
    assert resp.json()["quantity"] == 20


def test_delete_manual_on_hand_row(client, db):
    bu, p, c = _fixtures(db)
    row = InventoryOnHand(
        business_unit_id=bu.id, product_id=p.id, quantity=10, unit=UnitOfMeasure.PC, source_system="manual"
    )
    db.add(row)
    db.commit()
    row_id = row.id

    resp = client.delete(f"/company-inventory/on-hand/{row_id}")
    assert resp.status_code == 204

    resp = client.get("/company-inventory")
    assert resp.json()["on_hand"] == []


def test_patch_oracle_row_returns_409(client, db):
    bu, p, c = _fixtures(db)
    row = InventoryOnHand(
        business_unit_id=bu.id, product_id=p.id, quantity=10, unit=UnitOfMeasure.PC, source_system="oracle"
    )
    db.add(row)
    db.commit()

    resp = client.patch(f"/company-inventory/on-hand/{row.id}", json={"quantity": 20, "unit": "PC"})
    assert resp.status_code == 409
    assert "reason" in resp.json()["detail"] or isinstance(resp.json()["detail"], str)


def test_delete_oracle_row_returns_409(client, db):
    bu, p, c = _fixtures(db)
    row = InventoryOnHand(
        business_unit_id=bu.id, product_id=p.id, quantity=10, unit=UnitOfMeasure.PC, source_system="oracle"
    )
    db.add(row)
    db.commit()

    resp = client.delete(f"/company-inventory/on-hand/{row.id}")
    assert resp.status_code == 409
