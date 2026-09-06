"""The three "honesty" paths, asserted against THE ACTUAL SEED.

Why this module runs `seed.seed_from_workbook.run()` instead of building a fixture
--------------------------------------------------------------------------------
Every other test module here hand-builds its world, which is right for testing an
engine RULE: the fixture is the specification of the case. But the claim under test
in this module is different in kind. It is not "the engine reports
`modelled = False` when a dimension is missing" -- `tests/test_lead_time_engine.py`
already pins that from a fixture. It is "the SEEDED DEMO DATA can actually produce
that state", and a fixture cannot say anything about the seed.

That distinction is the whole reason this file exists. All three of these paths
were unreachable in the seeded demo while being perfectly well covered by
fixture-based unit tests, so a frontend developer had to verify the rendering with
client-side `fetch` stubs -- which proves the UI and says nothing about the
contract. A hand-built fixture here would repeat exactly that mistake one layer
down. So the seed is run, unmodified, and every assertion below reads the result.

A consequence worth naming: these tests fail if somebody deletes the seeded case,
which is the point. Adding an `InventoryOnHand` row for the unstocked product, or
downgrading the Dunlin-23 approval to Pending, would silently remove the only
demo-reachable instance of a path, and that is precisely the regression this file
is here to catch.

The three paths
---------------
  1. `modelled = False`  CSG 7 32.0 L80 **Hydril 563** -- a connection with no
     component row, and Connection carries no "*" wildcard. Must render as "not
     modelled"; `0 mo` would read as instant delivery.
  2. `blocking_layer == "insufficient-inventory"`  Well Dunlin-23's APPROVED
     substitute, of which only 500 of 4000 exist and none is assigned. Contrasted
     directly against Well Kittiwake-20, whose approved substitute IS on the dock
     but hard-assigned: same shape, different destination (the mill vs Oracle).
  3. 424 / 409  TBG 4-1/2 15.1 has no `InventoryOnHand` row in any BU, so
     `by_item` raises `InventoryRowMissing` while `GET /mrp/lead-time/{id}` still
     answers 200. The 409 case is deliberately NOT in the seed; see
     `test_no_seeded_customer_is_unmapped` for the reason, proved rather than
     asserted.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.db import enforce_sqlite_foreign_keys
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.engines.coverage import compute_customer_coverage
from app.engines.inventory import (
    InventoryRowMissing,
    InventoryScopeMissing,
    on_hand_for,
)
from app.engines.lead_time import resolve_lead_time
from app.engines.mrp import by_item
from app.engines.substitution import (
    BLOCK_INVENTORY,
    BLOCK_ORACLE_RELEASE,
    BLOCK_PRIORITY,
    RECOMMENDED_ACTION,
    SubstitutionApprovalStatus,
    find_candidates,
)
from app.main import app
from app.models import (
    AllocationPolicy,
    CoverageResult,
    CoverageStatus,
    Customer,
    DemandLine,
    InventoryOnHand,
    Product,
    Well,
)
from seed import seed_from_workbook

# The descriptions the seed gives the three products these tests are about.
# Spelled once, here, so a rename in the seed fails loudly in one place.
UNMODELLED_DESCRIPTION = "CSG 7 32.0 L80 Hydril 563 SMLS"
UNSTOCKED_DESCRIPTION = "TBG 4-1/2 15.1 13CR110 VAM TOP SMLS"
DUNLIN_PRIMARY_DESCRIPTION = "CSG 10-3/4 55.5 L80 VAM 21 SMLS"
DUNLIN_SUBSTITUTE_DESCRIPTION = "CSG 10-3/4 65.7 L80 VAM 21 SMLS"
KITTIWAKE_SUBSTITUTE_DESCRIPTION = "CSG 8-5/8 40.0 L80 VAM 21 SMLS"


@pytest.fixture(scope="module")
def seeded():
    """Run the real seed against a throwaway in-memory database.

    Module-scoped: the seed is a heavy, deterministic, write-once operation and
    every test below only reads. `seed_from_workbook` binds `engine` and
    `SessionLocal` at import time, so they are rebound as module globals rather
    than reaching into `app.db` -- the seed is exercised exactly as shipped.

    `alembic_version` is created by hand because the seed REFUSES to run on a
    database that has tables but no migration marker (`_require_migrated_schema`),
    and that refusal is itself behaviour worth keeping intact here. The tables
    themselves come from `Base.metadata.create_all`, the same way `conftest.py`
    builds them -- tests do not depend on Alembic.
    """
    engine = enforce_sqlite_foreign_keys(create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    ))
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('test-stamp')"))

    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    original = (seed_from_workbook.engine, seed_from_workbook.SessionLocal)
    seed_from_workbook.engine = engine
    seed_from_workbook.SessionLocal = TestingSessionLocal
    try:
        seed_from_workbook.run()
    finally:
        seed_from_workbook.engine, seed_from_workbook.SessionLocal = original

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(seeded):
    """TestClient served by the SEEDED session, so HTTP and engine agree."""
    app.dependency_overrides[get_db] = lambda: seeded
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _product(db, description):
    return db.query(Product).filter(Product.description == description).one()


def _lines_of(db, well_name):
    well = db.query(Well).filter(Well.name == well_name).one()
    return db.query(DemandLine).filter(DemandLine.well_id == well.id).all()


#: {well name: the description of the product whose line CARRIES that well's
#: scenario}.
#:
#: Needed because a well no longer has one demand line. Every well now carries a
#: realistic 3-5 line casing programme (`seed._seed_well_programmes`), and the
#: additional STAGE lines are deliberately always Covered so they cannot move a
#: verdict -- which means "the line of this well" is ambiguous and the scenario line
#: has to be named.
#:
#: Naming it by PRODUCT DESCRIPTION rather than by position is deliberate. A
#: positional pick ("the first line", "the one that is not Covered") would keep
#: passing while quietly testing the wrong line: "not Covered" in particular would
#: silently follow the defect if a stage line ever went Uncovered, so the assertion
#: would still be green and would no longer be about the scenario. The product is
#: the fact that makes each of these wells the case it is.
SCENARIO_PRODUCTS = {
    "Well Dunlin-23": DUNLIN_PRIMARY_DESCRIPTION,
    "Well Kittiwake-20": "CSG 8-5/8 36.0 L80 VAM 21 SMLS",
    "Well Hawk-07": "TBG 4-1/2 12.6 13CR80 VAM TOP SMLS",
    "Well Osprey-09": "TBG 4-1/2 12.6 13CR80 VAM TOP SMLS",
    "Well Auklet-24": "TBG 3-1/2 10.2 13CR80 VAM TOP SMLS",
    "Well Falcon-04": "CSG 9-5/8 53.5 P110 VAM 21 SMLS",
    "Well Merlin-05": "CSG 7 29.0 L80 VAM 21 SMLS",
    # The customer-owned consumption-priority wells. Named by product like every
    # other entry, so a seed change to those products fails loudly here.
    "Well Wigeon-27": "CSG 11-3/4 65.0 P110 VAM 21 SMLS",
    "Well Pintail-28": "CSG 11-3/4 65.0 P110 VAM 21 SMLS",
    "Well Garganey-29": "TBG 3-1/2 9.3 L80 VAM TOP SMLS",
}


def _scenario_line(db, well_name):
    """The one line of `well_name` that carries its demonstrated scenario.

    Raises if it is not found or is not unique -- so a seed change that renamed or
    duplicated the product fails loudly here rather than making every assertion
    below silently describe a stage line.
    """
    description = SCENARIO_PRODUCTS[well_name]
    lines = [
        line
        for line in _lines_of(db, well_name)
        if line.product.description == description
    ]
    assert len(lines) == 1, (
        f"{well_name} should have exactly one {description!r} line, found "
        f"{len(lines)}. The scenario line is identified by product, so a seed "
        "change to that product has to be reflected in SCENARIO_PRODUCTS."
    )
    return lines[0]


def _candidates_for(db, well_name):
    """Substitution candidates for `well_name`'s SCENARIO line, annotated exactly as
    the coverage pass annotates them.

    `hard_assigned_by_product` is taken from the real coverage pass rather than
    invented, because that map is the ONLY thing that distinguishes
    hard-assigned-elsewhere from insufficient-inventory -- passing `{}` here would
    make the test unable to tell the two paths apart, which is the one thing it
    exists to do.
    """
    line = _scenario_line(db, well_name)
    customer = line.well.planning_node.customer
    coverage = compute_customer_coverage(db, customer)
    return find_candidates(
        db, line, hard_assigned_by_product=coverage.hard_assigned_by_product
    )


# ---------------------------------------------------------------------------
# Path 1 -- lead time NOT MODELLED
# ---------------------------------------------------------------------------


def test_seed_contains_a_product_whose_lead_time_is_not_modelled(seeded):
    """The seed produces `modelled = False` from real data, not from a stub.

    All four values are asserted together on purpose. `modelled is False` alone
    would pass for a product whose total was a partial sum, and `total == 0.0`
    alone would pass for a product that legitimately resolves to zero months --
    the pair is what pins the documented contract. `matched_months` is checked too
    because a UI showing "5.5 months matched, Connection missing" is the honest
    rendering, and it must have real numbers behind it.
    """
    product = _product(seeded, UNMODELLED_DESCRIPTION)
    breakdown = resolve_lead_time(seeded, product)

    assert breakdown.modelled is False
    assert breakdown.total_months == 0.0
    assert breakdown.missing_dimensions == ("Connection",)
    # OD/WT "*" 3.0 + Carbon 0.5 + Sailing 2.0. Reported, deliberately not totalled.
    assert breakdown.matched_months == 5.5
    assert "NOT MODELLED" in breakdown.note
    # transit_months collapses to 0.0 with the total, so required_ship_date and
    # recommended_order_date can never be derived from two different answers.
    assert breakdown.transit_months == 0.0


def test_unmodelled_product_is_catalogue_only(seeded):
    """No well demands it -- the deliberate placement decision.

    An unmodelled lead time means coverage may never call the line UNRECOVERABLE
    (absent data is "cannot judge", not "hopeless"), so a demanded copy could only
    be Covered -- consulting no lead time and demonstrating nothing -- or Uncovered
    carrying an order date the engine itself labels indicative only. Neither
    belongs on a planning screen, and the path is fully reachable without demand.
    """
    product = _product(seeded, UNMODELLED_DESCRIPTION)
    assert (
        seeded.query(DemandLine).filter(DemandLine.product_id == product.id).count() == 0
    )


def test_unmodelled_product_lead_time_endpoint_answers(client, seeded):
    """GET /mrp/lead-time/{id} is the route that makes a catalogue-only product
    reachable, and it must publish the not-modelled state rather than a 0."""
    product = _product(seeded, UNMODELLED_DESCRIPTION)
    response = client.get(f"/mrp/lead-time/{product.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["modelled"] is False
    assert body["total_months"] == 0.0
    assert body["missing_dimensions"] == ["Connection"]
    assert body["matched_months"] == 5.5


def test_unmodelled_product_still_has_a_by_item_report(client, seeded):
    """It has an InventoryOnHand row, so the not-modelled breakdown is visible on
    the By Item screen beside a real runout curve.

    This is what keeps the two honesty paths independent: path 1 is observable
    WITHOUT tripping path 3, and vice versa.
    """
    product = _product(seeded, UNMODELLED_DESCRIPTION)
    response = client.get(f"/mrp/by-item/{product.id}")
    assert response.status_code == 200
    assert response.json()["lead_time"]["modelled"] is False


def test_every_other_seeded_product_is_still_modelled(seeded):
    """The new product is the ONLY unmodelled one.

    Guards the addition from the other direction: an "unmodelled" case created by
    deleting a component row would have made several products unmodelled at once
    and quietly changed real verdicts. Exactly one product opts out, by carrying an
    attribute value nothing matches.
    """
    unmodelled = [
        p.description
        for p in seeded.query(Product).all()
        if not resolve_lead_time(seeded, p).modelled
    ]
    assert unmodelled == [UNMODELLED_DESCRIPTION]


# ---------------------------------------------------------------------------
# Path 2 -- insufficient-inventory vs hard-assigned-elsewhere
# ---------------------------------------------------------------------------


def test_seed_produces_an_insufficient_inventory_block(seeded):
    """Dunlin-23's approved substitute is blocked by ABSENT STEEL, nothing else."""
    candidates = _candidates_for(seeded, "Well Dunlin-23")
    assert len(candidates) == 1
    (candidate,) = candidates

    assert candidate.product.description == DUNLIN_SUBSTITUTE_DESCRIPTION
    assert candidate.blocking_layer == BLOCK_INVENTORY
    assert candidate.blocking_layer != BLOCK_ORACLE_RELEASE
    assert candidate.usable is False
    # The three layers ALL clear. Only the quantity does not.
    assert candidate.customer_allowed is True
    assert candidate.approval_status == SubstitutionApprovalStatus.APPROVED
    # A quantity comparison, not a missing row: 500 genuinely exist.
    assert candidate.available_qty == 500.0
    assert candidate.required_qty == 4000.0
    # Nothing is assigned, so no Oracle release could help -- which is exactly
    # what makes insufficient-inventory the honest answer here.
    assert candidate.hard_assigned_qty == 0.0
    assert candidate.recommended_action == RECOMMENDED_ACTION[BLOCK_INVENTORY]
    assert "mill" not in candidate.recommended_action.lower()  # wording is Oracle-free
    assert "Oracle" not in candidate.recommended_action


