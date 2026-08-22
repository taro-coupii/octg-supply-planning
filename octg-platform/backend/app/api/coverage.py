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
from app.engines.coverage_view import (
    COVERED_LINE_STATUSES,
    planning_node_path,
    project_coverage,
)
from app.engines.well_dates import sort_by_earliest_ros
from app.models import (
    CoverageStatus,
    DemandProfile,
    DemandStatus,
    PlanningNode,
    Well,
)
from app.schemas import CoverageGridOut, CoverageGridRowOut, CoverageGridFiltersOut

router = APIRouter(prefix="/coverage", tags=["coverage"])

_COVERED_VALUES = {s.value for s in COVERED_LINE_STATUSES}


@router.get("", response_model=CoverageGridOut)
def coverage_grid(
    db: Session = Depends(get_db),
    customer_id: str | None = None,
    coverage_status: list[str] | None = Query(default=None),
    status: list[DemandStatus] | None = Query(default=None),
    profile: list[DemandProfile] | None = Query(default=None),
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

    query = db.query(Well).options(
        joinedload(Well.planning_node).joinedload(PlanningNode.customer)
    )
    if customer_id is not None:
        query = query.join(
            PlanningNode, Well.planning_node_id == PlanningNode.id
        ).filter(PlanningNode.customer_id == customer_id)
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
