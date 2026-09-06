"""Renaming and deleting a Business Unit, and the explicit coverage recompute.

Three pieces of surface adopted, after review, from a sibling implementation of this
platform. Each was taken because it closes a real hole here; what was NOT taken is
recorded too, because a rejected idea is worth as much as an accepted one.

WHAT WAS ADOPTED
================
PATCH /business-units/{id}   A BU could be created and never renamed. A typo in the
                             one field a human navigates by was permanent.
DELETE /business-units/{id}  ...and never removed, so a BU created by mistake stayed
                             in every picker for ever. The REFUSAL is the feature: a
                             BU that still has customers, stock or a pinned planner
                             cannot be deleted, because the boundary it defines is
                             absolute and stranding rows against a dead id breaks
                             coverage rather than tidying anything.
POST /coverage/recompute     There was no legal way to ask for fresh verdicts. C-08
                             and HANDOFF §7 both describe the same recurring problem:
                             the engine is fixed, the database still holds yesterday's
                             answer, and the only way to provoke a rewrite was to
                             invent an unrelated write.

WHAT WAS DELIBERATELY NOT ADOPTED
=================================
The sibling models Business Units as a TREE (`parent_id`) and offers a parent remap
with cycle detection. Ours is flat on purpose -- `app.engines.coverage` states that the
pool a customer draws from is a flat join and needs no recursive walk -- so importing a
hierarchy would add a field an operator can set, that looks meaningful, and that no
engine reads. `test_rename_is_the_only_editable_field_on_a_business_unit` pins that.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.engines.coverage import recompute_customer
from app.main import app
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    CoverageResult,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
)


@pytest.fixture()
def client(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _world(db, *, mapped=True, on_hand=5000.0):
    """One BU holding stock, one customer with one well and one 4000 Mtr line."""
    bu = BusinessUnit(id="bu-home", name="Tubular Home")
    db.add(bu)
    product = Product(
        id="p-main",
        description="9-5/8 53.5 P110 VAM TOP",
        type="CSG",
        size="9-5/8",
        weight=53.5,
        grade="P110",
        grade_type="Carbon",
        connection="VAM TOP",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.add(
        InventoryOnHand(
            business_unit_id=bu.id, product_id=product.id, quantity=on_hand
        )
    )
    customer = Customer(
        id="cust-1",
        name="Acme Drilling",
        business_unit_id=bu.id if mapped else None,
        allocation_policy=AllocationPolicy.SOFT,
    )
    db.add(customer)
    node = PlanningNode(
        id="node-1", name="Field A", node_type="Campaign", customer_id=customer.id
    )
    db.add(node)
    well = Well(
        id="well-1",
        name="A-01",
        planning_node_id=node.id,
        demand_status=DemandStatus.CONFIRMED,
    )
    db.add(well)
    db.add(
        DemandLine(
            id="line-1",
            well_id=well.id,
            product_id=product.id,
            quantity=4000.0,
            ros_date=datetime.utcnow() + timedelta(days=120),
            profile=DemandProfile.PRIMARY,
        )
    )
    db.flush()
    if mapped:
        recompute_customer(db, customer)
    db.commit()
    return bu, customer, well, product


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


def test_renaming_a_business_unit_changes_no_verdict(client, db_session):
    """The claim that makes a rename safe: the id is what every pool resolves through.

    Asserted through the stored verdict rather than by reasoning about it -- the
    CoverageResult rows are read before and after and must be identical.
    """
    bu, customer, _, _ = _world(db_session)
    before = {
        r.demand_line_id: r.status for r in db_session.query(CoverageResult).all()
    }
    assert before, "the fixture must have produced verdicts for this to mean anything"

    res = client.patch(f"/business-units/{bu.id}", json={"name": "Tubular North Sea"})
    assert res.status_code == 200, res.text
    assert res.json() == {"id": bu.id, "name": "Tubular North Sea"}

    db_session.expire_all()
    after = {
        r.demand_line_id: r.status for r in db_session.query(CoverageResult).all()
    }
    assert after == before
    assert db_session.get(Customer, customer.id).business_unit_id == bu.id


def test_rename_refuses_a_blank_name_and_a_case_insensitive_duplicate(
    client, db_session
):
    """Both refusals mirror the create path, because the reason is the same one.

    The stricter-than-the-database case-insensitive check is the interesting half: the
    constraint would happily store "Tubular Home" beside "tubular home", and the click
    those two sit next to remaps a customer into a different warehouse.
    """
    bu, _, _, _ = _world(db_session)
    other = BusinessUnit(id="bu-2", name="Tubular Offshore")
    db_session.add(other)
    db_session.commit()

    blank = client.patch(f"/business-units/{bu.id}", json={"name": "   "})
    assert blank.status_code == 400
    assert "must not be blank" in blank.json()["detail"]

    dup = client.patch(f"/business-units/{bu.id}", json={"name": "TUBULAR OFFSHORE"})
    assert dup.status_code == 409
    assert "Tubular Offshore" in dup.json()["detail"]

    db_session.expire_all()
    assert db_session.get(BusinessUnit, bu.id).name == "Tubular Home"


def test_renaming_a_business_unit_to_its_own_name_is_accepted(client, db_session):
    """A no-op rename is not a duplicate of itself -- the clash query excludes the row."""
    bu, _, _, _ = _world(db_session)
    res = client.patch(f"/business-units/{bu.id}", json={"name": "Tubular Home"})
    assert res.status_code == 200, res.text


def test_rename_is_the_only_editable_field_on_a_business_unit(client, db_session):
    """No parent, no hierarchy. See the module docstring for why this is deliberate.

    A sibling implementation offers `parent_id` here. Ours has no such column at all,
    so an unknown field must not silently appear to be accepted.
    """
    bu, _, _, _ = _world(db_session)
    assert not hasattr(db_session.get(BusinessUnit, bu.id), "parent_id")

    res = client.patch(
        f"/business-units/{bu.id}", json={"name": "Tubular Home", "parent_id": "bu-x"}
    )
    assert res.status_code == 200, res.text
    assert "parent_id" not in res.json()


def test_renaming_a_business_unit_that_does_not_exist_is_a_404(client, db_session):
    _world(db_session)
    res = client.patch("/business-units/bu-nope", json={"name": "Anything"})
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_deleting_a_business_unit_that_still_has_rows_is_refused_and_itemised(
    client, db_session
):
    """The refusal is the feature, and it must say what has to move.

    The fixture BU has a customer AND an on-hand row, so both counts have to appear:
    an operator told only about the first obstacle would move the customer and meet
    the second one on the next click.
    """
    bu, _, _, _ = _world(db_session)

    res = client.delete(f"/business-units/{bu.id}")
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["business_unit"]["id"] == bu.id
    assert detail["customer_count"] == 1
    assert detail["inventory_on_hand_row_count"] == 1
    assert "no cascade" in detail["note"]

    db_session.expire_all()
    assert db_session.get(BusinessUnit, bu.id) is not None


def test_an_empty_business_unit_deletes(client, db_session):
    _world(db_session)
    empty = BusinessUnit(id="bu-empty", name="Tubular Empty")
    db_session.add(empty)
    db_session.commit()

    res = client.delete(f"/business-units/{empty.id}")
    assert res.status_code == 204, res.text

    db_session.expire_all()
    assert db_session.get(BusinessUnit, "bu-empty") is None
    assert db_session.get(BusinessUnit, "bu-home") is not None


def test_deleting_a_business_unit_that_does_not_exist_is_a_404(client, db_session):
    _world(db_session)
    assert client.delete("/business-units/bu-nope").status_code == 404


# ---------------------------------------------------------------------------
# Explicit recompute
# ---------------------------------------------------------------------------


def test_recompute_rewrites_the_stored_verdict_after_an_out_of_band_change(
    client, db_session
):
    """The hole C-08 describes, closed.

    Stock is dropped underneath the stored verdict without going through any endpoint
    that recomputes -- exactly the "the data changed elsewhere" case. The verdict is
    stale until asked, and correct afterwards.
    """
    bu, customer, _, product = _world(db_session)
    stored = db_session.query(CoverageResult).filter_by(demand_line_id="line-1").one()
    assert stored.status.value == "Covered"

    row = (
        db_session.query(InventoryOnHand)
        .filter_by(business_unit_id=bu.id, product_id=product.id)
        .one()
    )
    row.quantity = 10.0
    db_session.commit()

    db_session.expire_all()
    still = db_session.query(CoverageResult).filter_by(demand_line_id="line-1").one()
    assert still.status.value == "Covered", "stale is the precondition, not a bug here"

    res = client.post("/coverage/recompute", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["computed_customers"] == 1
    assert body["computed_lines"] == 1
    assert body["skipped_customers"] == []
    assert "official stored verdicts" in body["note"]

    db_session.expire_all()
    fresh = db_session.query(CoverageResult).filter_by(demand_line_id="line-1").one()
    assert fresh.status.value != "Covered"
    assert customer.id


def test_naming_one_customer_recomputes_its_whole_business_unit(client, db_session):
    """Naming a customer recomputes the pool it shares.

    This asserted the opposite until 2026-09-06: a scoped recompute touched only
    the named customer, which was coherent while each customer was allocated on its
    own. Since the Business Unit became the unit of allocation (D01) that would
    leave the neighbours holding verdicts decided against a division of the steel
    this pass has just replaced -- so the recompute widens, and the response says
    how far it went.
    """
    bu, customer, _, product = _world(db_session)

    other = Customer(
        id="cust-2",
        name="Beta Drilling",
        business_unit_id=bu.id,
        allocation_policy=AllocationPolicy.SOFT,
    )
    db_session.add(other)
    node = PlanningNode(
        id="node-2", name="Field B", node_type="Campaign", customer_id=other.id
    )
    db_session.add(node)
    well = Well(
        id="well-2",
        name="B-01",
        planning_node_id=node.id,
        demand_status=DemandStatus.CONFIRMED,
    )
    db_session.add(well)
    db_session.add(
        DemandLine(
            id="line-2",
            well_id=well.id,
            product_id=product.id,
            quantity=100.0,
            ros_date=datetime.utcnow() + timedelta(days=120),
            profile=DemandProfile.PRIMARY,
        )
    )
    db_session.flush()
    recompute_customer(db_session, other)
    db_session.commit()

    res = client.post("/coverage/recompute", json={"customer_id": customer.id})
    assert res.status_code == 200, res.text
    # Both customers of the Business Unit, and both their lines.
    assert res.json()["computed_customers"] == 2
    assert res.json()["computed_lines"] == 2
    assert "whole Business Unit" in res.json()["note"]


def test_recompute_for_an_unknown_customer_is_a_404(client, db_session):
    _world(db_session)
    res = client.post("/coverage/recompute", json={"customer_id": "cust-nope"})
    assert res.status_code == 404


def test_recompute_isolates_a_customer_it_cannot_evaluate_and_names_it(
    client, db_session
):
    """C-07's lesson applied to the WRITE path.

    An unmapped customer has no inventory pool at all, so it cannot be evaluated. It
    must cost only itself: the mapped customer is still recomputed and committed, and
    the unmapped one comes back named with a reason rather than as a 409 for everyone.
    """
    _world(db_session)
    stranded = Customer(
        id="cust-stranded",
        name="Gamma Drilling",
        business_unit_id=None,
        allocation_policy=AllocationPolicy.SOFT,
    )
    db_session.add(stranded)
    db_session.commit()

    res = client.post("/coverage/recompute", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["computed_customers"] == 1
    assert [s["name"] for s in body["skipped_customers"]] == ["Gamma Drilling"]
    assert body["skipped_customers"][0]["customer_id"] == "cust-stranded"
    assert body["skipped_customers"][0]["reason"]
    assert "isolation, not partial success" in body["note"]


def test_a_substitution_candidate_names_the_product_it_replaces(client, db_session):
    """No UUID reaches the planner on the substitution screen.

    The engine's candidate carries a full Product for the `to` side only, so the
    screen used to render the first eight characters of the from-product's id.
    An id is not a name: it cannot be checked against a pipe tally, and this
    platform's own rule is that a human is never shown one. The description is a
    property of the LINE, so the API stamps it on every candidate.
    """
    from app.models import DemandLine, Product, TechnicalSubstitution

    _world(db_session)
    substitute = Product(
        id="p-sub",
        description="9-5/8 53.5 SM110XS VAM TOP",
        type="CSG",
        size="9-5/8",
        weight=53.5,
        grade="SM110XS",
        grade_type="Carbon",
        connection="VAM TOP",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db_session.add(substitute)
    db_session.add(
        TechnicalSubstitution(from_product_id="p-main", to_product_id=substitute.id)
    )
    db_session.add(
        InventoryOnHand(
            business_unit_id="bu-home", product_id=substitute.id, quantity=9000.0
        )
    )
    db_session.commit()

    line = db_session.get(DemandLine, "line-1")
    res = client.get(f"/demand-lines/{line.id}/substitution-candidates")
    assert res.status_code == 200, res.text
    rows = res.json()
    assert rows, "the fixture must offer a candidate for this to mean anything"
    for row in rows:
        assert row["from_product_description"] == "9-5/8 53.5 P110 VAM TOP"
        assert row["from_product_id"] not in (row["from_product_description"] or "")
