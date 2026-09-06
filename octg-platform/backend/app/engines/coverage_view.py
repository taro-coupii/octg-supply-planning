"""Read-only coverage PROJECTION for arbitrary status/profile filters.

Why this module exists
----------------------
`CoverageResult` rows are written by `app.engines.coverage.recompute_customer`
under ONE pair of filters -- `DEFAULT_STATUS_FILTER` / `DEFAULT_PROFILE_FILTER`.
The Coverage Workspace, however, exposes those filters as user-facing toggles. A
caller asking for "Confirmed only" or "Primary + Contingency" is asking a
DIFFERENT question than the one the stored rows answer, so the stored rows are
not valid for that request:

  * a line the request includes but the defaults excluded has no stored verdict
    at all (the engine deletes rows for out-of-scope lines), and
  * every line's verdict was computed against a pool built from the DEFAULT
    include-set, so widening or narrowing the set changes who competes for the
    same steel and therefore changes the answers of lines that ARE in both sets.

Serving default-filter rows under a non-default filter label would be the same
class of defect as the stale-`CoverageResult` bug: a superseded verdict presented
as if it described what was asked. So this module recomputes, and it recomputes
using the REAL engine rather than a second copy of its logic -- a second copy
would drift, and a drifted coverage answer is worse than none.

How it stays read-only
----------------------
The engine only ever `flush()`es; it never commits. So the projection:

  1. refuses to run if the caller's session already has pending work, so a
     rollback here can never discard somebody else's uncommitted changes;
  2. runs `recompute_customer` with the REQUESTED filters, which writes rows and
     well rollups inside the open (uncommitted) transaction;
  3. harvests everything it needs into plain scalars in frozen dataclasses -- no
     ORM instance escapes, so a caller cannot mutate a mapped attribute it was
     handed;
  4. `rollback()`s the transaction, undoing every row written in step 2, then
     `expire_all()`s so the in-memory identity map cannot keep serving the
     values that were just rolled back (`Well.coverage_status` in particular is
     assigned in Python and would otherwise survive the rollback);
  5. asserts the session is clean afterwards, as a tripwire for future edits.

This deliberately mirrors the guarantees of `app.engines.scenario` (values in,
values out, nothing persisted, tripwire on top) while reusing the production
engine instead of re-deriving coverage. The trade-off is honest: a non-default
request costs a full recompute, and the route that calls it must be a GET that
never commits.

When the requested filters ARE the defaults there is nothing to recompute: the
stored rows are exactly the answer, and the fast path reads them.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session, joinedload

from app.engines.coverage import recompute_customer
from app.engines.inventory import InventoryNotScoped
from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
    is_default_scope,
    scope_override,
)
from app.engines.well_dates import WellDates, well_dates
from app.models import (
    CoverageStatus,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    PlanningNode,
    Well,
)

# Rollup statuses that count a LINE as covered. Single definition, mirroring the
# engine's own rollup rule so the grid's "covered of in-scope" count and the
# well rollup can never disagree.
COVERED_LINE_STATUSES = (
    CoverageStatus.COVERED,
    CoverageStatus.COVERED_VIA_SUBSTITUTE,
)


@dataclass(frozen=True)
class ProjectedLine:
    """One in-scope demand line's coverage verdict, as scalars."""

    demand_line_id: str
    well_id: str
    status: str
    reason: str | None
    #: When THIS verdict was computed (CoverageResult.computed_at). On the
    #: recomputed read-only path this is "now"; on the stored path it is the
    #: age of the official verdict -- surfacing it is the C-08 fix.
    computed_at: datetime | None = None