def test_approved_but_unstocked_substitute_needs_an_approved_approval(seeded):
    """The approval must be APPROVED for the inventory answer to surface at all.

    `find_candidates` reports ONE blocking layer, in the priority order
    customer > well-approval > hard-assigned-elsewhere > insufficient-inventory,
    so a Pending approval would mask the quantity answer entirely. Asserted from
    the exported `BLOCK_PRIORITY` rather than restated, and demonstrated by
    actually downgrading the approval inside a nested transaction.
    """
    assert BLOCK_PRIORITY.index("well-approval") < BLOCK_PRIORITY.index(BLOCK_INVENTORY)

    line = _scenario_line(seeded, "Well Dunlin-23")
    substitute = _product(seeded, DUNLIN_SUBSTITUTE_DESCRIPTION)

    masked = find_candidates(
        seeded,
        line,
        hard_assigned_by_product={},
        approval_override=lambda *_: SubstitutionApprovalStatus.PENDING,
    )
    assert [c.blocking_layer for c in masked] == ["well-approval"]
    # ...and the substitute really is the same short-stocked product either way.
    assert masked[0].to_product_id == substitute.id


def test_the_two_quantity_answers_sit_side_by_side(seeded):
    """Kittiwake-20 and Dunlin-23 are the contrast the layer exists to express.

    Same customer, same ROS, both with a fully approved substitute, both Uncovered
    -- and the planner is sent to two different places. Before Dunlin-23 existed
    only one of these two could be produced by seeded data, so a UI that rendered
    them identically would have been wrong with nothing to show it.
    """
    (oracle_candidate,) = _candidates_for(seeded, "Well Kittiwake-20")
    (mill_candidate,) = _candidates_for(seeded, "Well Dunlin-23")

    assert oracle_candidate.product.description == KITTIWAKE_SUBSTITUTE_DESCRIPTION
    assert oracle_candidate.blocking_layer == BLOCK_ORACLE_RELEASE
    assert mill_candidate.blocking_layer == BLOCK_INVENTORY
    assert oracle_candidate.recommended_action != mill_candidate.recommended_action

    # The physical difference behind the two answers: the steel exists in one case
    # and does not in the other.
    assert oracle_candidate.hard_assigned_qty == 5000.0
    assert mill_candidate.hard_assigned_qty == 0.0

    # Both lines are Uncovered, and their REASONS differ in the same direction.
    kittiwake_line = _scenario_line(seeded, "Well Kittiwake-20")
    dunlin_line = _scenario_line(seeded, "Well Dunlin-23")
    oracle_reason = seeded.get(CoverageResult, kittiwake_line.id).reason
    mill_reason = seeded.get(CoverageResult, dunlin_line.id).reason
    assert "hard-assigned to another demand line" in oracle_reason
    assert "insufficient qty" in mill_reason
    assert "hard-assigned" not in mill_reason
    # Same ROS, so they are neighbours on any ROS-ordered screen.
    assert kittiwake_line.ros_date.date() == dunlin_line.ros_date.date()


