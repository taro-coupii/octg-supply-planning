"""Scenario Planning endpoints -- Phase 4.

Which routes write, and which cannot
------------------------------------
Only three handlers here reach production data, and they are the obvious three:
POST /scenarios (creates a scenario), the override add/remove pair, and
POST /scenarios/{id}/apply. Everything else is read-only.

GET /scenarios/{id}/preview in particular does NOT call `db.commit()`, exactly as
GET /analysis/cross-customer-sharing does not. The engine behind it writes nothing
(see `app.engines.scenario` for the four mechanisms enforcing that) so there is
nothing to commit -- and no path by which a GET could overwrite the official
coverage answer.

GET /scenarios also runs a preview per row, to produce the headline coverage
delta the list screen shows. Same reasoning: reads only. A preview that raises
for one scenario is caught and reported on that row alone, so one bad scenario
cannot blank the whole list.

Scenarios are SHARED
--------------------
No handler filters by user and none accepts a caller identity. `created_by` is
attribution the client supplies for display. There is no per-user ownership or
visibility model to build here, deliberately -- see app.models.scenario.Scenario.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db import get_db
from app.engines.overrides import (
    OVERRIDE_ENUM_VALUES,
    OVERRIDE_FIELDS,
    SUPPLY_KINDS,
    UNMODELLED_KINDS,
    OverrideError,
    validate,
)
from app.engines.scenario import (
    ScenarioImmutable,
    assert_mutable,
    assert_target_in_scope,
    preview,
)
from app.engines.scenario_apply import ScenarioNotApplicable, apply_to_base_plan
from app.quantities import InvalidQuantity, validate_quantity
from app.auth.scope import planner_bu
from app.models import (
    User,
    Customer,
    DemandLine,
    EDITABLE_SCENARIO_STATUSES,
    Product,
    Scenario,
    ScenarioOverride,
    ScenarioStatus,
    ScenarioTargetKind,
)
from app.schemas import (
    OverrideFieldsOut,
    ScenarioApplyOut,
    ScenarioDetailOut,
    ScenarioImpactOut,
    ScenarioIn,
    ScenarioOverrideIn,
    ScenarioOverrideOut,
    ScenarioPatch,
    ScenarioSummaryOut,
)

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _get_scenario(db: Session, scenario_id: str) -> Scenario:
    scenario = db.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return scenario


def _require_mutable(scenario: Scenario) -> None:
    """409 rather than 400: the request is well formed, the scenario's STATE
    forbids it. See app.models.scenario.Scenario for why Applied is terminal."""
    try:
        assert_mutable(scenario)
    except ScenarioImmutable as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _summary(
    db: Session, scenario: Scenario, with_delta: bool = True
) -> ScenarioSummaryOut:
    """Serialise one scenario, optionally with its headline coverage delta.

    The delta comes from the read-only preview, so the list screen's headline
    figure is produced by the same engine as the editor's detail -- there is no
    second, cheaper approximation that could disagree with it.
    """
    delta_wells: int | None = None
    delta_lines: int | None = None
    error: str | None = None
    if with_delta:
        try:
            impact = preview(db, scenario)
            delta_wells = impact.changed_well_count
            delta_lines = impact.changed_line_count
        except Exception as exc:  # noqa: BLE001 -- one bad row must not blank the list
            error = f"{type(exc).__name__}: {exc}"

    return ScenarioSummaryOut(
        id=scenario.id,
        name=scenario.name,
        description=scenario.description,
        customer_id=scenario.customer_id,
        customer_name=scenario.customer.name,
        status=scenario.status,
        created_by=scenario.created_by,
        created_by_user_id=scenario.created_by_user_id,
        created_by_user_name=scenario.created_by_user_name,
        created_at=scenario.created_at,
        updated_at=scenario.updated_at,
        applied_at=scenario.applied_at,
        override_count=len(scenario.overrides),
        coverage_delta_wells=delta_wells,
        coverage_delta_lines=delta_lines,
        preview_error=error,
    )


# --------------------------------------------------------------------------
# The override vocabulary
# --------------------------------------------------------------------------


@router.get("/override-fields", response_model=OverrideFieldsOut)
def get_override_fields():
    """What may be overridden, straight from the engine.

    Served so the editor's field pickers and value types cannot drift from
    `app.engines.overrides.OVERRIDE_FIELDS`. A hardcoded copy in the frontend
    would be a second definition of the override vocabulary, and the frontend
    would be the one that got it wrong.

    Declared BEFORE /{scenario_id} so the literal path is not swallowed by the
    parameterised one.
    """
    return OverrideFieldsOut(
        fields={
            kind.value: dict(fields) for kind, fields in OVERRIDE_FIELDS.items()
        },
        enum_values={
            f"{kind.value}.{field}": list(values)
            for (kind, field), values in OVERRIDE_ENUM_VALUES.items()
        },
        supply_kinds=[k.value for k in SUPPLY_KINDS],
        unmodelled_kinds=[k.value for k in UNMODELLED_KINDS],
    )


# --------------------------------------------------------------------------
# Scenario CRUD
# --------------------------------------------------------------------------


@router.get("", response_model=list[ScenarioSummaryOut])
def list_scenarios(
    customer_id: str | None = None,
    include_delta: bool = True,
    db: Session = Depends(get_db),
    bu_scope: str | None = Depends(planner_bu),
):
    """Every scenario, newest first, optionally narrowed to one customer.

    SHARED: no filtering by user, ever. `include_delta=false` skips the per-row
    preview for callers that only need the metadata.
    """
    query = db.query(Scenario)
    if bu_scope is not None:
        query = query.join(Customer, Scenario.customer_id == Customer.id).filter(
            Customer.business_unit_id == bu_scope
        )
    if customer_id is not None:
        query = query.filter(Scenario.customer_id == customer_id)
    scenarios = query.order_by(Scenario.created_at.desc()).all()
    return [_summary(db, s, with_delta=include_delta) for s in scenarios]


@router.post("", response_model=ScenarioDetailOut, status_code=201)
def create_scenario(body: ScenarioIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a Draft scenario against one customer."""
    customer = db.get(Customer, body.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    scenario = Scenario(
        name=body.name,
        description=body.description,
        customer_id=customer.id,
        status=ScenarioStatus.DRAFT,
        created_by=body.created_by,
        created_by_user_id=user.id,
    )
    db.add(scenario)
    db.commit()
    db.refresh(scenario)
    return _detail(db, scenario)


def _detail(db: Session, scenario: Scenario) -> ScenarioDetailOut:
    summary = _summary(db, scenario)
    return ScenarioDetailOut(
        **summary.model_dump(),
        overrides=[
            ScenarioOverrideOut.model_validate(o, from_attributes=True)
            for o in scenario.overrides
        ],
    )


@router.get("/{scenario_id}", response_model=ScenarioDetailOut)
def get_scenario(scenario_id: str, db: Session = Depends(get_db)):
    """One scenario with every override on it."""
    return _detail(db, _get_scenario(db, scenario_id))


@router.patch("/{scenario_id}", response_model=ScenarioDetailOut)
def patch_scenario(
    scenario_id: str, body: ScenarioPatch, db: Session = Depends(get_db)
):
    """Rename, re-describe, or move through the lifecycle.

    Cannot set status to Applied -- applying is not a metadata edit, it writes
    production data, and the only way to do it is POST /{id}/apply. Cannot touch
    an already-applied scenario at all.
    """
    scenario = _get_scenario(db, scenario_id)
    _require_mutable(scenario)

    if body.status == ScenarioStatus.APPLIED:
        raise HTTPException(
            status_code=400,
            detail=(
                "A scenario cannot be marked Applied directly. Applying writes "
                "demand revisions into production data -- use "
                f"POST /scenarios/{scenario_id}/apply, which does the work and "
                "sets the status as a consequence."
            ),
        )

    if body.name is not None:
        scenario.name = body.name
    if body.description is not None:
        scenario.description = body.description
    if body.status is not None:
        if body.status not in EDITABLE_SCENARIO_STATUSES:
            raise HTTPException(
                status_code=400, detail=f"Cannot move a scenario to {body.status.value}."
            )
        scenario.status = body.status

    scenario.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(scenario)
    return _detail(db, scenario)


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------


def _check_override_quantity(db: Session, override) -> None:
    """The unit-aware half of the quantity rule, once the product is known.

    `overrides.validate` runs without a session and can only say "not negative".
    Here the target resolves to a product, so 12.5 pieces on a PC-counted line
    and a zero demand quantity are refused the same way every other writer
    refuses them (app.quantities). Raised as 400 with the sentence.
    """
    if override.value_number is None:
        return
    product = None
    if override.target_demand_line_id:
        line = db.get(DemandLine, override.target_demand_line_id)
        product = line.product if line is not None else None
    elif override.target_product_id:
        product = db.get(Product, override.target_product_id)
    is_demand_qty = (
        override.target_kind == ScenarioTargetKind.DEMAND_LINE
        and override.field_name == "quantity"
    )
    try:
        validate_quantity(
            override.value_number,
            kind="demand" if is_demand_qty else "stock",
            unit=product.unit_of_measure if product is not None else None,
            label=f"{override.target_kind.value}.{override.field_name}",
        )
    except InvalidQuantity as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{scenario_id}/overrides", response_model=ScenarioOverrideOut, status_code=201)
def add_override(
    scenario_id: str, body: ScenarioOverrideIn, db: Session = Depends(get_db)
):
    """Add one override.

    Validated BEFORE it is persisted, by the same
    `app.engines.overrides.validate` the resolver re-runs at read time. A rejected
    override never lands, so a scenario can never contain an override that the
    preview would silently skip -- including a cross-Business-Unit inventory
    override, which is refused with 400 and an explanation.
    """
    scenario = _get_scenario(db, scenario_id)
    _require_mutable(scenario)

    override = ScenarioOverride(
        scenario_id=scenario.id,
        target_kind=body.target_kind,
        field_name=body.field_name,
        target_demand_line_id=body.target_demand_line_id,
        target_well_id=body.target_well_id,
        target_product_id=body.target_product_id,
        target_business_unit_id=body.target_business_unit_id,
        target_from_product_id=body.target_from_product_id,
        target_to_product_id=body.target_to_product_id,
        value_number=body.value_number,
        value_date=body.value_date,
        value_text=body.value_text,
        note=body.note,
    )

    try:
        validate(override, scenario.customer)
        _check_override_quantity(db, override)
        assert_target_in_scope(db, scenario, override)
    except (OverrideError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    db.add(override)
    scenario.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(override)
    return ScenarioOverrideOut.model_validate(override, from_attributes=True)


@router.delete("/{scenario_id}/overrides/{override_id}", status_code=204)
def delete_override(
    scenario_id: str, override_id: str, db: Session = Depends(get_db)
):
    """Remove one override."""
    scenario = _get_scenario(db, scenario_id)
    _require_mutable(scenario)

    override = db.get(ScenarioOverride, override_id)
    if override is None or override.scenario_id != scenario.id:
        raise HTTPException(status_code=404, detail="Override not found on this scenario")

    db.delete(override)
    scenario.updated_at = datetime.utcnow()
    db.commit()
    return None


# --------------------------------------------------------------------------
# Preview and apply
# --------------------------------------------------------------------------


@router.get("/{scenario_id}/preview", response_model=ScenarioImpactOut)
def get_preview(scenario_id: str, db: Session = Depends(get_db)):
    """Coverage, MRP and Risk impact of this scenario. A READ-ONLY WHAT-IF.

    Deliberately no `db.commit()`: the engine writes nothing, so there is nothing
    to commit. The official coverage verdict is untouched by this call and stays
    exactly what the coverage engine last wrote.
    """
    scenario = _get_scenario(db, scenario_id)
    try:
        impact = preview(db, scenario)
    except OverrideError as exc:
        # A persisted override that no longer validates (e.g. written by an older
        # build). Better a loud 409 than a preview computed with it silently
        # dropped.
        raise HTTPException(
            status_code=409,
            detail=(
                "This scenario contains an override that is no longer valid, so no "
                f"preview can be computed: {exc}"
            ),
        )
    return ScenarioImpactOut.model_validate(impact, from_attributes=True)


@router.post("/{scenario_id}/apply", response_model=ScenarioApplyOut)
def apply_scenario(
    scenario_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Write this scenario's overrides into production data. THIS ONE WRITES.

    Demand changes go through the revision machinery, so a DemandRevision and an
    ImpactRecord are created for every line that moves. Supply overrides are
    refused (409) because they target Oracle-owned projections -- see
    `app.engines.scenario_apply.apply_to_base_plan`. An already-applied scenario
    is refused (409) too.

    The refusals happen before anything is written, so a rejected apply leaves the
    base plan exactly as it was.
    """
    scenario = _get_scenario(db, scenario_id)
    try:
        result = apply_to_base_plan(db, scenario, actor_user_id=user.id)
    except (ScenarioImmutable, ScenarioNotApplicable) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except OverrideError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return ScenarioApplyOut.model_validate(result, from_attributes=True)
