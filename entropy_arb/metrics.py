"""Small in-process rolling metrics for latency observability.

No background worker or external telemetry service is required.  Samples are
bounded in memory and can later be exported without changing call sites.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, Optional


@dataclass(frozen=True)
class LatencySummary:
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        raise ValueError("percentile needs at least one value")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


class LatencyStats:
    def __init__(self, max_samples: int = 2000) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be > 0")
        self.max_samples = max_samples
        self._samples: Dict[str, Deque[float]] = {}

    def record(self, name: str, value_ms: Optional[float]) -> None:
        if value_ms is None:
            return
        value = float(value_ms)
        if not math.isfinite(value) or value < 0:
            return
        self._samples.setdefault(
            name, deque(maxlen=self.max_samples)).append(value)

    def extend(self, name: str, values_ms: Iterable[float]) -> None:
        for value in values_ms:
            self.record(name, value)

    def summary(self, name: str) -> Optional[LatencySummary]:
        values = sorted(self._samples.get(name, ()))
        if not values:
            return None
        return LatencySummary(
            count=len(values),
            p50_ms=_percentile(values, 0.50),
            p95_ms=_percentile(values, 0.95),
            p99_ms=_percentile(values, 0.99),
            max_ms=values[-1],
        )

    def snapshot(self) -> Dict[str, LatencySummary]:
        return {name: summary for name in sorted(self._samples)
                if (summary := self.summary(name)) is not None}
