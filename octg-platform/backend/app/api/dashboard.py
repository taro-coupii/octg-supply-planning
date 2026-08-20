# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
"""spec §Executive エンジン/API: GET /dashboard/executive.

MT headline conversion happens ONLY here (E-1) — the executive engine's
blocks stay native. status[]/profile[] scope-override semantics per E-2:
params absent entirely = default (stored CoverageResult fast path for the
coverage block); any params present (even an empty list) = non-default,
in-memory judge_customer path, with scope_is_default:false and a warning
field in the response.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.coverage import _scope
from app.engines.executive import (
    coverage_rows,
    demand_trend_rows,
    inventory_utilisation_rows,
    soft_allocation_rows,
    supply_risk_rows,
)
from app.models import (
    Customer,
    CoverageResult,
    CoverageVerdict,
    DemandLine,
    DemandRevision,
    Product,
    SubstitutionApproval,
    SubstitutionApprovalStatus,
    Well,
)
from app.services.dates import today

router = APIRouter(prefix="/dashboard")

# spec E-2 wording, verbatim — shared with /analysis/surplus.
SCOPE_OVERRIDE_WARNING = "Recomputed read-only — NOT the official stored verdicts"

# Human-readable verdict labels are a display-layer concern (spec §coverage block).
VERDICT_LABELS = {
    "Covered": "Covered",
    "CoveredViaSubstitute": "Covered via substitute",
    "PendingApproval": "Pending approval",
    "Uncovered": "Uncovered",
    "Unrecoverable": "Unrecoverable",
}


def _unit_value(unit) -> str:
    return unit.value if hasattr(unit, "value") else unit


def _mt_headline(db: Session, entries: list[tuple[str, object, float]]) -> tuple[float, bool]:
    """entries: (product_id, unit, qty). E-1: a row already in MT passes
    straight through (no weight needed). Any other unit needs
    Product.weight_kg (qty * weight_kg / 1000); a product with weight_kg=None
    is excluded from the MT total and flips mt_incomplete — never fabricated."""
    cache: dict[str, Product | None] = {}
    total = 0.0
    incomplete = False
    for product_id, unit, qty in entries:
        if _unit_value(unit) == "MT":
            total += qty
            continue
        if product_id not in cache:
            cache[product_id] = db.get(Product, product_id)
        product = cache[product_id]
        if product is None or product.weight_kg is None:
            incomplete = True
            continue
        total += qty * product.weight_kg / 1000.0
    return total, incomplete


def _native_by_unit(pairs) -> dict[str, float]:
    out: dict[str, float] = {}
    for unit, qty in pairs:
        uv = _unit_value(unit)
        out[uv] = out.get(uv, 0.0) + qty
    return out


def _product_name(db: Session, product_id: str) -> str:
    p = db.get(Product, product_id)
    return p.name if p is not None else product_id


def _resolve_scope(db: Session, status: list[str] | None, profile: list[str] | None):
    scope_is_default = status is None and profile is None
    default_statuses, default_profiles = _scope(db)
    statuses = set(status) if status is not None else default_statuses
    profiles = set(profile) if profile is not None else default_profiles
    return statuses, profiles, scope_is_default


def _build_demand_trend(db: Session, statuses, profiles, as_of) -> dict:
    months = demand_trend_rows(db, statuses, profiles, as_of)
    all_entries: list[tuple[str, object, float]] = []
    months_out = []
    for m in months:
        entries = [(r["product_id"], r["unit"], r["qty"]) for r in m.rows]
        all_entries.extend(entries)
        month_mt, month_incomplete = _mt_headline(db, entries)
        months_out.append(
            {
                "month": m.month,
                "qty_by_unit": _native_by_unit([(r["unit"], r["qty"]) for r in m.rows]),
                "mt_total": month_mt,
                "mt_incomplete": month_incomplete,
            }
        )
    mt_total, mt_incomplete = _mt_headline(db, all_entries)
    return {
        "available": True,
        "reason": None,
        "mt_total": mt_total,
        "mt_incomplete": mt_incomplete,
        "months": months_out,
    }


def _build_coverage(db: Session, statuses, profiles, use_stored: bool) -> dict:
    rows, reason = coverage_rows(db, statuses, profiles, use_stored)
    if rows is None:
        return {
            "available": False,
            "reason": reason,
            "mt_total": 0.0,
            "mt_incomplete": False,
            "by_verdict": {},
        }

    entries = [(r.product_id, r.unit, r.qty) for r in rows]
    mt_total, mt_incomplete = _mt_headline(db, entries)

    by_verdict: dict[str, dict] = {}
    for r in rows:
        v = by_verdict.setdefault(
            r.verdict,
            {"label": VERDICT_LABELS.get(r.verdict, r.verdict), "count": 0, "qty_by_unit": {}},
        )
        v["count"] += r.count
        uv = _unit_value(r.unit)
        v["qty_by_unit"][uv] = v["qty_by_unit"].get(uv, 0.0) + r.qty

    return {
        "available": True,
        "reason": None,
        "mt_total": mt_total,
        "mt_incomplete": mt_incomplete,
        "by_verdict": by_verdict,
    }


def _build_supply_risk(db: Session, statuses, profiles, horizon: int | None, as_of) -> dict:
    rows = supply_risk_rows(db, statuses, profiles, horizon=horizon, as_of=as_of)
    entries = [(r.product_id, r.unit, r.opening_total) for r in rows]
    mt_total, mt_incomplete = _mt_headline(db, entries)
    items = [
        {
            "product_id": r.product_id,
            "product_name": _product_name(db, r.product_id),
            "bu_id": r.bu_id,
            "unit": _unit_value(r.unit),
            "runout_month": r.runout_month,
            "opening_total": r.opening_total,
        }
        for r in rows
    ]
    return {
        "available": True,
        "reason": None,
        "mt_total": mt_total,
        "mt_incomplete": mt_incomplete,
        "items": items,
    }


def _build_soft_allocation(db: Session, statuses, profiles) -> dict:
    rows = soft_allocation_rows(db, statuses, profiles)
    entries = [(r.product_id, r.unit, r.from_free_qty) for r in rows]
    mt_total, mt_incomplete = _mt_headline(db, entries)

    by_customer: dict[str, dict] = {}
    for r in rows:
        c = by_customer.setdefault(r.customer_id, {"customer_name": r.customer_name, "qty_by_unit": {}})
        uv = _unit_value(r.unit)
        c["qty_by_unit"][uv] = c["qty_by_unit"].get(uv, 0.0) + r.from_free_qty

    return {
        "available": True,
        "reason": None,
        "mt_total": mt_total,
        "mt_incomplete": mt_incomplete,
        "by_customer": by_customer,
    }


def _build_inventory_utilisation(db: Session, statuses, profiles) -> dict:
    rows = inventory_utilisation_rows(db, statuses, profiles)
    entries = [(r.product_id, r.unit, r.surplus + r.obsolete) for r in rows]
    mt_total, mt_incomplete = _mt_headline(db, entries)

    totals_by_unit: dict[str, dict] = {}
    for r in rows:
        uv = _unit_value(r.unit)
        t = totals_by_unit.setdefault(uv, {"allocated": 0.0, "surplus": 0.0, "obsolete": 0.0})
        t["allocated"] += r.allocated
        t["surplus"] += r.surplus
        t["obsolete"] += r.obsolete

    products = [
        {
            "product_id": r.product_id,
            "product_name": _product_name(db, r.product_id),
            "unit": _unit_value(r.unit),
            "surplus": r.surplus,
            "obsolete": r.obsolete,
            "not_tied": r.surplus + r.obsolete,
        }
        for r in rows
    ]

    return {
        "available": True,
        "reason": None,
        "mt_total": mt_total,
        "mt_incomplete": mt_incomplete,
        "totals_by_unit": totals_by_unit,
        "products": products,
    }


@router.get("/executive")
def get_executive_dashboard(
    status: list[str] | None = Query(None),
    profile: list[str] | None = Query(None),
    horizon: int | None = None,
    db: Session = Depends(get_db),
):
    statuses, profiles, scope_is_default = _resolve_scope(db, status, profile)
    as_of = today()

    body = {
        "scope_is_default": scope_is_default,
        "status_scope": sorted(statuses),
        "profile_scope": sorted(profiles),
        "demand_trend": _build_demand_trend(db, statuses, profiles, as_of),
        "coverage": _build_coverage(db, statuses, profiles, use_stored=scope_is_default),
        "supply_risk": _build_supply_risk(db, statuses, profiles, horizon, as_of),
        "soft_allocation": _build_soft_allocation(db, statuses, profiles),
        "inventory_utilisation": _build_inventory_utilisation(db, statuses, profiles),
    }
    if not scope_is_default:
        body["warning"] = SCOPE_OVERRIDE_WARNING
    return body


# --- GET /dashboard/home (spec §Home Dashboard) -----------------------------

_ATTENTION_SEVERITY = {
    CoverageVerdict.UNCOVERED.value: 0,
    CoverageVerdict.UNRECOVERABLE.value: 1,
}


class CoverageKpiOut(BaseModel):
    rate: float | None
    covered_count: int
    evaluated_count: int
    not_evaluated_count: int


class HomeKpiOut(BaseModel):
    customer_count: int
    well_status_counts: dict[str, int]
    coverage: CoverageKpiOut
    pending_approvals_count: int


class DemandChangeOut(BaseModel):
    id: str
    well_id: str
    well_name: str
    revision_no: int
    applied_at: datetime.datetime
    source: str
    summary: str


class PendingUnionRowOut(BaseModel):
    source: str  # "request" | "verdict"
    label: str
    id: str
    well_id: str
    well_name: str
    demand_line_id: str
    link: str


class AttentionWellOut(BaseModel):
    well_id: str
    well_name: str
    customer_id: str
    customer_name: str
    worst_verdict: str
    line_count: int


class HomeOut(BaseModel):
    kpi: HomeKpiOut
    demand_changes: list[DemandChangeOut]
    pending_union: list[PendingUnionRowOut]
    attention_wells: list[AttentionWellOut]


def _build_kpi(db: Session) -> HomeKpiOut:
    customer_count = db.query(Customer).count()

    well_status_counts: dict[str, int] = {}
    for (status,) in db.query(Well.demand_status).all():
        key = _unit_value(status)
        well_status_counts[key] = well_status_counts.get(key, 0) + 1

    total_lines = db.query(DemandLine).count()
    verdict_counts: dict[str, int] = {}
    for (verdict,) in db.query(CoverageResult.verdict).all():
        key = _unit_value(verdict)
        verdict_counts[key] = verdict_counts.get(key, 0) + 1

    evaluated_count = sum(verdict_counts.values())
    not_evaluated_count = total_lines - evaluated_count
    covered_count = verdict_counts.get(CoverageVerdict.COVERED.value, 0) + verdict_counts.get(
        CoverageVerdict.COVERED_VIA_SUBSTITUTE.value, 0
    )
    rate = (covered_count / evaluated_count) if evaluated_count > 0 else None

    pending_approvals_count = (
        db.query(SubstitutionApproval)
        .filter_by(status=SubstitutionApprovalStatus.PENDING)
        .count()
    )

    return HomeKpiOut(
        customer_count=customer_count,
        well_status_counts=well_status_counts,
        coverage=CoverageKpiOut(
            rate=rate,
            covered_count=covered_count,
            evaluated_count=evaluated_count,
            not_evaluated_count=not_evaluated_count,
        ),
        pending_approvals_count=pending_approvals_count,
    )


def _build_demand_changes(db: Session) -> list[DemandChangeOut]:
    revisions = (
        db.query(DemandRevision).order_by(DemandRevision.applied_at.desc()).limit(15).all()
    )
    out: list[DemandChangeOut] = []
    for rev in revisions:
        well = db.get(Well, rev.well_id)
        out.append(
            DemandChangeOut(
                id=rev.id,
                well_id=rev.well_id,
                well_name=well.name if well else "",
                revision_no=rev.revision_no,
                applied_at=rev.applied_at,
                source=_unit_value(rev.source),
                summary=rev.summary,
            )
        )
    return out


def _build_pending_union(db: Session) -> list[PendingUnionRowOut]:
    """spec §Home: pending_union = 未決定 SubstitutionApproval 全件 ∪ 申請前の
    PendingApproval 判定明細. A demand line with BOTH an existing pending
    request AND a PendingApproval verdict appears only once, as "request"
    (the request is the actionable item; the verdict is its cause)."""
    out: list[PendingUnionRowOut] = []
    requested_line_ids: set[str] = set()

    pending_requests = (
        db.query(SubstitutionApproval).filter_by(status=SubstitutionApprovalStatus.PENDING).all()
    )
    for approval in pending_requests:
        requested_line_ids.add(approval.demand_line_id)
        well = db.get(Well, approval.well_id)
        out.append(
            PendingUnionRowOut(
                source="request",
                label="Substitution approval requested",
                id=approval.id,
                well_id=approval.well_id,
                well_name=well.name if well else "",
                demand_line_id=approval.demand_line_id,
                link="/approvals",
            )
        )

    verdict_rows = (
        db.query(CoverageResult).filter_by(verdict=CoverageVerdict.PENDING_APPROVAL).all()
    )
    for result in verdict_rows:
        if result.demand_line_id in requested_line_ids:
            continue
        line = db.get(DemandLine, result.demand_line_id)
        if line is None:
            continue
        well = db.get(Well, line.well_id)
        out.append(
            PendingUnionRowOut(
                source="verdict",
                label="Awaiting substitution request",
                id=result.id,
                well_id=line.well_id,
                well_name=well.name if well else "",
                demand_line_id=result.demand_line_id,
                link=f"/substitution?demand_line_id={result.demand_line_id}",
            )
        )

    return out


def _build_attention_wells(db: Session) -> list[AttentionWellOut]:
    """Top 5 wells whose worst verdict is Uncovered/Unrecoverable, worst first."""
    rows = (
        db.query(CoverageResult, DemandLine)
        .join(DemandLine, CoverageResult.demand_line_id == DemandLine.id)
        .filter(
            CoverageResult.verdict.in_(
                [CoverageVerdict.UNCOVERED, CoverageVerdict.UNRECOVERABLE]
            )
        )
        .all()
    )

    by_well: dict[str, dict] = {}
    for result, line in rows:
        entry = by_well.setdefault(line.well_id, {"worst": result.verdict.value, "line_count": 0})
        entry["line_count"] += 1
        if _ATTENTION_SEVERITY[result.verdict.value] > _ATTENTION_SEVERITY[entry["worst"]]:
            entry["worst"] = result.verdict.value

    ranked = sorted(
        by_well.items(),
        key=lambda kv: (-_ATTENTION_SEVERITY[kv[1]["worst"]], -kv[1]["line_count"]),
    )

    out: list[AttentionWellOut] = []
    for well_id, entry in ranked[:5]:
        well = db.get(Well, well_id)
        customer = db.get(Customer, well.customer_id) if well else None
        out.append(
            AttentionWellOut(
                well_id=well_id,
                well_name=well.name if well else "",
                customer_id=well.customer_id if well else "",
                customer_name=customer.name if customer else "",
                worst_verdict=entry["worst"],
                line_count=entry["line_count"],
            )
        )
    return out


@router.get("/home", response_model=HomeOut)
def get_home_dashboard(db: Session = Depends(get_db)):
    return HomeOut(
        kpi=_build_kpi(db),
        demand_changes=_build_demand_changes(db),
        pending_union=_build_pending_union(db),
        attention_wells=_build_attention_wells(db),
    )
