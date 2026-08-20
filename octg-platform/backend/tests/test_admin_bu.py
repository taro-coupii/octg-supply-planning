from app.models import BusinessUnit, Customer


def test_create_business_unit(client, db):
    resp = client.post("/admin/business-units", json={"name": "SC Global", "parent_id": None})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "SC Global"
    assert db.query(BusinessUnit).filter_by(name="SC Global").one()


def test_patch_rename_and_reparent(client, db):
    root = BusinessUnit(name="Root")
    child = BusinessUnit(name="Child")
    db.add_all([root, child])
    db.commit()

    resp = client.patch(f"/admin/business-units/{child.id}", json={"name": "Renamed", "parent_id": root.id})
    assert resp.status_code == 200
    db.refresh(child)
    assert child.name == "Renamed"
    assert child.parent_id == root.id


def test_patch_self_parent_rejected_422(client, db):
    a = BusinessUnit(name="A")
    db.add(a)
    db.commit()

    resp = client.patch(f"/admin/business-units/{a.id}", json={"parent_id": a.id})
    assert resp.status_code == 422


def test_patch_grandchild_cycle_rejected_422(client, db):
    a = BusinessUnit(name="A")
    b = BusinessUnit(name="B", parent=a)
    c = BusinessUnit(name="C", parent=b)
    db.add_all([a, b, c])
    db.commit()

    # Make A a child of C -> cycle A -> C -> B -> A
    resp = client.patch(f"/admin/business-units/{a.id}", json={"parent_id": c.id})
    assert resp.status_code == 422


def test_delete_with_children_409(client, db):
    a = BusinessUnit(name="A")
    b = BusinessUnit(name="B", parent=a)
    db.add_all([a, b])
    db.commit()

    resp = client.delete(f"/admin/business-units/{a.id}")
    assert resp.status_code == 409


def test_delete_with_customers_409(client, db):
    a = BusinessUnit(name="A")
    db.add(a)
    db.flush()
    db.add(Customer(name="Cust", business_unit_id=a.id))
    db.commit()

    resp = client.delete(f"/admin/business-units/{a.id}")
    assert resp.status_code == 409


def test_delete_happy_path(client, db):
    a = BusinessUnit(name="A")
    db.add(a)
    db.commit()

    resp = client.delete(f"/admin/business-units/{a.id}")
    assert resp.status_code == 204
    assert db.query(BusinessUnit).filter_by(id=a.id).first() is None
