from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
)
from app.engines.coverage_view import scoped_verdicts
from app.engines.executive import (
    ALLOCATION_HORIZONS,
    ScopeContradiction,
    executive_summary,
)
from app.engines.well_dates import sort_by_earliest_ros, well_dates
from app.auth.scope import planner_bu
from app.models import (
    CoverageResult,
    CoverageStatus,
    DemandLine,
    DemandProfile,
    DemandStatus,
    ImpactRecord,
    PlanningNode,
    Well,
    Product,
    Customer,
)
from app.schemas import (
    ExecutiveSummaryOut,
    HomeDashboardOut,
    ImpactRecordOut,
    PendingApprovalCardOut,
    WellSummary,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _impact_out(db: Session, record: ImpactRecord) -> ImpactRecordOut:
    """One "Demand Changes" row, with the product named and the quantities labelled.

    The product is reached through the demand line. It can be absent: an
    ImpactRecord outlives a deleted demand line (the FK has no cascade), so a
    historical row may have nothing to read -- reported as null rather than as a
    guessed unit.
    """
    line = db.get(DemandLine, record.demand_line_id)
    product = line.product if line is not None else None
    well = db.get(Well, record.well_id)
    node = well.planning_node if well is not None else None
    customer = node.customer if node is not None else None
    return ImpactRecordOut(
        id=record.id,
        demand_line_id=record.demand_line_id,
        well_id=record.well_id,
        well_name=well.name if well is not None else None,
        customer_name=customer.name if customer is not None else None,
        product_description=(
            (product.description or product.id) if product is not None else None
        ),
        unit_of_measure=product.unit_of_measure if product is not None else None,
        quantity_before=record.quantity_before,
        quantity_after=record.quantity_after,
        ros_date_before=record.ros_date_before,
        ros_date_after=record.ros_date_after,
        status_before=record.status_before,
        status_after=record.status_after,
        coverage_before=record.coverage_before,
        coverage_after=record.coverage_after,
        created_at=record.created_at,
    )


@router.get("/home", response_model=HomeDashboardOut)
def home_dashboard(
    db: Session = Depends(get_db), bu_scope: str | None = Depends(planner_bu)
):
    # A planner's Home is their Business Unit's Home (review 2026-09-06, F01):
    # every card below joins out to the customer and filters when scoped.
    changes_q = db.query(ImpactRecord)
    if bu_scope is not None:
        changes_q = (
            changes_q.join(DemandLine, ImpactRecord.demand_line_id == DemandLine.id)
            .join(Well, Well.id == DemandLine.well_id)
            .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .join(Customer, PlanningNode.customer_id == Customer.id)
            .filter(Customer.business_unit_id == bu_scope)
        )
    recent_changes = changes_q.order_by(ImpactRecord.created_at.desc()).limit(20).all()
    wells_q = (
        db.query(Well)
        .options(
            joinedload(Well.planning_node).joinedload(PlanningNode.customer)
        )
        .filter(Well.coverage_status == CoverageStatus.UNCOVERED.value)
    )
    if bu_scope is not None:
        wells_q = (
            wells_q.join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .join(Customer, PlanningNode.customer_id == Customer.id)
            .filter(Customer.business_unit_id == bu_scope)
        )
    uncovered_wells = wells_q.all()
    # The two dates the uncovered-wells card was unactionable without: when the
    # well first needs steel, and when it first falls short. Batched -- ONE call for
    # every uncovered well, not one per row (see `app.engines.well_dates`), which
    # matters because this card lists every uncovered well in the platform.
    uncovered_dates = well_dates(db, [w.id for w in uncovered_wells])
    # The "Pending Approvals" card is a UNION of two kinds of work item
    # (product-owner decision, 2026-08-14):
    #
    #   1. EVERY open WellSubstitutionApproval request -- whatever its line's
    #      verdict, whatever its well's scope. QA caught the old verdict-driven
    #      card hiding a freshly raised request whose line still read Uncovered,
    #      even though the Approvals queue listed it. A request awaits a
    #      customer decision; the work queue must say so.
    #   2. In-scope lines whose stored verdict is PendingApproval but which
    #      have NO open request yet -- the engine writes that verdict when a
    #      substitute WOULD cover the line if approved, before anyone has
    #      raised the request. The card renders these without buttons and
    #      points at the substitution screen, exactly as before.
    from app.models import SubstitutionApprovalStatus, WellSubstitutionApproval

    pending_requests = (
        db.query(WellSubstitutionApproval, DemandLine, Well)
        .join(DemandLine, WellSubstitutionApproval.demand_line_id == DemandLine.id)
        .join(Well, Well.id == DemandLine.well_id)
        .filter(
            WellSubstitutionApproval.status == SubstitutionApprovalStatus.PENDING
        )
    )
    if bu_scope is not None:
        pending_requests = (
            pending_requests.join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .join(Customer, PlanningNode.customer_id == Customer.id)
            .filter(Customer.business_unit_id == bu_scope)
        )
    pending_requests = (
        pending_requests.order_by(WellSubstitutionApproval.requested_at.desc()).all()
    )
    requested_line_ids = {line.id for _a, line, _w in pending_requests}
    # Verdict-driven lines keep the coverage-scope guard they always had: a
    # verdict outside the scope is one the engine no longer stands behind.
    verdict_only_lines = (
        db.query(DemandLine, Well)
        .join(CoverageResult, CoverageResult.demand_line_id == DemandLine.id)
        .join(Well, Well.id == DemandLine.well_id)
        .filter(
            CoverageResult.status == CoverageStatus.PENDING_APPROVAL,
            Well.demand_status.in_(
                sorted(effective_status_filter(db), key=lambda s: s.value)
            ),
            DemandLine.profile.in_(
                sorted(effective_profile_filter(db), key=lambda p: p.value)
            ),
        )
    )
    if bu_scope is not None:
        verdict_only_lines = (
            verdict_only_lines.join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .join(Customer, PlanningNode.customer_id == Customer.id)
            .filter(Customer.business_unit_id == bu_scope)
        )
    verdict_only_lines = verdict_only_lines.all()
    verdict_only_lines = [
        (line, well)
        for line, well in verdict_only_lines
        if line.id not in requested_line_ids
    ]

    return HomeDashboardOut(
        demand_changes=[_impact_out(db, r) for r in recent_changes],
        # SORTED BY EARLIEST ROS ASCENDING, server-side, exactly as the product
        # owner asked -- and by the same helper GET /wells and the coverage grid use,
        # so all three screens present wells in one order.
        uncovered_wells=sort_by_earliest_ros(
            [
                WellSummary(
                    id=w.id,
                    name=w.name,
                    customer_id=(
                        w.planning_node.customer_id
                        if w.planning_node is not None
                        else None
                    ),
                    customer_name=(
                        w.planning_node.customer.name
                        if w.planning_node is not None
                        and w.planning_node.customer is not None
                        else None
                    ),
                    demand_status=w.demand_status,
                    coverage_status=w.coverage_status,
                    earliest_ros_date=(
                        d.earliest_ros_date
                        if (d := uncovered_dates.get(w.id)) is not None
                        else None
                    ),
                    first_runout_date=(
                        d.first_runout_date
                        if (d := uncovered_dates.get(w.id)) is not None
                        else None
                    ),
                )
                for w in uncovered_wells
            ]
        ),
        pending_approvals=[
            PendingApprovalCardOut(
                well_id=well.id,
                well_name=well.name,
                customer_name=(
                    well.planning_node.customer.name
                    if well.planning_node is not None
                    and well.planning_node.customer is not None
                    else None
                ),
                demand_line_id=line.id,
                product_description=line.product.description or line.product_id,
                # The card shows a quantity, so it shows a unit. Same rule as every
                # other quantity-bearing payload.
                quantity=line.quantity,
                unit_of_measure=line.product.unit_of_measure,
                ros_date=line.ros_date.isoformat(),
                approval_id=approval.id,
                substitute_description=(
                    substitute.description if substitute is not None else None
                ),
            )
            for approval, line, well in pending_requests
            for substitute in [db.get(Product, approval.to_product_id)]
        ]
        + [
            PendingApprovalCardOut(
                well_id=well.id,
                well_name=well.name,
                customer_name=(
                    well.planning_node.customer.name
                    if well.planning_node is not None
                    and well.planning_node.customer is not None
                    else None
                ),
                demand_line_id=line.id,
                product_description=line.product.description or line.product_id,
                quantity=line.quantity,
                unit_of_measure=line.product.unit_of_measure,
                ros_date=line.ros_date.isoformat(),
                # No open request: the card renders these without decision
                # buttons and points at the substitution screen.
                approval_id=None,
                substitute_description=None,
            )
            for line, well in verdict_only_lines
        ],
    )


@router.get("/executive", response_model=ExecutiveSummaryOut)
def executive_dashboard(
    db: Session = Depends(get_db),
    customer_id: str | None = None,
    business_unit_id: str | None = None,
    allocation_horizon_months: int = Query(default=12),
    inventory_utilisation_horizon_months: int = Query(default=12),
    status: list[DemandStatus] | None = Query(default=None),
    profile: list[DemandProfile] | None = Query(default=None),
    bu_scope: str | None = Depends(planner_bu),
):
    """Executive aggregates, for a Business Unit and/or a customer.

    SCOPE
    -----
    Both filters are optional. `business_unit_id` is arguably the more natural
    management default: the Business Unit is the platform's HARD inventory
    boundary, so it is the smallest scope inside which the supply figures mean
    anything. The payload states which scope produced it, in `scope` and in the
    top-level `business_unit_id` / `business_unit_name` / `customer_id` /
    `customer_name` mirrors.

    Naming a customer together with a Business Unit it does not belong to is a
    **400**, not an empty result. The request describes a scope that cannot exist,
    and an empty payload would report no demand, no supply risk and no shortfall
    for it -- a measurement of nothing, which reads as good news. See
    `app.engines.executive.resolve_scope`.

    HONESTY
    -------
    This screen goes to management, so every block carries an `available` flag and
    a `reason` when false. A figure that cannot be computed from the data we hold
    is returned as unavailable -- never as 0, which would read as a measured zero.
    Specifically:

      * prior-period demand is reported unavailable unless every in-scope demand
        line's state at the comparison date can be genuinely reconstructed from
        `DemandRevision` history (see `app.engines.executive._state_as_of`);
      * coverage is measured by DEMAND QUANTITY, with the well counts kept beside it
        as reference, and the percentage is withheld when the in-scope book spans
        several units of measure;
      * `soft_allocation_coverage` reports what THIS platform's allocation achieved
        -- from the shared pool, from an Oracle assignment, via a substitute, or not
        at all -- and replaces the old `allocation` block, which reported Oracle's
        hard assignment and which the product owner asked to be reframed. Its
        figures come from the same single coverage pass as the coverage verdicts;
      * `incoming_supply` reports on-order material from the read-only projection of
        Oracle purchase orders. A product with no projected row has an UNKNOWN
        on-order quantity and the block says so; `oracle_integrated` stays false
        because the feed is still not live.

    Supply risk is the coverage engine's own `Unrecoverable` verdict, read rather
    than re-derived, and `first_runout` reuses `app.engines.well_dates`.
    """
    if business_unit_id is None and bu_scope is not None:
        # A planner's unscoped request means "my Business Unit", never "all".
        business_unit_id = bu_scope
    if allocation_horizon_months not in ALLOCATION_HORIZONS:
        raise HTTPException(
            status_code=400,
            detail=(
                "allocation_horizon_months must be one of "
                f"{list(ALLOCATION_HORIZONS)}"
            ),
        )
    if inventory_utilisation_horizon_months not in ALLOCATION_HORIZONS:
        raise HTTPException(
            status_code=400,
            detail=(
                "inventory_utilisation_horizon_months must be one of "
                f"{list(ALLOCATION_HORIZONS)}"
            ),
        )

    # Demand scope (well status / line profile) is variable per request, like
    # the Coverage Workspace's: the default serves the stored official
    # verdicts, anything else runs every block inside a read-only recompute
    # under the requested scope (scoped_verdicts) so all blocks agree and
    # nothing is persisted. The payload names the scope it was computed under.
    status_set = set(status) if status else None
    profile_set = set(profile) if profile else None
    try:
        with scoped_verdicts(db, status_set, profile_set) as (
            scope_is_default,
            skipped_customers,
        ):
            summary = executive_summary(
                db,
                customer_id=customer_id,
                business_unit_id=business_unit_id,
                allocation_horizon_months=allocation_horizon_months,
                inventory_utilisation_horizon_months=inventory_utilisation_horizon_months,
            )
            applied_status = sorted(
                s.value for s in (status_set or effective_status_filter(db))
            )
            applied_profile = sorted(
                p.value for p in (profile_set or effective_profile_filter(db))
            )
    except ScopeContradiction as exc:
        # 400, not an empty 200 -- see the docstring and `resolve_scope`.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        # An id that names nothing. Distinguished from the contradiction above
        # because they are different mistakes: one names a row that is not there,
        # the other names two rows that cannot be combined.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    summary_out = ExecutiveSummaryOut.model_validate(summary)
    summary_out.status_scope = applied_status
    summary_out.profile_scope = applied_profile
    summary_out.scope_is_default = scope_is_default
    summary_out.skipped_customers = list(skipped_customers)
    return summary_out
