import datetime

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


def test_surplus_shape_and_identity(client, db):
    bu = _bu(db)
    product = _product(db, name="Casing A")
    _on_hand(db, bu, product, 100)
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    _line(db, well, product, 30, datetime.date(2026, 2, 1))

    resp = client.get("/analysis/surplus")
    assert resp.status_code == 200
    body = resp.json()
    assert "scope" in body
    assert set(body["scope"].keys()) == {"statuses", "profiles"}
    assert body["identity_ok"] is True
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["product"] == "Casing A"
    assert row["unit"] == "Mtr"
    assert row["on_hand"] == row["allocated"] + row["surplus"] + row["obsolete"]

    assert "Mtr" in body["totals_by_unit"]
    total = body["totals_by_unit"]["Mtr"]
    assert total["on_hand"] == total["allocated"] + total["surplus"] + total["obsolete"]


def test_surplus_obsolete_product_with_no_demand(client, db):
    bu = _bu(db)
    product = _product(db, name="Idle Item")
    _on_hand(db, bu, product, 40)

    resp = client.get("/analysis/surplus")
    body = resp.json()
    row = body["rows"][0]
    assert row["allocated"] == 0
    assert row["obsolete"] == 40
    assert row["surplus"] == 0
