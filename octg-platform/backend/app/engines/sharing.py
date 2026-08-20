"""Sharing analysis engine (spec §API row 8, §3-3). Read-only what-if.

Uses app.engines.allocation.allocate_customer — the sole allocation
implementation — for both the target customer's shortfall lines and each
peer's releasable company stock. Never writes to the DB (no commit, no
flush of new objects); official_verdict is read verbatim from the stored
CoverageResult. Customer-owned inventory is never counted as releasable —
allocate_customer's `from_owned` figure is deliberately excluded.
"""

from __future__ import annotations

from app.engines.allocation import allocate_customer
from app.models import Customer, CoverageResult, CoverageVerdict, DemandLine, Well

_UNCOVERED_VERDICTS = (CoverageVerdict.UNCOVERED, CoverageVerdict.UNRECOVERABLE)


def _releasable_by_product_unit(db, peer: Customer) -> dict[tuple[str, object], float]:
    """Peer's releasable company stock per (product_id, unit): the sum of what
    the allocation engine gave this peer from hard assignments and free BU
    stock — customer-owned material is excluded (spec: never shareable)."""
    allocations = allocate_customer(db, peer)
    totals: dict[tuple[str, object], float] = {}
    for alloc in allocations:
        key = (alloc.line.product_id, alloc.line.unit)
        totals[key] = totals.get(key, 0.0) + alloc.from_hard + alloc.from_free
    return totals


def sharing_analysis(db, customer_id: str) -> list[dict] | None:
    """Return per-Uncovered/Unrecoverable-line what-if peer analysis for
    `customer_id`, or None if the customer does not exist. Read-only: makes
    no writes to the session (no commit, no add of new rows)."""
    target = db.get(Customer, customer_id)
    if target is None:
        return None

    if target.business_unit_id is None:
        return []

    bu_id = target.business_unit_id

    # Stored, uncovered lines for this customer only.
    rows = (
        db.query(CoverageResult, DemandLine)
        .join(DemandLine, CoverageResult.demand_line_id == DemandLine.id)
        .join(Well, DemandLine.well_id == Well.id)
        .filter(Well.customer_id == customer_id, CoverageResult.verdict.in_(_UNCOVERED_VERDICTS))
        .all()
    )
    if not rows:
        return []

    peers = (
        db.query(Customer)
        .filter(Customer.business_unit_id == bu_id, Customer.id != customer_id)
        .all()
    )
    # Cache each peer's releasable totals once (peer allocation is
    # independent of the target's shortfall lines).
    peer_releasable = {peer.id: _releasable_by_product_unit(db, peer) for peer in peers}

    out: list[dict] = []
    for coverage, line in rows:
        shortfall = line.quantity - (coverage.covered_qty or 0.0)
        key = (line.product_id, line.unit)
        peer_entries = []
        for peer in peers:
            releasable = peer_releasable[peer.id].get(key, 0.0)
            peer_entries.append(
                {
                    "customer_id": peer.id,
                    "customer_name": peer.name,
                    "releasable_qty_by_unit": releasable,
                    "would_cover": releasable >= shortfall if shortfall > 0 else True,
                }
            )
        out.append(
            {
                "line": {
                    "id": line.id,
                    "well_id": line.well_id,
                    "product_id": line.product_id,
                    "unit": line.unit.value,
                    "quantity": line.quantity,
                },
                "official_verdict": coverage.verdict.value,
                "peers": peer_entries,
            }
        )

    return out
