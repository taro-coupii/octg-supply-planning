"""Allocation engine (spec §割当エンジン, 裁定A-1/A-2/A-3). Pure, sole implementation.

Rulings:
- A-1: policy semantics — owned inventory always consumed first (never crosses to
  another customer). HARD then consumes only that customer's hard assignments
  (never free stock). SOFT then consumes only BU free stock (never hard
  assignments). HYBRID consumes hard then free.
- A-2: coverage nets physical stock only (on-hand + customer-owned); on_order is
  never used here (that is MRP/MOR's domain, out of scope for this engine).
- A-3: BU is an absolute boundary — only the customer's own BU's stock is
  visible. A BU-less customer causes this engine to raise
  InventoryScopeMissing so the caller can skip that customer in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import (
    AllocationPolicy,
    Customer,
    CustomerOwnedInventory,
    DemandLine,
    InventoryAssignment,
    InventoryOnHand,
)


class InventoryScopeMissing(Exception):
    """Raised when a customer has no business_unit_id (spec 裁定A-3)."""


@dataclass
class LineAllocation:
    line: DemandLine
    from_owned: float
    from_hard: float
    from_free: float
    shortfall: float


def hard_assigned_total(db, bu_id: str, product_id: str, unit) -> float:
    """Sum of all InventoryAssignment quantities for (bu, product, unit), across
    every customer. Later tasks (candidates API, sharing) reuse this — do not
    re-derive it elsewhere."""
    rows = (
        db.query(InventoryAssignment)
        .filter(
            InventoryAssignment.business_unit_id == bu_id,
            InventoryAssignment.product_id == product_id,
            InventoryAssignment.unit == unit,
        )
        .all()
    )
    return sum(row.quantity for row in rows)


def free_pool(db, bu_id: str, product_id: str, unit) -> float:
    """BU free stock = on_hand(bu, product, unit) − total hard assignments for
    that (bu, product, unit), floored at 0."""
    on_hand_rows = (
        db.query(InventoryOnHand)
        .filter(
            InventoryOnHand.business_unit_id == bu_id,
            InventoryOnHand.product_id == product_id,
            InventoryOnHand.unit == unit,
        )
        .all()
    )
    on_hand = sum(row.quantity for row in on_hand_rows)
    hard_total = hard_assigned_total(db, bu_id, product_id, unit)
    return max(0.0, on_hand - hard_total)


def _owned_pool(db, customer_id: str, product_id: str, unit) -> float:
    rows = (
        db.query(CustomerOwnedInventory)
        .filter(
            CustomerOwnedInventory.customer_id == customer_id,
            CustomerOwnedInventory.product_id == product_id,
            CustomerOwnedInventory.unit == unit,
        )
        .all()
    )
    return sum(row.quantity for row in rows)


def _hard_pool_for_customer(db, bu_id: str, product_id: str, customer_id: str, unit) -> float:
    rows = (
        db.query(InventoryAssignment)
        .filter(
            InventoryAssignment.business_unit_id == bu_id,
            InventoryAssignment.product_id == product_id,
            InventoryAssignment.customer_id == customer_id,
            InventoryAssignment.unit == unit,
        )
        .all()
    )
    return sum(row.quantity for row in rows)


@dataclass
class AllocationResult:
    allocations: list[LineAllocation]
    # Residual BU free stock per (product_id, unit) AFTER this allocation run,
    # for every (product_id, unit) touched by the given lines. Callers that need
    # to know free stock for a product never touched here must fall back to
    # allocation.free_pool for that key.
    residual_free: dict[tuple[str, object], float]


def allocate_lines(db, customer: Customer, lines: list[DemandLine]) -> list[LineAllocation]:
    """Allocate the given demand lines for `customer`, consuming pools
    statefully across lines in (ros_date, id) order. `lines` need not already
    be sorted or scoped — that filtering is the caller's (coverage engine's)
    responsibility; this function sorts and allocates whatever it is given.

    Thin wrapper over allocate_lines_with_residual for callers that only need
    the per-line allocations (candidates, sharing, etc).
    """
    return allocate_lines_with_residual(db, customer, lines).allocations


def allocate_lines_with_residual(db, customer: Customer, lines: list[DemandLine]) -> AllocationResult:
    """Same allocation as allocate_lines, but also returns the residual BU free
    stock per (product_id, unit) after this run — so callers that seed a
    substitute pool (coverage engine) can see stock already consumed by this
    customer's own lines in the same pass, instead of double-booking it."""
    if not customer.business_unit_id:
        raise InventoryScopeMissing(f"Customer {customer.id} has no business_unit_id")

    bu_id = customer.business_unit_id
    policy = customer.allocation_policy

    ordered = sorted(lines, key=lambda l: (l.ros_date, l.id))

    # Per-(product, unit) pool state, lazily initialized and depleted as lines consume it.
    owned_pools: dict[tuple[str, object], float] = {}
    hard_pools: dict[tuple[str, object], float] = {}
    free_pools: dict[tuple[str, object], float] = {}

    results: list[LineAllocation] = []
    for line in ordered:
        key = (line.product_id, line.unit)

        if key not in owned_pools:
            owned_pools[key] = _owned_pool(db, customer.id, line.product_id, line.unit)
        if key not in hard_pools:
            hard_pools[key] = _hard_pool_for_customer(db, bu_id, line.product_id, customer.id, line.unit)
        if key not in free_pools:
            free_pools[key] = free_pool(db, bu_id, line.product_id, line.unit)

        remaining = line.quantity

        from_owned = min(remaining, owned_pools[key])
        owned_pools[key] -= from_owned
        remaining -= from_owned

        from_hard = 0.0
        from_free = 0.0

        if policy == AllocationPolicy.HARD:
            from_hard = min(remaining, hard_pools[key])
            hard_pools[key] -= from_hard
            remaining -= from_hard
        elif policy == AllocationPolicy.SOFT:
            from_free = min(remaining, free_pools[key])
            free_pools[key] -= from_free
            remaining -= from_free
        elif policy == AllocationPolicy.HYBRID:
            from_hard = min(remaining, hard_pools[key])
            hard_pools[key] -= from_hard
            remaining -= from_hard
            from_free = min(remaining, free_pools[key])
            free_pools[key] -= from_free
            remaining -= from_free

        results.append(
            LineAllocation(
                line=line,
                from_owned=from_owned,
                from_hard=from_hard,
                from_free=from_free,
                shortfall=remaining,
            )
        )

    return AllocationResult(allocations=results, residual_free=free_pools)


def allocate_customer(db, customer: Customer) -> list[LineAllocation]:
    """Convenience: load ALL demand lines for this customer's wells (unscoped —
    the coverage engine applies scope filtering) and allocate them."""
    from app.models import Well

    lines = (
        db.query(DemandLine)
        .join(Well, DemandLine.well_id == Well.id)
        .filter(Well.customer_id == customer.id)
        .all()
    )
    return allocate_lines(db, customer, lines)
