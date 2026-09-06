from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.db import enforce_sqlite_foreign_keys
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.engines.coverage import recompute_well
from app.main import app
from app.models import (
    BusinessUnit,
    CoverageResult,
    CoverageStatus,
    Customer,
    CustomerSubstitutionRule,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    PlanningNode,
    Product,
    TechnicalSubstitution,
    UnitOfMeasure,
    Well,
    AllocationPolicy,
)


def _build_client_and_well():
    engine = enforce_sqlite_foreign_keys(create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    ))
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    db = TestingSessionLocal()
    # Quantity exists only per (BU, product), so the fixture needs a BU.
    bu = BusinessUnit(name="Wells BU")
    db.add(bu)
    db.flush()
    customer = Customer(
        name="Test Co",
        allocation_policy=AllocationPolicy.SOFT,
        business_unit_id=bu.id,
    )
    db.add(customer)
    db.flush()
    node = PlanningNode(customer_id=customer.id, node_type="Project", name="P1")
    db.add(node)
    db.flush()
    product = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="TBG", size="4-1/2", grade="13CR80", grade_type="13CR",
        connection="VAM TOP",
    )
    db.add(product)
    db.flush()
    db.add(
        InventoryOnHand(
            business_unit_id=bu.id, product_id=product.id, quantity=10000,
            source_system="synthetic",
        )
    )
    db.flush()
    well = Well(planning_node_id=node.id, name="Well Eagle-01", demand_status=DemandStatus.CONFIRMED)
    db.add(well)
    db.flush()
    line = DemandLine(
        well_id=well.id, product_id=product.id, quantity=5000,
        ros_date=datetime.utcnow() + timedelta(days=10),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.flush()
    recompute_well(db, well)
    db.commit()

    well_id = well.id
    db.close()
    return TestClient(app), well_id


def test_get_well_returns_coverage():
    client, well_id = _build_client_and_well()
    resp = client.get(f"/wells/{well_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["coverage_status"] == "Covered"
    assert len(body["demand_lines"]) == 1
    assert body["demand_lines"][0]["coverage_status"] == "Covered"

    app.dependency_overrides.clear()


def test_get_well_identifies_its_customer_without_a_second_lookup():
    """A well page needs anything scoped by customer -- the cross-customer sharing
    what-if above all -- so the owner has to be on the payload. Without it the UI
    resolved ownership by pulling the ENTIRE coverage grid and finding the matching
    row, i.e. a whole-estate fetch to answer "whose well is this"."""
    client, well_id = _build_client_and_well()
    body = client.get(f"/wells/{well_id}").json()

    assert body["customer_id"] is not None
    assert body["customer_name"] == "Test Co"
    # Same breadcrumb the coverage grid serves, so the two screens cannot disagree
    # about where a well sits.
    assert body["planning_node_path"] == "P1"

    app.dependency_overrides.clear()


def test_get_well_404():
    client, _ = _build_client_and_well()
    resp = client.get("/wells/does-not-exist")
    assert resp.status_code == 404
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Defect 4 -- the API must never serve a coverage status for a line the engine
# no longer evaluates.
# --------------------------------------------------------------------------


def _build_client_with_pending_then_revised_line():
    """A CONFIRMED/PRIMARY line that resolves PendingApproval, then gets revised
    to Planned -- the exact scenario that used to leave a stale PendingApproval
    row visible on GET /wells/{id} and on the Home Dashboard forever."""
    engine = enforce_sqlite_foreign_keys(create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    ))
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    db = TestingSessionLocal()
    bu = BusinessUnit(name="Stale BU")
    db.add(bu)
    db.flush()
    customer = Customer(
        name="Stale Co",
        allocation_policy=AllocationPolicy.SOFT,
        business_unit_id=bu.id,
    )
    db.add(customer)
    db.flush()
    node = PlanningNode(customer_id=customer.id, node_type="Project", name="P-Stale")
    db.add(node)
    db.flush()
    primary = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="CSG", size="9-5/8", grade="P110", grade_type="Carbon",
        connection="VAM 21", description="CSG 9-5/8 P110 VAM 21",
    )
    substitute = Product(
        unit_of_measure=UnitOfMeasure.MTR,
        type="CSG", size="9-5/8", grade="Q125", grade_type="Carbon",
        connection="VAM 21", description="CSG 9-5/8 Q125 VAM 21",
    )
    db.add_all([primary, substitute])
    db.flush()
    db.add_all([
        InventoryOnHand(
            business_unit_id=bu.id, product_id=primary.id, quantity=0,
            source_system="synthetic",
        ),
        InventoryOnHand(
            business_unit_id=bu.id, product_id=substitute.id, quantity=9000,
            source_system="synthetic",
        ),
    ])
    db.flush()
    well = Well(planning_node_id=node.id, name="Well Stale-01", demand_status=DemandStatus.CONFIRMED)
    db.add(well)
    db.flush()
    line = DemandLine(
        well_id=well.id, product_id=primary.id, quantity=4000,
        ros_date=datetime.utcnow() + timedelta(days=400),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.flush()

    db.add(
        TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id)
    )
    db.add(
        CustomerSubstitutionRule(
            customer_id=customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db.flush()
    recompute_well(db, well)
    assert line.coverage_result.status == CoverageStatus.PENDING_APPROVAL
    db.commit()

    well_id, line_id = well.id, line.id
    return TestClient(app), TestingSessionLocal, well_id, line_id


def test_pending_line_appears_on_dashboard_until_its_well_leaves_scope():
    """REPURPOSED to the well-level endpoint, and strictly stronger.

    Taking demand out of scope is a WELL operation now
    (`PUT /wells/{id}/demand-status`), because demand status is a property of the
    well. Every assertion about the CONSEQUENCE is unchanged: the stale
    PendingApproval row is deleted, the Home Dashboard card stops listing it, and
    GET /wells/{id} still lists the line but reports no verdict for it.

    Two things are checked that could not be before: that
    `POST /demand-lines/{id}/revisions` REFUSES a status rather than applying it
    silently, and that the well-level change wrote a revision and an impact record
    for the line it affected.
    """
    client, SessionLocal, well_id, line_id = _build_client_with_pending_then_revised_line()

    body = client.get("/dashboard/home").json()
    assert [p["demand_line_id"] for p in body["pending_approvals"]] == [line_id]
    well_body = client.get(f"/wells/{well_id}").json()
    assert well_body["demand_lines"][0]["coverage_status"] == "PendingApproval"

    # A status sent to the LINE endpoint is refused, and the refusal names the
    # well-level route. Silently ignoring it would return 200 while the planner
    # believed the well had moved.
    refused = client.post(
        f"/demand-lines/{line_id}/revisions",
        json={
            "quantity": 4000,
            "ros_date": (datetime.utcnow() + timedelta(days=400)).isoformat(),
            "status": "Planned",
            "profile": "Primary",
        },
    )
    assert refused.status_code == 400
    assert "property of the WELL" in refused.json()["detail"]
    assert f"/wells/{well_id}/demand-status" in refused.json()["detail"]

    # The planner moves the WELL to Planned -- out of scope for the engine.
    resp = client.put(
        f"/wells/{well_id}/demand-status", json={"demand_status": "Planned"}
    )
    assert resp.status_code == 200, resp.text
    changed = resp.json()
    assert changed["status_before"] == "Confirmed"
    assert changed["status_after"] == "Planned"
    assert changed["demand_line_ids"] == [line_id]
    assert len(changed["revision_ids"]) == 1
    assert len(changed["impact_record_ids"]) == 1

    # The stale row is gone, so the card no longer lists out-of-scope demand.
    # (2026-08-14: the card became a UNION of open requests + in-scope
    # PendingApproval verdicts. This line has NO open request, so the verdict
    # half governs it and the scope guard still empties the card.)
    body = client.get("/dashboard/home").json()
    assert body["pending_approvals"] == []

    # GET /wells/{id} still lists the line -- planners need to see their Planned
    # demand -- but reports no coverage verdict for it.
    well_body = client.get(f"/wells/{well_id}").json()
    assert len(well_body["demand_lines"]) == 1
    assert well_body["demand_lines"][0]["status"] == "Planned"
    assert well_body["demand_lines"][0]["coverage_status"] is None
    assert well_body["demand_lines"][0]["coverage_reason"] is None
    assert well_body["coverage_status"] is None

    # And the row really was deleted, not merely hidden.
    db = SessionLocal()
    try:
        assert db.get(CoverageResult, line_id) is None
    finally:
        db.close()

    app.dependency_overrides.clear()


def test_wells_api_hides_coverage_for_a_budgeted_line_even_if_a_row_survives():
    """Belt-and-braces: even if a CoverageResult row outlived its scope (e.g.
    written by an older build), the API must not present it as authoritative.

    REPURPOSED twice, and never weakened. It once used a Contingency line (which
    stopped leaving scope when the owner widened the profile default), then a
    Budgeted LINE, and now a Budgeted WELL -- because demand status is a property of
    the well. Each version stages the identical situation, a surviving verdict on a
    line the engine no longer evaluates, and the assertions are unchanged: the well
    payload withholds the coverage status, and the Home Dashboard's Pending Approvals
    card does not list it.
    """
    client, SessionLocal, well_id, line_id = _build_client_with_pending_then_revised_line()

    db = SessionLocal()
    try:
        # Move the line out of scope WITHOUT going through the engine, leaving
        # its PendingApproval row behind -- exactly what a row written by an
        # older build of the engine would look like. The move is made on the WELL,
        # because that is where demand status lives now, and by direct assignment
        # rather than through `set_well_demand_status` precisely so the engine does
        # not tidy up after it.
        line = db.get(DemandLine, line_id)
        line.well.demand_status = DemandStatus.BUDGETED
        db.commit()
        assert db.get(CoverageResult, line_id) is not None
    finally:
        db.close()

    well_body = client.get(f"/wells/{well_id}").json()
    assert well_body["demand_lines"][0]["status"] == "Budgeted"
    assert well_body["demand_lines"][0]["coverage_status"] is None

    body = client.get("/dashboard/home").json()
    assert body["pending_approvals"] == []

    app.dependency_overrides.clear()


def test_wells_api_SERVES_coverage_for_a_contingency_line():
    """The complement, and new: a Contingency line is in scope, so it keeps its
    verdict.

    Added because every other assertion in this module is about coverage being
    WITHHELD, and "withhold when out of scope" is only half a rule. An
    implementation that withheld coverage from every non-Primary line would pass all
    of them -- and that is precisely the behaviour the owner's default-profile change
    removed.
    """
    client, SessionLocal, well_id, line_id = _build_client_with_pending_then_revised_line()

    db = SessionLocal()
    try:
        line = db.get(DemandLine, line_id)
        line.profile = DemandProfile.CONTINGENCY
        db.commit()
        recompute_well(db, line.well)
        db.commit()
        assert db.get(CoverageResult, line_id) is not None
    finally:
        db.close()

    well_body = client.get(f"/wells/{well_id}").json()
    assert well_body["demand_lines"][0]["profile"] == "Contingency"
    assert well_body["demand_lines"][0]["coverage_status"] is not None

    app.dependency_overrides.clear()


def test_home_card_lists_every_open_request_even_off_verdict(ute_module_guard=None):
    """The Home card is a union of open requests and PendingApproval verdicts
    (product-owner decision, 2026-08-14).

    QA staged exactly this: a request raised on a line whose verdict stayed
    Uncovered (substitute stock insufficient) was invisible on the Home "work
    queue" while the Approvals queue listed it. The card must list every open
    REQUEST, with its approval_id so the buttons work.
    """
    client, SessionLocal, well_id, line_id = (
        _build_client_with_pending_then_revised_line()
    )
    # Take the well out of scope: verdict rows are deleted, so any card entry
    # from here on can only come from the REQUEST half of the union.
    client.put(f"/wells/{well_id}/demand-status", json={"demand_status": "Planned"})
    assert client.get("/dashboard/home").json()["pending_approvals"] == []

    db = SessionLocal()
    try:
        line = db.get(DemandLine, line_id)
        from_id, to_id = line.product_id, None
        from app.models import TechnicalSubstitution
        to_id = (
            db.query(TechnicalSubstitution)
            .filter(TechnicalSubstitution.from_product_id == from_id)
            .one()
            .to_product_id
        )
    finally:
        db.close()

    resp = client.post(
        f"/demand-lines/{line_id}/substitution-approvals",
        json={"from_product_id": from_id, "to_product_id": to_id},
    )
    assert resp.status_code == 200, resp.text

    body = client.get("/dashboard/home").json()
    assert [p["demand_line_id"] for p in body["pending_approvals"]] == [line_id]
    assert body["pending_approvals"][0]["approval_id"] is not None
    assert body["pending_approvals"][0]["substitute_description"] is not None

    app.dependency_overrides.clear()
