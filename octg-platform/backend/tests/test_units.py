"""`app.engines.units.to_metric_tonnes` -- the Executive Dashboard's
length/mass conversion layer. See that module's docstring for the boundary this
conversion must stay behind (presentation only) and MVP_COMPROMISES.md
C-04/C-05/C-06.

Never a fabricated 0.0: every "cannot convert" case is asserted to return
`tonnes is None`, not zero.
"""

import pytest

from app.engines.units import (
    JOINT_LENGTH_FEET,
    KILOGRAMS_PER_TONNE,
    METRES_PER_FOOT,
    POUNDS_TO_KILOGRAMS,
    to_metric_tonnes,
)
from app.models import UnitOfMeasure


def test_mtr_to_mt_worked_example():
    """1000 m of a 53.5 lb/ft product.

    Hand computation, independent of the function under test, using the same
    named constants: 1000 * 53.5 * 0.453592 / 0.3048 / 1000 = 79.612... tonnes.
    """
    weight = 53.5
    expected = 1000 * weight * POUNDS_TO_KILOGRAMS / METRES_PER_FOOT / KILOGRAMS_PER_TONNE
    assert expected == pytest.approx(79.62, abs=0.01)

    result = to_metric_tonnes(UnitOfMeasure.MTR, 1000, weight)
    assert result.available is True
    assert result.tonnes == pytest.approx(expected, abs=0.01)
    assert result.tonnes == pytest.approx(79.62, abs=0.01)


def test_ft_to_mt_worked_example():
    """1000 ft of the same 53.5 lb/ft product: 1000 * 53.5 * 0.453592 / 1000."""
    weight = 53.5
    expected = 1000 * weight * POUNDS_TO_KILOGRAMS / KILOGRAMS_PER_TONNE
    assert expected == pytest.approx(24.27, abs=0.01)

    result = to_metric_tonnes(UnitOfMeasure.FT, 1000, weight)
    assert result.available is True
    assert result.tonnes == pytest.approx(24.27, abs=0.01)


def test_jt_to_mt_goes_via_joint_length_feet_and_matches_the_ft_path():
    """A JT quantity must convert to EXACTLY the same tonnage as the equivalent
    number of feet -- `qty * JOINT_LENGTH_FEET` ft -- because that is literally
    the formula (see MVP-COMPROMISE[C-04])."""
    weight = 53.5
    qty_joints = 25

    jt_result = to_metric_tonnes(UnitOfMeasure.JT, qty_joints, weight)
    ft_equivalent = qty_joints * JOINT_LENGTH_FEET
    ft_result = to_metric_tonnes(UnitOfMeasure.FT, ft_equivalent, weight)

    assert jt_result.available is True
    assert ft_result.available is True
    assert jt_result.tonnes == pytest.approx(ft_result.tonnes, abs=1e-9)
    # And matches independent hand arithmetic too.
    expected = ft_equivalent * weight * POUNDS_TO_KILOGRAMS / KILOGRAMS_PER_TONNE
    assert jt_result.tonnes == pytest.approx(expected, abs=0.01)


def test_mt_passes_through_unchanged():
    result = to_metric_tonnes(UnitOfMeasure.MT, 42.5, weight_lb_per_ft=53.5)
    assert result.available is True
    assert result.tonnes == 42.5


def test_pc_is_never_convertible():
    result = to_metric_tonnes(UnitOfMeasure.PC, 100, weight_lb_per_ft=53.5)
    assert result.available is False
    assert result.tonnes is None
    assert result.reason is not None
    assert "no length" in result.reason.lower() or "no weight" in result.reason.lower()


def test_pc_is_unconvertible_even_with_no_weight_stated():
    """Belt and braces: the PC refusal must not depend on weight being present."""
    result = to_metric_tonnes(UnitOfMeasure.PC, 100, weight_lb_per_ft=None)
    assert result.available is False
    assert result.tonnes is None


@pytest.mark.parametrize("unit", [UnitOfMeasure.FT, UnitOfMeasure.MTR, UnitOfMeasure.JT])
def test_null_weight_is_unconvertible_never_zero(unit):
    """MVP-COMPROMISE[C-06]: a NULL `Product.weight` must report unconvertible,
    never a fabricated 0.0 tonnes."""
    result = to_metric_tonnes(unit, 100, weight_lb_per_ft=None)
    assert result.available is False
    assert result.tonnes is None
    assert result.reason is not None
    assert "0.0" not in result.reason


def test_mt_with_null_weight_still_converts():
    """MT needs no weight at all -- it is already a mass."""
    result = to_metric_tonnes(UnitOfMeasure.MT, 10.0, weight_lb_per_ft=None)
    assert result.available is True
    assert result.tonnes == 10.0
