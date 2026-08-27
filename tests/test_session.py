"""One-switch crypto/US-equity session classification tests."""
from datetime import datetime, timedelta, timezone
from entropy_arb.session import MarketSession, SessionClock


def ts(year, month, day, hour, minute=0, *, summer=True):
    """Build an epoch from a known US Eastern local wall time."""
    offset = -4 if summer else -5
    local_as_utc = datetime(year, month, day, hour, minute,
                            tzinfo=timezone.utc)
    return (local_as_utc - timedelta(hours=offset)).timestamp()


def test_disabled_is_crypto_24x7_even_on_weekend():
    status = SessionClock(False).status(ts(2026, 8, 29, 3, 0))
    assert status.session is MarketSession.CRYPTO_24X7
    assert status.entry_allowed and status.sampleable


def test_stock_session_boundaries_are_all_tradable_regimes():
    clock = SessionClock(True)
    overnight = clock.status(ts(2026, 8, 27, 3, 59))
    pre = clock.status(ts(2026, 8, 27, 4, 0))
    regular = clock.status(ts(2026, 8, 27, 9, 30))
    after = clock.status(ts(2026, 8, 27, 16, 0))
    night_open = clock.status(ts(2026, 8, 27, 20, 0))
    assert overnight.session is MarketSession.OVERNIGHT
    assert pre.session is MarketSession.PRE_MARKET
    assert regular.session is MarketSession.REGULAR
    assert after.session is MarketSession.AFTER_HOURS
    assert night_open.session is MarketSession.OVERNIGHT
    assert all(status.entry_allowed and status.sampleable for status in (
        overnight, pre, regular, after, night_open))


def test_weekend_and_equity_holidays_use_tradable_overnight_regime():
    clock = SessionClock(True)
    weekend = clock.status(ts(2026, 8, 29, 10, 0))
    independence_observed = clock.status(ts(2026, 7, 3, 10, 0))
    good_friday = clock.status(ts(2026, 4, 3, 10, 0))
    assert weekend.reason == "weekend"
    assert independence_observed.reason == "holiday"
    assert good_friday.reason == "holiday"
    assert weekend.session is MarketSession.OVERNIGHT
    assert independence_observed.session is MarketSession.OVERNIGHT
    assert good_friday.session is MarketSession.OVERNIGHT
    assert weekend.entry_allowed and weekend.sampleable
    assert independence_observed.entry_allowed and independence_observed.sampleable


def test_new_york_dst_offsets_are_applied_without_system_tzdata():
    clock = SessionClock(True)
    winter = clock.status(ts(2026, 1, 6, 9, 30, summer=False))
    summer = clock.status(ts(2026, 7, 6, 9, 30))
    assert winter.session is MarketSession.REGULAR
    assert summer.session is MarketSession.REGULAR
    assert winter.local_time.utcoffset() != summer.local_time.utcoffset()


def test_published_early_close_switches_to_tradable_after_hours_at_1300_et():
    clock = SessionClock(True)
    black_friday = clock.status(ts(2026, 11, 27, 13, 0, summer=False))
    christmas_eve = clock.status(ts(2026, 12, 24, 13, 0, summer=False))
    assert black_friday.session is MarketSession.AFTER_HOURS
    assert christmas_eve.session is MarketSession.AFTER_HOURS
    assert black_friday.entry_allowed and black_friday.sampleable
    assert christmas_eve.entry_allowed and christmas_eve.sampleable


def test_overnight_has_no_friday_or_holiday_gap_for_perpetuals():
    clock = SessionClock(True)
    sunday_night = clock.status(ts(2026, 8, 30, 21, 0))
    friday_night = clock.status(ts(2026, 8, 28, 21, 0))
    friday_close = clock.status(ts(2026, 8, 28, 19, 59))
    before_thanksgiving = clock.status(
        ts(2026, 11, 25, 20, 0, summer=False))
    thanksgiving_day = clock.status(
        ts(2026, 11, 26, 10, 0, summer=False))
    assert sunday_night.session is MarketSession.OVERNIGHT
    assert friday_close.session is MarketSession.AFTER_HOURS
    assert friday_night.session is MarketSession.OVERNIGHT
    assert before_thanksgiving.session is MarketSession.OVERNIGHT
    assert thanksgiving_day.session is MarketSession.OVERNIGHT
    assert all(status.entry_allowed and status.sampleable for status in (
        sunday_night, friday_close, friday_night, before_thanksgiving,
        thanksgiving_day))


def test_saturday_new_year_is_not_observed_on_preceding_friday():
    # The NYSE 2028 calendar explicitly keeps Friday 2027-12-31 open.
    status = SessionClock(True).status(
        ts(2027, 12, 31, 10, 0, summer=False))
    assert status.session is MarketSession.REGULAR
    assert status.entry_allowed
