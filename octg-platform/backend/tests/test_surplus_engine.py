"""Surplus List engine: the decomposition identity and the two classifications."""

from app.engines.surplus import SURPLUS_HORIZON_MONTHS, surplus_report

from tests.test_executive_inventory_utilisation import (
    _bu,
    _customer,
    _line,
    _on_hand,
    _product,
    _well,
)


def test_decomposition_sums_to_on_hand_exactly(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Acme")
    p = _product(db_session, "P-LIVE")
    _on_hand(db_session, bu, p, 5000)
    well = _well(db_session, node, "WELL-1")
    _line(db_session, well, p, 2000, days_out=60)
    db_session.commit()

    report = surplus_report(db_session)
    row = report.rows[0]
    assert row.allocated == 2000
    assert row.surplus == 3000
    assert row.obsolete == 0
    assert row.allocated + row.surplus + row.obsolete == row.on_hand


def test_no_demand_in_horizon_is_obsolete_not_surplus(db_session):
    bu = _bu(db_session)
    _customer(db_session, bu, "Acme")
    p = _product(db_session, "P-DEAD")
    _on_hand(db_session, bu, p, 800)
    db_session.commit()

    row = surplus_report(db_session).rows[0]
    assert row.obsolete == 800 and row.surplus == 0 and row.allocated == 0


def test_demand_beyond_horizon_still_counts_as_obsolete(db_session):
    """The horizon is the definition: demand 40 months out does not rescue the
    steel from the obsolete bucket -- and the horizon constant is what a reader
    should check if that ever surprises them."""
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Acme")
    p = _product(db_session, "P-FAR")
    _on_hand(db_session, bu, p, 500)
    well = _well(db_session, node, "WELL-FAR")
    _line(db_session, well, p, 500, days_out=SURPLUS_HORIZON_MONTHS * 31 + 60)
    db_session.commit()

    row = surplus_report(db_session).rows[0]
    assert row.obsolete == 500


def test_obsolete_rows_sort_first(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Acme")
    live = _product(db_session, "A-LIVE")
    dead = _product(db_session, "Z-DEAD")
    _on_hand(db_session, bu, live, 100)
    _on_hand(db_session, bu, dead, 100)
    well = _well(db_session, node, "WELL-1")
    _line(db_session, well, live, 100, days_out=30)
    db_session.commit()

    report = surplus_report(db_session)
    assert report.rows[0].product_id == dead.id


def test_tonnes_headlines_and_measured_zero(db_session):
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Acme")
    p = _product(db_session, "P-T", weight=53.5)
    _on_hand(db_session, bu, p, 1000)
    well = _well(db_session, node, "WELL-1")
    _line(db_session, well, p, 1000, days_out=30)
    db_session.commit()

    report = surplus_report(db_session)
    # Everything allocated: surplus and obsolete are measured zeros, not
    # "nothing to convert".
    assert report.surplus_tonnes.available and report.surplus_tonnes.value == 0.0
    assert report.obsolete_tonnes.available and report.obsolete_tonnes.value == 0.0
    assert report.allocated_tonnes.available
    expected = 1000 / 0.3048 * 53.5 * 0.453592 / 1000.0
    assert abs(report.allocated_tonnes.value - expected) < 1e-6


def test_overdue_only_demand_is_allocated_not_obsolete(db_session):
    """Overdue demand still claims the steel (2026-08-12 decision).

    A product whose ONLY demand has a passed ROS date is allocated, never
    obsolete -- lateness does not cancel demand -- and the overdue quantity is
    labelled on the row.
    """
    bu = _bu(db_session)
    customer, node = _customer(db_session, bu, "Acme")
    p = _product(db_session, "P-LATE")
    _on_hand(db_session, bu, p, 1000)
    well = _well(db_session, node, "WELL-LATE")
    _line(db_session, well, p, 700, days_out=-60)
    db_session.commit()

    row = surplus_report(db_session).rows[0]
    assert row.allocated == 700
    assert row.surplus == 300
    assert row.obsolete == 0
    assert row.demand_in_window == 700
    assert row.demand_overdue == 700
