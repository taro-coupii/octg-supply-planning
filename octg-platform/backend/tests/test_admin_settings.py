"""The two ADJUSTABLE settings: lead-time components, and the coverage scope default.

The product owner asked for "all adjustable elements in this tab for user to make
adjustment. for example, leadtime assumption per product components. also ... coverage
scope default to be adjustable here." Both were previously unreachable from the API --
lead-time components were seed-only rows, and the coverage scope was a pair of Python
constants -- so everything below is new surface and each test pins one claim that could
plausibly be got wrong.

WHAT THIS MODULE PINS, AND WHY EACH ONE IS HERE
===============================================
LEAD-TIME CRUD
  * The happy path, end to end through HTTP.
  * The (dimension, attribute_value) UNIQUE constraint produces a CLEAN 409 with an
    explanation -- not a raw `IntegrityError` and not a 500. The constraint has existed
    since `ee59aca3b091`; what is new is that a client can now reach it, so the failure
    mode is now user-facing.
  * An invalid `dimension` is refused with a 400 that NAMES the four valid values.
    `dimension` is deliberately not a Pydantic enum precisely so this message can
    exist; a test that only asserted "not 2xx" would pass against the generic 422 the
    design rejects.
  * `attribute_value` and `dimension` cannot be PATCHed. That is a design decision (see
    `app.schemas.LeadTimeComponentPatch`), so it gets a test rather than living only in
    a docstring.

NO CACHING -- THE ONE THAT MATTERS MOST
  * Deleting a component a seeded product depends on flips that product's `modelled`
    status ON THE NEXT READ, with nothing invalidated in between. `resolve_lead_time`
    queries the whole table on every call and memoises nothing, and this test is what
    keeps it that way: an lru_cache added to it for "performance" would break here
    rather than silently serve a superseded lead time.
  * And the half that IS cached: `CoverageResult.status` is PERSISTED, so the
    Uncovered/Unrecoverable choice does NOT self-heal. The mutation endpoints recompute,
    and that is asserted rather than assumed.

COVERAGE SCOPE DEFAULT
  * GET/PUT round trip.
  * Changing it changes what `compute_customer_coverage` ACTUALLY USES -- not merely
    what is stored. This is the load-bearing test of the whole constant/setting design:
    the module constants still exist and are still imported by a dozen call sites, so it
    would be entirely possible to persist a setting nothing read.
  * The recompute-on-save behaviour that was chosen (full, synchronous) is verifiably
    what happens: a stored `CoverageResult` written under the old scope is gone/rewritten
    by the time the PUT returns.
  * An empty filter is refused. It is syntactically legal and catastrophic -- every well
    out of scope, every verdict gone -- so the refusal is a feature with a test.
  * The shipped constants are UNCHANGED as a fallback, and a database with no row
    behaves exactly as it did before. This is what makes the migration safe.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.engines.coverage import (
    DEFAULT_PROFILE_FILTER,
    DEFAULT_STATUS_FILTER,
    compute_customer_coverage,
    recompute_customer,
)
from app.engines.coverage_scope import (
    InvalidCoverageScope,
    coverage_scope,
    effective_profile_filter,
    effective_status_filter,
    set_coverage_scope_defaults,
)
from app.engines.lead_time import resolve_lead_time
from app.main import app
from app.models import (
    ANY_ATTRIBUTE_VALUE,
    AllocationPolicy,
    BusinessUnit,
    CoverageResult,
    CoverageScopeDefault,
    CoverageStatus,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    LeadTimeComponent,
    LeadTimeDimension,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
)


def _http(db_session):
    """A TestClient bound to the test's own session, matching the house pattern."""

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def client(db_session):
    yield _http(db_session)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# A world with a COMPLETE lead-time model, mirroring the seed's shape
# ---------------------------------------------------------------------------


