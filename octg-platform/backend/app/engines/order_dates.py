"""Lead-time date arithmetic shared by the coverage engine and the MRP engine.

This module is deliberately tiny and dependency-light: `app.engines.coverage`
imports `is_recoverable` from here to decide UNRECOVERABLE, while
`app.engines.mrp` imports `order_feasibility` (and re-exports it) to build its
recommendations. Keeping it separate is what breaks the would-be import cycle
coverage -> mrp -> coverage.

Date derivation (documented once, used everywhere):

  transit_months      = the Logistics/Shipping term of the SAME breakdown the
                        total comes from (`LeadTimeBreakdown.transit_months`).
                        That is the ocean/inland leg between the mill releasing
                        the material and it standing at the wellsite, so it is
                        the shipping allowance the spec asks us to strip off the
                        ROS date.
  required_ship_date  = ros_date - transit_months
  lead_time_months    = app.engines.lead_time.resolve_lead_time(...).total_months
                        (attribute-based: OD/WT + Grade + Connection + Logistics,
                        never a stored per-SKU value). This ALREADY INCLUDES the
                        transit leg.
  recommended_order_date = ros_date - lead_time_months

ONE resolver, two views
-----------------------
`transit_months` no longer runs its own query. Both figures on any given call now
come from a single `resolve_lead_time` call, because a transit calculation that
drifted from the total calculation would desynchronise `required_ship_date` from
`recommended_order_date` -- two published dates that contradict each other, which
is precisely the class of bug the sailing double-count below was. The public
`transit_months(db, product)` helper survives for callers that want only that
figure, and it is a one-line view of the same resolver.

The sailing double-count is gone
--------------------------------
This module previously computed `recommended_order_date = required_ship_date -
lead_time_months`, i.e. `ros - transit - total`, subtracting the sailing leg
twice: once to fix the ship date and again inside the total. It was described as
deliberate conservatism, but it was never confirmed with planners and it
double-subtracts a real, physical quantity, which made the two published dates
mutually inconsistent (`required_ship_date` implied an arrival exactly at ROS,
while `recommended_order_date` implied one `transit` months early).

It is now removed. `recommended_order_date = ros - total_lead_time` is
internally consistent with `required_ship_date = ros - transit` -- order on that
date, spend `total - transit` months at the mill, sail for `transit` months,
arrive at ROS -- and it matches the spec's plain "Total Lead Time 8 months"
worked example. There is deliberately NO hidden safety buffer; if planners later
want one it must be introduced as an explicit named constant here, applied only
to `recommended_order_date`, and it must never feed `is_recoverable` (see below).

Recoverability is a physical fact, not planning advice
-----------------------------------------------------
`is_recoverable` answers exactly one question -- can steel ordered TODAY
physically arrive by the ROS date? -- and it answers it from the physics only:

    today + total_lead_time_months <= ros_date

It does not consult `recommended_order_date` and must never be re-expressed in
terms of it. `recommended_order_date` is advice a planner may reasonably ignore;
UNRECOVERABLE is a terminal factual claim ("ROS cannot be met even if ordered
today") that also drives MRP's escalate instruction. Deriving the second from
the first is what let a conservative recommendation buffer manufacture false
UNRECOVERABLE verdicts for demand that was still perfectly orderable.

Products whose lead time is NOT MODELLED are an explicit, distinct case: total
lead time is 0, which we treat as "lead time unknown" rather than "available
instantly". `order_feasibility` still returns dates, but `is_recoverable` reports
True unconditionally so an unmodelled product can never silently mark every
uncovered line UNRECOVERABLE.

"Not modelled" covers TWO situations, and deliberately treats them alike: no
matching components at all, and an INCOMPLETE set (say a Grade row but no OD/WT
row). `app.engines.lead_time` reports both as `modelled=False, total_months=0`
rather than summing a partial set, because a partial sum would put a confidently
wrong date on the screen and feed a physics verdict computed from months that are
known to be missing. Read that module's docstring for the full reasoning. When the
model is incomplete, transit is 0 too, so `required_ship_date` collapses to the
ROS date -- which is consistent with `recommended_order_date` also collapsing
there, and MRP's reason text says the dates are indicative only.
"""

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.engines.lead_time import (
    LeadTimeBreakdown,
    resolve_lead_time,
    total_lead_time_months,
)
from app.models import Product

# Average calendar days per month. Lead-time components are quoted in months
# (often fractional, e.g. 4.5), so month->day conversion needs one agreed
# constant rather than a per-call guess.
DAYS_PER_MONTH = 30.44


def months_to_timedelta(months: float) -> timedelta:
    return timedelta(days=round(months * DAYS_PER_MONTH))


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def transit_months(db: Session, product: Product) -> float:
    """Shipping/transit allowance for `product` -- the Logistics term of the ONE
    lead-time resolution, not a second query and not a hardcoded constant.

    Returns 0.0 when the product's lead time is not modelled, exactly as the
    total does. See module docstring.
    """
    return resolve_lead_time(db, product).transit_months


def order_feasibility(
    db: Session,
    product: Product,
    ros_date: date | datetime,
) -> tuple[date, date, float]:
    """Return (required_ship_date, recommended_order_date, lead_time_months)
    for satisfying `ros_date` with a fresh mill order of `product`.

    Both dates are PLANNING ADVICE. Do not use them to decide recoverability --
    call `is_recoverable` for that. See module docstring.

    The two dates come from ONE `resolve_lead_time` call, so the total and the
    transit leg can never disagree. Callers that also want the component
    breakdown should use `order_feasibility_with_breakdown`.
    """
    ship_by, order_by, lead_months, _breakdown = order_feasibility_with_breakdown(
        db, product, ros_date
    )
    return ship_by, order_by, lead_months


def order_feasibility_with_breakdown(
    db: Session,
    product: Product,
    ros_date: date | datetime,
) -> tuple[date, date, float, LeadTimeBreakdown]:
    """`order_feasibility` plus the explainable breakdown behind the dates.

    Exists so MRP can publish "why is the order date this date" alongside the
    date without resolving the lead time a second time (and risking a second,
    differing answer).
    """
    ros = _as_date(ros_date)
    breakdown = resolve_lead_time(db, product)
    lead_months = breakdown.total_months
    required_ship_date = ros - months_to_timedelta(breakdown.transit_months)
    # The full lead time already contains the transit leg, so it is subtracted
    # from the ROS date, NOT from the ship date.
    recommended_order_date = ros - months_to_timedelta(lead_months)
    return required_ship_date, recommended_order_date, lead_months, breakdown


def is_recoverable(
    db: Session,
    product: Product,
    ros_date: date | datetime,
    today: date | None = None,
) -> bool:
    """True when a mill order placed today could still physically hit `ros_date`.

    Computed straight from the physics -- `today + total_lead_time <= ros` --
    with no planning buffer of any kind, and deliberately independent of
    `order_feasibility`'s recommended order date. See module docstring.

    Returns True when the product's lead time is not modelled (total == 0 --
    either no matching components or an incomplete set): absent data means "we
    cannot judge", not "hopeless".
    """
    lead_months = total_lead_time_months(db, product)
    if lead_months <= 0:
        return True
    today = today or date.today()
    return today + months_to_timedelta(lead_months) <= _as_date(ros_date)
