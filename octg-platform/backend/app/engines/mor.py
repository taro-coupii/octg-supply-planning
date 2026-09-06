"""Material Order Requirements -- the monthly order grid.

Mirrors the customer workbook's "Material Order Req" tab, which is the sheet
procurement actually orders from: one row per product, demand bucketed by ROS
month, netted against today's inventory position, and the residual shown BOTH
in the month the steel is needed (`order_requirement`) and in the month the
order must leave for the mill (`order_by_month` = ROS month minus the
product's total attribute lead time).

NETTING, PRECISELY
------------------
Per product, with months m1..mN from today:

    opening      = on_hand + customer_owned(if known)
    arrivals(m)  = on-order quantity promised inside month m (undated PO
                   quantity is NEVER netted -- it has no month to land in, and
                   is reported beside the grid instead)
    short(m)     = max(0, cumulative_demand(m) - opening - cumulative_arrivals(m))
    order_requirement(m) = short(m) - short(m-1)

The incremental form makes the row SUM to the workbook's `Ttl. Order RQMT`
(total demand minus everything already secured, floored at 0) while placing
each shortfall in the first month it actually bites.

Customer-owned stock is included in `opening` for the same reason the MRP
runout opens with it (`app.engines.mrp.RunoutPoint`): it genuinely absorbs
that customer's demand before any company steel is drawn, so excluding it
would order steel the yard will never ship. It still is not ours -- the
position block reports it separately.

WHAT THIS ENGINE REFUSES TO DO
------------------------------
* A product with NO on-hand row anywhere is returned as an UNAVAILABLE row
  (reason attached), never as a row netted from a fabricated 0 -- and never
  dropped, which would silently shorten the order list. One unknown product
  must not kill the whole grid either (the C-07 lesson).
* A product whose lead time is not fully modelled gets
  `order_by_month = None` with the model's own note: an order-by date from a
  partial lead time is a confident wrong deadline.
* Demand quantities are never summed across products: every figure on a row
  is in that row's own `unit_of_measure`.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.engines.inventory import InventoryRowMissing, on_order_rows
from app.engines.lead_time import resolve_lead_time
from app.engines.mrp import _included_lines, _inventory_position, InventoryPosition
from app.models import (
    BusinessUnit,
    Customer,
    DemandLine,
    Product,
    SafetyStock,
    UnitOfMeasure,
)

#: Default horizon, matching the workbook's 18-month view.
DEFAULT_HORIZON_MONTHS = 18


def _month_start(value: date | datetime) -> date:
    if isinstance(value, datetime):
        value = value.date()
    return value.replace(day=1)


def _add_months(month: date, count: int) -> date:
    total = month.month - 1 + count
    return date(month.year + total // 12, total % 12 + 1, 1)


def _months_axis(today: date, horizon: int) -> list[date]:
    first = _month_start(today)
    return [_add_months(first, i) for i in range(horizon)]


@dataclass(frozen=True)
class MorCell:
    """One (product, month) cell. All quantities in `unit_of_measure` -- the
    unit is repeated per cell (matching the row's) so every quantity-bearing
    payload states its unit, per the platform-wide schema contract."""

    unit_of_measure: UnitOfMeasure
    month: date
    demand: float
    arrivals: float
    #: Projected balance at month END, after arrivals and demand, before any
    #: new order. Negative = cumulative unmet demand to date.
    projected_balance: float
    #: The shortfall that FIRST bites in this month -- the quantity to order
    #: for this month's demand. Sums across the row to `total_order_requirement`.
    order_requirement: float
    #: Month the order covering `order_requirement` must be placed (ex-mill):
    #: this month minus the product's total lead time. None when the lead time
    #: is not modelled. A month before today means the order is ALREADY LATE.
    order_by_month: date | None = None


@dataclass(frozen=True)
class MorRow:
    #: The planner-set safety stock (product's unit). None = not set, which is
    #: a real state distinct from an explicit 0. When set, the order
    #: requirement triggers as the balance dips BELOW this level.
    safety_stock: float | None
    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure
    available: bool
    position: InventoryPosition | None = None
    lead_time_months: float | None = None
    lead_time_modelled: bool = False
    lead_time_note: str = ""
    total_demand: float = 0.0
    #: Overdue portion of `total_demand`: demand whose ROS month has already
    #: passed. It is folded into the first month of the grid (lateness does
    #: not cancel demand) and labelled here as its own bucket per the
    #: 2026-08-12 product-owner decision -- never silently blended.
    total_overdue_demand: float = 0.0
    total_order_requirement: float = 0.0
    #: True exactly when something must be ordered inside the horizon -- the
    #: workbook's Order Flag.
    order_flag: bool = False
    #: Earliest order_by_month across cells with a requirement; the sort key
    #: that puts the most urgent rows first. None when nothing is required or
    #: the lead time is unmodelled.
    first_order_by: date | None = None
    #: True when `first_order_by` is before the current month: the mill order
    #: should already have been placed.
    already_late: bool = False
    cells: tuple[MorCell, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class MorGrid:
    months: tuple[date, ...]
    horizon_months: int
    generated_for_customer_id: str | None
    rows: tuple[MorRow, ...] = ()
    #: Products whose demand exists but whose position is unknown -- present in
    #: `rows` as unavailable entries; counted here so the header can say so.
    unavailable_count: int = 0
    notes: tuple[str, ...] = ()


def _arrivals_by_month(
    db: Session, product_id: str, business_unit_id: str | None = None
) -> dict[date, float]:
    """Promised on-order arrivals per month. Undated quantity is deliberately
    absent -- it has no month to land in; the position block reports it
    (`InventoryPosition.on_order_undated`) beside the grid instead.

    `business_unit_id` scopes to one BU (the customer-filtered grid);
    None sums every BU (the system-wide grid, same stance as MRP)."""
    if business_unit_id is not None:
        bu_ids = [business_unit_id]
    else:
        bu_ids = [bu.id for bu in db.query(BusinessUnit).all()]
    per_month: dict[date, float] = {}
    for bu_id in bu_ids:
        for row in on_order_rows(db, bu_id, {product_id}):
            if row.expected_arrival_date is None:
                continue
            key = _month_start(row.expected_arrival_date)
            per_month[key] = per_month.get(key, 0.0) + max(
                0.0, row.quantity or 0.0
            )
    return per_month


def _row_for(
    db: Session,
    product: Product,
    lines: list[DemandLine],
    months: list[date],
    today_month: date,
    business_unit_id: str | None = None,
    customer_id: str | None = None,
) -> MorRow:
    demand_by_month: dict[date, float] = {}
    overdue_demand = 0.0
    for line in lines:
        key = _month_start(line.ros_date)
        # Demand with ROS before the horizon start is not deleted by the axis:
        # it lands in the first month, where its shortfall (if any) bites
        # immediately -- the same "late demand is still demand" rule the
        # incoming-supply block applies to overdue POs. It is COUNTED and
        # LABELLED (total_overdue_demand), per the 2026-08-12 decision.
        if key < months[0]:
            key = months[0]
            overdue_demand += line.quantity
        if key > months[-1]:
            continue  # beyond the horizon: out of this grid's question
        demand_by_month[key] = demand_by_month.get(key, 0.0) + line.quantity

    try:
        position = _inventory_position(
            db,
            product,
            business_unit_id=business_unit_id,
            customer_id=customer_id,
        )
    except InventoryRowMissing as exc:
        return MorRow(
            safety_stock=None,
            product_id=product.id,
            product_description=product.description,
            unit_of_measure=product.unit_of_measure,
            available=False,
            total_demand=sum(demand_by_month.values()),
            total_overdue_demand=overdue_demand,
            reason=str(exc),
        )

    breakdown = resolve_lead_time(db, product)
    lead_months = breakdown.total_months if breakdown.modelled else None
    # Whole months for the calendar offset: 4.5 months of lead time means the
    # order must leave 5 calendar months ahead, not 4 -- rounding down would
    # manufacture half a month of slack that does not exist.
    lead_offset = math.ceil(breakdown.total_months) if breakdown.modelled else None

    arrivals = _arrivals_by_month(db, product.id, business_unit_id)

    # Safety stock raises the bar: an order is required as soon as the
    # projected balance would dip BELOW the safety level, not only at zero.
    # No row means no safety stock (a real state), contributing nothing.
    safety_row = (
        db.query(SafetyStock).filter(SafetyStock.product_id == product.id).first()
    )
    safety = max(0.0, safety_row.quantity) if safety_row is not None else 0.0

    opening = position.on_hand + (position.customer_owned or 0.0)
    cum_demand = 0.0
    cum_arrivals = 0.0
    prev_short = 0.0
    cells: list[MorCell] = []
    first_order_by: date | None = None
    for month in months:
        month_demand = demand_by_month.get(month, 0.0)
        month_arrivals = arrivals.get(month, 0.0)
        cum_demand += month_demand
        cum_arrivals += month_arrivals
        short = max(0.0, cum_demand + safety - opening - cum_arrivals)
        requirement = short - prev_short
        prev_short = short
        order_by = (
            _add_months(month, -lead_offset) if lead_offset is not None else None
        )
        if requirement > 0 and order_by is not None:
            if first_order_by is None or order_by < first_order_by:
                first_order_by = order_by
        cells.append(
            MorCell(
                unit_of_measure=product.unit_of_measure,
                month=month,
                demand=month_demand,
                arrivals=month_arrivals,
                projected_balance=opening + cum_arrivals - cum_demand,
                order_requirement=requirement,
                order_by_month=order_by if requirement > 0 else None,
            )
        )

    total_requirement = prev_short
    return MorRow(
        safety_stock=safety if safety_row is not None else None,
        product_id=product.id,
        product_description=product.description,
        unit_of_measure=product.unit_of_measure,
        available=True,
        position=position,
        lead_time_months=lead_months,
        lead_time_modelled=breakdown.modelled,
        lead_time_note=breakdown.note,
        total_demand=cum_demand,
        total_overdue_demand=overdue_demand,
        total_order_requirement=total_requirement,
        order_flag=total_requirement > 0,
        first_order_by=first_order_by,
        already_late=(
            first_order_by is not None and first_order_by < today_month
        ),
        cells=tuple(cells),
        reason=None,
    )


def order_requirements(
    db: Session,
    customer_id: str | None = None,
    business_unit_id: str | None = None,
    horizon_months: int = DEFAULT_HORIZON_MONTHS,
    today: date | None = None,
) -> MorGrid:
    """The grid: one row per product with in-scope demand inside the horizon.

    Scope and filters are exactly `app.engines.mrp._included_lines` -- the
    demand coverage evaluates, optionally narrowed to one customer -- so this
    grid can never disagree with the MRP summary beside it about WHAT counts
    as demand.
    """
    today = today or date.today()
    today_month = _month_start(today)
    months = _months_axis(today, horizon_months)

    # A customer-filtered grid nets against THAT customer's Business Unit and
    # THAT customer's owned steel only. The unfiltered grid keeps the
    # system-wide procurement stance it shares with MRP (see
    # `mrp.InventoryPosition` for why the all-BU sum is right there and
    # nowhere else) -- and says so in the notes.
    if customer_id is not None:
        customer = db.get(Customer, customer_id)
        business_unit_id = (
            customer.business_unit_id if customer is not None else None
        )

    lines_by_product: dict[str, list[DemandLine]] = {}
    for line in _included_lines(db, customer_id, business_unit_id):
        lines_by_product.setdefault(line.product_id, []).append(line)

    rows: list[MorRow] = []
    for product_id, lines in lines_by_product.items():
        product = db.get(Product, product_id)
        if product is None:
            # A demand line pointing at a deleted product must not 500 the
            # grid -- same rule as an unknown position.
            rows.append(MorRow(
                safety_stock=None,
                product_id=product_id,
                product_description=None,
                unit_of_measure=UnitOfMeasure.MTR,
                available=False,
                total_demand=sum(l.quantity for l in lines),
                total_overdue_demand=sum(
                    l.quantity
                    for l in lines
                    if _month_start(l.ros_date) < today_month
                ),
                reason=(
                    f"Product {product_id} no longer exists; its demand "
                    "cannot be netted."
                ),
            ))
            continue
        if customer_id is not None and business_unit_id is None:
            rows.append(MorRow(
                safety_stock=None,
                product_id=product.id,
                product_description=product.description,
                unit_of_measure=product.unit_of_measure,
                available=False,
                total_demand=sum(l.quantity for l in lines),
                total_overdue_demand=sum(
                    l.quantity
                    for l in lines
                    if _month_start(l.ros_date) < today_month
                ),
                reason=(
                    "This customer is not mapped to a Business Unit, so it "
                    "has no inventory pool to net against. Map the customer "
                    "in Administration and retry."
                ),
            ))
            continue
        row = _row_for(
            db,
            product,
            lines,
            months,
            today_month,
            business_unit_id=business_unit_id,
            customer_id=customer_id,
        )
        if row.total_demand > 0 or not row.available:
            rows.append(row)

    # Most urgent first: already-late orders, then earliest order-by, then the
    # unavailable rows (they need a data fix before they can need an order),
    # then everything quiet. Name ties broken for a stable order.
    def sort_key(row: MorRow):
        if not row.available:
            return (2, date.max, row.product_description or "")
        if row.first_order_by is not None:
            return (0 if row.already_late else 1, row.first_order_by, row.product_description or "")
        return (3, date.max, row.product_description or "")

    rows.sort(key=sort_key)

    unavailable = sum(1 for r in rows if not r.available)
    notes = [
        "Order requirements are netted from company stock PLUS customer-owned "
        "stock (it absorbs that customer's demand first), then from purchase "
        "orders in the month they are promised. Undated purchase-order "
        "quantity is never netted -- it has no month to land in.",
        (
            "Filtered to one customer: positions and arrivals are scoped to "
            "that customer's Business Unit, and only that customer's owned "
            "steel is counted."
            if customer_id is not None
            else "All customers: positions are summed across every Business "
            "Unit -- the system-wide procurement view, same as MRP. This is "
            "an ordering total, not a coverage figure."
        ),
        "Overdue demand (ROS month already passed) still counts: it is folded "
        "into the first month of the grid and reported per row as "
        "total_overdue_demand.",
        "Products with a safety stock set trigger an order requirement as soon "
        "as the projected balance would dip below that level, not only at "
        "zero.",
        "The order-by month subtracts the product's total attribute lead time "
        "from the month of need. Products without a fully modelled lead time "
        "show requirements but no order-by date.",
    ]
    if unavailable:
        # Scope-aware: on a filtered grid the row may exist in ANOTHER BU --
        # claiming "no row in any Business Unit" would assert something this
        # request never checked.
        notes.append(
            f"{unavailable} row(s) could not be netted -- see each row's own "
            "reason (unknown inventory position"
            + (
                " in this customer's Business Unit"
                if customer_id is not None
                else " in any Business Unit"
            )
            + ", or an unresolvable product/customer). They are listed "
            "without figures rather than netted from a fabricated 0."
        )
    return MorGrid(
        months=tuple(months),
        horizon_months=horizon_months,
        generated_for_customer_id=customer_id,
        rows=tuple(rows),
        unavailable_count=unavailable,
        notes=tuple(notes),
    )
