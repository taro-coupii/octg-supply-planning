# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
"""spec §API row 6: GET /substitution/candidates. Reuses allocation.free_pool /
hard_assigned_total (sole implementations) — never re-derives pool math here.

裁定S-1 blocked_by priority: customer > well-approval > oracle-release > null.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.allocation import allocate_lines, free_pool, hard_assigned_total
from app.engines.coverage import _scope, scoped_lines
from app.models import (
    Customer,
    CustomerSubstitutionRule,
    DemandLine,
    Product,
    SubstitutionApproval,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    Well,
)

router = APIRouter(prefix="/substitution")


class CandidateOut(BaseModel):
    substitution_id: str
    to_product: str
    technical_ok: bool
    customer_rule_allowed: bool
    free_qty_by_unit: float
    hard_assigned_qty: float
    verdict_if_applied: bool
    blocked_by: str | None


@router.get("/candidates", response_model=list[CandidateOut])
def get_candidates(demand_line_id: str, db: Session = Depends(get_db)):
    line = db.get(DemandLine, demand_line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="Demand line not found")

    well = db.get(Well, line.well_id)
    customer = db.get(Customer, well.customer_id)
    if customer.business_unit_id is None:
        raise HTTPException(status_code=422, detail=f"Customer {customer.id} has no business_unit_id")
    bu_id = customer.business_unit_id

    # Current shortfall for this line, computed against the customer's FULL
    # scoped line set (matching what recompute uses) — never in isolation, since
    # an earlier line in the same scope may already have depleted the pool this
    # line would otherwise draw from.
    statuses, profiles = _scope(db)
    scope_lines = scoped_lines(db, customer.id, statuses, profiles)
    if line not in scope_lines:
        scope_lines = scope_lines + [line]
    allocations = allocate_lines(db, customer, scope_lines)
    shortfall = next((a.shortfall for a in allocations if a.line.id == line.id), 0.0)

    subs = db.query(TechnicalSubstitution).filter_by(from_product_id=line.product_id).all()

    out: list[CandidateOut] = []
    for ts in subs:
        to_product = db.get(Product, ts.to_product_id)
        rule = (
            db.query(CustomerSubstitutionRule)
            .filter_by(customer_id=customer.id, technical_substitution_id=ts.id)
            .first()
        )
        customer_rule_allowed = bool(rule is not None and rule.allowed)

        free_qty = free_pool(db, bu_id, ts.to_product_id, line.unit)
        hard_qty = hard_assigned_total(db, bu_id, ts.to_product_id, line.unit)

        verdict_if_applied = shortfall <= 0 or free_qty >= shortfall

        blocked_by: str | None = None
        if not customer_rule_allowed:
            blocked_by = "customer"
        else:
            # Well-approval blocks if there's an existing PENDING request for
            # this (line, substitution) — the request exists but is undecided,
            # regardless of whether stock would already cover the shortfall.
            pending = (
                db.query(SubstitutionApproval)
                .filter_by(
                    demand_line_id=line.id,
                    technical_substitution_id=ts.id,
                    status=SubstitutionApprovalStatus.PENDING,
                )
                .first()
            )
            if pending is not None:
                blocked_by = "well-approval"
            elif not verdict_if_applied and hard_qty > 0 and (free_qty + hard_qty) >= shortfall:
                blocked_by = "oracle-release"

        out.append(
            CandidateOut(
                substitution_id=ts.id,
                to_product=to_product.name,
                technical_ok=True,
                customer_rule_allowed=customer_rule_allowed,
                free_qty_by_unit=free_qty,
                hard_assigned_qty=hard_qty,
                verdict_if_applied=verdict_if_applied,
                blocked_by=blocked_by,
            )
        )

    return out
