from app.models import Customer, Product, TechnicalSubstitution, UnitOfMeasure


def _product(db, name):
    p = Product(name=name, unit_of_measure=UnitOfMeasure.PC)
    db.add(p)
    db.flush()
    return p


def test_create_substitution(client, db):
    a = _product(db, "A")
    b = _product(db, "B")
    db.commit()

    resp = client.post("/admin/substitutions", json={"from_product_id": a.id, "to_product_id": b.id})
    assert resp.status_code == 201
    body = resp.json()
    assert body["from_product_id"] == a.id
    assert body["to_product_id"] == b.id


def test_create_substitution_self_rejected_422(client, db):
    a = _product(db, "A")
    db.commit()

    resp = client.post("/admin/substitutions", json={"from_product_id": a.id, "to_product_id": a.id})
    assert resp.status_code == 422


def test_create_duplicate_direction_409(client, db):
    a = _product(db, "A")
    b = _product(db, "B")
    db.add(TechnicalSubstitution(from_product_id=a.id, to_product_id=b.id))
    db.commit()

    resp = client.post("/admin/substitutions", json={"from_product_id": a.id, "to_product_id": b.id})
    assert resp.status_code == 409


def test_get_substitutions_list(client, db):
    a = _product(db, "A")
    b = _product(db, "B")
    db.add(TechnicalSubstitution(from_product_id=a.id, to_product_id=b.id))
    db.commit()

    resp = client.get("/admin/substitutions")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_delete_substitution(client, db):
    a = _product(db, "A")
    b = _product(db, "B")
    sub = TechnicalSubstitution(from_product_id=a.id, to_product_id=b.id)
    db.add(sub)
    db.commit()

    resp = client.delete(f"/admin/substitutions/{sub.id}")
    assert resp.status_code == 204


def test_customer_rules_get_and_put_upsert(client, db):
    a = _product(db, "A")
    b = _product(db, "B")
    sub = TechnicalSubstitution(from_product_id=a.id, to_product_id=b.id)
    cust = Customer(name="Equinor")
    db.add_all([sub, cust])
    db.commit()

    resp = client.put(
        f"/admin/substitutions/customer-rules?customer_id={cust.id}",
        json=[{"technical_substitution_id": sub.id, "allowed": True}],
    )
    assert resp.status_code == 200

    resp = client.get(f"/admin/substitutions/customer-rules?customer_id={cust.id}")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["allowed"] is True

    # Upsert again with different value should not duplicate
    resp = client.put(
        f"/admin/substitutions/customer-rules?customer_id={cust.id}",
        json=[{"technical_substitution_id": sub.id, "allowed": False}],
    )
    assert resp.status_code == 200

    resp = client.get(f"/admin/substitutions/customer-rules?customer_id={cust.id}")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["allowed"] is False
