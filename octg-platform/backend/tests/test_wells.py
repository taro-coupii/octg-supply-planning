import datetime

from app.models import Customer, DemandLine, DemandProfile, DemandRevision, DemandRevisionSource, DemandStatus, Product, UnitOfMeasure, Well


def _customer(db, name="C1"):
    c = Customer(name=name)
    db.add(c)
    db.commit()
    return c


def _product(db, name="P1", unit=UnitOfMeasure.MTR):
    p = Product(name=name, unit_of_measure=unit)
    db.add(p)
    db.commit()
    return p


def _well(db, customer, name="WellA", status=DemandStatus.PLANNED):
    w = Well(customer_id=customer.id, name=name, demand_status=status)
    db.add(w)
    db.commit()
    return w


def test_list_wells_by_customer_with_line_counts(client, db):
    c1 = _customer(db, "C1")
    c2 = _customer(db, "C2")
    p = _product(db)
    w1 = _well(db, c1, "WellA")
    w2 = _well(db, c2, "WellB")
    db.add(
        DemandLine(
            well_id=w1.id,
            product_id=p.id,
            quantity=1,
            unit=UnitOfMeasure.MTR,
            ros_date=datetime.date(2026, 9, 1),
            profile=DemandProfile.PRIMARY,
        )
    )
    db.commit()

    resp = client.get("/wells", params={"customer_id": c1.id})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "WellA"
    assert body[0]["line_count"] == 1


def test_get_well_detail_includes_lines_and_revisions_desc(client, db):
    c1 = _customer(db)
    p = _product(db)
    w = _well(db, c1)
    db.add(
        DemandLine(
            well_id=w.id,
            product_id=p.id,
            quantity=5,
            unit=UnitOfMeasure.MTR,
            ros_date=datetime.date(2026, 9, 1),
            profile=DemandProfile.PRIMARY,
        )
    )
    db.add(DemandRevision(well_id=w.id, revision_no=1, source=DemandRevisionSource.IMPORT, summary="s1"))
    db.add(DemandRevision(well_id=w.id, revision_no=2, source=DemandRevisionSource.MANUAL, summary="s2"))
    db.commit()

    resp = client.get(f"/wells/{w.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "WellA"
    assert len(body["lines"]) == 1
    assert [r["revision_no"] for r in body["revisions"]] == [2, 1]


def test_get_well_not_found_404(client, db):
    resp = client.get("/wells/nonexistent")
    assert resp.status_code == 404


def test_status_change_records_revision_with_before_after_summary(client, db):
    c1 = _customer(db)
    w = _well(db, c1, status=DemandStatus.PLANNED)

    resp = client.post(f"/wells/{w.id}/status", json={"status": "Confirmed"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["demand_status"] == "Confirmed"

    revisions = db.query(DemandRevision).filter_by(well_id=w.id).all()
    assert len(revisions) == 1
    assert revisions[0].source == DemandRevisionSource.STATUS_CHANGE
    assert "Planned" in revisions[0].summary
    assert "Confirmed" in revisions[0].summary


def test_status_change_same_value_returns_409(client, db):
    c1 = _customer(db)
    w = _well(db, c1, status=DemandStatus.PLANNED)

    resp = client.post(f"/wells/{w.id}/status", json={"status": "Planned"})
    assert resp.status_code == 409


def test_status_change_invalid_value_returns_422(client, db):
    c1 = _customer(db)
    w = _well(db, c1, status=DemandStatus.PLANNED)

    resp = client.post(f"/wells/{w.id}/status", json={"status": "NotAStatus"})
    assert resp.status_code == 422


def test_status_change_not_found_404(client, db):
    resp = client.post("/wells/nonexistent/status", json={"status": "Confirmed"})
    assert resp.status_code == 404
