"""Dynamic spread baseline, volatility, Z-score, and regime guard.

The estimator is deliberately exchange-agnostic and side-effect free except
for its bounded in-memory state.  It consumes the mid-to-mid Entropy premium
in bps; executable VWAP remains a separate concern in ``pricing.py``.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple


@dataclass(frozen=True)
class SpreadStats:
    timestamp: float
    spread_bps: float
    fast_midline_bps: float
    slow_midline_bps: float
    volatility_bps: float
    deviation_bps: float
    z_score: float
    sample_count: int
    window_span_seconds: float
    ready: bool


class DynamicMidline:
    """Time-windowed EMA + rolling median + STD/MAD estimator."""

    def __init__(self, *, fast_window_seconds: float,
                 slow_window_seconds: float, min_samples: int,
                 volatility_method: str,
                 volatility_window_seconds: float,
                 volatility_floor_bps: float) -> None:
        if fast_window_seconds <= 0 or slow_window_seconds <= 0:
            raise ValueError("midline windows must be > 0")
        if volatility_window_seconds <= 0 or volatility_floor_bps <= 0:
            raise ValueError("volatility window/floor must be > 0")
        if min_samples <= 0:
            raise ValueError("min_samples must be > 0")
        if volatility_method not in ("std", "mad"):
            raise ValueError("volatility_method must be std or mad")
        self.fast_window_seconds = float(fast_window_seconds)
        self.slow_window_seconds = float(slow_window_seconds)
        self.min_samples = int(min_samples)
        self.volatility_method = volatility_method
        self.volatility_window_seconds = float(volatility_window_seconds)
        self.volatility_floor_bps = float(volatility_floor_bps)
        self._retention_seconds = max(
            self.slow_window_seconds, self.volatility_window_seconds)
        self._samples: Deque[Tuple[float, float]] = deque()
        self._fast: Optional[float] = None
        self._last_timestamp: Optional[float] = None
        self._latest: Optional[SpreadStats] = None

    @property
    def latest(self) -> Optional[SpreadStats]:
        return self._latest

    def update(self, timestamp: float, spread_bps: float) -> SpreadStats:
        timestamp, spread_bps = float(timestamp), float(spread_bps)
        if not (math.isfinite(timestamp) and math.isfinite(spread_bps)):
            raise ValueError("timestamp and spread must be finite")
        if self._last_timestamp is not None:
            if timestamp < self._last_timestamp:
                raise ValueError("midline samples must be time ordered")
            if timestamp == self._last_timestamp:
                # One observation per timestamp prevents websocket bursts from
                # satisfying min_samples without real elapsed observations.
                return self._latest

        if self._fast is None:
            self._fast = spread_bps
        else:
            elapsed = timestamp - self._last_timestamp
            alpha = 1.0 - math.exp(-elapsed / self.fast_window_seconds)
            self._fast += alpha * (spread_bps - self._fast)
        self._last_timestamp = timestamp
        self._samples.append((timestamp, spread_bps))
        cutoff = timestamp - self._retention_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        slow_values = [value for ts, value in self._samples
                       if ts >= timestamp - self.slow_window_seconds]
        volatility_values = [value for ts, value in self._samples
                             if ts >= timestamp
                             - self.volatility_window_seconds]
        slow = float(statistics.median(slow_values))
        volatility = self._volatility(volatility_values)
        count = min(len(slow_values), len(volatility_values))
        first_relevant = max(timestamp - self.slow_window_seconds,
                             timestamp - self.volatility_window_seconds)
        relevant_ts = [ts for ts, _ in self._samples if ts >= first_relevant]
        span = timestamp - relevant_ts[0] if relevant_ts else 0.0
        deviation = spread_bps - slow
        self._latest = SpreadStats(
            timestamp=timestamp,
            spread_bps=spread_bps,
            fast_midline_bps=self._fast,
            slow_midline_bps=slow,
            volatility_bps=volatility,
            deviation_bps=deviation,
            z_score=deviation / volatility,
            sample_count=count,
            window_span_seconds=span,
            ready=count >= self.min_samples,
        )
        return self._latest

    def _volatility(self, values: list[float]) -> float:
        if len(values) < 2:
            raw = 0.0
        elif self.volatility_method == "std":
            raw = statistics.pstdev(values)
        else:
            center = statistics.median(values)
            raw = statistics.median(abs(value - center) for value in values)
            raw *= 1.4826  # normal-consistent MAD scale
        return max(float(raw), self.volatility_floor_bps)


@dataclass(frozen=True)
class RegimeStatus:
    paused: bool
    breaking: bool
    reasons: Tuple[str, ...]
    break_since: Optional[float]
    paused_since: Optional[float]
    recovery_since: Optional[float]


class RegimeDetector:
    """Persistence-aware PAUSE_NEW_ENTRY guard with hysteretic recovery."""

    def __init__(self, *, max_fast_slow_difference_bps: float,
                 max_z_score: float, max_absolute_spread_bps: float,
                 break_persist_seconds: float,
                 recovery_persist_seconds: float) -> None:
        self.max_fast_slow_difference_bps = max_fast_slow_difference_bps
        self.max_z_score = max_z_score
        self.max_absolute_spread_bps = max_absolute_spread_bps
        self.break_persist_seconds = break_persist_seconds
        self.recovery_persist_seconds = recovery_persist_seconds
        self._paused = False
        self._break_since: Optional[float] = None
        self._paused_since: Optional[float] = None
        self._recovery_since: Optional[float] = None
        self._reasons: Tuple[str, ...] = ()

    @property
    def status(self) -> RegimeStatus:
        return RegimeStatus(
            paused=self._paused,
            breaking=bool(self._break_since is not None and not self._paused),
            reasons=self._reasons,
            break_since=self._break_since,
            paused_since=self._paused_since,
            recovery_since=self._recovery_since,
        )

    def update(self, stats: SpreadStats, now: Optional[float] = None
               ) -> RegimeStatus:
        now = stats.timestamp if now is None else float(now)
        reasons = []
        if abs(stats.fast_midline_bps - stats.slow_midline_bps) \
                > self.max_fast_slow_difference_bps:
            reasons.append("fast_slow_divergence")
        if abs(stats.spread_bps) > self.max_absolute_spread_bps:
            reasons.append("absolute_spread")
        if abs(stats.z_score) > self.max_z_score:
            reasons.append("z_score")
        current_reasons = tuple(reasons)

        if self._paused:
            if current_reasons:
                self._reasons = current_reasons
                self._recovery_since = None
            else:
                if self._recovery_since is None:
                    self._recovery_since = now
                if now - self._recovery_since >= self.recovery_persist_seconds:
                    self._paused = False
                    self._break_since = None
                    self._paused_since = None
                    self._recovery_since = None
                    self._reasons = ()
            return self.status

        if current_reasons:
            if self._break_since is None:
                self._break_since = now
            self._reasons = current_reasons
            if now - self._break_since >= self.break_persist_seconds:
                self._paused = True
                self._paused_since = now
        else:
            self._break_since = None
            self._reasons = ()
        return self.status
