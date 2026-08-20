"""Scenario engine (spec §シナリオAPI, 裁定 E-3/E-4). In-memory preview + apply.

E-3 preview: copy the CURRENT session's sqlite database into a fresh in-memory
sqlite database using the sqlite3 backup API (raw connection), apply scenario
overrides to that COPY, then run the SAME production engines (coverage /
mrp) on the copy — no second judge/verdict implementation, no write to the
real DB (structurally avoids the pysqlite SAVEPOINT write/rollback trap).

E-4 apply: platform-owned overrides only (quantity/ros_date/well_status/
approval_flip) may ever be written to the real session. Any supply-owned
override present (po_arrival/hard_release) rejects the WHOLE apply — nothing
is written — the caller reports {rejected: [...]} as 422.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.engines import coverage as coverage_engine
from app.engines.mrp import mrp_rows
from app.models import (
    CoverageResult,
    DemandLine,
    DemandRevision,
    DemandRevisionSource,
    DemandStatus,
    InventoryAssignment,
    InventoryOnOrder,
    Scenario,
    ScenarioOverride,
    ScenarioOverrideKind,
    ScenarioStatus,
    SubstitutionApproval,
    SubstitutionApprovalStatus,
    Well,
)

# spec E-4: only these kinds may ever be written to the real session by apply.
PLATFORM_OWNED_KINDS = {
    ScenarioOverrideKind.QUANTITY,
    ScenarioOverrideKind.ROS_DATE,
    ScenarioOverrideKind.WELL_STATUS,
    ScenarioOverrideKind.APPROVAL_FLIP,
}
# spec E-4: presence of any of these in an apply request rejects the whole apply.
SUPPLY_OWNED_KINDS = {ScenarioOverrideKind.PO_ARRIVAL, ScenarioOverrideKind.HARD_RELEASE}


@contextmanager
def make_inmemory_copy(session: Session):
    """spec E-3: sqlite3 backup API copy of `session`'s current database into a
    fresh in-memory sqlite database, yielded as a brand-new ORM Session bound
    to a fresh Engine. The FK pragma is applied automatically (app.db's global
    Engine "connect" listener fires for any sqlite3 DBAPI connection).

    Context-managed so ALL resources of the copy — the ORM Session, the
    StaticPool Engine, and the raw sqlite3 target connection — are released
    together on exit. Without this, only the Session was ever closed and the
    Engine + raw connection (a full in-memory DB copy) leaked until GC."""
    raw_conn = session.connection().connection.driver_connection
    target_conn = sqlite3.connect(":memory:", check_same_thread=False)
    raw_conn.backup(target_conn)

    copy_engine = create_engine(
        "sqlite://",
        creator=lambda: target_conn,
        poolclass=StaticPool,
    )
    CopySession = sessionmaker(bind=copy_engine, expire_on_commit=False)
    copy_session = CopySession()
    try:
        yield copy_session
    finally:
        copy_session.close()
        copy_engine.dispose()
        target_conn.close()


def _parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(value).date()


def apply_overrides(session: Session, overrides: list[ScenarioOverride]) -> None:
    """Apply every override in `overrides` to `session` (real or in-memory
    copy) — kind-gating (platform-owned only for the real session) is the
    caller's job, not this function's; preview applies ALL kinds to the copy."""
    for ov in overrides:
        payload = json.loads(ov.payload) if ov.payload else {}

        if ov.kind == ScenarioOverrideKind.QUANTITY:
            line = session.get(DemandLine, ov.target_id)
            if line is not None:
                line.quantity = payload["value"]

        elif ov.kind == ScenarioOverrideKind.ROS_DATE:
            line = session.get(DemandLine, ov.target_id)
            if line is not None:
                line.ros_date = _parse_date(payload["value"])

        elif ov.kind == ScenarioOverrideKind.WELL_STATUS:
            well = session.get(Well, ov.target_id)
            if well is not None:
                well.demand_status = DemandStatus(payload["value"])

        elif ov.kind == ScenarioOverrideKind.PO_ARRIVAL:
            po = session.get(InventoryOnOrder, ov.target_id)
            if po is not None:
                po.expected_date = _parse_date(payload.get("value"))

        elif ov.kind == ScenarioOverrideKind.HARD_RELEASE:
            assignment = session.get(InventoryAssignment, ov.target_id)
            if assignment is not None:
                session.delete(assignment)

        elif ov.kind == ScenarioOverrideKind.APPROVAL_FLIP:
            approval = session.get(SubstitutionApproval, ov.target_id)
            if approval is not None:
                if "customer_approved" in payload:
                    approval.customer_approved = payload["customer_approved"]
                if "well_approved" in payload:
                    approval.well_approved = payload["well_approved"]
                if "status" in payload:
                    approval.status = SubstitutionApprovalStatus(payload["status"])

    session.flush()


def _coverage_verdict_counts(db: Session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in db.query(CoverageResult).all():
        counts[r.verdict.value] = counts.get(r.verdict.value, 0) + 1
    return counts


def _mrp_runouts(db: Session) -> dict[str, dict]:
    ledgers = mrp_rows(db)
    return {
        f"{l.bu_id}:{l.product_id}": {
            "unit": l.unit.value,
            "runout_months": dict(l.runout_months),
        }
        for l in ledgers
    }


def preview_scenario(db: Session, overrides: list[ScenarioOverride], sections: set[str]) -> dict:
    """spec E-3 / invariant 3: never writes to `db`. All overrides (any kind)
    are applied to an in-memory COPY; the SAME engines run on both the real
    session (before / stored) and the copy (after / recomputed)."""
    result: dict = {}

    with make_inmemory_copy(db) as copy_session:
        apply_overrides(copy_session, overrides)

        if "coverage" in sections:
            before = _coverage_verdict_counts(db)
            coverage_engine.recompute_all(copy_session)
            after = _coverage_verdict_counts(copy_session)
            result["coverage"] = {"before": before, "after": after}

        if "mrp" in sections:
            before = _mrp_runouts(db)
            after = _mrp_runouts(copy_session)
            result["mrp"] = {"before": before, "after": after}

    return result


class SupplyOverrideRejected(Exception):
    """Raised by apply_scenario when supply-owned overrides are present —
    nothing has been written when this is raised (checked before any write)."""

    def __init__(self, rejected: list[dict]):
        self.rejected = rejected
        super().__init__(f"supply-owned overrides rejected: {rejected}")


def apply_scenario(db: Session, scenario: Scenario, overrides: list[ScenarioOverride]) -> list[ScenarioOverride]:
    """spec E-4. Raises SupplyOverrideRejected (nothing written) if any
    po_arrival/hard_release override is present. Otherwise writes the
    platform-owned overrides to the REAL session, records a demand revision
    per touched well, and marks the scenario Applied. Caller commits."""
    supply = [ov for ov in overrides if ov.kind in SUPPLY_OWNED_KINDS]
    if supply:
        raise SupplyOverrideRejected(
            rejected=[{"id": ov.id, "kind": ov.kind.value, "target_id": ov.target_id} for ov in supply]
        )

    apply_overrides(db, overrides)

    touched_wells: set[str] = set()
    for ov in overrides:
        if ov.kind in (ScenarioOverrideKind.QUANTITY, ScenarioOverrideKind.ROS_DATE):
            line = db.get(DemandLine, ov.target_id)
            if line is not None:
                touched_wells.add(line.well_id)
        elif ov.kind == ScenarioOverrideKind.WELL_STATUS:
            touched_wells.add(ov.target_id)

    for well_id in touched_wells:
        max_rev = (
            db.query(DemandRevision)
            .filter_by(well_id=well_id)
            .order_by(DemandRevision.revision_no.desc())
            .first()
        )
        next_rev = (max_rev.revision_no + 1) if max_rev else 1
        db.add(
            DemandRevision(
                well_id=well_id,
                revision_no=next_rev,
                source=DemandRevisionSource.MANUAL,
                summary=f"Scenario '{scenario.name}' applied",
            )
        )

    scenario.status = ScenarioStatus.APPLIED
    scenario.applied_at = datetime.now(timezone.utc)
    return overrides
