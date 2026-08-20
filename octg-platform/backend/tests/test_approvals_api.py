import datetime

from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    CustomerSubstitutionRule,
    DemandLine,
    DemandProfile,
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


def _setup(db, rule_allowed=True):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    p_from = _product(db, "Casing A")
    p_to = _product(db, "Casing B")
    well = _well(db, cust)
    line = _line(db, well, p_from)
    ts = _sub(db, p_from, p_to)
    if rule_allowed is not None:
        _rule(db, cust, ts, allowed=rule_allowed)
    return cust, well, line, ts


def test_create_approval_success(client, db):
    cust, well, line, ts = _setup(db)
    resp = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "Pending"
    assert body["customer_approved"] is False
    assert body["well_approved"] is False
    assert body["decided_at"] is None

    row = db.query(SubstitutionApproval).filter_by(id=body["id"]).one()
    assert row.customer_id == cust.id
    assert row.well_id == well.id


def test_create_approval_duplicate_pending_409(client, db):
    cust, well, line, ts = _setup(db)
    r1 = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    assert r2.status_code == 409


def test_create_approval_rejected_does_not_block_new_request(client, db):
    cust, well, line, ts = _setup(db)
    r1 = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    approval_id = r1.json()["id"]
    dec = client.post(
        f"/substitution-approvals/{approval_id}/decide",
        json={"customer_approved": True, "well_approved": False},
    )
    assert dec.status_code == 200
    assert dec.json()["status"] == "Rejected"

    r2 = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    assert r2.status_code == 201


def test_create_approval_rule_disallowed_422(client, db):
    cust, well, line, ts = _setup(db, rule_allowed=False)
    resp = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    assert resp.status_code == 422


def test_create_approval_no_rule_422(client, db):
    cust, well, line, ts = _setup(db, rule_allowed=None)
    resp = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    assert resp.status_code == 422


def test_decide_both_true_approved(client, db):
    cust, well, line, ts = _setup(db)
    r1 = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    approval_id = r1.json()["id"]
    dec = client.post(
        f"/substitution-approvals/{approval_id}/decide",
        json={"customer_approved": True, "well_approved": True},
    )
    assert dec.status_code == 200
    body = dec.json()
    assert body["status"] == "Approved"
    assert body["decided_at"] is not None


def test_decide_any_false_rejected(client, db):
    cust, well, line, ts = _setup(db)
    r1 = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    approval_id = r1.json()["id"]
    dec = client.post(
        f"/substitution-approvals/{approval_id}/decide",
        json={"customer_approved": False, "well_approved": True},
    )
    assert dec.status_code == 200
    assert dec.json()["status"] == "Rejected"


def test_redecide_after_decided_409(client, db):
    cust, well, line, ts = _setup(db)
    r1 = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    approval_id = r1.json()["id"]
    client.post(
        f"/substitution-approvals/{approval_id}/decide",
        json={"customer_approved": True, "well_approved": True},
    )
    dec2 = client.post(
        f"/substitution-approvals/{approval_id}/decide",
        json={"customer_approved": True, "well_approved": True},
    )
    assert dec2.status_code == 409


def test_decide_missing_404(client, db):
    resp = client.post(
        "/substitution-approvals/nope/decide",
        json={"customer_approved": True, "well_approved": True},
    )
    assert resp.status_code == 404


def test_list_approvals_joins_names_and_filters_status(client, db):
    cust, well, line, ts = _setup(db)
    r1 = client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    approval_id = r1.json()["id"]

    resp = client.get("/substitution-approvals?status=Pending")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["id"] == approval_id
    assert row["customer_name"] == cust.name
    assert row["well_name"] == well.name
    assert row["to_product_name"] == "Casing B"

    client.post(
        f"/substitution-approvals/{approval_id}/decide",
        json={"customer_approved": True, "well_approved": True},
    )

    resp2 = client.get("/substitution-approvals?status=Pending")
    assert resp2.json() == []

    resp3 = client.get("/substitution-approvals?status=Approved")
    assert len(resp3.json()) == 1


def test_list_approvals_no_filter_returns_all(client, db):
    cust, well, line, ts = _setup(db)
    client.post(
        "/substitution-approvals",
        json={"demand_line_id": line.id, "technical_substitution_id": ts.id},
    )
    resp = client.get("/substitution-approvals")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
