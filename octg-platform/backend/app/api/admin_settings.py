# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
# COMPROMISE[C-15R]: require_admin exists (app/auth/deps.py) but is deliberately
# not applied here — PLANNER can reach these routes (see docs/superpowers/COMPROMISES.md).
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Setting

router = APIRouter(prefix="/admin")

STATUS_ENUM = {"Planned", "Budgeted", "Confirmed"}
PROFILE_ENUM = {"Primary", "Contingency"}

STATUSES_KEY = "coverage_scope_statuses"
PROFILES_KEY = "coverage_scope_profiles"

DEFAULT_STATUSES = ["Confirmed"]
DEFAULT_PROFILES = ["Primary", "Contingency"]


class CoverageScope(BaseModel):
    statuses: list[str]
    profiles: list[str]


def _get_setting(db: Session, key: str) -> str | None:
    row = db.query(Setting).filter_by(key=key).first()
    return row.value if row else None


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(Setting).filter_by(key=key).first()
    if row is None:
        row = Setting(key=key, value=value)
        db.add(row)
    else:
        row.value = value


@router.get("/coverage-scope", response_model=CoverageScope)
def get_coverage_scope(db: Session = Depends(get_db)):
    statuses_raw = _get_setting(db, STATUSES_KEY)
    profiles_raw = _get_setting(db, PROFILES_KEY)
    statuses = statuses_raw.split(",") if statuses_raw else DEFAULT_STATUSES
    profiles = profiles_raw.split(",") if profiles_raw else DEFAULT_PROFILES
    return CoverageScope(statuses=statuses, profiles=profiles)


@router.put("/coverage-scope", response_model=CoverageScope)
def put_coverage_scope(payload: CoverageScope, db: Session = Depends(get_db)):
    if not payload.statuses or not set(payload.statuses) <= STATUS_ENUM:
        raise HTTPException(status_code=422, detail="Invalid statuses")
    if not payload.profiles or not set(payload.profiles) <= PROFILE_ENUM:
        raise HTTPException(status_code=422, detail="Invalid profiles")

    _set_setting(db, STATUSES_KEY, ",".join(payload.statuses))
    _set_setting(db, PROFILES_KEY, ",".join(payload.profiles))
    db.commit()
    return payload
