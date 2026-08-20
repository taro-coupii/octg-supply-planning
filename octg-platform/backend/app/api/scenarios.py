# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
"""spec §シナリオAPI. CRUD sans DELETE on scenarios (裁定 E-4: intentionally no
DELETE endpoint — invariant 6). Overrides add/delete Draft-only. preview
(E-3) and apply (E-4) delegate to app.engines.scenario.
"""

from __future__ import annotations

import datetime
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.scenario import SupplyOverrideRejected, apply_scenario, preview_scenario
from app.models import (
    DemandLine,
    InventoryAssignment,
    InventoryOnOrder,
    Product,
    Scenario,
    ScenarioOverride,
    ScenarioOverrideKind,
    ScenarioStatus,
    SubstitutionApproval,
    Well,
)

router = APIRouter(prefix="/scenarios")


# --- payload validation per override kind -----------------------------------


def _validate_payload(kind: ScenarioOverrideKind, payload: dict) -> None:
    if kind == ScenarioOverrideKind.QUANTITY:
        value = payload.get("value")
        if not isinstance(value, (int, float)) or value <= 0:
            raise HTTPException(status_code=422, detail="quantity override requires numeric payload.value > 0")
    elif kind == ScenarioOverrideKind.ROS_DATE:
        value = payload.get("value")
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail="ros_date override requires payload.value as ISO date string")
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            raise HTTPException(status_code=422, detail="ros_date override payload.value must be an ISO date")
    elif kind == ScenarioOverrideKind.WELL_STATUS:
        value = payload.get("value")
        valid = {"Planned", "Budgeted", "Confirmed"}
        if value not in valid:
            raise HTTPException(status_code=422, detail=f"well_status override payload.value must be one of {valid}")
    elif kind == ScenarioOverrideKind.PO_ARRIVAL:
        value = payload.get("value")
        if value is not None:
            if not isinstance(value, str):
                raise HTTPException(status_code=422, detail="po_arrival override payload.value must be an ISO date or null")
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                raise HTTPException(status_code=422, detail="po_arrival override payload.value must be an ISO date")
    elif kind == ScenarioOverrideKind.HARD_RELEASE:
        pass  # no payload fields required
    elif kind == ScenarioOverrideKind.APPROVAL_FLIP:
        if "customer_approved" in payload and not isinstance(payload["customer_approved"], bool):
            raise HTTPException(status_code=422, detail="approval_flip payload.customer_approved must be boolean")
        if "well_approved" in payload and not isinstance(payload["well_approved"], bool):
            raise HTTPException(status_code=422, detail="approval_flip payload.well_approved must be boolean")
        if "status" in payload and payload["status"] not in {"Pending", "Approved", "Rejected"}:
            raise HTTPException(status_code=422, detail="approval_flip payload.status invalid")


def _target_exists(db: Session, kind: ScenarioOverrideKind, target_id: str) -> bool:
    if kind in (ScenarioOverrideKind.QUANTITY, ScenarioOverrideKind.ROS_DATE):
        return db.get(DemandLine, target_id) is not None
    if kind == ScenarioOverrideKind.WELL_STATUS:
        return db.get(Well, target_id) is not None
    if kind == ScenarioOverrideKind.PO_ARRIVAL:
        return db.get(InventoryOnOrder, target_id) is not None
    if kind == ScenarioOverrideKind.HARD_RELEASE:
        return db.get(InventoryAssignment, target_id) is not None
    if kind == ScenarioOverrideKind.APPROVAL_FLIP:
        return db.get(SubstitutionApproval, target_id) is not None
    return False


def _target_name(db: Session, kind: ScenarioOverrideKind, target_id: str) -> str:
    """Resolve a human-readable name for `target_id` — API responses must
    never surface a bare UUID (spec §データモデル note)."""
    if kind in (ScenarioOverrideKind.QUANTITY, ScenarioOverrideKind.ROS_DATE):
        line = db.get(DemandLine, target_id)
        if line is None:
            return target_id
        well = db.get(Well, line.well_id)
        product = db.get(Product, line.product_id)
        well_name = well.name if well else target_id
        product_name = product.name if product else line.product_id
        return f"{well_name} / {product_name}"
    if kind == ScenarioOverrideKind.WELL_STATUS:
        well = db.get(Well, target_id)
        return well.name if well else target_id
    if kind == ScenarioOverrideKind.PO_ARRIVAL:
        po = db.get(InventoryOnOrder, target_id)
        if po is None:
            return target_id
        product = db.get(Product, po.product_id)
        return product.name if product else po.product_id
    if kind == ScenarioOverrideKind.HARD_RELEASE:
        assignment = db.get(InventoryAssignment, target_id)
        if assignment is None:
            return target_id
        product = db.get(Product, assignment.product_id)
        return product.name if product else assignment.product_id
    if kind == ScenarioOverrideKind.APPROVAL_FLIP:
        approval = db.get(SubstitutionApproval, target_id)
        if approval is None:
            return target_id
        well = db.get(Well, approval.well_id)
        return well.name if well else target_id
    return target_id


