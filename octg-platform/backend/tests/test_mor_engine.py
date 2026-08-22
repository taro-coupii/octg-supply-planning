"""Material Order Requirements engine (app.engines.mor).

What these pin: the incremental netting math (cells sum to the workbook's
Ttl. Order RQMT), the calendar lead-time offset, and the three refusals --
unknown position is not 0, unmodelled lead time gives no order-by date, and
undated POs are never netted.
"""

from datetime import date, datetime, timedelta

from app.engines.mor import order_requirements
from app.models import InventoryOnOrder, UnitOfMeasure

from tests.test_executive_inventory_utilisation import (
    _bu,
    _customer,
    _customer_owned,
    _line,
    _on_hand,
    _product,
    _well,
)

TODAY = date(2026, 8, 15)


def _grid(db, **kw):
    return order_requirements(db, today=TODAY, **kw)


def _world(db, on_hand=1000, quantities=(600, 600), days=(45, 105)):
    """One product, demand lines in the months after TODAY."""
    bu = _bu(db)
    customer, node = _customer(db, bu, "Acme")
    product = _product(db, "P-MOR")
    _on_hand(db, bu, product, on_hand)
    well = _well(db, node, "WELL-M")
    for qty, d in zip(quantities, days):
        _line(db, well, product, qty, days_out=d)
    db.commit()
    return bu, customer, product, well


def test_incremental_requirements_sum_to_total_shortfall(db_session):
    # 1000 on hand vs 600 + 600: the second month is short exactly 200.
    _world(db_session)
    row = _grid(db_session).rows[0]
    assert row.available and row.order_flag
    assert row.total_order_requirement == 200.0
    reqs = [c.order_requirement for c in row.cells if c.order_requirement > 0]
    assert reqs == [200.0]
    # The shortfall bites in the month of the SECOND line, not spread around.
    biting = [c for c in row.cells if c.order_requirement > 0][0]
    assert biting.demand == 600.0
    assert sum(c.order_requirement for c in row.cells) == row.total_order_requirement


def test_covered_product_has_no_flag(db_session):
    _world(db_session, on_hand=5000)
    row = _grid(db_session).rows[0]
    assert row.available and not row.order_flag
    assert row.total_order_requirement == 0.0
    assert row.first_order_by is None


def test_order_by_is_lead_time_months_earlier_and_late_when_past(db_session):
    """The seeded lead-time components are absent here, so build the offset
    from the row's own reported lead time when modelled; when NOT modelled the
    engine must refuse the order-by date rather than guess."""
    _world(db_session, on_hand=0)
    row = _grid(db_session).rows[0]
    assert row.order_flag
    # No LeadTimeComponent rows exist in this world -> not modelled.
    assert not row.lead_time_modelled
    assert row.first_order_by is None
    assert not row.already_late
    for cell in row.cells:
        assert cell.order_by_month is None


def test_unknown_position_is_an_unavailable_row_not_zero(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "NoStock Co")
    product = _product(db_session, "P-UNKNOWN")
    well = _well(db_session, node, "WELL-U")
    _line(db_session, well, product, 500, days_out=60)
    db_session.commit()

    grid = _grid(db_session)
    assert grid.unavailable_count == 1
    row = [r for r in grid.rows if r.product_id == product.id][0]
    assert not row.available
    assert row.position is None and row.cells == ()
    assert "UNKNOWN" in row.reason
    # The demand is still stated -- the row exists to make the gap visible.
    assert row.total_demand == 500.0


def test_customer_owned_stock_reduces_the_requirement(db_session):
    bu, customer, product, well = _world(db_session, on_hand=1000)
    _customer_owned(db_session, customer, product, 200)
    db_session.commit()
    row = _grid(db_session).rows[0]
    # 1000 company + 200 customer-owned covers the 1200 of demand exactly.
    assert row.total_order_requirement == 0.0


def test_dated_arrivals_net_undated_do_not(db_session):
    bu, customer, product, well = _world(db_session, on_hand=1000)
    db_session.add(
        InventoryOnOrder(
            business_unit_id=bu.id,
            product_id=product.id,
            quantity=150,
            expected_arrival_date=datetime.combine(TODAY, datetime.min.time())
            + timedelta(days=20),
            source_system="synthetic",
        )
    )
    db_session.add(
        InventoryOnOrder(
            business_unit_id=bu.id,
            product_id=product.id,
            quantity=999,
            expected_arrival_date=None,
            source_system="synthetic",
        )
    )
    db_session.commit()
    row = _grid(db_session).rows[0]
    # 1000 + 150 dated against 1200 demand -> 50 short. The undated 999 must
    # NOT have been netted (it would wrongly zero the requirement).
    assert row.total_order_requirement == 50.0


