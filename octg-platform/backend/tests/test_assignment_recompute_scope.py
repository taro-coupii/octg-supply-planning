"""An assignment change updates every customer it affects, not only the assignee.

Reproduction from the 2026-09-06 review (F05), inverted. Coverage deducts other
customers' hard assignments from the pool a customer can draw on, so assigning
stock to B changes A's answer -- and A's STORED verdict must move with it, or
the official grid keeps promising steel that is no longer reachable.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.engines.coverage import compute_customer_coverage, recompute_customer
from app.main import app
from app.models import (
    AllocationPolicy, BusinessUnit, CoverageResult, Customer, DemandLine, DemandProfile,
    DemandStatus, InventoryAssignment, InventoryOnHand, PlanningNode, Product, UnitOfMeasure, Well,
)


@pytest.fixture()
def client(db_session):
    app.dependency_overrides[get_db] = lambda: (yield db_session)
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _world(db):
    db.add(BusinessUnit(id="bu", name="BU"))
    db.add(BusinessUnit(id="bu2", name="Other BU"))
    db.add(Product(id="p", description="P", type="CSG", size="9-5/8", weight=53.5, grade="P110",
                   grade_type="Carbon", connection="VAM", unit_of_measure=UnitOfMeasure.MTR))
    db.add(InventoryOnHand(business_unit_id="bu", product_id="p", quantity=6000.0))
    db.add(InventoryOnHand(business_unit_id="bu2", product_id="p", quantity=6000.0))
    for cid, name, bu in (("a", "A", "bu"), ("b", "B", "bu"), ("z", "Z", "bu2")):
        db.add(Customer(id=cid, name=name, business_unit_id=bu, allocation_policy=AllocationPolicy.SOFT))
        db.add(PlanningNode(id=f"n{cid}", name=name, node_type="Campaign", customer_id=cid))
        db.add(Well(id=f"w{cid}", name=f"W-{name}", planning_node_id=f"n{cid}", demand_status=DemandStatus.CONFIRMED))
    db.add(DemandLine(id="la", well_id="wa", product_id="p", quantity=5500.0,
                      ros_date=datetime.utcnow() + timedelta(days=120), profile=DemandProfile.PRIMARY))
    db.add(DemandLine(id="lb", well_id="wb", product_id="p", quantity=500.0,
                      ros_date=datetime.utcnow() + timedelta(days=200), profile=DemandProfile.PRIMARY))
    db.add(DemandLine(id="lz", well_id="wz", product_id="p", quantity=5500.0,
                      ros_date=datetime.utcnow() + timedelta(days=120), profile=DemandProfile.PRIMARY))
    db.flush()
    for cid in ("a", "b", "z"):
        recompute_customer(db, db.get(Customer, cid))
    db.commit()


def _stored(db, line_id):
    db.expire_all()
    return db.query(CoverageResult).filter_by(demand_line_id=line_id).one().status.value


def test_assigning_stock_to_b_updates_a_stored_verdict(client, db_session):
    _world(db_session)
    assert _stored(db_session, "la") == "Covered"
    r = client.post("/company-inventory/assignments",
                    json={"demand_line_id": "lb", "product_id": "p", "quantity": 1000.0})
    assert r.status_code == 201, r.text
    assert _stored(db_session, "la") == "Uncovered"
    # ...and the stored answer is the engine's answer, not a coincidence.
    fresh = compute_customer_coverage(db_session, db_session.get(Customer, "a")).by_line["la"].status.value
    assert fresh == "Uncovered"
    # The response names every customer it recomputed, so the caller can see the reach.
    assert set(r.json()["recomputed_customer_ids"]) >= {"a", "b"}


def test_the_other_business_unit_is_untouched(client, db_session):
    _world(db_session)
    before = _stored(db_session, "lz")
    client.post("/company-inventory/assignments",
                json={"demand_line_id": "lb", "product_id": "p", "quantity": 1000.0})
    assert _stored(db_session, "lz") == before == "Covered"


def test_shrinking_or_deleting_the_assignment_restores_a(client, db_session):
    _world(db_session)
    row = client.post("/company-inventory/assignments",
                      json={"demand_line_id": "lb", "product_id": "p", "quantity": 1000.0}).json()["row_id"]
    assert _stored(db_session, "la") == "Uncovered"
    assert client.patch(f"/company-inventory/assignments/{row}", json={"quantity": 100.0}).status_code == 200
    assert _stored(db_session, "la") == "Covered"
    client.patch(f"/company-inventory/assignments/{row}", json={"quantity": 1000.0})
    assert _stored(db_session, "la") == "Uncovered"
    assert client.delete(f"/company-inventory/assignments/{row}").status_code == 200
    assert _stored(db_session, "la") == "Covered"


def test_moving_a_customer_between_bus_re_judges_the_neighbours(client, db_session):
    """The remap half of F05. A's 1,000 assignment moves with A into the other
    BU, where Z was Covered against 6,000 with 5,500 of demand; Z must now read
    Uncovered in the STORED grid, not only in a fresh compute."""
    _world(db_session)
    db_session.add(InventoryAssignment(demand_line_id="la", product_id="p", quantity=1000.0,
                                       source_system="manual"))
    db_session.commit()
    recompute_customer(db_session, db_session.get(Customer, "a")); db_session.commit()
    assert _stored(db_session, "lz") == "Covered"

    r = client.patch("/customers/a", json={"business_unit_id": "bu2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["business_unit_changed"] is True
    assert body["recomputes_performed"] >= 2, "the moved customer plus at least one neighbour"
    assert body["neighbour_recompute_failures"] == []
    assert _stored(db_session, "lz") == "Uncovered"
    # And the origin BU's neighbour B was re-judged too (it can only improve).
    assert _stored(db_session, "lb") == "Covered"
