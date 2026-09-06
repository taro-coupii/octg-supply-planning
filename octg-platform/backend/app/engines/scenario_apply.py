"""Apply an agreed scenario TO THE BASE PLAN -- the one path that writes.

Deliberately a separate module from `app.engines.scenario`
--------------------------------------------------------
`app.engines.scenario` is a read-only projection and its first line of defence is
that it does not import anything that persists (layer 1 of the same four-layer
pattern `app.engines.sharing` uses). That guarantee is only worth something if it
is structural, and it stops being structural the moment `apply_revision` appears
among the preview module's imports -- after which "the preview cannot write" rests
on nobody calling the wrong function.

So the write lives here instead. This module imports `app.engines.scenario` (to
compute the promise it is about to keep) but never the other way round.

Demand changes go through the revision machinery, never around it
----------------------------------------------------------------
Demand overrides are applied by calling `app.engines.coverage.apply_revision` (a
line's quantity / ROS / profile) or `app.engines.coverage.set_well_demand_status`
(a well's demand status, cascaded to every line of that well). Those are the ONLY
sanctioned ways to change demand. Each appends a `DemandRevision`, bumps
`current_revision_no`, recomputes coverage and writes an `ImpactRecord`. Mutating `DemandLine.quantity` directly would be shorter and
would break three things at once: the spec's full-revision-history requirement,
the Home Dashboard's "Demand Changes" card (which reads `ImpactRecord`), and the
audit trail that makes a customer agreement defensible six months later.

ONE revision per demand line, not one per override
--------------------------------------------------
A `DemandRevision` is a SNAPSHOT of all four demand fields, not a diff of one. A
scenario that changes both the quantity and the ROS of one line therefore
produces ONE revision carrying both, not two revisions of which the first records
a state that never existed in the plan.

Supply overrides are REFUSED, with the reason stated
----------------------------------------------------
See `apply_to_base_plan`. This is the honest handling of a system boundary, not
an unimplemented feature.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.engines.coverage import (
    apply_revision,
    recompute_customer,
    set_well_demand_status,
)
from app.engines.overrides import ScenarioOverrides
from app.engines.scenario import (
    ScenarioImmutable,
    apply_blockers,
    assert_mutable,
    preview,
)
from app.engines.substitution import (
    decide_approval,
    effective_approval,
    request_approval,
)
from app.models import (
    DemandStatus,
    ScenarioStatus,
    ScenarioTargetKind,
    SubstitutionApprovalStatus,
    WellSubstitutionApproval,
)


class ScenarioNotApplicable(RuntimeError):
    """Raised when a scenario cannot be applied to the base plan.

    Distinct from `ScenarioImmutable`: that one means "already applied", this one
    means "contains something we must not write". The API maps both to 409.
    """


@dataclass(frozen=True)
class ApplyResult:
    """What applying actually did. Frozen scalars -- comparable with a preview.

    `line_status_after` and `well_status_after` are the coverage answer AFTER the
    write, in the same shape `app.engines.scenario.ScenarioImpact` reports its
    `status_after` values. That is what lets a caller (and the test suite) assert
    the promise was kept rather than take it on trust.
    """

    scenario_id: str
    scenario_name: str
    applied_at: datetime
    revised_demand_line_ids: tuple[str, ...] = ()
    demand_revision_ids: tuple[str, ...] = ()
    impact_record_ids: tuple[str, ...] = ()
    decided_approval_ids: tuple[str, ...] = ()
    #: ((demand_line_id, coverage_status), ...) sorted by line id.
    line_status_after: tuple[tuple[str, str], ...] = ()
    #: ((well_id, coverage_status or None), ...) sorted by well id.
    well_status_after: tuple[tuple[str, str | None], ...] = ()
    notes: tuple[str, ...] = ()


def apply_to_base_plan(
    db: Session, scenario, *, actor_user_id: str | None = None
) -> ApplyResult:
    """Write `scenario`'s overrides into production data and recompute coverage.

    Guarded, in this order:

      1. `assert_mutable` -- an APPLIED scenario is immutable and terminal.
      2. `apply_blockers` -- refuses SUPPLY overrides (see below) and any
         approval override that would rewrite a DECIDED approval (F06).

    Supply overrides and the Oracle system boundary
    ----------------------------------------------
    `InventoryOnHand` and `InventoryAssignment` are documented read-only LOCAL
    PROJECTIONS of Oracle-owned data, and purchase orders have no model here at
    all. Applying a supply override would mean writing to something this platform
    does not own.

    The decision: **preview them, refuse to apply them.** They are fully modelled
    in `app.engines.scenario.preview` -- "what if mill delivery accelerates?" is
    one of the spec's own examples and a planner must be able to ask it -- but
    `apply_to_base_plan` refuses, names the offending overrides, and says what to
    do instead (raise the change in Oracle, then re-preview against synced data).

    The rejected alternative was to write the value locally with a
    "pending Oracle reconciliation" marker. It reads as pragmatic and it is
    worse. A local value would be authoritative for coverage until the next
    Oracle sync silently reverted it, so the platform would show a covered well
    that Oracle's data does not support, and the planner -- having watched the
    apply succeed -- would have no reason to doubt it. A refusal is visible at
    exactly the moment the decision is made; a silent revert is discovered by a
    customer. If a genuine local-override capability is wanted later it needs its
    own table with its own provenance and precedence rules, decided with the
    Oracle integration in view, not a nullable column smuggled into a projection.

    The refusal is ALL-OR-NOTHING. Applying the demand half of an agreed scenario
    and dropping the supply half would leave the base plan in a state nobody
    agreed to, and the scenario marked Applied as though it had all landed.

    `actor_user_id` is the authenticated user applying the scenario (F09); it is
    recorded on every approval this apply raises or decides.

    Returns an `ApplyResult`; raises before writing anything if either guard trips.
    """
    assert_mutable(scenario)

    blockers = apply_blockers(scenario)
    if blockers:
        raise ScenarioNotApplicable(
            f"Scenario {scenario.name!r} cannot be applied to the base plan:\n  - "
            + "\n  - ".join(blockers)
        )

    customer = scenario.customer

    # The promise, computed BEFORE anything is written and by the same code the
    # editor showed the planner. Kept so the notes can state what was expected;
    # the assertion that it was kept is the test suite's job (test_scenario_engine
    # ::test_apply_matches_preview), because a mismatch is a bug in the engine
    # rather than something a runtime guard could sensibly recover from.
    promised = preview(db, scenario)

    resolver = ScenarioOverrides(scenario.overrides, customer)
    notes: list[str] = []

    # ---- 1. Substitution approvals ---------------------------------------
    # Done FIRST so the demand recomputes below already see the decided
    # approvals, and so a scenario containing only approval overrides still ends
    # with a recompute (see step 3).
    #
    # Unlike inventory, WellSubstitutionApproval is OURS: the three-layer
    # substitution model is this platform's own, the approval is a decision made
    # in this platform, and there is no Oracle owner to conflict with. So an
    # approval override applies cleanly.
    decided: list[str] = []
    # Declared up here because step 1b below also appends to them.
    revised: list[str] = []
    revision_ids: list[str] = []
    impact_ids: list[str] = []
    for override in scenario.overrides:
        if override.target_kind != ScenarioTargetKind.SUBSTITUTION_APPROVAL:
            continue
        line = override.target_demand_line
        target = SubstitutionApprovalStatus(override.value_text)
        # The from-side may be left implicit; a substitution is always FROM the
        # line's own product, so that is the only value it could take.
        from_product_id = override.target_from_product_id or line.product_id

        # The row the VERDICT reads (newest, but Approved wins), not merely the
        # newest row -- so this writes against the same row coverage looks at.
        approval = effective_approval(
            db, line.id, from_product_id, override.target_to_product_id
        )
        if approval is not None and approval.status != SubstitutionApprovalStatus.PENDING:
            # A decided approval is FINAL (F06). `apply_blockers` above already
            # refused the two rewrites (re-open to Pending, Reject an Approved),
            # so what is left is a restatement (no-op) or Approving a Rejected
            # pair, which is a NEW superseding request rather than an edit.
            if target == approval.status:
                notes.append(
                    f"Substitution approval {approval.id} was already "
                    f"{approval.status.value}; the scenario restates it, nothing "
                    "was changed."
                )
                continue
            approval = request_approval(
                db, line, from_product_id, override.target_to_product_id,
                actor_user_id=actor_user_id,
            )
            notes.append(
                f"Substitution approval {approval.id} supersedes an earlier "
                "Rejected decision; the earlier row is kept as history."
            )
        elif approval is None:
            # The common case: the planner asked "what if this were approved?"
            # about a pair nobody had even requested. Applying the agreement means
            # the request now exists AND has been decided, so create the request
            # through the normal engine rather than fabricating a row.
            approval = request_approval(
                db, line, from_product_id, override.target_to_product_id,
                actor_user_id=actor_user_id,
            )

        if target == SubstitutionApprovalStatus.PENDING:
            # Only reachable for a row that IS pending (or was just created):
            # nothing to decide, and re-opening a decided row was refused above.
            db.flush()
        else:
            decide_approval(
                db,
                approval.id,
                approved=(target == SubstitutionApprovalStatus.APPROVED),
                actor_user_id=actor_user_id,
            )
        decided.append(approval.id)

    # ---- 1b. Well demand status ------------------------------------------
    # BEFORE the per-line revisions, deliberately. `apply_revision` snapshots the
    # well's CURRENT status into the revision it writes, so doing the status change
    # first means a line that is also having its quantity revised gets ONE coherent
    # history: revision N carries the new status for every line of the well, and
    # revision N+1 carries the new quantity alongside that same new status. The
    # other order would record the new quantity against the OLD status and then
    # immediately supersede it -- a state that never existed in the plan.
    #
    # This goes through `set_well_demand_status`, the only sanctioned writer of the
    # column, for the same reason demand goes through `apply_revision`: it cascades
    # a revision to every line of the well and writes the ImpactRecords the Home
    # Dashboard reads. Assigning `well.demand_status` here would be shorter and
    # would leave every line's history claiming the old status.
    status_changed_well_ids: list[str] = []
    for override in scenario.overrides:
        if override.target_kind != ScenarioTargetKind.WELL:
            continue
        well = override.target_well
        change = set_well_demand_status(
            db, well, DemandStatus(override.value_text)
        )
        if change.unchanged:
            notes.append(
                f"Well {well.name!r} was left alone: this scenario's demand-status "
                f"override is {change.status_after}, which the well already is, so "
                "no revision was created."
            )
            continue
        status_changed_well_ids.append(well.id)
        notes.append(
            f"Well {well.name!r} demand status {change.status_before} -> "
            f"{change.status_after}, cascaded as a revision to "
            f"{change.revised_line_count} demand line(s) of that well."
        )
        revision_ids.extend(change.revision_ids)
        impact_ids.extend(change.impact_record_ids)

    # ---- 2. Demand revisions ---------------------------------------------
    # Grouped per line so each line gets ONE revision carrying every overridden
    # field, not one revision per field. The effective values come from the SAME
    # resolver the preview used, so what is written is exactly what was shown.
    demand_line_ids = sorted(
        {
            o.target_demand_line_id
            for o in scenario.overrides
            if o.target_kind == ScenarioTargetKind.DEMAND_LINE
        }
    )
    for line_id in demand_line_ids:
        override_row = next(
            o
            for o in scenario.overrides
            if o.target_kind == ScenarioTargetKind.DEMAND_LINE
            and o.target_demand_line_id == line_id
        )
        line = override_row.target_demand_line
        view = resolver.view(line)
        if not view.overridden:
            # Every overridden field happens to equal the current value. Writing a
            # revision would append history recording a change of nothing.
            notes.append(
                f"Demand line {line_id} was left alone: its override(s) match the "
                "values already in the base plan, so no revision was created."
            )
            continue

        before_rev = line.current_revision_no
        impact = apply_revision(
            db,
            line,
            quantity=view.quantity,
            ros_date=view.ros_date,
            profile=view.profile,
        )
        revised.append(line_id)
        impact_ids.append(impact.id)
        revision_ids.extend(
            r.id for r in line.revisions if r.revision_no > before_rev
        )

    # ---- 3. Final recompute ----------------------------------------------
    # Unconditional. `apply_revision` recomputes for each revised line's customer,
    # but a scenario whose only overrides were substitution approvals would
    # otherwise never trigger one, and a scenario with no applicable overrides at
    # all still deserves a consistent answer. Recompute is idempotent, so the
    # extra pass costs a pass and buys the guarantee.
    computed = recompute_customer(db, customer)

    applied_at = datetime.utcnow()
    scenario.status = ScenarioStatus.APPLIED
    scenario.applied_at = applied_at
    scenario.updated_at = applied_at
    scenario.version = (scenario.version or 1) + 1
    db.flush()

    notes.append(
        "Applied to the base plan. This scenario is now an immutable record of "
        "what was agreed and cannot be edited or applied again."
    )
    if promised.changed_well_count:
        notes.append(
            f"The preview promised {promised.changed_well_count} well "
            f"coverage change(s) and {promised.changed_line_count} demand line "
            "change(s)."
        )

    return ApplyResult(
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        applied_at=applied_at,
        revised_demand_line_ids=tuple(revised),
        demand_revision_ids=tuple(revision_ids),
        impact_record_ids=tuple(impact_ids),
        decided_approval_ids=tuple(decided),
        line_status_after=tuple(
            (line_id, verdict.status.value)
            for line_id, verdict in sorted(computed.by_line.items())
        ),
        well_status_after=tuple(sorted(computed.well_status.items())),
        notes=tuple(notes),
    )


__all__ = [
    "ApplyResult",
    "ScenarioNotApplicable",
    "ScenarioImmutable",
    "apply_to_base_plan",
]
