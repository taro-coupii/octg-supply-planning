import datetime

from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryAssignment,
    InventoryOnHand,
    Product,
    UnitOfMeasure,
    Well,
)
from app.services import dates


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


def _product(db, name="Casing", unit=UnitOfMeasure.MTR, weight_kg=None):
    p = Product(name=name, unit_of_measure=unit, weight_kg=weight_kg)
    db.add(p)
    db.commit()
    return p


def _well(db, customer, name="Well1", status=DemandStatus.CONFIRMED):
    w = Well(customer_id=customer.id, name=name, demand_status=status)
    db.add(w)
    db.commit()
    return w


def _line(db, well, product, quantity, ros_date, unit=UnitOfMeasure.MTR, profile=DemandProfile.PRIMARY, created_at=None):
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=quantity, unit=unit, ros_date=ros_date, profile=profile
    )
    if created_at is not None:
        line.created_at = created_at
    db.add(line)
    db.commit()
    return line


def _on_hand(db, bu, product, quantity, unit=UnitOfMeasure.MTR):
    row = InventoryOnHand(business_unit_id=bu.id, product_id=product.id, quantity=quantity, unit=unit)
    db.add(row)
    db.commit()
    return row


def _hard_assign(db, bu, product, customer, quantity, unit=UnitOfMeasure.MTR):
    row = InventoryAssignment(business_unit_id=bu.id, product_id=product.id, customer_id=customer.id, quantity=quantity, unit=unit)
    db.add(row)
    db.commit()
    return row


AS_OF = datetime.date(2026, 8, 19)


