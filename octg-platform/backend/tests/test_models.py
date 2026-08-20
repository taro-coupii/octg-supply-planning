import pytest
from sqlalchemy.exc import IntegrityError

from app.models import BusinessUnit, Customer, Product, UnitOfMeasure


def test_business_unit_hierarchy(db):
    parent = BusinessUnit(name="SC Global")
    child = BusinessUnit(name="SCEU Norway", parent=parent)
    db.add_all([parent, child])
    db.commit()
    assert child.parent_id == parent.id
    assert parent.children == [child]


def test_business_unit_name_unique(db):
    db.add(BusinessUnit(name="SCEU Norway"))
    db.commit()
    db.add(BusinessUnit(name="SCEU Norway"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_customer_may_lack_business_unit(db):
    # BU-less customers are representable; engines decide how to treat them (spec §4)
    db.add(Customer(name="Orphan Oil"))
    db.commit()
    assert db.query(Customer).one().business_unit_id is None


def test_product_requires_unit_of_measure(db):
    db.add(Product(name="9-5/8 casing"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_foreign_keys_enforced(db):
    # sqlite defaults to not enforcing FKs; app/db.py must turn this on
    db.add(Customer(name="Bad Ref", business_unit_id="nonexistent"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_product_weight_nullable_c06(db):
    # C-06R: weight_kg nullable is a recorded compromise, not an accident
    db.add(Product(name="9-5/8 casing", unit_of_measure=UnitOfMeasure.MTR))
    db.commit()
    assert db.query(Product).one().weight_kg is None
