from app.models import BusinessUnit, Product, SafetyStock, UnitOfMeasure


def _fixtures(db):
    bu = BusinessUnit(name="BU")
    p = Product(name="P", unit_of_measure=UnitOfMeasure.PC)
    db.add_all([bu, p])
    db.commit()
    return bu, p


def test_get_safety_stocks_only_lists_set_rows(client, db):
    bu, p = _fixtures(db)
    resp = client.get("/admin/safety-stocks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_put_safety_stocks_creates_explicit_zero_row(client, db):
    bu, p = _fixtures(db)

    resp = client.put(
        "/admin/safety-stocks",
        json=[{"business_unit_id": bu.id, "product_id": p.id, "quantity": 0, "unit": "PC"}],
    )
    assert resp.status_code == 200

    resp = client.get("/admin/safety-stocks")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["quantity"] == 0


def test_put_safety_stocks_null_quantity_deletes_row(client, db):
    bu, p = _fixtures(db)
    db.add(SafetyStock(business_unit_id=bu.id, product_id=p.id, quantity=5.0, unit=UnitOfMeasure.PC))
    db.commit()

    resp = client.put(
        "/admin/safety-stocks",
        json=[{"business_unit_id": bu.id, "product_id": p.id, "quantity": None, "unit": "PC"}],
    )
    assert resp.status_code == 200

    resp = client.get("/admin/safety-stocks")
    assert resp.json() == []
    assert db.query(SafetyStock).filter_by(business_unit_id=bu.id, product_id=p.id).first() is None


def test_unset_row_absent_from_get_distinguishes_from_zero(client, db):
    bu, p = _fixtures(db)
    db.add(SafetyStock(business_unit_id=bu.id, product_id=p.id, quantity=0, unit=UnitOfMeasure.PC))
    db.commit()

    resp = client.get("/admin/safety-stocks")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["quantity"] == 0

    # Different BU/product with no row at all -> not present, i.e. unset (not confused with 0)
    other_bu = BusinessUnit(name="Other")
    db.add(other_bu)
    db.commit()
    ids = [(r["business_unit_id"], r["product_id"]) for r in rows]
    assert (other_bu.id, p.id) not in ids


def test_put_safety_stocks_rejects_negative_quantity_422(client, db):
    bu, p = _fixtures(db)

    resp = client.put(
        "/admin/safety-stocks",
        json=[{"business_unit_id": bu.id, "product_id": p.id, "quantity": -1, "unit": "PC"}],
    )
    assert resp.status_code == 422


def test_put_safety_stocks_rejects_invalid_unit_422(client, db):
    bu, p = _fixtures(db)

    resp = client.put(
        "/admin/safety-stocks",
        json=[{"business_unit_id": bu.id, "product_id": p.id, "quantity": 1, "unit": "XX"}],
    )
    assert resp.status_code == 422
