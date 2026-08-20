import datetime

from app.engines.coverage import _scope, judge_customer, recompute_all, recompute_customer
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    CoverageResult,
    CoverageVerdict,
    CustomerSubstitutionRule,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryAssignment,
    InventoryOnHand,
    Product,
    Setting,
    SubstitutionApproval,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    UnitOfMeasure,
    Well,
)


def _bu(db, name="BU1"):
    bu = BusinessUnit(name=name)
    db.add(bu)
    db.commit()
    return bu


def _customer(db, name="Cust1", bu=None, policy=AllocationPolicy.SOFT):
    c = Customer(name=name, business_unit_id=bu.id if bu else None, allocation_policy=policy)
    db.add(c)
    db.commit()
    return c


def _product(db, name="Casing"):
    p = Product(name=name, unit_of_measure=UnitOfMeasure.MTR)
    db.add(p)
    db.commit()
    return p


def _well(db, customer, name="Well1", status=DemandStatus.CONFIRMED):
    w = Well(customer_id=customer.id, name=name, demand_status=status)
    db.add(w)
    db.commit()
    return w


def _line(db, well, product, quantity=10.0, unit=UnitOfMeasure.MTR, ros_date=None, profile=DemandProfile.PRIMARY):
    line = DemandLine(
        well_id=well.id,
        product_id=product.id,
        quantity=quantity,
        unit=unit,
        ros_date=ros_date or datetime.date(2026, 1, 1),
        profile=profile,
    )
    db.add(line)
    db.commit()
    return line


def _on_hand(db, bu, product, quantity, unit=UnitOfMeasure.MTR):
    row = InventoryOnHand(business_unit_id=bu.id, product_id=product.id, quantity=quantity, unit=unit)
    db.add(row)
    db.commit()
    return row


def _hard(db, bu, product, customer, quantity, unit=UnitOfMeasure.MTR):
    row = InventoryAssignment(
        business_unit_id=bu.id, product_id=product.id, customer_id=customer.id, quantity=quantity, unit=unit
    )
    db.add(row)
    db.commit()
    return row


def _sub(db, from_product, to_product):
    ts = TechnicalSubstitution(from_product_id=from_product.id, to_product_id=to_product.id)
    db.add(ts)
    db.commit()
    return ts


def _rule(db, customer, ts, allowed=True):
    r = CustomerSubstitutionRule(customer_id=customer.id, technical_substitution_id=ts.id, allowed=allowed)
    db.add(r)
    db.commit()
    return r


def _approval(db, customer, well, line, ts, status=SubstitutionApprovalStatus.PENDING):
    a = SubstitutionApproval(
        customer_id=customer.id,
        well_id=well.id,
        demand_line_id=line.id,
        technical_substitution_id=ts.id,
        status=status,
        customer_approved=(status == SubstitutionApprovalStatus.APPROVED),
        well_approved=(status == SubstitutionApprovalStatus.APPROVED),
        requested_at=datetime.datetime.now(datetime.timezone.utc),
        decided_at=(
            datetime.datetime.now(datetime.timezone.utc)
            if status != SubstitutionApprovalStatus.PENDING
            else None
        ),
    )
    db.add(a)
    db.commit()
    return a


def _result_for(db, line):
    return db.query(CoverageResult).filter_by(demand_line_id=line.id).one()


# --- invariant 3: all five verdicts reproduced with reason/action ---


