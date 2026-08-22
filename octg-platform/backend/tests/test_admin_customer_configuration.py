"""Admin editing of the Business Unit -> Customer hierarchy and the allocation policy.

`Customer.business_unit_id` and `Customer.allocation_policy` were seed-fixed and there
was no way to create a Business Unit at all. Everything below is new surface, and each
test pins one claim that could plausibly be got wrong.

WHAT THIS MODULE PINS, AND WHY EACH ONE IS HERE
==============================================
BUSINESS UNIT CREATION
  * Happy path, and that the response WARNS the new BU holds no InventoryOnHand rows.
    A fresh BU looks configured and is inert; the interesting failure arrives one click
    later, at the remap.
  * A duplicate name is a clean 409 -- CASE-INSENSITIVELY, which is stricter than the
    database constraint. The name is the only handle a human has on a BU, and the click
    it sits next to remaps a customer into a different warehouse.

ALLOCATION POLICY
  * SOFT -> HARD GENUINELY FLIPS A WELL. Constructed so the pool covers the demand and
    no InventoryAssignment exists, so the well is Covered under SOFT and Uncovered
    under HARD. This is the test that proves the recompute is real rather than
    cosmetic: it asserts the stored `Well.coverage_status` moved AND that the response's
    well_changes reports it.
  * ...and back again, so the change is not a one-way door.

BU REMAP
  * null -> mapped recomputes correctly. This is the "onboard this customer properly"
    case, and before the change it was the workflow `app.models.customer.Customer`'s
    docstring described but the API did not offer.
  * A REMAP THAT WOULD STRAND THE CUSTOMER IS REFUSED AND ROLLED BACK. The destination
    BU has no InventoryOnHand row for a product the customer demands, so the pass
    raises InventoryRowMissing -- and the test asserts the customer's business_unit_id
    is UNCHANGED afterwards, which is the actual claim. It also asserts the missing
    product is NAMED, because a refusal an operator cannot act on is barely better than
    a 500.
  * Un-mapping a mapped customer is refused with a 409. It is a de-configuration with
    no data-feed fix, not a mapping correction.

COMBINED PATCH
  * Both fields in one call recompute EXACTLY ONCE (`recomputes_performed == 1`) and
    both changes land. Two passes would briefly store an intermediate state -- the new
    BU under the old policy -- that nobody asked for.

VALIDATION
  * A bogus enum value and a nonexistent BU id are both clean 400s naming what is
    valid, never a raw DB error and never Pydantic's generic 422.
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
    CoverageStatus,
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
    """A TestClient bound to the test's own session, matching the house pattern."""

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _product(db, *, pid, description):
    product = Product(
        id=pid,
        description=description,
        type="CSG",
        size="9-5/8",
        weight=53.5,
        grade="P110",
        grade_type="Carbon",
        connection="VAM TOP",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    return product


def _world(db, *, policy=AllocationPolicy.SOFT, mapped=True, on_hand=5000.0):
    """One BU with stock, one customer, one well, one line for 4000 ft.

    Under SOFT the 5000 on hand covers the 4000 demanded, so the well is Covered. Under
    HARD nothing is assigned to the line, so it is not -- which is the flip the policy
    tests need, and it is a property of the DATA rather than of any assertion.
    """
    bu = BusinessUnit(id="bu-home", name="Tubular Home")
    db.add(bu)
    product = _product(db, pid="p-main", description="9-5/8 53.5 P110 VAM TOP")
    db.add(
        InventoryOnHand(
            business_unit_id=bu.id, product_id=product.id, quantity=on_hand
        )
    )
    customer = Customer(
        id="cust-1",
        name="Acme Drilling",
        business_unit_id=bu.id if mapped else None,
        allocation_policy=policy,
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
# Business Unit creation
# ---------------------------------------------------------------------------


def test_creating_a_business_unit_succeeds_and_warns_that_it_holds_no_inventory(
    client, db_session
):
    _world(db_session)

    res = client.post("/business-units", json={"name": "Tubular Offshore"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["business_unit"]["name"] == "Tubular Offshore"
    assert body["business_unit"]["id"]
    assert body["inventory_on_hand_row_count"] == 0
    # The warning is the point of the response body, not decoration: a new BU looks
    # configured and holds no steel, so mapping a customer into it now is the remap the
    # PATCH endpoint refuses.
    assert "InventoryOnHand" in body["note"]
    assert "REFUSED" in body["note"]

    listed = client.get("/business-units").json()
    assert [u["name"] for u in listed] == ["Tubular Home", "Tubular Offshore"]


def test_duplicate_business_unit_name_is_a_clean_409_even_in_a_different_case(
    client, db_session
):
    _world(db_session)

    # Different case, so the DATABASE constraint would happily store it. The endpoint
    # is deliberately stricter: two BUs a human cannot tell apart sit one click from a
    # remap into the wrong warehouse.
    res = client.post("/business-units", json={"name": "tubular HOME"})
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert "Tubular Home" in detail
    assert "case-insensitive" in detail
    assert "Nothing was saved" in detail
    # And nothing was.
    assert db_session.query(BusinessUnit).count() == 1


def test_blank_business_unit_name_is_a_400(client, db_session):
    _world(db_session)
    res = client.post("/business-units", json={"name": "   "})
    assert res.status_code == 400, res.text
    assert "only handle a human has on it" in res.json()["detail"]
    assert db_session.query(BusinessUnit).count() == 1


# ---------------------------------------------------------------------------
# Allocation policy -- the recompute must be REAL
# ---------------------------------------------------------------------------


def test_soft_to_hard_genuinely_flips_a_wells_coverage_and_is_reported(
    client, db_session
):
    """The load-bearing test for "the recompute is not cosmetic".

    5000 ft on hand, 4000 ft demanded, NO InventoryAssignment. Under SOFT the pool
    covers the line; under HARD coverage comes only from a physical assignment, so the
    identical inventory and identical demand produce the opposite verdict.
    """
    _bu, customer, well, _prod = _world(db_session, policy=AllocationPolicy.SOFT)
    assert well.coverage_status == CoverageStatus.COVERED.value

    res = client.patch(
        f"/customers/{customer.id}", json={"allocation_policy": "hard"}
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["allocation_policy_changed"] is True
    assert body["business_unit_changed"] is False
    assert body["allocation_policy_before"] == "soft"
    assert body["allocation_policy_after"] == "hard"
    assert body["customer"]["allocation_policy"] == "hard"
    assert body["recomputes_performed"] == 1
    assert body["wells_examined"] == 1
    assert body["coverage_resolvable_before"] is True
    assert body["unresolved_reason"] is None

    # The well-by-well before/after, in the shape the substitution-admin delete report
    # uses.
    assert len(body["well_changes"]) == 1
    change = body["well_changes"][0]
    assert change["well_id"] == "well-1"
    assert change["well_name"] == "A-01"
    assert change["coverage_before"] == CoverageStatus.COVERED.value
    assert change["coverage_after"] != CoverageStatus.COVERED.value

    # STORED state, not just the report: the verdict was rewritten in the database.
    db_session.expire_all()
    assert db_session.get(Well, "well-1").coverage_status != CoverageStatus.COVERED.value
    assert (
        db_session.get(Customer, customer.id).allocation_policy
        == AllocationPolicy.HARD
    )


def test_hard_back_to_soft_restores_the_coverage(client, db_session):
    """Not a one-way door. Pins that the recompute is a re-derivation from current data
    rather than an accumulating edit."""
    _bu, customer, _well, _prod = _world(db_session, policy=AllocationPolicy.HARD)
    db_session.expire_all()
    assert db_session.get(Well, "well-1").coverage_status != CoverageStatus.COVERED.value

    res = client.patch(
        f"/customers/{customer.id}", json={"allocation_policy": "soft"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["well_changes"][0]["coverage_after"] == CoverageStatus.COVERED.value
    db_session.expire_all()
    assert db_session.get(Well, "well-1").coverage_status == CoverageStatus.COVERED.value


def test_patching_the_policy_already_in_force_is_an_explicit_no_op(client, db_session):
    _bu, customer, _well, _prod = _world(db_session, policy=AllocationPolicy.SOFT)
    res = client.patch(
        f"/customers/{customer.id}", json={"allocation_policy": "soft"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["unchanged"] is True
    # Nothing was recomputed, and the note says so rather than showing a diff that
    # would teach an operator to distrust the report.
    assert body["recomputes_performed"] == 0
    assert body["well_changes"] == []
    assert "No change made" in body["note"]


def test_an_empty_patch_body_is_refused_rather_than_treated_as_a_no_op(
    client, db_session
):
    _bu, customer, _well, _prod = _world(db_session)
    res = client.patch(f"/customers/{customer.id}", json={})
    assert res.status_code == 400, res.text
    assert "nothing to update" in res.json()["detail"]


# ---------------------------------------------------------------------------
# BU remap
# ---------------------------------------------------------------------------


def test_remapping_an_unmapped_customer_into_a_bu_computes_coverage_for_the_first_time(
    client, db_session
):
    """The "onboard this customer properly" case.

    An unmapped customer has NO inventory pool, so it has no coverage at all -- every
    read for it raises InventoryScopeMissing. Mapping it is what gives it one, and the
    report's `coverage_resolvable_before: false` is what stops an empty before-column
    reading as "it used to be uncovered".
    """
    bu, customer, well, _prod = _world(db_session, mapped=False)
    assert customer.business_unit_id is None
    assert well.coverage_status is None

    res = client.patch(
        f"/customers/{customer.id}", json={"business_unit_id": bu.id}
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["business_unit_changed"] is True
    assert body["business_unit_id_before"] is None
    assert body["business_unit_name_before"] is None
    assert body["business_unit_id_after"] == bu.id
    assert body["business_unit_name_after"] == "Tubular Home"
    assert body["allocation_policy_changed"] is False
    assert body["recomputes_performed"] == 1
    assert body["coverage_resolvable_before"] is False
    assert body["unresolved_reason"] is None

    assert len(body["well_changes"]) == 1
    assert body["well_changes"][0]["coverage_before"] is None
    assert body["well_changes"][0]["coverage_after"] == CoverageStatus.COVERED.value

    db_session.expire_all()
    assert db_session.get(Customer, customer.id).business_unit_id == bu.id
    assert db_session.get(Well, "well-1").coverage_status == CoverageStatus.COVERED.value


def test_a_remap_that_would_strand_the_customer_is_refused_and_rolled_back(
    client, db_session
):
    """THE test for the rollback decision.

    The destination BU exists and holds inventory -- of a DIFFERENT product. Nothing
    follows the customer across a BU boundary, so the product it actually demands has
    no InventoryOnHand row there, the coverage pass raises InventoryRowMissing, and
    there is no honest verdict to store.

    The remap is rolled back rather than completed-and-reported, because a customer
    sitting under a BU whose inventory cannot answer for its demand looks correctly
    configured on every screen while every coverage read for it fails.
    """
    _bu, customer, well, _prod = _world(db_session)
    before_status = well.coverage_status
    assert before_status == CoverageStatus.COVERED.value

    other = BusinessUnit(id="bu-empty", name="Tubular Greenfield")
    db_session.add(other)
    unrelated = _product(db_session, pid="p-other", description="7 29 L80 VAM 21")
    db_session.add(
        InventoryOnHand(
            business_unit_id=other.id, product_id=unrelated.id, quantity=9999.0
        )
    )
    db_session.commit()

    res = client.patch(
        f"/customers/{customer.id}", json={"business_unit_id": other.id}
    )
    assert res.status_code == 424, res.text
    payload = res.json()["detail"]
    assert payload["error"] == "remap_unresolvable"
    assert payload["business_unit_id"] == other.id
    # The refusal is ACTIONABLE: it names the product whose (BU, product) row the feed
    # still owes, not merely that something failed.
    assert payload["missing_product_ids"] == ["p-main"]
    assert "9-5/8 53.5 P110 VAM TOP" in payload["detail"]
    assert "Tubular Greenfield" in payload["detail"]
    assert "rolled back" in payload["detail"]

    # THE ACTUAL CLAIM: the customer was not left half-moved, and its verdicts stand.
    db_session.expire_all()
    assert db_session.get(Customer, customer.id).business_unit_id == "bu-home"
    assert db_session.get(Well, "well-1").coverage_status == before_status
    # And the API agrees on a fresh read.
    assert client.get(f"/customers/{customer.id}").json()["business_unit_id"] == "bu-home"


def test_unmapping_a_mapped_customer_is_refused(client, db_session):
    """Explicitly null business_unit_id on a MAPPED customer -> 409, nothing saved.

    Not a mapping correction: an unmapped customer has no inventory pool at all, so
    every coverage read, MRP figure and dashboard tile for it fails -- and unlike a
    missing inventory row, nothing anybody could load would fix it, because there would
    be no pool to load it into.
    """
    _bu, customer, _well, _prod = _world(db_session)
    res = client.patch(f"/customers/{customer.id}", json={"business_unit_id": None})
    assert res.status_code == 409, res.text
    payload = res.json()["detail"]
    assert payload["error"] == "unmap_refused"
    assert "not yet mapped" in payload["detail"]
    assert "Nothing was saved" in payload["detail"]

    db_session.expire_all()
    assert db_session.get(Customer, customer.id).business_unit_id == "bu-home"


def test_a_policy_change_on_an_unmapped_customer_is_performed_and_reports_the_caveat(
    client, db_session
):
    """The deliberate ASYMMETRY with the remap refusal.

    An unmapped customer's coverage cannot be resolved at all. A policy change cannot be
    the cause -- the products a pass must resolve are identical under all three policies
    -- and refusing would leave `allocation_policy` permanently uneditable for exactly
    the customers whose configuration most needs fixing, including setting the policy
    correctly BEFORE mapping.
    """
    _bu, customer, _well, _prod = _world(db_session, mapped=False)
    res = client.patch(f"/customers/{customer.id}", json={"allocation_policy": "hybrid"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["allocation_policy_after"] == "hybrid"
    assert body["coverage_resolvable_before"] is False
    # The change landed AND the failure is reported, so an empty well_changes cannot be
    # misread as "nothing moved".
    assert body["unresolved_reason"] is not None
    assert "no Business Unit was supplied" in body["unresolved_reason"]
    assert "CAVEAT" in body["note"]

    db_session.expire_all()
    assert (
        db_session.get(Customer, customer.id).allocation_policy
        == AllocationPolicy.HYBRID
    )


# ---------------------------------------------------------------------------
# Both fields at once
# ---------------------------------------------------------------------------


def test_changing_both_fields_in_one_patch_recomputes_exactly_once(client, db_session):
    """One endpoint, one pass. Two passes would briefly store an intermediate state --
    the new BU under the old policy -- that nobody asked for."""
    bu, customer, well, product = _world(db_session, mapped=False)
    assert well.coverage_status is None

    # A second, fully-stocked BU so the remap can succeed and the assertions are about
    # the recompute count rather than about a refusal.
    dest = BusinessUnit(id="bu-dest", name="Tubular Second")
    db_session.add(dest)
    db_session.add(
        InventoryOnHand(
            business_unit_id=dest.id, product_id=product.id, quantity=5000.0
        )
    )
    db_session.commit()

    res = client.patch(
        f"/customers/{customer.id}",
        json={"business_unit_id": dest.id, "allocation_policy": "hard"},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["business_unit_changed"] is True
    assert body["allocation_policy_changed"] is True
    # THE CLAIM: one pass, not two.
    assert body["recomputes_performed"] == 1
    assert body["business_unit_id_after"] == dest.id
    assert body["allocation_policy_after"] == "hard"
    # HARD with no assignment covers nothing, so the well did NOT become Covered even
    # though the destination holds ample stock -- proof the pass ran under the NEW
    # policy and the NEW pool together, never an intermediate combination.
    assert body["well_changes"] == [] or (
        body["well_changes"][0]["coverage_after"] != CoverageStatus.COVERED.value
    )
    db_session.expire_all()
    stored = db_session.get(Customer, customer.id)
    assert stored.business_unit_id == dest.id
    assert stored.allocation_policy == AllocationPolicy.HARD
    assert db_session.get(Well, "well-1").coverage_status != CoverageStatus.COVERED.value


# ---------------------------------------------------------------------------
# Validation -- clean 400s, never a raw DB error and never a generic 422
# ---------------------------------------------------------------------------


def test_an_invalid_allocation_policy_is_a_400_naming_the_three_valid_values(
    client, db_session
):
    _bu, customer, _well, _prod = _world(db_session)
    res = client.patch(
        f"/customers/{customer.id}", json={"allocation_policy": "aggressive"}
    )
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert "'aggressive' is not an allocation policy" in detail
    for value in ("soft", "hard", "hybrid"):
        assert value in detail
    # And it says what each one MEANS, which a 422 could not.
    assert "earliest-ROS" in detail
    db_session.expire_all()
    assert (
        db_session.get(Customer, customer.id).allocation_policy == AllocationPolicy.SOFT
    )


def test_a_nonexistent_business_unit_id_is_a_400_not_an_integrity_error(
    client, db_session
):
    _bu, customer, _well, _prod = _world(db_session)
    res = client.patch(
        f"/customers/{customer.id}", json={"business_unit_id": "bu-does-not-exist"}
    )
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert "is not a Business Unit on this platform" in detail
    assert "Nothing was saved" in detail
    db_session.expire_all()
    assert db_session.get(Customer, customer.id).business_unit_id == "bu-home"


def test_a_null_allocation_policy_is_a_400(client, db_session):
    _bu, customer, _well, _prod = _world(db_session)
    res = client.patch(f"/customers/{customer.id}", json={"allocation_policy": None})
    assert res.status_code == 400, res.text
    assert "cannot be null" in res.json()["detail"]


def test_patching_an_unknown_customer_is_a_404(client, db_session):
    _world(db_session)
    res = client.patch("/customers/nope", json={"allocation_policy": "hard"})
    assert res.status_code == 404, res.text
