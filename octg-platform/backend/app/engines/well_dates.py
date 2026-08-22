"""Per-well DATES: earliest ROS, and the first date the well hits a shortage.

Both figures are DERIVED and are computed on demand, never stored. That is the
same rule `CoverageResult` follows for a different reason: a stored copy of a
derived fact goes stale silently, and this project's stated preference is to
delete derived data rather than let it rot (see
`app.models.coverage.CoverageResult`). A stored `first_runout_date` would be
wrong the moment anybody revised a quantity, and nothing would say so.

--------------------------------------------------------------------------
WHAT "FIRST RUNOUT DATE" MEANS HERE, AND WHY
--------------------------------------------------------------------------
    The earliest ROS date among this well's IN-SCOPE demand lines that coverage
    did NOT satisfy.

"Did not satisfy" means the line's `CoverageResult.status` is not one of
COVERED / COVERED_VIA_SUBSTITUTE -- i.e. UNCOVERED, UNRECOVERABLE,
PENDING_APPROVAL, or no verdict at all. A well whose every in-scope line is
satisfied returns None. None means "this well has no shortage", and it is a real
answer rather than a missing one; there is deliberately no sentinel date, because
a far-future date sorts and renders like a fact.

The rejected alternative, and the two cases that reject it
---------------------------------------------------------
The other candidate reading was "the first month the MRP runout projection for
one of this well's products closes negative" -- `app.engines.mrp._runout_series`
computes exactly that, per product, and reusing it was the obvious move. It is
NOT used, and the reason is that it answers a question about a PRODUCT while this
field makes a claim about a WELL. Two concrete cases in the seeded demo data show
the gap, in opposite directions:

  It reports a date for a well that has no problem, too early.
      `_runout_series` buckets the demand of EVERY well charged to a product. If
      the 13CR TBG pool is exhausted in month M by Osprey-09's demand, the series
      closes negative in M for every consumer of that product -- including
      Eagle-01, whose own line was satisfied out of the same pool because its ROS
      came first. Eagle-01 is Covered. Handing it a runout date in month M would
      state that a fully covered well "has a problem from month M", which is
      false, and it would put Eagle-01 on any screen that sorts wells by trouble.

  It reports NO date for a well that certainly does have a problem.
      Merlin-05 (HARD allocation) is Uncovered beside 8000 unassigned tubulars.
      Total demand on that product never exceeds what the Business Unit holds, so
      the projection's closing balance never goes negative and the projection
      reading returns None -- "no shortage" -- for a well the coverage engine has
      declared Uncovered. The shortage there is created by the ALLOCATION POLICY,
      which a monthly balance curve cannot see at all. The same blindness applies
      to Gadwall-22 (short only because another customer holds the assignment) and
      to Kittiwake-20 (blocked pending an Oracle release).

So the projection is the wrong instrument: a quantity curve knows about tonnage
over time and knows nothing about assignments, approvals, substitution or
Business Unit scope, all of which decide whether THIS well can be served.
`CoverageResult` is the platform's single authority on that question -- it is
what the coverage engine wrote after applying every one of those rules -- so this
module READS that verdict and uses ROS only for the timing. It re-derives no
coverage of its own, exactly as `app.engines.mrp` does not.

The date is therefore the date the well is first let down, which is what
"this well has a problem from date X" means to a planner: the first ROS they
cannot meet. Not the date the warehouse balance crossed zero, which may be
somebody else's fault entirely and is already reported, correctly and per
product, on the MRP By Item screen.

A line with NO CoverageResult counts as unsatisfied
---------------------------------------------------
Same convention as `app.engines.mrp._unresolved`, and for the same reason: a
missing row means the coverage engine has not evaluated that line, and
unevaluated demand must never be silently reported as fine. The honest reading of
"not evaluated" is "not known to be covered", so such a line contributes its ROS.

--------------------------------------------------------------------------
FILTERS AND BATCHING
--------------------------------------------------------------------------
Scope is the SAME status/profile filter pair the coverage engine applied, passed
in rather than hard-coded so the Coverage Workspace's non-default projections can
ask for their own -- `earliest_ros_date` under "Confirmed only" is a different and
equally legitimate number from the one under "Confirmed + Planned".

`well_dates` answers for MANY wells in TWO queries total, independent of the
number of wells: one for the wells themselves and one for their demand lines with
the coverage verdict outer-joined. The Home Dashboard and the coverage grid both
list every well, so a per-well round trip -- let alone a per-well runout
projection, which would have been N full passes over the whole demand book -- is
the difference between one query and several hundred. `tests/test_well_dates.py`
asserts the query count does not grow with N, so a future edit that reintroduces
a per-well lookup fails rather than merely gets slower.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engines.coverage_scope import (
    effective_profile_filter,
    effective_status_filter,
)
from app.models import (
    CoverageResult,
    CoverageStatus,
    DemandLine,
    DemandProfile,
    DemandStatus,
    Well,
)

__all__ = ["WellDates", "SATISFIED_STATUSES", "well_dates", "sort_by_earliest_ros"]

# Verdicts that mean this line HAS been satisfied and so contributes no shortage
# date. Everything else does, including the absence of a verdict. Mirrors
# `app.engines.coverage_view.COVERED_LINE_STATUSES` -- kept as its own name here
# because the question is "was it satisfied", not "does it count as covered in a
# percentage", and the two could legitimately diverge later.
SATISFIED_STATUSES = frozenset(
    {CoverageStatus.COVERED.value, CoverageStatus.COVERED_VIA_SUBSTITUTE.value}
)


@dataclass(frozen=True)
class WellDates:
    """The two dates for one well. Scalars only -- no ORM row escapes.

    `earliest_ros_date` is None only when the well has NO in-scope demand at all,
    which is the same condition that makes its coverage rollup None ("unevaluated
    -- neither covered nor uncovered").

    `first_runout_date` is None when every in-scope line was satisfied. See the
    module docstring; None is an answer, not a gap.
    """

    well_id: str
    earliest_ros_date: datetime | None
    first_runout_date: datetime | None
    #: How many in-scope lines the two dates were computed from. Carried so a
    #: caller can tell "no shortage" (None with lines > 0) apart from "nothing was
    #: looked at" (None with lines == 0) without a second query.
    in_scope_line_count: int = 0

    @property
    def has_shortage(self) -> bool:
        return self.first_runout_date is not None


def well_dates(
    db: Session,
    well_ids: list[str] | set[str] | None = None,
    status_filter: set[DemandStatus] | None = None,
    profile_filter: set[DemandProfile] | None = None,
) -> dict[str, WellDates]:
    """{well_id: WellDates} for `well_ids`, or for EVERY well when None.

    Every requested well gets an entry, including wells with no demand at all --
    an absent key would force each caller to invent the empty case, and one of
    them would invent it differently.

    Two queries regardless of how many wells are asked for. See the module
    docstring.
    """
    # `None` means the platform's CURRENT coverage scope, not the shipped
    # constant -- these dates are read straight onto the same grid rows as the
    # coverage verdicts, so a date computed under a different scope than the
    # verdict beside it would be exactly the mismatch
    # `CoverageProjection.filters_are_default` exists to make impossible.
    status_filter = set(status_filter or effective_status_filter(db))
    profile_filter = set(profile_filter or effective_profile_filter(db))

    well_query = select(Well.id)
    if well_ids is not None:
        wanted = list({w for w in well_ids})
        if not wanted:
            return {}
        well_query = well_query.where(Well.id.in_(wanted))
    all_well_ids = [row_id for (row_id,) in db.execute(well_query)]

    # The verdict is OUTER-joined: a line with no CoverageResult must still be
    # returned, because "not evaluated" contributes a shortage date (see the
    # module docstring). An inner join would silently drop exactly the lines whose
    # absence is the thing worth surfacing.
    # The status filter joins WELLS: demand status lives on `Well.demand_status`
    # (see `app.models.well.Well`), so a well is in or out of scope as a whole. The
    # join is INNER -- every demand line has a well, by a NOT NULL foreign key --
    # so it removes no row that the filter itself would have kept.
    line_query = (
        select(DemandLine.well_id, DemandLine.ros_date, CoverageResult.status)
        .join(Well, Well.id == DemandLine.well_id)
        .outerjoin(CoverageResult, CoverageResult.demand_line_id == DemandLine.id)
        .where(
            Well.demand_status.in_(sorted(status_filter, key=lambda s: s.value)),
            DemandLine.profile.in_(sorted(profile_filter, key=lambda p: p.value)),
        )
    )
    if well_ids is not None:
        line_query = line_query.where(DemandLine.well_id.in_(all_well_ids))

    earliest: dict[str, datetime] = {}
    shortage: dict[str, datetime] = {}
    counts: dict[str, int] = {}
    for well_id, ros_date, status in db.execute(line_query):
        counts[well_id] = counts.get(well_id, 0) + 1
        if well_id not in earliest or ros_date < earliest[well_id]:
            earliest[well_id] = ros_date
        # `status` arrives as the enum (or None from the outer join). Compare on
        # the VALUE so a plain string from a future raw-SQL caller behaves the
        # same, and so None -- an unevaluated line -- falls through to counting as
        # unsatisfied rather than needing its own branch.
        status_value = getattr(status, "value", status)
        if status_value in SATISFIED_STATUSES:
            continue
        if well_id not in shortage or ros_date < shortage[well_id]:
            shortage[well_id] = ros_date

    return {
        well_id: WellDates(
            well_id=well_id,
            earliest_ros_date=earliest.get(well_id),
            first_runout_date=shortage.get(well_id),
            in_scope_line_count=counts.get(well_id, 0),
        )
        for well_id in all_well_ids
    }


def sort_by_earliest_ros(rows: list, key=lambda row: row) -> list:
    """Sort `rows` by earliest ROS ascending -- THE server-side well ordering.

    Lives here, and is used by every consumer, so the Home Dashboard's uncovered
    list, the coverage grid and anything added later agree on the order rather
    than each sorting to its own taste. The product owner asked for wells "in
    order of the earliest date for the well"; if two screens disagree about that
    order, at most one of them is showing it.

    A well with NO earliest ROS (no in-scope demand at all) sorts LAST rather than
    first. None is not "the beginning of time" -- such a well has no date, so it
    cannot be urgent, and sorting it to the top would put the least informative
    rows above real ones. Ties break on the well NAME so the order is total and
    the same on every call; two wells genuinely due the same day would otherwise
    swap places between requests for no reason a user could explain.

    `key` extracts the object carrying `earliest_ros_date` and `well_name` from
    each row, for callers whose rows are tuples or wrappers.
    """

    def sort_key(row):
        target = key(row)
        ros = getattr(target, "earliest_ros_date", None)
        name = getattr(target, "well_name", None) or getattr(target, "name", "") or ""
        return (ros is None, ros or datetime.max, name)

    return sorted(rows, key=sort_key)
