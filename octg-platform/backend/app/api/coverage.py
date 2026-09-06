"""Cross-well coverage grid -- the Coverage Workspace.

Read-only. When the caller asks for non-default status/profile filters the grid
is recomputed READ-ONLY for exactly those filters (see
`app.engines.coverage_view`), because the stored `CoverageResult` rows were
computed under the DEFAULTS and do not answer the question that was asked. The
response always states which happened, so a projection can never be mistaken for
the official stored verdict.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.engines.coverage import recompute_all_business_units
from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
)
from app.engines.coverage_view import (
    COVERED_LINE_STATUSES,
    planning_node_path,
    project_coverage,
)
from app.engines.inventory import InventoryNotScoped
from app.engines.well_dates import sort_by_earliest_ros
from app.auth.scope import planner_bu
from app.models import (
    CoverageStatus,
    Customer,
    DemandProfile,
    DemandStatus,
    PlanningNode,
    Well,
)
from app.schemas import (
    CoverageGridOut,
    CoverageGridRowOut,
    CoverageGridFiltersOut,
    CoverageRecomputeIn,
    CoverageRecomputeOut,
    CoverageRecomputeSkippedOut,
)

router = APIRouter(prefix="/coverage", tags=["coverage"])

_COVERED_VALUES = {s.value for s in COVERED_LINE_STATUSES}


@router.get("", response_model=CoverageGridOut)
def coverage_grid(
    db: Session = Depends(get_db),
    customer_id: str | None = None,
    coverage_status: list[str] | None = Query(default=None),
    status: list[DemandStatus] | None = Query(default=None),
    profile: list[DemandProfile] | None = Query(default=None),
    bu_scope: str | None = Depends(planner_bu),
):
    """One row per well: counts of in-scope and covered lines, plus the rollup.

    `status` / `profile` are the Coverage Workspace's filter toggles. Omit them to
    get the platform defaults (Confirmed only, Primary + Contingency) and therefore the
    OFFICIAL stored verdicts.

    Passing anything else triggers a read-only recompute for those filters and
    sets `filters.recomputed_read_only = true`. That is deliberate rather than
    convenient: the stored rows were computed under the defaults, and the filters
    do not merely hide lines -- they change WHICH lines compete for the same
    inventory, so a line present under both filter sets can legitimately have a
    different verdict under each. Returning default-filter rows under a
    non-default label would be the stale-coverage defect again, in a new place.
    Nothing is persisted by such a request; the official verdicts are untouched.

    `coverage_status` filters on the WELL ROLLUP ("Covered", "Uncovered", or
    "Unevaluated" for a well with no in-scope demand at all).
    """
    projection = project_coverage(db, status_filter=status, profile_filter=profile)

    query = (
        db.query(Well)
        .options(joinedload(Well.planning_node).joinedload(PlanningNode.customer))
        .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
    )
    if bu_scope is not None:
        # A planner's grid is their Business Unit's grid (review 2026-09-06, F01).
        query = query.join(Customer, PlanningNode.customer_id == Customer.id).filter(
            Customer.business_unit_id == bu_scope
        )
    if customer_id is not None:
        query = query.filter(PlanningNode.customer_id == customer_id)
    # Fetched by name for a deterministic base order; the ROWS are then sorted by
    # earliest ROS (see below), with the name as the tie-break, so this ordering
    # only decides ties.
    wells = query.order_by(Well.name).all()

    # In-scope line counts come from the SAME projection as the verdicts, so the
    # denominator and the numerator can never describe different filter sets.
    in_scope_by_well: dict[str, int] = {}
    covered_by_well: dict[str, int] = {}
    for line in projection.lines.values():
        in_scope_by_well[line.well_id] = in_scope_by_well.get(line.well_id, 0) + 1
        if line.status in _COVERED_VALUES:
            covered_by_well[line.well_id] = covered_by_well.get(line.well_id, 0) + 1

    rows: list[CoverageGridRowOut] = []
    for well in wells:
        node = well.planning_node
        rollup = projection.well_rollups.get(well.id)
        in_scope = in_scope_by_well.get(well.id, 0)
        # From the projection, so under non-default filters these are the dates for
        # the filters actually requested rather than the stored defaults. See
        # `CoverageProjection.well_dates`.
        dates = projection.well_dates.get(well.id)
        rows.append(
            CoverageGridRowOut(
                well_id=well.id,
                well_name=well.name,
                planning_node_id=node.id if node else None,
                planning_node_path=planning_node_path(node),
                customer_id=node.customer_id if node else None,
                customer_name=node.customer.name if node and node.customer else None,
                # Planner-set input, so an unevaluated row can name its own cause
                # rather than only its consequence.
                demand_status=well.demand_status.value,
                in_scope_line_count=in_scope,
                covered_line_count=covered_by_well.get(well.id, 0),
                # A well with no in-scope demand is UNEVALUATED, not covered and
                # not uncovered. Presenting it as either would be a claim the
                # engine never made.
                coverage_status=rollup,
                evaluated=rollup is not None,
                earliest_ros_date=dates.earliest_ros_date if dates else None,
                first_runout_date=dates.first_runout_date if dates else None,
            )
        )

    # Sorted by earliest ROS ascending, server-side, by the same helper the Home
    # Dashboard and GET /wells use -- so the three screens agree on the order rather
    # than each sorting to its own taste. Applied BEFORE the coverage_status filter
    # below, so filtering removes rows without reordering the survivors.
    rows = sort_by_earliest_ros(rows)

    if coverage_status:
        wanted = {s.strip().lower() for s in coverage_status}
        allowed = {
            CoverageStatus.COVERED.value.lower(),
            CoverageStatus.UNCOVERED.value.lower(),
            "unevaluated",
        }
        unknown = wanted - allowed
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    "coverage_status must be one of Covered, Uncovered, "
                    f"Unevaluated (well rollup values); got {sorted(unknown)}"
                ),
            )
        rows = [
            row
            for row in rows
            if (row.coverage_status or "unevaluated").lower() in wanted
        ]

    return CoverageGridOut(
        verdicts_computed_from=projection.verdicts_computed_from,
        verdicts_computed_to=projection.verdicts_computed_to,
        filters=CoverageGridFiltersOut(
            status=sorted(s.value for s in projection.status_filter),
            profile=sorted(p.value for p in projection.profile_filter),
            are_default=projection.filters_are_default,
            recomputed_read_only=projection.recomputed_read_only,
            skipped_customers=list(projection.skipped_customers),
            explanation=(
                "Official stored coverage verdicts, computed under the platform "
                "default filters."
                if projection.filters_are_default
                else (
                    "PROJECTION. These filters differ from the platform defaults, "
                    "so coverage was recomputed read-only for exactly the filters "
                    "requested. Nothing was saved and the official stored verdicts "
                    "are unchanged. Stored rows were not reused because the filters "
                    "change which demand lines compete for the same inventory, not "
                    "merely which ones are displayed."
                )
            ),
        ),
        well_count=len(rows),
        rows=rows,
    )


@router.post("/recompute", response_model=CoverageRecomputeOut)
def recompute_coverage(
    payload: CoverageRecomputeIn | None = None,
    db: Session = Depends(get_db),
):
    """Rewrite the stored coverage verdicts. Every customer, or one named customer.

    THE TRIGGER C-08 SAID WAS MISSING
    =================================
    `CoverageResult` is written only as a side effect of something happening -- a
    revision applied, an approval decided, a well status changed, inventory edited,
    a substitution registered. That is normally right: the verdict follows the fact
    that changed it. It leaves one hole, and it is the hole this platform actually
    fell into repeatedly (see HANDOFF §7): after an out-of-band data change, or after
    an ENGINE fix, the code is correct and the database still holds yesterday's
    answer, with no legal way to ask for a new one short of inventing a write.
    `computed_at` on the grid already makes that staleness visible; this makes it
    fixable.

    IT WRITES THE OFFICIAL VERDICT, SO IT TAKES NO FILTERS
    =====================================================
    The official verdict is by definition the one computed under the platform's
    default scope, so this endpoint reads that scope and passes it -- it does not
    accept status/profile parameters. The question "what would the verdict be under a
    different scope" is answered read-only by `app.engines.coverage_view`, which
    recomputes, yields and then ROLLS BACK precisely so a projection can never be
    mistaken for, or written over, the stored answer. Letting a caller persist an
    arbitrary scope here would destroy that distinction.

    PER-CUSTOMER ISOLATION, PER-CUSTOMER COMMIT
    ===========================================
    Each customer is recomputed and committed on its own. That is deliberate and it
    is NOT what the read path does: `coverage_view._recompute_all` runs every customer
    inside one transaction that its callers always roll back, and its docstring warns
    that a failed customer can leave partial in-transaction writes. Safe there,
    unacceptable here -- this path commits, so a half-written customer would become
    the stored truth. Committing per customer means a customer with incomplete
    inventory facts costs only itself: it keeps whatever verdicts it already had, and
    is NAMED in `skipped_customers` rather than being silently dropped or taking every
    other customer's recompute down with it. The same C-07 lesson, applied to writes.
    """
    body = payload or CoverageRecomputeIn()

    if body.customer_id is not None:
        customer = db.get(Customer, body.customer_id)
        if customer is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no customer with id {body.customer_id!r}. Nothing was recomputed."
                ),
            )
        customers = [customer]
    else:
        customers = db.query(Customer).order_by(Customer.name).all()

    status_filter = effective_status_filter(db)
    profile_filter = effective_profile_filter(db)

    computed_customers = 0
    computed_lines = 0
    skipped: list[CoverageRecomputeSkippedOut] = []

    # ONE pass per Business Unit -- the unit of allocation since D01 -- with each
    # pool's failure isolated to the customers that share it. The response still
    # names customers, because that is who an operator is looking for.
    sweep = recompute_all_business_units(
        db,
        status_filter=status_filter,
        profile_filter=profile_filter,
        customers=customers,
    )
    db.commit()
    computed_customers = len(sweep.recomputed_customer_ids)
    computed_lines = sweep.recomputed_line_count
    skipped = [
        CoverageRecomputeSkippedOut(
            customer_id=customer_id,
            name=customer_name,
            reason=failure.reason,
        )
        for failure in sweep.failures
        for customer_id, customer_name in zip(
            failure.customer_ids, failure.customer_names
        )
    ]

    scope = (
        f"status [{', '.join(sorted(s.value for s in status_filter))}] / "
        f"profile [{', '.join(sorted(p.value for p in profile_filter))}]"
    )
    widened = (
        " Naming a customer recomputes its whole Business Unit: the pool is divided "
        "once across every customer that shares it, so one customer's verdicts "
        "cannot be re-derived on their own."
    )
    if skipped:
        note = (
            f"Recomputed {computed_customers} customer(s) and {computed_lines} demand "
            f"line(s) under the platform default scope: {scope}.{widened} "
            f"{len(skipped)} customer(s) could NOT be evaluated and keep the verdicts "
            "they already had -- each is named above with the reason. This is "
            "isolation, not partial success: a Business Unit whose inventory facts "
            "are incomplete costs only the customers that share its pool."
        )
    else:
        note = (
            f"Recomputed {computed_customers} customer(s) and {computed_lines} demand "
            f"line(s) under the platform default scope: {scope}.{widened} These are "
            "now the official stored verdicts."
        )

    return CoverageRecomputeOut(
        computed_customers=computed_customers,
        computed_lines=computed_lines,
        skipped_customers=skipped,
        note=note,
    )
