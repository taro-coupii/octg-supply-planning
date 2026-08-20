import datetime

from app.engines.coverage import recompute_customer
from app.engines.mor import mor_rows
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
    LeadTime,
    Product,
    SafetyStock,
    UnitOfMeasure,
    Well,
)
from app.models import CoverageResult


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


def _lead_time(db, months, bu=None, product=None):
    row = LeadTime(business_unit_id=bu.id if bu else None, product_id=product.id if product else None, months=months)
    db.add(row)
    db.commit()
    return row


def _safety(db, bu, product, quantity, unit=UnitOfMeasure.MTR):
    row = SafetyStock(business_unit_id=bu.id, product_id=product.id, quantity=quantity, unit=unit)
    db.add(row)
    db.commit()
    return row


AS_OF = datetime.date(2026, 8, 1)


def _find(rows, bu_id, product_id):
    for r in rows:
        if r.bu_id == bu_id and r.product_id == product_id:
            return r
    raise AssertionError("row not found")


def test_incremental_shortfall_charged_to_first_deficit_month(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    _on_hand(db, bu, product, 10.0)
    # demand 6/mo for 3 months, no receipts -> balances: Aug 4, Sep -2, Oct -8
    _line(db, well, product, 6.0, datetime.date(2026, 8, 15))
    _line(db, well, product, 6.0, datetime.date(2026, 9, 15))
    _line(db, well, product, 6.0, datetime.date(2026, 10, 15))

    result = mor_rows(db, horizon=3, as_of=AS_OF)
    row = _find(result.rows, bu.id, product.id)

    reqs = {r.need_month: r.qty for r in row.requirements}
    # Sep deficit 2 (first negative), Oct deficit 8 total -> incremental 6 charged to Oct only.
    assert reqs == {"2026-09": 2.0, "2026-10": 6.0}
    assert row.markers["physical_runout"] == "2026-09"


def test_safety_breach_distinct_from_runout_and_dedup(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    _on_hand(db, bu, product, 10.0)
    _safety(db, bu, product, 5.0)
    # demand 6/mo: Aug close 4 (< safety 5 -> safety breach, not yet negative),
    # Sep close -2 (physical runout)
    _line(db, well, product, 6.0, datetime.date(2026, 8, 15))
    _line(db, well, product, 6.0, datetime.date(2026, 9, 15))

    result = mor_rows(db, horizon=2, as_of=AS_OF)
    row = _find(result.rows, bu.id, product.id)

    assert row.markers["safety_breach"] == "2026-08"
    assert row.markers["physical_runout"] == "2026-09"
    assert row.markers["safety_breach"] != row.markers["physical_runout"]

    # Combined (deduped) requirement series: Aug deficit-to-safety = 5-4=1;
    # Sep combined min level is safety(5), balance -2 -> total deficit 7,
    # incremental over Aug's 1 = 6. No double count of Aug's 1 in Sep.
    reqs = {r.need_month: r.qty for r in row.requirements}
    assert reqs == {"2026-08": 1.0, "2026-09": 6.0}


def test_lead_time_specificity_and_ex_mill_overdue(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    other_product = _product(db, "Other")
    well = _well(db, cust)
    _on_hand(db, bu, product, 10.0)
    _lead_time(db, 6)  # global default
    _lead_time(db, 1, product=other_product)  # product-only, doesn't apply
    _lead_time(db, 2, bu=bu, product=product)  # most specific -> wins
    # demand that creates a requirement in month 0 (Aug) so ex_mill = Aug - 2 = Jun (past -> overdue)
    _line(db, well, product, 12.0, datetime.date(2026, 8, 15))

    result = mor_rows(db, horizon=1, as_of=AS_OF)
    row = _find(result.rows, bu.id, product.id)
    assert len(row.requirements) == 1
    req = row.requirements[0]
    assert req.need_month == "2026-08"
    assert req.ex_mill_month == "2026-06"
    assert req.overdue is True
    assert row.markers["order_deadline"] == "2026-06"


def test_customer_filter_bu_boundary_and_unavailable(db):
    bu1 = _bu(db, "BU1")
    bu2 = _bu(db, "BU2")
    cust1 = _customer(db, "C1", bu=bu1)
    cust2 = _customer(db, "C2", bu=bu2)
    product = _product(db)
    _on_hand(db, bu1, product, 100.0)  # bu1 stock, should not count for cust2's own product row scope test
    _on_hand(db, bu2, product, 4.0)
    _owned(db, cust1, product, 50.0)  # other customer's owned material, must not be visible to cust2
    _owned(db, cust2, product, 3.0)
    well2 = _well(db, cust2)
    _line(db, well2, product, 6.0, datetime.date(2026, 8, 15))

    result = mor_rows(db, horizon=1, as_of=AS_OF, customer_id=cust2.id)
    assert result.unavailable_reason is None
    row = _find(result.rows, bu2.id, product.id)
    # opening = bu2 on_hand(4) + cust2's own owned(3) = 7, NOT cust1's owned or bu1 stock.
    assert row.opening_balance == 7.0

    # customer with no BU -> unavailable
    cust3 = _customer(db, "C3", bu=None)
    result_unavail = mor_rows(db, horizon=1, as_of=AS_OF, customer_id=cust3.id)
    assert result_unavail.rows == []
    assert result_unavail.unavailable_reason is not None


def test_demand_only_product_appears_in_mor(db):
    """A product with in-scope demand but zero on_hand/on_order/owned must
    still produce a MOR row — this is exactly the case most needing an
    order (bug: key set was previously derived from stock/PO tables only)."""
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db, "DemandOnly")
    well = _well(db, cust)
    _line(db, well, product, 5.0, datetime.date(2026, 8, 15))

    result = mor_rows(db, horizon=2, as_of=AS_OF)
    row = _find(result.rows, bu.id, product.id)
    assert row.opening_balance == 0.0
    reqs = {r.need_month: r.qty for r in row.requirements}
    assert reqs == {"2026-08": 5.0}


def test_owned_only_product_appears_in_mor(db):
    """A product with only CustomerOwnedInventory (no on_hand/on_order/demand)
    must still produce a row with the owned quantity as opening balance."""
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db, "OwnedOnly")
    _owned(db, cust, product, 12.0)

    result = mor_rows(db, horizon=1, as_of=AS_OF)
    row = _find(result.rows, bu.id, product.id)
    assert row.opening_balance == 12.0
    assert row.requirements == []


def test_coverage_results_unchanged_by_mor_and_safety_stock(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    _line(db, well, product, 5.0, datetime.date(2026, 8, 15))
    _on_hand(db, bu, product, 20.0)
    _safety(db, bu, product, 100.0)  # would trigger heavy MOR requirements

    recompute_customer(db, cust.id)
    before = [(r.demand_line_id, r.verdict, r.reason) for r in db.query(CoverageResult).all()]

    mor_rows(db, horizon=6, as_of=AS_OF)  # must not touch CoverageResult

    after = [(r.demand_line_id, r.verdict, r.reason) for r in db.query(CoverageResult).all()]
    assert before == after