# --- schemas ------------------------------------------------------------


class ScenarioCreateIn(BaseModel):
    name: str


class ScenarioOut(BaseModel):
    id: str
    name: str
    created_at: datetime.datetime
    status: str
    applied_at: datetime.datetime | None


def _scenario_out(row: Scenario) -> ScenarioOut:
    return ScenarioOut(
        id=row.id,
        name=row.name,
        created_at=row.created_at,
        status=row.status.value,
        applied_at=row.applied_at,
    )


class OverrideOut(BaseModel):
    id: str
    kind: str
    target_id: str
    target_name: str
    payload: dict
    created_at: datetime.datetime


def _override_out(db: Session, row: ScenarioOverride) -> OverrideOut:
    return OverrideOut(
        id=row.id,
        kind=row.kind.value,
        target_id=row.target_id,
        target_name=_target_name(db, row.kind, row.target_id),
        payload=json.loads(row.payload),
        created_at=row.created_at,
    )


class ScenarioDetailOut(ScenarioOut):
    overrides: list[OverrideOut]


# --- scenario CRUD (sans DELETE — spec E-4 / invariant 6) ---------------


@router.get("", response_model=list[ScenarioOut])
def list_scenarios(db: Session = Depends(get_db)):
    rows = db.query(Scenario).order_by(Scenario.created_at.desc()).all()
    return [_scenario_out(r) for r in rows]


@router.post("", response_model=ScenarioOut, status_code=201)
def create_scenario(payload: ScenarioCreateIn, db: Session = Depends(get_db)):
    scenario = Scenario(name=payload.name, created_at=datetime.datetime.now(datetime.timezone.utc))
    db.add(scenario)
    db.commit()
    db.refresh(scenario)
    return _scenario_out(scenario)


@router.get("/{scenario_id}", response_model=ScenarioDetailOut)
def get_scenario(scenario_id: str, db: Session = Depends(get_db)):
    scenario = db.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    overrides = db.query(ScenarioOverride).filter_by(scenario_id=scenario_id).order_by(ScenarioOverride.created_at).all()
    return ScenarioDetailOut(
        **_scenario_out(scenario).model_dump(),
        overrides=[_override_out(db, o) for o in overrides],
    )


# --- overrides ------------------------------------------------------------


class OverrideCreateIn(BaseModel):
    kind: str
    target_id: str
    payload: dict = {}


@router.post("/{scenario_id}/overrides", response_model=OverrideOut, status_code=201)
def add_override(scenario_id: str, payload: OverrideCreateIn, db: Session = Depends(get_db)):
    scenario = db.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    if scenario.status != ScenarioStatus.DRAFT:
        raise HTTPException(status_code=409, detail="Scenario is not Draft")

    try:
        kind = ScenarioOverrideKind(payload.kind)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid kind: {payload.kind!r}")

    if not _target_exists(db, kind, payload.target_id):
        raise HTTPException(status_code=422, detail="target not found for this kind")

    _validate_payload(kind, payload.payload)

    override = ScenarioOverride(
        scenario_id=scenario_id,
        kind=kind,
        target_id=payload.target_id,
        payload=json.dumps(payload.payload),
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(override)
    db.commit()
    db.refresh(override)
    return _override_out(db, override)


@router.delete("/{scenario_id}/overrides/{override_id}", status_code=204)
def delete_override(scenario_id: str, override_id: str, db: Session = Depends(get_db)):
    scenario = db.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    override = db.get(ScenarioOverride, override_id)
    if override is None or override.scenario_id != scenario_id:
        raise HTTPException(status_code=404, detail="Override not found")
    if scenario.status != ScenarioStatus.DRAFT:
        raise HTTPException(status_code=409, detail="Scenario is not Draft")

    db.delete(override)
    db.commit()
    return None


# --- preview (spec E-3) ----------------------------------------------------


@router.get("/{scenario_id}/preview")
def preview(scenario_id: str, sections: str | None = None, db: Session = Depends(get_db)):
    scenario = db.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")

    section_set = {s.strip() for s in sections.split(",") if s.strip()} if sections else {"coverage", "mrp"}
    overrides = db.query(ScenarioOverride).filter_by(scenario_id=scenario_id).all()
    return preview_scenario(db, overrides, section_set)


# --- apply (spec E-4) -------------------------------------------------------


class ApplyOut(BaseModel):
    applied_overrides: list[str]
    rejected: list[dict]


@router.post("/{scenario_id}/apply", response_model=ApplyOut)
def apply(scenario_id: str, db: Session = Depends(get_db)):
    scenario = db.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    if scenario.status != ScenarioStatus.DRAFT:
        raise HTTPException(status_code=409, detail="Scenario already applied")

    overrides = db.query(ScenarioOverride).filter_by(scenario_id=scenario_id).all()

    try:
        applied = apply_scenario(db, scenario, overrides)
    except SupplyOverrideRejected as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail={"rejected": exc.rejected})

    db.commit()
    return ApplyOut(applied_overrides=[o.id for o in applied], rejected=[])
