import datetime

from app.engines.surplus import surplus_rows
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


AS_OF = datetime.date(2026, 1, 15)


def test_identity_holds_per_product_and_totals(db):
    bu = _bu(db)
    product = _product(db)
    _on_hand(db, bu, product, 100)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    well = _well(db, cust)
    _line(db, well, product, 30, datetime.date(2026, 2, 1))

    rows = surplus_rows(db, as_of=AS_OF)
    assert len(rows) == 1
    row = rows[0]
    assert row.on_hand == row.allocated + row.surplus + row.obsolete

    total_on_hand = sum(r.on_hand for r in rows)
    total_alloc = sum(r.allocated for r in rows)
    total_surplus = sum(r.surplus for r in rows)
    total_obsolete = sum(r.obsolete for r in rows)
    assert total_on_hand == total_alloc + total_surplus + total_obsolete


def test_allocated_is_hard_plus_free_excludes_owned(db):
    bu = _bu(db)
    product = _product(db)
    _on_hand(db, bu, product, 100)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    well = _well(db, cust)
    _line(db, well, product, 40, datetime.date(2026, 2, 1))

    rows = surplus_rows(db, as_of=AS_OF)
    row = rows[0]
    assert row.allocated == 40
    assert row.surplus == 60
    assert row.obsolete == 0


def test_obsolete_when_zero_scoped_demand_in_horizon(db):
    bu = _bu(db)
    product = _product(db, name="Obsolete Item")
    _on_hand(db, bu, product, 50)
    # No demand lines at all for this product.

    rows = surplus_rows(db, as_of=AS_OF)
    row = rows[0]
    assert row.allocated == 0
    assert row.obsolete == 50
    assert row.surplus == 0


def test_demand_outside_horizon_still_counts_as_obsolete(db):
    bu = _bu(db)
    product = _product(db)
    _on_hand(db, bu, product, 50)
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    # 40 months out — beyond the fixed 36-month horizon.
    _line(db, well, product, 10, datetime.date(2029, 6, 1))

    rows = surplus_rows(db, as_of=AS_OF)
    row = rows[0]
    # allocate_customer allocates ALL of a customer's lines regardless of
    # horizon (it is unscoped by design), so the 10 units this line drew are
    # still `allocated`; only the remaining unallocated on_hand is obsolete.
    assert row.allocated == 10
    assert row.obsolete == 40
    assert row.on_hand == row.allocated + row.surplus + row.obsolete


def test_out_of_scope_demand_does_not_prevent_obsolete(db):
    bu = _bu(db)
    product = _product(db)
    _on_hand(db, bu, product, 50)
    cust = _customer(db, bu=bu)
    well = _well(db, cust, status=DemandStatus.PLANNED)  # out of default scope
    _line(db, well, product, 10, datetime.date(2026, 3, 1))

    rows = surplus_rows(db, as_of=AS_OF)
    row = rows[0]
    # allocate_customer allocates ALL lines regardless of coverage scope, so
    # the line still counts as allocated even though it is out of the
    # Administration demand scope for obsolescence purposes.
    assert row.allocated == 10
    assert row.obsolete == 40
    assert row.on_hand == row.allocated + row.surplus + row.obsolete
