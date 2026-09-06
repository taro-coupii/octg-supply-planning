"""F06 -- a decided well-layer approval is immutable AT THE SERVICE LAYER.

Before this, the only thing standing between a decided approval and a rewrite was
a pre-check in one API endpoint; `scenario_apply` went round it and flipped an
Approved row back to Pending, clearing `decided_at`, so the coverage recomputed on
top of that verdict was silently orphaned. The rule now lives in
`decide_approval` (the sole writer) and `apply_blockers` names the rewrite before
anything is written.
"""

import pytest

from app.engines.coverage import recompute_customer
from app.engines.scenario import preview
from app.engines.scenario_apply import ScenarioNotApplicable, apply_to_base_plan
from app.engines.substitution import (
    ApprovalAlreadyDecided,
    decide_approval,
    request_approval,
)
from app.models import (
    CoverageStatus,
    CustomerSubstitutionRule,
    ScenarioStatus,
    ScenarioTargetKind,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    WellSubstitutionApproval,
)
from tests.test_scenario_engine import (
    _bu,
    _customer,
    _line,
    _override,
    _product,
    _scenario,
    _stock,
    _well,
)


def _world(db):
    bu = _bu(db)
    primary = _product(db, grade="13CR80")
    substitute = _product(db, grade="13CR110")
    _stock(db, bu, primary, 0)
    _stock(db, bu, substitute, 9000)
    customer, node = _customer(db, bu=bu)
    well = _well(db, node, "W-Final")
    line = _line(db, well, primary, quantity=4000)
    db.add(TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id))
    db.add(
        CustomerSubstitutionRule(
            customer_id=customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db.flush()
    recompute_customer(db, customer)
    return customer, line, primary, substitute


def _approval_override(db, scenario, line, substitute, status_text):
    return _override(
        db, scenario,
        target_kind=ScenarioTargetKind.SUBSTITUTION_APPROVAL,
        target_demand_line_id=line.id,
        target_to_product_id=substitute.id,
        field_name="approval_status", value_text=status_text,
    )


def test_engine_refuses_to_redecide(db_session):
    customer, line, primary, substitute = _world(db_session)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)
    decided_at = approval.decided_at

    with pytest.raises(ApprovalAlreadyDecided, match="final"):
        decide_approval(db_session, approval.id, approved=False)

    assert approval.status == SubstitutionApprovalStatus.APPROVED
    assert approval.decided_at == decided_at


def test_scenario_cannot_reopen_a_decided_approval(db_session):
    """The exact hole the review found: apply used to set status=Pending and
    decided_at=None on an Approved row."""
    customer, line, primary, substitute = _world(db_session)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)
    recompute_customer(db_session, customer)
    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE
    decided_at = approval.decided_at

    scenario = _scenario(db_session, customer, "Not so sure any more")
    _approval_override(db_session, scenario, line, substitute, "Pending")

    impact = preview(db_session, scenario)
    assert impact.applicable is False
    assert any("already decided (Approved)" in b for b in impact.apply_blockers)

    with pytest.raises(ScenarioNotApplicable, match="already decided"):
        apply_to_base_plan(db_session, scenario)

    db_session.refresh(approval)
    assert approval.status == SubstitutionApprovalStatus.APPROVED
    assert approval.decided_at == decided_at
    assert scenario.status == ScenarioStatus.DRAFT
    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE


def test_scenario_cannot_reject_an_approved_pair(db_session):
    customer, line, primary, substitute = _world(db_session)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)

    scenario = _scenario(db_session, customer, "Take it back")
    _approval_override(db_session, scenario, line, substitute, "Rejected")

    with pytest.raises(ScenarioNotApplicable, match="cannot be set to Rejected"):
        apply_to_base_plan(db_session, scenario)
    assert db_session.query(WellSubstitutionApproval).count() == 1
    assert approval.status == SubstitutionApprovalStatus.APPROVED


def test_scenario_restating_the_same_verdict_is_a_noop(db_session):
    customer, line, primary, substitute = _world(db_session)
    approval = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, approval.id, approved=True)
    decided_at = approval.decided_at

    scenario = _scenario(db_session, customer, "Same again")
    _approval_override(db_session, scenario, line, substitute, "Approved")

    result = apply_to_base_plan(db_session, scenario)

    assert result.decided_approval_ids == ()
    assert any("restates it" in n for n in result.notes)
    assert db_session.query(WellSubstitutionApproval).count() == 1
    assert approval.decided_at == decided_at


def test_scenario_approving_a_rejected_pair_supersedes_with_a_new_row(db_session):
    """Reversal = a NEW decision. The Rejected row stays as history, untouched."""
    customer, line, primary, substitute = _world(db_session)
    rejected = request_approval(db_session, line, primary.id, substitute.id)
    decide_approval(db_session, rejected.id, approved=False)
    rejected_at = rejected.decided_at

    scenario = _scenario(db_session, customer, "Customer came round")
    _approval_override(db_session, scenario, line, substitute, "Approved")

    result = apply_to_base_plan(db_session, scenario)

    rows = db_session.query(WellSubstitutionApproval).all()
    assert len(rows) == 2
    assert rejected.status == SubstitutionApprovalStatus.REJECTED
    assert rejected.decided_at == rejected_at
    new = next(r for r in rows if r.id != rejected.id)
    assert new.status == SubstitutionApprovalStatus.APPROVED
    assert result.decided_approval_ids == (new.id,)
    assert line.coverage_result.status == CoverageStatus.COVERED_VIA_SUBSTITUTE


def test_api_decision_on_decided_row_is_409_and_unchanged():
    from tests.phase5_fixtures import build_client

    client, session_factory, world = build_client()
    with session_factory() as db:
        from app.models import DemandLine

        line = db.get(DemandLine, world.l2_id)
        approval = request_approval(db, line, world.p_b_id, world.p_a_id)
        db.commit()
        approval_id = approval.id

    r = client.post(f"/substitution-approvals/{approval_id}/decision", json={"approved": True})
    assert r.status_code == 200, r.text
    r = client.post(f"/substitution-approvals/{approval_id}/decision", json={"approved": False})
    assert r.status_code == 409
    assert "final" in r.json()["detail"]
    with session_factory() as db:
        row = db.get(WellSubstitutionApproval, approval_id)
        assert row.status == SubstitutionApprovalStatus.APPROVED
        assert row.decided_at is not None
