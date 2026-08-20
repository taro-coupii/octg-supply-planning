import datetime

import pytest

from app.engines.allocation import (
    InventoryScopeMissing,
    allocate_customer,
    allocate_lines,
    free_pool,
    hard_assigned_total,
)
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    CustomerOwnedInventory,
    DemandLine,
    DemandProfile,
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


_NO_BU = object()


def _customer(db, name="Cust1", bu=_NO_BU, policy=AllocationPolicy.SOFT):
    if bu is _NO_BU:
        bu = _bu(db)
    c = Customer(
        name=name,
        business_unit_id=bu.id if bu else None,
        allocation_policy=policy,
    )
    db.add(c)
    db.commit()
    return c


def _product(db, name="Casing"):
    p = Product(name=name, unit_of_measure=UnitOfMeasure.MTR)
    db.add(p)
    db.commit()
    return p


def _well(db, customer, name="Well1"):
    w = Well(customer_id=customer.id, name=name)
    db.add(w)
    db.commit()
    return w


def _line(db, well, product, quantity=10.0, unit=UnitOfMeasure.MTR, ros_date=None):
    line = DemandLine(
        well_id=well.id,
        product_id=product.id,
        quantity=quantity,
        unit=unit,
        ros_date=ros_date or datetime.date(2026, 1, 1),
        profile=DemandProfile.PRIMARY,
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
        business_unit_id=bu.id,
        product_id=product.id,
        customer_id=customer.id,
        quantity=quantity,
        unit=unit,
    )
    db.add(row)
    db.commit()
    return row


def _owned(db, customer, product, quantity, unit=UnitOfMeasure.MTR):
    row = CustomerOwnedInventory(customer_id=customer.id, product_id=product.id, quantity=quantity, unit=unit)
    db.add(row)
    db.commit()
    return row


# --- invariant 1: owned-first, never flows to another customer ---


@pytest.mark.parametrize(
    "policy", [AllocationPolicy.SOFT, AllocationPolicy.HARD, AllocationPolicy.HYBRID]
)
def test_owned_consumed_before_hard_or_free(db, policy):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=policy)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    _owned(db, cust, product, 6.0)
    _hard(db, bu, product, cust, 10.0)
    _on_hand(db, bu, product, 10.0)

    [alloc] = allocate_lines(db, cust, [line])

    assert alloc.from_owned == 6.0
    # remaining 4 comes from hard/free depending on policy, never more owned
    assert alloc.from_owned == 6.0


def test_owned_inventory_isolated_between_customers(db):
    bu = _bu(db)
    product = _product(db)
    cust1 = _customer(db, name="C1", bu=bu)
    cust2 = _customer(db, name="C2", bu=bu)
    well1 = _well(db, cust1)
    line1 = _line(db, well1, product, quantity=5.0)
    _owned(db, cust2, product, 100.0)  # belongs to cust2 only

    [alloc] = allocate_lines(db, cust1, [line1])

    assert alloc.from_owned == 0.0
    assert alloc.shortfall == 5.0


# --- invariant 2: policy semantics ---


def test_hard_policy_never_touches_free_pool(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.HARD)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    # no hard assignment for this customer, but plenty of free stock
    _on_hand(db, bu, product, 100.0)

    [alloc] = allocate_lines(db, cust, [line])

    assert alloc.from_free == 0.0
    assert alloc.from_hard == 0.0
    assert alloc.shortfall == 10.0


def test_hard_policy_consumes_hard_assignment(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.HARD)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    _hard(db, bu, product, cust, 7.0)

    [alloc] = allocate_lines(db, cust, [line])

    assert alloc.from_hard == 7.0
    assert alloc.from_free == 0.0
    assert alloc.shortfall == 3.0


