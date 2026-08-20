"""Surplus engine (spec §Surplusエンジン). horizon is fixed at 36 months (not a
caller parameter) — this engine answers "how much company stock is genuinely
free vs allocated vs obsolete", which is a fixed-horizon strategic view, not a
tunable planning window like MRP/MOR.

Rulings implemented:
- `allocated` = for every customer in the BU, the (from_hard + from_free)
  portions of app.engines.allocation.allocate_customer's per-line allocation,
  summed by (product_id, unit). Owned inventory is deliberately excluded —
  it is not company stock, so it can never be "allocated company stock".
  allocate_customer is reused as-is (each call independently derives its own
  free pool state, same as every other caller of this engine in the codebase);
  this engine does not alter or re-implement that pooling.
- `obsolete` = for a (bu, product, unit) with ZERO scope-filtered demand
  lines whose ros_date falls within the 36-month horizon, the entire
  remaining (unallocated) company on_hand is obsolete. A product with any
  in-scope demand in the horizon is never obsolete, even if some of its
  on_hand is unallocated (that remainder is `surplus`, not `obsolete`).
- `surplus` = on_hand − allocated − obsolete. By construction (obsolete is
  either 0, or exactly on_hand − allocated for a zero-demand product), this
  makes on_hand == allocated + surplus + obsolete an identity for every row
  and for every unit-scoped total (spec invariant 6) — never computed by an
  independent path that could drift from the components.
- Money valuation is out of scope (no Price master yet).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.engines.allocation import InventoryScopeMissing, allocate_customer
from app.engines.coverage import _scope, scoped_lines
from app.models import Customer, InventoryOnHand
from app.services.dates import today

HORIZON_MONTHS = 36


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
class SurplusRow:
    bu_id: str
    product_id: str
    unit: object
    on_hand: float
    allocated: float
    surplus: float
    obsolete: float


def _bu_products(db) -> list[tuple[str, str, object]]:
    keys: dict[tuple[str, str], object] = {}
    for row in db.query(InventoryOnHand).all():
        keys[(row.business_unit_id, row.product_id)] = row.unit
    return [(bu_id, product_id, unit) for (bu_id, product_id), unit in keys.items()]


def surplus_rows(db, as_of=None, statuses=None, profiles=None) -> list[SurplusRow]:
    """`statuses`/`profiles` optionally override the stored Administration scope
    (spec E-2: GET /analysis/surplus status[]/profile[] params). Passing
    neither (the default) reproduces the original stored-scope behaviour
    exactly. This engine never persists anything, so an override call cannot
    corrupt stored state by construction."""
    as_of = as_of or today()
    month_keys = set(_month_seq(as_of, HORIZON_MONTHS))
    default_statuses, default_profiles = _scope(db)
    statuses = statuses if statuses is not None else default_statuses
    profiles = profiles if profiles is not None else default_profiles

    bu_products = _bu_products(db)
    bu_ids = {bu_id for bu_id, _, _ in bu_products}

    # allocated, per bu: sum from_hard+from_free across every customer's
    # allocate_customer() call, keyed by (product_id, unit).
    allocated_by_bu: dict[str, dict[tuple[str, object], float]] = {}
    # in-horizon scoped demand presence, per bu: {(product_id, unit)} seen.
    demand_present_by_bu: dict[str, set[tuple[str, object]]] = {}

    for bu_id in bu_ids:
        customers = db.query(Customer).filter(Customer.business_unit_id == bu_id).all()
        alloc_acc: dict[tuple[str, object], float] = {}
        demand_acc: set[tuple[str, object]] = set()
        for cust in customers:
            try:
                allocations = allocate_customer(db, cust)
            except InventoryScopeMissing:
                allocations = []
            for a in allocations:
                key = (a.line.product_id, a.line.unit)
                alloc_acc[key] = alloc_acc.get(key, 0.0) + a.from_hard + a.from_free

            for line in scoped_lines(db, cust.id, statuses, profiles):
                if _month_key(line.ros_date) in month_keys:
                    demand_acc.add((line.product_id, line.unit))

        allocated_by_bu[bu_id] = alloc_acc
        demand_present_by_bu[bu_id] = demand_acc

    results: list[SurplusRow] = []
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
        on_hand = sum(r.quantity for r in on_hand_rows)

        key = (product_id, unit)
        allocated = allocated_by_bu.get(bu_id, {}).get(key, 0.0)
        has_demand = key in demand_present_by_bu.get(bu_id, set())

        obsolete = 0.0 if has_demand else max(0.0, on_hand - allocated)
        surplus = on_hand - allocated - obsolete

        results.append(
            SurplusRow(
                bu_id=bu_id,
                product_id=product_id,
                unit=unit,
                on_hand=on_hand,
                allocated=allocated,
                surplus=surplus,
                obsolete=obsolete,
            )
        )

    return results
