import datetime

from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    CustomerSubstitutionRule,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryAssignment,
    InventoryOnHand,
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


def test_candidates_basic_shape(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    p_from = _product(db, "Casing A")
    p_to = _product(db, "Casing B")
    well = _well(db, cust)
    line = _line(db, well, p_from, quantity=10.0)
    ts = _sub(db, p_from, p_to)
    _rule(db, cust, ts, allowed=True)
    _on_hand(db, bu, p_to, 50.0)

    resp = client.get(f"/substitution/candidates?demand_line_id={line.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["substitution_id"] == ts.id
    assert row["to_product"] == "Casing B"
    assert row["technical_ok"] is True
    assert row["customer_rule_allowed"] is True
    assert row["free_qty_by_unit"] == 50.0
    assert row["hard_assigned_qty"] == 0.0
    assert row["verdict_if_applied"] is True
    assert row["blocked_by"] is None


def test_candidates_blocked_by_customer_when_rule_disallowed(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    p_from = _product(db, "Casing A")
    p_to = _product(db, "Casing B")
    well = _well(db, cust)
    line = _line(db, well, p_from, quantity=10.0)
    ts = _sub(db, p_from, p_to)
    _rule(db, cust, ts, allowed=False)
    _on_hand(db, bu, p_to, 50.0)

    resp = client.get(f"/substitution/candidates?demand_line_id={line.id}")
    body = resp.json()
    row = body[0]
    assert row["customer_rule_allowed"] is False
    assert row["blocked_by"] == "customer"


def test_candidates_blocked_by_customer_when_no_rule(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    p_from = _product(db, "Casing A")
    p_to = _product(db, "Casing B")
    well = _well(db, cust)
    line = _line(db, well, p_from, quantity=10.0)
    ts = _sub(db, p_from, p_to)
    _on_hand(db, bu, p_to, 50.0)

    resp = client.get(f"/substitution/candidates?demand_line_id={line.id}")
    row = resp.json()[0]
    assert row["customer_rule_allowed"] is False
    assert row["blocked_by"] == "customer"


def test_candidates_blocked_by_well_approval_when_pending(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    p_from = _product(db, "Casing A")
    p_to = _product(db, "Casing B")
    well = _well(db, cust)
    line = _line(db, well, p_from, quantity=10.0)
    ts = _sub(db, p_from, p_to)
    _rule(db, cust, ts, allowed=True)
    _on_hand(db, bu, p_to, 50.0)
    _approval(db, cust, well, line, ts, status=SubstitutionApprovalStatus.PENDING)

    resp = client.get(f"/substitution/candidates?demand_line_id={line.id}")
    row = resp.json()[0]
    assert row["blocked_by"] == "well-approval"


def test_candidates_blocked_by_oracle_release_when_free_insufficient_but_hard_available(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    other = _customer(db, name="Cust2", bu=bu)
    p_from = _product(db, "Casing A")
    p_to = _product(db, "Casing B")
    well = _well(db, cust)
    line = _line(db, well, p_from, quantity=10.0)
    ts = _sub(db, p_from, p_to)
    _rule(db, cust, ts, allowed=True)
    # on_hand 20, all 20 hard-assigned to other customer -> free_pool = 0, hard_assigned = 20
    _on_hand(db, bu, p_to, 20.0)
    _hard(db, bu, p_to, other, 20.0)

    resp = client.get(f"/substitution/candidates?demand_line_id={line.id}")
    row = resp.json()[0]
    assert row["free_qty_by_unit"] == 0.0
    assert row["hard_assigned_qty"] == 20.0
    assert row["verdict_if_applied"] is False
    assert row["blocked_by"] == "oracle-release"


def test_candidates_null_when_would_cover_and_no_blocks(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    p_from = _product(db, "Casing A")
    p_to = _product(db, "Casing B")
    well = _well(db, cust)
    line = _line(db, well, p_from, quantity=10.0)
    ts = _sub(db, p_from, p_to)
    _rule(db, cust, ts, allowed=True)
    _on_hand(db, bu, p_to, 100.0)

    resp = client.get(f"/substitution/candidates?demand_line_id={line.id}")
    row = resp.json()[0]
    assert row["blocked_by"] is None
    assert row["verdict_if_applied"] is True


def test_candidates_use_full_line_set_shortfall_not_isolated(client, db):
    bu = _bu(db)
    cust = _customer(db, bu=bu)
    p_from = _product(db, "Casing A")
    p_to = _product(db, "Casing B")
    well = _well(db, cust)
    # line1 (earlier ros_date) drains all of product A's free stock first.
    line1 = _line(db, well, p_from, quantity=10.0, ros_date=datetime.date(2026, 1, 1))
    line2 = _line(db, well, p_from, quantity=10.0, ros_date=datetime.date(2026, 2, 1))
    ts = _sub(db, p_from, p_to)
    _rule(db, cust, ts, allowed=True)
    _on_hand(db, bu, p_from, 10.0)  # only enough for line1
    _on_hand(db, bu, p_to, 5.0)  # not enough to cover line2's full 10 shortfall

    resp = client.get(f"/substitution/candidates?demand_line_id={line2.id}")
    row = resp.json()[0]
    # line2's real shortfall is 10 (all of product A went to line1), so 5 free
    # substitute stock is NOT enough -> must not report verdict_if_applied True.
    assert row["verdict_if_applied"] is False


def test_candidates_404_for_missing_line(client, db):
    resp = client.get("/substitution/candidates?demand_line_id=nope")
    assert resp.status_code == 404
