from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from sqlalchemy.orm import joinedload, object_session

from app.db import get_db
from app.engines.coverage import set_well_demand_status
from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
)
from app.engines.coverage_view import planning_node_path
from app.engines.well_dates import WellDates, sort_by_earliest_ros, well_dates
from app.auth.scope import planner_bu
from app.models import CoverageStatus, DemandLine, DemandStatus, PlanningNode, Well, Customer
from app.schemas import (
    DemandLineOut,
    WellDemandStatusIn,
    WellDemandStatusChangeOut,
    WellOut,
    WellSummary,
)

router = APIRouter(prefix="/wells", tags=["wells"])


def _well_summary(well: Well, dates: WellDates | None) -> WellSummary:
    """A well as it appears in a LIST, with its two dates.

    The dates come from `app.engines.well_dates` -- the same function the coverage
    grid uses -- so a well's "earliest ROS" is one number in the platform rather
    than one per screen.
    """
    node = well.planning_node
    customer = node.customer if node is not None else None
    return WellSummary(
        id=well.id,
        name=well.name,
        customer_id=customer.id if customer is not None else None,
        customer_name=customer.name if customer is not None else None,
        demand_status=well.demand_status,
        coverage_status=well.coverage_status,
        earliest_ros_date=dates.earliest_ros_date if dates else None,
        first_runout_date=dates.first_runout_date if dates else None,
    )


def _well_out(well: Well, dates: WellDates | None = None) -> WellOut:
    """Serialise a well and all of its demand lines.

    A well whose DEMAND STATUS is out of scope has ALL of its lines out of scope
    -- status is a property of the well (`app.models.well.Well.demand_status`), so
    the exclusion is never partial. Such lines are
    still LISTED, because a planner needs to see their Planned and Budgeted
    demand as well as the demand that competes for steel -- but they are
    never given a coverage status. The engine does not evaluate them, so it has
    no verdict to report, and reporting a superseded one would be worse than
    reporting none. The engine also deletes their CoverageResult rows, so this is
    belt-and-braces against any row that outlives its scope.

    WHICH scope, and where it comes from
    -----------------------------------
    The platform's CURRENT coverage scope, resolved from the well's own session
    (`app.engines.coverage_scope`) rather than from the shipped constants. That
    matters as soon as an administrator widens the scope: the engine would then be
    writing verdicts for -- say -- Budgeted wells, and a belt-and-braces filter
    still pinned to the constant would HIDE every one of them, so the Well
    Workspace would show no verdict for a well the coverage grid showed a verdict
    for. The guard has to describe the same scope it is guarding.

    Resolved through `object_session` because this function's signature is called
    with a well alone in several places (including the test suite) and takes no
    `db`; it costs one primary-key load per session, identity-mapped thereafter.
    """
    db = object_session(well)
    status_filter = effective_status_filter(db)
    profile_filter = effective_profile_filter(db)
    lines = []
    for line in sorted(well.demand_lines, key=lambda l: (l.ros_date, l.id)):
        evaluated = (
            well.demand_status in status_filter
            and line.profile in profile_filter
        )
        cr = line.coverage_result if evaluated else None
        lines.append(
            DemandLineOut(
                id=line.id,
                product_id=line.product_id,
                # The defect this fixes: without a description the Well Workspace
                # had nothing but the UUID to put in the product column. Falls back
                # to the id only when the catalogue row genuinely has no
                # description, which at least renders as an identifier rather than
                # as a blank cell.
                product_description=line.product.description or line.product_id,
                quantity=line.quantity,
                unit_of_measure=line.product.unit_of_measure,
                ros_date=line.ros_date,
                # The WELL's demand status, repeated on every line of the well.
                # Served per line because the Well Workspace renders a demand
                # table and a status column that vanished from it would read as
                # "unknown"; it is the same value on every row by construction.
                status=well.demand_status,
                profile=line.profile,
                current_revision_no=line.current_revision_no,
                coverage_status=cr.status.value if cr else None,
                coverage_reason=cr.reason if cr else None,
            )
        )
    node = well.planning_node
    customer = node.customer if node is not None else None
    return WellOut(
        id=well.id,
        name=well.name,
        demand_status=well.demand_status,
        coverage_status=well.coverage_status,
        customer_id=customer.id if customer is not None else None,
        customer_name=customer.name if customer is not None else None,
        planning_node_path=planning_node_path(node),
        earliest_ros_date=dates.earliest_ros_date if dates else None,
        first_runout_date=dates.first_runout_date if dates else None,
        demand_lines=lines,
    )


