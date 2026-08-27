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


@dataclass(frozen=True)
class BookObservation:
    bid: float
    ask: float
    observed_at: float
    source: str

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

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
        self.quote_pairs: Dict[tuple[str, str], BookObservation] = {}

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

    def set_quote_pair(self, base_asset: str, quote_asset: str,
                       bid: float, ask: float, *,
                       observed_at: Optional[float] = None,
                       source: str = "test") -> None:
        base, quote = base_asset.upper(), quote_asset.upper()
        bid, ask = float(bid), float(ask)
        if base == quote:
            raise ValueError("quote pair assets must differ")
        if (not all(math.isfinite(value) and value > 0
                    for value in (bid, ask)) or bid > ask):
            raise ValueError("quote pair must be finite, positive, and not crossed")
        timestamp = time.time() if observed_at is None else float(observed_at)
        if not math.isfinite(timestamp):
            raise ValueError("quote pair observation time must be finite")
        self.quote_pairs[(base, quote)] = BookObservation(
            bid, ask, timestamp, source)

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

    def fresh_quote_pair(self, base_asset: str, quote_asset: str,
                         now: Optional[float] = None) -> tuple[float, float]:
        """Return executable ``base/quote`` bid and ask, inverting exactly.

        A direct Kraken cross is preferred. Other asset combinations fall
        back to fresh quote/USD midpoints, preserving legacy behavior without
        inventing a spread that was not observed.
        """
        base, quote = base_asset.upper(), quote_asset.upper()
        if base == quote:
            return 1.0, 1.0
        current = time.time() if now is None else now
        direct = self.quote_pairs.get((base, quote))
        if self._fresh(direct, self.stablecoin_max_age_seconds, current):
            return direct.bid, direct.ask
        reverse = self.quote_pairs.get((quote, base))
        if self._fresh(reverse, self.stablecoin_max_age_seconds, current):
            return 1.0 / reverse.ask, 1.0 / reverse.bid
        base_rate = self._fresh_asset_rate(base, current)
        quote_rate = self._fresh_asset_rate(quote, current)
        midpoint = base_rate / quote_rate
        return midpoint, midpoint

    def directional_quote_rates(self, *, buy_key: str, sell_key: str,
                                now: Optional[float] = None
                                ) -> tuple[float, float]:
        """Return conversion rates for an executable buy/sell pair.

        USDG is converted through the direct USDG/USDC ask when it must be
        bought and through the bid when it is received. The USDC/USD midpoint
        keeps both outputs in USD units for sizing and accounting.
        """
        buy_asset = self.quote_assets[buy_key]
        sell_asset = self.quote_assets[sell_key]
        if not self.stablecoin_enabled:
            return 1.0, 1.0
        current = time.time() if now is None else now
        cross = self.quote_pairs.get(("USDG", "USDC"))
        if ({buy_asset, sell_asset}.issubset({"USDG", "USDC"})
                and self._fresh(cross, self.stablecoin_max_age_seconds,
                                current)):
            usdc_usd = self._fresh_asset_rate("USDC", current)
            buy_rate = (cross.ask * usdc_usd
                        if buy_asset == "USDG" else usdc_usd)
            sell_rate = (cross.bid * usdc_usd
                         if sell_asset == "USDG" else usdc_usd)
            return buy_rate, sell_rate
        return (self._fresh_asset_rate(buy_asset, current),
                self._fresh_asset_rate(sell_asset, current))

    def _fresh_asset_rate(self, asset: str, now: float) -> float:
        if asset == "USD":
            return 1.0
        observation = self.quote_usd.get(asset)
        if not self._fresh(observation, self.stablecoin_max_age_seconds, now):
            raise KeyError(f"stale quote/USD observation for {asset}")
        return observation.value

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
        buy_usd, sell_usd = self.directional_quote_rates(
            buy_key=buy_key, sell_key=sell_key)
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
        """Refresh Kraken quote books as one atomic observation set.

        USDG/USDC is read directly so its executable bid/ask spread is retained.
        Kraken level timestamps describe when a resting level last changed, not
        when this REST snapshot was received, so freshness uses receipt time.
        """
        assets = sorted(set(self.quote_assets.values()) - {"USD"})
        if not assets:
            return
        pending: Dict[str, RateObservation] = {}
        pending_pairs: Dict[tuple[str, str], BookObservation] = {}
        direct_usdg_usdc = {"USDC", "USDG"}.issubset(assets)
        usd_assets = [asset for asset in assets
                      if not (direct_usdg_usdc and asset == "USDG")]
        try:
            for asset in usd_assets:
                book = await self._fetch_kraken_book(
                    session, source_url, f"{asset}USD")
                pending[asset] = RateObservation(
                    book.mid, book.observed_at, book.source)
            if direct_usdg_usdc:
                cross = await self._fetch_kraken_book(
                    session, source_url, "USDGUSDC")
                pending_pairs[("USDG", "USDC")] = cross
                usdc = pending["USDC"]
                pending["USDG"] = RateObservation(
                    cross.mid * usdc.value,
                    min(cross.observed_at, usdc.observed_at),
                    cross.source)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError,
                TypeError, IndexError, KeyError):
            # Keep the prior timestamps. They naturally become stale and fail
            # closed if either the USD anchor or direct cross is unavailable.
            return
        self.quote_usd.update(pending)
        self.quote_pairs.update(pending_pairs)

    async def _fetch_kraken_book(self, session: aiohttp.ClientSession,
                                 source_url: str,
                                 pair: str) -> BookObservation:
        url = source_url.rstrip("/") + "/0/public/Depth"
        async with session.get(
                url, params={"pair": pair, "count": "1"},
                timeout=aiohttp.ClientTimeout(total=10.0)) as response:
            response.raise_for_status()
            data = await response.json()
        observed_at = time.time()
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
        bid, bid_qty, _ = self._kraken_level(book.get("bids"))
        ask, ask_qty, _ = self._kraken_level(book.get("asks"))
        if bid > ask:
            raise ValueError("Kraken book is crossed")
        spread_bps = (ask / bid - 1.0) * 1e4
        if (not math.isfinite(spread_bps)
                or spread_bps > self.stablecoin_max_spread_bps + 1e-9):
            raise ValueError("Kraken book spread is too wide")
        if bid_qty <= 0 or ask_qty <= 0:
            raise ValueError("Kraken book quantity must be positive")
        return BookObservation(
            bid, ask, observed_at, f"{url}?pair={pair}&count=1")

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
