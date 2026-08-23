from __future__ import annotations

import pytest

from backend.app.plot_tuning.bounds import bounds_for


def test_int_scales_five_times_either_side():
    result = bounds_for("int", 30)
    assert (result.minimum, result.maximum, result.step) == (6.0, 150.0, 1.0)


def test_int_floors_towards_zero():
    result = bounds_for("int", 3)
    assert (result.minimum, result.maximum) == (0.0, 15.0)


def test_int_zero_falls_back_to_a_usable_range():
    result = bounds_for("int", 0)
    assert (result.minimum, result.maximum) == (0.0, 10.0)


def test_negative_int_keeps_min_below_max():
    result = bounds_for("int", -10)
    assert result.minimum < result.maximum
    assert (result.minimum, result.maximum) == (-50.0, -2.0)


def test_int_range_never_collapses_to_a_point():
    # value 1 floors to 1/5 == 0 and 1*5 == 5, but value -1 would give (-5, 0);
    # the guard matters for any value whose scaled ends coincide.
    result = bounds_for("int", 1)
    assert result.minimum < result.maximum


def test_float_scales_five_times_either_side():
    result = bounds_for("float", 0.5)
    assert (result.minimum, result.maximum) == pytest.approx((0.1, 2.5))


def test_float_step_is_one_hundredth_of_the_range():
    result = bounds_for("float", 0.5)
    assert result.step == pytest.approx((2.5 - 0.1) / 100)


def test_float_zero_falls_back_to_a_unit_range():
    result = bounds_for("float", 0.0)
    assert (result.minimum, result.maximum) == (0.0, 1.0)


def test_negative_float_keeps_min_below_max():
    result = bounds_for("float", -0.5)
    assert result.minimum < result.maximum


@pytest.mark.parametrize("kind", ["bool", "str", "tuple", "list"])
def test_non_numeric_kinds_have_no_slider(kind):
    assert bounds_for(kind, 0) is None
