import datetime

from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    CoverageResult,
    CoverageVerdict,
    DemandLine,
    DemandProfile,
    DemandRevision,
    DemandRevisionSource,
    DemandStatus,
    Product,
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


def _revision(db, well, revision_no, applied_at, source=DemandRevisionSource.IMPORT, summary="s"):
    rev = DemandRevision(
        well_id=well.id,
        revision_no=revision_no,
        applied_at=applied_at,
        source=source,
        summary=summary,
    )
    db.add(rev)
    db.commit()
    return rev


def _coverage_result(db, line, verdict, computed_at=None):
    row = CoverageResult(
        demand_line_id=line.id,
        verdict=verdict,
        reason="r",
        action=None,
        covered_qty=0.0,
        covered_via=None,
        computed_at=computed_at or datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def _tech_sub(db, from_product, to_product):
    ts = TechnicalSubstitution(from_product_id=from_product.id, to_product_id=to_product.id)
    db.add(ts)
    db.commit()
    return ts


def _approval(db, customer, well, line, ts, status=SubstitutionApprovalStatus.PENDING):
    approval = SubstitutionApproval(
        customer_id=customer.id,
        well_id=well.id,
        demand_line_id=line.id,
        technical_substitution_id=ts.id,
        status=status,
        customer_approved=False,
        well_approved=False,
        requested_at=datetime.datetime.now(datetime.timezone.utc),
        decided_at=None,
    )
    db.add(approval)
    db.commit()
    return approval


def test_home_requires_auth(anon_client):
    resp = anon_client.get("/dashboard/home")
    assert resp.status_code == 401


def test_pending_union_includes_both_sources_when_only_one_side_present(client, db):
    """Invariant 1: pending union includes both sources — a pending
    SubstitutionApproval request AND a PendingApproval coverage verdict
    line that has NO corresponding approval request."""
    bu = _bu(db)
    customer = _customer(db, bu=bu)
    well = _well(db, customer)
    product_a = _product(db, name="A")
    product_b = _product(db, name="B")

    # Side 1: a pending SubstitutionApproval request (with a real coverage
    # verdict tied to a DIFFERENT line so it doesn't collide with side 2).
    line1 = _line(db, well, product_a)
    ts1 = _tech_sub(db, product_a, product_b)
    _approval(db, customer, well, line1, ts1, status=SubstitutionApprovalStatus.PENDING)

    # Side 2: a PendingApproval coverage verdict with NO approval request at all.
    line2 = _line(db, well, product_a)
    _coverage_result(db, line2, CoverageVerdict.PENDING_APPROVAL)

    resp = client.get("/dashboard/home")
    assert resp.status_code == 200
    body = resp.json()

    sources = {row["source"] for row in body["pending_union"]}
    assert sources == {"request", "verdict"}
    assert len(body["pending_union"]) == 2

    line_ids = {row["demand_line_id"] for row in body["pending_union"]}
    assert line1.id in line_ids
    assert line2.id in line_ids


def test_pending_union_excludes_verdict_line_that_has_a_request(client, db):
    """A PendingApproval verdict whose line already has a pending
    SubstitutionApproval request should appear only once (as "request"),
    not double-counted as a "verdict" row too."""
    bu = _bu(db)
    customer = _customer(db, bu=bu)
    well = _well(db, customer)
    product_a = _product(db, name="A")
    product_b = _product(db, name="B")

    line = _line(db, well, product_a)
    ts = _tech_sub(db, product_a, product_b)
    _approval(db, customer, well, line, ts, status=SubstitutionApprovalStatus.PENDING)
    _coverage_result(db, line, CoverageVerdict.PENDING_APPROVAL)

    resp = client.get("/dashboard/home")
    body = resp.json()
    assert len(body["pending_union"]) == 1
    assert body["pending_union"][0]["source"] == "request"


def test_demand_changes_newest_first_capped_at_15(client, db):
    """Invariant 2: demand changes are newest-first, capped at 15."""
    bu = _bu(db)
    customer = _customer(db, bu=bu)
    well = _well(db, customer)

    base = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    for i in range(20):
        _revision(
            db,
            well,
            revision_no=i + 1,
            applied_at=base + datetime.timedelta(days=i),
            summary=f"rev {i + 1}",
        )

    resp = client.get("/dashboard/home")
    body = resp.json()
    changes = body["demand_changes"]
    assert len(changes) == 15
    # newest first: revision 20 (last applied_at) comes first
    assert changes[0]["summary"] == "rev 20"
    assert changes[-1]["summary"] == "rev 6"
    applied_ats = [c["applied_at"] for c in changes]
    assert applied_ats == sorted(applied_ats, reverse=True)


def test_coverage_rate_excludes_not_evaluated_from_denominator(client, db):
    """Invariant 4: coverage rate's denominator excludes NotEvaluated lines."""
    bu = _bu(db)
    customer = _customer(db, bu=bu)
    well = _well(db, customer)
    product = _product(db)

    covered_line = _line(db, well, product)
    _coverage_result(db, covered_line, CoverageVerdict.COVERED)

    uncovered_line = _line(db, well, product)
    _coverage_result(db, uncovered_line, CoverageVerdict.UNCOVERED)

    # NotEvaluated: a demand line with no CoverageResult row at all.
    _line(db, well, product)

    resp = client.get("/dashboard/home")
    body = resp.json()
    coverage = body["kpi"]["coverage"]
    assert coverage["evaluated_count"] == 2
    assert coverage["not_evaluated_count"] == 1
    assert coverage["covered_count"] == 1
    assert coverage["rate"] == 0.5


def test_home_empty_state_is_honest(client, db):
    resp = client.get("/dashboard/home")
    assert resp.status_code == 200
    body = resp.json()
    assert body["demand_changes"] == []
    assert body["pending_union"] == []
    assert body["attention_wells"] == []
    assert body["kpi"]["coverage"]["evaluated_count"] == 0
    assert body["kpi"]["coverage"]["rate"] is None


def test_kpi_customer_and_well_counts(client, db):
    bu = _bu(db)
    c1 = _customer(db, name="C1", bu=bu)
    _well(db, c1, name="W1", status=DemandStatus.CONFIRMED)
    _well(db, c1, name="W2", status=DemandStatus.PLANNED)

    resp = client.get("/dashboard/home")
    body = resp.json()
    assert body["kpi"]["customer_count"] == 1
    assert body["kpi"]["well_status_counts"] == {"Confirmed": 1, "Planned": 1}


def test_attention_wells_top5_by_worst_verdict(client, db):
    bu = _bu(db)
    customer = _customer(db, bu=bu)
    product = _product(db)

    for i in range(6):
        well = _well(db, customer, name=f"Well{i}")
        line = _line(db, well, product)
        verdict = CoverageVerdict.UNRECOVERABLE if i == 0 else CoverageVerdict.UNCOVERED
        _coverage_result(db, line, verdict)

    resp = client.get("/dashboard/home")
    body = resp.json()
    attention = body["attention_wells"]
    assert len(attention) == 5
    # worst (Unrecoverable) should be included/sorted first
    assert attention[0]["worst_verdict"] == "Unrecoverable"
