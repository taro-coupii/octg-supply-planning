import datetime

from app.models import Customer, DemandLine, DemandProfile, DemandStatus, Product, UnitOfMeasure, Well
from app.services import dates as dates_service


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


def _line(db, well, product, ros_date, qty=10.0, unit=UnitOfMeasure.MTR, profile=DemandProfile.PRIMARY):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=qty, unit=unit, ros_date=ros_date, profile=profile
    )
    db.add(line)
    db.commit()
    return line


def test_lines_include_joined_names_and_unit(client, db):
    c = _customer(db)
    p = _product(db)
    w = _well(db, c)
    _line(db, w, p, datetime.date(2026, 9, 1))

    resp = client.get("/demand/lines")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["well_name"] == "WellA"
    assert item["customer_name"] == "C1"
    assert item["product_name"] == "P1"
    assert item["unit"] == "Mtr"


def test_overdue_boundary_previous_month_end_is_overdue(client, db, monkeypatch):
    monkeypatch.setattr(dates_service, "today", lambda: datetime.date(2026, 8, 19))
    c = _customer(db)
    p = _product(db)
    w = _well(db, c)
    _line(db, w, p, datetime.date(2026, 7, 31))

    resp = client.get("/demand/lines")
    item = resp.json()["items"][0]
    assert item["overdue"] is True


def test_overdue_boundary_current_month_first_day_is_not_overdue(client, db, monkeypatch):
    monkeypatch.setattr(dates_service, "today", lambda: datetime.date(2026, 8, 19))
    c = _customer(db)
    p = _product(db)
    w = _well(db, c)
    _line(db, w, p, datetime.date(2026, 8, 1))

    resp = client.get("/demand/lines")
    item = resp.json()["items"][0]
    assert item["overdue"] is False


def test_lines_filter_by_bad_status_returns_422(client, db):
    resp = client.get("/demand/lines", params={"status": "Bogus"})
    assert resp.status_code == 422


def test_filters_combine_status_profile_ros_range(client, db):
    c = _customer(db)
    p = _product(db)
    w1 = _well(db, c, name="WellA", status=DemandStatus.CONFIRMED)
    w2 = _well(db, c, name="WellB", status=DemandStatus.PLANNED)
    _line(db, w1, p, datetime.date(2026, 9, 1), profile=DemandProfile.PRIMARY)
    _line(db, w1, p, datetime.date(2026, 9, 1), profile=DemandProfile.CONTINGENCY)
    _line(db, w2, p, datetime.date(2026, 9, 1), profile=DemandProfile.PRIMARY)
    _line(db, w1, p, datetime.date(2026, 12, 1), profile=DemandProfile.PRIMARY)

    resp = client.get(
        "/demand/lines",
        params={
            "status": "Confirmed",
            "profile": "Primary",
            "ros_from": "2026-09-01",
            "ros_to": "2026-09-30",
        },
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["well_name"] == "WellA"


def test_pagination_page_2_returns_correct_slice(client, db):
    c = _customer(db)
    p = _product(db)
    w = _well(db, c)
    for i in range(30):
        _line(db, w, p, datetime.date(2026, 9, 1) + datetime.timedelta(days=i))

    resp = client.get("/demand/lines", params={"page": 2, "page_size": 25})
    body = resp.json()
    assert body["total"] == 30
    assert body["page"] == 2
    assert body["page_size"] == 25
    assert len(body["items"]) == 5


def test_default_page_size_is_25(client, db):
    c = _customer(db)
    p = _product(db)
    w = _well(db, c)
    for i in range(30):
        _line(db, w, p, datetime.date(2026, 9, 1) + datetime.timedelta(days=i))

    resp = client.get("/demand/lines")
    body = resp.json()
    assert body["page_size"] == 25
    assert len(body["items"]) == 25


def test_qty_summary_not_used_in_lines_but_units_preserved_per_row(client, db):
    c = _customer(db)
    p_mtr = _product(db, name="PMtr", unit=UnitOfMeasure.MTR)
    p_pc = _product(db, name="PPc", unit=UnitOfMeasure.PC)
    w = _well(db, c)
    _line(db, w, p_mtr, datetime.date(2026, 9, 1), unit=UnitOfMeasure.MTR)
    _line(db, w, p_pc, datetime.date(2026, 9, 1), unit=UnitOfMeasure.PC)

    resp = client.get("/demand/lines")
    units = {item["unit"] for item in resp.json()["items"]}
    assert units == {"Mtr", "PC"}


def test_filter_by_customer_well_product(client, db):
    c1 = _customer(db, "C1")
    c2 = _customer(db, "C2")
    p1 = _product(db, "P1")
    p2 = _product(db, "P2")
    w1 = _well(db, c1, "WellA")
    w2 = _well(db, c2, "WellB")
    _line(db, w1, p1, datetime.date(2026, 9, 1))
    _line(db, w2, p2, datetime.date(2026, 9, 1))

    resp = client.get("/demand/lines", params={"customer_id": c1.id})
    assert resp.json()["total"] == 1

    resp = client.get("/demand/lines", params={"well_id": w2.id})
    assert resp.json()["total"] == 1

    resp = client.get("/demand/lines", params={"product_id": p1.id})
    assert resp.json()["total"] == 1
