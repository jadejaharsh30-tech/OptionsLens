# optionslens/backend/tests/test_eod_vol.py
"""Constant-maturity interpolation and percentile rank."""
import pytest

from eod_vol import (constant_maturity_iv, expiries_for_tenor,
                     percentile_rank, term_structure_slope)


class TestConstantMaturityIV:

    def test_exact_tenor_match_is_returned_untouched(self):
        pts = [(7.0, 0.10), (30.0, 0.12), (60.0, 0.13)]
        assert constant_maturity_iv(pts, 30.0) == pytest.approx(0.12)

    def test_flat_term_structure_interpolates_to_the_same_vol(self):
        pts = [(7.0, 0.12), (60.0, 0.12)]
        assert constant_maturity_iv(pts, 30.0) == pytest.approx(0.12, abs=1e-12)

    def test_interpolates_in_variance_not_in_vol(self):
        # Variance-linear and vol-linear disagree whenever the term structure
        # is steep; this pins the behaviour to the variance convention.
        pts = [(10.0, 0.10), (40.0, 0.20)]
        got = constant_maturity_iv(pts, 30.0)

        w1, w2 = 0.10 ** 2 * 10.0, 0.20 ** 2 * 40.0
        w = w1 + (w2 - w1) * (30.0 - 10.0) / (40.0 - 10.0)
        assert got == pytest.approx((w / 30.0) ** 0.5)

        vol_linear = 0.10 + (0.20 - 0.10) * (30.0 - 10.0) / (40.0 - 10.0)
        assert got != pytest.approx(vol_linear, abs=1e-6)

    def test_result_is_bracketed_by_its_neighbours(self):
        pts = [(7.0, 0.09), (45.0, 0.14)]
        got = constant_maturity_iv(pts, 30.0)
        assert 0.09 < got < 0.14

    def test_uses_the_nearest_bracketing_pair_not_the_outermost(self):
        near = [(25.0, 0.12), (35.0, 0.13)]
        wide = [(1.0, 0.30)] + near + [(300.0, 0.05)]
        assert constant_maturity_iv(wide, 30.0) == pytest.approx(
            constant_maturity_iv(near, 30.0))

    def test_refuses_to_extrapolate_past_the_last_expiry(self):
        assert constant_maturity_iv([(5.0, 0.10), (9.0, 0.11)], 30.0) is None

    def test_refuses_to_extrapolate_before_the_first_expiry(self):
        assert constant_maturity_iv([(45.0, 0.10), (90.0, 0.11)], 30.0) is None

    def test_duplicate_tenors_are_averaged(self):
        pts = [(20.0, 0.10), (20.0, 0.14), (40.0, 0.12)]
        both = constant_maturity_iv(pts, 30.0)
        single = constant_maturity_iv([(20.0, 0.12), (40.0, 0.12)], 30.0)
        assert both == pytest.approx(single)

    def test_unusable_readings_are_dropped_not_treated_as_zero(self):
        pts = [(7.0, None), (-1.0, 0.10), (20.0, 0.11), (40.0, 0.12), (50.0, 0.0)]
        assert constant_maturity_iv(pts, 30.0) is not None

    def test_empty_and_single_point_return_none(self):
        assert constant_maturity_iv([], 30.0) is None
        assert constant_maturity_iv([(30.5, 0.12)], 30.0) is None

    def test_order_of_input_does_not_matter(self):
        pts = [(60.0, 0.13), (7.0, 0.10), (40.0, 0.125), (20.0, 0.115)]
        assert (constant_maturity_iv(pts, 30.0)
                == pytest.approx(constant_maturity_iv(sorted(pts), 30.0)))