def test_executive_dashboard_default_scope_shape(client, db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Casing A", weight_kg=10.0)
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    _line(db, well, product, 30, datetime.date(2026, 8, 1))
    _on_hand(db, bu, product, 100)

    resp = client.get("/dashboard/executive")
    assert resp.status_code == 200
    body = resp.json()

    assert body["scope_is_default"] is True
    assert "warning" not in body
    assert set(body.keys()) >= {
        "demand_trend",
        "coverage",
        "supply_risk",
        "soft_allocation",
        "inventory_utilisation",
        "status_scope",
        "profile_scope",
    }
    for block in ("demand_trend", "coverage", "supply_risk", "soft_allocation", "inventory_utilisation"):
        assert "mt_total" in body[block]
        assert "mt_incomplete" in body[block]
        assert "available" in body[block]


def test_coverage_block_uses_stored_results_by_default(client, db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Casing A", weight_kg=10.0)
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    _line(db, well, product, 30, datetime.date(2026, 8, 1))
    _on_hand(db, bu, product, 100)

    # No recompute has run yet -> unavailable, no fabricated numbers.
    resp = client.get("/dashboard/executive")
    assert resp.json()["coverage"]["available"] is False

    client.post("/coverage/recompute", json={})

    resp = client.get("/dashboard/executive")
    coverage = resp.json()["coverage"]
    assert coverage["available"] is True
    assert "Covered" in coverage["by_verdict"]
    assert coverage["by_verdict"]["Covered"]["label"] == "Covered"
    assert coverage["by_verdict"]["Covered"]["qty_by_unit"]["Mtr"] == 30
    # weight_kg=10 -> 30 * 10 / 1000 = 0.3 MT
    assert coverage["mt_total"] == 0.3
    assert coverage["mt_incomplete"] is False


def test_coverage_block_mt_incomplete_when_weight_missing(client, db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "No Weight", weight_kg=None)
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    _line(db, well, product, 30, datetime.date(2026, 8, 1))
    _on_hand(db, bu, product, 100)
    client.post("/coverage/recompute", json={})

    resp = client.get("/dashboard/executive")
    coverage = resp.json()["coverage"]
    assert coverage["mt_incomplete"] is True
    assert coverage["mt_total"] == 0.0


def test_mt_unit_product_passes_through_without_weight(client, db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "MT Product", unit=UnitOfMeasure.MT, weight_kg=None)
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    _line(db, well, product, 12, datetime.date(2026, 8, 1), unit=UnitOfMeasure.MT)
    _on_hand(db, bu, product, 100, unit=UnitOfMeasure.MT)
    client.post("/coverage/recompute", json={})

    resp = client.get("/dashboard/executive")
    coverage = resp.json()["coverage"]
    assert coverage["mt_incomplete"] is False
    assert coverage["mt_total"] == 12


def test_demand_trend_excludes_lines_created_after_as_of(client, db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Casing A")
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    # backdated line: created before as_of, included.
    _line(
        db, well, product, 10, datetime.date(2026, 8, 1),
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    )
    # future-dated creation: created after as_of, excluded.
    future_line = _line(
        db, well, product, 999, datetime.date(2026, 8, 1),
        created_at=datetime.datetime(2026, 12, 1, tzinfo=datetime.timezone.utc),
    )

    resp = client.get("/dashboard/executive")
    months = resp.json()["demand_trend"]["months"]
    aug = next(m for m in months if m["month"] == "2026-08")
    assert aug["qty_by_unit"]["Mtr"] == 10  # not 1009 — future_line excluded


def test_supply_risk_lists_products_with_baseline_runout_ascending(client, db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Runs Out")
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    _on_hand(db, bu, product, 5)
    _line(db, well, product, 10, datetime.date(2026, 9, 1))

    resp = client.get("/dashboard/executive")
    risk = resp.json()["supply_risk"]
    assert risk["available"] is True
    assert len(risk["items"]) == 1
    assert risk["items"][0]["product_id"] == product.id
    assert risk["items"][0]["runout_month"] == "2026-09"


def test_soft_allocation_sums_from_free_per_customer(client, db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Casing A")
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    well = _well(db, cust)
    _line(db, well, product, 30, datetime.date(2026, 8, 1))
    _on_hand(db, bu, product, 100)

    resp = client.get("/dashboard/executive")
    soft = resp.json()["soft_allocation"]
    assert soft["available"] is True
    assert cust.id in soft["by_customer"]
    assert soft["by_customer"][cust.id]["qty_by_unit"]["Mtr"] == 30


def test_engine_blocks_carry_no_mt_fields_native_only(db, monkeypatch):
    """spec E-1: MT conversion happens ONLY at the API layer. The engine
    module's own dataclasses must carry no mt_total/mt_incomplete/mt-anything
    field — engine output stays native."""
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    from app.engines import executive as exec_engine

    bu = _bu(db)
    product = _product(db, "Casing A", weight_kg=10.0)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    well = _well(db, cust)
    _line(db, well, product, 30, datetime.date(2026, 8, 1))
    _on_hand(db, bu, product, 100)

    statuses, profiles = {"Confirmed"}, {"Primary", "Contingency"}

    dataclasses_to_check = [
        exec_engine.DemandTrendMonth,
        exec_engine.CoverageBlockRow,
        exec_engine.SupplyRiskRow,
        exec_engine.SoftAllocationRow,
        exec_engine.InventoryUtilRow,
    ]
    for dc in dataclasses_to_check:
        field_names = {f for f in dc.__dataclass_fields__}
        assert not any("mt" in name.lower() for name in field_names), f"{dc.__name__} leaked an MT field: {field_names}"

    # And actual instances, from live calls, carry no MT attributes either.
    months = exec_engine.demand_trend_rows(db, statuses, profiles, AS_OF)
    assert months and not any("mt" in k.lower() for row in months[0].rows for k in row.keys())

    coverage_rows, _ = exec_engine.coverage_rows(db, statuses, profiles, use_stored=False)
    assert coverage_rows

    supply_risk = exec_engine.supply_risk_rows(db, statuses, profiles)
    soft_alloc = exec_engine.soft_allocation_rows(db, statuses, profiles)
    inv_util = exec_engine.inventory_utilisation_rows(db, statuses, profiles)
    # Nothing above raised or needed a weight lookup — native only.
    assert isinstance(supply_risk, list)
    assert isinstance(soft_alloc, list)
    assert isinstance(inv_util, list)


def test_mt_headline_partial_exclusion_with_mixed_weighted_products(client, db, monkeypatch):
    """One weighted + one unweighted product feeding the same block: the
    weighted product's qty contributes to mt_total, the unweighted product's
    qty is excluded, and mt_incomplete is set — never fabricated."""
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    weighted = _product(db, "Weighted", weight_kg=10.0)
    unweighted = _product(db, "Unweighted", weight_kg=None)
    cust = _customer(db, bu=bu)
    well = _well(db, cust)
    _line(db, well, weighted, 30, datetime.date(2026, 8, 1))
    _line(db, well, unweighted, 50, datetime.date(2026, 8, 1))
    _on_hand(db, bu, weighted, 100)
    _on_hand(db, bu, unweighted, 100)
    client.post("/coverage/recompute", json={})

    resp = client.get("/dashboard/executive")
    coverage = resp.json()["coverage"]
    assert coverage["mt_incomplete"] is True
    # Only the weighted product's 30 units contribute: 30 * 10 / 1000 = 0.3 MT.
    assert coverage["mt_total"] == 0.3

    demand_trend = resp.json()["demand_trend"]
    assert demand_trend["mt_incomplete"] is True
    assert demand_trend["mt_total"] == 0.3


def test_inventory_utilisation_summarises_surplus_engine(client, db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Idle Item")
    _on_hand(db, bu, product, 40)

    resp = client.get("/dashboard/executive")
    util = resp.json()["inventory_utilisation"]
    assert util["available"] is True
    assert util["totals_by_unit"]["Mtr"]["obsolete"] == 40
    assert util["products"][0]["not_tied"] == 40