@dataclass(frozen=True)
class CoverageProjection:
    """Coverage as it stands under one specific pair of filters.

    `filters_are_default` is False when the numbers came from a read-only
    recompute rather than from the stored rows, and the API surfaces that so the
    UI can never present a projection as the official stored verdict.

    `lines` contains ONLY lines in scope of the filters. A line's absence means
    "not evaluated under these filters", never "covered".
    """

    status_filter: frozenset
    profile_filter: frozenset
    filters_are_default: bool
    recomputed_read_only: bool
    lines: dict[str, ProjectedLine]
    well_rollups: dict[str, str | None]
    #: Oldest / newest `computed_at` across the in-scope verdicts, so a screen
    #: can state how old the answers are instead of implying "just now" (C-08).
    verdicts_computed_from: datetime | None = None
    verdicts_computed_to: datetime | None = None
    #: Customers whose recompute was SKIPPED because they are not mapped to a
    #: Business Unit (no inventory pool exists for them). Their wells keep
    #: whatever rows the table already held; the API names them rather than
    #: letting one unmapped customer 409 the whole grid (the C-07 fix).
    skipped_customers: tuple[str, ...] = ()
    #: {well_id: WellDates} -- earliest ROS and first runout, under THESE filters.
    #:
    #: Harvested inside the projection rather than fetched by the caller afterwards,
    #: and that placement is load-bearing. `first_runout_date` is derived from the
    #: coverage verdicts, so on the non-default path it has to be read while the
    #: recomputed rows still exist: after the rollback the table holds the DEFAULT
    #: verdicts again, and a caller computing dates then would pair projected
    #: rollups with default-filter dates on the same grid row. That is precisely the
    #: mismatch `filters_are_default` exists to make impossible, so the dates travel
    #: with the projection that produced them.
    well_dates: dict[str, WellDates] = field(default_factory=dict)


def _pending(db: Session) -> tuple[int, int, int]:
    return (len(db.new), len(db.dirty), len(db.deleted))


def _in_scope(
    line: DemandLine,
    status_filter: set[DemandStatus],
    profile_filter: set[DemandProfile],
) -> bool:
    """Whether the coverage engine would evaluate `line` under these filters.

    The status is read from the line's WELL (`Well.demand_status`) and the profile
    from the line, mirroring `app.engines.coverage.compute_customer_coverage`
    exactly: status selects whole wells, profile selects lines. `_harvest`
    eager-loads the well, so this costs no query per line.
    """
    return (
        line.well.demand_status in status_filter and line.profile in profile_filter
    )


def project_coverage(
    db: Session,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
) -> CoverageProjection:
    """Coverage for every well, under `status_filter` / `profile_filter`.

    Writes nothing. See the module docstring for the mechanism and for why the
    non-default case must not be answered from stored rows.
    """
    # "Default" now means the platform's CURRENT persisted default, not the
    # shipped constant, and `is_default_scope` is the single definition of that
    # comparison. It matters in both directions: after an administrator widens the
    # scope, the stored rows are the ones written under the WIDER scope, so
    # comparing against the constant would send the fast path down the read-only
    # recompute for the request the table already answers -- and would label the
    # answer a projection -- while treating the now-ad-hoc old constant as
    # official.
    status_filter = set(status_filter or effective_status_filter(db))
    profile_filter = set(profile_filter or effective_profile_filter(db))
    is_default = is_default_scope(db, status_filter, profile_filter)

    if is_default:
        return _harvest(
            db, status_filter, profile_filter, is_default=True, recomputed=False
        )

    before = _pending(db)
    if before != (0, 0, 0):
        raise RuntimeError(
            "project_coverage recomputes coverage inside the caller's "
            "transaction and rolls it back, so it refuses to run on a session "
            f"with pending work (new, dirty, deleted) = {before}. Call it from a "
            "read-only request handler."
        )

    customers = db.query(Customer).all()
    try:
        skipped = _recompute_all(db, customers, status_filter, profile_filter)
        projection = _harvest(
            db,
            status_filter,
            profile_filter,
            is_default=False,
            recomputed=True,
            skipped_customers=skipped,
        )
    finally:
        # Undo every row the recompute wrote, then drop the identity map so no
        # attribute assigned in Python (Well.coverage_status) outlives it.
        db.rollback()
        db.expire_all()

    after = _pending(db)
    if after != (0, 0, 0):
        raise AssertionError(
            "project_coverage must leave the session clean; pending "
            f"(new, dirty, deleted) = {after}"
        )
    return projection


