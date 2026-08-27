"""Fresh funding and quote-asset basis observations for executable pricing.

Enabled inputs fail closed: a missing or stale observation blocks OPEN/ADD
instead of silently treating an unknown cost as zero.
"""
from __future__ import annotations

import asyncio
import math
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
                 quote_assets: Dict[str, str],
                 stablecoin_max_spread_bps: float = 10.0) -> None:
        self.funding_enabled = funding_enabled
        self.expected_holding_hours = expected_holding_hours
        self.funding_max_age_seconds = funding_max_age_seconds
        self.stablecoin_enabled = stablecoin_enabled
        self.stablecoin_max_age_seconds = stablecoin_max_age_seconds
        self.warning_deviation_bps = warning_deviation_bps
        self.halt_deviation_bps = halt_deviation_bps
        self.stablecoin_max_spread_bps = stablecoin_max_spread_bps
        self.quote_assets = {k: v.upper() for k, v in quote_assets.items()}
        self.funding_rates: Dict[str, RateObservation] = {}
        self.quote_usd: Dict[str, RateObservation] = {
            "USD": RateObservation(1.0, time.time(), "identity")}

    def set_funding(self, venue_key: str, hourly_rate: float, *,
                    observed_at: Optional[float] = None,
                    source: str = "test") -> None:
        if not math.isfinite(float(hourly_rate)):
            raise ValueError("funding rate must be finite")
        if abs(float(hourly_rate)) >= 1.0:
            raise ValueError("funding rate is out of range; expected an "
                             "hourly decimal rate in (-1, 1)")
        if observed_at is not None and not math.isfinite(float(observed_at)):
            raise ValueError("funding observation time must be finite")
        self.funding_rates[venue_key] = RateObservation(
            float(hourly_rate), time.time() if observed_at is None else observed_at,
            source)

    def set_quote_usd(self, asset: str, value: float, *,
                      observed_at: Optional[float] = None,
                      source: str = "test") -> None:
        if not math.isfinite(float(value)) or value <= 0:
            raise ValueError("quote USD value must be finite and > 0")
        if observed_at is not None and not math.isfinite(float(observed_at)):
            raise ValueError("quote observation time must be finite")
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

    def funding_rate(self, venue_key: str) -> float:
        if not self.funding_enabled:
            return 0.0
        return self.funding_rates[venue_key].value

    def quote_rate(self, venue_key: str) -> float:
        if not self.stablecoin_enabled:
            return 1.0
        return self.quote_usd[self.quote_assets[venue_key]].value

    def fresh_quote_rate(self, venue_key: str,
                         now: Optional[float] = None) -> float:
        """Return a current quote/USD rate or fail closed with ``KeyError``."""
        if not self.stablecoin_enabled:
            return 1.0
        asset = self.quote_assets[venue_key]
        if asset == "USD":
            return 1.0
        observation = self.quote_usd.get(asset)
        current = time.time() if now is None else now
        if not self._fresh(observation, self.stablecoin_max_age_seconds,
                           current):
            raise KeyError(f"stale quote/USD observation for {asset}")
        return observation.value

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
        """Refresh Kraken ``ASSET/USD`` books as one atomic observation set.

        Kraken's level timestamps are used instead of request completion time:
        an HTTP connection can remain healthy while a thin book stops changing.
        If any required asset is invalid, stale, crossed, or too wide, none of
        the rates are advanced and the existing max-age guard fails closed.
        """
        assets = sorted(set(self.quote_assets.values()) - {"USD"})
        if not assets:
            return
        now = time.time()
        pending: Dict[str, RateObservation] = {}
        for asset in assets:
            pair = f"{asset}USD"
            url = source_url.rstrip("/") + "/0/public/Depth"
            try:
                async with session.get(
                        url, params={"pair": pair, "count": "1"},
                        timeout=aiohttp.ClientTimeout(total=10.0)) as response:
                    response.raise_for_status()
                    data = await response.json()
                if not isinstance(data, dict):
                    raise ValueError("Kraken response must be an object")
                errors = data.get("error")
                result = data.get("result")
                if not isinstance(errors, list) or errors:
                    raise ValueError("Kraken returned an API error")
                if not isinstance(result, dict) or set(result) != {pair}:
                    raise ValueError("Kraken returned the wrong asset pair")
                book = result[pair]
                if not isinstance(book, dict):
                    raise ValueError("Kraken book must be an object")
                bid, bid_qty, bid_at = self._kraken_level(book.get("bids"))
                ask, ask_qty, ask_at = self._kraken_level(book.get("asks"))
                if bid > ask:
                    raise ValueError("Kraken book is crossed")
                spread_bps = (ask / bid - 1.0) * 1e4
                if (not math.isfinite(spread_bps)
                        or spread_bps > self.stablecoin_max_spread_bps):
                    raise ValueError("Kraken book spread is too wide")
                observed_at = min(bid_at, ask_at)
                if (observed_at < now - self.stablecoin_max_age_seconds
                        or observed_at > now + 5.0):
                    raise ValueError("Kraken book timestamp is not current")
                # Quantities are deliberately validated even though this
                # monitor consumes only the midpoint. Empty/dummy levels must
                # never become a trusted risk conversion rate.
                if bid_qty <= 0 or ask_qty <= 0:
                    raise ValueError("Kraken book quantity must be positive")
                pending[asset] = RateObservation(
                    (bid + ask) / 2.0, observed_at,
                    f"{url}?pair={pair}&count=1")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError,
                    TypeError, IndexError, KeyError):
                # Keep the prior timestamp. It will naturally become stale and
                # fail closed if the source remains unavailable.
                return
        self.quote_usd.update(pending)

    @staticmethod
    def _kraken_level(levels: object) -> tuple[float, float, float]:
        if not isinstance(levels, list) or len(levels) != 1:
            raise ValueError("Kraken level-1 book must contain one level")
        level = levels[0]
        if not isinstance(level, (list, tuple)) or len(level) < 3:
            raise ValueError("Kraken book level is malformed")
        price, quantity, observed_at = map(float, level[:3])
        if not all(math.isfinite(value)
                   for value in (price, quantity, observed_at)):
            raise ValueError("Kraken book level must be finite")
        if price <= 0 or quantity <= 0 or observed_at <= 0:
            raise ValueError("Kraken book level must be positive")
        return price, quantity, observed_at
