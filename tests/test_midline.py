"""Dynamic baseline, robust volatility, Z-score, and regime tests."""
import math

from entropy_arb.midline import (DynamicMidline, RegimeDetector, SpreadStats)


def estimator(**overrides):
    params = dict(
        fast_window_seconds=10.0,
        slow_window_seconds=30.0,
        min_samples=3,
        volatility_method="std",
        volatility_window_seconds=30.0,
        volatility_floor_bps=0.1,
    )
    params.update(overrides)
    return DynamicMidline(**params)


def stats(ts=0.0, spread=5.0, fast=5.0, slow=5.0, z=0.0):
    return SpreadStats(
        timestamp=ts, spread_bps=spread,
        fast_midline_bps=fast, slow_midline_bps=slow,
        volatility_bps=1.0, deviation_bps=spread - slow,
        z_score=z, sample_count=10, window_span_seconds=10.0, ready=True)


def test_time_based_ema_and_rolling_median_std_zscore():
    midline = estimator()
    first = midline.update(0.0, 0.0)
    second = midline.update(10.0, 10.0)
    final = midline.update(20.0, 20.0)

    assert first.fast_midline_bps == 0.0
    assert math.isclose(second.fast_midline_bps,
                        10.0 * (1.0 - math.exp(-1.0)))
    assert final.slow_midline_bps == 10.0
    assert math.isclose(final.volatility_bps, math.sqrt(200.0 / 3.0))
    assert math.isclose(final.z_score,
                        10.0 / math.sqrt(200.0 / 3.0))
    assert final.ready


def test_mad_is_scaled_and_volatility_floor_prevents_division_by_zero():
    robust = estimator(volatility_method="mad")
    robust.update(0.0, 0.0)
    robust.update(1.0, 10.0)
    result = robust.update(2.0, 20.0)
    assert math.isclose(result.volatility_bps, 14.826)

    flat = estimator(volatility_floor_bps=0.25)
    flat.update(0.0, 5.0)
    flat.update(1.0, 5.0)
    result = flat.update(2.0, 5.0)
    assert result.volatility_bps == 0.25
    assert result.z_score == 0.0


def test_time_windows_prune_old_samples_and_warmup_is_fail_closed():
    midline = estimator(min_samples=3, slow_window_seconds=5.0,
                        volatility_window_seconds=5.0)
    assert not midline.update(0.0, 1.0).ready
    assert not midline.update(1.0, 2.0).ready
    assert midline.update(2.0, 3.0).ready
    result = midline.update(10.0, 20.0)
    assert result.sample_count == 1
    assert result.slow_midline_bps == 20.0
    assert not result.ready


def test_duplicate_timestamp_does_not_fake_sample_count():
    midline = estimator(min_samples=2)
    first = midline.update(1.0, 5.0)
    duplicate = midline.update(1.0, 50.0)
    assert duplicate is first
    assert duplicate.sample_count == 1
    try:
        midline.update(0.5, 4.0)
    except ValueError as exc:
        assert "time ordered" in str(exc)
    else:
        raise AssertionError("out-of-order samples must be rejected")


def test_regime_break_requires_persistence_and_reports_all_reasons():
    detector = RegimeDetector(
        max_fast_slow_difference_bps=8.0, max_z_score=5.0,
        max_absolute_spread_bps=50.0, break_persist_seconds=2.0,
        recovery_persist_seconds=3.0)
    abnormal = stats(spread=60.0, fast=20.0, slow=5.0, z=6.0)
    first = detector.update(abnormal, now=10.0)
    assert first.breaking and not first.paused
    assert set(first.reasons) == {
        "fast_slow_divergence", "absolute_spread", "z_score"}
    assert not detector.update(abnormal, now=11.9).paused
    assert detector.update(abnormal, now=12.0).paused


def test_regime_recovery_requires_continuous_healthy_period():
    detector = RegimeDetector(
        max_fast_slow_difference_bps=8.0, max_z_score=5.0,
        max_absolute_spread_bps=50.0, break_persist_seconds=0.0,
        recovery_persist_seconds=3.0)
    abnormal = stats(spread=60.0)
    healthy = stats(spread=5.0)
    assert detector.update(abnormal, now=0.0).paused
    assert detector.update(healthy, now=1.0).paused
    assert detector.update(abnormal, now=2.0).paused
    assert detector.update(healthy, now=3.0).paused
    assert detector.update(healthy, now=6.0).paused is False
