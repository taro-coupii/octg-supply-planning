"""Coverage engine (spec §カバレッジエンジン, 裁定C-1). Sole writer of CoverageResult (§3-6).

Verdict priority: Covered > CoveredViaSubstitute > PendingApproval > Uncovered >
Unrecoverable. Substitute stock checks reuse app.engines.allocation.free_pool —
no re-derivation of pool math here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from app.engines.allocation import (
    InventoryScopeMissing,
    LineAllocation,
    allocate_lines_with_residual,
    free_pool,
)
from app.models import (
    Customer,
    CoverageResult,
    CoverageVerdict,
    CustomerSubstitutionRule,
    DemandLine,
    InventoryAssignment,
    Product,
    Setting,
    SubstitutionApproval,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    Well,
)

STATUSES_KEY = "coverage_scope_statuses"
PROFILES_KEY = "coverage_scope_profiles"
DEFAULT_STATUSES = ["Confirmed"]
DEFAULT_PROFILES = ["Primary", "Contingency"]
LAST_RECOMPUTE_SKIPS_KEY = "last_recompute_skips"


def _save_last_recompute_skips(db, skipped: list[tuple[str, str]]) -> None:
    """Persist the last recompute_all's skipped customers so GET /coverage can
    report them (they are not derivable from stored CoverageResult rows)."""
    payload = json.dumps([{"name": name, "reason": reason} for name, reason in skipped])
    row = db.query(Setting).filter_by(key=LAST_RECOMPUTE_SKIPS_KEY).first()
    if row is None:
        row = Setting(key=LAST_RECOMPUTE_SKIPS_KEY, value=payload)
        db.add(row)
    else:
        row.value = payload
    db.commit()


def last_recompute_skips(db) -> list[dict]:
    row = db.query(Setting).filter_by(key=LAST_RECOMPUTE_SKIPS_KEY).first()
    if row is None or not row.value:
        return []
    return json.loads(row.value)


def _fmt(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _scope(db) -> tuple[set[str], set[str]]:
    def _get(key: str, default: list[str]) -> set[str]:
        row = db.query(Setting).filter_by(key=key).first()
        raw = row.value if row and row.value else None
        return set(raw.split(",")) if raw else set(default)

    return _get(STATUSES_KEY, DEFAULT_STATUSES), _get(PROFILES_KEY, DEFAULT_PROFILES)


def _all_customer_line_ids(db, customer_id: str) -> list[str]:
    rows = (
        db.query(DemandLine.id)
        .join(Well, DemandLine.well_id == Well.id)
        .filter(Well.customer_id == customer_id)
        .all()
    )
    return [r[0] for r in rows]


def scoped_lines(db, customer_id: str, statuses: set[str], profiles: set[str]) -> list[DemandLine]:
    """Demand lines for `customer_id` whose well.demand_status and line.profile
    are within the configured scope (spec: settings-driven scope filter)."""
    rows = (
        db.query(DemandLine, Well)
        .join(Well, DemandLine.well_id == Well.id)
        .filter(Well.customer_id == customer_id)
        .all()
    )
    return [
        line
        for line, well in rows
        if well.demand_status.value in statuses and line.profile.value in profiles
    ]


def _other_customer_hard_total(db, bu_id: str, product_id: str, exclude_customer_id: str, unit) -> float:
    rows = (
        db.query(InventoryAssignment)
        .filter(
            InventoryAssignment.business_unit_id == bu_id,
            InventoryAssignment.product_id == product_id,
            InventoryAssignment.unit == unit,
            InventoryAssignment.customer_id != exclude_customer_id,
        )
        .all()
    )
    return sum(row.quantity for row in rows)


def _allowed_substitutes(db, customer_id: str, product_id: str) -> list[TechnicalSubstitution]:
    subs = db.query(TechnicalSubstitution).filter_by(from_product_id=product_id).all()
    allowed = []
    for ts in subs:
        rule = (
            db.query(CustomerSubstitutionRule)
            .filter_by(customer_id=customer_id, technical_substitution_id=ts.id)
            .first()
        )
        if rule is not None and rule.allowed:
            allowed.append(ts)
    return allowed


def _sub_pool_qty(db, sub_pools: dict, bu_id: str, product_id: str, unit) -> float:
    """Per-recompute substitute-pool state: seeded from this recompute's residual
    free stock (i.e. allocation.free_pool net of what this customer's OWN lines
    already consumed in this same recompute — bug fix: a product's free stock
    could otherwise be double-booked between a line that directly allocates it
    and another line's substitute check), falling back to allocation.free_pool
    only for products this recompute's allocation never touched. Then depleted
    as lines in the same recompute_customer call actually consume it as a
    substitute (bug fix: two lines could otherwise both claim the same free
    substitute stock)."""
    key = (product_id, unit)
    if key not in sub_pools:
        sub_pools[key] = free_pool(db, bu_id, product_id, unit)
    return sub_pools[key]


# Approval statuses that count as an existing request — a Rejected row must not
# block a fresh request (spec 裁定: 覆すには新申請).
_ACTIVE_APPROVAL_STATUSES = (SubstitutionApprovalStatus.PENDING, SubstitutionApprovalStatus.APPROVED)


def _judge(db, customer: Customer, alloc: LineAllocation, sub_pools: dict):
    """Return (verdict, reason, action, covered_qty, covered_via) for one line's allocation.

    `sub_pools` is shared mutable state across all lines in one recompute_customer
    call so substitute stock is not double-booked across lines.
    """
    line = alloc.line
    unit = line.unit
    allocated = alloc.from_owned + alloc.from_hard + alloc.from_free
    base = (
        f"Need {_fmt(line.quantity)} {unit.value}, allocated {_fmt(allocated)} {unit.value} "
        f"(owned {_fmt(alloc.from_owned)}, hard {_fmt(alloc.from_hard)}, free {_fmt(alloc.from_free)})"
    )

    if alloc.shortfall <= 0:
        return CoverageVerdict.COVERED, base + "; fully covered.", None, allocated, None

    shortfall = alloc.shortfall
    bu_id = customer.business_unit_id
    allowed_subs = _allowed_substitutes(db, customer.id, line.product_id)

    # CoveredViaSubstitute: an Approved approval for this line whose substitute
    # has enough free stock (net of this recompute's prior consumption) to cover
    # the shortfall.
    for ts in allowed_subs:
        approval = (
            db.query(SubstitutionApproval)
            .filter_by(
                demand_line_id=line.id,
                technical_substitution_id=ts.id,
                status=SubstitutionApprovalStatus.APPROVED,
            )
            .first()
        )
        if approval is None:
            continue
        free_qty = _sub_pool_qty(db, sub_pools, bu_id, ts.to_product_id, unit)
        if free_qty >= shortfall:
            key = (ts.to_product_id, unit)
            sub_pools[key] -= shortfall
            to_product = db.get(Product, ts.to_product_id)
            reason = (
                base
                + f"; short {_fmt(shortfall)} {unit.value}, covered via approved substitute "
                f"{to_product.name} (free {_fmt(free_qty)} {unit.value})."
            )
            covered_via = json.dumps({"product_id": ts.to_product_id, "product_name": to_product.name})
            return CoverageVerdict.COVERED_VIA_SUBSTITUTE, reason, None, line.quantity, covered_via

    # PendingApproval: an undecided request whose substitute would cover the shortfall
    # against the (possibly already-depleted-by-earlier-lines) pool.
    pending_rows = (
        db.query(SubstitutionApproval)
        .filter_by(demand_line_id=line.id, status=SubstitutionApprovalStatus.PENDING)
        .all()
    )
    for pending in pending_rows:
        ts = db.get(TechnicalSubstitution, pending.technical_substitution_id)
        if ts is None:
            continue
        free_qty = _sub_pool_qty(db, sub_pools, bu_id, ts.to_product_id, unit)
        if free_qty >= shortfall:
            to_product = db.get(Product, ts.to_product_id)
            reason = (
                base
                + f"; short {_fmt(shortfall)} {unit.value}, substitution approval pending for "
                f"{to_product.name} (free {_fmt(free_qty)} {unit.value})."
            )
            return CoverageVerdict.PENDING_APPROVAL, reason, None, allocated, None

    # Uncovered (a): hard assignments to OTHER customers on this product/BU would cover it.
    other_hard = _other_customer_hard_total(db, bu_id, line.product_id, customer.id, unit)
    if other_hard >= shortfall:
        reason = (
            base
            + f"; short {_fmt(shortfall)} {unit.value}; {_fmt(other_hard)} {unit.value} is "
            "hard-assigned to other customers and could be released."
        )
        return CoverageVerdict.UNCOVERED, reason, "Release the hard assignment in Oracle", allocated, None

    # Uncovered (b): an allowed substitute has enough free stock (net of this
    # recompute's prior consumption), but no ACTIVE (Pending/Approved) request
    # exists — a Rejected row must not block a fresh request.
    for ts in allowed_subs:
        free_qty = _sub_pool_qty(db, sub_pools, bu_id, ts.to_product_id, unit)
        if free_qty < shortfall:
            continue
        existing = (
            db.query(SubstitutionApproval)
            .filter(
                SubstitutionApproval.demand_line_id == line.id,
                SubstitutionApproval.technical_substitution_id == ts.id,
                SubstitutionApproval.status.in_(_ACTIVE_APPROVAL_STATUSES),
            )
            .first()
        )
        if existing is not None:
            continue
        to_product = db.get(Product, ts.to_product_id)
        reason = (
            base
            + f"; short {_fmt(shortfall)} {unit.value}; substitute {to_product.name} has "
            f"{_fmt(free_qty)} {unit.value} free stock but no approval has been requested."
        )
        return CoverageVerdict.UNCOVERED, reason, "Request substitution approval", allocated, None

    # Unrecoverable: no physical means within the BU to close the gap.
    reason = base + f"; short {_fmt(shortfall)} {unit.value}; no physical stock available in the BU by any means."
    return CoverageVerdict.UNRECOVERABLE, reason, "Order from mill", allocated, None


@dataclass
class JudgedLine:
    """One demand line's in-memory verdict — no persistence attached.

    Shared by recompute_customer (which persists it as a CoverageResult) and
    the scope-override read paths (Executive/Surplus, spec E-2), which call
    judge_customer directly and never write. ONE judge implementation."""

    line: DemandLine
    verdict: CoverageVerdict
    reason: str
    action: str | None
    covered_qty: float
    covered_via: str | None


def judge_customer(db, customer: Customer, statuses: set[str], profiles: set[str]) -> list[JudgedLine]:
    """Compute verdicts for `customer`'s demand lines within the given
    status/profile scope. Pure read — never writes CoverageResult rows, never
    commits. Raises InventoryScopeMissing (from allocate_lines_with_residual)
    if the customer has no business_unit_id.
    """
    lines = scoped_lines(db, customer.id, statuses, profiles)

    alloc_result = allocate_lines_with_residual(db, customer, lines)

    # Seed the substitute pool from this run's residual free stock (net of
    # what this customer's own lines already consumed directly), not raw
    # free_pool — otherwise substitute checks could double-book stock a line
    # already claimed.
    sub_pools: dict = dict(alloc_result.residual_free)
    judged: list[JudgedLine] = []
    for alloc in alloc_result.allocations:
        verdict, reason, action, covered_qty, covered_via = _judge(db, customer, alloc, sub_pools)
        judged.append(
            JudgedLine(
                line=alloc.line,
                verdict=verdict,
                reason=reason,
                action=action,
                covered_qty=covered_qty,
                covered_via=covered_via,
            )
        )
    return judged


def recompute_customer(db, customer_id: str) -> list[CoverageResult]:
    """Full-replace this customer's CoverageResults for in-scope demand lines.
    computed_at is always "now", even if the verdict set is unchanged (C-08 lesson).
    Raises InventoryScopeMissing (before any writes) if the customer has no BU.
    """
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise ValueError(f"Customer {customer_id} not found")

    statuses, profiles = _scope(db)

    # judge_customer raises InventoryScopeMissing before any mutation.
    judged = judge_customer(db, customer, statuses, profiles)

    all_line_ids = _all_customer_line_ids(db, customer_id)
    if all_line_ids:
        db.query(CoverageResult).filter(CoverageResult.demand_line_id.in_(all_line_ids)).delete(
            synchronize_session=False
        )

    now = datetime.now(timezone.utc)
    results: list[CoverageResult] = []
    for j in judged:
        cr = CoverageResult(
            demand_line_id=j.line.id,
            verdict=j.verdict,
            reason=j.reason,
            action=j.action,
            covered_qty=j.covered_qty,
            covered_via=j.covered_via,
            computed_at=now,
        )
        db.add(cr)
        results.append(cr)

    db.commit()
    return results


def recompute_all(db) -> dict:
    """Per-customer isolation: a failing customer is recorded in skipped and
    does not prevent the rest from being recomputed."""
    customers = db.query(Customer).all()
    computed = 0
    skipped: list[tuple[str, str]] = []
    for customer in customers:
        try:
            results = recompute_customer(db, customer.id)
            computed += len(results)
        except InventoryScopeMissing as exc:
            db.rollback()
            skipped.append((customer.name, str(exc)))
        except Exception as exc:  # per-customer failure isolation (spec 裁定A-3 / invariant 5)
            db.rollback()
            skipped.append((customer.name, str(exc)))
    _save_last_recompute_skips(db, skipped)
    return {"computed": computed, "skipped_customers": skipped}
