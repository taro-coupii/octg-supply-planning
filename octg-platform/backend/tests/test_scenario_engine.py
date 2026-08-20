import ast
import datetime
import json
from pathlib import Path

import pytest

from app.engines import scenario as scenario_engine
from app.engines.coverage import recompute_all
from app.models import (
    BusinessUnit,
    Customer,
    DemandLine,
    DemandProfile,
    InventoryOnHand,
    Product,
    Scenario,
    ScenarioOverride,
    ScenarioOverrideKind,
    ScenarioStatus,
    UnitOfMeasure,
    Well,
)


def _seed(db):
    bu = BusinessUnit(name="BU1")
    db.add(bu)
    db.flush()
    customer = Customer(name="Cust1", business_unit_id=bu.id)
    db.add(customer)
    db.flush()
    from app.models import DemandStatus

    well = Well(customer_id=customer.id, name="Well1", demand_status=DemandStatus.CONFIRMED)
    db.add(well)
    db.flush()
    product = Product(name="Prod1", unit_of_measure=UnitOfMeasure.MTR, weight_kg=10.0)
    db.add(product)
    db.flush()
    line = DemandLine(
        well_id=well.id,
        product_id=product.id,
        quantity=100.0,
        unit=UnitOfMeasure.MTR,
        ros_date=datetime.date(2026, 9, 1),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.add(
        InventoryOnHand(
            business_unit_id=bu.id,
            product_id=product.id,
            quantity=50.0,
            unit=UnitOfMeasure.MTR,
        )
    )
    db.commit()
    return bu, customer, well, product, line


def _row_counts(db) -> dict:
    from app.db import Base

    counts = {}
    for table in Base.metadata.sorted_tables:
        counts[table.name] = db.execute(table.select()).rowcount if False else len(
            list(db.execute(table.select()))
        )
    return counts


def test_make_inmemory_copy_reflects_source_data(db):
    bu, customer, well, product, line = _seed(db)

    with scenario_engine.make_inmemory_copy(db) as copy_session:
        copied_line = copy_session.get(DemandLine, line.id)
        assert copied_line is not None
        assert copied_line.quantity == 100.0

        # Mutating the copy must never affect the source session.
        copied_line.quantity = 999.0
        copy_session.commit()

        db.expire_all()
        original = db.get(DemandLine, line.id)
        assert original.quantity == 100.0


def test_make_inmemory_copy_disposes_engine_and_connection_on_exit(db):
    """The copy's Engine and raw sqlite3 connection must both be released when
    the context manager exits — otherwise a full DB copy leaks per preview."""
    bu, customer, well, product, line = _seed(db)

    with scenario_engine.make_inmemory_copy(db) as copy_session:
        raw_conn = copy_session.connection().connection.driver_connection

    # The raw sqlite3 connection must be closed post-exit — proof the copy's
    # underlying DB (not just the ORM Session) was actually released.
    import sqlite3

    with pytest.raises(sqlite3.ProgrammingError):
        raw_conn.execute("select 1")


def test_preview_scenario_twice_does_not_error(db):
    """Calling preview_scenario repeatedly must not raise or leak — each call's
    in-memory copy is fully torn down before the next begins."""
    bu, customer, well, product, line = _seed(db)
    recompute_all(db)

    scenario = Scenario(name="Sp", created_at=datetime.datetime.now(datetime.timezone.utc))
    db.add(scenario)
    db.flush()
    override = ScenarioOverride(
        scenario_id=scenario.id,
        kind=ScenarioOverrideKind.QUANTITY,
        target_id=line.id,
        payload=json.dumps({"value": 5.0}),
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(override)
    db.commit()

    result1 = scenario_engine.preview_scenario(db, [override], {"coverage", "mrp"})
    result2 = scenario_engine.preview_scenario(db, [override], {"coverage", "mrp"})
    assert result1 == result2


def test_invariant_3_preview_leaves_all_row_counts_unchanged(db):
    bu, customer, well, product, line = _seed(db)
    recompute_all(db)

    scenario = Scenario(name="S1", created_at=datetime.datetime.now(datetime.timezone.utc))
    db.add(scenario)
    db.flush()
    override = ScenarioOverride(
        scenario_id=scenario.id,
        kind=ScenarioOverrideKind.QUANTITY,
        target_id=line.id,
        payload=json.dumps({"value": 5.0}),
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(override)
    db.commit()

    before_counts = _row_counts(db)

    result = scenario_engine.preview_scenario(db, [override], {"coverage", "mrp"})

    after_counts = _row_counts(db)
    assert after_counts == before_counts
    assert "coverage" in result
    assert "mrp" in result


def test_invariant_3_no_second_judge_function_exists():
    """Static check: app/engines/scenario.py must not define its own verdict/
    judge implementation — preview must call the production coverage engine."""
    source = Path(scenario_engine.__file__).read_text()
    tree = ast.parse(source)
    forbidden_names = {"judge", "_judge", "judge_customer", "verdict"}
    defined_funcs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not (defined_funcs & forbidden_names), defined_funcs
    # Must actually import and use the real engine functions.
    assert "coverage_engine" in source
    assert "recompute_all" in source


def test_preview_quantity_override_changes_coverage_after(db):
    bu, customer, well, product, line = _seed(db)
    # shrink stock so the original 100 qty is uncovered; recompute stored baseline.
    recompute_all(db)
    before_result = db.query(__import__("app.models", fromlist=["CoverageResult"]).CoverageResult).all()
    assert len(before_result) == 1

    scenario = Scenario(name="S2", created_at=datetime.datetime.now(datetime.timezone.utc))
    db.add(scenario)
    db.flush()
    override = ScenarioOverride(
        scenario_id=scenario.id,
        kind=ScenarioOverrideKind.QUANTITY,
        target_id=line.id,
        payload=json.dumps({"value": 10.0}),  # now well within the 50 on-hand
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(override)
    db.commit()

    result = scenario_engine.preview_scenario(db, [override], {"coverage"})
    assert result["coverage"]["before"] != result["coverage"]["after"]
    assert result["coverage"]["after"].get("Covered") == 1


def test_apply_mixed_supply_and_platform_is_rejected_atomically(db):
    bu, customer, well, product, line = _seed(db)
    from app.models import BookingStatus, InventoryOnOrder

    po = InventoryOnOrder(
        business_unit_id=bu.id,
        product_id=product.id,
        quantity=20.0,
        unit=UnitOfMeasure.MTR,
        expected_date=datetime.date(2026, 9, 1),
        booking_status=BookingStatus.BOOKED,
    )
    db.add(po)
    db.commit()

    scenario = Scenario(name="S3", created_at=datetime.datetime.now(datetime.timezone.utc))
    db.add(scenario)
    db.flush()
    ov_platform = ScenarioOverride(
        scenario_id=scenario.id,
        kind=ScenarioOverrideKind.QUANTITY,
        target_id=line.id,
        payload=json.dumps({"value": 5.0}),
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    ov_supply = ScenarioOverride(
        scenario_id=scenario.id,
        kind=ScenarioOverrideKind.PO_ARRIVAL,
        target_id=po.id,
        payload=json.dumps({"value": "2026-10-01"}),
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add_all([ov_platform, ov_supply])
    db.commit()

    try:
        scenario_engine.apply_scenario(db, scenario, [ov_platform, ov_supply])
        assert False, "expected SupplyOverrideRejected"
    except scenario_engine.SupplyOverrideRejected as exc:
        assert len(exc.rejected) == 1
        assert exc.rejected[0]["kind"] == "po_arrival"

    db.rollback()
    db.expire_all()
    reloaded_line = db.get(DemandLine, line.id)
    assert reloaded_line.quantity == 100.0
    reloaded_scenario = db.get(Scenario, scenario.id)
    assert reloaded_scenario.status == ScenarioStatus.DRAFT


def test_apply_platform_only_transitions_and_records_revision(db):
    bu, customer, well, product, line = _seed(db)

    scenario = Scenario(name="S4", created_at=datetime.datetime.now(datetime.timezone.utc))
    db.add(scenario)
    db.flush()
    override = ScenarioOverride(
        scenario_id=scenario.id,
        kind=ScenarioOverrideKind.QUANTITY,
        target_id=line.id,
        payload=json.dumps({"value": 42.0}),
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(override)
    db.commit()

    scenario_engine.apply_scenario(db, scenario, [override])
    db.commit()

    from app.models import DemandRevision

    db.expire_all()
    reloaded_line = db.get(DemandLine, line.id)
    assert reloaded_line.quantity == 42.0
    reloaded_scenario = db.get(Scenario, scenario.id)
    assert reloaded_scenario.status == ScenarioStatus.APPLIED
    assert reloaded_scenario.applied_at is not None

    revisions = db.query(DemandRevision).filter_by(well_id=well.id).all()
    assert len(revisions) == 1
    assert revisions[0].source.value == "manual"
