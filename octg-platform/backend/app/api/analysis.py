"""Analysis endpoints -- read-only projections.

Nothing under this router mutates state. The surplus report reads stored
verdicts (or, for a non-default scope, a recompute that is rolled back -- see
`app.engines.coverage_view.scoped_verdicts`), so there is nothing to commit and
no path by which a GET could overwrite the official coverage answer.

The cross-customer sharing what-if used to live here. It answered "could this
customer's uncovered demand be covered from a neighbour's surplus?", which was a
question worth asking only while coverage was allocated per customer: since the
product owner's ruling of 2026-09-06 (D01) the Business Unit's pool is divided
across its customers together, so any surplus has already gone to whoever needed
it soonest and the answer is always "no, and here is the verdict that says so".
Retired rather than left as a screen that could only ever agree with itself.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
)
from app.engines.coverage_view import scoped_verdicts
from app.engines.surplus import surplus_report
from app.auth.scope import planner_bu
from app.models import BusinessUnit, Customer, DemandProfile, DemandStatus
from app.schemas import SurplusReportOut

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.get("/surplus", response_model=SurplusReportOut)
def get_surplus(
    business_unit_id: str | None = None,
    status: list[DemandStatus] | None = Query(default=None),
    profile: list[DemandProfile] | None = Query(default=None),
    db: Session = Depends(get_db),
    bu_scope: str | None = Depends(planner_bu),
):
    """The Surplus List: on-hand decomposed into allocated / surplus /
    obsolete per (BU, product). Read-only; quantity-based -- see
    app.engines.surplus for the definitions and their limits.

    `status` / `profile` vary the demand scope the allocation counts against
    (default: the platform's coverage scope). A non-default scope runs inside
    a read-only recompute (scoped_verdicts) and is labelled as such in the
    payload -- widening to Planned/Budgeted shows how much of today's surplus
    the future programme would absorb."""
    if business_unit_id is None and bu_scope is not None:
        business_unit_id = bu_scope
    # An unknown BU id is a 404, not an empty 200: an empty surplus report
    # reads as "no idle steel", which is a claim, not an absence of one.
    if business_unit_id is not None and db.get(BusinessUnit, business_unit_id) is None:
        raise HTTPException(status_code=404, detail="Business Unit not found")
    status_set = set(status) if status else None
    profile_set = set(profile) if profile else None
    with scoped_verdicts(db, status_set, profile_set) as (
        scope_is_default,
        skipped_customers,
    ):
        report = surplus_report(db, business_unit_id=business_unit_id)
        applied_status = sorted(
            s.value for s in (status_set or effective_status_filter(db))
        )
        applied_profile = sorted(
            p.value for p in (profile_set or effective_profile_filter(db))
        )
    out = SurplusReportOut.model_validate(report, from_attributes=True)
    out.status_scope = applied_status
    out.profile_scope = applied_profile
    out.scope_is_default = scope_is_default
    out.skipped_customers = list(skipped_customers)
    return out