def test_verdict_covered(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    _on_hand(db, bu, product, 20.0)

    recompute_customer(db, cust.id)

    result = _result_for(db, line)
    assert result.verdict == CoverageVerdict.COVERED
    assert result.action is None
    assert "Need 10 Mtr" in result.reason
    assert "fully covered" in result.reason


def test_verdict_covered_via_substitute(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    from_product = _product(db, "From")
    to_product = _product(db, "To")
    well = _well(db, cust)
    line = _line(db, well, from_product, quantity=10.0)
    ts = _sub(db, from_product, to_product)
    _rule(db, cust, ts, allowed=True)
    _on_hand(db, bu, to_product, 10.0)
    _approval(db, cust, well, line, ts, status=SubstitutionApprovalStatus.APPROVED)

    recompute_customer(db, cust.id)

    result = _result_for(db, line)
    assert result.verdict == CoverageVerdict.COVERED_VIA_SUBSTITUTE
    assert result.action is None
    assert to_product.name in result.reason
    assert result.covered_via is not None
    assert to_product.id in result.covered_via


def test_verdict_pending_approval(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    from_product = _product(db, "From")
    to_product = _product(db, "To")
    well = _well(db, cust)
    line = _line(db, well, from_product, quantity=10.0)
    ts = _sub(db, from_product, to_product)
    _rule(db, cust, ts, allowed=True)
    _on_hand(db, bu, to_product, 10.0)
    _approval(db, cust, well, line, ts, status=SubstitutionApprovalStatus.PENDING)

    recompute_customer(db, cust.id)

    result = _result_for(db, line)
    assert result.verdict == CoverageVerdict.PENDING_APPROVAL
    assert "pending" in result.reason.lower()


def test_verdict_uncovered_hard_release(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    other = _customer(db, name="Other", bu=bu)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    _hard(db, bu, product, other, 10.0)  # no on-hand beyond hard-assigned

    recompute_customer(db, cust.id)

    result = _result_for(db, line)
    assert result.verdict == CoverageVerdict.UNCOVERED
    assert result.action == "Release the hard assignment in Oracle"


def test_verdict_uncovered_request_substitution(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    from_product = _product(db, "From")
    to_product = _product(db, "To")
    well = _well(db, cust)
    line = _line(db, well, from_product, quantity=10.0)
    ts = _sub(db, from_product, to_product)
    _rule(db, cust, ts, allowed=True)
    _on_hand(db, bu, to_product, 10.0)  # sufficient free substitute stock, no request made

    recompute_customer(db, cust.id)

    result = _result_for(db, line)
    assert result.verdict == CoverageVerdict.UNCOVERED
    assert result.action == "Request substitution approval"


def test_verdict_unrecoverable(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    # no stock anywhere, no substitutes

    recompute_customer(db, cust.id)

    result = _result_for(db, line)
    assert result.verdict == CoverageVerdict.UNRECOVERABLE
    assert result.action == "Order from mill"


# --- scope filtering ---


def test_scope_excludes_planned_well_under_default_confirmed_only_scope(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust, status=DemandStatus.PLANNED)
    line = _line(db, well, product, quantity=10.0)

    recompute_customer(db, cust.id)

    assert db.query(CoverageResult).filter_by(demand_line_id=line.id).one_or_none() is None


def test_scope_respects_configured_statuses_and_profiles(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    db.add(Setting(key="coverage_scope_statuses", value="Planned"))
    db.add(Setting(key="coverage_scope_profiles", value="Contingency"))
    db.commit()

    well_confirmed = _well(db, cust, name="W1", status=DemandStatus.CONFIRMED)
    line_confirmed_primary = _line(db, well_confirmed, product, profile=DemandProfile.PRIMARY)

    well_planned = _well(db, cust, name="W2", status=DemandStatus.PLANNED)
    line_planned_contingency = _line(db, well_planned, product, profile=DemandProfile.CONTINGENCY)
    line_planned_primary = _line(db, well_planned, product, profile=DemandProfile.PRIMARY)

    recompute_customer(db, cust.id)

    assert db.query(CoverageResult).filter_by(demand_line_id=line_confirmed_primary.id).one_or_none() is None
    assert db.query(CoverageResult).filter_by(demand_line_id=line_planned_primary.id).one_or_none() is None
    assert db.query(CoverageResult).filter_by(demand_line_id=line_planned_contingency.id).one_or_none() is not None


# --- invariant 4: computed_at advances on same-value recompute ---


def test_computed_at_advances_on_same_value_recompute(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    _on_hand(db, bu, product, 10.0)

    recompute_customer(db, cust.id)
    first = _result_for(db, line).computed_at

    recompute_customer(db, cust.id)
    second = _result_for(db, line).computed_at

    assert second > first
    # verdict is unchanged, but computed_at still moved forward
    assert _result_for(db, line).verdict == CoverageVerdict.COVERED


# --- invariant 5: per-customer failure isolation in recompute_all ---


def test_recompute_all_isolates_bu_less_customer(db):
    bu = _bu(db)
    good = _customer(db, name="Good", bu=bu)
    bad = _customer(db, name="Bad", bu=None)
    product = _product(db)
    well_good = _well(db, good)
    line_good = _line(db, well_good, product, quantity=10.0)
    _on_hand(db, bu, product, 10.0)

    well_bad = _well(db, bad)
    _line(db, well_bad, product, quantity=5.0)

    outcome = recompute_all(db)

    assert outcome["computed"] == 1
    assert len(outcome["skipped_customers"]) == 1
    assert outcome["skipped_customers"][0][0] == "Bad"
    assert _result_for(db, line_good).verdict == CoverageVerdict.COVERED


# --- invariant 8: only the engine writes CoverageResult ---


def test_no_api_module_imports_coverage_result_for_direct_writes():
    import ast
    import pathlib

    api_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "api"
    for path in api_dir.glob("*.py"):
        if path.name == "coverage.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CoverageResult":
                raise AssertionError(f"{path} directly constructs CoverageResult")


# --- invariant 9: NotEvaluated is not 0 or Covered ---


def test_lines_without_coverage_result_are_not_evaluated(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    # never recomputed

    assert db.query(CoverageResult).filter_by(demand_line_id=line.id).one_or_none() is None


# --- substitute pool depletion across lines in one recompute (bug fix) ---


def test_substitute_pool_depletes_across_lines_same_recompute(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    from_product = _product(db, "From")
    to_product = _product(db, "To")
    well = _well(db, cust)
    line1 = _line(db, well, from_product, quantity=10.0, ros_date=datetime.date(2026, 1, 1))
    line2 = _line(db, well, from_product, quantity=10.0, ros_date=datetime.date(2026, 2, 1))
    ts = _sub(db, from_product, to_product)
    _rule(db, cust, ts, allowed=True)
    _on_hand(db, bu, to_product, 10.0)  # enough for only ONE line's shortfall
    _approval(db, cust, well, line1, ts, status=SubstitutionApprovalStatus.APPROVED)
    _approval(db, cust, well, line2, ts, status=SubstitutionApprovalStatus.APPROVED)

    recompute_customer(db, cust.id)

    result1 = _result_for(db, line1)
    result2 = _result_for(db, line2)
    assert result1.verdict == CoverageVerdict.COVERED_VIA_SUBSTITUTE
    # line2 must NOT also claim the same depleted substitute stock
    assert result2.verdict != CoverageVerdict.COVERED_VIA_SUBSTITUTE
    assert result2.verdict == CoverageVerdict.UNRECOVERABLE


# --- rejected approval must not block re-request (bug fix) ---


def test_rejected_approval_does_not_block_request_substitution_action(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    from_product = _product(db, "From")
    to_product = _product(db, "To")
    well = _well(db, cust)
    line = _line(db, well, from_product, quantity=10.0)
    ts = _sub(db, from_product, to_product)
    _rule(db, cust, ts, allowed=True)
    _on_hand(db, bu, to_product, 10.0)
    _approval(db, cust, well, line, ts, status=SubstitutionApprovalStatus.REJECTED)

    recompute_customer(db, cust.id)

    result = _result_for(db, line)
    assert result.verdict == CoverageVerdict.UNCOVERED
    assert result.action == "Request substitution approval"


# --- full-replace semantics: line falling out of scope loses its stale row ---


def test_recompute_customer_clears_stale_result_for_line_now_out_of_scope(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    product = _product(db)
    well = _well(db, cust, status=DemandStatus.CONFIRMED)
    line = _line(db, well, product, quantity=10.0)
    _on_hand(db, bu, product, 10.0)

    recompute_customer(db, cust.id)
    assert _result_for(db, line).verdict == CoverageVerdict.COVERED

    well.demand_status = DemandStatus.PLANNED
    db.commit()

    recompute_customer(db, cust.id)
    assert db.query(CoverageResult).filter_by(demand_line_id=line.id).one_or_none() is None


# --- allocation<->substitute double-booking (bug fix) ---


def test_substitute_pool_excludes_stock_already_consumed_by_earlier_line_direct_allocation(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    product_a = _product(db, "A")
    product_b = _product(db, "B")
    well = _well(db, cust)
    # line1 needs product B directly and consumes ALL of B's free stock.
    line1 = _line(db, well, product_b, quantity=10.0, ros_date=datetime.date(2026, 1, 1))
    # line2 needs product A, short, with an approved substitute A -> B.
    line2 = _line(db, well, product_a, quantity=10.0, ros_date=datetime.date(2026, 2, 1))
    ts = _sub(db, product_a, product_b)
    _rule(db, cust, ts, allowed=True)
    _on_hand(db, bu, product_b, 10.0)  # only enough for line1's own direct need
    _approval(db, cust, well, line2, ts, status=SubstitutionApprovalStatus.APPROVED)

    recompute_customer(db, cust.id)

    result1 = _result_for(db, line1)
    result2 = _result_for(db, line2)
    assert result1.verdict == CoverageVerdict.COVERED
    # line2 must NOT be covered via substitute — B's stock was already consumed by line1.
    assert result2.verdict != CoverageVerdict.COVERED_VIA_SUBSTITUTE
    assert result2.verdict == CoverageVerdict.UNRECOVERABLE


# --- judge_customer / recompute_customer identity (ONE judge implementation) ---


def test_judge_customer_matches_recompute_customer_for_default_scope(db):
    """Stage-6 Task 2 invariant: the in-memory judge path (used by scope
    override) and the persisting recompute path must produce identical
    verdicts for the default scope — there is only one judge implementation."""
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.HYBRID)
    product_a = _product(db, "A")
    product_b = _product(db, "B")
    well = _well(db, cust)
    _line(db, well, product_a, quantity=10.0, ros_date=datetime.date(2026, 1, 1))
    line2 = _line(db, well, product_b, quantity=20.0, ros_date=datetime.date(2026, 2, 1))
    ts = _sub(db, product_b, product_a)
    _rule(db, cust, ts, allowed=True)
    _approval(db, cust, well, line2, ts, status=SubstitutionApprovalStatus.APPROVED)
    _on_hand(db, bu, product_a, 12.0)
    _on_hand(db, bu, product_b, 5.0)

    recompute_customer(db, cust.id)
    stored = {r.demand_line_id: r for r in db.query(CoverageResult).all()}

    statuses, profiles = _scope(db)
    judged = judge_customer(db, cust, statuses, profiles)

    assert len(judged) == len(stored)
    for j in judged:
        cr = stored[j.line.id]
        assert cr.verdict == j.verdict
        assert cr.reason == j.reason
        assert cr.action == j.action
        assert cr.covered_qty == j.covered_qty
        assert cr.covered_via == j.covered_via
