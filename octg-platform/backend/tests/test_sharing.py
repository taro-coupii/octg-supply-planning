import datetime

from app.engines.coverage import recompute_customer
from app.engines.sharing import sharing_analysis
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    CoverageResult,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryAssignment,
    InventoryOnHand,
    Product,
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


def _row_counts(db):
    return {
        "coverage_results": db.query(CoverageResult).count(),
        "on_hand": db.query(InventoryOnHand).count(),
        "assignments": db.query(InventoryAssignment).count(),
        "demand_lines": db.query(DemandLine).count(),
    }


def test_sharing_peer_releasable_covers_line(db):
    bu = _bu(db)
    target = _customer(db, "Target", bu=bu, policy=AllocationPolicy.SOFT)
    peer = _customer(db, "Peer", bu=bu, policy=AllocationPolicy.HARD)
    product = _product(db)

    well_t = _well(db, target)
    line_t = _line(db, well_t, product, quantity=10.0)
    # target has no stock at all -> Unrecoverable
    recompute_customer(db, target.id)

    # peer has demand and a hard assignment they could theoretically release
    well_p = _well(db, peer)
    _line(db, well_p, product, quantity=5.0)
    _on_hand(db, bu, product, 20.0)
    _hard(db, bu, product, peer, 20.0)
    recompute_customer(db, peer.id)

    result = sharing_analysis(db, target.id)
    assert len(result) == 1
    entry = result[0]
    assert entry["line"]["id"] == line_t.id
    assert entry["official_verdict"] in ("Unrecoverable", "Uncovered")
    peers = entry["peers"]
    assert len(peers) == 1
    assert peers[0]["customer_name"] == "Peer"
    # peer allocated 5 from hard, 0 from free -> releasable 5
    assert peers[0]["releasable_qty_by_unit"] == 5.0
    assert peers[0]["would_cover"] is False  # 5 < 10 shortfall


def test_sharing_owned_never_shared(db):
    bu = _bu(db)
    target = _customer(db, "Target", bu=bu)
    peer = _customer(db, "Peer", bu=bu)
    product = _product(db)
    well_t = _well(db, target)
    _line(db, well_t, product, quantity=10.0)
    recompute_customer(db, target.id)

    from app.models import CustomerOwnedInventory

    well_p = _well(db, peer)
    _line(db, well_p, product, quantity=3.0)
    db.add(CustomerOwnedInventory(customer_id=peer.id, product_id=product.id, quantity=100.0, unit=UnitOfMeasure.MTR))
    db.commit()
    recompute_customer(db, peer.id)

    result = sharing_analysis(db, target.id)
    peers = result[0]["peers"]
    # peer's owned stock (100) must never appear as releasable
    assert peers[0]["releasable_qty_by_unit"] == 0.0


def test_sharing_bu_isolation(db):
    bu1 = _bu(db, "BU1")
    bu2 = _bu(db, "BU2")
    target = _customer(db, "Target", bu=bu1)
    other_bu_peer = _customer(db, "OtherBU", bu=bu2)
    product = _product(db)
    well_t = _well(db, target)
    _line(db, well_t, product, quantity=10.0)
    recompute_customer(db, target.id)

    well_p = _well(db, other_bu_peer)
    _line(db, well_p, product, quantity=5.0)
    _on_hand(db, bu2, product, 50.0)
    recompute_customer(db, other_bu_peer.id)

    result = sharing_analysis(db, target.id)
    peer_names = [p["customer_name"] for p in result[0]["peers"]]
    assert "OtherBU" not in peer_names


def test_sharing_only_covers_uncovered_and_unrecoverable_lines(db):
    bu = _bu(db)
    target = _customer(db, "Target", bu=bu)
    product = _product(db)
    well_t = _well(db, target)
    _line(db, well_t, product, quantity=10.0)
    _on_hand(db, bu, product, 100.0)  # fully covered
    recompute_customer(db, target.id)

    result = sharing_analysis(db, target.id)
    assert result == []


def test_sharing_does_not_write_to_db(db):
    bu = _bu(db)
    target = _customer(db, "Target", bu=bu)
    peer = _customer(db, "Peer", bu=bu)
    product = _product(db)
    well_t = _well(db, target)
    _line(db, well_t, product, quantity=10.0)
    recompute_customer(db, target.id)

    well_p = _well(db, peer)
    _line(db, well_p, product, quantity=5.0)
    _on_hand(db, bu, product, 20.0)
    recompute_customer(db, peer.id)

    before = _row_counts(db)
    sharing_analysis(db, target.id)
    after = _row_counts(db)
    assert before == after


def test_sharing_unknown_customer_returns_none(db):
    from app.engines.sharing import sharing_analysis as sa

    assert sa(db, "nope") is None


def test_sharing_api_unknown_customer_404(client, db):
    resp = client.get("/analysis/sharing?customer_id=nope")
    assert resp.status_code == 404


def test_sharing_api_returns_shape(client, db):
    bu = _bu(db)
    target = _customer(db, "Target", bu=bu)
    peer = _customer(db, "Peer", bu=bu)
    product = _product(db)
    well_t = _well(db, target)
    _line(db, well_t, product, quantity=10.0)
    recompute_customer(db, target.id)

    well_p = _well(db, peer)
    _line(db, well_p, product, quantity=5.0)
    _on_hand(db, bu, product, 20.0)
    recompute_customer(db, peer.id)

    resp = client.get(f"/analysis/sharing?customer_id={target.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["official_verdict"] in ("Uncovered", "Unrecoverable")
    assert body[0]["peers"][0]["customer_name"] == "Peer"
