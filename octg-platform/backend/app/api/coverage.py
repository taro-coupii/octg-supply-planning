# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.allocation import InventoryScopeMissing
from app.engines.coverage import last_recompute_skips, recompute_all, recompute_customer
from app.models import Customer, CoverageResult, CoverageVerdict, DemandLine, Well

router = APIRouter(prefix="/coverage")

# spec 裁定C-1: NotEvaluated is a real state — never folded into 0/Covered.
NOT_EVALUATED = "NotEvaluated"

_SEVERITY = {
    CoverageVerdict.COVERED.value: 0,
    CoverageVerdict.COVERED_VIA_SUBSTITUTE.value: 1,
    CoverageVerdict.PENDING_APPROVAL.value: 2,
    CoverageVerdict.UNCOVERED.value: 3,
    CoverageVerdict.UNRECOVERABLE.value: 4,
    NOT_EVALUATED: 5,
}


class RecomputeIn(BaseModel):
    customer_id: str | None = None


class SkippedCustomerOut(BaseModel):
    name: str
    reason: str


class RecomputeOut(BaseModel):
    computed: int
    skipped_customers: list[SkippedCustomerOut]


@router.post("/recompute", response_model=RecomputeOut)
def post_recompute(body: RecomputeIn, db: Session = Depends(get_db)):
    if body.customer_id:
        customer = db.get(Customer, body.customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        try:
            results = recompute_customer(db, body.customer_id)
        except InventoryScopeMissing as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return RecomputeOut(computed=len(results), skipped_customers=[])

    outcome = recompute_all(db)
    return RecomputeOut(
        computed=outcome["computed"],
        skipped_customers=[
            SkippedCustomerOut(name=name, reason=reason) for name, reason in outcome["skipped_customers"]
        ],
    )


class WellRollupOut(BaseModel):
    id: str
    name: str
    status: str
    line_count: int
    verdict_rollup: dict[str, int]
    worst_verdict: str | None


class CustomerGridOut(BaseModel):
    id: str
    name: str
    policy: str
    wells: list[WellRollupOut]


class CoverageGridOut(BaseModel):
    customers: list[CustomerGridOut]
    skipped_customers: list[SkippedCustomerOut]
    computed_at_min: datetime.datetime | None
    computed_at_max: datetime.datetime | None


@router.get("", response_model=CoverageGridOut)
def get_coverage_grid(db: Session = Depends(get_db)):
    """Reads stored CoverageResult rows only — never recomputes here."""
    all_results = db.query(CoverageResult).all()
    verdict_by_line_id = {r.demand_line_id: r.verdict.value for r in all_results}

    computed_ats = [r.computed_at for r in all_results]
    computed_at_min = min(computed_ats) if computed_ats else None
    computed_at_max = max(computed_ats) if computed_ats else None

    customers_out: list[CustomerGridOut] = []
    for customer in db.query(Customer).order_by(Customer.name).all():
        wells_out: list[WellRollupOut] = []
        for well in db.query(Well).filter_by(customer_id=customer.id).order_by(Well.name).all():
            lines = db.query(DemandLine).filter_by(well_id=well.id).all()
            rollup: dict[str, int] = {}
            for line in lines:
                verdict = verdict_by_line_id.get(line.id, NOT_EVALUATED)
                rollup[verdict] = rollup.get(verdict, 0) + 1
            worst_verdict = max(rollup, key=lambda v: _SEVERITY[v]) if rollup else None
            wells_out.append(
                WellRollupOut(
                    id=well.id,
                    name=well.name,
                    status=well.demand_status.value,
                    line_count=len(lines),
                    verdict_rollup=rollup,
                    worst_verdict=worst_verdict,
                )
            )
        customers_out.append(
            CustomerGridOut(id=customer.id, name=customer.name, policy=customer.allocation_policy.value, wells=wells_out)
        )

    return CoverageGridOut(
        customers=customers_out,
        skipped_customers=[SkippedCustomerOut(**row) for row in last_recompute_skips(db)],
        computed_at_min=computed_at_min,
        computed_at_max=computed_at_max,
    )
