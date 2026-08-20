import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    AllocationPolicy,
    Customer,
    CoverageResult,
    CoverageVerdict,
    DemandLine,
    DemandProfile,
    Product,
    SubstitutionApproval,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    UnitOfMeasure,
    Well,
)


def _customer(db, name="Cust1"):
    c = Customer(name=name)
    db.add(c)
    db.commit()
    return c


def _product(db, name="Casing"):
    p = Product(name=name, unit_of_measure=UnitOfMeasure.MTR)
    db.add(p)
    db.commit()
    return p


def _well(db, customer=None, name="Well1"):
    customer = customer or _customer(db)
    w = Well(customer_id=customer.id, name=name)
    db.add(w)
    db.commit()
    return w


def _demand_line(db, well=None, product=None):
    well = well or _well(db)
    product = product or _product(db)
    line = DemandLine(
        well_id=well.id,
        product_id=product.id,
        quantity=10.0,
        unit=UnitOfMeasure.MTR,
        ros_date=datetime.date(2026, 1, 1),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.commit()
    return line


def _technical_substitution(db, from_product=None, to_product=None):
    from_product = from_product or _product(db, "From")
    to_product = to_product or _product(db, "To")
    ts = TechnicalSubstitution(from_product_id=from_product.id, to_product_id=to_product.id)
    db.add(ts)
    db.commit()
    return ts


# --- customers.allocation_policy ---


def test_customer_allocation_policy_defaults_soft(db):
    c = _customer(db)
    assert c.allocation_policy == AllocationPolicy.SOFT


def test_customer_allocation_policy_values_match_frontend_enum():
    assert AllocationPolicy.SOFT.value == "SOFT"
    assert AllocationPolicy.HARD.value == "HARD"
    assert AllocationPolicy.HYBRID.value == "HYBRID"


def test_customer_allocation_policy_can_be_set_explicitly(db):
    c = Customer(name="Cust2", allocation_policy=AllocationPolicy.HARD)
    db.add(c)
    db.commit()
    assert c.allocation_policy == AllocationPolicy.HARD


# --- coverage_results ---


def test_coverage_result_unique_demand_line_id(db):
    line = _demand_line(db)
    db.add(
        CoverageResult(
            demand_line_id=line.id,
            verdict=CoverageVerdict.COVERED,
            reason="fully covered",
            covered_qty=10.0,
            computed_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    db.commit()
    db.add(
        CoverageResult(
            demand_line_id=line.id,
            verdict=CoverageVerdict.COVERED,
            reason="fully covered again",
            covered_qty=10.0,
            computed_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_coverage_result_foreign_key_enforced(db):
    db.add(
        CoverageResult(
            demand_line_id="nonexistent",
            verdict=CoverageVerdict.UNCOVERED,
            reason="no line",
            covered_qty=0.0,
            computed_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_coverage_result_verdict_values_match_spec():
    assert CoverageVerdict.COVERED.value == "Covered"
    assert CoverageVerdict.COVERED_VIA_SUBSTITUTE.value == "CoveredViaSubstitute"
    assert CoverageVerdict.PENDING_APPROVAL.value == "PendingApproval"
    assert CoverageVerdict.UNCOVERED.value == "Uncovered"
    assert CoverageVerdict.UNRECOVERABLE.value == "Unrecoverable"


def test_coverage_result_action_and_covered_via_nullable(db):
    line = _demand_line(db)
    cr = CoverageResult(
        demand_line_id=line.id,
        verdict=CoverageVerdict.COVERED,
        reason="fully covered",
        action=None,
        covered_qty=10.0,
        covered_via=None,
        computed_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(cr)
    db.commit()
    assert cr.action is None
    assert cr.covered_via is None


def test_coverage_result_row_deletable_and_replaceable(db):
    line = _demand_line(db)
    cr = CoverageResult(
        demand_line_id=line.id,
        verdict=CoverageVerdict.UNCOVERED,
        reason="short by 5",
        covered_qty=5.0,
        computed_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(cr)
    db.commit()
    db.delete(cr)
    db.commit()
    assert db.query(CoverageResult).filter_by(demand_line_id=line.id).one_or_none() is None

    replacement = CoverageResult(
        demand_line_id=line.id,
        verdict=CoverageVerdict.COVERED,
        reason="now fully covered",
        covered_qty=10.0,
        computed_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(replacement)
    db.commit()
    assert db.query(CoverageResult).filter_by(demand_line_id=line.id).one().verdict == CoverageVerdict.COVERED


# --- substitution_approvals ---


def test_substitution_approval_status_default_pending(db):
    cust = _customer(db)
    well = _well(db, customer=cust)
    line = _demand_line(db, well=well)
    ts = _technical_substitution(db)
    sa = SubstitutionApproval(
        customer_id=cust.id,
        well_id=well.id,
        demand_line_id=line.id,
        technical_substitution_id=ts.id,
        customer_approved=False,
        well_approved=False,
        requested_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(sa)
    db.commit()
    assert sa.status == SubstitutionApprovalStatus.PENDING
    assert sa.decided_at is None
    assert sa.note is None


def test_substitution_approval_status_values():
    assert SubstitutionApprovalStatus.PENDING.value == "Pending"
    assert SubstitutionApprovalStatus.APPROVED.value == "Approved"
    assert SubstitutionApprovalStatus.REJECTED.value == "Rejected"


def test_substitution_approval_foreign_keys_enforced(db):
    db.add(
        SubstitutionApproval(
            customer_id="nonexistent",
            well_id="nonexistent",
            demand_line_id="nonexistent",
            technical_substitution_id="nonexistent",
            customer_approved=False,
            well_approved=False,
            requested_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_substitution_approval_can_be_decided(db):
    cust = _customer(db)
    well = _well(db, customer=cust)
    line = _demand_line(db, well=well)
    ts = _technical_substitution(db)
    sa = SubstitutionApproval(
        customer_id=cust.id,
        well_id=well.id,
        demand_line_id=line.id,
        technical_substitution_id=ts.id,
        customer_approved=True,
        well_approved=True,
        status=SubstitutionApprovalStatus.APPROVED,
        requested_at=datetime.datetime.now(datetime.timezone.utc),
        decided_at=datetime.datetime.now(datetime.timezone.utc),
        note="approved by both",
    )
    db.add(sa)
    db.commit()
    assert sa.status == SubstitutionApprovalStatus.APPROVED
    assert sa.decided_at is not None
    assert sa.note == "approved by both"
