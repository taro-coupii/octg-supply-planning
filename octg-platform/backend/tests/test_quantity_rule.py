"""Every quantity that enters the platform obeys one rule (app.quantities).

Reproductions from the 2026-09-06 adversarial review, inverted:
  F03-A  quantity=-100 on a demand revision returned 200, stored -100, judged Covered.
  F03-B  JSON 1e309 on an on-hand PATCH returned 200 and stored +inf.
Plus the owner's rulings: demand > 0, stock >= 0, PC/JT whole numbers only.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.engines.coverage import recompute_customer
from app.main import app
from app.models import (
    AllocationPolicy, BusinessUnit, CoverageResult, Customer, DemandLine, DemandProfile,
    DemandStatus, InventoryOnHand, PlanningNode, Product, UnitOfMeasure, Well,
)
from app.quantities import InvalidQuantity, validate_quantity


@pytest.fixture()
def client(db_session):
    app.dependency_overrides[get_db] = lambda: (yield db_session)
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _world(db, unit=UnitOfMeasure.MTR):
    db.add(BusinessUnit(id="bu", name="BU"))
    db.add(Product(id="p", description="P", type="CSG", size="9-5/8", weight=53.5, grade="P110",
                   grade_type="Carbon", connection="VAM", unit_of_measure=unit))
    db.add(InventoryOnHand(id="oh", business_unit_id="bu", product_id="p", quantity=6000.0))
    db.add(Customer(id="c", name="C", business_unit_id="bu", allocation_policy=AllocationPolicy.SOFT))
    db.add(PlanningNode(id="n", name="N", node_type="Campaign", customer_id="c"))
    db.add(Well(id="w", name="W", planning_node_id="n", demand_status=DemandStatus.CONFIRMED))
    db.add(DemandLine(id="l", well_id="w", product_id="p", quantity=4000.0,
                      ros_date=datetime.utcnow() + timedelta(days=120), profile=DemandProfile.PRIMARY))
    db.flush(); recompute_customer(db, db.get(Customer, "c")); db.commit()


def _rev(qty):
    return {"quantity": qty, "ros_date": (datetime.utcnow() + timedelta(days=120)).isoformat(), "profile": "Primary"}


# --- the pure rule ---------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 1e309, 2_000_000_000])
def test_non_finite_or_oversized_is_refused_for_any_kind(bad):
    for kind in ("demand", "stock"):
        with pytest.raises(InvalidQuantity):
            validate_quantity(bad, kind=kind, unit="Mtr")


def test_demand_must_be_positive_but_stock_may_be_zero():
    with pytest.raises(InvalidQuantity):
        validate_quantity(0, kind="demand", unit="Mtr")
    with pytest.raises(InvalidQuantity):
        validate_quantity(-1, kind="demand", unit="Mtr")
    assert validate_quantity(0, kind="stock", unit="Mtr") == 0.0
    with pytest.raises(InvalidQuantity):
        validate_quantity(-1, kind="stock", unit="Mtr")


def test_pieces_and_joints_are_whole_numbers():
    for unit in ("PC", "JT"):
        with pytest.raises(InvalidQuantity):
            validate_quantity(12.5, kind="demand", unit=unit)
        assert validate_quantity(12, kind="demand", unit=unit) == 12.0
    assert validate_quantity(12.5, kind="demand", unit="Mtr") == 12.5


# --- F03-A: negative demand ------------------------------------------------

def test_a_negative_revision_is_refused_and_changes_nothing(client, db_session):
    _world(db_session)
    r = client.post("/demand-lines/l/revisions", json=_rev(-100))
    assert r.status_code in (400, 422), r.text
    db_session.expire_all()
    assert db_session.get(DemandLine, "l").quantity == 4000.0
    assert db_session.query(CoverageResult).filter_by(demand_line_id="l").one().status.value == "Covered"


def test_a_zero_revision_is_refused_with_the_reason(client, db_session):
    _world(db_session)
    r = client.post("/demand-lines/l/revisions", json=_rev(0))
    assert r.status_code == 400, r.text
    assert "not demand" in r.json()["detail"]


def test_a_fractional_piece_count_is_refused(client, db_session):
    _world(db_session, unit=UnitOfMeasure.PC)
    r = client.post("/demand-lines/l/revisions", json=_rev(12.5))
    assert r.status_code == 400, r.text
    assert "whole" in r.json()["detail"]
    assert client.post("/demand-lines/l/revisions", json=_rev(12)).status_code == 200


# --- F03-B: infinite inventory ---------------------------------------------

@pytest.mark.parametrize("raw", ['{"quantity": 1e309}', '{"quantity": -5}', '{"quantity": 5e9}', '{"quantity": NaN}'])
def test_on_hand_refuses_infinity_negatives_and_absurd_sizes(client, db_session, raw):
    """Sent as raw text: python's json.dumps refuses inf, but a wire client does not."""
    _world(db_session)
    r = client.patch("/company-inventory/on-hand/oh", content=raw, headers={"content-type": "application/json"})
    assert r.status_code in (400, 422), r.text
    db_session.expire_all()
    assert db_session.get(InventoryOnHand, "oh").quantity == 6000.0


def test_on_hand_zero_is_a_legal_counted_fact(client, db_session):
    _world(db_session)
    r = client.patch("/company-inventory/on-hand/oh", json={"quantity": 0})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(InventoryOnHand, "oh").quantity == 0.0


def test_safety_stock_takes_the_same_rule(client, db_session):
    _world(db_session, unit=UnitOfMeasure.JT)
    assert client.put("/admin/safety-stocks/p", content='{"quantity": 1e309}', headers={"content-type": "application/json"}).status_code in (400, 422)
    assert client.put("/admin/safety-stocks/p", json={"quantity": 3.5}).status_code == 400
    assert client.put("/admin/safety-stocks/p", json={"quantity": 0}).status_code == 200
