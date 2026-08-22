"""Surplus List -- OH decomposed into Allocated / Surplus / Obsolete.

Mirrors the customer workbook's "Surplus List" tab, QUANTITY-ONLY for now
(the workbook also values the surplus in money; that needs a price master and
inventory ages the platform does not hold yet, and is deliberately out of
scope -- see HANDOFF).

THE DECOMPOSITION
-----------------
Per (Business Unit, product), over a LONG horizon (36 months -- long enough
that "no demand inside it" genuinely means idle, not merely far out):

    allocated = tied      (the utilisation engine's netting: demand in the
                           window, minus the customer-owned stock that absorbs
                           it first, capped at on-hand)
    leftover  = on_hand - allocated
    obsolete  = leftover  when the (BU, product) has NO in-scope demand at all
                          inside the horizon
    surplus   = leftover  otherwise (the product is alive -- something wants
                           it -- there is simply more steel than demand)

    allocated + surplus + obsolete == on_hand   EXACTLY, per row.

The tied/not-tied split is COMPUTED BY `app.engines.executive
.inventory_utilisation` -- the same single implementation the Executive
Dashboard renders -- called here with the longer horizon. This module adds
only the surplus-vs-obsolete classification on top; a second netting
implementation would drift from the dashboard's.

"Obsolete" here is a DEMAND-BASED statement (nothing needs this steel in this
BU for three years), not a metallurgical one -- the platform holds no ageing
or condition data. The note says so on every payload.

Same refusals as everywhere else: a demanded product with no on-hand row
anywhere is surfaced in `unknown_position` (from the utilisation pass), never
counted as zero stock.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.engines.executive import (
    Measure,
    inventory_utilisation,
    pairs_to_tonnes,
)
from app.models import BusinessUnit, Product, UnitOfMeasure

#: Long enough that "no demand inside it" means idle rather than merely far
#: out; matches the platform's longest Executive horizon.
SURPLUS_HORIZON_MONTHS = 36

SURPLUS_NOTE = (
    "Quantity-based decomposition. 'Allocated' is on-hand tied to in-scope "
    "demand inside the horizon (customer-owned stock absorbs its owner's "
    "demand first, exactly as the Executive Dashboard's utilisation block "
    "nets). 'Obsolete' means no in-scope demand for this product in this "
    "Business Unit within the horizon -- a demand statement, not a condition "
    "statement; the platform holds no ageing or valuation data yet. Overdue "
    "demand (ROS month already passed -- month granularity, the same rule the "
    "Order Requirements grid applies) still counts as demand and is reported "
    "separately in demand_overdue."
)


@dataclass(frozen=True)
class SurplusRow:
    """One (Business Unit, product). allocated + surplus + obsolete == on_hand."""

    business_unit_id: str
    business_unit_name: str | None
    product_id: str
    product_description: str | None
    unit_of_measure: UnitOfMeasure
    on_hand: float
    allocated: float
    surplus: float
    obsolete: float
    demand_in_window: float
    #: Overdue portion of `demand_in_window` (ROS month already passed). Counted
    #: into the tie -- lateness does not cancel demand -- but labelled as its
    #: own bucket per the 2026-08-12 product-owner decision.
    demand_overdue: float
    customer_owned: float


@dataclass(frozen=True)
class SurplusReport:
    horizon_months: int
    generated_at: datetime
    rows: tuple[SurplusRow, ...] = ()
    #: MT headlines across all rows (display-layer conversion, C-05; floor
    #: semantics -- see pairs_to_tonnes).
    allocated_tonnes: Measure = Measure(available=False, reason="Not computed.")
    surplus_tonnes: Measure = Measure(available=False, reason="Not computed.")
    obsolete_tonnes: Measure = Measure(available=False, reason="Not computed.")
    #: Products with in-scope demand but NO on-hand row anywhere: their
    #: position is unknown, not zero, and they cannot appear in `rows`.
    unknown_position_count: int = 0
    note: str = SURPLUS_NOTE


def surplus_report(
    db: Session,
    business_unit_id: str | None = None,
    now: datetime | None = None,
) -> SurplusReport:
    now = now or datetime.utcnow()
    utilisation = inventory_utilisation(
        db,
        business_unit_id=business_unit_id,
        horizon_months=SURPLUS_HORIZON_MONTHS,
        now=now,
    )

    bu_names = {bu.id: bu.name for bu in db.query(BusinessUnit).all()}
    products = {p.id: p for p in db.query(Product).all()}

    rows: list[SurplusRow] = []
    for item in utilisation.products:
        leftover = item.not_tied
        no_demand = item.demand_in_window <= 0
        rows.append(
            SurplusRow(
                business_unit_id=item.business_unit_id,
                business_unit_name=bu_names.get(item.business_unit_id),
                product_id=item.product_id,
                product_description=item.product_description,
                unit_of_measure=item.unit_of_measure,
                on_hand=item.on_hand_quantity,
                allocated=item.tied,
                surplus=0.0 if no_demand else leftover,
                obsolete=leftover if no_demand else 0.0,
                demand_in_window=item.demand_in_window,
                demand_overdue=item.demand_overdue,
                customer_owned=item.customer_owned_quantity,
            )
        )

    # Idle steel first -- the whole point of the screen -- biggest idle
    # quantity per unit is not comparable across units, so the order is
    # (has idle at all, obsolete before surplus, then product name) rather
    # than a cross-unit quantity sort that would silently add Mtr to PC.
    rows.sort(
        key=lambda r: (
            0 if r.obsolete > 0 else 1 if r.surplus > 0 else 2,
            r.product_description or "",
            r.business_unit_name or "",
        )
    )

    def tonnes(field: str) -> Measure:
        total_quantity = sum(getattr(r, field) for r in rows)
        if total_quantity <= 0:
            # A real, measured zero -- nothing sits in this bucket.
            return Measure(available=True, value=0.0)
        return pairs_to_tonnes(
            ((products[r.product_id], getattr(r, field)) for r in rows),
            field,
        )

    return SurplusReport(
        horizon_months=SURPLUS_HORIZON_MONTHS,
        generated_at=now,
        rows=tuple(rows),
        allocated_tonnes=tonnes("allocated"),
        surplus_tonnes=tonnes("surplus"),
        obsolete_tonnes=tonnes("obsolete"),
        unknown_position_count=len(utilisation.unknown_position),
    )
