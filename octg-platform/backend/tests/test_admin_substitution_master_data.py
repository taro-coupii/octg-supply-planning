"""Admin CRUD for the two substitution MASTER-DATA tables.

A planner asked "where can I register a substitution item?" and the answer was
nowhere: `TechnicalSubstitution` (layer 1) and `CustomerSubstitutionRule` (layer 2)
were seed-only rows with no API and no UI. Everything below is new surface, and each
test pins one claim that could plausibly be got wrong.

WHAT THIS MODULE PINS, AND WHY EACH ONE IS HERE
===============================================
TECHNICAL SUBSTITUTIONS
  * CRUD happy path through HTTP, including that the list joins Product and serves
    DESCRIPTIONS -- this API has a standing rule against bare uuids where a description
    exists, and a picker of raw ids is unusable by the person it is for.
  * `from == to` is refused with a 400 that says WHY. A self-substitution is not a
    weak claim but a meaningless one: it would make `find_candidates` offer a demand
    line its own product back, presenting one quantity of steel as both primary
    coverage and substitute availability.
  * A duplicate ordered pair is a CLEAN 409 naming the existing row, not a raw
    `IntegrityError` and not a 500. Same guarantee the lead-time endpoints make.
  * The reverse direction is still creatable after that 409. Directionality is
    documented on the model, so it gets a test rather than living only in a docstring.
  * THE ONE THAT MATTERS MOST: deleting a technical substitution that an APPROVED
    well-layer approval depends on must not crash `find_candidates` on the next read.
    The approval row is deliberately kept as history, so it is left DANGLING -- and
    this test is what keeps the "derive the candidate set from the technical rows"
    structure in `find_candidates` intact. A future refactor that iterated approvals
    and looked up their technical row would fail here rather than 500 in production.

CUSTOMER SUBSTITUTION RULES
  * CRUD happy path including the PATCH toggle, which exists as a separate verb
    because flipping a permission is a normal commercial change and delete-then-create
    would pass through "no row" -- a third state with different provenance.
  * A duplicate (customer, from, to) is a clean 409 that points at PATCH.
  * A rule for a pair with NO technical substitution is ACCEPTED WITH A WARNING, not
    refused. That is a decision (see `app.api.admin.create_customer_substitution_rule`
    for the three reasons), so the test names it and asserts both halves: the row is
    written, and the response says it is inert and why.
  * DELETING an allowing rule REVERTS THE SUBSTITUTION TO BLOCKED -- verified through
    `find_candidates` and the stored coverage verdict, not merely by observing the row
    is gone. Layer 2 is a true allow-list, so absence is not neutrality, and that is
    the single easiest thing about this feature to get wrong.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.engines.coverage import recompute_well
from app.engines.substitution import (
    BLOCK_CUSTOMER,
    decide_approval,
    find_candidates,
    request_approval,
)
from app.main import app
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    CoverageStatus,
    Customer,
    CustomerSubstitutionRule,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    PlanningNode,
    Product,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    UnitOfMeasure,
    Well,
    WellSubstitutionApproval,
)


@pytest.fixture()
def client(db_session):
    """A TestClient bound to the test's own session, matching the house pattern."""

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _world(db, *, line_qty=4000.0, primary_on_hand=1000.0, substitute_on_hand=5000.0):
    """One customer, one Confirmed/Primary line whose own product cannot cover it,
    plus a substitute product that could -- and a third product with no relationship
    to either, so "unrelated" cases have something real to point at.

    Mirrors `tests.test_substitution_engine._scenario` on purpose: the engine tests and
    these API tests must be reasoning about the same world, or a difference between
    them would be indistinguishable from a bug.
    """
    bu = BusinessUnit(name="Sub Admin BU")
    db.add(bu)
    db.flush()
    customer = Customer(
        name="Sub Admin Co",
        allocation_policy=AllocationPolicy.SOFT,
        business_unit_id=bu.id,
    )
    db.add(customer)
    db.flush()
    node = PlanningNode(
        customer_id=customer.id, node_type="Campaign", name="Sub Admin Campaign"
    )
    db.add(node)
    db.flush()

    def _product(size, weight, grade, description):
        return Product(
            unit_of_measure=UnitOfMeasure.MTR,
            type="CSG",
            size=size,
            weight=weight,
            grade=grade,
            grade_type="Carbon",
            connection="VAM 21",
            description=description,
        )

    primary = _product("9-5/8", 53.5, "P110", "CSG 9-5/8 53.5 P110 VAM 21")
    substitute = _product("9-5/8", 58.4, "Q125", "CSG 9-5/8 58.4 Q125 VAM 21")
    other = _product("7", 29.0, "L80", "CSG 7 29.0 L80 VAM 21")
    db.add_all([primary, substitute, other])
    db.flush()
    db.add_all(
        [
            InventoryOnHand(
                business_unit_id=bu.id,
                product_id=primary.id,
                quantity=primary_on_hand,
                source_system="synthetic",
            ),
            InventoryOnHand(
                business_unit_id=bu.id,
                product_id=substitute.id,
                quantity=substitute_on_hand,
                source_system="synthetic",
            ),
            InventoryOnHand(
                business_unit_id=bu.id,
                product_id=other.id,
                quantity=0.0,
                source_system="synthetic",
            ),
        ]
    )
    well = Well(
        planning_node_id=node.id,
        name="WELL-SUBADM-01",
        demand_status=DemandStatus.CONFIRMED,
    )
    db.add(well)
    db.flush()
    line = DemandLine(
        well_id=well.id,
        product_id=primary.id,
        quantity=line_qty,
        ros_date=datetime.utcnow() + timedelta(days=400),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.flush()
    return customer, well, line, primary, substitute, other


# ---------------------------------------------------------------------------
# Technical substitutions
# ---------------------------------------------------------------------------


def test_technical_substitution_crud_happy_path(client, db_session):
    _customer, _well, _line, primary, substitute, _other = _world(db_session)

    empty = client.get("/admin/technical-substitutions")
    assert empty.status_code == 200
    assert empty.json()["substitutions"] == []

    created = client.post(
        "/admin/technical-substitutions",
        json={"from_product_id": primary.id, "to_product_id": substitute.id},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["action"] == "created"
    row = body["substitution"]
    assert row["from_product_id"] == primary.id
    assert row["to_product_id"] == substitute.id
    # Descriptions, not bare uuids. The standing rule for this API.
    assert row["from_product_description"] == primary.description
    assert row["to_product_description"] == substitute.description
    # Layer 1 alone grants nothing, and the note has to say so or the screen implies
    # otherwise.
    assert "No customer rule permits this pair yet" in body["note"]

    listed = client.get("/admin/technical-substitutions").json()
    assert len(listed["substitutions"]) == 1
    assert listed["substitutions"][0]["id"] == row["id"]
    assert listed["substitutions"][0]["from_product_description"] == primary.description
    # A technical row nobody permits is counted, because the table looks configured and
    # is not.
    assert listed["unpermitted_count"] == 1

    deleted = client.request(
        "DELETE", f"/admin/technical-substitutions/{row['id']}"
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["action"] == "deleted"
    # Null on a delete -- the row is gone and echoing it back would invite a client to
    # render it as though it still existed.
    assert deleted.json()["substitution"] is None
    assert client.get("/admin/technical-substitutions").json()["substitutions"] == []

    assert client.request(
        "DELETE", f"/admin/technical-substitutions/{row['id']}"
    ).status_code == 404


def test_self_substitution_is_refused_with_a_stated_reason(client, db_session):
    _c, _w, _l, primary, _substitute, _other = _world(db_session)

    res = client.post(
        "/admin/technical-substitutions",
        json={"from_product_id": primary.id, "to_product_id": primary.id},
    )
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    # Not merely "not 2xx": the refusal must EXPLAIN, which is this codebase's
    # convention and the reason the check is hand-written rather than a Pydantic
    # validator.
    assert "cannot be registered as a substitute for itself" in detail
    assert "Nothing was saved" in detail
    assert db_session.query(TechnicalSubstitution).count() == 0


def test_duplicate_technical_pair_gives_clean_409_naming_the_existing_row(
    client, db_session
):
    _c, _w, _l, primary, substitute, _other = _world(db_session)
    first = client.post(
        "/admin/technical-substitutions",
        json={"from_product_id": primary.id, "to_product_id": substitute.id},
    )
    assert first.status_code == 201
    existing_id = first.json()["substitution"]["id"]

    dup = client.post(
        "/admin/technical-substitutions",
        json={"from_product_id": primary.id, "to_product_id": substitute.id},
    )
    assert dup.status_code == 409, dup.text
    detail = dup.json()["detail"]
    # The existing row is NAMED -- an operator has to be able to go and look at it.
    assert existing_id in detail
    assert primary.description in detail
    assert substitute.description in detail
    assert "Nothing was saved" in detail
    # And it is a clean 409, not a leaked driver message.
    assert "IntegrityError" not in detail and "UNIQUE constraint" not in detail
    assert db_session.query(TechnicalSubstitution).count() == 1


def test_reverse_direction_is_still_creatable(client, db_session):
    """A -> B does not imply B -> A, so the 409 must not have blocked the reverse.

    The model documents the directionality; without this test a future "helpful"
    unordered-pair constraint would silently forbid a real reciprocal compatibility.
    """
    _c, _w, _l, primary, substitute, _other = _world(db_session)
    client.post(
        "/admin/technical-substitutions",
        json={"from_product_id": primary.id, "to_product_id": substitute.id},
    )
    reverse = client.post(
        "/admin/technical-substitutions",
        json={"from_product_id": substitute.id, "to_product_id": primary.id},
    )
    assert reverse.status_code == 201, reverse.text
    assert db_session.query(TechnicalSubstitution).count() == 2


def test_unknown_product_is_refused_with_400_not_a_foreign_key_error(
    client, db_session
):
    _c, _w, _l, primary, _substitute, _other = _world(db_session)
    res = client.post(
        "/admin/technical-substitutions",
        json={"from_product_id": primary.id, "to_product_id": "no-such-product"},
    )
    assert res.status_code == 400, res.text
    assert "is not a product on this platform" in res.json()["detail"]


def test_technical_delete_reports_the_approvals_that_relied_on_it(client, db_session):
    """The consequence report, following the lead-time DELETE's discipline."""
    customer, well, line, primary, substitute, _other = _world(db_session)
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.add(
        CustomerSubstitutionRule(
            customer_id=customer.id,
            from_product_id=primary.id,
            to_product_id=substitute.id,
            allowed=True,
        )
    )
    db_session.flush()
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)
    recompute_well(db_session, well)
    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE

    tech_id = (
        db_session.query(TechnicalSubstitution)
        .filter(TechnicalSubstitution.from_product_id == primary.id)
        .one()
        .id
    )
    res = client.request("DELETE", f"/admin/technical-substitutions/{tech_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["well_approvals_affected"] == 1
    assert body["approved_well_approvals_affected"] == 1
    assert body["customer_rules_affected"] == 1
    assert body["allowing_customer_rules_affected"] == 1
    assert "were NOT deleted" in body["note"]
    # Coverage was repaired IN THIS REQUEST -- a stored CoveredViaSubstitute verdict
    # resting on a withdrawn technical claim would be misinformation, not merely stale.
    assert body["recomputed_customer_ids"] == [customer.id]
    assert body["wells_examined"] == 1
    moved = {w["well_id"]: w for w in body["well_changes"]}
    assert well.id in moved
    assert moved[well.id]["coverage_after"] == CoverageStatus.UNCOVERED.value
    db_session.refresh(line.coverage_result)
    assert line.coverage_result.status == CoverageStatus.UNCOVERED


def test_deleting_a_technical_row_leaves_a_dangling_approval_that_cannot_crash(
    client, db_session
):
    """A withdrawn technical claim under an APPROVED approval: `find_candidates` must
    stay graceful on the next read.

    The approval row is deliberately KEPT (it records a decision the customer actually
    made) so it is genuinely dangling afterwards. Verified by reading
    `find_candidates` first: it derives `to_ids` from the technical rows and both
    queries approvals within that set AND iterates over the technical rows, so an
    approval outside the set is never visited. NO fix was needed. This test is what
    keeps that structure -- a refactor that iterated approvals and looked up their
    technical row would raise or return a candidate with `product=None` here.
    """
    customer, well, line, primary, substitute, _other = _world(db_session)
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.add(
        CustomerSubstitutionRule(
            customer_id=customer.id,
            from_product_id=primary.id,
            to_product_id=substitute.id,
            allowed=True,
        )
    )
    db_session.flush()
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)
    recompute_well(db_session, well)

    tech_id = (
        db_session.query(TechnicalSubstitution)
        .filter(TechnicalSubstitution.from_product_id == primary.id)
        .one()
        .id
    )
    assert (
        client.request("DELETE", f"/admin/technical-substitutions/{tech_id}").status_code
        == 200
    )

    # The approval survives, still Approved, and is now dangling.
    surviving = db_session.query(WellSubstitutionApproval).all()
    assert len(surviving) == 1
    assert surviving[0].status == SubstitutionApprovalStatus.APPROVED
    assert (
        db_session.query(TechnicalSubstitution)
        .filter(
            TechnicalSubstitution.from_product_id == primary.id,
            TechnicalSubstitution.to_product_id == substitute.id,
        )
        .first()
        is None
    )

    # The next read: no exception, and an empty candidate list rather than a half-built
    # candidate. Both the engine directly and the HTTP endpoint.
    assert find_candidates(db_session, line) == []
    api_read = client.get(f"/demand-lines/{line.id}/substitution-candidates")
    assert api_read.status_code == 200, api_read.text
    assert api_read.json() == []

    # And re-registering the pair brings the customer's decision back to life, which is
    # the behaviour the "keep it as history" choice implies.
    again = client.post(
        "/admin/technical-substitutions",
        json={"from_product_id": primary.id, "to_product_id": substitute.id},
    )
    assert again.status_code == 201, again.text
    assert again.json()["approved_well_approvals_affected"] == 1
    candidates = find_candidates(db_session, line)
    assert len(candidates) == 1
    assert candidates[0].usable is True


# ---------------------------------------------------------------------------
# Customer substitution rules
# ---------------------------------------------------------------------------


def test_customer_rule_crud_happy_path_including_the_patch_toggle(client, db_session):
    customer, _well, _line, primary, substitute, _other = _world(db_session)
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.flush()

    created = client.post(
        "/admin/customer-substitution-rules",
        json={
            "customer_id": customer.id,
            "from_product_id": primary.id,
            "to_product_id": substitute.id,
            "allowed": False,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["action"] == "created"
    # Null means "the row did not exist on that side", never a third permission value.
    assert body["allowed_before"] is None
    assert body["allowed_after"] is False
    # The pair HAS a technical row, so no inert warning.
    assert body["warning"] is None
    rule_id = body["rule"]["id"]
    assert body["rule"]["customer_name"] == customer.name
    assert body["rule"]["from_product_description"] == primary.description
    assert body["rule"]["to_product_description"] == substitute.description
    assert body["rule"]["technical_substitution_exists"] is True

    listed = client.get(
        f"/admin/customer-substitution-rules?customer_id={customer.id}"
    ).json()
    assert listed["customer_id"] == customer.id
    assert [r["id"] for r in listed["rules"]] == [rule_id]
    assert listed["inert_count"] == 0
    # Unfiltered sees the same row.
    assert len(client.get("/admin/customer-substitution-rules").json()["rules"]) == 1

    # The toggle: a normal commercial change, hence its own verb.
    flipped = client.patch(
        f"/admin/customer-substitution-rules/{rule_id}", json={"allowed": True}
    )
    assert flipped.status_code == 200, flipped.text
    assert flipped.json()["action"] == "updated"
    assert flipped.json()["allowed_before"] is False
    assert flipped.json()["allowed_after"] is True
    assert "now PERMITS" in flipped.json()["note"]
    db_session.refresh(db_session.get(CustomerSubstitutionRule, rule_id))
    assert db_session.get(CustomerSubstitutionRule, rule_id).allowed is True

    # And back again, because a veto is a legitimate destination too.
    back = client.patch(
        f"/admin/customer-substitution-rules/{rule_id}", json={"allowed": False}
    )
    assert back.status_code == 200
    assert back.json()["allowed_before"] is True
    assert back.json()["allowed_after"] is False

    gone = client.request("DELETE", f"/admin/customer-substitution-rules/{rule_id}")
    assert gone.status_code == 200, gone.text
    assert gone.json()["action"] == "deleted"
    assert gone.json()["allowed_before"] is False
    assert gone.json()["allowed_after"] is None
    assert gone.json()["rule"] is None
    assert db_session.query(CustomerSubstitutionRule).count() == 0
    assert (
        client.patch(
            f"/admin/customer-substitution-rules/{rule_id}", json={"allowed": True}
        ).status_code
        == 404
    )


def test_duplicate_customer_rule_gives_clean_409_pointing_at_patch(client, db_session):
    customer, _well, _line, primary, substitute, _other = _world(db_session)
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.flush()
    payload = {
        "customer_id": customer.id,
        "from_product_id": primary.id,
        "to_product_id": substitute.id,
        "allowed": True,
    }
    first = client.post("/admin/customer-substitution-rules", json=payload)
    assert first.status_code == 201
    existing_id = first.json()["rule"]["id"]

    dup = client.post(
        "/admin/customer-substitution-rules", json={**payload, "allowed": False}
    )
    assert dup.status_code == 409, dup.text
    detail = dup.json()["detail"]
    assert existing_id in detail
    assert customer.name in detail
    # The 409 must offer the correct alternative, or an operator's only route is
    # delete-then-create -- which passes through "no row" and changes the verdict on the
    # way.
    assert f"PATCH /admin/customer-substitution-rules/{existing_id}" in detail
    assert "IntegrityError" not in detail and "UNIQUE constraint" not in detail
    assert db_session.query(CustomerSubstitutionRule).count() == 1
    # The refused body must not have flipped the existing row either.
    assert db_session.get(CustomerSubstitutionRule, existing_id).allowed is True


def test_rule_without_a_technical_substitution_is_ACCEPTED_WITH_A_WARNING(
    client, db_session
):
    """THE DECISION: a layer-2 rule for a pair with no layer-1 claim is SAVED, and
    flagged as inert. It is not refused.

    Three reasons, in `app.api.admin.create_customer_substitution_rule`. The load-bearing
    one is that the sibling write on this model already behaves this way --
    `app.engines.substitution.request_approval` accepts a layer-3 request "even when the
    technical/customer layers do not clear", because the layers are documented as
    independent. Enforcing a layer-1 prerequisite here and not there would be two
    endpoints disagreeing about the model. The hazard is INVISIBILITY, not
    incorrectness: `find_candidates` never reads such a rule, so it cannot produce a
    wrong verdict -- it can only look like configuration while doing nothing, and this
    codebase answers that with a loud statement rather than a refusal.
    """
    customer, _well, _line, primary, _substitute, other = _world(db_session)
    # No TechnicalSubstitution for (primary, other) anywhere.
    res = client.post(
        "/admin/customer-substitution-rules",
        json={
            "customer_id": customer.id,
            "from_product_id": primary.id,
            "to_product_id": other.id,
            "allowed": True,
        },
    )
    # SAVED.
    assert res.status_code == 201, res.text
    body = res.json()
    assert db_session.query(CustomerSubstitutionRule).count() == 1

    # AND flagged, in three places a client might read.
    assert body["warning"] is not None
    assert "NO EFFECT" in body["warning"]
    assert "no technical substitution is registered" in body["warning"]
    assert body["rule"]["technical_substitution_exists"] is False
    assert body["rule"]["inert_reason"] is not None

    listed = client.get("/admin/customer-substitution-rules").json()
    assert listed["inert_count"] == 1
    assert "have no effect at all" in listed["note"]

    # And it really is inert: the engine offers nothing.
    assert find_candidates(db_session, _line) == []


def test_deleting_an_allowing_rule_reverts_the_substitution_to_blocked(
    client, db_session
):
    """Layer 2 is a TRUE ALLOW-LIST, so removing a permission is not a cleanup.

    Verified through `find_candidates` and the STORED coverage verdict rather than by
    observing the row is gone -- "the row was deleted" would pass even if absence were
    treated as neutral, which is precisely the bug this asserts against.
    """
    customer, well, line, primary, substitute, _other = _world(db_session)
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.flush()
    rule_id = client.post(
        "/admin/customer-substitution-rules",
        json={
            "customer_id": customer.id,
            "from_product_id": primary.id,
            "to_product_id": substitute.id,
            "allowed": True,
        },
    ).json()["rule"]["id"]
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)
    recompute_well(db_session, well)

    # All three layers clear: usable, and stored as covered via the substitute.
    before = find_candidates(db_session, line)
    assert len(before) == 1
    assert before[0].usable is True
    assert before[0].customer_allowed is True
    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE

    res = client.request("DELETE", f"/admin/customer-substitution-rules/{rule_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["allowed_before"] is True
    assert body["allowed_after"] is None
    assert "now BLOCKED for this customer" in body["note"]

    # The candidate is STILL OFFERED (layer 1 stands) but is blocked at the customer
    # layer -- absence and an explicit veto reach the same verdict.
    after = find_candidates(db_session, line)
    assert len(after) == 1
    assert after[0].customer_allowed is False
    assert after[0].usable is False
    assert after[0].blocking_layer == BLOCK_CUSTOMER

    # And the STORED verdict was repaired in the same request.
    moved = {w["well_id"]: w for w in body["well_changes"]}
    assert moved[well.id]["coverage_before"] == CoverageStatus.COVERED.value
    assert moved[well.id]["coverage_after"] == CoverageStatus.UNCOVERED.value
    db_session.refresh(line.coverage_result)
    assert line.coverage_result.status == CoverageStatus.UNCOVERED


def test_patching_allowed_to_true_covers_the_line_via_the_substitute(
    client, db_session
):
    """The toggle's other direction, end to end: a commercial change that COVERS."""
    customer, well, line, primary, substitute, _other = _world(db_session)
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.flush()
    rule_id = client.post(
        "/admin/customer-substitution-rules",
        json={
            "customer_id": customer.id,
            "from_product_id": primary.id,
            "to_product_id": substitute.id,
            "allowed": False,
        },
    ).json()["rule"]["id"]
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)
    recompute_well(db_session, well)
    assert line.coverage_result.status == CoverageStatus.UNCOVERED

    res = client.patch(
        f"/admin/customer-substitution-rules/{rule_id}", json={"allowed": True}
    )
    assert res.status_code == 200, res.text
    moved = {w["well_id"]: w for w in res.json()["well_changes"]}
    assert moved[well.id]["coverage_after"] == CoverageStatus.COVERED.value
    db_session.refresh(line.coverage_result)
    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE


def test_rule_list_for_an_unknown_customer_is_404_not_an_empty_allow_list(
    client, db_session
):
    """An empty allow-list is a real and strong claim ("nothing is permitted"), so a
    typo'd customer must not be served as one."""
    _world(db_session)
    res = client.get("/admin/customer-substitution-rules?customer_id=no-such-customer")
    assert res.status_code == 404, res.text
    assert "not found" in res.json()["detail"]


def test_rule_for_an_unknown_customer_is_refused(client, db_session):
    _c, _w, _l, primary, substitute, _other = _world(db_session)
    res = client.post(
        "/admin/customer-substitution-rules",
        json={
            "customer_id": "no-such-customer",
            "from_product_id": primary.id,
            "to_product_id": substitute.id,
            "allowed": True,
        },
    )
    assert res.status_code == 400, res.text
    assert "is not a customer on this platform" in res.json()["detail"]


def test_a_customer_with_incomplete_inventory_does_not_veto_the_registration(
    client, db_session
):
    """FOUND BY RUNNING IT, not predicted: two seeded products reproduce this.

    A `TechnicalSubstitution` is BU-AGNOSTIC but the recompute it triggers is BU-scoped,
    and `app.engines.inventory` refuses to invent an on-hand quantity. So registering a
    pair adds a substitute TARGET that some Business Unit may hold no inventory row for,
    and the whole POST used to fail with 424 having saved nothing -- one warehouse's
    missing Oracle feed vetoing an engineering fact that applies to every BU, with a
    refusal naming a product the operator never mentioned.

    Now the write PROCEEDS and the un-recomputable customer is REPORTED. Both halves are
    asserted, plus the third thing that matters: that customer's stored verdicts are
    intact rather than half-erased, which is what the per-customer SAVEPOINT buys.

    The refusal itself is NOT weakened anywhere -- no zero is substituted and no verdict
    is guessed for the affected customer. `test_second_bu_without_a_row_still_raises`
    below pins that the engine still raises on the ordinary read path.
    """
    customer, well, line, primary, substitute, _other = _world(db_session)

    # A SECOND customer in its OWN BU, demanding the same primary product, whose BU has
    # an inventory row for the primary but NONE for the substitute. Exactly the seeded
    # shape that produced the 424.
    bu2 = BusinessUnit(name="Incomplete BU")
    db_session.add(bu2)
    db_session.flush()
    other_customer = Customer(
        name="Incomplete Feed Co",
        allocation_policy=AllocationPolicy.SOFT,
        business_unit_id=bu2.id,
    )
    db_session.add(other_customer)
    db_session.flush()
    node2 = PlanningNode(
        customer_id=other_customer.id, node_type="Campaign", name="Incomplete Campaign"
    )
    db_session.add(node2)
    db_session.flush()
    well2 = Well(
        planning_node_id=node2.id,
        name="WELL-INCOMPLETE-01",
        demand_status=DemandStatus.CONFIRMED,
    )
    db_session.add(well2)
    db_session.flush()
    db_session.add(
        InventoryOnHand(
            business_unit_id=bu2.id,
            product_id=primary.id,
            quantity=10.0,
            source_system="synthetic",
        )
    )
    line2 = DemandLine(
        well_id=well2.id,
        product_id=primary.id,
        quantity=500.0,
        ros_date=datetime.utcnow() + timedelta(days=400),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(line2)
    db_session.flush()
    recompute_well(db_session, well2)
    verdict_before = line2.coverage_result.status
    assert verdict_before == CoverageStatus.UNCOVERED

    res = client.post(
        "/admin/technical-substitutions",
        json={"from_product_id": primary.id, "to_product_id": substitute.id},
    )
    # PERFORMED, not refused with the 424 this used to produce.
    assert res.status_code == 201, res.text
    body = res.json()
    assert db_session.query(TechnicalSubstitution).count() == 1

    # AND reported. The failing customer is named, with the engine's own message.
    failures = body["coverage_recompute_failures"]
    assert len(failures) == 1, failures
    assert failures[0]["customer_id"] == other_customer.id
    assert failures[0]["customer_name"] == "Incomplete Feed Co"
    assert substitute.description in failures[0]["reason"]
    assert bu2.id in failures[0]["reason"]
    # The prose carries it too, because the note is what a minimal screen renders.
    assert "could NOT be recomputed" in body["note"]

    # Absence from `recomputed_customer_ids` is explained, never silent.
    assert other_customer.id not in body["recomputed_customer_ids"]
    assert customer.id in body["recomputed_customer_ids"]

    # The SAVEPOINT: that customer's stored verdict is intact, not half-erased.
    db_session.refresh(line2)
    assert line2.coverage_result is not None
    assert line2.coverage_result.status == verdict_before


def test_the_inventory_refusal_itself_is_not_weakened(client, db_session):
    """The per-customer catch must not have turned the refusal into a global zero.

    `app.engines.substitution_admin` catches `InventoryRowMissing` in ONE place, for the
    narrow purpose of not vetoing a master-data write. The ordinary read path must still
    refuse rather than invent a quantity -- otherwise the fix would have traded a
    usability problem for the exact dishonesty this platform is built to avoid.
    """
    from app.engines.inventory import InventoryRowMissing

    customer, _well, line, primary, substitute, _other = _world(db_session)
    db_session.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db_session.flush()
    # Remove the substitute's on-hand row for this customer's own BU.
    db_session.query(InventoryOnHand).filter(
        InventoryOnHand.product_id == substitute.id
    ).delete()
    db_session.flush()

    with pytest.raises(InventoryRowMissing):
        find_candidates(db_session, line)