def _recompute_all(
    db: Session,
    customers: list[Customer],
    status_filter: set[DemandStatus],
    profile_filter: set[DemandProfile],
) -> tuple[str, ...]:
    """Recompute every Business Unit, ISOLATING failures (C-07 fix).

    BOTH `InventoryNotScoped` shapes are isolated: a customer with no
    Business Unit (`InventoryScopeMissing` -- our own reference data) and a
    customer whose BU is missing an on-hand row for a demanded product
    (`InventoryRowMissing` -- incomplete upstream data, the LIKELIER failure
    in production). Before this isolation existed, either one took the whole
    coverage grid down with a 409/424 for every user. Now the customer is
    skipped and NAMED -- the caller reports who was left out rather than
    failing everyone or hiding the gap.
    """
    # ONE pass per Business Unit -- the unit of allocation since D01 -- and each
    # entry NAMES ITS OWN REASON. It used to be a bare customer name, which the
    # screens then diagnosed for themselves as "not mapped to a Business Unit";
    # that was the only cause while the unit was the customer, and it is now
    # wrong whenever a mapped Business Unit is missing one on-hand row and every
    # customer sharing that pool is skipped together.
    #
    # NO SAVEPOINT is used inside, deliberately: pysqlite's transaction handling
    # breaks SAVEPOINT semantics (a RELEASE can behave as a commit), which turned
    # the read-only rollback into a persist. Every caller of this function rolls
    # the WHOLE transaction back (project_coverage / scoped_verdicts), so nothing
    # persists; the cost is that a skipped pool's harvested verdicts can be a mix
    # of old and half-new for the duration of one response, and the response
    # names it.
    from app.engines.coverage import recompute_all_business_units

    sweep = recompute_all_business_units(
        db,
        status_filter=status_filter,
        profile_filter=profile_filter,
        customers=customers,
        use_savepoints=False,
    )
    return tuple(
        sorted(
            f"{name} -- {failure.reason}"
            for failure in sweep.failures
            for name in failure.customer_names
        )
    )


def _harvest(
    db: Session,
    status_filter: set[DemandStatus],
    profile_filter: set[DemandProfile],
    is_default: bool,
    recomputed: bool,
    skipped_customers: tuple[str, ...] = (),
) -> CoverageProjection:
    """Read the current in-transaction coverage state into plain scalars.

    The status/profile filters are RE-APPLIED here rather than trusted. On the
    default fast path that is the same belt-and-braces `app.api.wells._well_out`
    applies: a `CoverageResult` row written by an older build can outlive its
    scope, and such a row must never be served. On the recomputed path the
    filter is a no-op by construction, which is exactly what makes it safe to
    apply unconditionally.
    """
    rows = (
        db.query(DemandLine)
        .options(
            joinedload(DemandLine.coverage_result),
            # The well carries the demand status the filter selects on, so it is
            # eager-loaded rather than lazily fetched once per line -- this query
            # spans every demand line in the platform.
            joinedload(DemandLine.well),
        )
        .all()
    )
    lines: dict[str, ProjectedLine] = {}
    for line in rows:
        if not _in_scope(line, status_filter, profile_filter):
            continue
        cr = line.coverage_result
        if cr is None:
            continue
        lines[line.id] = ProjectedLine(
            demand_line_id=line.id,
            well_id=line.well_id,
            status=cr.status.value,
            reason=cr.reason,
            computed_at=cr.computed_at,
        )
    stamps = [l.computed_at for l in lines.values() if l.computed_at is not None]

    well_rollups = {
        well_id: status
        for well_id, status in db.query(Well.id, Well.coverage_status).all()
    }
    # Same filters, and the SAME single definition of both dates
    # (`app.engines.well_dates`), so the grid cannot invent its own reading of
    # "earliest ROS" and the Home Dashboard cannot invent a different one.
    dates = well_dates(
        db, status_filter=status_filter, profile_filter=profile_filter
    )
    return CoverageProjection(
        status_filter=frozenset(status_filter),
        profile_filter=frozenset(profile_filter),
        filters_are_default=is_default,
        recomputed_read_only=recomputed,
        lines=lines,
        well_rollups=well_rollups,
        well_dates=dates,
        verdicts_computed_from=min(stamps) if stamps else None,
        verdicts_computed_to=max(stamps) if stamps else None,
        skipped_customers=skipped_customers,
    )