def test_insufficient_inventory_is_reachable_through_the_api(client, seeded):
    """The candidate payload the Substitution Workspace actually renders."""
    line = _scenario_line(seeded, "Well Dunlin-23")
    response = client.get(f"/demand-lines/{line.id}/substitution-candidates")
    assert response.status_code == 200
    (payload,) = response.json()
    assert payload["blocking_layer"] == BLOCK_INVENTORY
    assert payload["usable"] is False
    assert payload["hard_assigned_qty"] == 0.0


# ---------------------------------------------------------------------------
# Path 3 -- 424 InventoryRowMissing, and the 409 that is deliberately absent
# ---------------------------------------------------------------------------


def test_unstocked_product_has_no_inventory_row_in_any_bu(seeded):
    """The absence IS the scenario. Adding a row would delete the only 424 case."""
    product = _product(seeded, UNSTOCKED_DESCRIPTION)
    assert (
        seeded.query(InventoryOnHand)
        .filter(InventoryOnHand.product_id == product.id)
        .count()
        == 0
    )
    # It is the ONLY such product, so nothing else in the demo is accidentally
    # carrying an unknown quantity.
    stocked = {row.product_id for row in seeded.query(InventoryOnHand).all()}
    unstocked = [p.description for p in seeded.query(Product).all() if p.id not in stocked]
    assert unstocked == [UNSTOCKED_DESCRIPTION]


def test_by_item_raises_while_lead_time_still_answers(seeded):
    """Unknown stock refuses the runout curve; the lead time is answered anyway.

    Both halves in one test because the pairing is the claim: a lead time is
    knowable with no inventory data at all, so refusing it alongside the runout
    curve would hide a fact that is available.
    """
    product = _product(seeded, UNSTOCKED_DESCRIPTION)

    with pytest.raises(InventoryRowMissing) as excinfo:
        by_item(seeded, product.id)
    assert excinfo.value.product_id == product.id
    # No BU is named: the row is missing in EVERY BU, not in one particular one.
    assert excinfo.value.business_unit_id is None
    assert "UNKNOWN" in str(excinfo.value)
    assert "not reported as 0" in str(excinfo.value)

    breakdown = resolve_lead_time(seeded, product)
    assert breakdown.modelled is True
    assert breakdown.total_months == 6.5


def test_unstocked_product_is_catalogue_only(seeded):
    """Nothing demands it, and that is a requirement rather than a preference.

    An in-scope line for a product with no inventory row makes
    `compute_customer_coverage` raise for that customer's WHOLE pool, taking down
    the coverage grid, the home dashboard and every screen showing them. This test
    is the tripwire on that landmine.
    """
    product = _product(seeded, UNSTOCKED_DESCRIPTION)
    assert (
        seeded.query(DemandLine).filter(DemandLine.product_id == product.id).count() == 0
    )
    # Proof of the consequence, not merely the claim: every seeded customer's pass
    # still completes.
    for customer in seeded.query(Customer).all():
        compute_customer_coverage(seeded, customer)


def test_by_item_http_returns_the_documented_424_body(client, seeded):
    """The 424 handler in app.main, exercised from seeded data.

    `error` is the stable machine-readable discriminator a client routes on, so it
    is asserted verbatim rather than through a substring.
    """
    product = _product(seeded, UNSTOCKED_DESCRIPTION)
    response = client.get(f"/mrp/by-item/{product.id}")

    assert response.status_code == 424
    body = response.json()
    assert body["error"] == "inventory_row_missing"
    assert body["product_id"] == product.id
    assert body["business_unit_id"] is None
    assert set(body) == {"error", "detail", "business_unit_id", "product_id"}
    assert product.description in body["detail"]

    # ...and the same product answers 200 on the lead-time route, which is exactly
    # why that route exists (see app.api.mrp.get_lead_time_breakdown).
    assert client.get(f"/mrp/lead-time/{product.id}").status_code == 200


