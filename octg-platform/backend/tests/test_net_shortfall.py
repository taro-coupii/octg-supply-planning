"""F04 -- the MRP figure is the NET shortfall, with its breakdown.

Review reproduction: a 5,000 line against 4,000 on hand. Coverage drew the
4,000 (a partial draw is real under SOFT) and left the line Uncovered; MRP then
recommended ordering 5,000, so the 4,000 was counted twice -- as consumed stock
and as steel to buy. Ruling 2026-09-06: the net 1,000 is the figure, shown with
what was drawn from each tier.
"""

from app.engines.coverage import recompute_well
from app.engines.mrp import mrp_summary
from app.engines.scenario import preview
from app.models import CoverageResult, CoverageStatus
from tests.test_mrp_engine import _customer, _lead_times, _line, _product, _well
from tests.test_scenario_engine import _demand_override, _scenario


def _world(db, on_hand=4000, demand=5000):
    customer, node = _customer(db)
    product = _product(db, on_hand_qty=on_hand)
    _lead_times(db)
    well = _well(db, node, "Well Net-01")
    line = _line(db, well, product, quantity=demand, days_out=500)
    recompute_well(db, well)
    return customer, well, line, product


def test_5000_against_4000_is_a_1000_shortfall(db_session):
    _customer_, well, line, product = _world(db_session)

    stored = db_session.get(CoverageResult, line.id)
    assert stored.status == CoverageStatus.UNCOVERED
    assert (stored.demand_quantity, stored.drawn_company, stored.residual) == (5000, 4000, 1000)
    assert stored.drawn_customer_owned == 0 and stored.drawn_substitute == 0

    rows = mrp_summary(db_session)
    assert len(rows) == 1
    assert rows[0].quantity == 1000
    assert rows[0].demand_quantity == 5000
    assert rows[0].drawn_company == 4000
    assert rows[0].whole_line_ids == []
    assert "1000 net shortfall" in rows[0].reason
    assert "5000 demanded, 4000 already drawn" in rows[0].reason


def test_covered_line_has_zero_residual(db_session):
    _customer_, well, line, product = _world(db_session, on_hand=6000)
    stored = db_session.get(CoverageResult, line.id)
    assert stored.status == CoverageStatus.COVERED
    assert (stored.drawn_company, stored.residual) == (5000, 0)
    assert mrp_summary(db_session) == []


def test_a_verdict_without_net_figures_is_counted_whole_and_says_so(db_session):
    """A row computed before the columns existed: MRP must not invent a draw."""
    _customer_, well, line, product = _world(db_session)
    stored = db_session.get(CoverageResult, line.id)
    stored.residual = None
    stored.demand_quantity = None
    stored.drawn_company = None
    db_session.flush()

    rows = mrp_summary(db_session)
    assert rows[0].quantity == 5000
    assert rows[0].whole_line_ids == [line.id]
    assert "counted whole" in rows[0].reason


def test_preview_reads_the_same_net_position_as_the_stored_path(db_session):
    """Scenario preview computes MRP from its own pass, not from CoverageResult;
    it must reach the same net figure, and move it when demand moves."""
    customer, well, line, product = _world(db_session)
    scenario = _scenario(db_session, customer, "Cut demand")
    _demand_override(db_session, scenario, line, "quantity", number=4500)

    impact = preview(db_session, scenario)
    change = next(c for c in impact.mrp_changes if c.product_id == product.id)
    assert change.quantity_before == 1000
    assert change.quantity_after == 500
