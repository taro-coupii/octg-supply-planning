from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.db import get_db
from app.engines.coverage import compute_customer_coverage, recompute_well
from app.engines.substitution import (
    ApprovalAlreadyDecided,
    approval_by_date,
    decide_approval,
    find_candidates,
    request_approval,
)
from app.auth.scope import planner_bu
from app.models import (
    DemandLine,
    PlanningNode,
    User,
    Product,
    SubstitutionApprovalStatus,
    Well,
    WellSubstitutionApproval,
    Customer,
)
from app.schemas import (
    ApprovalQueueRowOut,
    SubstitutionApprovalIn,
    SubstitutionApprovalOut,
    SubstitutionCandidateOut,
    SubstitutionDecisionIn,
)

router = APIRouter(tags=["substitution"])


@router.get(
    "/substitution-approvals", response_model=list[ApprovalQueueRowOut]
)
def list_substitution_approvals(
    status: SubstitutionApprovalStatus | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    bu_scope: str | None = Depends(planner_bu),
):
    """The approval QUEUE: every well-substitution approval, newest request
    first, optionally filtered by status.

    Exists because approvals were only reachable per demand line (and the
    Home card caps at a handful) -- a planner with twenty pending requests
    had no screen listing them. Decisions still go through the one existing
    endpoint (`POST /substitution-approvals/{id}/decision`)."""
    query = db.query(WellSubstitutionApproval)
    if bu_scope is not None:
        query = (
            query.join(DemandLine, WellSubstitutionApproval.demand_line_id == DemandLine.id)
            .join(Well, Well.id == DemandLine.well_id)
            .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .join(Customer, PlanningNode.customer_id == Customer.id)
            .filter(Customer.business_unit_id == bu_scope)
        )
    if status is not None:
        query = query.filter(WellSubstitutionApproval.status == status)
    approvals = (
        query.order_by(WellSubstitutionApproval.requested_at.desc())
        .limit(limit)
        .all()
    )

    # Bulk lookups, not one round trip per approval: the queue is a LIST
    # screen and its cost must not scale 6 queries per row.
    line_ids = {a.demand_line_id for a in approvals}
    lines = {
        line.id: line
        for line in db.query(DemandLine)
        .options(
            joinedload(DemandLine.well)
            .joinedload(Well.planning_node)
            .joinedload(PlanningNode.customer),
            joinedload(DemandLine.product),
        )
        .filter(DemandLine.id.in_(sorted(line_ids)))
        .all()
    }
    product_ids = {a.from_product_id for a in approvals} | {
        a.to_product_id for a in approvals
    }
    products = {
        p.id: p
        for p in db.query(Product).filter(Product.id.in_(sorted(product_ids))).all()
    }

    out: list[ApprovalQueueRowOut] = []
    for a in approvals:
        line = lines.get(a.demand_line_id)
        well = line.well if line is not None else None
        node = well.planning_node if well is not None else None
        from_product = products.get(a.from_product_id)
        to_product = products.get(a.to_product_id)
        out.append(ApprovalQueueRowOut(
            approval_id=a.id,
            demand_line_id=a.demand_line_id,
            status=a.status.value,
            requested_at=a.requested_at,
            decided_at=a.decided_at,
            decided_by_user_name=a.decided_by_user_name,
            well_id=well.id if well is not None else None,
            well_name=well.name if well is not None else None,
            customer_name=(
                node.customer.name
                if node is not None and node.customer is not None
                else None
            ),
            from_product_description=(
                (from_product.description or from_product.id)
                if from_product is not None
                else a.from_product_id
            ),
            to_product_description=(
                (to_product.description or to_product.id)
                if to_product is not None
                else a.to_product_id
            ),
            quantity=line.quantity if line is not None else None,
            unit_of_measure=(
                line.product.unit_of_measure
                if line is not None and line.product is not None
                else None
            ),
            ros_date=line.ros_date if line is not None else None,
        ))
    return out


def _get_line(db: Session, demand_line_id: str) -> DemandLine:
    line = db.get(DemandLine, demand_line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="Demand line not found")
    return line


@router.get(
    "/demand-lines/{demand_line_id}/substitution-candidates",
    response_model=list[SubstitutionCandidateOut],
)
def list_substitution_candidates(demand_line_id: str, db: Session = Depends(get_db)):
    line = _get_line(db, demand_line_id)

    # Two of the annotations a planner needs are POOL-WIDE facts a single line
    # cannot derive: which substitute quantity is hard-assigned to another demand
    # line (so the platform may not take it), and how much PendingApproval demand
    # is riding on the same substitute (so approving all of it cannot succeed).
    # Both come from the one coverage implementation rather than a second
    # derivation here -- `compute_customer_coverage` computes and writes nothing.
    computed = compute_customer_coverage(db, line.well.planning_node.customer)
    pending_load = {
        load.to_product_id: load for load in computed.pending_substitute_load
    }

    # A property of the LINE, not of any one candidate -- computed once and
    # stamped on every row. See `SubstitutionCandidateOut.approval_by_date`.
    abd = approval_by_date(db, line)

    candidates = [
        SubstitutionCandidateOut.model_validate(c, from_attributes=True)
        for c in find_candidates(
            db,
            line,
            hard_assigned_by_product=computed.hard_assigned_by_product,
            pending_load_by_product=pending_load,
        )
    ]
    from_description = line.product.description if line.product else None
    for out in candidates:
        out.from_product_description = from_description
        out.approval_by_date_available = abd.available
        out.approval_by_date = abd.approval_by_date
        out.still_recoverable = abd.still_recoverable
        out.approval_by_date_reason = abd.reason
    return candidates


@router.post(
    "/demand-lines/{demand_line_id}/substitution-approvals",
    response_model=SubstitutionApprovalOut,
)
def create_substitution_approval(
    demand_line_id: str,
    body: SubstitutionApprovalIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    line = _get_line(db, demand_line_id)
    # Validated here, not trusted: an unknown product id would otherwise
    # 500 on a real FK database (and quietly write a dangling row on SQLite).
    for label, pid in (("from_product_id", body.from_product_id),
                       ("to_product_id", body.to_product_id)):
        if db.get(Product, pid) is None:
            raise HTTPException(
                status_code=404, detail=f"{label} {pid} names no product"
            )
    approval = request_approval(
        db, line, body.from_product_id, body.to_product_id, actor_user_id=user.id
    )
    db.commit()
    db.refresh(approval)
    return approval


@router.post(
    "/substitution-approvals/{approval_id}/decision",
    response_model=SubstitutionApprovalOut,
)
def decide_substitution_approval(
    approval_id: str,
    body: SubstitutionDecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existing = db.get(WellSubstitutionApproval, approval_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Substitution approval not found")
    # A decided approval is FINAL (product-owner decision, 2026-08-12). The rule
    # is enforced by `decide_approval` itself (F06), so this endpoint only
    # translates the refusal; it is not the last line of defence.
    try:
        approval = decide_approval(
            db, approval_id, body.approved, actor_user_id=user.id
        )
    except ApprovalAlreadyDecided as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # The decision changes the well-layer verdict, so coverage for the affected
    # well must be recomputed before the response is returned.
    recompute_well(db, approval.demand_line.well)
    db.commit()
    db.refresh(approval)
    return approval
