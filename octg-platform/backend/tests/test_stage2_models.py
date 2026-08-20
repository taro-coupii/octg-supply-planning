import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    BusinessUnit,
    BookingStatus,
    Customer,
    CustomerOwnedInventory,
    CustomerOwnedUpload,
    CustomerSubstitutionRule,
    InventoryAssignment,
    InventoryOnHand,
    InventoryOnOrder,
    LeadTime,
    Product,
    SafetyStock,
    Setting,
    TechnicalSubstitution,
    UnitOfMeasure,
)


def _bu(db, name="BU1"):
    bu = BusinessUnit(name=name)
    db.add(bu)
    db.commit()
    return bu


def _product(db, name="Casing"):
    p = Product(name=name, unit_of_measure=UnitOfMeasure.MTR)
    db.add(p)
    db.commit()
    return p


def test_safety_stock_unique_bu_product(db):
    bu = _bu(db)
    p = _product(db)
    db.add(SafetyStock(business_unit_id=bu.id, product_id=p.id, quantity=10.0, unit=UnitOfMeasure.MTR))
    db.commit()
    db.add(SafetyStock(business_unit_id=bu.id, product_id=p.id, quantity=5.0, unit=UnitOfMeasure.MTR))
    with pytest.raises(IntegrityError):
        db.commit()


def test_inventory_on_hand_unique_bu_product(db):
    bu = _bu(db)
    p = _product(db)
    db.add(InventoryOnHand(business_unit_id=bu.id, product_id=p.id, quantity=1.0, unit=UnitOfMeasure.MTR))
    db.commit()
    db.add(InventoryOnHand(business_unit_id=bu.id, product_id=p.id, quantity=2.0, unit=UnitOfMeasure.MTR))
    with pytest.raises(IntegrityError):
        db.commit()


def test_inventory_on_hand_source_system_defaults_manual(db):
    bu = _bu(db)
    p = _product(db)
    row = InventoryOnHand(business_unit_id=bu.id, product_id=p.id, quantity=1.0, unit=UnitOfMeasure.MTR)
    db.add(row)
    db.commit()
    assert row.source_system == "manual"


def test_technical_substitution_from_ne_to_check(db):
    p = _product(db)
    db.add(TechnicalSubstitution(from_product_id=p.id, to_product_id=p.id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_technical_substitution_unique_direction_pair(db):
    p1 = _product(db, "A")
    p2 = _product(db, "B")
    db.add(TechnicalSubstitution(from_product_id=p1.id, to_product_id=p2.id))
    db.commit()
    db.add(TechnicalSubstitution(from_product_id=p1.id, to_product_id=p2.id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_technical_substitution_reverse_direction_allowed(db):
    p1 = _product(db, "A")
    p2 = _product(db, "B")
    db.add(TechnicalSubstitution(from_product_id=p1.id, to_product_id=p2.id))
    db.commit()
    db.add(TechnicalSubstitution(from_product_id=p2.id, to_product_id=p1.id))
    db.commit()
    assert db.query(TechnicalSubstitution).count() == 2


def test_customer_owned_inventory_unique_customer_product(db):
    cust = Customer(name="Cust1")
    p = _product(db)
    db.add(cust)
    db.commit()
    db.add(CustomerOwnedInventory(customer_id=cust.id, product_id=p.id, quantity=1.0, unit=UnitOfMeasure.MTR))
    db.commit()
    db.add(CustomerOwnedInventory(customer_id=cust.id, product_id=p.id, quantity=2.0, unit=UnitOfMeasure.MTR))
    with pytest.raises(IntegrityError):
        db.commit()


def test_inventory_on_order_expected_date_nullable(db):
    bu = _bu(db)
    p = _product(db)
    row = InventoryOnOrder(
        business_unit_id=bu.id,
        product_id=p.id,
        quantity=3.0,
        unit=UnitOfMeasure.MTR,
        expected_date=None,
        booking_status=BookingStatus.POED,
    )
    db.add(row)
    db.commit()
    assert db.query(InventoryOnOrder).one().expected_date is None


def test_booking_status_values():
    assert BookingStatus.POED.value == "PO'ed"
    assert BookingStatus.BOOKED.value == "Book'ed"


def test_foreign_keys_enforced_on_safety_stock(db):
    p = _product(db)
    db.add(SafetyStock(business_unit_id="nonexistent", product_id=p.id, quantity=1.0, unit=UnitOfMeasure.MTR))
    with pytest.raises(IntegrityError):
        db.commit()


def test_customer_substitution_rule_unique_customer_substitution(db):
    cust = Customer(name="Cust1")
    p1 = _product(db, "A")
    p2 = _product(db, "B")
    db.add(cust)
    db.commit()
    sub = TechnicalSubstitution(from_product_id=p1.id, to_product_id=p2.id)
    db.add(sub)
    db.commit()
    db.add(CustomerSubstitutionRule(customer_id=cust.id, technical_substitution_id=sub.id, allowed=True))
    db.commit()
    db.add(CustomerSubstitutionRule(customer_id=cust.id, technical_substitution_id=sub.id, allowed=False))
    with pytest.raises(IntegrityError):
        db.commit()


def test_setting_key_unique(db):
    db.add(Setting(key="coverage_scope_statuses", value="Confirmed"))
    db.commit()
    db.add(Setting(key="coverage_scope_statuses", value="Other"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_inventory_assignment_creates(db):
    bu = _bu(db)
    p = _product(db)
    cust = Customer(name="Cust1")
    db.add(cust)
    db.commit()
    row = InventoryAssignment(
        business_unit_id=bu.id,
        product_id=p.id,
        customer_id=cust.id,
        quantity=1.0,
        unit=UnitOfMeasure.MTR,
        reference=None,
    )
    db.add(row)
    db.commit()
    assert db.query(InventoryAssignment).one().reference is None


def test_customer_owned_upload_creates(db):
    cust = Customer(name="Cust1")
    db.add(cust)
    db.commit()
    row = CustomerOwnedUpload(customer_id=cust.id, filename="x.xlsx", row_count=3)
    db.add(row)
    db.commit()
    assert db.query(CustomerOwnedUpload).one().row_count == 3


def test_lead_time_months_positive_check(db):
    db.add(LeadTime(business_unit_id=None, product_id=None, months=0))
    with pytest.raises(IntegrityError):
        db.commit()