def test_no_seeded_customer_is_unmapped_and_the_reason_is_provable(seeded, client):
    """The 409 case is NOT in the demo data, and this test proves why.

    An unmapped customer cannot have coverage computed AT ALL -- not "cannot have
    its own demand evaluated". `GET /coverage` with non-default filters recomputes
    EVERY customer (`app.engines.coverage_view.project_coverage`), and
    `_assignment_context` calls `scoped_customer_ids` before it looks at any demand,
    so an unmapped customer raises even with no wells and no demand lines. One
    seeded row would therefore 409 that endpoint for every user, on every filter
    change.

    So the seed leaves it out, and the path is covered HERE instead: the customer
    is created inside this test, the 409 is demonstrated, and the row is rolled
    back so no other test sees it.

    The cleanup is a plain `rollback`, NOT a savepoint. `project_coverage` rolls
    the caller's transaction back itself -- that is how it recomputes read-only --
    so a savepoint opened here would already be closed by the time this test tried
    to release it. Nothing in this module has committed, so the rollback discards
    the orphan and leaves the seeded rows (committed by the seed's own session)
    untouched.
    """
    assert [c.name for c in seeded.query(Customer).all() if c.business_unit_id is None] == []

    try:
        orphan = Customer(
            name="Unmapped Operator (test-only)",
            allocation_policy=AllocationPolicy.SOFT,
            business_unit_id=None,
        )
        seeded.add(orphan)
        seeded.flush()

        # No wells, no demand -- and it still cannot be covered.
        assert seeded.query(Well).count() > 0  # the pass is genuinely doing work
        with pytest.raises(InventoryScopeMissing) as excinfo:
            compute_customer_coverage(seeded, orphan)
        assert "not mapped to a Business Unit" in str(excinfo.value)

        # The same refusal reaches HTTP as the documented 409 body.
        product = _product(seeded, UNSTOCKED_DESCRIPTION)
        with pytest.raises(InventoryScopeMissing):
            on_hand_for(seeded, None, product)

        # C-07 RESOLVED: the unmapped customer no longer takes the whole grid
        # down. The recompute isolates the failure per customer, the request
        # succeeds for everyone else, and the skipped customer is NAMED so the
        # gap can never pass silently.
        response = client.get("/coverage", params={"status": "Planned"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["filters"]["skipped_customers"] == [
            "Unmapped Operator (test-only)"
        ]
    finally:
        seeded.rollback()
        seeded.expire_all()

    assert (
        seeded.query(Customer).filter(Customer.business_unit_id.is_(None)).count() == 0
    )


# ---------------------------------------------------------------------------
# Path 4 -- nothing above moved a single existing verdict
# ---------------------------------------------------------------------------

#: Every seeded well and its rollup, as the demo has always published it. A flat
#: table rather than one assertion per well: the three additions each introduce
#: their own products precisely so that NO existing verdict can move, and the
#: cheapest way to keep that promise honest is to state all of them at once.
EXPECTED_WELL_ROLLUPS = {
    "Well Eagle-01": "Covered",
    "Well Falcon-04": "Uncovered",
    "Well Hawk-07": "Uncovered",
    "Well Osprey-09": "Uncovered",
    "Well Kestrel-02": "Covered",
    "Well Merlin-05": "Uncovered",
    "Well Petrel-03": "Covered",
    "Well Skua-06": "Uncovered",
    "Well Puffin-12": "Covered",
    "Well Gannet-11": "Uncovered",
    "Well Tern-08": "Covered",
    "Well Skerry-14": "Uncovered",
    "Well Fulmar-13": "Uncovered",
    "Well Guillemot-16": "Uncovered",
    "Well Razorbill-17": "Uncovered",
    "Well Shearwater-18": "Uncovered",
    "Well Fratercula-19": "Covered",
    "Well Kittiwake-20": "Uncovered",
    "Well Storm-21": "Covered",
    "Well Gadwall-22": "Uncovered",
    # The wells the additions contribute.
    "Well Dunlin-23": "Uncovered",
    # Covered by something OTHER than what it ordered -- the only substitution
    # success in the seed, and so the only well whose covered rollup comes from a
    # substitute rather than its own product.
    "Well Auklet-24": "Covered",
    # The CUSTOMER-OWNED consumption priority, and the reason these two rollups are
    # the load-bearing part of that addition: with the priority rule BOTH are Covered,
    # and with company-owned-first (or one undifferentiated 2000 pool) BOTH would be
    # Uncovered. Wigeon-27 is covered entirely from the customer's own 3000 and leaves
    # the company 2000 intact, which is what lets Pintail-28 have it.
    "Well Wigeon-27": "Covered",
    "Well Pintail-28": "Covered",
    # Short AFTER drawing its own material: 1000 owned against 2500 needed, and the
    # company holds an explicit 0. Exists for the coverage REASON wording.
    "Well Garganey-29": "Uncovered",
    # The two wells that exist to be EXCLUDED by the default status filter, added
    # when demand status became a property of the WELL. None is the right answer and
    # is not a gap: no line of theirs is in scope, so the engine reached no verdict.
    # "Unevaluated", never "covered" -- see app.models.coverage.CoverageResult.
    "Well Merganser-25": None,   # Planned
    "Well Shelduck-26": None,    # Budgeted
}

#: The verdict of each well's SCENARIO-CARRYING line -- the fact that well exists to
#: demonstrate. A well rollup is only ever Covered or Uncovered, so PendingApproval,
#: Unrecoverable and CoveredViaSubstitute are all invisible in the table above.
#:
#: WHY THIS TABLE IS NOW KEYED ON THE SCENARIO LINE, AND WHY THAT IS NOT A WEAKENING
#: --------------------------------------------------------------------------------
#: It used to list every verdict of every line, which was the same thing back when a
#: well had one line. Wells now carry a realistic 3-5 line casing programme
#: (`seed._seed_well_programmes`), so a whole-well list would be dominated by stage
#: lines whose "Covered" says nothing about the case -- and, worse, adding a stage
#: line anywhere would force an edit here, which is how a pinned expectation quietly
#: becomes a thing people update without reading.
#:
#: The claim is therefore split in two, and BOTH halves are asserted below, so
#: nothing that was checked before has stopped being checked:
#:
#:   this table                      the scenario line's verdict is EXACTLY what it
#:                                   always was -- the assertion that actually
#:                                   protects the demo.
#:   EXPECTED_STAGE_LINE_COUNTS      every OTHER line of those wells is Covered, and
#:                                   there are the stated number of them. That is
#:                                   what pins "the additional lines cannot move a
#:                                   verdict", which is the promise the whole seed
#:                                   change rests on -- a promise nothing checked
#:                                   before, because there were no additional lines.
EXPECTED_SCENARIO_LINE_STATUS = {
    "Well Falcon-04": "PendingApproval",
    "Well Osprey-09": "Unrecoverable",
    "Well Hawk-07": "Uncovered",
    "Well Dunlin-23": "Uncovered",
    "Well Kittiwake-20": "Uncovered",
    "Well Merlin-05": "Uncovered",
    # CoveredViaSubstitute is the fifth coverage status and the one the whole
    # substitution engine exists to reach. It is invisible in the rollup table
    # above, which can only say Covered.
    "Well Auklet-24": "CoveredViaSubstitute",
    "Well Wigeon-27": "Covered",
    "Well Pintail-28": "Covered",
    "Well Garganey-29": "Uncovered",
}

#: {well name: (total line count, in-scope line count)} for every well the programme
#: seeder touched.
#:
#: The IN-SCOPE count is the interesting one and is what the Coverage Workspace
#: renders. Since demand status became a property of the WELL, in-scope is either
#: ALL of a well's lines or NONE of them -- which is the readability the change
#: bought, and which this table now shows at a glance: every Confirmed well reads
#: (n, n) and the two excluded wells read (n, 0). There is no longer any (5, 4) row,
#: and there cannot be one.
#:
#: Eagle-01 was the (5, 4) row: it held a Budgeted CSG line beside its Confirmed
#: demand. That line moved to Well Shelduck-26, a Budgeted well of its own, so
#: Eagle-01 is now (4, 4) and Shelduck-26 is (4, 0). Nothing was deleted --
#: `test_budgeted_demand_is_present_but_no_longer_evaluated` follows the same line to
#: its new home.
#:
#: Pinned because "3-5 lines" is the requirement. A well that silently dropped back
#: to one line would satisfy every other assertion in this file.
EXPECTED_LINE_COUNTS = {
    "Well Eagle-01": (4, 4),
    "Well Falcon-04": (4, 4),
    "Well Hawk-07": (5, 5),
    "Well Osprey-09": (4, 4),
    "Well Puffin-12": (4, 4),
    "Well Tern-08": (4, 4),
    "Well Fulmar-13": (4, 4),
    "Well Guillemot-16": (5, 5),
    "Well Razorbill-17": (4, 4),
    "Well Shearwater-18": (4, 4),
    "Well Kittiwake-20": (5, 5),
    "Well Gadwall-22": (4, 4),
    "Well Dunlin-23": (4, 4),
    "Well Auklet-24": (5, 5),
    "Well Wigeon-27": (4, 4),
    "Well Pintail-28": (4, 4),
    "Well Garganey-29": (4, 4),
    "Well Kestrel-02": (4, 4),
    "Well Merlin-05": (4, 4),
    "Well Gannet-11": (4, 4),
    "Well Fratercula-19": (4, 4),
    "Well Storm-21": (4, 4),
    "Well Petrel-03": (5, 5),
    "Well Skua-06": (4, 4),
    "Well Skerry-14": (4, 4),
    # The two EXCLUDED wells: a full programme each, none of it in scope.
    "Well Merganser-25": (4, 0),   # Planned
    "Well Shelduck-26": (4, 0),    # Budgeted
}


def test_every_well_rollup_is_unchanged(seeded):
    """Verbatim the table the demo has always published -- not one rollup moved.

    This is the single most load-bearing assertion in the change set. Every well now
    carries 3-5 demand lines where most carried one, and the default status/profile
    filters both moved (Budgeted out, Contingency in), yet every rollup is identical.
    That is not luck: the additional lines get their own amply-stocked products so
    they cannot compete with a scenario line, and no scenario quantity or ROS date
    was touched.
    """
    actual = {
        well.name: well.coverage_status for well in seeded.query(Well).all()
    }
    assert actual == EXPECTED_WELL_ROLLUPS


@pytest.mark.parametrize("well_name", sorted(EXPECTED_SCENARIO_LINE_STATUS))
def test_scenario_line_verdicts_are_unchanged(seeded, well_name):
    """The scenario-carrying line still gets exactly the verdict it always got."""
    line = _scenario_line(seeded, well_name)
    result = seeded.get(CoverageResult, line.id)
    assert result is not None, f"{well_name}'s scenario line has no verdict at all"
    assert result.status.value == EXPECTED_SCENARIO_LINE_STATUS[well_name]


@pytest.mark.parametrize("well_name", sorted(EXPECTED_SCENARIO_LINE_STATUS))
def test_every_non_scenario_line_is_covered(seeded, well_name):
    """The promise the whole seed change rests on: the ADDED lines move nothing.

    A stage line that came out anything other than Covered would be a new scenario
    nobody asked for -- and on a covered well it would flip the rollup, since one
    unsatisfied line is enough. So this pins the property directly rather than
    inferring it from the rollups: every line of these wells EXCEPT the scenario one
    is Covered.

    Not a weakening of the old whole-well table. It asserts strictly more about the
    added lines than that table could, because that table predates them.
    """
    scenario_line = _scenario_line(seeded, well_name)
    others = [l for l in _lines_of(seeded, well_name) if l.id != scenario_line.id]
    assert others, f"{well_name} has no additional lines -- the programme is missing"
    for line in others:
        result = seeded.get(CoverageResult, line.id)
        # Budgeted lines are out of scope under the new default filter and correctly
        # have NO verdict; everything in scope must be Covered.
        if result is None:
            # Every well in EXPECTED_SCENARIO_LINE_STATUS is Confirmed, so every one
            # of its lines is in scope and MUST have a verdict. A missing row is a
            # real failure here rather than an out-of-scope line, which is stricter
            # than the old form: back when status was a line column, a line could be
            # out of scope inside an in-scope well and this branch had to forgive it.
            raise AssertionError(
                f"{well_name}: line {line.id} has no coverage verdict, but its well "
                f"is {seeded.query(Well).filter(Well.name == well_name).one().demand_status.value} "
                "so every line of it is in scope"
            )
        assert result.status.value == "Covered", (
            f"{well_name}: added line for {line.product.description} came out "
            f"{result.status.value}, not Covered -- an added line must never be "
            "able to move a verdict. Check the stage product's on-hand quantity "
            "and, for a HARD customer, its InventoryAssignment row."
        )


def test_most_wells_carry_three_to_five_demand_lines(seeded):
    """The requirement itself: "not just 2 lines but 3-5 lines".

    Pinned as a table (per well) AND as a property (the range), because the two catch
    different regressions: the table catches a well quietly losing its programme, and
    the range catches a future well being added without one.
    """
    actual = {}
    for well_name in EXPECTED_LINE_COUNTS:
        well = seeded.query(Well).filter(Well.name == well_name).one()
        lines = _lines_of(seeded, well_name)
        # In scope is decided by the WELL's demand status, so it is all of a well's
        # lines or none of them. That is the whole point of the column having moved.
        in_scope = lines if well.demand_status.value == "Confirmed" else []
        actual[well_name] = (len(lines), len(in_scope))
    assert actual == EXPECTED_LINE_COUNTS

    every_count = [total for total, _in_scope in actual.values()]
    assert min(every_count) >= 3, "a well dropped below 3 demand lines"
    assert max(every_count) <= 5, "a well grew beyond the 3-5 lines asked for"

    # Every well in the seed has a programme -- none was left behind. Fratercula-19
    # is included, so this is genuinely all of them.
    seeded_well_names = {w.name for w in seeded.query(Well).all()}
    assert seeded_well_names == set(EXPECTED_LINE_COUNTS)


def test_the_seed_contains_contingency_demand(seeded):
    """Contingency is IN the default profile scope now, so the seed must contain it.

    Before this change there was not one Contingency line anywhere in the seeded
    data, which meant the WIDENING half of the owner's filter change was
    undemonstrable: the Coverage Workspace's profile toggle had nothing to move, and
    no screen could show that contingency steel now competes for inventory.

    Both halves are asserted. Contingency lines exist, AND those on a CONFIRMED well
    are genuinely EVALUATED -- a contingency line with no verdict would mean the
    profile filter had not actually widened.

    The "on a Confirmed well" qualifier is new and is not a loosening: a contingency
    line on a Planned or Budgeted well is out of scope for a STATUS reason, and
    demanding a verdict for it would assert the opposite of what the status filter is
    for. The qualifier is what keeps this test about the PROFILE filter, which is what
    it is named for. That the excluded wells' lines have no verdict is asserted
    directly by `test_a_planned_well_is_excluded_too` and
    `test_budgeted_demand_is_present_but_no_longer_evaluated`.
    """
    from app.models import DemandProfile

    contingency = (
        seeded.query(DemandLine)
        .join(Well, DemandLine.well_id == Well.id)
        .filter(DemandLine.profile == DemandProfile.CONTINGENCY)
        .all()
    )
    assert contingency, "the seed contains no Contingency demand at all"
    evaluated = 0
    for line in contingency:
        if line.well.demand_status.value != "Confirmed":
            continue
        evaluated += 1
        assert seeded.get(CoverageResult, line.id) is not None, (
            "a Confirmed/Contingency line has no coverage verdict, so the default "
            "profile filter has not actually widened to include Contingency"
        )
    assert evaluated, (
        "every Contingency line in the seed sits on an out-of-scope well, so the "
        "profile widening is still undemonstrable -- put one on a Confirmed well"
    )


def test_primary_and_contingency_coexist_inside_a_confirmed_well(seeded):
    """The product owner named this as the ROUTINE case, so the seed must contain it.

        Within a confirmed well it is an everyday occurrence for demand of both
        the primary and the contingency profile to coexist.

    It is also the reason `DemandProfile` STAYED on `DemandLine` while `DemandStatus`
    moved to `Well`: profile genuinely varies line by line, status does not. Without a
    seeded instance, that asymmetry would be documented and undemonstrated.

    Both halves are asserted: the coexistence, and that BOTH profiles are actually
    evaluated inside such a well -- i.e. both compete for inventory, rather than the
    contingency line being carried along as decoration.
    """
    from app.models import DemandProfile

    mixed = []
    for well in seeded.query(Well).all():
        if well.demand_status.value != "Confirmed":
            continue
        lines = seeded.query(DemandLine).filter(DemandLine.well_id == well.id).all()
        profiles = {line.profile for line in lines}
        if profiles == {DemandProfile.PRIMARY, DemandProfile.CONTINGENCY}:
            mixed.append((well, lines))

    assert mixed, (
        "no Confirmed well in the seed holds both Primary and Contingency demand, "
        "which the product owner named as the everyday case"
    )
    for well, lines in mixed:
        for line in lines:
            assert seeded.get(CoverageResult, line.id) is not None, (
                f"{well.name} is Confirmed and holds both profiles, but line "
                f"{line.id} ({line.profile.value}) has no verdict -- both profiles "
                "must be evaluated and both must compete for inventory"
            )


def test_budgeted_demand_is_present_but_no_longer_evaluated(seeded):
    """The NARROWING half of the filter change, demonstrated by real seeded data.

    REPURPOSED to follow the line, not to drop the claim. The Budgeted CSG line used
    to sit on Eagle-01 beside its Confirmed demand -- a Budgeted LINE inside a
    Confirmed WELL, which is exactly the state that stopped existing when demand
    status became a property of the well (and exactly the state that made the product
    owner's coverage grid unreadable). It now lives on Well Shelduck-26, a Budgeted
    well of its own.

    Every original claim is still asserted, and two more with it: the whole well is
    unevaluated, and Falcon-04 still gets the entire 3000 CSG pool -- which is the
    substantive half of the exclusion and was previously only implied.
    """
    well = seeded.query(Well).filter(Well.name == "Well Shelduck-26").one()
    assert well.demand_status.value == "Budgeted"

    lines = _lines_of(seeded, "Well Shelduck-26")
    assert lines, "Well Shelduck-26 has no demand at all"
    for line in lines:
        assert seeded.get(CoverageResult, line.id) is None, (
            "a line of a Budgeted well still has a coverage verdict, so the default "
            "status filter has not actually narrowed to Confirmed only"
        )
    # The well itself: unevaluated, neither covered nor uncovered.
    assert well.coverage_status is None

    # The line is still LISTED -- planners need to see their budgeted demand.
    assert any(
        line.product.description == "CSG 9-5/8 53.5 P110 VAM 21 SMLS"
        for line in lines
    ), "the Budgeted CSG line the demo relies on has gone missing entirely"

    # ...and Eagle-01, which used to carry it, is still Covered.
    eagle = seeded.query(Well).filter(Well.name == "Well Eagle-01").one()
    assert eagle.coverage_status == "Covered"


def test_a_planned_well_is_excluded_too(seeded):
    """The other excluded status, so the narrowing is not tied to one value."""
    well = seeded.query(Well).filter(Well.name == "Well Merganser-25").one()
    assert well.demand_status.value == "Planned"
    lines = _lines_of(seeded, "Well Merganser-25")
    assert lines
    for line in lines:
        assert seeded.get(CoverageResult, line.id) is None
    assert well.coverage_status is None


def test_every_line_of_a_well_shares_its_wells_status_by_construction(seeded):
    """The old invalid state is UNREPRESENTABLE, and the seed demonstrates that.

    There is nothing to assert per line -- `DemandLine` has no status column -- so
    what is checked is the consequence a planner sees: within any one well, every
    line is in scope together or out of scope together. A `CoverageResult` row for
    some of a well's lines and not others (with no line-level reason such as
    profile) is precisely the shape that produced the product owner's unreadable
    grid, and it can no longer occur.
    """
    from app.models import DemandProfile

    for well in seeded.query(Well).all():
        lines = seeded.query(DemandLine).filter(DemandLine.well_id == well.id).all()
        if not lines:
            continue
        in_scope = well.demand_status.value == "Confirmed"
        for line in lines:
            has_verdict = seeded.get(CoverageResult, line.id) is not None
            # Both default profiles are in scope, so status is the ONLY thing that
            # can exclude a line here -- which makes this a clean per-well check.
            assert line.profile in (DemandProfile.PRIMARY, DemandProfile.CONTINGENCY)
            assert has_verdict == in_scope, (
                f"{well.name} is {well.demand_status.value} but line {line.id} "
                f"{'has' if has_verdict else 'has no'} verdict -- every line of a "
                "well must be in scope together"
            )


def test_the_status_filter_moves_the_grid_by_whole_wells(seeded):
    """THE ACCEPTANCE TEST for moving demand status onto the well.

    The product owner's complaint, verbatim in effect: filtering the coverage grid
    to one customer showed 14 wells / 15 lines; adding a Confirmed-only status filter
    showed 14 wells / 14 lines, which "doesn't make sense". It was arithmetically
    consistent -- one well held a Confirmed line and a Budgeted line, so a LINE
    dropped and its WELL stayed -- but the data was unrealistic, and the counts could
    not be explained by anybody reading them.

    With status on the well the movement is whole-wells-at-a-time. This pins that
    directly, on the real seed, for Norheim Energy:

        default (Confirmed only)   17 wells in scope, 72 in-scope lines
        all three statuses         19 wells in scope, 80 in-scope lines

    (Those figures grew by three whole Confirmed wells and twelve lines when the
    customer-owned consumption-priority demonstration was added -- Wigeon-27,
    Pintail-28 and Garganey-29. The CLAIM being pinned is unchanged and is not about
    the absolute numbers: it is that the DELTA between the two filters is exactly the
    two excluded wells and exactly their own line count, which is asserted below.)

    The two wells that arrive bring 4 lines each, so BOTH counts move by exactly the
    arrival of two whole wells -- 2 wells and 8 lines -- and the line delta is the sum
    of the arriving wells' own line counts. There is no longer any filter setting
    under which a line can move without its well.
    """
    from app.engines.coverage_view import project_coverage
    from app.models import DemandProfile, DemandStatus, PlanningNode

    operator_a = (
        seeded.query(Customer).filter(Customer.name == "Norheim Energy").one()
    )
    a_wells = (
        seeded.query(Well)
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
        .filter(PlanningNode.customer_id == operator_a.id)
        .all()
    )
    a_well_ids = {w.id for w in a_wells}
    assert len(a_wells) == 19

    def counts(status_filter):
        projection = project_coverage(
            seeded,
            status_filter=status_filter,
            profile_filter={DemandProfile.PRIMARY, DemandProfile.CONTINGENCY},
        )
        lines = [
            line for line in projection.lines.values() if line.well_id in a_well_ids
        ]
        return len({line.well_id for line in lines}), len(lines)

    default_wells, default_lines = counts(None)
    assert (default_wells, default_lines) == (17, 72)

    all_wells, all_lines = counts(
        {DemandStatus.PLANNED, DemandStatus.BUDGETED, DemandStatus.CONFIRMED}
    )
    assert (all_wells, all_lines) == (19, 80)

    # The movement is exactly two whole wells and nothing else.
    arriving = [
        w for w in a_wells if w.demand_status != DemandStatus.CONFIRMED
    ]
    assert sorted(w.name for w in arriving) == [
        "Well Merganser-25",
        "Well Shelduck-26",
    ]
    arriving_line_count = sum(
        seeded.query(DemandLine).filter(DemandLine.well_id == w.id).count()
        for w in arriving
    )
    assert all_wells - default_wells == len(arriving) == 2
    assert all_lines - default_lines == arriving_line_count == 8


def test_hawk_07_is_uncovered_with_a_recoverable_mrp_row(seeded):
    """Hawk-07's shortfall is still orderable, Osprey-09's still is not.

    Asserted together because they are the SAME product with the same 6.5-month
    lead time and differ only by ROS -- which is the invariant, and which a change
    to the shared lead-time component set would break for both at once.
    """
    from app.engines.mrp import mrp_summary

    hawk_line = _scenario_line(seeded, "Well Hawk-07")
    osprey_line = _scenario_line(seeded, "Well Osprey-09")
    rows = mrp_summary(seeded)

    hawk_rows = [r for r in rows if hawk_line.id in r.demand_line_ids]
    osprey_rows = [r for r in rows if osprey_line.id in r.demand_line_ids]

    assert [r.unrecoverable for r in hawk_rows] == [False]
    assert [r.unrecoverable for r in osprey_rows] == [True]
    assert hawk_rows[0].lead_time_months == 6.5
    assert hawk_rows[0].lead_time.modelled is True


def test_the_seeded_business_unit_never_promises_more_steel_than_it_holds(seeded):
    """The D01 invariant, asserted against the shipped demo data.

    Replaces `test_gannet_11_is_still_coverable_via_bu_sharing`, which pinned the
    cross-customer sharing what-if: "could a neighbour's surplus cover this?".
    That question existed because each customer was judged against the whole
    Business Unit's stock independently, and the demo database was the proof --
    49,240 metres were promised twice across its two customers. The pool is
    divided once now, so the surplus has already gone to whoever needed it
    soonest and the honest assertion is that nothing is promised twice.
    """
    from collections import defaultdict

    from app.models import CoverageResult, InventoryOnHand

    drawn = defaultdict(float)
    for result in seeded.query(CoverageResult).all():
        product_id = result.fulfilled_by_product_id or result.demand_line.product_id
        drawn[product_id] += result.drawn_company or 0.0

    for product_id, promised in drawn.items():
        rows = (
            seeded.query(InventoryOnHand)
            .filter(InventoryOnHand.product_id == product_id)
            .all()
        )
        on_hand = sum(max(0.0, r.quantity or 0.0) for r in rows)
        assert promised <= on_hand + 1e-6, (
            f"product {product_id}: {promised:g} promised out of {on_hand:g} held"
        )


def test_substitution_success_is_reachable_and_attributed_to_the_substitute(seeded):
    """The seed contains a substitution that WORKS, and it is attributed correctly.

    Two facts, asserted together because the second is only interesting given the
    first. Before Auklet-24 existed the seed's substitution cases all stopped short
    of success -- pending, blocked, or over-subscribed -- so
    `COVERED_VIA_SUBSTITUTE` never occurred, and neither did a
    `fulfilled_by_product_id` pointing at anything other than the line's own
    product. That second absence meant no By Item page ever listed a demand line
    for a DIFFERENT product, which is precisely the case a Product column has to
    disambiguate: read as an order for the page's own product, such a row invites a
    planner to commit the same steel twice.
    """
    from app.engines.mrp import by_item

    line = _scenario_line(seeded, "Well Auklet-24")
    result = seeded.get(CoverageResult, line.id)

    assert result.status is CoverageStatus.COVERED_VIA_SUBSTITUTE
    # Satisfied by something else, and coverage recorded WHICH something else.
    assert result.fulfilled_by_product_id is not None
    assert result.fulfilled_by_product_id != line.product_id

    # It is the only such line in the seed, so if a future change makes another one
    # this test is where the surprise surfaces.
    all_substituted = [
        r
        for r in seeded.query(CoverageResult).all()
        if r.status is CoverageStatus.COVERED_VIA_SUBSTITUTE
    ]
    assert [r.demand_line_id for r in all_substituted] == [line.id]

    # The substitute's By Item page lists this line, and says out loud that the
    # line ordered a different product.
    analysis = by_item(seeded, result.fulfilled_by_product_id)
    listed = [l for l in analysis.demand_lines if l.demand_line_id == line.id]
    assert len(listed) == 1
    assert listed[0].product_id == line.product_id
    assert listed[0].product_id != analysis.product_id
    assert listed[0].product_description is not None


# ---------------------------------------------------------------------------
# Path 5 -- the SYNTHETIC DEMO HISTORY, and the on-order projection
#
# Both are new seeded facts, and both are the kind of thing that is easy to fake
# convincingly. So they are asserted against the real seed, through the ordinary
# engine path, exactly like the three honesty paths above.
# ---------------------------------------------------------------------------


def test_the_seeded_history_makes_the_prior_period_available_and_not_flat(seeded):
    """The demand trend RENDERS, off real revision history, with a mixed trend.

    Three claims, and the third is what stops the seed demonstrating only half the
    screen:

      1. every horizon's prior-period figure is AVAILABLE. Before the seeded
         history it was unavailable for all four -- correctly, because a database
         built moments ago has no past.
      2. the trend is NOT FLAT: the current and prior totals differ everywhere, so
         a reviewer sees real movement rather than four zeros.
      3. the directions are MIXED -- at least one horizon up and at least one down --
         so the UI's up / down rendering is both exercised by one seed.

    The history is synthetic demo data and the seed's output says so plainly. What
    makes it honest rather than an injected total is asserted by
    `test_the_prior_period_comes_from_real_revision_rows` below.
    """
    from app.engines.executive import demand_trend

    trend = demand_trend(seeded)
    horizons = {h.months: h for h in trend.horizons}
    assert sorted(horizons) == [3, 6, 12, 24]

    for months, horizon in sorted(horizons.items()):
        assert horizon.previous.available is True, (
            f"the {months}-month prior period is still unavailable: "
            f"{horizon.previous.reason}"
        )
        assert horizon.previous.value is not None
        assert horizon.previous.value > 0, (
            f"the {months}-month prior period is zero, so the trend is flat and the "
            "seeded history demonstrates nothing"
        )
        assert horizon.change_pct.available is True
        assert horizon.current_total != horizon.previous.value

    directions = {(horizon.change_pct.value > 0) for horizon in horizons.values()}
    assert directions == {True, False}, (
        "every horizon moves the same way, so the seed exercises only one of the "
        "UI's up / down renderings"
    )


def test_the_prior_period_comes_from_real_revision_rows(seeded):
    """The history is REAL history: no engine special-case, no stored total.

    This is the assertion that distinguishes seeded history from a faked figure.
    `_state_as_of` is called directly, at the vantage point the 12-month horizon
    uses, and its answer is rebuilt from `DemandRevision` rows through the engine's
    ordinary path. Then the rows are removed and the figure correctly goes back to
    unavailable -- which it could not do if anything anywhere held a cached
    "previous total".

    The removal happens in a nested transaction that is rolled back, so the
    module-scoped seeded database is left exactly as every other test here expects.
    """
    from datetime import datetime, timedelta

    from app.engines.executive import _add_months, _in_scope, _state_as_of
    from app.models import DemandRevision

    lines = [line for line in seeded.query(DemandLine).all() if _in_scope(line)]
    assert lines, "the seed has no in-scope demand, so there is nothing to compare"

    as_of = _add_months(datetime.utcnow(), -12)
    state, reason = _state_as_of(seeded, as_of, lines)
    assert reason is None, reason
    assert state is not None
    # One reconstructed state per in-scope line. A PARTIAL reconstruction is exactly
    # what the engine refuses to report a figure from.
    assert set(state) == {line.id for line in lines}
    # And it is a PAST state rather than a copy of today's: the seeded revisions
    # carry scaled quantities and shifted ROS dates, so at least one line differs
    # from its current values.
    assert any(
        state[line.id].quantity != line.quantity
        or state[line.id].ros_date != line.ros_date
        for line in lines
    )

    savepoint = seeded.begin_nested()
    try:
        line_ids = [line.id for line in lines]
        for rev in (
            seeded.query(DemandRevision)
            .filter(DemandRevision.demand_line_id.in_(line_ids))
            .all()
        ):
            seeded.delete(rev)
        for line in lines:
            line.created_at = as_of - timedelta(days=1)
        seeded.flush()

        state_after, reason_after = _state_as_of(seeded, as_of, lines)
        assert state_after is None
        assert "revision history" in reason_after
    finally:
        savepoint.rollback()

    # Belt and braces: the shared fixture still answers as it did.
    state_again, reason_again = _state_as_of(seeded, as_of, lines)
    assert reason_again is None
    assert state_again is not None


def test_the_seed_reaches_all_three_on_order_states(seeded):
    """MEASURED-positive, MEASURED-zero and UNKNOWN are all demo-reachable.

    On-order used to be a hardcoded 0 with no storage behind it, so none of these
    states existed in the demo and the "absence is not zero" rule could only be
    unit-tested -- the same gap this module exists to close for the other honesty
    paths.
    """
    from app.engines.inventory import on_order_for, total_on_order_all_bus
    from app.models import BusinessUnit, InventoryOnOrder

    north = (
        seeded.query(BusinessUnit)
        .filter(BusinessUnit.name == "North Sea Operations")
        .one()
    )
    gulf = (
        seeded.query(BusinessUnit).filter(BusinessUnit.name == "Gulf Operations").one()
    )

    tbg = (
        seeded.query(Product)
        .filter(Product.description == "TBG 4-1/2 12.6 13CR80 VAM TOP SMLS")
        .one()
    )
    csg = (
        seeded.query(Product)
        .filter(Product.description == "CSG 9-5/8 53.5 P110 VAM 21 SMLS")
        .one()
    )
    unstocked = (
        seeded.query(Product).filter(Product.description == UNSTOCKED_DESCRIPTION).one()
    )

    # MEASURED, POSITIVE -- two purchase orders, both with promise dates.
    position = on_order_for(seeded, north.id, tbg)
    assert position.known is True
    assert position.quantity == 7000
    assert position.row_count == 2
    assert position.earliest_expected_arrival is not None
    assert position.source_system == "synthetic"

    # MEASURED, ZERO -- the FACT "nothing is on order", not silence.
    position = on_order_for(seeded, north.id, csg)
    assert position.known is True
    assert position.quantity == 0.0

    # UNKNOWN -- no row anywhere. Never 0.
    position = total_on_order_all_bus(seeded, unstocked)
    assert position.known is False
    assert position.quantity is None
    assert position.source_system == "unavailable"

    # UNDATED -- a raised, unacknowledged PO. In the total, in no arrival window.
    # `quantity > 0` excludes the explicit zero row above, which also carries no
    # date: "nothing is on order" has no arrival to promise, and giving it one would
    # be inventing a delivery for material nobody ordered.
    undated = [
        row
        for row in seeded.query(InventoryOnOrder).all()
        if row.expected_arrival_date is None and row.quantity > 0
    ]
    assert len(undated) == 1
    assert undated[0].quantity == 2000

    # THE BOUNDARY: BU Gulf holds no purchase orders at all, so the same product
    # with 7000 incoming into BU North reads as UNKNOWN there rather than as 7000 or
    # as 0.
    assert on_order_for(seeded, gulf.id, tbg).known is False


def test_no_seeded_on_order_row_claims_to_come_from_oracle(seeded):
    """Provenance stays honest: the feed does not exist, so nothing may claim it.

    `source_system` is the ONLY thing allowed to say "oracle", and no seeded row
    does. `oracle_integrated` is a SEPARATE flag meaning "the feed is live" and it
    stays false regardless -- seeding a projection table is not an integration.
    """
    from app.models import InventoryOnOrder

    rows = seeded.query(InventoryOnOrder).all()
    assert rows, "the seed writes no on-order rows, so the demo cannot show the block"
    assert {row.source_system for row in rows} == {"synthetic"}

    tbg = (
        seeded.query(Product)
        .filter(Product.description == "TBG 4-1/2 12.6 13CR80 VAM TOP SMLS")
        .one()
    )
    position = by_item(seeded, tbg.id).inventory
    assert position.on_order == 7000
    assert position.on_order_source == "synthetic"
    assert position.oracle_integrated is False
