"""Optional US-equity session awareness.

The feature is intentionally controlled by one switch. Disabled means one
24/7 crypto statistics pool. Enabled classifies US stock-perpetual oracle
regimes, but never blocks perpetual trading by itself: every session remains
sampleable and entry-capable after its independent estimator is ready.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
from typing import Optional


class MarketSession(str, Enum):
    CRYPTO_24X7 = "crypto_24x7"
    PRE_MARKET = "pre_market"
    REGULAR = "regular"
    AFTER_HOURS = "after_hours"
    OVERNIGHT = "overnight"
    # Kept only so persisted historical Pair/session data remains readable.
    # SessionClock no longer emits CLOSED for stock perpetuals.
    CLOSED = "closed"


@dataclass(frozen=True)
class SessionStatus:
    session: MarketSession
    local_time: datetime
    entry_allowed: bool
    sampleable: bool
    reason: Optional[str] = None

    @property
    def stats_key(self) -> str:
        return self.session.value


def _observed(day: date) -> date:
    if day.weekday() == calendar.SATURDAY:
        return day - timedelta(days=1)
    if day.weekday() == calendar.SUNDAY:
        return day + timedelta(days=1)
    return day


def _observed_new_year(day: date) -> date:
    # NYSE does not observe New Year's Day on the preceding Friday when
    # January 1 falls on Saturday (for example, the published 2028 calendar).
    if day.weekday() == calendar.SUNDAY:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _new_york_time(instant: datetime) -> datetime:
    """Convert UTC to US Eastern time without an OS tzdata dependency.

    US rules since 2007: DST starts at 02:00 local standard time on the
    second Sunday in March and ends at 02:00 local daylight time on the first
    Sunday in November.  Trading timestamps are modern, so pre-2007 rules are
    intentionally outside this strategy's contract.
    """
    instant = instant.astimezone(timezone.utc)
    year = instant.year
    march = _nth_weekday(year, 3, calendar.SUNDAY, 2)
    november = _nth_weekday(year, 11, calendar.SUNDAY, 1)
    dst_start = datetime(
        year, 3, march.day, 7, 0, tzinfo=timezone.utc)   # 02:00 EST
    dst_end = datetime(
        year, 11, november.day, 6, 0, tzinfo=timezone.utc)  # 02:00 EDT
    is_dst = dst_start <= instant < dst_end
    offset = timedelta(hours=-4 if is_dst else -5)
    return instant.astimezone(timezone(offset, "EDT" if is_dst else "EST"))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian computus, valid for the years used by this strategy."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


@lru_cache(maxsize=32)
def us_equity_holidays(year: int) -> frozenset[date]:
    """Regular full-day US equity holidays for a calendar year.

    One-off national closures are deliberately not predicted.  A venue or
    oracle outage still fails closed through the existing feed/risk guards.
    """
    days = {
        _observed_new_year(date(year, 1, 1)),
        _nth_weekday(year, 1, calendar.MONDAY, 3),       # MLK Day
        _nth_weekday(year, 2, calendar.MONDAY, 3),       # Presidents Day
        _easter_sunday(year) - timedelta(days=2),        # Good Friday
        _last_weekday(year, 5, calendar.MONDAY),         # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, calendar.MONDAY, 1),       # Labor Day
        _nth_weekday(year, 11, calendar.THURSDAY, 4),    # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))            # Juneteenth
    return frozenset(days)


@lru_cache(maxsize=32)
def us_equity_early_closes(year: int) -> frozenset[date]:
    """Recurring NYSE 13:00 ET closes published in its calendar."""
    holidays = us_equity_holidays(year)
    thanksgiving = _nth_weekday(year, 11, calendar.THURSDAY, 4)
    candidates = {
        thanksgiving + timedelta(days=1),
        date(year, 7, 3),
        date(year, 12, 24),
    }
    return frozenset(
        day for day in candidates
        if day.weekday() < calendar.SATURDAY and day not in holidays)


class SessionClock:
    """Classify an instant as 24/7 crypto or a stock-oracle regime."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def status(self, timestamp: Optional[float] = None) -> SessionStatus:
        instant = (datetime.now(timezone.utc) if timestamp is None else
                   datetime.fromtimestamp(float(timestamp), timezone.utc))
        local = _new_york_time(instant)
        if not self.enabled:
            return SessionStatus(
                MarketSession.CRYPTO_24X7, local,
                entry_allowed=True, sampleable=True)

        day = local.date()
        minute = local.hour * 60 + local.minute
        # This is a stock-perpetual liquidity/oracle classifier, not a cash
        # exchange gate. Collapse every non-cash regime into OVERNIGHT so the
        # strategy has exactly four independent statistics pools and no
        # artificial 20:00-21:00 ET trading gap.
        if minute >= 20 * 60 or minute < 4 * 60:
            return SessionStatus(
                MarketSession.OVERNIGHT, local, True, True, "off_cash_hours")

        if day.weekday() >= calendar.SATURDAY:
            return SessionStatus(
                MarketSession.OVERNIGHT, local, True, True, "weekend")
        if day in us_equity_holidays(day.year):
            return SessionStatus(
                MarketSession.OVERNIGHT, local, True, True, "holiday")

        regular_close = (13 * 60 if day in us_equity_early_closes(day.year)
                         else 16 * 60)
        if 4 * 60 <= minute < 9 * 60 + 30:
            session = MarketSession.PRE_MARKET
        elif 9 * 60 + 30 <= minute < regular_close:
            session = MarketSession.REGULAR
        else:
            session = MarketSession.AFTER_HOURS
        return SessionStatus(
            session, local,
            entry_allowed=True,
            sampleable=True,
            reason=None)