class TestBoundedExtrapolation:
    """Stocks list monthly expiries only, so just after an expiry the nearest
    contract is already beyond 30 days. A few days of extrapolation keeps the
    series alive; more than that is refused."""

    def test_extrapolates_a_short_way_below_the_first_expiry(self):
        pts = [(33.0, 0.20), (61.0, 0.22)]
        got = constant_maturity_iv(pts, 30.0)
        w1, w2 = 0.20 ** 2 * 33.0, 0.22 ** 2 * 61.0
        w = w1 + (w2 - w1) * (30.0 - 33.0) / (61.0 - 33.0)
        assert got == pytest.approx((w / 30.0) ** 0.5)

    def test_extrapolates_a_short_way_past_the_last_expiry(self):
        assert constant_maturity_iv([(20.0, 0.12), (27.0, 0.13)], 30.0) is not None

    def test_refuses_beyond_the_tolerance(self):
        assert constant_maturity_iv([(36.0, 0.20), (64.0, 0.22)], 30.0) is None

    def test_tolerance_of_zero_restores_strict_interpolation(self):
        pts = [(33.0, 0.20), (61.0, 0.22)]
        assert constant_maturity_iv(pts, 30.0, max_extrapolation_days=0) is None

    def test_one_point_is_never_extrapolated(self):
        assert constant_maturity_iv([(31.0, 0.20)], 30.0) is None


class TestExpiriesForTenor:

    def test_picks_the_bracketing_pair(self):
        tenors = [2.0, 9.0, 16.0, 23.0, 37.0, 65.0]
        assert expiries_for_tenor(tenors, 30.0) == [3, 4]

    def test_input_order_is_respected_in_the_returned_indices(self):
        tenors = [65.0, 23.0, 2.0, 37.0]
        assert sorted(expiries_for_tenor(tenors, 30.0)) == [1, 3]

    def test_exact_match_needs_only_one_chain(self):
        assert expiries_for_tenor([7.0, 30.0, 60.0], 30.0) == [1]

    def test_two_nearest_on_one_side_when_unbracketed(self):
        assert expiries_for_tenor([33.0, 61.0, 91.0], 30.0) == [0, 1]

    def test_ignores_expired_and_empty(self):
        assert expiries_for_tenor([0.0, -1.0, 45.0], 30.0) == []
        assert expiries_for_tenor([], 30.0) == []

    def test_selection_matches_what_the_interpolation_uses(self):
        tenors = [1.0, 8.0, 22.0, 29.0, 36.0, 57.0]
        ivs = [0.30, 0.12, 0.11, 0.115, 0.118, 0.12]
        pts = list(zip(tenors, ivs))
        chosen = [pts[i] for i in expiries_for_tenor(tenors, 30.0)]
        assert constant_maturity_iv(chosen, 30.0) == pytest.approx(
            constant_maturity_iv(pts, 30.0))


class TestTermStructureSlope:

    def test_contango_is_positive(self):
        pts = [(20.0, 0.10), (40.0, 0.12), (80.0, 0.14)]
        assert term_structure_slope(pts, 30.0, 60.0) > 0

    def test_backwardation_is_negative(self):
        pts = [(20.0, 0.20), (40.0, 0.16), (80.0, 0.13)]
        assert term_structure_slope(pts, 30.0, 60.0) < 0

    def test_none_when_the_far_leg_is_unbracketed(self):
        pts = [(20.0, 0.10), (40.0, 0.12)]
        assert term_structure_slope(pts, 30.0, 60.0) is None


class TestPercentileRank:

    def test_none_below_twenty_observations(self):
        assert percentile_rank([0.1] * 19, 0.1) is None
        assert percentile_rank([0.1] * 20, 0.1) is not None

    def test_lowest_and_highest_readings_sit_at_the_extremes(self):
        hist = [float(i) for i in range(20)]
        assert percentile_rank(hist, -1.0) == pytest.approx(0.0)
        assert percentile_rank(hist, 100.0) == pytest.approx(100.0)

    def test_midpoint_of_a_uniform_history(self):
        hist = [float(i) for i in range(100)]
        assert percentile_rank(hist, 50.0) == pytest.approx(50.5)

    def test_survives_an_outlier_that_would_wreck_iv_rank(self):
        # IV Rank divides by the high-low range, so one spike compresses every
        # other reading toward zero. Percentile rank counts observations.
        hist = [0.10] * 50 + [0.11] * 49 + [0.90]
        assert percentile_rank(hist, 0.11) > 50.0

    def test_ties_are_counted_at_their_midpoint(self):
        hist = [0.10] * 20
        assert percentile_rank(hist, 0.10) == pytest.approx(50.0)