def test_customer_scope_narrows_the_grid(db_session):
    bu, customer, product, well = _world(db_session)
    other_customer, other_node = _customer(db_session, bu, "Other Co")
    p2 = _product(db_session, "P-OTHER")
    _on_hand(db_session, bu, p2, 0)
    w2 = _well(db_session, other_node, "WELL-O")
    _line(db_session, w2, p2, 100, days_out=60)
    db_session.commit()

    all_rows = {r.product_id for r in _grid(db_session).rows}
    scoped = {r.product_id for r in _grid(db_session, customer_id=customer.id).rows}
    assert p2.id in all_rows
    assert scoped == {product.id}


def test_safety_stock_triggers_requirement_below_the_level(db_session):
    """1000 on hand vs 1200 demand would be short 200; a 300 safety stock
    raises the requirement to 500 -- the balance must never dip below it."""
    from app.models import SafetyStock

    bu, customer, product, well = _world(db_session, on_hand=1000)
    db_session.add(SafetyStock(product_id=product.id, quantity=300))
    db_session.commit()

    row = _grid(db_session).rows[0]
    assert row.safety_stock == 300
    assert row.total_order_requirement == 500.0


def test_no_safety_stock_row_reports_none_not_zero(db_session):
    _world(db_session, on_hand=5000)
    row = _grid(db_session).rows[0]
    assert row.safety_stock is None


def test_customer_filter_nets_against_that_bu_only(db_session):
    """A customer-filtered grid must not net against another BU's stock or
    another customer's owned steel -- the two ownership walls the platform
    enforces everywhere else. The unfiltered grid keeps the system-wide MRP
    stance (and says so in its notes)."""
    bu, customer, product, _well = _world(db_session, on_hand=500)
    # A SECOND BU holding plenty of the same product, and a foreign customer
    # in it owning a parcel. Neither may serve the first customer's demand.
    other_bu = _bu(db_session, "Other BU")
    _on_hand(db_session, other_bu, product, 100000)
    other_customer, _node = _customer(db_session, other_bu, "Foreign")
    _customer_owned(db_session, other_customer, product, 5000)
    db_session.commit()

    scoped = _grid(db_session, customer_id=customer.id).rows[0]
    assert scoped.available
    # 500 on hand in the customer's own BU vs 1200 demand: short by 700.
    assert scoped.total_order_requirement == 700.0
    assert scoped.position.on_hand == 500.0

    # The unfiltered grid deliberately sums every BU (documented stance).
    system_wide = _grid(db_session).rows[0]
    assert system_wide.position.on_hand == 100500.0
    assert any("every Business Unit" in n for n in _grid(db_session).notes)
    assert any(
        "that customer's Business Unit" in n
        for n in _grid(db_session, customer_id=customer.id).notes
    )


def test_customer_filter_missing_bu_row_degrades_not_raises(db_session):
    """Regression: the BU-scoped position raise used a name mrp.py never
    imported, so this exact case 500ed the whole grid with a NameError
    instead of degrading to an unavailable row."""
    bu, customer, product, well = _world(db_session, on_hand=500)
    # A SECOND product demanded by the same customer, with stock in ANOTHER
    # BU only -- so the customer-scoped position for it is UNKNOWN.
    other_bu = _bu(db_session, "Elsewhere BU")
    orphan_product = _product(db_session, "P-ELSEWHERE")
    _on_hand(db_session, other_bu, orphan_product, 9999)
    _line(db_session, well, orphan_product, 300, days_out=60)
    db_session.commit()

    grid = _grid(db_session, customer_id=customer.id)
    row = next(r for r in grid.rows if r.product_id == orphan_product.id)
    assert row.available is False
    assert "UNKNOWN" in (row.reason or "")
    # The healthy product's row is unaffected -- one unknown must not kill
    # the grid (the C-07 lesson, restated for the filtered path).
    healthy = next(r for r in grid.rows if r.product_id == product.id)
    assert healthy.available is True


def test_overdue_demand_folds_into_first_month_and_is_labelled(db_session):
    """Overdue demand counts (2026-08-12 decision) and is named on the row.

    A line 60 days in the past lands in the grid's first month -- its shortfall
    bites immediately -- and the folded quantity is reported as
    total_overdue_demand rather than silently blended into total_demand.
    """
    _world(db_session, on_hand=500, quantities=(600, 300), days=(-60, 45))
    grid = _grid(db_session)
    row = grid.rows[0]
    assert row.total_demand == 900
    assert row.total_overdue_demand == 600
    # 500 opening against 600 in month one: short 100 immediately.
    assert row.cells[0].demand == 600
    assert row.cells[0].order_requirement == 100
    assert any("overdue" in n.lower() for n in grid.notes)


def test_no_overdue_demand_reports_zero(db_session):
    _world(db_session, on_hand=2000, quantities=(600,), days=(45,))
    row = _grid(db_session).rows[0]
    assert row.total_overdue_demand == 0.0
