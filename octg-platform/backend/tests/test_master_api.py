from app.models import BusinessUnit, Customer, Product, UnitOfMeasure


def test_business_units_returns_nested_hierarchy(client, db):
    root = BusinessUnit(name="SC Global")
    child = BusinessUnit(name="SCEU Norway", parent=root)
    db.add_all([root, child])
    db.commit()

    resp = client.get("/business-units")
    assert resp.status_code == 200
    data = resp.json()
    assert [n["name"] for n in data] == ["SC Global"]
    assert [c["name"] for c in data[0]["children"]] == ["SCEU Norway"]
    assert data[0]["children"][0]["children"] == []


def test_business_units_survives_parent_cycle(client, db):
    a = BusinessUnit(name="A")
    b = BusinessUnit(name="B")
    db.add_all([a, b])
    db.flush()
    a.parent_id = b.id
    b.parent_id = a.id
    db.commit()

    resp = client.get("/business-units")
    assert resp.status_code == 200


def test_customers_list_includes_bu_id_and_null_bu(client, db):
    bu = BusinessUnit(name="SCEU Norway")
    db.add(bu)
    db.flush()
    db.add_all(
        [
            Customer(name="Equinor Norway", business_unit_id=bu.id),
            Customer(name="Orphan Oil"),
        ]
    )
    db.commit()

    resp = client.get("/customers")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["name"] for r in rows] == ["Equinor Norway", "Orphan Oil"]
    assert rows[0]["business_unit_id"] == bu.id
    assert rows[1]["business_unit_id"] is None


def test_products_list_serializes_unit_value(client, db):
    db.add(Product(name="9-5/8 casing", unit_of_measure=UnitOfMeasure.MTR, weight_kg=53.5))
    db.commit()

    resp = client.get("/products")
    assert resp.status_code == 200
    [row] = resp.json()
    assert row["unit_of_measure"] == "Mtr"
    assert row["weight_kg"] == 53.5


def test_product_detail_404_for_unknown_id(client):
    resp = client.get("/products/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Product not found"}


def test_product_detail_returns_row(client, db):
    p = Product(name="7in tubing", unit_of_measure=UnitOfMeasure.PC)
    db.add(p)
    db.commit()

    resp = client.get(f"/products/{p.id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "7in tubing"
    assert resp.json()["weight_kg"] is None