def _world(db, *, quantity=1000.0, on_hand=0.0, days_out=200):
    """One customer, one Confirmed/Primary well, one product, all four dimensions.

    `on_hand` defaults to 0 so the line is short and therefore reaches the
    `is_recoverable` branch -- which is the branch lead time actually decides. A covered
    line would make every lead-time assertion below invisible.

    The component set mirrors the seed's shape deliberately: specific OD/WT, Grade and
    Connection rows plus a WILDCARD Logistics row, so both matching paths are exercised.
    """
    bu = BusinessUnit(name="Admin BU")
    db.add(bu)
    db.flush()
    customer = Customer(
        name="Admin Co",
        allocation_policy=AllocationPolicy.SOFT,
        business_unit_id=bu.id,
    )
    db.add(customer)
    db.flush()
    node = PlanningNode(customer_id=customer.id, node_type="Project", name="Admin Prj")
    db.add(node)
    db.flush()
    well = Well(
        planning_node_id=node.id,
        name="WELL-ADMIN-01",
        demand_status=DemandStatus.CONFIRMED,
    )
    db.add(well)
    db.flush()
    product = Product(
        type="CSG",
        size="9-5/8",
        weight=53.5,
        grade="P110",
        grade_type="Carbon",
        connection="VAM 21",
        commodity="SMLS",
        description="CSG 9-5/8 53.5 P110 VAM 21",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.flush()
    db.add(
        InventoryOnHand(
            business_unit_id=bu.id,
            product_id=product.id,
            quantity=on_hand,
            source_system="synthetic",
        )
    )
    line = DemandLine(
        well_id=well.id,
        product_id=product.id,
        quantity=quantity,
        ros_date=datetime.utcnow() + timedelta(days=days_out),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.add_all(
        [
            LeadTimeComponent(
                dimension=LeadTimeDimension.OD_WT,
                attribute_value="9-5/8 53.5",
                months=4.0,
                label="Ex-mill 9-5/8 53.5",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.GRADE,
                attribute_value="Carbon",
                months=0.5,
                label="Carbon melt slot",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.CONNECTION,
                attribute_value="VAM 21",
                months=1.0,
                label="VAM 21 threading",
            ),
            # The shared row, stored ONCE for everything -- the wildcard path.
            LeadTimeComponent(
                dimension=LeadTimeDimension.LOGISTICS,
                attribute_value=ANY_ATTRIBUTE_VALUE,
                months=2.0,
                label="Sailing",
            ),
        ]
    )
    db.flush()
    return customer, well, product, line


# ---------------------------------------------------------------------------
# Lead-time components: CRUD
# ---------------------------------------------------------------------------


def test_lead_time_component_crud_happy_path(db_session, client):
    """List, create, patch, delete -- and the list reflects each step.

    The whole point of the feature is that a planner who knows the mill queue has
    lengthened can say so, so the assertions follow the resolved TOTAL through every
    step rather than only checking HTTP codes.
    """
    _customer, _well, product, _line = _world(db_session)

    listed = client.get("/admin/lead-time-components")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert len(body["components"]) == 4
    assert body["dimensions"] == ["OD/WT", "Grade", "Connection", "Logistics"]
    assert body["wildcard"] == ANY_ATTRIBUTE_VALUE
    # All four dimensions have rows, so nothing is missing and there is no warning.
    assert body["dimensions_with_no_rows"] == []
    assert body["incomplete_note"] is None
    # The wildcard row is flagged, and only it.
    shared = [c for c in body["components"] if c["shared"]]
    assert [c["dimension"] for c in shared] == ["Logistics"]

    # 4 + 0.5 + 1 + 2
    assert resolve_lead_time(db_session, product).total_months == 7.5

    # CREATE a more specific Logistics row: a specific row beats the wildcard.
    created = client.post(
        "/admin/lead-time-components",
        json={
            "dimension": "Logistics",
            "attribute_value": "Carbon",
            "months": 3.0,
            "label": "Sailing (carbon, longer route)",
        },
    )
    assert created.status_code == 201, created.text
    change = created.json()
    assert change["action"] == "created"
    assert change["component"]["months"] == 3.0
    assert change["component"]["shared"] is False
    component_id = change["component"]["id"]

    # The new row wins over the wildcard, and it is visible immediately -- the total
    # moved by exactly the difference, with no invalidation step anywhere.
    assert resolve_lead_time(db_session, product).total_months == 8.5
    # And the endpoint SAID so, rather than leaving the caller to discover it.
    (reported,) = change["product_changes"]
    assert reported["product_id"] == product.id
    assert reported["total_months_before"] == 7.5
    assert reported["total_months_after"] == 8.5
    assert reported["modelled_before"] is True and reported["modelled_after"] is True
    assert reported["became_unmodelled"] is False
    assert change["products_examined"] == 1

    # PATCH the months.
    patched = client.patch(
        f"/admin/lead-time-components/{component_id}", json={"months": 5.0}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["component"]["months"] == 5.0
    assert resolve_lead_time(db_session, product).total_months == 10.5

    # PATCH the label alone. It is presentation only and must move NO number.
    relabelled = client.patch(
        f"/admin/lead-time-components/{component_id}", json={"label": "Renamed"}
    )
    assert relabelled.status_code == 200, relabelled.text
    assert relabelled.json()["component"]["label"] == "Renamed"
    assert relabelled.json()["component"]["months"] == 5.0
    assert relabelled.json()["product_changes"] == [], (
        "a label is never matched on and never affects arithmetic, so relabelling "
        "must not report a lead-time change"
    )
    assert resolve_lead_time(db_session, product).total_months == 10.5

    # DELETE it -- the wildcard takes over again, so the product stays MODELLED.
    deleted = client.delete(f"/admin/lead-time-components/{component_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["action"] == "deleted"
    assert deleted.json()["component"] is None, (
        "the row is gone; echoing it back would invite a client to render it as if "
        "it still existed"
    )
    breakdown = resolve_lead_time(db_session, product)
    assert breakdown.total_months == 7.5
    assert breakdown.modelled is True

    assert len(client.get("/admin/lead-time-components").json()["components"]) == 4


def test_duplicate_dimension_and_value_is_a_clean_409_not_a_database_error(
    db_session, client
):
    """The UNIQUE constraint reaches the client as an explained 409.

    Two rows for one (dimension, attribute_value) pair make
    `resolve_lead_time` raise `AmbiguousLeadTimeComponent` rather than pick a winner,
    because a silently chosen term is invisible in the published breakdown. That was
    previously unreachable from outside; now that a client can POST, the constraint is
    a user-facing failure and the response has to be a sentence rather than a driver
    message.
    """
    _customer, _well, product, _line = _world(db_session)

    conflict = client.post(
        "/admin/lead-time-components",
        json={"dimension": "Grade", "attribute_value": "Carbon", "months": 9.0},
    )
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    # It names what already exists, so the operator can act without a second request.
    assert "already exists" in detail
    assert "Carbon" in detail
    assert "0.5 months" in detail
    # It explains WHY, not merely that.
    assert "unexplainable" in detail
    # And it points at the corrective action.
    assert "PATCH /admin/lead-time-components/" in detail
    assert "Nothing was saved" in detail
    # Nothing leaked.
    assert "IntegrityError" not in detail
    assert "uq_lead_time_component" not in detail

    # And nothing WAS saved: still four rows, and the lead time is untouched.
    assert db_session.query(LeadTimeComponent).count() == 4
    assert resolve_lead_time(db_session, product).total_months == 7.5


def test_a_duplicate_wildcard_row_says_it_is_the_wildcard(db_session, client):
    """The wildcard conflict gets its own clause, because its blast radius is wider.

    A duplicate specific row is ambiguous for the products matching that value. A
    duplicate WILDCARD row is ambiguous for every product with no more specific row on
    that dimension, which is usually most of the catalogue -- so the message says so.
    """
    _world(db_session)
    conflict = client.post(
        "/admin/lead-time-components",
        json={
            "dimension": "Logistics",
            "attribute_value": ANY_ATTRIBUTE_VALUE,
            "months": 1.0,
        },
    )
    assert conflict.status_code == 409, conflict.text
    assert "WILDCARD" in conflict.json()["detail"]


def test_an_invalid_dimension_is_refused_with_the_four_valid_values(db_session, client):
    """A 400 that NAMES the closed set, not Pydantic's generic schema complaint.

    `dimension` is typed `str` in the request schema for exactly this reason. The set
    of dimensions is a modelling decision -- the spec names four and the resolver
    requires all four -- and a typo'd free-text dimension used to silently create an
    additive term nobody could find. Asserting the message, not merely the status, is
    what stops a future edit from "simplifying" the field to an enum and losing the
    explanation.
    """
    _world(db_session)
    for bad in ("Logistic", "Shipping", "od/wt ", "", "Freight"):
        refused = client.post(
            "/admin/lead-time-components",
            json={"dimension": bad, "attribute_value": "X", "months": 1.0},
        )
        if bad == "od/wt ":
            # Case and surrounding whitespace ARE forgiven -- that is a spelling of a
            # real dimension, not a different one.
            assert refused.status_code == 201, refused.text
            continue
        assert refused.status_code == 400, (bad, refused.text)
        detail = refused.json()["detail"]
        assert "is not a lead-time dimension" in detail
        for valid in ("OD/WT", "Grade", "Connection", "Logistics"):
            assert valid in detail
        assert "CLOSED" in detail
        assert "Nothing was saved" in detail

    # 422 is what a Pydantic enum would have produced. It must not be what we produce.
    assert (
        client.post(
            "/admin/lead-time-components",
            json={"dimension": "Freight", "attribute_value": "X", "months": 1.0},
        ).status_code
        != 422
    )


def test_dimension_and_attribute_value_cannot_be_patched(db_session, client):
    """The identity pair is immutable; re-pointing a term is delete + create.

    A design decision, so it gets a test. Changing (dimension, attribute_value) does
    not edit a term, it re-points it at a different set of products -- silently
    removing it from everything that used to match, which can flip products to "not
    modelled" and retract an Unrecoverable verdict. Extra keys are IGNORED by Pydantic
    rather than rejected, so what this pins is that they have NO EFFECT.
    """
    _world(db_session)
    row = (
        db_session.query(LeadTimeComponent)
        .filter(LeadTimeComponent.dimension == LeadTimeDimension.GRADE)
        .one()
    )
    response = client.patch(
        f"/admin/lead-time-components/{row.id}",
        json={
            "months": 2.0,
            "dimension": "Connection",
            "attribute_value": "13CR",
        },
    )
    assert response.status_code == 200, response.text
    component = response.json()["component"]
    assert component["months"] == 2.0, "the permitted field did change"
    assert component["dimension"] == "Grade", "the dimension must be untouched"
    assert component["attribute_value"] == "Carbon", "the value must be untouched"

    db_session.refresh(row)
    assert row.dimension is LeadTimeDimension.GRADE
    assert row.attribute_value == "Carbon"


def test_an_empty_patch_is_refused_rather_than_reported_as_success(db_session, client):
    """A dropped body must not come back 200. See `app.api.admin`."""
    _world(db_session)
    row = db_session.query(LeadTimeComponent).first()
    refused = client.patch(f"/admin/lead-time-components/{row.id}", json={})
    assert refused.status_code == 400, refused.text
    assert "nothing to update" in refused.json()["detail"]


def test_negative_months_is_refused(db_session, client):
    """A negative term would shorten a lead time and produce a false 'recoverable'."""
    _world(db_session)
    refused = client.post(
        "/admin/lead-time-components",
        json={"dimension": "Grade", "attribute_value": "13CR", "months": -1.0},
    )
    assert refused.status_code == 400, refused.text
    assert "must not be negative" in refused.json()["detail"]

    row = db_session.query(LeadTimeComponent).first()
    refused = client.patch(
        f"/admin/lead-time-components/{row.id}", json={"months": -0.5}
    )
    assert refused.status_code == 400, refused.text


def test_zero_months_is_a_real_answer_and_is_not_the_incomplete_case(db_session, client):
    """0 months means "this dimension adds nothing"; ABSENT means "not modelled".

    The spec's own worked example has Connection at +0 months. It is a ROW's presence,
    never its value, that makes a dimension modelled, and this keeps the two apart.
    """
    _customer, _well, product, _line = _world(db_session)
    row = (
        db_session.query(LeadTimeComponent)
        .filter(LeadTimeComponent.dimension == LeadTimeDimension.CONNECTION)
        .one()
    )
    assert (
        client.patch(
            f"/admin/lead-time-components/{row.id}", json={"months": 0.0}
        ).status_code
        == 200
    )
    breakdown = resolve_lead_time(db_session, product)
    assert breakdown.total_months == 6.5  # 4 + 0.5 + 0 + 2
    assert breakdown.modelled is True, "a 0-month row is configured, not missing"
    assert breakdown.missing_dimensions == ()


# ---------------------------------------------------------------------------
# NOTHING IS CACHED -- and the one thing that IS, is repaired
# ---------------------------------------------------------------------------


def test_deleting_a_depended_on_component_flips_modelled_on_the_next_read(
    db_session, client
):
    """The no-caching guarantee, asserted as a behaviour rather than trusted.

    `resolve_lead_time` queries the whole component table on every call and memoises
    nothing, so removing the last row a product matches on a dimension makes that
    product "not modelled" the very next time anybody asks -- no invalidation, no
    stale window. An `lru_cache` added to that resolver for "performance" would fail
    HERE, which is the point of pinning it.

    It also pins the CONSERVATIVE half of the contract: the matched months are still
    reported for information and are deliberately NOT totalled, because a partial sum
    is a confidently wrong date.
    """
    _customer, _well, product, _line = _world(db_session)
    before = resolve_lead_time(db_session, product)
    assert before.modelled is True and before.total_months == 7.5

    grade_row = (
        db_session.query(LeadTimeComponent)
        .filter(LeadTimeComponent.dimension == LeadTimeDimension.GRADE)
        .one()
    )
    response = client.delete(f"/admin/lead-time-components/{grade_row.id}")
    assert response.status_code == 200, response.text

    # Read fresh, through the resolver, with nothing invalidated in between.
    after = resolve_lead_time(db_session, product)
    assert after.modelled is False
    assert after.total_months == 0.0, (
        "an incomplete set must report 0/not-modelled, never a partial sum -- see "
        "app.engines.lead_time"
    )
    assert after.missing_dimensions == ("Grade",)
    # The matched months are still visible so a planner can see how far the model got.
    assert after.matched_months == 7.0
    assert "NOT MODELLED" in after.note

    # And the endpoint reported exactly that, before anybody had to go looking.
    body = response.json()
    (reported,) = body["product_changes"]
    assert reported["became_unmodelled"] is True
    assert reported["modelled_before"] is True and reported["modelled_after"] is False
    assert reported["total_months_before"] == 7.5
    assert reported["total_months_after"] == 0.0
    assert reported["missing_dimensions_after"] == ["Grade"]
    assert "NOT MODELLED" in body["note"]
    assert "Unrecoverable" in body["note"], (
        "the note has to say that an unmodelled product can no longer be judged "
        "Unrecoverable -- that is the consequence a planner cares about"
    )

    # The list endpoint now warns that a whole dimension is empty, which is the state
    # that looks healthy and is not.
    listing = client.get("/admin/lead-time-components").json()
    assert listing["dimensions_with_no_rows"] == ["Grade"]
    assert "NO product on this platform has a modelled lead time" in listing[
        "incomplete_note"
    ]


def test_deleting_a_component_repairs_the_stored_unrecoverable_verdict(
    db_session, client
):
    """The one thing that IS persisted: the Uncovered/Unrecoverable choice.

    `CoverageResult.status` is stored, and the choice between `UNCOVERED` and
    `UNRECOVERABLE` is made by `is_recoverable` -- which reads lead time -- at recompute
    time. So unlike every other lead-time consumer, this one does NOT self-heal on
    read, and a lead-time edit would otherwise leave a stored verdict describing a
    superseded assumption.

    The endpoint recomputes in the same request. This test proves it by setting up a
    genuinely UNRECOVERABLE line (ROS closer than the lead time), then deleting a
    component so the product becomes unmodelled -- at which point `is_recoverable`
    must stop judging it hopeless, because absent data means "cannot judge".
    """
    # ROS 30 days out against a 7.5-month lead time: not recoverable even if ordered
    # today.
    customer, well, product, line = _world(db_session, days_out=30)
    recompute_customer(db_session, customer)
    stored = db_session.get(CoverageResult, line.id)
    assert stored.status is CoverageStatus.UNRECOVERABLE, (
        "the fixture must actually produce the terminal verdict, or this test proves "
        "nothing"
    )
    assert well.coverage_status == CoverageStatus.UNCOVERED.value

    grade_row = (
        db_session.query(LeadTimeComponent)
        .filter(LeadTimeComponent.dimension == LeadTimeDimension.GRADE)
        .one()
    )
    response = client.delete(f"/admin/lead-time-components/{grade_row.id}")
    assert response.status_code == 200, response.text

    # THE STORED ROW WAS REWRITTEN, in that same request.
    db_session.expire_all()
    repaired = db_session.get(CoverageResult, line.id)
    assert repaired.status is CoverageStatus.UNCOVERED, (
        "an unmodelled product is never judged Unrecoverable -- absent data means "
        "'cannot judge', not 'hopeless' -- so the stored terminal verdict had to be "
        "retracted by this call, not left for a later recompute"
    )
    assert response.json()["recomputed_customer_ids"] == [customer.id]


def test_the_resolver_is_not_memoised_anywhere(db_session):
    """Structural guard on the no-caching claim, beside the behavioural one above.

    Stated as a test because it is a DESIGN commitment a well-meaning edit would break:
    a decorator on `resolve_lead_time` or `_rows_by_dimension` would make every
    lead-time edit invisible until a process restart, and the behavioural test above
    could in principle be satisfied by a cache that happened to be keyed on something
    that changed.
    """
    from app.engines import lead_time

    for name in ("resolve_lead_time", "_rows_by_dimension", "total_lead_time_months"):
        fn = getattr(lead_time, name)
        assert not hasattr(fn, "cache_info"), (
            f"{name} has been given a cache. Lead time is resolved fresh on every read "
            "so an administrator's edit is visible immediately; a cache here would "
            "serve a superseded lead time into is_recoverable and the MRP order dates."
        )
        assert not hasattr(fn, "cache_clear")


# ---------------------------------------------------------------------------
# Coverage scope defaults
# ---------------------------------------------------------------------------


def test_the_shipped_constants_are_unchanged_and_are_the_fallback(db_session):
    """A database with no settings row behaves exactly as it did before.

    This is what makes the migration safe and what keeps every existing test honest.
    The constants are still `{Confirmed}` and `{Primary, Contingency}` -- pinned
    independently by `tests/test_coverage_engine.py` -- and with no row present the
    accessors return precisely them.
    """
    assert db_session.query(CoverageScopeDefault).count() == 0

    assert effective_status_filter(db_session) == DEFAULT_STATUS_FILTER
    assert effective_profile_filter(db_session) == DEFAULT_PROFILE_FILTER

    scope = coverage_scope(db_session)
    assert scope.persisted is False, (
        "no row exists, so the platform is running on the shipped values -- not on a "
        "stored copy of them"
    )
    assert scope.updated_at is None
    assert scope.matches_shipped_default is True


def test_get_and_put_coverage_scope_defaults_round_trip(db_session, client):
    """The GET reports provenance, and the PUT changes it."""
    _world(db_session)

    got = client.get("/admin/coverage-scope-defaults")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status_filter"] == ["Confirmed"]
    assert body["profile_filter"] == ["Contingency", "Primary"]
    assert body["persisted"] is False
    assert body["matches_shipped_default"] is True
    assert body["shipped_status_filter"] == ["Confirmed"]
    # The vocabulary is served so the UI cannot offer an option the server refuses.
    assert set(body["available_statuses"]) == {s.value for s in DemandStatus}
    assert set(body["available_profiles"]) == {p.value for p in DemandProfile}
    assert "NOT EVALUATED" in body["note"]
    assert "Nobody has adjusted the scope" in body["note"]

    put = client.put(
        "/admin/coverage-scope-defaults",
        json={
            "status_filter": ["Confirmed", "Budgeted"],
            "profile_filter": ["Primary"],
        },
    )
    assert put.status_code == 200, put.text
    change = put.json()
    assert change["unchanged"] is False
    assert change["status_filter_before"] == ["Confirmed"]
    assert change["status_filter_after"] == ["Budgeted", "Confirmed"]
    assert change["profile_filter_before"] == ["Contingency", "Primary"]
    assert change["profile_filter_after"] == ["Primary"]
    assert change["recomputed_synchronously"] is True

    after = client.get("/admin/coverage-scope-defaults").json()
    assert after["status_filter"] == ["Budgeted", "Confirmed"]
    assert after["profile_filter"] == ["Primary"]
    assert after["persisted"] is True
    assert after["matches_shipped_default"] is False
    assert after["updated_at"] is not None
    assert "This scope was set from this screen" in after["note"]


def test_saving_the_scope_already_in_effect_writes_nothing(db_session, client):
    """A no-op must not create a row, and must not pay the platform-wide recompute."""
    _world(db_session)
    put = client.put(
        "/admin/coverage-scope-defaults",
        json={
            "status_filter": ["Confirmed"],
            "profile_filter": ["Primary", "Contingency"],
        },
    )
    assert put.status_code == 200, put.text
    assert put.json()["unchanged"] is True
    assert put.json()["recomputed_synchronously"] is False
    assert put.json()["recomputed_customer_ids"] == []
    assert "already in effect" in put.json()["note"]
    assert db_session.query(CoverageScopeDefault).count() == 0, (
        "saving the shipped values on a never-adjusted platform must not create a row "
        "that changes nothing -- 'never adjusted' and 'adjusted back to shipped' are "
        "different facts the GET reports separately"
    )


def test_changing_the_default_changes_what_compute_customer_coverage_USES(db_session):
    """THE load-bearing test of the constant/setting design.

    The module constants still exist and are still imported by a dozen call sites, so
    it would be entirely possible to build a settings table that persisted a value
    nothing ever read. This asserts the opposite: the engine's behaviour changes.

    A BUDGETED well is out of scope under the shipped default, so its lines get no
    verdict at all. After the scope is widened to include Budgeted, the SAME call with
    no explicit filters must evaluate it.
    """
    customer, well, product, line = _world(db_session, on_hand=10_000)
    # A second well, Budgeted, sharing the product -- so it is genuinely out of scope
    # rather than merely absent.
    budgeted = Well(
        planning_node_id=well.planning_node_id,
        name="WELL-ADMIN-02-BUDGETED",
        demand_status=DemandStatus.BUDGETED,
    )
    db_session.add(budgeted)
    db_session.flush()
    budgeted_line = DemandLine(
        well_id=budgeted.id,
        product_id=product.id,
        quantity=500.0,
        ros_date=datetime.utcnow() + timedelta(days=250),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(budgeted_line)
    db_session.flush()

    before = compute_customer_coverage(db_session, customer)
    assert budgeted_line.id not in before.by_line
    assert budgeted_line.id in before.excluded_line_ids
    assert before.well_status[budgeted.id] is None, "out of scope means NOT EVALUATED"

    set_coverage_scope_defaults(
        db_session,
        {DemandStatus.CONFIRMED, DemandStatus.BUDGETED},
        set(DEFAULT_PROFILE_FILTER),
    )

    # Same call. No explicit filters. Different answer.
    after = compute_customer_coverage(db_session, customer)
    assert budgeted_line.id in after.by_line, (
        "the persisted default must be what the engine actually reads -- otherwise the "
        "setting is decoration"
    )
    assert budgeted_line.id not in after.excluded_line_ids
    assert after.well_status[budgeted.id] is not None

    # And the reverse direction: narrowing the PROFILE filter drops a Contingency line.
    contingency = DemandLine(
        well_id=well.id,
        product_id=product.id,
        quantity=100.0,
        ros_date=datetime.utcnow() + timedelta(days=260),
        profile=DemandProfile.CONTINGENCY,
    )
    db_session.add(contingency)
    db_session.flush()
    assert contingency.id in compute_customer_coverage(db_session, customer).by_line

    set_coverage_scope_defaults(
        db_session,
        {DemandStatus.CONFIRMED, DemandStatus.BUDGETED},
        {DemandProfile.PRIMARY},
    )
    narrowed = compute_customer_coverage(db_session, customer)
    assert contingency.id not in narrowed.by_line
    assert contingency.id in narrowed.excluded_line_ids


def test_saving_the_scope_recomputes_every_customer_synchronously(db_session, client):
    """The recompute behaviour that was CHOSEN is verifiably what happens.

    Deferring was rejected (see `app.engines.coverage_scope`): a stored
    `CoverageResult` computed against the old include-set answers a question nobody
    asked, and the coverage grid would serve it under a label claiming the new scope.

    So this asserts the repair, not the intention: a verdict row that exists under the
    old scope must be GONE by the time the PUT returns, and one that did not exist must
    have appeared -- with no other call in between.
    """
    customer, well, product, line = _world(db_session, on_hand=10_000)
    budgeted = Well(
        planning_node_id=well.planning_node_id,
        name="WELL-ADMIN-03-BUDGETED",
        demand_status=DemandStatus.BUDGETED,
    )
    db_session.add(budgeted)
    db_session.flush()
    budgeted_line = DemandLine(
        well_id=budgeted.id,
        product_id=product.id,
        quantity=500.0,
        ros_date=datetime.utcnow() + timedelta(days=250),
        profile=DemandProfile.PRIMARY,
    )
    db_session.add(budgeted_line)
    db_session.flush()
    recompute_customer(db_session, customer)

    assert db_session.get(CoverageResult, line.id) is not None
    assert db_session.get(CoverageResult, budgeted_line.id) is None
    assert budgeted.coverage_status is None

    put = client.put(
        "/admin/coverage-scope-defaults",
        json={
            "status_filter": ["Budgeted"],
            "profile_filter": ["Primary"],
        },
    )
    assert put.status_code == 200, put.text
    change = put.json()
    assert change["recomputed_synchronously"] is True
    assert change["recomputed_customer_ids"] == [customer.id]
    assert change["recomputed_well_count"] == 2

    db_session.expire_all()
    # The Confirmed well left scope, so its stored verdict is DELETED -- absence now
    # means "not currently evaluated", which is the whole point.
    assert db_session.get(CoverageResult, line.id) is None, (
        "a verdict for a line the engine no longer evaluates is misinformation, not "
        "merely stale, and had to go in this same request"
    )
    # The Budgeted well entered scope, so it has a verdict for the first time.
    assert db_session.get(CoverageResult, budgeted_line.id) is not None
    assert db_session.get(Well, budgeted.id).coverage_status is not None
    assert db_session.get(Well, well.id).coverage_status is None

    # Both transitions are REPORTED, in both directions.
    moved = {w["well_name"]: (w["coverage_before"], w["coverage_after"]) for w in change[
        "well_changes"
    ]}
    assert moved["WELL-ADMIN-01"][1] is None
    assert moved["WELL-ADMIN-03-BUDGETED"][0] is None
    assert moved["WELL-ADMIN-03-BUDGETED"][1] is not None
    assert "changed coverage rollup" in change["note"]


def test_an_empty_filter_is_refused_with_a_stated_reason(db_session, client):
    """Legal, catastrophic, and therefore refused.

    An empty status filter puts every well out of scope, so every line on the platform
    becomes "not evaluated" and the coverage grid empties. No UI accident may reach it,
    and the refusal has to say why rather than emit a schema complaint.
    """
    _world(db_session)
    for payload in (
        {"status_filter": [], "profile_filter": ["Primary"]},
        {"status_filter": ["Confirmed"], "profile_filter": []},
    ):
        refused = client.put("/admin/coverage-scope-defaults", json=payload)
        assert refused.status_code == 400, refused.text
        detail = refused.json()["detail"]
        assert "empty" in detail
        assert "out of scope" in detail

    # And directly on the engine, which is the only writer -- a caller that skipped the
    # parser must not be able to empty the platform either.
    with pytest.raises(InvalidCoverageScope):
        set_coverage_scope_defaults(db_session, set(), set(DEFAULT_PROFILE_FILTER))
    with pytest.raises(InvalidCoverageScope):
        set_coverage_scope_defaults(db_session, set(DEFAULT_STATUS_FILTER), set())

    assert db_session.query(CoverageScopeDefault).count() == 0


def test_an_unknown_status_or_profile_is_refused_and_never_skipped(db_session, client):
    """An unrecognised token narrows the scope by whatever was misspelled. Refuse it."""
    _world(db_session)
    refused = client.put(
        "/admin/coverage-scope-defaults",
        json={"status_filter": ["Confirmed", "Approved"], "profile_filter": ["Primary"]},
    )
    assert refused.status_code == 400, refused.text
    detail = refused.json()["detail"]
    assert "'Approved' is not a demand status" in detail
    assert "Nothing was saved" in detail
    assert db_session.query(CoverageScopeDefault).count() == 0

    refused = client.put(
        "/admin/coverage-scope-defaults",
        json={"status_filter": ["Confirmed"], "profile_filter": ["Secondary"]},
    )
    assert refused.status_code == 400, refused.text
    assert "is not a demand profile" in refused.json()["detail"]


def test_only_one_settings_row_can_ever_exist(db_session, client):
    """Repeated saves UPDATE the singleton; they do not accumulate rows.

    Two rows would mean two platform-wide defaults, and whichever a query returned
    first would decide every verdict -- unexplainably, and possibly differently between
    two servers reading the same data. The CHECK constraint enforces it in the
    database; this pins that the engine takes the update path.
    """
    _world(db_session)
    for statuses in (["Budgeted"], ["Planned"], ["Confirmed", "Planned"]):
        response = client.put(
            "/admin/coverage-scope-defaults",
            json={"status_filter": statuses, "profile_filter": ["Primary"]},
        )
        assert response.status_code == 200, response.text
        assert db_session.query(CoverageScopeDefault).count() == 1

    (row,) = db_session.query(CoverageScopeDefault).all()
    assert row.id == "platform"
    # Stored canonically and sorted, so the column is stable between saves of one set.
    assert row.status_filter == "Confirmed,Planned"
    assert row.profile_filter == "Primary"


def test_the_adjusted_scope_reaches_every_screen_that_re_applies_it(db_session, client):
    """The belt-and-braces filters had to follow the setting, not the constant.

    Several read paths re-apply the scope defensively before serving a stored verdict
    (`app.api.wells._well_out`, the demand list, the dashboard's pending-approvals
    card). Every one of them previously compared against the module constant. Left that
    way, widening the scope would have had the engine WRITE verdicts for newly in-scope
    wells while each of those screens HID them -- so the Well Workspace would show no
    verdict for a well the coverage grid showed one for.
    """
    customer, well, product, line = _world(db_session, on_hand=10_000)
    budgeted = Well(
        planning_node_id=well.planning_node_id,
        name="WELL-ADMIN-04-BUDGETED",
        demand_status=DemandStatus.BUDGETED,
    )
    db_session.add(budgeted)
    db_session.flush()
    db_session.add(
        DemandLine(
            well_id=budgeted.id,
            product_id=product.id,
            quantity=500.0,
            ros_date=datetime.utcnow() + timedelta(days=250),
            profile=DemandProfile.PRIMARY,
        )
    )
    db_session.flush()
    recompute_customer(db_session, customer)

    # Out of scope: listed, but served no verdict.
    served = client.get(f"/wells/{budgeted.id}").json()
    assert len(served["demand_lines"]) == 1
    assert served["demand_lines"][0]["coverage_status"] is None

    assert (
        client.put(
            "/admin/coverage-scope-defaults",
            json={
                "status_filter": ["Confirmed", "Budgeted"],
                "profile_filter": ["Primary", "Contingency"],
            },
        ).status_code
        == 200
    )

    served = client.get(f"/wells/{budgeted.id}").json()
    assert served["demand_lines"][0]["coverage_status"] is not None, (
        "the well is in scope now and the engine wrote it a verdict; a guard still "
        "pinned to the shipped constant would withhold it"
    )

    # The densest list screen in the platform re-applies the scope too, in SQL and
    # again per row. Both had to follow the setting.
    rows = client.get("/demand-lines").json()["rows"]
    budgeted_row = next(r for r in rows if r["well_id"] == budgeted.id)
    assert budgeted_row["coverage_status"] is not None
