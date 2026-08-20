import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    Customer,
    DemandImport,
    DemandImportRow,
    DemandImportStatus,
    DemandLine,
    DemandProfile,
    DemandRevision,
    DemandRevisionSource,
    DemandStatus,
    Product,
    UnitOfMeasure,
    Well,
)


def _customer(db, name="Cust1"):
    c = Customer(name=name)
    db.add(c)
    db.commit()
    return c


def _product(db, name="Casing"):
    p = Product(name=name, unit_of_measure=UnitOfMeasure.MTR)
    db.add(p)
    db.commit()
    return p


def _well(db, customer=None, name="Well1"):
    customer = customer or _customer(db)
    w = Well(customer_id=customer.id, name=name)
    db.add(w)
    db.commit()
    return w


def test_well_unique_customer_name(db):
    cust = _customer(db)
    db.add(Well(customer_id=cust.id, name="Well1"))
    db.commit()
    db.add(Well(customer_id=cust.id, name="Well1"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_well_demand_status_defaults_planned(db):
    w = _well(db)
    assert w.demand_status == DemandStatus.PLANNED


def test_well_demand_status_values_match_frontend_enum():
    assert DemandStatus.PLANNED.value == "Planned"
    assert DemandStatus.BUDGETED.value == "Budgeted"
    assert DemandStatus.CONFIRMED.value == "Confirmed"


def test_demand_line_has_no_status_column():
    assert not hasattr(DemandLine, "status")
    assert "status" not in DemandLine.__table__.columns


def test_demand_line_requires_ros_date_unit_profile(db):
    w = _well(db)
    p = _product(db)
    line = DemandLine(
        well_id=w.id,
        product_id=p.id,
        quantity=10.0,
        unit=UnitOfMeasure.MTR,
        ros_date=None,
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    with pytest.raises(IntegrityError):
        db.commit()


def test_demand_line_quantity_must_be_positive(db):
    import datetime

    w = _well(db)
    p = _product(db)
    db.add(
        DemandLine(
            well_id=w.id,
            product_id=p.id,
            quantity=0,
            unit=UnitOfMeasure.MTR,
            ros_date=datetime.date(2026, 1, 1),
            profile=DemandProfile.PRIMARY,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_demand_line_profile_values_match_frontend_enum():
    assert DemandProfile.PRIMARY.value == "Primary"
    assert DemandProfile.CONTINGENCY.value == "Contingency"


def test_demand_line_foreign_keys_enforced(db):
    import datetime

    db.add(
        DemandLine(
            well_id="nonexistent",
            product_id="nonexistent",
            quantity=1.0,
            unit=UnitOfMeasure.MTR,
            ros_date=datetime.date(2026, 1, 1),
            profile=DemandProfile.PRIMARY,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_demand_revision_unique_well_revision_no(db):
    w = _well(db)
    db.add(DemandRevision(well_id=w.id, revision_no=1, source=DemandRevisionSource.IMPORT, summary="s"))
    db.commit()
    db.add(DemandRevision(well_id=w.id, revision_no=1, source=DemandRevisionSource.MANUAL, summary="s2"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_demand_revision_source_values():
    assert DemandRevisionSource.IMPORT.value == "import"
    assert DemandRevisionSource.MANUAL.value == "manual"
    assert DemandRevisionSource.STATUS_CHANGE.value == "status_change"


def test_demand_revision_append_only_no_update_delete_api():
    # Contract: no API path exists to UPDATE/DELETE revisions. This test asserts
    # the model itself carries no soft-delete/editable marker columns.
    cols = set(DemandRevision.__table__.columns.keys())
    assert "deleted_at" not in cols
    assert "updated_at" not in cols


def test_demand_import_status_default_pending(db):
    cust = _customer(db)
    imp = DemandImport(customer_id=cust.id, filename="x.xlsx")
    db.add(imp)
    db.commit()
    assert imp.status == DemandImportStatus.PENDING


def test_demand_import_status_values():
    assert DemandImportStatus.PENDING.value == "pending"
    assert DemandImportStatus.APPLIED.value == "applied"
    assert DemandImportStatus.DISCARDED.value == "discarded"


def test_demand_import_row_creates(db):
    import datetime

    cust = _customer(db)
    p = _product(db)
    imp = DemandImport(customer_id=cust.id, filename="x.xlsx")
    db.add(imp)
    db.commit()
    row = DemandImportRow(
        import_id=imp.id,
        row_no=1,
        well_name="Well1",
        product_id=p.id,
        quantity=5.0,
        unit=UnitOfMeasure.MTR,
        ros_date=datetime.date(2026, 3, 1),
        profile=DemandProfile.CONTINGENCY,
    )
    db.add(row)
    db.commit()
    assert db.query(DemandImportRow).one().well_name == "Well1"


def test_demand_import_row_foreign_key_enforced(db):
    db.add(
        DemandImportRow(
            import_id="nonexistent",
            row_no=1,
            well_name="Well1",
            product_id=None,
            quantity=5.0,
            unit=UnitOfMeasure.MTR,
            ros_date=None,
            profile=DemandProfile.PRIMARY,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
