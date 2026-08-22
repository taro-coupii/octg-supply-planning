"""Metric-tonnes headlines across the Executive Dashboard blocks.

The MT figures are DISPLAY-LAYER conversions (MVP_COMPROMISES.md C-05) layered
beside the native figures, which stay the ground truth. What these tests pin:

  * each block's `*_tonnes` converts exactly the quantities its native figures
    sum (checked against an INDEPENDENT hand conversion, not by calling
    `to_metric_tonnes` back);
  * a NULL `Product.weight` makes the MT total a FLOOR with a reason -- never a
    fabricated 0 t -- while the native breakdown is untouched;
  * the MT headline exists for a MIXED-UNIT scope, where the native scalar is
    rightly refused -- that is the whole point of adding it;
  * partition invariants hold in MT space (by_status sums to the total,
    channels sum to the horizon) -- the outside-the-implementation checks
    HANDOFF §8's lesson calls for.
"""

from app.engines.coverage import recompute_customer
from app.engines.executive import (
    coverage_summary,
    demand_trend,
    first_runout_detail,
    soft_allocation_coverage,
    supply_risk,
)
from app.models import UnitOfMeasure

from tests.test_executive_inventory_utilisation import (
    _bu,
    _customer,
    _line,
    _on_hand,
    _product,
    _well,
)

# Independent of app.engines.units on purpose: a bug in its constants must FAIL
# these tests, not be mirrored by them.
_LB_TO_KG = 0.453592
_M_PER_FT = 0.3048


def _mtr_tonnes(quantity_m: float, weight_lb_per_ft: float) -> float:
    return quantity_m / _M_PER_FT * weight_lb_per_ft * _LB_TO_KG / 1000.0


def _world(db, *, second_unit=UnitOfMeasure.MTR, second_weight=53.5):
    """One BU, one customer, two products, two demand lines, ample stock."""
    bu = _bu(db)
    customer, node = _customer(db, bu, "Acme")
    p_a = _product(db, "P-A", weight=53.5)
    p_b = _product(db, "P-B", unit=second_unit, weight=second_weight)
    _on_hand(db, bu, p_a, 10_000)
    _on_hand(db, bu, p_b, 10_000)
    well = _well(db, node, "WELL-1")
    _line(db, well, p_a, 100)
    _line(db, well, p_b, 40)
    db.commit()
    recompute_customer(db, customer)
    db.commit()
    return bu, customer, p_a, p_b


def test_demand_trend_tonnes_matches_a_hand_conversion(db_session):
    _world(db_session)
    trend = demand_trend(db_session)
    horizon = trend.horizons[0]
    assert horizon.current_tonnes.available
    expected = _mtr_tonnes(100, 53.5) + _mtr_tonnes(40, 53.5)
    assert abs(horizon.current_tonnes.value - expected) < 1e-6
    # Fully convertible -> an exact total, not a floor.
    assert horizon.current_tonnes.reason is None


def test_mixed_unit_horizon_gets_an_mt_headline_where_native_refuses(db_session):
    """The reason the MT layer exists: MTR + MT demand has no native scalar
    (unit_of_measure is None), but tonnes ARE one unit, so the MT headline is
    available -- and MT-unit quantities pass through unconverted."""
    _world(db_session, second_unit=UnitOfMeasure.MT, second_weight=None)
    horizon = demand_trend(db_session).horizons[0]
    assert horizon.unit_of_measure is None  # native scalar refused
    assert len(horizon.quantities_by_unit) == 2  # native breakdown intact
    assert horizon.current_tonnes.available
    assert abs(horizon.current_tonnes.value - (_mtr_tonnes(100, 53.5) + 40)) < 1e-6


def test_null_weight_makes_the_mt_total_a_floor_not_a_zero(db_session):
    _world(db_session, second_weight=None)
    horizon = demand_trend(db_session).horizons[0]
    assert horizon.current_tonnes.available
    # Only the weighted product contributes; the weightless one is EXCLUDED
    # (not added as 0 t) and the reason says so.
    assert abs(horizon.current_tonnes.value - _mtr_tonnes(100, 53.5)) < 1e-6
    assert "excluded" in horizon.current_tonnes.reason
    # The native per-unit breakdown still counts BOTH lines -- no leak of the
    # conversion gap into the ground truth.
    assert sum(u.quantity for u in horizon.quantities_by_unit) == 140


def test_coverage_tonnes_partition_and_pct(db_session):
    _world(db_session)
    summary = coverage_summary(db_session)
    assert summary.total_tonnes.available
    expected_total = _mtr_tonnes(140, 53.5)
    assert abs(summary.total_tonnes.value - expected_total) < 1e-6
    # Everything is covered (ample stock), so covered == total and pct == 100.
    assert abs(summary.covered_tonnes.value - expected_total) < 1e-6
    assert summary.coverage_pct_tonnes.available
    assert abs(summary.coverage_pct_tonnes.value - 100.0) < 1e-6
    # Invariant: the by_status MT buckets partition the MT total exactly.
    bucket_sum = sum(
        b.tonnes.value for b in summary.by_status if b.tonnes.available
    )
    assert abs(bucket_sum - summary.total_tonnes.value) < 1e-6


def test_coverage_pct_tonnes_refused_when_a_total_is_partial(db_session):
    """A percentage of a FLOOR would mix conversion gaps into the ratio."""
    _world(db_session, second_weight=None)
    summary = coverage_summary(db_session)
    assert summary.total_tonnes.available
    assert summary.total_tonnes.reason is not None  # partial -> floor
    assert not summary.coverage_pct_tonnes.available


def test_supply_risk_zero_unrecoverable_is_a_measured_zero_tonnes(db_session):
    _world(db_session)
    risk = supply_risk(db_session)
    assert risk.unrecoverable_tonnes.available
    assert risk.unrecoverable_tonnes.value == 0.0


def test_soft_allocation_channels_sum_to_the_horizon_in_mt(db_session):
    _world(db_session)
    allocation = soft_allocation_coverage(db_session)
    assert allocation.total_tonnes.available
    channel_sum = sum(
        c.tonnes.value for c in allocation.channels if c.tonnes.available
    )
    assert abs(channel_sum - allocation.total_tonnes.value) < 1e-6
    assert abs(allocation.total_tonnes.value - _mtr_tonnes(140, 53.5)) < 1e-6


def test_first_runout_shortfall_tonnes(db_session):
    """A well short of steel reports its shortfall in MT too."""
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Short Co")
    product = _product(db_session, "P-SHORT", weight=53.5)
    _on_hand(db_session, bu, product, 0)
    well = _well(db_session, node, "WELL-DRY")
    _line(db_session, well, product, 200)
    db_session.commit()
    recompute_customer(db_session, customer)
    db_session.commit()

    detail = first_runout_detail(db_session)
    assert detail.available and detail.wells
    row = detail.wells[0]
    assert row.shortfall_tonnes.available
    assert abs(row.shortfall_tonnes.value - _mtr_tonnes(200, 53.5)) < 1e-6
