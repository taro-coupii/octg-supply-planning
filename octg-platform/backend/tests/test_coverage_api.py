import datetime

from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    CoverageResult,
    CoverageVerdict,
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


def _line(db, well, product, quantity=10.0, unit=UnitOfMeasure.MTR, ros_date=None, profile=DemandProfile.PRIMARY):
    line = DemandLine(
        well_id=well.id,
        product_id=product.id,
        quantity=quantity,
        unit=unit,
        ros_date=ros_date or datetime.date(2026, 1, 1),
        profile=profile,
    )
    db.add(line)
    db.commit()
    return line


def _on_hand(db, bu, product, quantity, unit=UnitOfMeasure.MTR):
    row = InventoryOnHand(business_unit_id=bu.id, product_id=product.id, quantity=quantity, unit=unit)
    db.add(row)
    db.commit()
    return row


# --- POST /coverage/recompute ---


def test_recompute_all_returns_computed_and_skipped(client, db):
    bu = _bu(db)
    good = _customer(db, name="Good", bu=bu)
    bad = _customer(db, name="Bad", bu=None)
    product = _product(db)
    well_good = _well(db, good)
    _line(db, well_good, product, quantity=10.0)
    _on_hand(db, bu, product, 10.0)
    well_bad = _well(db, bad)
    _line(db, well_bad, product, quantity=5.0)

    resp = client.post("/coverage/recompute", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["computed"] == 1
    assert body["skipped_customers"] == [{"name": "Bad", "reason": body["skipped_customers"][0]["reason"]}]


def test_get_coverage_returns_last_recompute_all_skipped_customers(client, db):
    bu = _bu(db)
    good = _customer(db, name="Good", bu=bu)
    bad = _customer(db, name="Bad", bu=None)
    product = _product(db)
    well_good = _well(db, good)
    _line(db, well_good, product, quantity=10.0)
    _on_hand(db, bu, product, 10.0)
    well_bad = _well(db, bad)
    _line(db, well_bad, product, quantity=5.0)

    resp = client.post("/coverage/recompute", json={})
    assert resp.status_code == 200

    grid = client.get("/coverage")
    assert grid.status_code == 200
    names = [row["name"] for row in grid.json()["skipped_customers"]]
    assert names == ["Bad"]


def test_recompute_single_customer(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    _line(db, well, product, quantity=10.0)
    _on_hand(db, bu, product, 10.0)

    resp = client.post("/coverage/recompute", json={"customer_id": cust.id})
    assert resp.status_code == 200
    assert resp.json() == {"computed": 1, "skipped_customers": []}


def test_recompute_single_customer_scope_missing_422(client, db):
    cust = _customer(db, bu=None)
    product = _product(db)
    well = _well(db, cust)
    _line(db, well, product, quantity=10.0)

    resp = client.post("/coverage/recompute", json={"customer_id": cust.id})
    assert resp.status_code == 422


def test_recompute_unknown_customer_404(client, db):
    resp = client.post("/coverage/recompute", json={"customer_id": "nonexistent"})
    assert resp.status_code == 404


# --- GET /coverage grid ---


def test_grid_reads_stored_results_only_no_recompute(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    _line(db, well, product, quantity=10.0)
    _on_hand(db, bu, product, 10.0)
    # deliberately never call recompute

    resp = client.get("/coverage")
    assert resp.status_code == 200
    body = resp.json()
    customer_out = body["customers"][0]
    well_out = customer_out["wells"][0]
    assert well_out["verdict_rollup"] == {"NotEvaluated": 1}
    assert well_out["worst_verdict"] == "NotEvaluated"


def test_grid_rollup_after_recompute(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    _line(db, well, product, quantity=10.0)
    _on_hand(db, bu, product, 10.0)

    client.post("/coverage/recompute", json={"customer_id": cust.id})

    resp = client.get("/coverage")
    body = resp.json()
    well_out = body["customers"][0]["wells"][0]
    assert well_out["verdict_rollup"] == {"Covered": 1}
    assert well_out["worst_verdict"] == "Covered"
    assert body["computed_at_min"] is not None
    assert body["computed_at_max"] is not None


# --- GET /wells/{id} coverage extension ---


def test_well_detail_includes_coverage_or_null(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    line_covered = _line(db, well, product, quantity=10.0, ros_date=datetime.date(2026, 1, 1))
    line_unscoped = _line(db, well, product, quantity=5.0, profile=DemandProfile.PRIMARY, ros_date=datetime.date(2026, 2, 1))
    _on_hand(db, bu, product, 10.0)

    client.post("/coverage/recompute", json={"customer_id": cust.id})

    resp = client.get(f"/wells/{well.id}")
    assert resp.status_code == 200
    lines_by_id = {l["id"]: l for l in resp.json()["lines"]}

    assert lines_by_id[line_covered.id]["coverage"]["verdict"] == "Covered"
    # second line had no more free stock -> not covered, but must still carry a coverage object (evaluated)
    assert lines_by_id[line_unscoped.id]["coverage"] is not None
    assert lines_by_id[line_unscoped.id]["coverage"]["verdict"] == "Unrecoverable"


def test_well_detail_coverage_null_when_never_evaluated(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)

    resp = client.get(f"/wells/{well.id}")
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert lines[0]["coverage"] is None
