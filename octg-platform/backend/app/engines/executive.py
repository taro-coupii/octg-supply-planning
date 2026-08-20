"""Executive engine (spec §Executive エンジン/API). Composes existing engines
(coverage's judge_customer, mrp.mrp_rows, allocation.allocate_lines_with_residual,
surplus.surplus_rows) into 5 read-only dashboard blocks. Output here is always
NATIVE (qty_by_unit / per-product rows) — MT headline conversion is deliberately
NOT done here (spec E-1: "換算は表示層（API応答組立）のみ"); the API layer
(app/api/dashboard.py) computes MT from these native rows plus Product.weight_kg.

Scope-override note (spec E-2, 全5ブロック対応): every block takes explicit
statuses/profiles and is fully scope-overridable, in-memory / non-persisting.
supply_risk and inventory_utilisation thread their statuses/profiles into
mrp.mrp_rows / surplus.surplus_rows, which both accept an optional scope
override (default None reproduces the stored-scope behaviour exactly, so
every other caller of those two engines is unaffected).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.engines.allocation import InventoryScopeMissing, allocate_lines_with_residual
from app.engines.coverage import judge_customer, scoped_lines
from app.engines.mrp import mrp_rows
from app.engines.surplus import surplus_rows
from app.models import Customer, CoverageResult, DemandLine
from app.services.dates import today

DEMAND_TREND_PAST_MONTHS = 6
DEMAND_TREND_FUTURE_MONTHS = 12
DEFAULT_SUPPLY_RISK_HORIZON = 12


def _month_key(d) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_seq_from(start_year: int, start_month: int, count: int) -> list[str]:
    keys = []
    y, m = start_year, start_month
    for i in range(count):
        mm = m + i
        yy = y + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        keys.append(f"{yy:04d}-{mm:02d}")
    return keys


def _created_at_ok(line: DemandLine, as_of) -> bool:
    """Invariant 5: DemandLine.created_at > as_of is excluded (trend basis-date
    integrity) — a line created after the reporting date must not appear in
    the trend, even for backdated future-dated demand."""
    created = line.created_at
    created_date = created.date() if hasattr(created, "date") else created
    return created_date <= as_of


# --- 1. demand_trend --------------------------------------------------------


@dataclass
class DemandTrendMonth:
    month: str
    rows: list[dict]  # {"product_id": str, "unit": UnitOfMeasure, "qty": float}


def demand_trend_rows(db, statuses: set[str], profiles: set[str], as_of=None) -> list[DemandTrendMonth]:
    as_of = as_of or today()
    start_m = as_of.month - DEMAND_TREND_PAST_MONTHS
    start_y = as_of.year + (start_m - 1) // 12
    start_m = (start_m - 1) % 12 + 1
    total_months = DEMAND_TREND_PAST_MONTHS + DEMAND_TREND_FUTURE_MONTHS + 1
    month_keys = _month_seq_from(start_y, start_m, total_months)
    month_index = {k: i for i, k in enumerate(month_keys)}

    acc: list[dict] = [dict() for _ in month_keys]

    for cust in db.query(Customer).all():
        for line in scoped_lines(db, cust.id, statuses, profiles):
            if not _created_at_ok(line, as_of):
                continue
            idx = month_index.get(_month_key(line.ros_date))
            if idx is None:
                continue
            key = (line.product_id, line.unit)
            acc[idx][key] = acc[idx].get(key, 0.0) + line.quantity

    return [
        DemandTrendMonth(
            month=key,
            rows=[{"product_id": pid, "unit": unit, "qty": qty} for (pid, unit), qty in acc[i].items()],
        )
        for i, key in enumerate(month_keys)
    ]


# --- 2. coverage -------------------------------------------------------------


@dataclass
class CoverageBlockRow:
    verdict: str
    product_id: str
    unit: object
    qty: float
    count: int


def coverage_rows(
    db, statuses: set[str], profiles: set[str], use_stored: bool
) -> tuple[list[CoverageBlockRow] | None, str | None]:
    """use_stored=True (default scope): fast read of persisted CoverageResult
    rows. use_stored=False (scope override, E-2): in-memory judge_customer per
    customer — never writes. Per-customer isolation mirrors recompute_all."""
    acc: dict[tuple[str, str, object], list] = {}

    if use_stored:
        results = db.query(CoverageResult).all()
        if not results:
            return None, "No coverage recompute has run yet"
        line_ids = [r.demand_line_id for r in results]
        lines_by_id = {
            line.id: line
            for line in db.query(DemandLine).filter(DemandLine.id.in_(line_ids)).all()
        }
        for r in results:
            line = lines_by_id.get(r.demand_line_id)
            if line is None:
                continue
            key = (r.verdict.value, line.product_id, line.unit)
            entry = acc.setdefault(key, [0.0, 0])
            entry[0] += line.quantity
            entry[1] += 1
    else:
        for cust in db.query(Customer).all():
            try:
                judged = judge_customer(db, cust, statuses, profiles)
            except InventoryScopeMissing:
                continue
            for j in judged:
                key = (j.verdict.value, j.line.product_id, j.line.unit)
                entry = acc.setdefault(key, [0.0, 0])
                entry[0] += j.line.quantity
                entry[1] += 1

    rows = [
        CoverageBlockRow(verdict=v, product_id=p, unit=u, qty=q, count=c)
        for (v, p, u), (q, c) in acc.items()
    ]
    return rows, None


# --- 3. supply_risk ----------------------------------------------------------


@dataclass
class SupplyRiskRow:
    product_id: str
    bu_id: str
    unit: object
    runout_month: str
    opening_total: float


def supply_risk_rows(
    db, statuses: set[str], profiles: set[str], horizon: int | None = None, as_of=None
) -> list[SupplyRiskRow]:
    """First-runout-month view over mrp_rows' baseline series, ascending."""
    ledgers = mrp_rows(
        db, horizon=horizon or DEFAULT_SUPPLY_RISK_HORIZON, as_of=as_of, statuses=statuses, profiles=profiles
    )
    rows = [
        SupplyRiskRow(
            product_id=l.product_id,
            bu_id=l.bu_id,
            unit=l.unit,
            runout_month=l.runout_months.get("baseline"),
            opening_total=l.opening.total,
        )
        for l in ledgers
        if l.runout_months.get("baseline") is not None
    ]
    rows.sort(key=lambda r: r.runout_month)
    return rows


