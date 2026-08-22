from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.engines.coverage import apply_revision
from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
)
from app.engines.coverage_view import planning_node_path
from app.models import (
    CoverageResult,
    CoverageStatus,
    DemandLine,
    DemandProfile,
    DemandStatus,
    PlanningNode,
    Product,
    Well,
)
from app.schemas import (
    DemandLineListOut,
    DemandLineRowOut,
    DemandRevisionIn,
    ImpactRecordOut,
)

router = APIRouter(prefix="/demand-lines", tags=["demand"])


def _sorted_enum(values) -> list:
    return sorted(values, key=lambda v: v.value)


@router.get("", response_model=DemandLineListOut)
def list_demand_lines(
    db: Session = Depends(get_db),
    customer_id: str | None = None,
    well_id: str | None = None,
    product_id: str | None = None,
    status: list[DemandStatus] | None = Query(default=None),
    profile: list[DemandProfile] | None = Query(default=None),
    coverage_status: list[CoverageStatus] | None = Query(default=None),
    ros_from: datetime | None = None,
    ros_to: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """Flat, filterable, paginated demand across every well.

    Coverage status is WITHHELD for out-of-scope lines
    --------------------------------------------------
    A line outside the coverage engine's status/profile filters -- i.e. any line of
    a well that is not Confirmed, and any line of a profile out of scope -- is
    still listed, because planners need
    to see their Planned and Budgeted demand -- but is served `coverage_status = null` and
    `coverage_reason = null`. The engine does not evaluate such a line, so there
    is no verdict to report, and reporting a superseded one is worse than
    reporting none. This is the same rule `app.api.wells._well_out` applies, for
    the same reason: stale `CoverageResult` rows surfacing as if current was a
    reported critical defect. The engine deletes those rows now; withholding here
    is the second line of defence, and it also covers rows written by an older
    build of the engine.

    Consequently a `coverage_status=` filter implicitly restricts the result to
    in-scope lines -- an out-of-scope line has no status to match, so it can never
    satisfy such a filter.

    One SQL statement per response
    ------------------------------
    `well` (plus its planning node), `product` and `coverage_result` are
    eager-loaded, so serialising N rows costs no additional queries. Per-row lazy
    loading here would be several times N queries on the densest list screen in
    the platform.
    """
    if ros_from is not None and ros_to is not None and ros_from > ros_to:
        raise HTTPException(
            status_code=400, detail="ros_from must not be later than ros_to"
        )

    # The platform's CURRENT coverage scope, resolved ONCE for this response and
    # used by both the SQL filter and the per-row withholding below, so the query
    # and the serialisation cannot disagree about which lines were evaluated.
    #
    # Not the shipped constants: an administrator may have widened the scope, in
    # which case the engine has written verdicts for lines the constants exclude,
    # and a guard pinned to the constants would withhold every one of them -- the
    # list screen would show blanks for lines the coverage grid shows verdicts for.
    # See `app.engines.coverage_scope`.
    #
    # Deliberately NOT confused with the `status=` / `profile=` REQUEST parameters
    # above. Those choose which lines to LIST; this pair decides which listed lines
    # have a verdict to show at all.
    scope_status = effective_status_filter(db)
    scope_profile = effective_profile_filter(db)

    def base():
        query = (
            db.query(DemandLine)
            .join(Well, DemandLine.well_id == Well.id)
            .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .join(Product, DemandLine.product_id == Product.id)
        )
        if customer_id is not None:
            query = query.filter(PlanningNode.customer_id == customer_id)
        if well_id is not None:
            query = query.filter(DemandLine.well_id == well_id)
        if product_id is not None:
            query = query.filter(DemandLine.product_id == product_id)
        if status:
            # Demand status lives on the WELL, so this filter selects whole wells
            # -- a `status=Confirmed` request can never return some lines of a
            # well and not others. `Well` is already joined above.
            query = query.filter(Well.demand_status.in_(status))
        if profile:
            query = query.filter(DemandLine.profile.in_(profile))
        if ros_from is not None:
            query = query.filter(DemandLine.ros_date >= ros_from)
        if ros_to is not None:
            query = query.filter(DemandLine.ros_date <= ros_to)
        if coverage_status:
            # Restricting to in-scope lines is not an optimisation, it is the
            # correctness rule above expressed in SQL: an out-of-scope line is
            # served no status, so it must not be matched by a status filter even
            # if a CoverageResult row for it happens to survive.
            query = (
                query.join(
                    CoverageResult, CoverageResult.demand_line_id == DemandLine.id
                )
                .filter(CoverageResult.status.in_(coverage_status))
                .filter(
                    Well.demand_status.in_(_sorted_enum(scope_status)),
                    DemandLine.profile.in_(_sorted_enum(scope_profile)),
                )
            )
        return query

    total = base().count()
    lines = (
        base()
        .options(
            joinedload(DemandLine.well)
            .joinedload(Well.planning_node)
            .joinedload(PlanningNode.customer),
            joinedload(DemandLine.product),
            joinedload(DemandLine.coverage_result),
        )
        .order_by(DemandLine.ros_date, DemandLine.id)
        .offset(offset)
        .limit(limit)
        .all()
    )

    rows: list[DemandLineRowOut] = []
    for line in lines:
        well = line.well
        evaluated = (
            well.demand_status in scope_status
            and line.profile in scope_profile
        )
        cr = line.coverage_result if evaluated else None
        node = well.planning_node
        rows.append(
            DemandLineRowOut(
                id=line.id,
                well_id=line.well_id,
                well_name=well.name,
                planning_node_id=node.id if node else None,
                planning_node_path=planning_node_path(node),
                customer_id=node.customer_id if node else None,
                customer_name=(
                    node.customer.name if node and node.customer else None
                ),
                product_id=line.product_id,
                product_description=line.product.description or line.product_id,
                quantity=line.quantity,
                unit_of_measure=line.product.unit_of_measure,
                ros_date=line.ros_date,
                # The WELL's status, the same on every line of that well.
                status=well.demand_status,
                profile=line.profile,
                current_revision_no=line.current_revision_no,
                evaluated=evaluated,
                coverage_status=cr.status.value if cr else None,
                coverage_reason=cr.reason if cr else None,
            )
        )

    return DemandLineListOut(
        total=total, limit=limit, offset=offset, returned=len(rows), rows=rows
    )


@router.post("/{demand_line_id}/revisions", response_model=ImpactRecordOut)
def create_revision(demand_line_id: str, body: DemandRevisionIn, db: Session = Depends(get_db)):
    """Revise ONE line's quantity, ROS and profile.

    A `status` IN THE BODY IS REFUSED WITH 400
    -----------------------------------------
    Demand status is a property of the well
    (`app.models.well.Well.demand_status`): confirming a well confirms every line
    of it. So there is no honest thing this route could do with a status.

      * Applying it to the whole well would be a well-level write behind a
        line-level URL -- the caller asked to revise one line and every sibling's
        history would change.
      * Applying it to this line alone is the state that no longer exists.
      * Ignoring it silently is the worst of the three: the request would return
        200 and the planner would believe the well was confirmed.

    So the field is still ACCEPTED BY THE SCHEMA and rejected here, with the
    well-level endpoint named in the message. Dropping it from the schema entirely
    would have made an old client's status silently vanish (Pydantic ignores
    unknown fields by default), which is the same silent success in a different
    place. See `app.schemas.DemandRevisionIn`.
    """
    line = db.get(DemandLine, demand_line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="Demand line not found")
    if body.status is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Demand status cannot be revised on a demand line: it is a property "
                "of the WELL, so confirming a well confirms every line of it. Use "
                f"PUT /wells/{line.well_id}/demand-status instead, which cascades a "
                "revision to every line of the well and records an impact for each. "
                "Send this request again without 'status' to revise only this line's "
                "quantity, ROS date and profile."
            ),
        )
    impact = apply_revision(db, line, body.quantity, body.ros_date, body.profile)
    db.commit()
    db.refresh(impact)
    return impact
