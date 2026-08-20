import datetime

from app.engines.mrp import mrp_rows
from app.models import (
    BusinessUnit,
    Customer,
    CustomerOwnedInventory,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    InventoryOnOrder,
    BookingStatus,
    Product,
    UnitOfMeasure,
    Well,
)


def _bu(db, name="BU1"):
    bu = BusinessUnit(name=name)
    db.add(bu)
    db.commit()
    return bu


def _customer(db, name="Cust1", bu=None):
    c = Customer(name=name, business_unit_id=bu.id if bu else None)
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


def _owned(db, customer, product, quantity, unit=UnitOfMeasure.MTR):
    row = CustomerOwnedInventory(customer_id=customer.id, product_id=product.id, quantity=quantity, unit=unit)
    db.add(row)
    db.commit()
    return row


def _po(db, bu, product, quantity, expected_date, unit=UnitOfMeasure.MTR, status=BookingStatus.BOOKED):
    row = InventoryOnOrder(
        business_unit_id=bu.id,
        product_id=product.id,
        quantity=quantity,
        unit=unit,
        expected_date=expected_date,
        booking_status=status,
    )
    db.add(row)
    db.commit()
    return row


AS_OF = datetime.date(2026, 8, 1)


def _find(rows, bu_id, product_id):
    for r in rows:
        if r.bu_id == bu_id and r.product_id == product_id:
            return r
    raise AssertionError("row not found")


def test_identity_and_opening_matches_current_on_hand(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    _line(db, well, product, 5.0, datetime.date(2026, 8, 15))
    _on_hand(db, bu, product, 20.0)
    _owned(db, cust, product, 3.0)

    rows = mrp_rows(db, horizon=3, as_of=AS_OF)
    row = _find(rows, bu.id, product.id)

    assert row.opening.company == 20.0
    assert row.opening.owned == 3.0
    for m in row.months:
        opening_total = m.opening.company + m.opening.owned
        closing_total = m.closing.company + m.closing.owned
        assert (
            opening_total + m.receipts_booked + m.receipts_recommended - m.issues
        ) == closing_total


def test_owned_consumed_before_company_and_negative_visible(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    _line(db, well, product, 8.0, datetime.date(2026, 8, 10))
    _on_hand(db, bu, product, 5.0)
    _owned(db, cust, product, 3.0)

    rows = mrp_rows(db, horizon=1, as_of=AS_OF)
    row = _find(rows, bu.id, product.id)
    m = row.months[0]

    # owned (3.0) consumed fully first, remaining 5.0 issued from company -> company opens at 5, closes at 0
    assert m.opening.owned == 3.0
    assert m.opening.company == 5.0
    assert m.closing.owned == 0.0
    assert m.closing.company == 0.0

    # Now push demand beyond both -> company goes negative for visibility.
    _line(db, well, product, 10.0, datetime.date(2026, 8, 20))
    rows2 = mrp_rows(db, horizon=1, as_of=AS_OF)
    row2 = _find(rows2, bu.id, product.id)
    m2 = row2.months[0]
    assert m2.closing.owned == 0.0
    assert m2.closing.company == 5.0 - 15.0
    assert m2.closing.company < 0


def test_undated_po_excluded_from_months_and_separated(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    _on_hand(db, bu, product, 10.0)
    _po(db, bu, product, 7.0, expected_date=None)
    _po(db, bu, product, 4.0, expected_date=datetime.date(2026, 9, 1))

    rows = mrp_rows(db, horizon=3, as_of=AS_OF)
    row = _find(rows, bu.id, product.id)

    assert row.on_order_undated == 7.0
    total_booked = sum(m.receipts_booked for m in row.months)
    assert total_booked == 4.0


def test_horizon_clamp(db):
    bu = _bu(db)
    _customer(db, bu=bu)
    product = _product(db)
    _on_hand(db, bu, product, 1.0)

    rows_low = mrp_rows(db, horizon=0, as_of=AS_OF)
    row_low = _find(rows_low, bu.id, product.id)
    assert len(row_low.months) == 1

    rows_high = mrp_rows(db, horizon=100, as_of=AS_OF)
    row_high = _find(rows_high, bu.id, product.id)
    assert len(row_high.months) == 36

    rows_default = mrp_rows(db, as_of=AS_OF)
    row_default = _find(rows_default, bu.id, product.id)
    assert len(row_default.months) == 12


def test_runout_three_series(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    _on_hand(db, bu, product, 10.0)
    # demand of 6/month for 3 months
    _line(db, well, product, 6.0, datetime.date(2026, 8, 15))
    _line(db, well, product, 6.0, datetime.date(2026, 9, 15))
    _line(db, well, product, 6.0, datetime.date(2026, 10, 15))
    _po(db, bu, product, 6.0, expected_date=datetime.date(2026, 9, 15))

    recommended = {(bu.id, product.id): {"2026-10": 6.0}}
    rows = mrp_rows(db, horizon=3, as_of=AS_OF, recommended_by_product=recommended)
    row = _find(rows, bu.id, product.id)

    # baseline (no receipts at all): 10-6=4 (Aug), 4-6=-2 (Sep) -> runs out Sep.
    assert row.runout_months["baseline"] == "2026-09"
    # on_order (booked PO in Sep only): 10-6=4, 4+6-6=4, 4-6=-2 -> runs out Oct.
    assert row.runout_months["on_order"] == "2026-10"
    # with_recommended (booked Sep + recommended Oct): never goes negative in horizon.
    assert row.runout_months["with_recommended"] is None


def test_demand_only_product_appears_with_negative_closing(db):
    """A product with in-scope demand but zero on_hand/on_order/owned must
    still produce a ledger row, opening at 0 and closing negative."""
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db, "DemandOnly")
    well = _well(db, cust)
    _line(db, well, product, 5.0, datetime.date(2026, 8, 15))

    rows = mrp_rows(db, horizon=1, as_of=AS_OF)
    row = _find(rows, bu.id, product.id)
    assert row.opening.company == 0.0
    assert row.opening.owned == 0.0
    assert row.months[0].closing.total == -5.0


def test_owned_only_product_appears_with_owned_opening(db):
    """A product with only customer-owned inventory (no on_hand/on_order/
    demand) must still produce a ledger row, opening at the owned amount."""
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db, "OwnedOnly")
    _owned(db, cust, product, 9.0)

    rows = mrp_rows(db, horizon=1, as_of=AS_OF)
    row = _find(rows, bu.id, product.id)
    assert row.opening.owned == 9.0
    assert row.opening.company == 0.0


def test_scope_filters_demand_lines(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust, status=DemandStatus.PLANNED)
    _line(db, well, product, 8.0, datetime.date(2026, 8, 10))
    _on_hand(db, bu, product, 5.0)

    rows = mrp_rows(db, horizon=1, as_of=AS_OF)
    row = _find(rows, bu.id, product.id)
    assert row.months[0].issues == 0.0
