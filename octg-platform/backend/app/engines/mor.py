"""MOR (Material Order Requirements) engine (spec §MORエンジン, 裁定 M-4).
New time-phased engine, independent of MRP's split (owned/company) ledger —
MOR nets a single running balance per (bu, product) against booked receipts
and scope-filtered demand, and turns net deficits into order requirements.

Rulings implemented:
- Netting: month-by-month running balance = opening (current on_hand total +
  customer_owned total, per M-4 customer-filter rules below) + booked PO
  receipts (expected_date in that month) − scope-filtered demand issues.
  Recommended orders (MOR's own output) are NOT fed back into this balance —
  MOR reports what is needed, it does not assume its own advice was already
  acted on.
- Deficit / requirement algorithm (spec: 増分不足を初発月に required_qty,
  安全在庫は独立トリガーだが二重発注しない — implemented as ONE unified
  deficit series so physical and safety-stock deficits never double-count):
    minimum_level[m] = safety_stock if a SafetyStock row exists for (bu,
        product), else 0.
    deficit[m] = max(0, minimum_level[m] − balance[m])
    requirement at month m = deficit[m] − deficit[m-1] (deficit[-1] = 0),
        emitted only when that increment is > 0, with need_month = m.
  Because minimum_level is safety_stock (a constant >= 0) when a safety row
  exists, deficit-to-safety at any given balance is always >= the pure
  physical deficit (0 minimum) at that same balance, so this single series
  is the tighter of the two everywhere and no separate summation is needed —
  a physical requirement is never separately re-added on top of a safety one.
- Markers (independent of the requirement series, spec pinned distinct):
    physical_runout = first month whose balance < 0 (computed from balance[],
        entirely independent of any safety_stock row).
    safety_breach = first month whose balance < safety_stock, only if a
        SafetyStock row exists for (bu, product); may land in an EARLIER
        month than physical_runout, or never fire.
    order_deadline = ex_mill_month of the first (earliest need_month)
        requirement, or None if there are no requirements.
- ex_mill_month = need_month − lead_time_months. LeadTime resolution order
  (most specific wins): (bu_id, product_id) row > (None, product_id) row >
  (bu_id, None) row > (None, None) row > 0 months if no row matches at all.
  A requirement is `overdue` when its ex_mill_month's calendar month is
  strictly before `as_of`'s month (i.e. the order should already have been
  placed).
- M-4 customer filter: when `customer_id` is given, the ledger for that
  customer's BU only considers that BU's on_hand/on_order, and ONLY that one
  customer's customer_owned rows (never other customers' owned material, and
  never another BU's stock at all — BU is an absolute boundary, same as
  allocation's A-3). A customer with no business_unit_id yields a result with
  rows=[] and `unavailable_reason` set instead of raising, so the API layer
  can render an explanation.
- This engine never writes CoverageResult and never reads/affects it — safety
  stock and MOR requirements are wholly outside coverage's verdict logic
  (invariant 3 regression: coverage results before/after calling this engine
  must be identical).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.engines.coverage import _scope, scoped_lines
from app.models import (
    Customer,
    CustomerOwnedInventory,
    InventoryOnHand,
    InventoryOnOrder,
    LeadTime,
    SafetyStock,
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


def _shift_month(key: str, delta_months: int) -> str:
    y, m = int(key[:4]), int(key[5:7])
    total = y * 12 + (m - 1) - delta_months
    yy, mm = divmod(total, 12)
    return f"{yy:04d}-{mm + 1:02d}"


@dataclass
class Requirement:
    need_month: str
    qty: float
    unit: object
    ex_mill_month: str
    overdue: bool


@dataclass
class MorRow:
    bu_id: str
    product_id: str
    unit: object
    opening_balance: float
    strip: list  # list of {"month": str, "closing_balance": float}
    markers: dict
    requirements: list[Requirement]
    safety_stock: float | None


@dataclass
class MorResult:
    rows: list[MorRow]
    unavailable_reason: str | None = None


def _lead_time_months(db, bu_id: str, product_id: str) -> int:
    rows = db.query(LeadTime).all()
    by_key = {(r.business_unit_id, r.product_id): r.months for r in rows}
    for key in [(bu_id, product_id), (None, product_id), (bu_id, None), (None, None)]:
        if key in by_key:
            return by_key[key]
    return 0


def _safety_stock(db, bu_id: str, product_id: str):
    row = (
        db.query(SafetyStock)
        .filter(SafetyStock.business_unit_id == bu_id, SafetyStock.product_id == product_id)
        .first()
    )
    return row.quantity if row is not None else None


def _stock_bu_products(db, bu_id_filter: str | None = None) -> dict[tuple[str, str], object]:
    keys: dict[tuple[str, str], object] = {}
    oh_query = db.query(InventoryOnHand)
    oo_query = db.query(InventoryOnOrder)
    if bu_id_filter is not None:
        oh_query = oh_query.filter(InventoryOnHand.business_unit_id == bu_id_filter)
        oo_query = oo_query.filter(InventoryOnOrder.business_unit_id == bu_id_filter)
    for row in oh_query.all():
        keys[(row.business_unit_id, row.product_id)] = row.unit
    for row in oo_query.all():
        keys.setdefault((row.business_unit_id, row.product_id), row.unit)
    return keys


def _owned_bu_products(
    db, customer_bu: dict[str, str], bu_id_filter: str | None, filter_customer
) -> dict[tuple[str, str], object]:
    """(bu, product) -> unit for every CustomerOwnedInventory row, keyed by
    the owning customer's BU (never stock tables) — this is what makes an
    owned-only product (zero on_hand/on_order) visible."""
    keys: dict[tuple[str, str], object] = {}
    query = db.query(CustomerOwnedInventory)
    if filter_customer is not None:
        query = query.filter(CustomerOwnedInventory.customer_id == filter_customer.id)
    for row in query.all():
        bu_id = customer_bu.get(row.customer_id)
        if bu_id is None:
            continue
        if bu_id_filter is not None and bu_id != bu_id_filter:
            continue
        keys.setdefault((bu_id, row.product_id), row.unit)
    return keys


def mor_rows(db, horizon: int | None = None, as_of=None, customer_id: str | None = None) -> MorResult:
    horizon = _clamp_horizon(horizon)
    as_of = as_of or today()
    month_keys = _month_seq(as_of, horizon)
    statuses, profiles = _scope(db)

    filter_customer = None
    bu_id_filter = None
    if customer_id is not None:
        filter_customer = db.get(Customer, customer_id)
        if filter_customer is None:
            return MorResult(rows=[], unavailable_reason=f"Customer {customer_id} not found")
        if not filter_customer.business_unit_id:
            return MorResult(
                rows=[], unavailable_reason=f"Customer {filter_customer.name} has no business unit assigned"
            )
        bu_id_filter = filter_customer.business_unit_id

    all_customers = db.query(Customer).all()
    customer_bu = {c.id: c.business_unit_id for c in all_customers}

    stock_products = _stock_bu_products(db, bu_id_filter=bu_id_filter)
    owned_products = _owned_bu_products(db, customer_bu, bu_id_filter, filter_customer)

    # BUs to consider: stock-derived, owned-derived, and (when unfiltered)
    # every BU that owns a customer at all — a demand-only product can appear
    # for any of those. M-4: filtered to exactly one BU when customer_id set.
    if bu_id_filter is not None:
        bu_ids = {bu_id_filter}
    else:
        bu_ids = {bu for (bu, _) in stock_products} | {bu for (bu, _) in owned_products}
        bu_ids |= {bu for bu in customer_bu.values() if bu is not None}

    # Pre-compute per-BU scoped demand by (product, month), and the
    # (bu, product) -> unit map for demand-only products (zero stock/owned).
    # When filtered to a customer, only that customer's own lines count (M-4).
    issues_by_bu: dict[str, dict[tuple[str, str], float]] = {}
    demand_products: dict[tuple[str, str], object] = {}
    for bu_id in bu_ids:
        acc: dict[tuple[str, str], float] = {}
        if filter_customer is not None:
            customers = [filter_customer]
        else:
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

    rows: list[MorRow] = []
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

        if filter_customer is not None:
            owned_rows = (
                db.query(CustomerOwnedInventory)
                .filter(
                    CustomerOwnedInventory.customer_id == filter_customer.id,
                    CustomerOwnedInventory.product_id == product_id,
                    CustomerOwnedInventory.unit == unit,
                )
                .all()
            )
        else:
            customer_ids = [c.id for c in db.query(Customer).filter(Customer.business_unit_id == bu_id).all()]
            owned_rows = (
                db.query(CustomerOwnedInventory)
                .filter(
                    CustomerOwnedInventory.customer_id.in_(customer_ids or [""]),
                    CustomerOwnedInventory.product_id == product_id,
                    CustomerOwnedInventory.unit == unit,
                )
                .all()
            )
        owned0 = sum(r.quantity for r in owned_rows)
        opening_balance = company0 + owned0

        booked_rows = (
            db.query(InventoryOnOrder)
            .filter(
                InventoryOnOrder.business_unit_id == bu_id,
                InventoryOnOrder.product_id == product_id,
                InventoryOnOrder.unit == unit,
            )
            .all()
        )
        booked_by_month: dict[str, float] = {k: 0.0 for k in month_keys}
        for r in booked_rows:
            if r.expected_date is None:
                continue
            key = _month_key(r.expected_date)
            if key in booked_by_month:
                booked_by_month[key] += r.quantity

        issues_for_product = issues_by_bu.get(bu_id, {})

        safety = _safety_stock(db, bu_id, product_id)
        lead_time = _lead_time_months(db, bu_id, product_id)

        strip = []
        balance = opening_balance
        physical_runout = None
        safety_breach = None
        prev_deficit = 0.0
        requirements: list[Requirement] = []
        for key in month_keys:
            issues = issues_for_product.get((product_id, key), 0.0)
            balance = balance + booked_by_month.get(key, 0.0) - issues
            strip.append({"month": key, "closing_balance": balance})

            if physical_runout is None and balance < 0:
                physical_runout = key
            if safety is not None and safety_breach is None and balance < safety:
                safety_breach = key

            minimum_level = safety if safety is not None else 0.0
            deficit = max(0.0, minimum_level - balance)
            increment = deficit - prev_deficit
            if increment > 1e-9:
                ex_mill = _shift_month(key, lead_time)
                overdue = ex_mill < _month_key(as_of)
                requirements.append(
                    Requirement(need_month=key, qty=increment, unit=unit, ex_mill_month=ex_mill, overdue=overdue)
                )
            prev_deficit = deficit

        order_deadline = requirements[0].ex_mill_month if requirements else None
        markers = {
            "order_deadline": order_deadline,
            "physical_runout": physical_runout,
            "safety_breach": safety_breach,
        }

        rows.append(
            MorRow(
                bu_id=bu_id,
                product_id=product_id,
                unit=unit,
                opening_balance=opening_balance,
                strip=strip,
                markers=markers,
                requirements=requirements,
                safety_stock=safety,
            )
        )

    return MorResult(rows=rows, unavailable_reason=None)
