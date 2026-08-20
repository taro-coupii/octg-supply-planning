# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
"""spec §API row 8: GET /analysis/sharing. Read-only what-if — no writes."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.coverage import _scope
from app.engines.sharing import sharing_analysis
from app.engines.surplus import surplus_rows
from app.models import Product

router = APIRouter(prefix="/analysis")

# spec E-2: non-default-scope reads are recomputed in-memory and are NOT the
# official stored verdicts (surplus itself never persists, but the warning
# still signals "this differs from the Administration default scope view").
SCOPE_OVERRIDE_WARNING = "Recomputed read-only — NOT the official stored verdicts"


class LineOut(BaseModel):
    id: str
    well_id: str
    product_id: str
    unit: str
    quantity: float


class PeerOut(BaseModel):
    customer_id: str
    customer_name: str
    releasable_qty_by_unit: float
    would_cover: bool


class SharingEntryOut(BaseModel):
    line: LineOut
    official_verdict: str
    peers: list[PeerOut]


@router.get("/sharing", response_model=list[SharingEntryOut])
def get_sharing_analysis(customer_id: str, db: Session = Depends(get_db)):
    result = sharing_analysis(db, customer_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return result


class ScopeOut(BaseModel):
    statuses: list[str]
    profiles: list[str]


class SurplusRowOut(BaseModel):
    product: str
    unit: str
    on_hand: float
    allocated: float
    surplus: float
    obsolete: float


class SurplusOut(BaseModel):
    rows: list[SurplusRowOut]
    totals_by_unit: dict[str, SurplusRowOut]
    identity_ok: bool
    scope: ScopeOut
    scope_is_default: bool
    warning: str | None = None


@router.get("/surplus", response_model=SurplusOut)
def get_surplus(
    status: list[str] | None = Query(None),
    profile: list[str] | None = Query(None),
    db: Session = Depends(get_db),
):
    # spec E-2: default scope = params absent entirely; an explicitly-passed
    # (even empty) list is a non-default override.
    scope_is_default = status is None and profile is None
    default_statuses, default_profiles = _scope(db)
    statuses = set(status) if status is not None else default_statuses
    profiles = set(profile) if profile is not None else default_profiles

    engine_rows = surplus_rows(db, statuses=statuses, profiles=profiles)

    rows_out: list[SurplusRowOut] = []
    identity_ok = True
    for r in engine_rows:
        if abs(r.on_hand - (r.allocated + r.surplus + r.obsolete)) > 1e-6:
            identity_ok = False
        p = db.get(Product, r.product_id)
        rows_out.append(
            SurplusRowOut(
                product=p.name if p is not None else r.product_id,
                unit=r.unit.value,
                on_hand=r.on_hand,
                allocated=r.allocated,
                surplus=r.surplus,
                obsolete=r.obsolete,
            )
        )

    totals_by_unit: dict[str, SurplusRowOut] = {}
    for r in engine_rows:
        unit = r.unit.value
        if unit not in totals_by_unit:
            totals_by_unit[unit] = SurplusRowOut(
                product="__total__", unit=unit, on_hand=0.0, allocated=0.0, surplus=0.0, obsolete=0.0
            )
        agg = totals_by_unit[unit]
        agg.on_hand += r.on_hand
        agg.allocated += r.allocated
        agg.surplus += r.surplus
        agg.obsolete += r.obsolete
        if abs(agg.on_hand - (agg.allocated + agg.surplus + agg.obsolete)) > 1e-6:
            identity_ok = False

    return SurplusOut(
        rows=rows_out,
        totals_by_unit=totals_by_unit,
        identity_ok=identity_ok,
        scope=ScopeOut(statuses=sorted(statuses), profiles=sorted(profiles)),
        scope_is_default=scope_is_default,
        warning=None if scope_is_default else SCOPE_OVERRIDE_WARNING,
    )
