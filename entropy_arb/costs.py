"""Fresh funding and quote-asset basis observations for executable pricing.

Enabled inputs fail closed: a missing or stale observation blocks OPEN/ADD
instead of silently treating an unknown cost as zero.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

import aiohttp


@dataclass(frozen=True)
class RateObservation:
    value: float
    observed_at: float
    source: str

    def age_seconds(self, now: Optional[float] = None) -> float:
        return max((time.time() if now is None else now) - self.observed_at, 0.0)


class CostMonitor:
    def __init__(self, *, funding_enabled: bool,
                 expected_holding_hours: float, funding_max_age_seconds: float,
                 stablecoin_enabled: bool, stablecoin_max_age_seconds: float,
                 warning_deviation_bps: float, halt_deviation_bps: float,
                 quote_assets: Dict[str, str]) -> None:
        self.funding_enabled = funding_enabled
        self.expected_holding_hours = expected_holding_hours
        self.funding_max_age_seconds = funding_max_age_seconds
        self.stablecoin_enabled = stablecoin_enabled
        self.stablecoin_max_age_seconds = stablecoin_max_age_seconds
        self.warning_deviation_bps = warning_deviation_bps
        self.halt_deviation_bps = halt_deviation_bps
        self.quote_assets = {k: v.upper() for k, v in quote_assets.items()}
        self.funding_rates: Dict[str, RateObservation] = {}
        self.quote_usd: Dict[str, RateObservation] = {
            "USD": RateObservation(1.0, time.time(), "identity")}

    def set_funding(self, venue_key: str, hourly_rate: float, *,
                    observed_at: Optional[float] = None,
                    source: str = "test") -> None:
        self.funding_rates[venue_key] = RateObservation(
            float(hourly_rate), time.time() if observed_at is None else observed_at,
            source)

    def set_quote_usd(self, asset: str, value: float, *,
                      observed_at: Optional[float] = None,
                      source: str = "test") -> None:
        if value <= 0:
            raise ValueError("quote USD value must be > 0")
        self.quote_usd[asset.upper()] = RateObservation(
            float(value), time.time() if observed_at is None else observed_at,
            source)

    def _fresh(self, observation: Optional[RateObservation], max_age: float,
               now: float) -> bool:
        return observation is not None and observation.age_seconds(now) <= max_age

    def pause_reason(self, now: Optional[float] = None) -> Optional[str]:
        current = time.time() if now is None else now
        if self.funding_enabled:
            for key in self.quote_assets:
                if not self._fresh(self.funding_rates.get(key),
                                   self.funding_max_age_seconds, current):
                    return f"funding_stale:{key}"
        if self.stablecoin_enabled:
            for asset in sorted(set(self.quote_assets.values())):
                if asset == "USD":
                    continue
                obs = self.quote_usd.get(asset)
                if not self._fresh(obs, self.stablecoin_max_age_seconds, current):
                    return f"stablecoin_stale:{asset}"
                deviation = abs(obs.value - 1.0) * 1e4
                if deviation >= self.halt_deviation_bps:
                    return f"stablecoin_depeg:{asset}"
        return None

    def warning_assets(self, now: Optional[float] = None) -> list[str]:
        current = time.time() if now is None else now
        result = []
        for asset, obs in self.quote_usd.items():
            if asset != "USD" and self._fresh(
                    obs, self.stablecoin_max_age_seconds, current):
                if abs(obs.value - 1.0) * 1e4 >= self.warning_deviation_bps:
                    result.append(asset)
        return sorted(result)

    def funding_cost_bps(self, direction: str) -> float:
        if not self.funding_enabled:
            return 0.0
        entropy = self.funding_rates["entropy"].value
        hedge = self.funding_rates["hedge"].value
        hourly_cost = (-entropy + hedge if direction == "sell_entropy"
                       else entropy - hedge)
        return hourly_cost * self.expected_holding_hours * 1e4

    def quote_rate(self, venue_key: str) -> float:
        if not self.stablecoin_enabled:
            return 1.0
        return self.quote_usd[self.quote_assets[venue_key]].value

    def stablecoin_basis_cost_bps(self, *, buy_key: str, sell_key: str,
                                  raw_sell_buy_ratio: float = 1.0) -> float:
        if not self.stablecoin_enabled:
            return 0.0
        buy_usd = self.quote_rate(buy_key)
        sell_usd = self.quote_rate(sell_key)
        raw = (raw_sell_buy_ratio - 1.0) * 1e4
        adjusted = (raw_sell_buy_ratio * sell_usd / buy_usd - 1.0) * 1e4
        return raw - adjusted

    async def refresh_funding(self, venues: Iterable[object]) -> None:
        venues = list(venues)
        results = await asyncio.gather(
            *(venue.fetch_funding_rate() for venue in venues),
            return_exceptions=True)
        now = time.time()
        for venue, result in zip(venues, results):
            if isinstance(result, BaseException) or result is None:
                continue
            self.set_funding(venue.key, float(result), observed_at=now,
                             source=venue.name)

    async def refresh_stablecoins(self, session: aiohttp.ClientSession,
                                  source_url: str) -> None:
        assets = set(self.quote_assets.values()) - {"USD"}
        now = time.time()
        for asset in assets:
            url = source_url.rstrip("/") + f"/products/{asset}-USD/book"
            try:
                async with session.get(
                        url, params={"level": "1"},
                        timeout=aiohttp.ClientTimeout(total=10.0)) as response:
                    response.raise_for_status()
                    data = await response.json()
                bid = float((data.get("bids") or [])[0][0])
                ask = float((data.get("asks") or [])[0][0])
                if bid > 0 and ask > 0:
                    self.set_quote_usd(asset, (bid + ask) / 2.0,
                                       observed_at=now, source=source_url)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError,
                    TypeError, IndexError, KeyError):
                # Keep the prior timestamp. It will naturally become stale and
                # fail closed if the source remains unavailable.
                continue
