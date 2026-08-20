from app.models import BusinessUnit, LeadTime, Product, UnitOfMeasure


def test_get_lead_times_lists_all_rows(client, db):
    bu = BusinessUnit(name="BU")
    p = Product(name="P", unit_of_measure=UnitOfMeasure.PC)
    db.add_all([bu, p])
    db.flush()
    db.add_all(
        [
            LeadTime(business_unit_id=None, product_id=None, months=6),
            LeadTime(business_unit_id=bu.id, product_id=p.id, months=2),
        ]
    )
    db.commit()

    resp = client.get("/admin/lead-times")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert any(r["business_unit_id"] is None and r["product_id"] is None and r["months"] == 6 for r in rows)


def test_put_lead_times_replaces_all(client, db):
    db.add(LeadTime(business_unit_id=None, product_id=None, months=6))
    db.commit()

    resp = client.put(
        "/admin/lead-times",
        json=[{"business_unit_id": None, "product_id": None, "months": 3}],
    )
    assert resp.status_code == 200
    rows = db.query(LeadTime).all()
    assert len(rows) == 1
    assert rows[0].months == 3


def test_put_lead_times_rejects_non_positive_months_422(client, db):
    resp = client.put(
        "/admin/lead-times",
        json=[{"business_unit_id": None, "product_id": None, "months": 0}],
    )
    assert resp.status_code == 422


def test_put_lead_times_rejects_duplicate_bu_product_pairs_422(client, db):
    bu = BusinessUnit(name="BU")
    p = Product(name="P", unit_of_measure=UnitOfMeasure.PC)
    db.add_all([bu, p])
    db.commit()

    resp = client.put(
        "/admin/lead-times",
        json=[
            {"business_unit_id": bu.id, "product_id": p.id, "months": 3},
            {"business_unit_id": bu.id, "product_id": p.id, "months": 6},
        ],
    )
    assert resp.status_code == 422

    # nothing was written since validation failed
    assert db.query(LeadTime).count() == 0
