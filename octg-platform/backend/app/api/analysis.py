"""Analysis endpoints -- read-only what-if projections.

Nothing under this router mutates state. In particular the cross-customer
sharing route deliberately does NOT call `db.commit()`: the analysis writes
nothing (see app.engines.sharing for the four mechanisms enforcing that), so
there is nothing to commit and no path by which a GET could overwrite the
official coverage answer.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
)
from app.engines.coverage_view import scoped_verdicts
from app.engines.sharing import cross_customer_sharing
from app.engines.surplus import surplus_report
from app.models import BusinessUnit, Customer, DemandProfile, DemandStatus
from app.schemas import CrossCustomerSharingOut, SurplusReportOut

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.get("/cross-customer-sharing", response_model=CrossCustomerSharingOut)
def get_cross_customer_sharing(customer_id: str, db: Session = Depends(get_db)):
    """Which of this customer's UNCOVERED demand could BU-level sharing cover?

    A read-only what-if. The official coverage verdict remains customer-scoped
    and is untouched by this call. Sharing is evaluated within the customer's
    Business Unit only -- stock in any other BU is never offered, whatever its
    quantity -- and only genuine surplus (what is left after every customer in
    the BU has taken its own committed quantity) is on the table.

    A customer with no Business Unit mapped is treated as isolated: the response
    is empty and `notes` explains why.
    """
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    analysis = cross_customer_sharing(db, customer)
    return CrossCustomerSharingOut.model_validate(analysis, from_attributes=True)

@router.get("/surplus", response_model=SurplusReportOut)
def get_surplus(
    business_unit_id: str | None = None,
    status: list[DemandStatus] | None = Query(default=None),
    profile: list[DemandProfile] | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """The Surplus List: on-hand decomposed into allocated / surplus /
    obsolete per (BU, product). Read-only; quantity-based -- see
    app.engines.surplus for the definitions and their limits.

    `status` / `profile` vary the demand scope the allocation counts against
    (default: the platform's coverage scope). A non-default scope runs inside
    a read-only recompute (scoped_verdicts) and is labelled as such in the
    payload -- widening to Planned/Budgeted shows how much of today's surplus
    the future programme would absorb."""
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