@router.get("", response_model=list[WellSummary])
def list_wells(
    db: Session = Depends(get_db),
    coverage_status: str | None = None,
    demand_status: list[DemandStatus] | None = Query(default=None),
    bu_scope: str | None = Depends(planner_bu),
):
    """Every well, SORTED BY EARLIEST ROS ASCENDING.

    The order is set on the server, not left to the caller, because the product
    owner asked for wells "in order of the earliest date for the well" and an
    ordering each screen applies for itself is an ordering they disagree about. See
    `app.engines.well_dates.sort_by_earliest_ros` for the tie-break rule and for
    why a well with no in-scope demand sorts last rather than first.

    `coverage_status` filters on the well ROLLUP, so the Home Dashboard's
    uncovered-wells card and this route return the same rows in the same order.
    """
    # Eager-load the node->customer chain: the serializer names the customer
    # on every row, and lazy-loading it was two queries per well on the
    # primary list screen.
    query = db.query(Well).options(
        joinedload(Well.planning_node).joinedload(PlanningNode.customer)
    )
    if bu_scope is not None:
        # A planner's list is their Business Unit's list (review 2026-09-06, F01).
        query = (
            query.join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .join(Customer, PlanningNode.customer_id == Customer.id)
            .filter(Customer.business_unit_id == bu_scope)
        )
    if coverage_status is not None:
        query = query.filter(Well.coverage_status == coverage_status)
    if demand_status:
        # Filters on the PLANNER-SET input, not the engine-written rollup above.
        # Whole wells, which is the point: there is no such thing as half a well
        # being Confirmed.
        query = query.filter(Well.demand_status.in_(demand_status))
    wells = query.all()
    dates = well_dates(db, [w.id for w in wells])
    rows = [_well_summary(w, dates.get(w.id)) for w in wells]
    return sort_by_earliest_ros(rows, key=lambda row: row)


@router.get("/{well_id}", response_model=WellOut)
def get_well(well_id: str, db: Session = Depends(get_db)):
    well = (
        db.query(Well)
        .options(
            joinedload(Well.demand_lines).joinedload(DemandLine.product),
            joinedload(Well.demand_lines).joinedload(DemandLine.coverage_result),
            joinedload(Well.planning_node).joinedload(PlanningNode.customer),
        )
        .filter(Well.id == well_id)
        .one_or_none()
    )
    if well is None:
        raise HTTPException(status_code=404, detail="Well not found")
    # One well, so `well_dates` is asked for exactly one -- it takes an id list
    # precisely so the single-well and whole-list cases share one implementation of
    # the definition rather than two.
    dates = well_dates(db, [well.id]).get(well.id)
    return _well_out(well, dates)


@router.put(
    "/{well_id}/demand-status", response_model=WellDemandStatusChangeOut
)
def set_demand_status(
    well_id: str, body: WellDemandStatusIn, db: Session = Depends(get_db)
):
    """Change this well's demand status. THE well-level demand operation.

    Demand status is a property of the well, so this is where it changes -- there
    is deliberately no per-line equivalent, and
    `POST /demand-lines/{id}/revisions` REFUSES a status rather than accepting one
    and quietly applying it to the whole well.

    It is not a plain field update. The engine
    (`app.engines.coverage.set_well_demand_status`) cascades a `DemandRevision` to
    EVERY demand line of the well, writes one `ImpactRecord` per line so the Home
    Dashboard's "Demand Changes" card shows the transition, and recomputes coverage
    for the whole customer pool -- because the status filter selects wells, so this
    well has just entered or left the pool that competes for inventory and every
    other well's verdict may legitimately move with it.

    Setting the status it already has is accepted and writes NOTHING;
    `unchanged: true` says so. That avoids appending a revision to every line
    recording a change of nothing.
    """
    well = db.get(Well, well_id)
    if well is None:
        raise HTTPException(status_code=404, detail="Well not found")
    change = set_well_demand_status(db, well, body.demand_status)
    db.commit()
    return WellDemandStatusChangeOut.model_validate(change, from_attributes=True)