def test_soft_policy_never_touches_hard_assignment(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    _hard(db, bu, product, cust, 10.0)  # hard assigned to this customer, must be ignored
    _on_hand(db, bu, product, 10.0)  # but no free stock beyond the hard-assigned amount

    [alloc] = allocate_lines(db, cust, [line])

    assert alloc.from_hard == 0.0
    assert alloc.from_free == 0.0
    assert alloc.shortfall == 10.0


def test_soft_policy_consumes_free_pool(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    _on_hand(db, bu, product, 20.0)

    [alloc] = allocate_lines(db, cust, [line])

    assert alloc.from_free == 10.0
    assert alloc.from_hard == 0.0
    assert alloc.shortfall == 0.0


def test_hybrid_policy_consumes_hard_then_free_in_order(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.HYBRID)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    _hard(db, bu, product, cust, 4.0)
    _on_hand(db, bu, product, 4.0 + 20.0)  # free = 20 after subtracting hard total

    [alloc] = allocate_lines(db, cust, [line])

    assert alloc.from_hard == 4.0
    assert alloc.from_free == 6.0
    assert alloc.shortfall == 0.0


# --- ros_date -> id ordering determinism, including same-date ties ---


def test_lines_consumed_in_ros_date_order(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    product = _product(db)
    well = _well(db, cust)
    late = _line(db, well, product, quantity=10.0, ros_date=datetime.date(2026, 6, 1))
    early = _line(db, well, product, quantity=10.0, ros_date=datetime.date(2026, 1, 1))
    _on_hand(db, bu, product, 10.0)  # only enough for one line

    results = allocate_lines(db, cust, [late, early])
    by_line_id = {r.line.id: r for r in results}

    assert by_line_id[early.id].from_free == 10.0
    assert by_line_id[early.id].shortfall == 0.0
    assert by_line_id[late.id].from_free == 0.0
    assert by_line_id[late.id].shortfall == 10.0


def test_lines_on_same_ros_date_ordered_by_id(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    product = _product(db)
    well = _well(db, cust)
    same_date = datetime.date(2026, 3, 1)
    line_a = _line(db, well, product, quantity=10.0, ros_date=same_date)
    line_b = _line(db, well, product, quantity=10.0, ros_date=same_date)
    _on_hand(db, bu, product, 10.0)

    ordered_ids = sorted([line_a.id, line_b.id])
    results = allocate_lines(db, cust, [line_b, line_a])
    by_line_id = {r.line.id: r for r in results}

    assert by_line_id[ordered_ids[0]].from_free == 10.0
    assert by_line_id[ordered_ids[1]].from_free == 0.0
    assert by_line_id[ordered_ids[1]].shortfall == 10.0


def test_pools_deplete_sequentially_across_lines(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    product = _product(db)
    well = _well(db, cust)
    line1 = _line(db, well, product, quantity=6.0, ros_date=datetime.date(2026, 1, 1))
    line2 = _line(db, well, product, quantity=6.0, ros_date=datetime.date(2026, 2, 1))
    _on_hand(db, bu, product, 10.0)

    results = allocate_lines(db, cust, [line1, line2])
    by_line_id = {r.line.id: r for r in results}

    assert by_line_id[line1.id].from_free == 6.0
    assert by_line_id[line1.id].shortfall == 0.0
    assert by_line_id[line2.id].from_free == 4.0
    assert by_line_id[line2.id].shortfall == 2.0


# --- unit mismatch ignored ---


def test_unit_mismatched_rows_are_ignored(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.HYBRID)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0, unit=UnitOfMeasure.MTR)
    _owned(db, cust, product, 100.0, unit=UnitOfMeasure.PC)
    _hard(db, bu, product, cust, 100.0, unit=UnitOfMeasure.PC)
    _on_hand(db, bu, product, 100.0, unit=UnitOfMeasure.PC)

    [alloc] = allocate_lines(db, cust, [line])

    assert alloc.from_owned == 0.0
    assert alloc.from_hard == 0.0
    assert alloc.from_free == 0.0
    assert alloc.shortfall == 10.0


# --- BU boundary ---


def test_other_bu_stock_is_invisible(db):
    bu1 = _bu(db, "BU1")
    bu2 = _bu(db, "BU2")
    product = _product(db)
    cust = _customer(db, bu=bu1, policy=AllocationPolicy.SOFT)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)
    _on_hand(db, bu2, product, 100.0)  # other BU, same product

    [alloc] = allocate_lines(db, cust, [line])

    assert alloc.from_free == 0.0
    assert alloc.shortfall == 10.0


def test_bu_less_customer_raises_inventory_scope_missing(db):
    cust = _customer(db, bu=None)
    product = _product(db)
    well = _well(db, cust)
    line = _line(db, well, product, quantity=10.0)

    with pytest.raises(InventoryScopeMissing):
        allocate_lines(db, cust, [line])


# --- free pool floor at 0 when over-assigned ---


def test_free_pool_floors_at_zero_when_hard_exceeds_on_hand(db):
    bu = _bu(db)
    product = _product(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    other = _customer(db, name="Other", bu=bu)
    _on_hand(db, bu, product, 5.0)
    _hard(db, bu, product, other, 20.0)  # over-assigned relative to on_hand

    assert free_pool(db, bu.id, product.id, UnitOfMeasure.MTR) == 0.0


def test_hard_assigned_total_sums_across_customers(db):
    bu = _bu(db)
    product = _product(db)
    cust = _customer(db, bu=bu)
    other = _customer(db, name="Other", bu=bu)
    _hard(db, bu, product, cust, 3.0)
    _hard(db, bu, product, other, 4.0)

    assert hard_assigned_total(db, bu.id, product.id, UnitOfMeasure.MTR) == 7.0


# --- allocate_customer convenience loads all lines ---


def test_allocate_customer_loads_all_lines_for_customer(db):
    bu = _bu(db)
    cust = _customer(db, bu=bu, policy=AllocationPolicy.SOFT)
    product = _product(db)
    well = _well(db, cust)
    line1 = _line(db, well, product, quantity=1.0, ros_date=datetime.date(2026, 1, 1))
    line2 = _line(db, well, product, quantity=1.0, ros_date=datetime.date(2026, 2, 1))
    _on_hand(db, bu, product, 10.0)

    results = allocate_customer(db, cust)

    assert {r.line.id for r in results} == {line1.id, line2.id}
