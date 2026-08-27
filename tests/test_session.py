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


def test_stock_session_boundaries_and_only_regular_allows_entry():
    clock = SessionClock(True)
    assert clock.status(ts(2026, 8, 27, 3, 59)).session is MarketSession.CLOSED
    pre = clock.status(ts(2026, 8, 27, 4, 0))
    regular = clock.status(ts(2026, 8, 27, 9, 30))
    after = clock.status(ts(2026, 8, 27, 16, 0))
    closed = clock.status(ts(2026, 8, 27, 20, 0))
    assert pre.session is MarketSession.PRE_MARKET and not pre.entry_allowed
    assert regular.session is MarketSession.REGULAR and regular.entry_allowed
    assert after.session is MarketSession.AFTER_HOURS and not after.entry_allowed
    assert closed.session is MarketSession.CLOSED and not closed.sampleable


def test_weekend_and_equity_holidays_fail_closed():
    clock = SessionClock(True)
    weekend = clock.status(ts(2026, 8, 29, 10, 0))
    independence_observed = clock.status(ts(2026, 7, 3, 10, 0))
    good_friday = clock.status(ts(2026, 4, 3, 10, 0))
    assert weekend.reason == "weekend"
    assert independence_observed.reason == "holiday"
    assert good_friday.reason == "holiday"
    assert not weekend.entry_allowed
    assert not independence_observed.entry_allowed


def test_new_york_dst_offsets_are_applied_without_system_tzdata():
    clock = SessionClock(True)
    winter = clock.status(ts(2026, 1, 6, 9, 30, summer=False))
    summer = clock.status(ts(2026, 7, 6, 9, 30))
    assert winter.session is MarketSession.REGULAR
    assert summer.session is MarketSession.REGULAR
    assert winter.local_time.utcoffset() != summer.local_time.utcoffset()


def test_published_early_close_stops_regular_entries_at_1300_et():
    clock = SessionClock(True)
    black_friday = clock.status(ts(2026, 11, 27, 13, 0, summer=False))
    christmas_eve = clock.status(ts(2026, 12, 24, 13, 0, summer=False))
    assert black_friday.session is MarketSession.AFTER_HOURS
    assert christmas_eve.session is MarketSession.AFTER_HOURS
    assert not black_friday.entry_allowed and not christmas_eve.entry_allowed


def test_saturday_new_year_is_not_observed_on_preceding_friday():
    # The NYSE 2028 calendar explicitly keeps Friday 2027-12-31 open.
    status = SessionClock(True).status(
        ts(2027, 12, 31, 10, 0, summer=False))
    assert status.session is MarketSession.REGULAR
    assert status.entry_allowed