# --- 4. soft_allocation -------------------------------------------------------


@dataclass
class SoftAllocationRow:
    customer_id: str
    customer_name: str
    product_id: str
    unit: object
    from_free_qty: float


def soft_allocation_rows(db, statuses: set[str], profiles: set[str]) -> list[SoftAllocationRow]:
    """Per-customer from_free consumption (SOFT/HYBRID free-stock dependency).
    Isolated per customer (InventoryScopeMissing is skipped, not raised)."""
    rows: list[SoftAllocationRow] = []
    for cust in db.query(Customer).all():
        lines = scoped_lines(db, cust.id, statuses, profiles)
        try:
            alloc_result = allocate_lines_with_residual(db, cust, lines)
        except InventoryScopeMissing:
            continue
        acc: dict[tuple[str, object], float] = {}
        for a in alloc_result.allocations:
            if a.from_free <= 0:
                continue
            key = (a.line.product_id, a.line.unit)
            acc[key] = acc.get(key, 0.0) + a.from_free
        for (product_id, unit), qty in acc.items():
            rows.append(
                SoftAllocationRow(
                    customer_id=cust.id, customer_name=cust.name, product_id=product_id, unit=unit, from_free_qty=qty
                )
            )
    return rows


# --- 5. inventory_utilisation -------------------------------------------------


@dataclass
class InventoryUtilRow:
    product_id: str
    bu_id: str
    unit: object
    on_hand: float
    allocated: float
    surplus: float
    obsolete: float


def inventory_utilisation_rows(db, statuses: set[str], profiles: set[str]) -> list[InventoryUtilRow]:
    """36-month surplus engine summary, product rows sorted by not-tied
    (surplus + obsolete) descending for the collapsed detail view."""
    rows = [
        InventoryUtilRow(
            product_id=r.product_id,
            bu_id=r.bu_id,
            unit=r.unit,
            on_hand=r.on_hand,
            allocated=r.allocated,
            surplus=r.surplus,
            obsolete=r.obsolete,
        )
        for r in surplus_rows(db, statuses=statuses, profiles=profiles)
    ]
    rows.sort(key=lambda r: (r.surplus + r.obsolete), reverse=True)
    return rows
