import datetime

from app.services.dates import today
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    Product,
    UnitOfMeasure,
    Well,
)


def _bu(db, name="BU1"):
    bu = BusinessUnit(name=name)
    db.add(bu)
    db.commit()
    return bu


def _customer(db, name="Cust1", bu=None, policy=AllocationPolicy.SOFT):
    c = Customer(name=name, business_unit_id=bu.id if bu else None, allocation_policy=policy)
    db.add(c)
    db.commit()
    return c


def _product(db, name="Casing"):
    p = Product(name=name, unit_of_measure=UnitOfMeasure.MTR)
    db.add(p)
    db.commit()
    return p


def _well(db, customer, name="Well1", status=DemandStatus.CONFIRMED):
    w = Well(customer_id=customer.id, name=name, demand_status=status)
    db.add(w)
    db.commit()
    return w


def _line(db, well, product, quantity, ros_date, unit=UnitOfMeasure.MTR, profile=DemandProfile.PRIMARY):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity, unit=unit, ros_date=ros_date, profile=profile
    )
    db.add(line)
    db.commit()
    return line


def _on_hand(db, bu, product, quantity, unit=UnitOfMeasure.MTR):
    row = InventoryOnHand(business_unit_id=bu.id, product_id=product.id, quantity=quantity, unit=unit)
    db.add(row)
    db.commit()
    return row


def _setup(db):
    bu = _bu(db)
    product = _product(db)
    _on_hand(db, bu, product, 50)
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    now = today()
    _line(db, well, product, 20, datetime.date(now.year, now.month, min(now.day, 27)))
    return bu, product, cust, well


def test_mrp_summary_shape_and_scope(client, db):
    bu, product, cust, well = _setup(db)

    resp = client.get("/mrp/summary", params={"horizon": 6})
    assert resp.status_code == 200
    body = resp.json()
    assert "scope" in body
    assert set(body["scope"].keys()) == {"statuses", "profiles"}
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["product"] == product.id
    assert row["unit"] == "Mtr"
    assert len(row["months"]) == 6
    month0 = row["months"][0]
    assert set(month0.keys()) == {
        "month",
        "opening",
        "receipts_booked",
        "receipts_recommended",
        "issues",
        "closing",
    }
    assert set(row["runout_months"].keys()) == {"baseline", "with_recommended", "on_order"}
    assert "on_order_undated" in row


def test_mrp_summary_composes_mor_recommendations(client, db):
    """A product with a MOR-triggered requirement should show up in
    receipts_recommended for the requirement's need_month."""
    bu = _bu(db)
    product = _product(db)
    # Zero on_hand -> MOR will require the full demand quantity in month 0.
    # (an explicit zero row is needed so the product has a ledger at all.)
    _on_hand(db, bu, product, 0)
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    now = today()
    _line(db, well, product, 15, datetime.date(now.year, now.month, min(now.day, 27)))

    resp = client.get("/mrp/summary", params={"horizon": 3})
    body = resp.json()
    row = body["rows"][0]
    total_recommended = sum(m["receipts_recommended"] for m in row["months"])
    assert total_recommended == 15


def test_mrp_item_detail_includes_lines(client, db):
    bu, product, cust, well = _setup(db)

    resp = client.get(f"/mrp/items/{product.id}", params={"bu_id": bu.id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["row"]["product"] == product.id
    assert len(body["row"]["lines"]) == 1
    line = body["row"]["lines"][0]
    assert line["quantity"] == 20
    assert line["customer_id"] == cust.id
    assert "scope" in body


def test_mrp_item_404_for_unknown_product(client, db):
    resp = client.get("/mrp/items/nonexistent")
    assert resp.status_code == 404


def test_order_requirements_shape(client, db):
    bu, product, cust, well = _setup(db)

    resp = client.get("/mrp/order-requirements", params={"horizon": 6})
    assert resp.status_code == 200
    body = resp.json()
    assert "scope" in body
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert set(row.keys()) >= {
        "product",
        "unit",
        "strip",
        "markers",
        "requirements",
        "safety_stock",
    }
    assert set(row["markers"].keys()) == {"order_deadline", "physical_runout", "safety_breach"}


def test_mrp_summary_rows_carry_business_unit_and_multi_bu_no_doublecount(client, db):
    """Two BUs sharing the same product must each get their own MOR
    requirement injected into their own ledger only — never the other BU's
    requirement summed in (double-count), and each row must carry its BU."""
    bu1 = _bu(db, "BU1")
    bu2 = _bu(db, "BU2")
    product = _product(db)
    _on_hand(db, bu1, product, 0)
    _on_hand(db, bu2, product, 0)
    cust1 = _customer(db, "C1", bu=bu1)
    cust2 = _customer(db, "C2", bu=bu2)
    well1 = _well(db, cust1)
    well2 = _well(db, cust2)
    now = today()
    day = min(now.day, 27)
    _line(db, well1, product, 15, datetime.date(now.year, now.month, day))
    _line(db, well2, product, 40, datetime.date(now.year, now.month, day))

    resp = client.get("/mrp/summary", params={"horizon": 3})
    body = resp.json()
    rows = [r for r in body["rows"] if r["product"] == product.id]
    assert len(rows) == 2
    assert {r["business_unit_id"] for r in rows} == {bu1.id, bu2.id}
    by_bu = {r["business_unit_id"]: r for r in rows}
    assert sum(m["receipts_recommended"] for m in by_bu[bu1.id]["months"]) == 15
    assert sum(m["receipts_recommended"] for m in by_bu[bu2.id]["months"]) == 40


def test_order_requirements_rows_carry_business_unit(client, db):
    bu, product, cust, well = _setup(db)
    resp = client.get("/mrp/order-requirements", params={"horizon": 6})
    body = resp.json()
    row = body["rows"][0]
    assert row["business_unit_id"] == bu.id
    assert "business_unit_name" in row


def test_order_requirements_unavailable_customer(client, db):
    bu = _bu(db)
    cust = _customer(db, name="NoBU", bu=None)

    resp = client.get("/mrp/order-requirements", params={"customer_id": cust.id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["unavailable_reason"] is not None
