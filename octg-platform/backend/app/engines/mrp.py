"""MRP engine (spec §MRPエンジン, 裁定 M-0..M-3). New time-phased engine — does
NOT reuse allocation's point-in-time free_pool math; opening balances here are
raw on_hand / customer_owned totals per (bu, product), not free pools.

Rulings implemented:
- M-0: demand scope = Administration default settings (coverage_scope_statuses /
  coverage_scope_profiles), same parsing as app.engines.coverage._scope.
- M-1: month buckets are the calendar month of ROS date / PO expected_date.
  A PO with expected_date=None is never placed in a month; it is reported
  separately as `on_order_undated`.
- M-2: one independent ledger per (business_unit_id, product_id); quantities
  are never summed across units (this schema keys on_hand/on_order/owned rows
  by bu+product only, so a mixed-unit product would be a data error upstream —
  out of scope for this engine to detect).
- M-3: horizon is a month count, default 12, clamped to [1, 36]. Month 0 is the
  calendar month of `as_of` (defaults to dates.today()).

Ledger mechanics (spec pinned identity):
- Opening balance for month 0 = current on_hand (company) + current
  customer_owned total across all customers in the BU (owned), split so the
  breakdown is visible. Opening for month N>0 = closing of month N-1.
- receipts_booked = PO quantity (any booking_status) whose expected_date falls
  in that month. receipts_recommended = MOR's recommended-order quantities for
  that (bu, product) in that month, injected via `recommended_by_product`
  (engine takes it as a plain argument, keyed by (bu_id, product_id) so
  requirements from one BU never leak into another BU's ledger for a product
  shared across BUs; wiring MOR's output into MRP is the API layer's job, not
  this engine's).
- issues = sum of in-scope demand line quantities whose ros_date falls in that
  month, for wells belonging to customers in this BU.
- Owned-first consumption (invariant 7): each month, issues first reduce the
  owned balance (floored at 0), then any remainder reduces the company
  balance. The company balance is allowed to go negative (visibility of
  unmet demand) — owned is never negative since it is capped by the issues
  actually charged to it.
- closing.company + closing.owned == opening.company + opening.owned +
  receipts_booked + receipts_recommended - issues, every month (pinned).

Runout series (3, each a list of month-end balances for baseline horizon):
- baseline: no receipts at all (opening totals only, depleted by issues).
- on_order: booked receipts only (no recommended).
- with_recommended: booked + recommended receipts (the full ledger totals).
Each series' runout month is the first month whose closing total is < 0, or
None if it never goes negative within the horizon.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.engines.coverage import _scope, scoped_lines
from app.models import (
    Customer,
    CustomerOwnedInventory,
    InventoryOnHand,
    InventoryOnOrder,
)
from app.services.dates import today

MIN_HORIZON = 1
MAX_HORIZON = 36
DEFAULT_HORIZON = 12


def _clamp_horizon(horizon: int | None) -> int:
    if horizon is None:
        horizon = DEFAULT_HORIZON
    return max(MIN_HORIZON, min(MAX_HORIZON, horizon))


def _month_key(d) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_seq(as_of, horizon: int) -> list[str]:
    keys = []
    y, m = as_of.year, as_of.month
    for i in range(horizon):
        mm = m + i
        yy = y + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        keys.append(f"{yy:04d}-{mm:02d}")
    return keys


@dataclass
class Balance:
    company: float
    owned: float

    @property
    def total(self) -> float:
        return self.company + self.owned


@dataclass
class MonthLedger:
    month: str
    opening: Balance
    receipts_booked: float
    receipts_recommended: float
    issues: float
    closing: Balance


@dataclass
class ProductLedger:
    bu_id: str
    product_id: str
    unit: object
    opening: Balance
    months: list[MonthLedger]
    runout_months: dict
    on_order_undated: float


def _stock_bu_products(db) -> dict[tuple[str, str], object]:
    keys: dict[tuple[str, str], object] = {}
    for row in db.query(InventoryOnHand).all():
        keys[(row.business_unit_id, row.product_id)] = row.unit
    for row in db.query(InventoryOnOrder).all():
        keys.setdefault((row.business_unit_id, row.product_id), row.unit)
    return keys


def _owned_bu_products(db, customer_bu: dict[str, str]) -> dict[tuple[str, str], object]:
    """(bu, product) -> unit for every CustomerOwnedInventory row, keyed by
    the owning customer's BU — makes an owned-only product (zero on_hand/
    on_order) visible instead of silently dropped."""
    keys: dict[tuple[str, str], object] = {}
    for row in db.query(CustomerOwnedInventory).all():
        bu_id = customer_bu.get(row.customer_id)
        if bu_id is None:
            continue
        keys.setdefault((bu_id, row.product_id), row.unit)
    return keys


def _owned_total_for_bu(db, bu_id: str, product_id: str, unit) -> float:
    """Sum of customer_owned for ALL customers in this BU (spec M-... 期首残
    の客先材 = BU内全顧客の customer_owned 合計)."""
    customer_ids = [c.id for c in db.query(Customer).filter(Customer.business_unit_id == bu_id).all()]
    if not customer_ids:
        return 0.0
    rows = (
        db.query(CustomerOwnedInventory)
        .filter(
            CustomerOwnedInventory.customer_id.in_(customer_ids),
            CustomerOwnedInventory.product_id == product_id,
            CustomerOwnedInventory.unit == unit,
        )
        .all()
    )
    return sum(r.quantity for r in rows)


def _booked_receipts_by_month(db, bu_id: str, product_id: str, unit, month_keys: list[str]) -> tuple[dict, float]:
    rows = (
        db.query(InventoryOnOrder)
        .filter(
            InventoryOnOrder.business_unit_id == bu_id,
            InventoryOnOrder.product_id == product_id,
            InventoryOnOrder.unit == unit,
        )
        .all()
    )
    by_month = {k: 0.0 for k in month_keys}
    undated = 0.0
    for row in rows:
        if row.expected_date is None:
            undated += row.quantity
            continue
        key = _month_key(row.expected_date)
        if key in by_month:
            by_month[key] += row.quantity
        # Outside the horizon window: not placed in any bucket (not undated
        # either — it simply falls outside the requested window).
    return by_month, undated


def mrp_rows(
    db,
    horizon: int | None = None,
    as_of=None,
    recommended_by_product: dict | None = None,
    statuses=None,
    profiles=None,
) -> list[ProductLedger]:
    """`statuses`/`profiles` optionally override the stored Administration
    scope (spec E-2: Executive dashboard scope override applies to every
    block, including supply_risk). Passing neither (the default) reproduces
    the original stored-scope behaviour exactly — zero change for existing
    callers (MOR wiring, /mrp/summary, /mrp/export)."""
    horizon = _clamp_horizon(horizon)
    as_of = as_of or today()
    month_keys = _month_seq(as_of, horizon)
    recommended_by_product = recommended_by_product or {}

    default_statuses, default_profiles = _scope(db)
    statuses = statuses if statuses is not None else default_statuses
    profiles = profiles if profiles is not None else default_profiles

    all_customers = db.query(Customer).all()
    customer_bu = {c.id: c.business_unit_id for c in all_customers}

    stock_products = _stock_bu_products(db)
    owned_products = _owned_bu_products(db, customer_bu)

    # BUs to consider: stock-derived, owned-derived, and every BU that owns a
    # customer at all — a demand-only product (zero stock/owned) can appear
    # for any of those.
    bu_ids = {bu for (bu, _) in stock_products} | {bu for (bu, _) in owned_products}
    bu_ids |= {bu for bu in customer_bu.values() if bu is not None}

    # Pre-compute, per BU, all in-scope demand lines grouped by (product_id, month)
    # so we do not re-scan customers/lines once per product, and the
    # (bu, product) -> unit map for demand-only products.
    issues_by_bu: dict[str, dict[tuple[str, str], float]] = {}
    demand_products: dict[tuple[str, str], object] = {}
    for bu_id in bu_ids:
        acc: dict[tuple[str, str], float] = {}
        customers = [c for c in all_customers if c.business_unit_id == bu_id]
        for cust in customers:
            for line in scoped_lines(db, cust.id, statuses, profiles):
                key = _month_key(line.ros_date)
                demand_products.setdefault((bu_id, line.product_id), line.unit)
                if key not in month_keys:
                    continue
                acc[(line.product_id, key)] = acc.get((line.product_id, key), 0.0) + line.quantity
        issues_by_bu[bu_id] = acc

    merged_products: dict[tuple[str, str], object] = {}
    merged_products.update(stock_products)
    for key, unit in demand_products.items():
        merged_products.setdefault(key, unit)
    for key, unit in owned_products.items():
        merged_products.setdefault(key, unit)
    bu_products = [(bu_id, product_id, unit) for (bu_id, product_id), unit in merged_products.items()]

    results: list[ProductLedger] = []
    for bu_id, product_id, unit in bu_products:
        on_hand_rows = (
            db.query(InventoryOnHand)
            .filter(
                InventoryOnHand.business_unit_id == bu_id,
                InventoryOnHand.product_id == product_id,
                InventoryOnHand.unit == unit,
            )
            .all()
        )
        company0 = sum(r.quantity for r in on_hand_rows)
        owned0 = _owned_total_for_bu(db, bu_id, product_id, unit)
        opening0 = Balance(company=company0, owned=owned0)

        booked_by_month, undated = _booked_receipts_by_month(db, bu_id, product_id, unit, month_keys)
        recommended_by_month = recommended_by_product.get((bu_id, product_id), {})
        issues_for_product = issues_by_bu.get(bu_id, {})

        months: list[MonthLedger] = []
        company, owned = company0, owned0
        for key in month_keys:
            opening = Balance(company=company, owned=owned)
            receipts_booked = booked_by_month.get(key, 0.0)
            receipts_recommended = float(recommended_by_month.get(key, 0.0))
            issues = issues_for_product.get((product_id, key), 0.0)

            from_owned = min(issues, owned)
            owned = owned - from_owned
            remaining_issue = issues - from_owned
            company = company + receipts_booked + receipts_recommended - remaining_issue

            closing = Balance(company=company, owned=owned)
            months.append(
                MonthLedger(
                    month=key,
                    opening=opening,
                    receipts_booked=receipts_booked,
                    receipts_recommended=receipts_recommended,
                    issues=issues,
                    closing=closing,
                )
            )

        runout_months = {
            "baseline": _runout_month(month_keys, opening0, months, use_booked=False, use_recommended=False),
            "with_recommended": _runout_month(month_keys, opening0, months, use_booked=True, use_recommended=True),
            "on_order": _runout_month(month_keys, opening0, months, use_booked=True, use_recommended=False),
        }

        results.append(
            ProductLedger(
                bu_id=bu_id,
                product_id=product_id,
                unit=unit,
                opening=opening0,
                months=months,
                runout_months=runout_months,
                on_order_undated=undated,
            )
        )

    return results


def _runout_month(month_keys, opening0: Balance, months: list[MonthLedger], use_booked: bool, use_recommended: bool):
    """Recompute a single-total (company+owned) running balance under a given
    receipts policy, and return the first month key whose closing total < 0,
    or None. Uses each month's actual `issues` (scope-filtered) already
    computed in the main ledger pass — only the receipts side varies."""
    balance = opening0.total
    for m in months:
        receipts = 0.0
        if use_booked:
            receipts += m.receipts_booked
        if use_recommended:
            receipts += m.receipts_recommended
        balance = balance + receipts - m.issues
        if balance < 0:
            return m.month
    return None
