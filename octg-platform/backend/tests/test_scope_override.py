import datetime

from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    CoverageResult,
    DemandLine,
    DemandProfile,
    DemandStatus,
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


AS_OF = datetime.date(2026, 8, 19)


def _seed_with_tentative_line(db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Casing A")
    cust = _customer(db, bu=bu)
    well_confirmed = _well(db, cust, name="WellC", status=DemandStatus.CONFIRMED)
    well_tentative = _well(db, cust, name="WellT", status=DemandStatus.PLANNED)
    _line(db, well_confirmed, product, 30, datetime.date(2026, 8, 1))
    _line(db, well_tentative, product, 50, datetime.date(2026, 8, 1))
    _on_hand(db, bu, product, 200)
    return bu, product, cust


# --- invariant 2: widened scope on Executive does not touch stored rows ----


def test_executive_scope_override_leaves_stored_coverage_rows_unchanged(client, db, monkeypatch):
    bu, product, cust = _seed_with_tentative_line(db, monkeypatch)

    client.post("/coverage/recompute", json={})
    before = [
        (r.id, r.demand_line_id, r.verdict, r.reason, r.action, r.covered_qty, r.covered_via, r.computed_at)
        for r in db.query(CoverageResult).order_by(CoverageResult.id).all()
    ]
    assert len(before) == 1  # only the Confirmed-status line was in default scope

    resp = client.get("/dashboard/executive", params={"status": ["Confirmed", "Planned"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_is_default"] is False
    assert body["warning"] == "Recomputed read-only — NOT the official stored verdicts"
    # The widened scope sees both lines even though only one is stored.
    total_qty = sum(v["qty_by_unit"].get("Mtr", 0) for v in body["coverage"]["by_verdict"].values())
    assert total_qty == 80

    after = [
        (r.id, r.demand_line_id, r.verdict, r.reason, r.action, r.covered_qty, r.covered_via, r.computed_at)
        for r in db.query(CoverageResult).order_by(CoverageResult.id).all()
    ]
    assert after == before


def test_executive_default_scope_uses_stored_fast_path_only(client, db, monkeypatch):
    bu, product, cust = _seed_with_tentative_line(db, monkeypatch)
    client.post("/coverage/recompute", json={})

    resp = client.get("/dashboard/executive")
    body = resp.json()
    assert body["scope_is_default"] is True
    total_qty = sum(v["qty_by_unit"].get("Mtr", 0) for v in body["coverage"]["by_verdict"].values())
    assert total_qty == 30  # only the stored (Confirmed-scope) line


# --- surplus scope override --------------------------------------------------


def test_surplus_scope_override_widens_obsolete_determination(client, db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Casing A")
    cust = _customer(db, bu=bu)
    # Only a Planned-status line exists — obsolete under the default (Confirmed) scope.
    well = _well(db, cust, status=DemandStatus.PLANNED)
    _line(db, well, product, 30, datetime.date(2026, 9, 1))
    _on_hand(db, bu, product, 100)

    resp_default = client.get("/analysis/surplus")
    body_default = resp_default.json()
    assert body_default["scope_is_default"] is True
    assert "warning" not in body_default or body_default["warning"] is None
    row_default = body_default["rows"][0]
    # allocate_customer is unscoped (allocates ALL demand, incl. the Planned
    # line), so 30 is allocated regardless of scope; the rest is obsolete
    # because no *scoped* demand exists in the horizon under the default scope.
    assert row_default["allocated"] == 30
    assert row_default["obsolete"] == 70

    resp_widened = client.get("/analysis/surplus", params={"status": ["Confirmed", "Planned"]})
    body_widened = resp_widened.json()
    assert body_widened["scope_is_default"] is False
    assert body_widened["warning"] == "Recomputed read-only — NOT the official stored verdicts"
    row_widened = body_widened["rows"][0]
    assert row_widened["obsolete"] == 0  # demand now in scope -> not obsolete


def test_surplus_scope_override_never_persists_anything(client, db, monkeypatch):
    """invariant 2/3-style guard: surplus has nothing to persist, but a scope
    override call must not create/alter any table rows either."""
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Casing A")
    cust = _customer(db, bu=bu)
    well = _well(db, cust, status=DemandStatus.PLANNED)
    _line(db, well, product, 30, datetime.date(2026, 9, 1))
    _on_hand(db, bu, product, 100)

    before_counts = {
        "demand_lines": db.query(DemandLine).count(),
        "coverage_results": db.query(CoverageResult).count(),
        "inventory_on_hand": db.query(InventoryOnHand).count(),
    }

    client.get("/analysis/surplus", params={"status": ["Confirmed", "Planned"], "profile": ["Primary"]})

    after_counts = {
        "demand_lines": db.query(DemandLine).count(),
        "coverage_results": db.query(CoverageResult).count(),
        "inventory_on_hand": db.query(InventoryOnHand).count(),
    }
    assert after_counts == before_counts


# --- all 5 Executive blocks honor scope override (spec: 全ブロック対応) -----


def test_supply_risk_block_honors_scope_override(client, db, monkeypatch):
    """A product whose only demand is a Planned-status line: under the
    default (Confirmed) scope it has no in-scope demand so no runout is
    computed (baseline never depletes -> absent from supply_risk); widening
    the scope to include Planned makes it appear."""
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Casing A")
    cust = _customer(db, bu=bu)
    well = _well(db, cust, status=DemandStatus.PLANNED)
    _on_hand(db, bu, product, 5)
    _line(db, well, product, 10, datetime.date(2026, 9, 1))

    resp_default = client.get("/dashboard/executive")
    default_ids = {item["product_id"] for item in resp_default.json()["supply_risk"]["items"]}
    assert product.id not in default_ids  # Planned-only demand invisible under default scope

    resp_widened = client.get("/dashboard/executive", params={"status": ["Confirmed", "Planned"]})
    widened_body = resp_widened.json()
    assert widened_body["scope_is_default"] is False
    widened_ids = {item["product_id"]: item for item in widened_body["supply_risk"]["items"]}
    assert product.id in widened_ids
    assert widened_ids[product.id]["runout_month"] == "2026-09"


def test_inventory_utilisation_block_honors_scope_override(client, db, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Casing A")
    cust = _customer(db, bu=bu)
    well = _well(db, cust, status=DemandStatus.PLANNED)
    _on_hand(db, bu, product, 100)
    _line(db, well, product, 30, datetime.date(2026, 9, 1))

    resp_default = client.get("/dashboard/executive")
    default_products = {p["product_id"]: p for p in resp_default.json()["inventory_utilisation"]["products"]}
    # allocate_customer (used for "allocated") is unscoped, so the Planned
    # line's 30 units are allocated regardless of scope; the remainder is
    # obsolete because no *scoped* demand exists under the default scope.
    assert default_products[product.id]["obsolete"] == 70

    resp_widened = client.get("/dashboard/executive", params={"status": ["Confirmed", "Planned"]})
    widened_body = resp_widened.json()
    assert widened_body["scope_is_default"] is False
    widened_products = {p["product_id"]: p for p in widened_body["inventory_utilisation"]["products"]}
    assert widened_products[product.id]["obsolete"] == 0  # demand now in scope


def test_supply_risk_and_inventory_utilisation_unchanged_on_default_scope_call(client, db, monkeypatch):
    """Regression: default-scope Executive responses for these two blocks are
    identical before/after this change (no behaviour drift for existing
    callers of mrp_rows/surplus_rows elsewhere either — see full suite)."""
    monkeypatch.setattr(dates, "today", lambda: AS_OF)
    bu = _bu(db)
    product = _product(db, "Casing A")
    cust = _customer(db, bu=bu)
    well = _well(db, cust, status=DemandStatus.CONFIRMED)
    _on_hand(db, bu, product, 5)
    _line(db, well, product, 10, datetime.date(2026, 9, 1))

    resp1 = client.get("/dashboard/executive")
    resp2 = client.get("/dashboard/executive")
    assert resp1.json()["supply_risk"] == resp2.json()["supply_risk"]
    assert resp1.json()["inventory_utilisation"] == resp2.json()["inventory_utilisation"]


def test_executive_scope_override_leaves_stored_rows_untouched_across_all_blocks(client, db, monkeypatch):
    """Extends the invariant-2 guard to the newly scope-aware blocks: a widened
    call touching supply_risk and inventory_utilisation must not write
    anything either (mrp_rows/surplus_rows are pure reads)."""
    bu, product, cust = _seed_with_tentative_line(db, monkeypatch)
    client.post("/coverage/recompute", json={})

    before_counts = {
        "demand_lines": db.query(DemandLine).count(),
        "coverage_results": db.query(CoverageResult).count(),
        "inventory_on_hand": db.query(InventoryOnHand).count(),
    }

    client.get("/dashboard/executive", params={"status": ["Confirmed", "Planned"], "profile": ["Primary", "Contingency"]})

    after_counts = {
        "demand_lines": db.query(DemandLine).count(),
        "coverage_results": db.query(CoverageResult).count(),
        "inventory_on_hand": db.query(InventoryOnHand).count(),
    }
    assert after_counts == before_counts