@contextmanager
def scoped_verdicts(
    db: Session,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
):
    """Run a read-only computation under a caller-requested coverage scope.

    Yields `(filters_are_default: bool, skipped_customers: tuple[str, ...])`.
    Skipped customers are the ones whose recompute failed on missing scope
    data (no BU, or a missing on-hand row) -- isolated per customer via
    `_recompute_all`, exactly as on the coverage grid, and NAMED so the
    caller can put them in its payload rather than let the gap pass
    silently. Inside the block, EVERYTHING agrees on the requested scope:

      * the accessors (`effective_status_filter` / `effective_profile_filter`)
        answer with it, via `coverage_scope.scope_override`, so every engine
        that resolves the scope internally -- executive's `_in_scope`,
        `well_dates`, `compute_customer_coverage`, `inventory_utilisation` --
        computes against the same include-set; and
      * the stored `CoverageResult` rows and `Well.coverage_status` rollups
        describe that scope too, because on the non-default path every customer
        is recomputed inside the open (uncommitted) transaction first --
        exactly `project_coverage`'s mechanism, generalised so a multi-block
        reader (the Executive dashboard, the Surplus report) can sit inside it.

    On exit the transaction is rolled back and the identity map expired, so
    nothing persists. The caller must therefore harvest PLAIN VALUES inside the
    block -- every engine used this way already returns scalar dataclasses.

    When the requested scope IS the platform default there is nothing to do:
    the block runs against the stored rows with no override and no rollback.
    """
    status_filter = set(status_filter or effective_status_filter(db))
    profile_filter = set(profile_filter or effective_profile_filter(db))
    if is_default_scope(db, status_filter, profile_filter):
        yield True, ()
        return

    before = _pending(db)
    if before != (0, 0, 0):
        raise RuntimeError(
            "scoped_verdicts recomputes coverage inside the caller's "
            "transaction and rolls it back, so it refuses to run on a session "
            f"with pending work (new, dirty, deleted) = {before}. Call it from "
            "a read-only request handler."
        )
    try:
        # The recompute runs BEFORE the override is armed. It receives the
        # filters explicitly, so it does not need the override -- and a failed
        # customer's SAVEPOINT rollback fires after_soft_rollback, which would
        # clear the override's memo mid-computation if it were already set.
        skipped = _recompute_all(
            db, db.query(Customer).all(), status_filter, profile_filter
        )
        with scope_override(db, status_filter, profile_filter):
            yield False, skipped
    finally:
        db.rollback()
        db.expire_all()

    after = _pending(db)
    if after != (0, 0, 0):
        raise AssertionError(
            "scoped_verdicts must leave the session clean; pending "
            f"(new, dirty, deleted) = {after}"
        )


def planning_node_path(node: PlanningNode | None) -> str:
    """"Customer / Project / Pad" style breadcrumb for a well's planning node.

    Walks `parent_id` upward. PlanningNode is a generic, self-referencing
    hierarchy of arbitrary depth (`node_type` is free text on purpose), so the
    path is built rather than assumed to be one level. A cycle -- which the
    schema does not prevent -- terminates the walk instead of hanging.
    """
    if node is None:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    current: PlanningNode | None = node
    while current is not None and current.id not in seen:
        seen.add(current.id)
        parts.append(current.name)
        current = current.parent
    return " / ".join(reversed(parts))
