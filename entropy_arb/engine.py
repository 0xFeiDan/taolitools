"""Two-venue arbitrage engine: Entropy vs one hedge venue.

The signal is a fixed band around a configured midline (config.yaml):

    SELL entropy / BUY hedge  when executable premium >= midline + upper (+fees)
    BUY entropy / SELL hedge  when executable premium <= midline - lower (+fees)

Around the signal: per-direction persistence arming,
per-venue inventory ladder + position caps, per-venue order budgets and
reactive rate-limit exclusion, net-delta hedging, venue-outage pausing with
probing, and periodic on-chain reconciliation. There is no paper mode: the
bot either trades live or runs --record-only (data collection, no strategy).
Both venues' books are recorded to 1-minute CSV bars throughout.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import time
from collections import deque
from typing import Dict, List, Optional

import aiohttp

from .book import ArbPlan, floor_step, plan_arb, plan_vwap_arb
from .config import Config
from .costs import CostMonitor
from .ledger import PairLedger, PairPnL
from .midline import DynamicMidline, RegimeDetector
from .metrics import LatencyStats
from .models import (
    ExecutionState,
    PairDirection,
    PairExecution,
    PairPosition,
    PairStateEvent,
    RiskAction,
    RiskEvent,
    SignalAction,
    make_pair_id,
)
from .recorder import MinuteRecorder
from .session import SessionClock
from .venue_hl import HLVenue
from .venue_lighter import LighterVenue

log = logging.getLogger("engine")

CSV_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
              "buy_limit", "sell_limit", "buy_notional", "sell_notional",
              "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
              "sizing_mode", "buy_vwap", "sell_vwap",
              "gross_vwap_edge_bps", "adjusted_gross_edge_bps",
              "funding_cost_bps", "stablecoin_basis_bps",
              "expected_net_edge_bps",
              "required_net_edge_bps", "fee_cost_bps", "extra_cost_bps",
              "max_vwap_slippage_bps", "max_book_impact_bps",
              "spread_bps", "midline_bps", "fast_midline_bps",
              "volatility_bps", "deviation_bps", "z_score", "regime_state",
              "pair_id", "signal_action", "market_session",
              "inv_add_bps", "ok", "buy_fill", "sell_fill",
              "buy_status", "sell_status", "fill_edge_usd"]
BALANCE_POLL_SEC = 30.0
TERMINAL_ORDER_STATUS_PREFIXES = (
    "filled", "canceled", "cancelled", "rejected", "expired",
    "send-failed", "preflight-rejected",
)


class Engine:
    def __init__(self, cfg: Config, record_only: bool = False,
                 clear_risk_pause: bool = False) -> None:
        self.cfg = cfg
        self.record_only = record_only
        self.clear_risk_pause_requested = clear_risk_pause
        self.session: Optional[aiohttp.ClientSession] = None
        self.entropy = None
        self.hedge = None
        self.venues: Dict[str, object] = {}
        self.recorder: Optional[MinuteRecorder] = None
        self.markets_ready = False
        self.stop = asyncio.Event()
        self._update_evt = asyncio.Event()
        self._reconcile_evt = asyncio.Event()
        # per-venue locks: an execution holds both; a reconcile holds one, so
        # a chain read can never race an in-flight order on that venue
        self._venue_locks: Dict[str, asyncio.Lock] = {}
        self._exec_tasks: set = set()
        self.halted = False
        self.consec_errors = 0
        self.last_trade_ts = 0.0
        self.trades = 0
        self.hedges = 0
        self.total_exp_edge = 0.0
        self.total_fill_edge = 0.0
        self.start_ts = time.time()
        self._last_skiplog = 0.0
        self._poke_due: Optional[float] = None
        # per-direction persistence arming: direction key -> first-seen ts
        self._armed: Dict[str, Optional[float]] = {"sell_entropy": None,
                                                   "buy_entropy": None}
        self._step = 1e-4
        self._min_base = 0.0
        self._min_notional = 10.0
        self._mtm_baseline: Optional[float] = None
        # proactive per-venue send budget: timestamps of recent order sends
        self._sends: Dict[str, deque] = {}
        # reactive per-venue throttle: venue key -> excluded until
        self._venue_limited_until: Dict[str, float] = {}
        # venue outage tracking: key -> down-since ts; a down venue pauses
        # trading and is probed every venue_probe_sec until it answers
        self._venue_down: Dict[str, float] = {}
        self._venue_probe_at: Dict[str, float] = {}
        self._venue_fetch_fails: Dict[str, int] = {}
        # per-execution records for the dashboard (newest last)
        self.recent_trades: deque = deque(maxlen=50)
        self.latency = LatencyStats()
        self.pair_position = PairPosition()
        self.execution_history: deque = deque(maxlen=200)
        self._recovery_executions: List[PairExecution] = []
        self._unmatched_legs: Dict[str, dict] = {}
        self.risk_events: deque = deque(maxlen=200)
        self._entry_pause_reasons: set[str] = set()
        self._transient_entry_pause_reasons: set[str] = set()
        self._active_risk_triggers: set[str] = set()
        self.consecutive_partial_fills = 0
        self._unhedged_since: Optional[float] = None
        self._flatten_required = False
        self._flatten_lock = asyncio.Lock()
        self._flatten_attempts = 0
        self._next_flatten_at = 0.0
        self._cost_pause_trigger: Optional[str] = None
        self.costs = CostMonitor(
            funding_enabled=cfg.funding.enabled,
            expected_holding_hours=cfg.funding.expected_holding_hours,
            funding_max_age_seconds=cfg.funding.max_age_seconds,
            stablecoin_enabled=cfg.stablecoin.enabled,
            stablecoin_max_age_seconds=cfg.stablecoin.max_age_seconds,
            warning_deviation_bps=cfg.stablecoin.warning_deviation_bps,
            halt_deviation_bps=cfg.stablecoin.halt_deviation_bps,
            quote_assets={"entropy": cfg.entropy.quote_asset,
                          "hedge": cfg.hedge.quote_asset},
            stablecoin_max_spread_bps=cfg.stablecoin.max_spread_bps)
        self.ledger = (PairLedger(cfg.accounting.ledger_jsonl,
                                  cfg.accounting.state_json)
                       if cfg.accounting.enabled else None)
        self.session_clock = SessionClock(cfg.session.enabled)
        self.market_session = self.session_clock.status()
        self._session_midlines = {}
        self._session_regimes = {}
        self.dynamic_midline = self._new_dynamic_midline()
        self.spread_stats = None
        self._last_ready_spread_stats = None
        self._last_spread_sample_ts: Optional[float] = None
        self._last_spread_book_versions = None
        self.regime_detector = self._new_regime_detector()
        self._session_midlines[self.market_session.stats_key] = (
            self.dynamic_midline)
        self._session_regimes[self.market_session.stats_key] = (
            self.regime_detector)
        self._restore_runtime()

    def _restore_runtime(self) -> None:
        if self.ledger is None:
            return
        if self.ledger.current is not None:
            pair = self.ledger.current
            self.pair_position.sync(
                PairDirection(pair.direction), pair.remaining_base,
                pair_id=pair.pair_id, at=pair.entry_time)
        runtime = self.ledger.runtime
        self._entry_pause_reasons.update(runtime.get("entry_pause_reasons", []))
        self._active_risk_triggers.update(self._entry_pause_reasons)
        self._flatten_required = bool(runtime.get("flatten_required", False))
        self._flatten_attempts = int(runtime.get("flatten_attempts", 0))
        self._unmatched_legs = {
            str(key): dict(value) for key, value in
            (runtime.get("unmatched_legs") or {}).items()}
        for raw in runtime.get("risk_events", [])[-200:]:
            try:
                self.risk_events.append(RiskEvent(
                    trigger=raw["trigger"], action=RiskAction(raw["action"]),
                    reason=raw["reason"], at=float(raw["at"]),
                    pair_id=raw.get("pair_id"),
                    observed_value=raw.get("observed_value"),
                    threshold=raw.get("threshold"),
                    data=dict(raw.get("data") or {})))
            except (KeyError, TypeError, ValueError):
                continue
        for raw in runtime.get("executions", [])[-200:]:
            try:
                events = [PairStateEvent(
                    at=float(event["at"]),
                    from_state=(ExecutionState(event["from_state"])
                                if event.get("from_state") else None),
                    to_state=ExecutionState(event["to_state"]),
                    reason=event["reason"], data=dict(event.get("data") or {}))
                    for event in raw.get("events", [])]
                self.execution_history.append(PairExecution(
                    pair_id=raw["pair_id"], symbol=raw["symbol"],
                    venue_a=raw["venue_a"], venue_b=raw["venue_b"],
                    direction=PairDirection(raw["direction"]),
                    state=ExecutionState(raw["state"]),
                    created_at=float(raw["created_at"]),
                    updated_at=float(raw["updated_at"]), events=events))
            except (KeyError, TypeError, ValueError):
                continue
        if any(item.state in {
                ExecutionState.ORDERS_SENT, ExecutionState.PARTIAL,
                ExecutionState.BOTH_FILLED, ExecutionState.RECOVERY,
                ExecutionState.HEDGED, ExecutionState.UNWINDING,
        } for item in self.execution_history):
            # A crash can happen after network dispatch but before a terminal
            # result or pause event is durably recorded. The non-terminal
            # execution record is itself sufficient evidence of ambiguity.
            self._entry_pause_reasons.add("restart_inflight_execution")
            self._active_risk_triggers.add("restart_inflight_execution")

    def _runtime_state(self) -> dict:
        executions = []
        for item in self.execution_history:
            executions.append({
                "pair_id": item.pair_id, "symbol": item.symbol,
                "venue_a": item.venue_a, "venue_b": item.venue_b,
                "direction": item.direction.value, "state": item.state.value,
                "created_at": item.created_at, "updated_at": item.updated_at,
                "events": [{
                    "at": event.at,
                    "from_state": (event.from_state.value
                                   if event.from_state else None),
                    "to_state": event.to_state.value,
                    "reason": event.reason, "data": event.data,
                } for event in item.events],
            })
        risks = [{
            "trigger": event.trigger, "action": event.action.value,
            "reason": event.reason, "at": event.at,
            "pair_id": event.pair_id,
            "observed_value": event.observed_value,
            "threshold": event.threshold, "data": event.data,
        } for event in self.risk_events]
        return {
            "entry_pause_reasons": sorted(self._entry_pause_reasons),
            "flatten_required": self._flatten_required,
            "flatten_attempts": self._flatten_attempts,
            "unmatched_legs": self._unmatched_legs,
            "executions": executions, "risk_events": risks,
        }

    def _persist_runtime(self) -> None:
        if self.ledger is None:
            return
        try:
            self.ledger.snapshot(self._runtime_state())
        except Exception as exc:
            self._mark_persistence_failure(exc)

    def _mark_persistence_failure(self, exc: BaseException) -> None:
        # Persistence is itself a risk control. Keep risk-reducing tasks
        # alive, but permanently stop strategy entry in this process.
        self.halted = True
        self._entry_pause_reasons.add("persistence_failure")
        self._active_risk_triggers.add("persistence_failure")
        log.critical("[RISK] persistence failure; OPEN/ADD halted: %r", exc)

    def _require_live_persistence(self) -> None:
        if not self.record_only and self.ledger is None:
            raise RuntimeError(
                "live trading requires accounting.enabled: true so unknown "
                "orders and recovery state survive restart")
        if (not self.record_only
                and any(venue.quote_asset != "USD"
                        for venue in (self.cfg.entropy, self.cfg.hedge))
                and not self.cfg.stablecoin.enabled):
            raise RuntimeError(
                "live trading with non-USD quote assets requires fresh "
                "stablecoin USD conversion; enable stablecoin and VWAP sizing")

    async def _wait_for_inflight_shutdown(
            self, timeout: Optional[float] = None) -> set[asyncio.Task]:
        """Bound shutdown while persisting ambiguity before tasks are lost."""
        if not self._exec_tasks:
            return set()
        log.info("waiting for %d in-flight execution(s) to settle",
                 len(self._exec_tasks))
        _, pending = await asyncio.wait(
            self._exec_tasks,
            timeout=(self.cfg.settle_timeout_sec + 2.0
                     if timeout is None else timeout))
        if pending:
            self._risk_event(
                "shutdown_inflight_unknown", RiskAction.PAUSE_NEW_ENTRY,
                f"shutdown timed out with {len(pending)} execution task(s) "
                "still unresolved; startup reconciliation required",
                persistent=True)
            self._reconcile_evt.set()
        return set(pending)

    def _clear_persistent_risk_if_safe(self) -> None:
        if not self.clear_risk_pause_requested:
            return
        if self._flatten_required:
            raise RuntimeError("cannot clear risk pause while emergency flatten "
                               "is pending")
        nonflat = {venue.name: venue.position for venue in self.venues.values()
                   if abs(venue.position) > self.cfg.net_tolerance_base}
        if nonflat:
            raise RuntimeError("cannot clear risk pause with non-flat positions: "
                               + repr(nonflat))
        cleared = sorted(self._entry_pause_reasons)
        self._entry_pause_reasons.clear()
        self._active_risk_triggers.difference_update(cleared)
        if self.ledger is not None:
            self.ledger.append_event("RISK_PAUSE_CLEARED", {
                "triggers": cleared, "operator_requested": True})
        self._persist_runtime()
        log.warning("operator cleared persisted risk pauses while flat: %s",
                    cleared or "none")

    def _guard_startup_positions(self) -> None:
        """Fail closed before strategy tasks exist when startup is unhedged."""
        net = sum(venue.position for venue in self.venues.values())
        if abs(net) <= self.cfg.net_tolerance_base:
            return
        self._risk_event(
            "startup_unhedged_position", RiskAction.PAUSE_NEW_ENTRY,
            "startup positions are not delta neutral; reconcile/hedge required",
            observed_value=abs(net), threshold=self.cfg.net_tolerance_base,
            persistent=True)
        self._reconcile_evt.set()

    # ------------------------------------------------------------- utilities

    def _vlock(self, key: str) -> asyncio.Lock:
        lock = self._venue_locks.get(key)
        if lock is None:
            lock = self._venue_locks[key] = asyncio.Lock()
        return lock

    def _venue_rate_ok(self, v) -> bool:
        """True while the venue is under its max_orders_per_min (sliding 60s)."""
        dq = self._sends.setdefault(v.key, deque())
        now = time.time()
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        return len(dq) < v.orders_per_min

    def _venue_limited(self, v) -> bool:
        return time.time() < self._venue_limited_until.get(v.key, 0.0)

    def _mark_limited(self, v) -> None:
        self._venue_limited_until[v.key] = time.time() + self.cfg.rate_limit_pause_sec
        log.warning("[%s] rate limited — trading paused for %.0fs",
                    v.name, self.cfg.rate_limit_pause_sec)

    def _record_send(self, v) -> None:
        self._sends.setdefault(v.key, deque()).append(time.time())

    @staticmethod
    def _consume_background_order(task: asyncio.Task) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def _send_bounded(self, venue, *, is_buy: bool, qty: float,
                            limit_px: float, reduce_only: bool) -> object:
        """Bound every adapter call; timeout means UNKNOWN, never no-fill."""
        task = asyncio.create_task(venue.send_taker(
            is_buy=is_buy, qty=qty, limit_px=limit_px,
            reduce_only=reduce_only))
        self._exec_tasks.add(task)
        task.add_done_callback(self._exec_tasks.discard)
        task.add_done_callback(self._consume_background_order)
        done, pending = await asyncio.wait(
            {task}, timeout=self.cfg.settle_timeout_sec)
        if pending:
            task.cancel()
            return {
                "status": "engine-timeout", "filled_base": 0.0,
                "avg_px": None,
                "err": "order response unresolved at engine timeout",
                "unresolved": True, "accounting_complete": False,
            }
        try:
            return task.result()
        except asyncio.CancelledError:
            return {
                "status": "engine-cancelled", "filled_base": 0.0,
                "avg_px": None,
                "err": "order response cancelled before resolution",
                "unresolved": True, "accounting_complete": False,
            }
        except Exception as exc:
            return exc

    def _normalize_order_result(self, raw, requested_qty: float, *,
                                venue_key: str, pair_id: Optional[str] = None
                                ) -> dict:
        """Validate untrusted adapter output before it reaches accounting."""
        if not isinstance(raw, dict):
            raw = {
                "status": "adapter-exception", "filled_base": 0.0,
                "avg_px": None,
                "err": f"{type(raw).__name__} from venue adapter",
                "unresolved": True,
            }
        info = dict(raw)
        invalid = False
        try:
            filled = float(info.get("filled_base") or 0.0)
        except (TypeError, ValueError):
            filled = 0.0
            invalid = True
        tolerance = max(self._step / 2.0, 1e-12)
        if (not math.isfinite(filled) or filled < 0
                or filled > requested_qty + tolerance):
            filled = 0.0
            invalid = True
        avg_raw = info.get("avg_px")
        avg_px = None
        if avg_raw not in (None, ""):
            try:
                avg_px = float(avg_raw)
            except (TypeError, ValueError):
                avg_px = None
            if avg_px is None or not math.isfinite(avg_px) or avg_px <= 0:
                avg_px = None
                if filled > 0:
                    info["accounting_complete"] = False
        if invalid:
            info.update({
                "status": "invalid-result", "filled_base": 0.0,
                "avg_px": None, "unresolved": True,
                "accounting_complete": False,
            })
            self._risk_event(
                "invalid_order_result", RiskAction.PAUSE_NEW_ENTRY,
                f"{venue_key} returned an invalid fill quantity",
                pair_id=pair_id, persistent=True)
        else:
            info["filled_base"] = filled
            info["avg_px"] = avg_px
            status = str(info.get("status", "unknown")).strip().lower()
            terminal = status.startswith(TERMINAL_ORDER_STATUS_PREFIXES)
            info["unresolved"] = (bool(info.get("unresolved"))
                                  or not terminal)
            if filled > 0 and avg_px is None:
                info["accounting_complete"] = False
        info.setdefault("status", "unknown")
        info.setdefault("err", None)
        for key in ("order_send_ts", "order_ack_ts", "first_fill_ts",
                    "final_fill_ts"):
            value = info.get(key)
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
            info[key] = value if value is not None and math.isfinite(value) else None
        return info

    def _book_quality(self, v, now: Optional[float] = None):
        max_book_age_ms = (self.cfg.market_data.max_book_age_ms
                           if self.cfg.market_data.enforce_book_age else None)
        return v.book.quality(
            self.cfg.staleness_sec, max_book_age_ms=max_book_age_ms, now=now)

    def _record_market_latency(self, buy, sell, signal_ts: float) -> None:
        updates = [buy.book.last_update_ts, sell.book.last_update_ts]
        if all(ts > 0 for ts in updates):
            self.latency.record(
                "market_to_signal_ms",
                (signal_ts - max(updates)) * 1000.0)
            self.latency.record(
                "book_update_skew_ms", abs(updates[0] - updates[1]) * 1000.0)
        for venue in (buy, sell):
            self.latency.record(
                f"{venue.key}_exchange_to_local_ms",
                venue.book.exchange_lag_ms())

    def _new_dynamic_midline(self) -> DynamicMidline:
        mid = self.cfg.midline
        return DynamicMidline(
            fast_window_seconds=mid.fast_window_seconds,
            slow_window_seconds=mid.slow_window_seconds,
            min_samples=mid.min_samples,
            volatility_method=mid.volatility_method,
            volatility_window_seconds=mid.volatility_window_seconds,
            volatility_floor_bps=mid.volatility_floor_bps)

    def _new_regime_detector(self):
        regime = self.cfg.regime
        return (RegimeDetector(
            max_fast_slow_difference_bps=(
                regime.max_fast_slow_difference_bps),
            max_z_score=regime.max_z_score,
            max_absolute_spread_bps=regime.max_absolute_spread_bps,
            break_persist_seconds=regime.break_persist_seconds,
            recovery_persist_seconds=regime.recovery_persist_seconds)
                if regime.enabled else None)

    def _activate_market_session(self, now: Optional[float] = None):
        """Select the independent Dynamic Midline bank for this session."""
        status = self.session_clock.status(now)
        previous_key = self.market_session.stats_key
        self.market_session = status
        if status.stats_key == previous_key:
            return status

        if status.sampleable:
            self.dynamic_midline = self._session_midlines.setdefault(
                status.stats_key, self._new_dynamic_midline())
            if status.stats_key not in self._session_regimes:
                self._session_regimes[status.stats_key] = (
                    self._new_regime_detector())
            self.regime_detector = self._session_regimes[status.stats_key]
            self.spread_stats = self.dynamic_midline.latest
        else:
            self.spread_stats = None
            self.regime_detector = None
        self._last_spread_sample_ts = None
        self._last_spread_book_versions = None
        log.info("[SESSION] %s -> %s (%s)", previous_key,
                 status.session.value, status.reason or "active")
        return status

    def active_midline_bps(self) -> float:
        self._activate_market_session()
        if (self.cfg.midline.mode == "dynamic" and self.spread_stats is not None
                and self.spread_stats.ready):
            return self.spread_stats.slow_midline_bps
        return self.cfg.midline_bps

    def _update_spread_state(self, now: float, *, force: bool = False) -> None:
        """Sample fresh mid prices at most once per second."""
        session = self._activate_market_session(now)
        if not session.sampleable:
            return
        if self.cfg.midline.mode != "dynamic" and not self.cfg.regime.enabled:
            return
        if self.entropy is None or self.hedge is None:
            return
        if (not force and self._last_spread_sample_ts is not None
                and now - self._last_spread_sample_ts < 1.0):
            return
        if not (self._book_quality(self.entropy, now).ok
                and self._book_quality(self.hedge, now).ok):
            return
        book_versions = (self.entropy.book.last_update_ts,
                         self.hedge.book.last_update_ts)
        if not force and book_versions == self._last_spread_book_versions:
            return
        spread = self.premium_bps()
        if spread is None:
            return
        self.spread_stats = self.dynamic_midline.update(now, spread)
        if self.spread_stats.ready:
            self._last_ready_spread_stats = self.spread_stats
        if self.ledger is not None and self.ledger.current is not None:
            self.ledger.current.update_market(spread)
        self._last_spread_sample_ts = now
        self._last_spread_book_versions = book_versions
        if self.regime_detector is not None and self.spread_stats.ready:
            before = self.regime_detector.status
            after = self.regime_detector.update(self.spread_stats, now)
            if after.paused and not before.paused:
                log.error("[REGIME] PAUSE_NEW_ENTRY: %s | spread %.2f "
                          "fast %.2f slow %.2f z %.2f",
                          ",".join(after.reasons), self.spread_stats.spread_bps,
                          self.spread_stats.fast_midline_bps,
                          self.spread_stats.slow_midline_bps,
                          self.spread_stats.z_score)
                self._risk_event(
                    "regime_break", RiskAction.PAUSE_NEW_ENTRY,
                    ",".join(after.reasons),
                    observed_value=abs(self.spread_stats.z_score),
                    threshold=self.cfg.regime.max_z_score)
            elif before.paused and not after.paused:
                log.warning("[REGIME] recovered after %.1fs healthy; new entries "
                            "resumed", self.cfg.regime.recovery_persist_seconds)
                self._clear_transient_risk("regime_break")

    def strategy_pause_reason(self) -> Optional[str]:
        self._activate_market_session()
        if self.cfg.midline.mode == "dynamic":
            if self.spread_stats is None or not self.spread_stats.ready:
                return "dynamic_warmup"
        if self.cfg.regime.enabled:
            if self.spread_stats is None or not self.spread_stats.ready:
                return "regime_warmup"
            status = self.regime_detector.status
            if status.paused:
                return "regime:" + ",".join(status.reasons)
        if self._entry_pause_reasons:
            return "kill:" + ",".join(sorted(self._entry_pause_reasons))
        if self._transient_entry_pause_reasons:
            return "risk:" + ",".join(
                sorted(self._transient_entry_pause_reasons))
        cost_reason = self.costs.pause_reason()
        if cost_reason is not None:
            return "cost:" + cost_reason
        return None

    def _risk_event(self, trigger: str, action: RiskAction, reason: str, *,
                    observed_value: Optional[float] = None,
                    threshold: Optional[float] = None,
                    pair_id: Optional[str] = None,
                    persistent: bool = False) -> None:
        if trigger in self._active_risk_triggers:
            return
        self._active_risk_triggers.add(trigger)
        event = RiskEvent(
            trigger=trigger, action=action, reason=reason,
            pair_id=pair_id, observed_value=observed_value,
            threshold=threshold)
        self.risk_events.append(event)
        if persistent or action is RiskAction.EMERGENCY_FLATTEN:
            self._entry_pause_reasons.add(trigger)
        elif action in (RiskAction.PAUSE_NEW_ENTRY,
                         RiskAction.EMERGENCY_HEDGE):
            self._transient_entry_pause_reasons.add(trigger)
        log.critical("[RISK] %s -> %s: %s (value=%s threshold=%s pair=%s)",
                     trigger, action.value, reason, observed_value, threshold,
                     pair_id or "—")
        self._persist_runtime()

    def _clear_transient_risk(self, trigger: str) -> None:
        if trigger not in self._entry_pause_reasons:
            removed = trigger in self._active_risk_triggers
            self._active_risk_triggers.discard(trigger)
            self._transient_entry_pause_reasons.discard(trigger)
            if removed:
                self._persist_runtime()

    def request_stop(self) -> None:
        self._persist_runtime()
        self.stop.set()
        self._update_evt.set()
        self._reconcile_evt.set()

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        # Long keepalive so order-path connections survive quiet spells; the
        # keepalive loop pings inside this window to hold them open.
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
            keepalive_timeout=75.0, ttl_dns_cache=300))
        try:
            await self._run_inner()
        finally:
            await self.session.close()

    def _make_venue(self, vc):
        if vc.kind == "lighter":
            return LighterVenue(vc, self.session, self.cfg.settle_timeout_sec)
        return HLVenue(vc, self.cfg.hl_api_url, self.cfg.hl_ws_url,
                       self.session, self.cfg.settle_timeout_sec)

    async def _run_inner(self) -> None:
        cfg = self.cfg
        self._require_live_persistence()
        self.entropy = self._make_venue(cfg.entropy)
        self.hedge = self._make_venue(cfg.hedge)
        self.venues = {"entropy": self.entropy, "hedge": self.hedge}
        await asyncio.gather(self.entropy.load_market(), self.hedge.load_market())
        self.markets_ready = True
        if cfg.funding.enabled or cfg.stablecoin.enabled:
            await self._refresh_costs_once()

        live = not self.record_only
        if live:
            if not cfg.creds_complete:
                raise RuntimeError(
                    "live trading needs credentials for both venues in .env "
                    "(see .env.example); use --record-only to run without "
                    "them / 实盘需要在 .env 中配置两个交易所的密钥，仅采集数据"
                    "请用 --record-only")
            self.entropy.init_signer()
            self.hedge.init_signer()
            if self.hedge.kind == "hl":
                self.entropy.share_nonces_with(self.hedge)
        if (self.hedge.kind == "hl"
                and self.entropy._query_address()
                and self.entropy._query_address() == self.hedge._query_address()):
            self.hedge.include_core_equity = False  # shared account: count once

        self._step = 10 ** -min(self.entropy.size_decimals,
                                self.hedge.size_decimals)
        self._min_base = max(self.entropy.min_base, self.hedge.min_base,
                             self._step)
        self._min_notional = max(cfg.min_order_notional,
                                 self.entropy.min_quote, self.hedge.min_quote)
        log.info("pair ENTROPY(%s)-%s(%s): midline=%s seed=%+.2fbps "
                 "band=[-%.2f, +%.2f] "
                 "fees=%.2f+%.2f step=%g min_ntl=$%g",
                 self.entropy.conf.symbol, self.hedge.name,
                 self.hedge.conf.symbol, cfg.midline.mode, cfg.midline_bps,
                 cfg.lower_bps, cfg.upper_bps,
                 self.entropy.fee_bps, self.hedge.fee_bps,
                 self._step, self._min_notional)

        if self.record_only:
            log.warning("RECORD-ONLY — collecting minute data, no strategy, "
                        "no orders")
        else:
            log.warning("LIVE — real orders will be sent (use --record-only "
                        "for credential-less data collection)")
            await self._reconcile_positions(hedge=False, strict=True)
            self._guard_startup_positions()
            self._clear_persistent_risk_if_safe()
            log.info("starting positions: %s (net %+.6g)",
                     " ".join(f"{v.name}={v.position:+.6g}"
                              for v in self.venues.values()),
                     sum(v.position for v in self.venues.values()))

        tasks: List[asyncio.Task] = []
        for v in self.venues.values():
            tasks += v.start_tasks(self.stop, self._update_evt.set, live)
        if cfg.recorder_enabled or self.record_only:
            rate_getter = (self.costs.fresh_quote_rate
                           if cfg.stablecoin.enabled
                           or (cfg.entropy.quote_asset == "USD"
                               and cfg.hedge.quote_asset == "USD")
                           else None)
            self.recorder = MinuteRecorder(cfg.recorder_csv, self.entropy.book,
                                           self.hedge.book, cfg.staleness_sec,
                                           quote_rate_getter=rate_getter,
                                           quote_pair_getter=(
                                               self.costs.fresh_quote_pair
                                               if cfg.stablecoin.enabled
                                               else None),
                                           entropy_quote_asset=(
                                               cfg.entropy.quote_asset),
                                           hedge_quote_asset=(
                                               cfg.hedge.quote_asset))
            tasks.append(asyncio.create_task(self.recorder.run(self.stop),
                                             name="recorder"))
        if cfg.midline.mode == "dynamic" or cfg.regime.enabled:
            tasks.append(asyncio.create_task(self._spread_stats_loop(),
                                             name="spread-stats"))
        if cfg.execution_risk.enabled or cfg.kill_switch.enabled:
            tasks.append(asyncio.create_task(self._risk_loop(), name="risk"))
        if cfg.funding.enabled or cfg.stablecoin.enabled:
            tasks.append(asyncio.create_task(self._cost_loop(), name="costs"))
        if not self.record_only:
            tasks.append(asyncio.create_task(self._strategy_loop(),
                                             name="strategy"))
            tasks.append(asyncio.create_task(self._balance_loop(),
                                             name="balances"))
            tasks.append(asyncio.create_task(self._http_keepalive_loop(),
                                             name="keepalive"))
        tasks.append(asyncio.create_task(self._status_loop(), name="status"))
        if live:
            tasks.append(asyncio.create_task(self._reconcile_loop(),
                                             name="reconcile"))

        await self.stop.wait()
        await self._wait_for_inflight_shutdown()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for v in self.venues.values():
            await v.close()
        self._persist_runtime()
        log.info("shutdown — %d trades, %d hedges, exp edge $%.4f, "
                 "fill edge $%.4f", self.trades, self.hedges,
                 self.total_exp_edge, self.total_fill_edge)

    # --------------------------------------------------------------- signals

    def _inv_add_bps(self, buy, sell) -> float:
        """Inventory ladder: a surcharge that grows once a venue's position
        passes floor_frac of its cap in the direction the trade would add to
        (buying adds when that venue is >= flat long; selling adds when the
        venue is <= flat short). Max of the two venues' ramps."""
        scale = self.cfg.inventory_scale_bps
        if scale <= 0:
            return 0.0
        floor = min(max(self.cfg.inventory_floor_frac, 0.0), 0.99)

        def ramp(v, adding: bool) -> float:
            if not adding:
                return 0.0
            ref = v.book.mid()
            if ref is None:
                return 0.0
            u = min(self._notional_usd(v, v.position, ref) / v.cap_usd, 1.0)
            if u <= floor:
                return 0.0
            return scale * (u - floor) / (1.0 - floor)

        return max(ramp(buy, buy.position >= 0), ramp(sell, sell.position <= 0))

    def _eff_threshold(self, buy, sell) -> float:
        """Net hurdle (bps, on top of fees) for the direction buy->sell.

        selling entropy: executable premium must clear midline + upper;
        buying entropy: the raw lower boundary is converted by reciprocal."""
        deviation = ((self.cfg.upper_bps if sell.key == "entropy"
                      else self.cfg.lower_bps)
                     + self._inv_add_bps(buy, sell))
        raw_target = (self.active_midline_bps() + deviation
                      if sell.key == "entropy" else
                      self.active_midline_bps() - deviation)
        return self._directional_midline_usd(buy, sell, raw_target)

    @staticmethod
    def _pair_direction_for_key(direction_key: str) -> PairDirection:
        return (PairDirection.SELL_ENTROPY
                if direction_key == "sell_entropy"
                else PairDirection.BUY_ENTROPY)

    def _signal_action(self, direction_key: str) -> SignalAction:
        direction = self._pair_direction_for_key(direction_key)
        if not self.pair_position.is_open:
            return SignalAction.OPEN
        if self.pair_position.direction is direction:
            return SignalAction.ADD
        return SignalAction.EXIT

    def _closeable_pair_base(self) -> float:
        if not self.pair_position.is_open:
            return 0.0
        if self.pair_position.direction is PairDirection.SELL_ENTROPY:
            venue_base = min(max(-self.entropy.position, 0.0),
                             max(self.hedge.position, 0.0))
        else:
            venue_base = min(max(self.entropy.position, 0.0),
                             max(-self.hedge.position, 0.0))
        return max(min(self.pair_position.base_qty, venue_base), 0.0)

    def _dynamic_signal_context(self, buy, sell, direction_key: str):
        """Return (action, required net edge bps, max base) or None."""
        action = self._signal_action(direction_key)
        stats = self.spread_stats
        # A session transition must never trap an existing Pair merely because
        # the new session's independent estimator is still warming up.  EXIT
        # may conservatively use the last ready baseline; OPEN/ADD may not.
        if ((stats is None or not stats.ready)
                and action is SignalAction.EXIT):
            stats = self._last_ready_spread_stats
        if stats is None or not stats.ready:
            return None
        direction = self._pair_direction_for_key(direction_key)
        entry_z = self.cfg.midline.entry_z_score
        exit_z = self.cfg.midline.exit_z_score
        z = stats.z_score
        if action in (SignalAction.OPEN, SignalAction.ADD):
            qualifies = (z >= entry_z if direction is PairDirection.SELL_ENTROPY
                         else z <= -entry_z)
            if not qualifies:
                return None
            deviation = max(
                entry_z * stats.volatility_bps
                + self._inv_add_bps(buy, sell),
                self.cfg.vwap_sizing.minimum_net_edge_bps
                if self.cfg.vwap_sizing.enabled else 0.0)
            raw_target = (stats.slow_midline_bps + deviation
                          if sell.key == "entropy" else
                          stats.slow_midline_bps - deviation)
            required = self._directional_midline_usd(
                buy, sell, raw_target)
            return action, required, None

        # EXIT always reduces the currently tracked opposite pair. Its edge
        # threshold is the configured return-to-center zone, not entry edge.
        qualifies = (z >= -exit_z if direction is PairDirection.SELL_ENTROPY
                     else z <= exit_z)
        if not qualifies:
            return None
        max_exit_base = self._closeable_pair_base()
        if max_exit_base < self._min_base:
            return None
        exit_deviation = exit_z * stats.volatility_bps
        raw_target = (stats.slow_midline_bps - exit_deviation
                      if sell.key == "entropy" else
                      stats.slow_midline_bps + exit_deviation)
        required = self._directional_midline_usd(buy, sell, raw_target)
        return action, required, max_exit_base

    def _directional_midline_usd(self, buy, sell,
                                 entropy_midline_bps: float) -> float:
        """Convert the configured Entropy/hedge basis into this leg direction.

        The reverse direction is a reciprocal, not simply ``-midline``. Quote
        assets are converted exactly once for legacy ``raw`` thresholds;
        ``usd`` thresholds are already normalized by the recorder/analyzer.
        """
        entropy_hedge_ratio = 1.0 + entropy_midline_bps / 1e4
        if (not math.isfinite(entropy_hedge_ratio)
                or entropy_hedge_ratio <= 0):
            raise ValueError("midline implies a non-positive price ratio")
        raw_direction_ratio = (entropy_hedge_ratio
                               if sell.key == "entropy"
                               else 1.0 / entropy_hedge_ratio)
        adjusted_ratio = raw_direction_ratio
        if self.cfg.threshold_price_basis == "raw":
            try:
                buy_rate, sell_rate = self.costs.directional_quote_rates(
                    buy_key=buy.key, sell_key=sell.key)
            except KeyError:
                buy_rate = sell_rate = 1.0
            adjusted_ratio *= sell_rate / buy_rate
        return (adjusted_ratio - 1.0) * 1e4

    def _vwap_required_net_edge(self, buy, sell) -> float:
        """Absolute directional hurdle after all modeled execution costs.

        ``minimum_net_edge_bps`` is a minimum deviation from the active
        midline, not from zero.  This preserves the strategy's symmetric
        mean-reversion semantics when the normal cross-venue basis is nonzero.
        """
        inv = self._inv_add_bps(buy, sell)
        configured_band = (self.cfg.upper_bps if sell.key == "entropy"
                           else self.cfg.lower_bps)
        deviation = max(configured_band + inv,
                        self.cfg.vwap_sizing.minimum_net_edge_bps)
        raw_target = (self.active_midline_bps() + deviation
                      if sell.key == "entropy" else
                      self.active_midline_bps() - deviation)
        return self._directional_midline_usd(buy, sell, raw_target)

    def _quote_rate(self, venue) -> float:
        try:
            return self.costs.quote_rate(venue.key)
        except KeyError:
            # Missing enabled cost data already blocks OPEN/ADD. Risk-reducing
            # EXIT/hedge paths still need an executable fallback.
            return 1.0

    def _notional_usd(self, venue, qty: float, price: float) -> float:
        return abs(qty * price) * self._quote_rate(venue)

    def _headroom(self, buy, sell, buy_px: float, sell_px: float) -> float:
        hb = (buy.cap_usd
              - buy.position * buy_px * self._quote_rate(buy))
        hs = (sell.cap_usd
              + sell.position * sell_px * self._quote_rate(sell))
        return min(hb, hs)

    def _plan(self, buy, sell, cap_notional: float, *,
              required_edge_bps: Optional[float] = None,
              max_base: Optional[float] = None,
              signal_action: Optional[SignalAction] = None):
        if self.cfg.vwap_sizing.enabled:
            sizing = self.cfg.vwap_sizing
            direction = ("sell_entropy" if sell.key == "entropy"
                         else "buy_entropy")
            action = signal_action or SignalAction.OPEN
            try:
                funding_cost = (self.costs.funding_cost_bps(direction)
                                if action is not SignalAction.EXIT else 0.0)
                buy_funding_rate = (
                    self.costs.funding_rate(buy.key)
                    if action is not SignalAction.EXIT else None)
                sell_funding_rate = (
                    self.costs.funding_rate(sell.key)
                    if action is not SignalAction.EXIT else None)
                buy_quote_usd, sell_quote_usd = (
                    self.costs.directional_quote_rates(
                        buy_key=buy.key, sell_key=sell.key))
            except KeyError:
                # Missing enabled cost data already blocks OPEN/ADD via
                # strategy_pause_reason. EXIT remains available for safety.
                funding_cost = 0.0
                buy_funding_rate = sell_funding_rate = None
                buy_quote_usd = sell_quote_usd = 1.0
            result = plan_vwap_arb(
                buy.book, sell.book,
                required_net_edge_bps=(
                    self._vwap_required_net_edge(buy, sell)
                    if required_edge_bps is None else required_edge_bps),
                buy_fee_bps=buy.fee_bps, sell_fee_bps=sell.fee_bps,
                min_order_usd=max(
                    sizing.min_order_usd, self.cfg.min_order_notional,
                    buy.min_quote * buy_quote_usd,
                    sell.min_quote * sell_quote_usd),
                max_order_usd=min(sizing.max_order_usd, cap_notional),
                max_vwap_slippage_bps=sizing.max_vwap_slippage_bps,
                max_book_impact_bps=sizing.max_book_impact_bps,
                safety_buffer_bps=sizing.safety_buffer_bps,
                expected_latency_cost_bps=sizing.expected_latency_cost_bps,
                min_base=self._min_base, size_step=self._step,
                max_base=max_base,
                funding_cost_bps=funding_cost,
                buy_funding_rate=buy_funding_rate,
                sell_funding_rate=sell_funding_rate,
                expected_holding_hours=(
                    self.cfg.funding.expected_holding_hours
                    if buy_funding_rate is not None else 0.0),
                buy_quote_usd=buy_quote_usd,
                sell_quote_usd=sell_quote_usd,
            )
        else:
            result = plan_arb(
                buy.book, sell.book,
                threshold_bps=(self._eff_threshold(buy, sell)
                               if required_edge_bps is None
                               else required_edge_bps),
                buy_fee_bps=buy.fee_bps, sell_fee_bps=sell.fee_bps,
                take_fraction=self.cfg.take_fraction,
                cap_notional=cap_notional,
                min_base=self._min_base,
                min_notional=self._min_notional,
                size_step=self._step,
                max_base=max_base,
            )
        plan, reason = result
        if plan is not None:
            action = signal_action or SignalAction.OPEN
            stats = self.spread_stats
            if ((stats is None or not stats.ready)
                    and action is SignalAction.EXIT):
                stats = self._last_ready_spread_stats
            plan.signal_midline_bps = (
                stats.slow_midline_bps if stats is not None and stats.ready
                else self.active_midline_bps())
            if stats is not None:
                plan.signal_spread_bps = stats.spread_bps
                plan.signal_fast_midline_bps = stats.fast_midline_bps
                plan.signal_volatility_bps = stats.volatility_bps
                plan.signal_deviation_bps = stats.deviation_bps
                plan.signal_z_score = stats.z_score
            status = (self.regime_detector.status
                      if self.regime_detector is not None else None)
            plan.regime_state = ("paused" if status and status.paused else
                                 "breaking" if status and status.breaking else
                                 "healthy" if status else "static")
            plan.signal_action = action.value
            if action is SignalAction.OPEN:
                plan.pair_id = make_pair_id()
            else:
                plan.pair_id = self.pair_position.pair_id
        return plan, reason

    # -------------------------------------------------------------- strategy

    async def _strategy_loop(self) -> None:
        while not self.stop.is_set():
            await self._update_evt.wait()
            self._update_evt.clear()
            if self.stop.is_set():
                break
            try:
                await self._evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("evaluate failed")

    async def _spread_stats_loop(self) -> None:
        """Maintain strategy statistics in both record-only and live modes."""
        while not self.stop.is_set():
            self._update_spread_state(time.time())
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _risk_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self._risk_check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("risk check failed")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

    async def _refresh_pair_funding(self, pair: PairPnL) -> None:
        if not self.cfg.funding.enabled:
            return
        results = await asyncio.gather(
            *(venue.fetch_funding_cost_since(pair.entry_time)
              for venue in self.venues.values()), return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            return
        try:
            funding_usd = sum(
                float(result) * self.costs.quote_rate(venue.key)
                for venue, result in zip(self.venues.values(), results))
        except (KeyError, TypeError, ValueError):
            return
        if not math.isfinite(funding_usd):
            return
        pair.set_realized_funding(funding_usd)
        if self.ledger is not None:
            self.ledger.append_event("PAIR_FUNDING", {
                "pair_id": pair.pair_id, "funding": pair.funding,
                "source": pair.funding_source})
            self._persist_runtime()

    async def _refresh_costs_once(self) -> None:
        if self.cfg.funding.enabled:
            await self.costs.refresh_funding(self.venues.values())
        if self.cfg.stablecoin.enabled and self.session is not None:
            await self.costs.refresh_stablecoins(
                self.session, self.cfg.stablecoin.source_url)
        reason = self.costs.pause_reason()
        if reason is not None:
            trigger = "cost_" + reason.replace(":", "_")
            if self._cost_pause_trigger and self._cost_pause_trigger != trigger:
                self._clear_transient_risk(self._cost_pause_trigger)
            self._cost_pause_trigger = trigger
            self._risk_event(trigger, RiskAction.PAUSE_NEW_ENTRY, reason)
        elif self._cost_pause_trigger:
            self._clear_transient_risk(self._cost_pause_trigger)
            self._cost_pause_trigger = None
        if self.ledger is not None and self.ledger.current is not None:
            await self._refresh_pair_funding(self.ledger.current)

    async def _cost_loop(self) -> None:
        interval = min(
            self.cfg.funding.refresh_seconds if self.cfg.funding.enabled
            else float("inf"),
            self.cfg.stablecoin.refresh_seconds if self.cfg.stablecoin.enabled
            else float("inf"))
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=interval)
                continue
            except asyncio.TimeoutError:
                pass
            try:
                await self._refresh_costs_once()
                self._update_evt.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cost refresh failed")

    def _net_delta_usd(self) -> Optional[float]:
        mids = [v.book.mid() for v in self.venues.values()]
        if not mids or any(mid is None for mid in mids):
            return None
        try:
            reference = sum(
                mid * self.costs.quote_rate(venue.key)
                for venue, mid in zip(self.venues.values(), mids)) / len(mids)
        except KeyError:
            return None
        return abs(sum(v.position for v in self.venues.values())) * reference

    async def _risk_check_once(self) -> None:
        now = time.time()
        if (self.cfg.kill_switch.emergency_flatten_enabled
                and self._flatten_required and now >= self._next_flatten_at
                and (self.cfg.kill_switch.emergency_flatten_max_attempts == 0
                     or self._flatten_attempts
                     < self.cfg.kill_switch.emergency_flatten_max_attempts)):
            await self._emergency_flatten()
        net_base = abs(sum(v.position for v in self.venues.values()))
        net_usd = self._net_delta_usd()
        threshold = self.cfg.execution_risk.max_unhedged_delta_usd
        unpriced_delta = (net_usd is None
                          and net_base > self.cfg.net_tolerance_base)
        if unpriced_delta or (net_usd is not None and net_usd > threshold):
            if self._unhedged_since is None:
                self._unhedged_since = now
                self._risk_event(
                    "net_delta_limit", RiskAction.EMERGENCY_HEDGE,
                    ("net delta cannot be valued because required market/cost "
                     "data is unavailable" if unpriced_delta else
                     "net delta exceeded recovery threshold"),
                    observed_value=(net_base if unpriced_delta else net_usd),
                    threshold=(self.cfg.net_tolerance_base
                               if unpriced_delta else threshold))
            await self._maybe_hedge()
            self._sync_pair_position_from_venues()
            self._settle_recovery_executions()
            remaining = self._net_delta_usd()
            remaining_base = abs(sum(v.position for v in self.venues.values()))
            remaining_unpriced = (remaining is None and remaining_base
                                  > self.cfg.net_tolerance_base)
            still_unhedged = (remaining_unpriced
                              or (remaining is not None
                                  and remaining > threshold))
            if (self.cfg.kill_switch.enabled and still_unhedged
                    and (now - self._unhedged_since) * 1000.0
                    >= self.cfg.kill_switch.max_unhedged_duration_ms):
                self._risk_event(
                    "net_delta_duration", RiskAction.PAUSE_NEW_ENTRY,
                    "net delta remained above limit too long",
                    observed_value=(now - self._unhedged_since) * 1000.0,
                    threshold=self.cfg.kill_switch.max_unhedged_duration_ms,
                    persistent=True)
                if self.cfg.kill_switch.emergency_flatten_enabled:
                    self._risk_event(
                        "emergency_flatten", RiskAction.EMERGENCY_FLATTEN,
                        "persistent unhedged delta", observed_value=remaining,
                        threshold=threshold, persistent=True)
                    if not self._flatten_required:
                        await self._emergency_flatten()
        else:
            self._unhedged_since = None
            self._clear_transient_risk("net_delta_limit")

        loss_limit = self.cfg.kill_switch.max_session_loss_usd
        if self.cfg.kill_switch.enabled and loss_limit > 0:
            pnl = self.session_pnl()
            if pnl is not None and pnl <= -loss_limit:
                self._risk_event(
                    "session_loss", RiskAction.PAUSE_NEW_ENTRY,
                    "session MTM loss limit breached", observed_value=-pnl,
                    threshold=loss_limit, persistent=True)
                if self.cfg.kill_switch.emergency_flatten_enabled:
                    self._risk_event(
                        "session_loss_flatten", RiskAction.EMERGENCY_FLATTEN,
                        "flatten after session loss breach",
                        observed_value=-pnl, threshold=loss_limit,
                        persistent=True)
                    if not self._flatten_required:
                        await self._emergency_flatten()

    async def _emergency_flatten(self) -> bool:
        """Retryable reduce-only flatten of every known venue position."""
        if self._flatten_lock.locked():
            return False
        async with self._flatten_lock:
            return await self._emergency_flatten_locked()

    async def _emergency_flatten_locked(self) -> bool:
        self._flatten_required = True
        self._flatten_attempts += 1
        self._next_flatten_at = (time.time()
                                 + self.cfg.kill_switch.emergency_flatten_retry_sec)
        errors = []
        flatten_fills = []
        for venue in self.venues.values():
            position = venue.position
            if not math.isfinite(float(position)):
                errors.append(f"{venue.key}:invalid_position")
                self._risk_event(
                    "invalid_position_state", RiskAction.PAUSE_NEW_ENTRY,
                    f"{venue.name} position is not finite", persistent=True)
                continue
            if abs(position) < venue.min_base:
                continue
            lock = self._vlock(venue.key)
            if lock.locked() or venue.key in self._venue_down:
                errors.append(f"{venue.key}:unavailable")
                continue
            is_buy = position < 0
            ref = venue.book.best_ask() if is_buy else venue.book.best_bid()
            if ref is None or not math.isfinite(float(ref)) or ref <= 0:
                errors.append(f"{venue.key}:no_book")
                continue
            slip = self.cfg.hedge_slippage_bps / 1e4
            limit = venue.px_round(ref * (1 + slip), True) if is_buy else \
                venue.px_round(ref * (1 - slip), False)
            qty = floor_step(abs(position), self._step)
            if (qty < venue.min_base or not math.isfinite(qty)
                    or not math.isfinite(limit) or limit <= 0):
                errors.append(f"{venue.key}:invalid_order")
                continue
            minimum_quote = max(
                venue.min_quote,
                self.cfg.min_order_notional / self._quote_rate(venue))
            if qty * limit < minimum_quote:
                errors.append(f"{venue.key}:below_min_notional")
                continue
            await lock.acquire()
            try:
                self._record_send(venue)
                try:
                    raw = await self._send_bounded(
                        venue,
                        is_buy=is_buy, qty=qty, limit_px=limit,
                        reduce_only=True)
                except Exception as exc:
                    raw = exc
                info = self._normalize_order_result(
                    raw, qty, venue_key=venue.key,
                    pair_id=self.pair_position.pair_id)
                if not info.get("err") and not info.get("unresolved"):
                    fill = info.get("filled_base", 0.0)
                    venue.position += fill if is_buy else -fill
                    if fill:
                        px = info.get("avg_px")
                        if px is None:
                            venue.accounting_complete = False
                            self._risk_event(
                                "incomplete_fill_accounting",
                                RiskAction.PAUSE_NEW_ENTRY,
                                "emergency fill has no actual average price",
                                pair_id=self.pair_position.pair_id,
                                persistent=True)
                        else:
                            fee = venue.fee_bps / 1e4
                            venue.cash += (-fill * px * (1 + fee) if is_buy
                                           else fill * px * (1 - fee))
                            venue.volume_usd += self._notional_usd(
                                venue, fill, px)
                            flatten_fills.append((venue, is_buy, fill, px))
                else:
                    errors.append(f"{venue.key}:order_failed")
                    if info.get("unresolved"):
                        self._risk_event(
                            "unknown_risk_order_outcome",
                            RiskAction.PAUSE_NEW_ENTRY,
                            "emergency flatten order outcome is unknown",
                            pair_id=self.pair_position.pair_id,
                            persistent=True)
                        self._reconcile_evt.set()
            finally:
                lock.release()
        if self.ledger is not None and self.ledger.current is not None:
            buys = [item for item in flatten_fills if item[1]]
            sells = [item for item in flatten_fills if not item[1]]
            if buys and sells:
                buy_v, _, buy_fill, buy_px = buys[0]
                sell_v, _, sell_fill, sell_px = sells[0]
                matched = min(buy_fill, sell_fill,
                              self.ledger.current.remaining_base)
                if matched > 0:
                    try:
                        buy_quote, sell_quote = (
                            self.costs.directional_quote_rates(
                                buy_key=buy_v.key, sell_key=sell_v.key))
                    except KeyError:
                        buy_quote = sell_quote = 1.0
                    raw_ratio = sell_px / buy_px
                    basis_bps = self.costs.stablecoin_basis_cost_bps(
                        buy_key=buy_v.key, sell_key=sell_v.key,
                        raw_sell_buy_ratio=raw_ratio)
                    pair = self.ledger.current
                    self.ledger.record_fill(pair, {
                        "action": "EXIT", "qty": matched,
                        "buy_key": buy_v.key, "sell_key": sell_v.key,
                        "buy_px": buy_px, "sell_px": sell_px,
                        "planned_buy_px": buy_px,
                        "planned_sell_px": sell_px,
                        "buy_fee_rate": buy_v.fee_bps / 1e4,
                        "sell_fee_rate": sell_v.fee_bps / 1e4,
                        "buy_quote_usd": buy_quote,
                        "sell_quote_usd": sell_quote,
                        "spread_bps": self.premium_bps(),
                        "z_score": (self.spread_stats.z_score
                                    if self.spread_stats else None),
                        "midline_bps": self.active_midline_bps(),
                        "funding_cost_bps": 0.0,
                        "stablecoin_basis_bps": basis_bps,
                        "at": time.time(),
                    })
        self._sync_pair_position_from_venues()
        remaining = [f"{venue.key}:{venue.position:+.8g}"
                     for venue in self.venues.values()
                     if abs(venue.position) >= venue.min_base]
        complete = not remaining
        if complete:
            self._flatten_required = False
            if self.ledger is not None:
                self.ledger.append_event("EMERGENCY_FLATTEN_COMPLETE", {
                    "attempts": self._flatten_attempts})
        else:
            if self.ledger is not None:
                self.ledger.append_event("EMERGENCY_FLATTEN_RETRY", {
                    "attempt": self._flatten_attempts,
                    "remaining": remaining, "errors": errors})
            maximum = self.cfg.kill_switch.emergency_flatten_max_attempts
            if maximum and self._flatten_attempts >= maximum:
                self._risk_event(
                    "flatten_retry_exhausted", RiskAction.PAUSE_NEW_ENTRY,
                    "emergency flatten retry limit exhausted",
                    observed_value=float(self._flatten_attempts),
                    threshold=float(maximum), persistent=True)
        self._persist_runtime()
        return complete

    def _schedule_poke(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time() + max(delay, 0.01)
        if self._poke_due is not None and self._poke_due <= due + 0.02:
            return

        def _fire() -> None:
            self._poke_due = None
            self._update_evt.set()

        self._poke_due = due
        loop.call_at(due, _fire)

    def _skiplog(self, fmt: str, *args) -> None:
        now = time.time()
        if now - self._last_skiplog >= 2.0:
            self._last_skiplog = now
            log.info(fmt, *args)

    async def _evaluate(self) -> None:
        cfg = self.cfg
        if self.halted and not self.pair_position.is_open:
            return
        now = time.time()
        best = self._scan(now)
        if best is None:
            return
        buy, sell, plan = best
        signal_ts = time.time()
        self._record_market_latency(buy, sell, signal_ts)
        # _scan verified both locks free and nothing ran since (no awaits),
        # so these acquires take the no-suspension fast path
        await self._vlock(buy.key).acquire()
        await self._vlock(sell.key).acquire()
        # run as a task so a shutdown cancels the strategy loop's await, never
        # the in-flight execution itself (both legs must settle)
        t = asyncio.create_task(
            self._execute_locked(buy, sell, plan, signal_ts=signal_ts))
        self._exec_tasks.add(t)
        t.add_done_callback(self._exec_tasks.discard)
        await asyncio.shield(t)

    async def _execute_locked(self, buy, sell, plan: ArbPlan, *,
                              signal_ts: Optional[float] = None) -> None:
        """Run one execution while holding both venue locks (acquired by the
        caller), then release them and settle the aftermath: unresolved
        outcomes escalate to reconcile, everything else gets a net-delta
        check."""
        unresolved = False
        try:
            unresolved = await self._execute(
                buy, sell, plan, signal_ts=signal_ts)
            if unresolved:
                self._risk_event(
                    "unresolved_order_outcome", RiskAction.PAUSE_NEW_ENTRY,
                    "one or both order outcomes are unknown; reconcile required",
                    pair_id=plan.pair_id, persistent=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            unresolved = True
            log.exception("execute failed")
            self._risk_event(
                "execution_exception_unknown", RiskAction.PAUSE_NEW_ENTRY,
                "execution raised after dispatch; order outcome may be unknown "
                f"({type(exc).__name__})",
                pair_id=plan.pair_id, persistent=True)
        finally:
            self._vlock(buy.key).release()
            self._vlock(sell.key).release()
        if unresolved:
            self._reconcile_evt.set()
        else:
            await self._maybe_hedge(pair_id=plan.pair_id)
            self._sync_pair_position_from_venues()
            self._settle_recovery_executions()
        self._update_evt.set()  # freed venues may have a queued opportunity

    def _scan(self, now: float):
        """Evaluate both directions; returns the best executable
        (buy, sell, plan), or None."""
        cfg = self.cfg
        self._update_spread_state(now)
        market_reasons = ("disconnected", "not_ready", "empty_book",
                          "connection_stale", "book_stale", "exchange_stale",
                          "crossed_book")
        for venue in self.venues.values():
            if self._book_quality(venue, now).ok:
                for reason in market_reasons:
                    self._clear_transient_risk(
                        f"market_data_{venue.key}_{reason}")
        pause_reason = self.strategy_pause_reason()
        if (pause_reason in ("dynamic_warmup", "regime_warmup")
                and not self.pair_position.is_open):
            self._armed["sell_entropy"] = None
            self._armed["buy_entropy"] = None
            self._skiplog("new entries paused: %s", pause_reason)
            return None
        best = None
        for buy, sell, dkey in ((self.hedge, self.entropy, "sell_entropy"),
                                (self.entropy, self.hedge, "buy_entropy")):
            action = self._signal_action(dkey)
            signal_context = None
            if cfg.midline.mode == "dynamic":
                signal_context = self._dynamic_signal_context(buy, sell, dkey)
                if signal_context is None:
                    self._armed[dkey] = None
                    continue
                action, required_edge, max_base = signal_context
            else:
                required_edge = None
                max_base = (self._closeable_pair_base()
                            if action is SignalAction.EXIT else None)
                if action is SignalAction.EXIT and max_base < self._min_base:
                    self._armed[dkey] = None
                    continue
            if pause_reason is not None and action is not SignalAction.EXIT:
                self._armed[dkey] = None
                continue
            if (action is not SignalAction.EXIT
                    and now - self.last_trade_ts < cfg.cooldown_sec):
                self._armed[dkey] = None
                self._schedule_poke(
                    cfg.cooldown_sec - (now - self.last_trade_ts))
                continue
            bq, sq = self._book_quality(buy, now), self._book_quality(sell, now)
            if not (bq.ok and sq.ok):
                self._armed[dkey] = None
                blocked = buy if not bq.ok else sell
                quality = bq if not bq.ok else sq
                self._skiplog("%s blocked: %s market data %s",
                              dkey, blocked.name, quality.reason)
                trigger = f"market_data_{blocked.key}_{quality.reason}"
                self._risk_event(
                    trigger, RiskAction.PAUSE_NEW_ENTRY,
                    f"{blocked.name} market data {quality.reason}")
                continue
            for reason in market_reasons:
                self._clear_transient_risk(f"market_data_{buy.key}_{reason}")
                self._clear_transient_risk(f"market_data_{sell.key}_{reason}")
            if not (buy.ready_to_trade() and sell.ready_to_trade()):
                continue
            if self._venue_down:
                continue  # a venue in outage pauses the (only) pair
            if self._vlock(buy.key).locked() or self._vlock(sell.key).locked():
                continue  # mid-execution or mid-reconcile
            if (action is not SignalAction.EXIT
                    and (self._venue_limited(buy)
                         or self._venue_limited(sell))):
                continue  # reactive 429 exclusion
            if (action is not SignalAction.EXIT
                    and not (self._venue_rate_ok(buy)
                             and self._venue_rate_ok(sell))):
                self._skiplog("%s deferred: venue order budget exhausted", dkey)
                continue
            # never refire into books that predate the venue's own last trade
            if (buy.book.last_update_ts <= buy.last_traded_ts
                    or sell.book.last_update_ts <= sell.last_traded_ts):
                continue
            plan_cap = (cfg.vwap_sizing.max_order_usd
                        if cfg.vwap_sizing.enabled
                        else cfg.max_order_notional)
            plan, reason = self._plan(
                buy, sell, plan_cap, required_edge_bps=required_edge,
                max_base=max_base, signal_action=action)
            edge_present = (plan is not None if cfg.vwap_sizing.enabled
                            else reason not in ("no_edge", "empty_book"))
            if not edge_present:
                self._armed[dkey] = None
                continue
            armed = self._armed.get(dkey)
            if action is SignalAction.EXIT:
                self._armed[dkey] = None
            elif armed is None:
                # premium persistence: only fire if the edge survives
                # premium_persist_sec (filters one-tick phantoms)
                self._armed[dkey] = now
                self._schedule_poke(cfg.premium_persist_sec)
                continue
            elif now - armed < cfg.premium_persist_sec:
                self._schedule_poke(cfg.premium_persist_sec - (now - armed))
                continue
            if plan is None:
                continue
            if action is not SignalAction.EXIT:
                headroom = self._headroom(
                    buy, sell, plan.buy_vwap or plan.buy_limit,
                    plan.sell_vwap or plan.sell_limit)
                if headroom <= 0:
                    self._skiplog("%s blocked by position caps (no headroom)",
                                  dkey)
                    continue
                if headroom < max(plan.buy_notional, plan.sell_notional):
                    plan, _ = self._plan(
                        buy, sell, min(plan_cap, headroom),
                        required_edge_bps=required_edge, max_base=max_base,
                        signal_action=action)
                    if plan is None:
                        self._skiplog(
                            "%s blocked by position caps (headroom $%.0f)",
                            dkey, max(headroom, 0.0))
                        continue
            if best is None or plan.exp_edge_usd > best[2].exp_edge_usd:
                best = (buy, sell, plan)
        return best

    # ------------------------------------------------------------- execution

    def _transition_execution(self, execution: PairExecution,
                              state: ExecutionState, reason: str, **data) -> None:
        event = execution.transition(state, reason, data=data)
        log.info("[PAIR %s] %s -> %s: %s", execution.pair_id,
                 event.from_state.value if event.from_state else "—",
                 event.to_state.value, reason)
        if self.ledger is not None:
            try:
                self.ledger.append_event("EXECUTION_STATE", {
                    "pair_id": execution.pair_id,
                    "from_state": (event.from_state.value
                                   if event.from_state else None),
                    "to_state": event.to_state.value,
                    "reason": reason, "data": data,
                })
            except Exception as exc:
                self._mark_persistence_failure(exc)
        self._persist_runtime()

    def _new_pair_execution(self, plan: ArbPlan, direction: str) -> PairExecution:
        execution = PairExecution.new(
            pair_id=plan.pair_id or make_pair_id(), symbol=self.cfg.symbol,
            venue_a=self.entropy.name, venue_b=self.hedge.name,
            direction=PairDirection(direction))
        self.execution_history.append(execution)
        self._transition_execution(
            execution, ExecutionState.SIGNAL_CONFIRMED,
            f"{plan.signal_action} signal confirmed",
            action=plan.signal_action, z_score=plan.signal_z_score,
            expected_net_edge_bps=plan.expected_net_edge_bps)
        return execution

    def _settle_recovery_executions(self) -> None:
        net = sum(v.position for v in self.venues.values())
        if abs(net) > self.cfg.net_tolerance_base:
            return
        pending = list(self._recovery_executions)
        self._recovery_executions.clear()
        for execution in pending:
            if execution.state is ExecutionState.RECOVERY:
                self._transition_execution(
                    execution, ExecutionState.HEDGED,
                    "net delta restored within tolerance", net_delta_base=net)
                self._transition_execution(
                    execution, ExecutionState.COMPLETE,
                    "recovery completed")

    async def _recover_known_leg(self, venue, *, original_is_buy: bool,
                                 qty: float, plan: ArbPlan):
        """Undo a confirmed OPEN/ADD leg while its counterpart is unknown."""
        if qty <= 0 or SignalAction(plan.signal_action) is SignalAction.EXIT:
            return None
        is_buy = not original_is_buy
        ref = venue.book.best_ask() if is_buy else venue.book.best_bid()
        if ref is None:
            return None
        slip = self.cfg.hedge_slippage_bps / 1e4
        limit = venue.px_round(ref * (1 + slip), True) if is_buy else \
            venue.px_round(ref * (1 - slip), False)
        qty = floor_step(qty, self._step)
        minimum_quote = max(
            venue.min_quote,
            self.cfg.min_order_notional / self._quote_rate(venue))
        if qty < venue.min_base or qty * limit < minimum_quote:
            return None
        log.error("[RECOVERY] pair=%s undo known %s leg %.6g on %s",
                  plan.pair_id, "BUY" if original_is_buy else "SELL",
                  qty, venue.name)
        self._record_send(venue)
        try:
            result = await self._send_bounded(
                venue,
                is_buy=is_buy, qty=qty, limit_px=limit, reduce_only=True)
        except Exception as exc:
            result = exc
        return {"venue": venue, "original_is_buy": original_is_buy,
                "limit_px": limit, "requested_qty": qty, "result": result}

    async def _execute(self, buy, sell, plan: ArbPlan, *,
                       signal_ts: Optional[float] = None) -> bool:
        """Send both legs and settle the fills. Both venue locks are held by
        the caller. Returns True when an outcome is unresolved and the caller
        must escalate to reconcile."""
        if self.record_only:
            log.critical("record-only execution attempt blocked before dispatch")
            return False
        try:
            action = SignalAction(plan.signal_action)
        except ValueError:
            self._risk_event(
                "invalid_order_plan", RiskAction.PAUSE_NEW_ENTRY,
                "order plan has an invalid signal action", persistent=True)
            return False
        numeric_plan = (plan.qty, plan.buy_limit, plan.sell_limit,
                        plan.buy_notional, plan.sell_notional)
        if (not all(math.isfinite(float(value)) for value in numeric_plan)
                or plan.qty <= 0 or plan.buy_limit <= 0
                or plan.sell_limit <= 0):
            self._risk_event(
                "invalid_order_plan", RiskAction.PAUSE_NEW_ENTRY,
                "order plan contains non-finite or non-positive values",
                pair_id=plan.pair_id, persistent=True)
            return False
        if self.halted and action is not SignalAction.EXIT:
            return False
        if not (self._book_quality(buy).ok and self._book_quality(sell).ok
                and buy.ready_to_trade() and sell.ready_to_trade()):
            return False
        if action is not SignalAction.EXIT and self.strategy_pause_reason():
            return False
        if (action is SignalAction.EXIT
                and plan.qty > self._closeable_pair_base() + self._step / 2):
            self._risk_event(
                "exit_quantity_changed", RiskAction.PAUSE_NEW_ENTRY,
                "closeable venue quantity fell below the planned EXIT",
                pair_id=plan.pair_id, persistent=True)
            return False
        cfg = self.cfg
        direction = "sell_entropy" if sell.key == "entropy" else "buy_entropy"
        if action is not SignalAction.EXIT:
            # Positions/cost inputs may change after signal generation. Recheck
            # both the action and the current-book executable slice while the
            # two venue locks are held, before either order is dispatched.
            if self._signal_action(direction) is not action:
                return False
            headroom = self._headroom(
                buy, sell, plan.buy_vwap or plan.buy_limit,
                plan.sell_vwap or plan.sell_limit)
            if headroom <= 0:
                return False
            plan_cap = (cfg.vwap_sizing.max_order_usd
                        if cfg.vwap_sizing.enabled
                        else cfg.max_order_notional)
            refreshed_required = None
            refreshed_max_base = plan.qty
            if cfg.midline.mode == "dynamic":
                context = self._dynamic_signal_context(
                    buy, sell, direction)
                if context is None or context[0] is not action:
                    return False
                _, refreshed_required, context_max_base = context
                if context_max_base is not None:
                    refreshed_max_base = min(
                        refreshed_max_base, context_max_base)
            refreshed, _ = self._plan(
                buy, sell, min(plan_cap, headroom),
                required_edge_bps=refreshed_required,
                max_base=refreshed_max_base, signal_action=action)
            if refreshed is None or refreshed.qty + self._step / 2 < plan.qty:
                return False
        inv_bps = self._inv_add_bps(buy, sell)
        execution = self._new_pair_execution(plan, direction)
        # Ledger fsync is intentionally before order dispatch. If it stalls,
        # wall-clock freshness and cost/risk state may have expired even
        # though no coroutine could update the in-memory book meanwhile.
        if (not self._book_quality(buy).ok
                or not self._book_quality(sell).ok
                or (action is not SignalAction.EXIT
                    and self.strategy_pause_reason())):
            self._transition_execution(
                execution, ExecutionState.FAILED,
                "final pre-dispatch freshness/risk gate failed")
            return False
        self.last_trade_ts = time.time()
        log.info("[ARB] %s %s pair=%s: BUY %s %.6g @<=%.6g | "
                 "SELL %s @>=%.6g | "
                 "take $%.0f of $%.0f | %s edge %.2fbps req %.2fbps | "
                 "VWAP %.6g/%.6g | exp $%.4f",
                 direction, plan.signal_action, plan.pair_id or "—",
                 buy.name, plan.qty, plan.buy_limit, sell.name,
                 plan.sell_limit, plan.buy_notional, plan.q_max_notional,
                 plan.sizing_mode, plan.expected_net_edge_bps,
                 plan.required_net_edge_bps, plan.buy_vwap, plan.sell_vwap,
                 plan.exp_edge_usd)
        slip = cfg.leg_slippage_bps / 1e4
        buy_bound = buy.px_round(plan.buy_limit * (1 + slip), round_up=False)
        sell_bound = sell.px_round(plan.sell_limit * (1 - slip), round_up=True)
        dispatch_ts = time.time()
        if signal_ts is not None:
            self.latency.record(
                "signal_to_send_ms", (dispatch_ts - signal_ts) * 1000.0)
        self._record_send(buy)
        self._record_send(sell)
        self._transition_execution(
            execution, ExecutionState.ORDERS_SENT,
            "both taker legs dispatched", qty=plan.qty)
        reduce_only = action is SignalAction.EXIT
        tasks = [
            asyncio.create_task(buy.send_taker(
                is_buy=True, qty=plan.qty, limit_px=buy_bound,
                reduce_only=reduce_only)),
            asyncio.create_task(sell.send_taker(
                is_buy=False, qty=plan.qty, limit_px=sell_bound,
                reduce_only=reduce_only)),
        ]
        recovery = None
        timed_out = False
        if cfg.execution_risk.enabled:
            done, pending = await asyncio.wait(
                tasks, timeout=min(
                    cfg.execution_risk.hedge_timeout_ms / 1000.0,
                    cfg.settle_timeout_sec))
            timed_out = bool(pending)
            if timed_out:
                self._transition_execution(
                    execution, ExecutionState.RECOVERY,
                    "hedge timeout before both legs settled",
                    timeout_ms=cfg.execution_risk.hedge_timeout_ms,
                    completed_legs=len(done))
                self._recovery_executions.append(execution)
                if len(done) == 1:
                    finished = next(iter(done))
                    index = tasks.index(finished)
                    try:
                        raw_known = finished.result()
                    except Exception as exc:
                        raw_known = exc
                    known_venue = buy if index == 0 else sell
                    known = self._normalize_order_result(
                        raw_known, plan.qty, venue_key=known_venue.key,
                        pair_id=plan.pair_id)
                    if (not known.get("unresolved")
                            and known.get("filled_base", 0) > 0):
                        recovery = await self._recover_known_leg(
                            known_venue,
                            original_is_buy=index == 0,
                            qty=known["filled_base"], plan=plan)
        # Adapter implementations are expected to bound their own settlement
        # waits, but the engine must not trust that contract with real money.
        # A coroutine that never returns after dispatch is an UNKNOWN order,
        # never evidence of a rejection or zero fill. Bound the pair-level
        # wait, cancel only the local waiter, persist the ambiguity below, and
        # let reconciliation establish the exchange-side result.
        remaining = max(
            cfg.settle_timeout_sec - (time.time() - dispatch_ts), 0.0)
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        if pending:
            timed_out = True
            for task in pending:
                task.cancel()
                task.add_done_callback(self._consume_background_order)
        res = []
        for task in tasks:
            if task in pending:
                res.append({
                    "status": "engine-timeout",
                    "filled_base": 0.0,
                    "avg_px": None,
                    "err": "order response unresolved at engine timeout",
                    "unresolved": True,
                    "accounting_complete": False,
                })
                continue
            try:
                res.append(task.result())
            except asyncio.CancelledError:
                res.append({
                    "status": "engine-cancelled",
                    "filled_base": 0.0,
                    "avg_px": None,
                    "err": "order response cancelled before resolution",
                    "unresolved": True,
                    "accounting_complete": False,
                })
            except Exception as exc:
                res.append(exc)
        settled_ts = time.time()
        self.latency.record(
            "orders_to_settle_ms", (settled_ts - dispatch_ts) * 1000.0)
        binfo = self._normalize_order_result(
            res[0], plan.qty, venue_key=buy.key, pair_id=plan.pair_id)
        sinfo = self._normalize_order_result(
            res[1], plan.qty, venue_key=sell.key, pair_id=plan.pair_id)
        for info in (binfo, sinfo):
            send_ts, ack_ts = info.get("order_send_ts"), info.get("order_ack_ts")
            first_fill_ts = info.get("first_fill_ts")
            if send_ts is not None and ack_ts is not None:
                self.latency.record(
                    "send_to_ack_ms", (ack_ts - send_ts) * 1000.0)
            if ack_ts is not None and first_fill_ts is not None:
                self.latency.record(
                    "ack_to_fill_ms", (first_fill_ts - ack_ts) * 1000.0)
        bfinal, sfinal = binfo.get("final_fill_ts"), sinfo.get("final_fill_ts")
        if bfinal is not None and sfinal is not None:
            self.latency.record(
                "leg_fill_gap_ms", abs(bfinal - sfinal) * 1000.0)
        for v, info, side in ((buy, binfo, "buy"), (sell, sinfo, "sell")):
            if info.get("err"):
                log.error("[%s] %s leg: %s", v.name, side, info["err"])
        bfill = binfo["filled_base"]
        sfill = sinfo["filled_base"]
        missing_fill_price = (
            (bfill > 0 and (not binfo.get("avg_px")
                            or not binfo.get("accounting_complete", True)))
            or (sfill > 0 and (not sinfo.get("avg_px")
                               or not sinfo.get("accounting_complete", True))))
        if missing_fill_price:
            self._risk_event(
                "incomplete_fill_accounting", RiskAction.PAUSE_NEW_ENTRY,
                "filled quantity is known but actual average price is missing",
                pair_id=plan.pair_id, persistent=True)
        bpx = binfo.get("avg_px")
        spx = sinfo.get("avg_px")
        buy.position += bfill
        sell.position -= sfill
        if bfill:
            if bpx is None:
                buy.accounting_complete = False
            else:
                buy.cash -= bfill * bpx * (1 + plan.buy_fee)
                buy.volume_usd += self._notional_usd(buy, bfill, bpx)
        if sfill:
            if spx is None:
                sell.accounting_complete = False
            else:
                sell.cash += sfill * spx * (1 - plan.sell_fee)
                sell.volume_usd += self._notional_usd(sell, sfill, spx)

        recovered_buy = recovered_sell = 0.0
        recovery_accounting = None
        recovery_unresolved = False
        recovery_hard_err = False
        if recovery is not None:
            venue = recovery["venue"]
            info = self._normalize_order_result(
                recovery["result"], recovery["requested_qty"],
                venue_key=venue.key, pair_id=plan.pair_id)
            recovery_unresolved = bool(info.get("unresolved"))
            recovery_hard_err = info.get("err") is not None
            recovered = (0.0 if recovery_unresolved
                         else info.get("filled_base", 0.0))
            if recovered:
                px = info.get("avg_px")
                if recovery["original_is_buy"]:
                    recovered_buy = recovered
                    venue.position -= recovered
                    original_px = bpx
                else:
                    recovered_sell = recovered
                    venue.position += recovered
                    original_px = spx
                if px is None or original_px is None:
                    venue.accounting_complete = False
                    self._risk_event(
                        "incomplete_fill_accounting",
                        RiskAction.PAUSE_NEW_ENTRY,
                        "recovery fill has no actual average price",
                        pair_id=plan.pair_id, persistent=True)
                else:
                    fee = venue.fee_bps / 1e4
                    if recovery["original_is_buy"]:
                        venue.cash += recovered * px * (1 - fee)
                    else:
                        venue.cash -= recovered * px * (1 + fee)
                    recovery_accounting = (
                        venue, recovery["original_is_buy"], original_px,
                        px, recovered)
                    venue.volume_usd += self._notional_usd(
                        venue, recovered, px)

        effective_buy = max(bfill - recovered_buy, 0.0)
        effective_sell = max(sfill - recovered_sell, 0.0)
        matched = min(effective_buy, effective_sell)
        pair_direction = (PairDirection.SELL_ENTROPY
                          if direction == "sell_entropy"
                          else PairDirection.BUY_ENTROPY)
        action = SignalAction(plan.signal_action)
        ledger_direction = (self.pair_position.direction
                            if action is SignalAction.EXIT
                            and self.pair_position.direction is not None
                            else pair_direction)
        if matched > 0:
            self._record_pair_fill(
                ledger_direction, action, buy, sell, plan, matched, bpx, spx,
                accounting_complete=not missing_fill_price)
            if action is SignalAction.EXIT:
                self.pair_position.reduce(matched)
            else:
                self.pair_position.add(
                    pair_direction, matched, pair_id=plan.pair_id)
        if recovery_accounting is not None:
            self._record_recovery_roundtrip(
                plan.pair_id, pair_direction, *recovery_accounting,
                complete_if_flat=(action is SignalAction.OPEN and matched <= 0
                                  and abs(effective_buy - effective_sell)
                                  <= self._step / 2),
                signal_spread_bps=plan.signal_spread_bps,
                signal_z_score=plan.signal_z_score,
                signal_midline_bps=plan.signal_midline_bps)
        imbalance_qty = effective_buy - effective_sell
        if plan.pair_id and abs(imbalance_qty) > self._step / 2:
            self._unmatched_legs[plan.pair_id] = {
                "venue_key": buy.key if imbalance_qty > 0 else sell.key,
                "original_is_buy": imbalance_qty > 0,
                "original_px": bpx if imbalance_qty > 0 else spx,
                "remaining_qty": abs(imbalance_qty),
                "pair_direction": ledger_direction.value,
                "signal_spread_bps": plan.signal_spread_bps,
                "signal_z_score": plan.signal_z_score,
                "signal_midline_bps": plan.signal_midline_bps,
            }
            self._persist_runtime()
        fill_edge = 0.0
        if matched > 0 and binfo.get("avg_px") and sinfo.get("avg_px"):
            fill_edge = matched * (
                sinfo["avg_px"] * self._quote_rate(sell)
                * (1 - plan.sell_fee)
                - binfo["avg_px"] * self._quote_rate(buy)
                * (1 + plan.buy_fee))
            self.total_fill_edge += fill_edge
        log.info("[SETTLED] %s: buy %s %s %.6g/%.6g | sell %s %s %.6g/%.6g | "
                 "matched %.6g | fill edge $%.4f", direction,
                 buy.name, binfo["status"], bfill, plan.qty,
                 sell.name, sinfo["status"], sfill, plan.qty, matched, fill_edge)
        buy.last_traded_ts = sell.last_traded_ts = time.time()

        unresolved = (binfo.get("unresolved") or sinfo.get("unresolved")
                      or recovery_unresolved)
        hard_err = (binfo.get("err") is not None
                    or sinfo.get("err") is not None or recovery_hard_err)
        imbalance = abs(effective_buy - effective_sell)
        partial = (bfill < plan.qty - self._step / 2
                   or sfill < plan.qty - self._step / 2)
        self.consecutive_partial_fills = (
            self.consecutive_partial_fills + 1 if partial else 0)
        if (self.cfg.kill_switch.enabled
                and self.consecutive_partial_fills
                >= self.cfg.kill_switch.max_consecutive_partial_fills):
            self._risk_event(
                "consecutive_partial_fills", RiskAction.PAUSE_NEW_ENTRY,
                "too many consecutive unequal fills",
                observed_value=float(self.consecutive_partial_fills),
                threshold=float(
                    self.cfg.kill_switch.max_consecutive_partial_fills),
                pair_id=execution.pair_id, persistent=True)
        if execution.state is ExecutionState.ORDERS_SENT:
            if unresolved:
                self._transition_execution(
                    execution, ExecutionState.RECOVERY,
                    "one or both order outcomes unresolved")
                self._recovery_executions.append(execution)
            elif hard_err and effective_buy <= 0 and effective_sell <= 0:
                self._transition_execution(
                    execution, ExecutionState.FAILED,
                    "both-leg execution failed without fills")
            elif (partial or imbalance > self._step / 2
                  or not (effective_buy and effective_sell)):
                self._transition_execution(
                    execution, ExecutionState.PARTIAL,
                    "partial, unequal, or one-leg fill", buy_fill=effective_buy,
                    sell_fill=effective_sell)
                self._transition_execution(
                    execution, ExecutionState.RECOVERY,
                    "delta recovery required")
                self._recovery_executions.append(execution)
            else:
                self._transition_execution(
                    execution, ExecutionState.BOTH_FILLED,
                    "both legs filled with matched delta", matched=matched)
                self._transition_execution(
                    execution, ExecutionState.COMPLETE,
                    "pair execution delta neutral")
        elif (execution.state is ExecutionState.RECOVERY
              and not timed_out and execution not in self._recovery_executions):
            self._recovery_executions.append(execution)
        rate_limited = False
        for v, info in ((buy, binfo), (sell, sinfo)):
            if str(info.get("err", "")).startswith("RATE_LIMITED"):
                rate_limited = True
                self._mark_limited(v)
            elif "margin" in str(info.get("status", "")).lower():
                log.warning("[%s] margin rejection — collateral exhausted, "
                            "pausing venue", v.name)
                self._mark_limited(v)
        sent_ok = not hard_err and not unresolved
        if sent_ok:
            self.consec_errors = 0
        elif not rate_limited:
            self.consec_errors += 1
            if self.consec_errors >= cfg.max_consecutive_errors:
                if cfg.kill_switch.enabled:
                    self._risk_event(
                        "consecutive_execution_failures",
                        RiskAction.PAUSE_NEW_ENTRY,
                        "too many consecutive execution failures",
                        observed_value=float(self.consec_errors),
                        threshold=float(cfg.max_consecutive_errors),
                        pair_id=execution.pair_id, persistent=True)
                else:
                    self.halted = True
                    log.critical("HALTED after %d consecutive execution problems "
                                 "— flatten manually and restart / 连续执行异常，"
                                 "引擎已停止，请手动平仓后重启", self.consec_errors)
        if sent_ok:
            self.trades += 1
            self.total_exp_edge += plan.exp_edge_usd
        self._record_trade(direction, plan,
                           None if unresolved else fill_edge,
                           f"{binfo['status']}/{sinfo['status']}", sent_ok)
        self._log_csv(direction, buy, sell, plan, sent_ok, bfill, sfill,
                      binfo["status"], sinfo["status"], fill_edge, inv_bps)
        self.last_trade_ts = time.time()
        return bool(unresolved)

    def _record_recovery_roundtrip(
            self, pair_id: str, pair_direction: PairDirection, venue,
            original_is_buy: bool, original_px: Optional[float],
            recovery_px: Optional[float],
            qty: float, complete_if_flat: bool = False, *,
            signal_spread_bps: Optional[float] = None,
            signal_z_score: Optional[float] = None,
            signal_midline_bps: Optional[float] = None) -> None:
        if (self.ledger is None or not pair_id or qty <= 0
                or original_px is None or recovery_px is None):
            return
        pair = self.ledger.ensure_pair(
            pair_id=pair_id, symbol=self.cfg.symbol,
            venue_a=self.entropy.name, venue_b=self.hedge.name,
            direction=(self.ledger.current.direction
                       if self.ledger.current else pair_direction.value))
        if pair.entry_spread is None:
            pair.entry_spread = signal_spread_bps
            pair.entry_z = signal_z_score
            pair.entry_midline = signal_midline_bps
        try:
            quote_usd = self.costs.quote_rate(venue.key)
        except KeyError:
            quote_usd = 1.0
        gross = ((recovery_px - original_px) if original_is_buy
                 else (original_px - recovery_px)) * qty * quote_usd
        fees = ((original_px + recovery_px) * qty * quote_usd
                * venue.fee_bps / 1e4)
        self.ledger.record_recovery(
            pair, gross_cashflow_usd=gross, fees_usd=fees,
            reason=f"one-leg roundtrip on {venue.name}",
            complete_if_flat=complete_if_flat)
        self._persist_runtime()

    def _record_pair_fill(self, pair_direction: PairDirection,
                          action: SignalAction, buy, sell, plan: ArbPlan,
                          matched: float, buy_px: Optional[float],
                          sell_px: Optional[float], *,
                          accounting_complete: bool = True) -> None:
        if self.ledger is None or not plan.pair_id:
            return
        pair = self.ledger.ensure_pair(
            pair_id=plan.pair_id, symbol=self.cfg.symbol,
            venue_a=self.entropy.name, venue_b=self.hedge.name,
            direction=pair_direction.value)
        if not accounting_complete:
            pair.accounting_complete = False
        try:
            buy_quote_usd, sell_quote_usd = (
                self.costs.directional_quote_rates(
                    buy_key=buy.key, sell_key=sell.key))
        except KeyError:
            buy_quote_usd = sell_quote_usd = 1.0
        common = {
            "action": action.value, "qty": matched,
            "spread_bps": plan.signal_spread_bps,
            "z_score": plan.signal_z_score,
            "midline_bps": plan.signal_midline_bps,
            "market_session": self.market_session.session.value,
            "at": time.time(),
        }
        if not accounting_complete or buy_px is None or sell_px is None:
            self.ledger.record_unpriced_fill(pair, common)
            self.ledger.append_event("PAIR_ACCOUNTING_INCOMPLETE", {
                "pair_id": pair.pair_id,
                "reason": "actual fill average price unavailable"})
            self._persist_runtime()
            return
        fill = {
            **common,
            "buy_key": buy.key, "sell_key": sell.key,
            "buy_px": buy_px, "sell_px": sell_px,
            "planned_buy_px": plan.buy_vwap or plan.buy_limit,
            "planned_sell_px": plan.sell_vwap or plan.sell_limit,
            "buy_fee_rate": plan.buy_fee, "sell_fee_rate": plan.sell_fee,
            "buy_quote_usd": buy_quote_usd,
            "sell_quote_usd": sell_quote_usd,
            "funding_cost_bps": plan.funding_cost_bps,
            "stablecoin_basis_bps": plan.stablecoin_basis_bps,
        }
        self.ledger.record_fill(pair, fill)
        self._persist_runtime()
        if pair.complete and self.cfg.funding.enabled:
            task = asyncio.create_task(self._refresh_pair_funding(pair))
            self._exec_tasks.add(task)
            task.add_done_callback(self._exec_tasks.discard)

    def _record_trade(self, direction: str, plan: ArbPlan, fill_edge,
                      status: str, ok: bool) -> None:
        self.recent_trades.append({
            "ts": time.time(), "direction": direction, "qty": plan.qty,
            "notional": plan.buy_notional,
            "prem_bps": plan.expected_net_edge_bps,
            "gross_bps": plan.gross_vwap_edge_bps,
            "required_bps": plan.required_net_edge_bps,
            "sizing_mode": plan.sizing_mode,
            "buy_vwap": plan.buy_vwap,
            "sell_vwap": plan.sell_vwap,
            "midline_bps": plan.signal_midline_bps,
            "z_score": plan.signal_z_score,
            "regime_state": plan.regime_state,
            "pair_id": plan.pair_id,
            "signal_action": plan.signal_action,
            "market_session": self.market_session.session.value,
            "exp": plan.exp_edge_usd, "fill": fill_edge, "status": status,
            "ok": ok})

    async def _maybe_hedge(self, pair_id: Optional[str] = None) -> None:
        net = sum(v.position for v in self.venues.values())
        if abs(net) > self.cfg.net_tolerance_base:
            if pair_id is None and len(self._unmatched_legs) == 1:
                pair_id = next(iter(self._unmatched_legs))
            await self._hedge(net, pair_id=pair_id)

    def _sync_pair_position_from_venues(self) -> None:
        """Conservatively recover matched pair exposure after startup sync."""
        e, h = self.entropy.position, self.hedge.position
        tolerance = max(self._min_base, self.cfg.net_tolerance_base)
        if e < -tolerance and h > tolerance:
            pair_id = (self.pair_position.pair_id
                       if self.pair_position.direction
                       is PairDirection.SELL_ENTROPY else None)
            self.pair_position.sync(
                PairDirection.SELL_ENTROPY, min(-e, h),
                pair_id=pair_id or make_pair_id(token="RECOVERED"))
        elif e > tolerance and h < -tolerance:
            pair_id = (self.pair_position.pair_id
                       if self.pair_position.direction
                       is PairDirection.BUY_ENTROPY else None)
            self.pair_position.sync(
                PairDirection.BUY_ENTROPY, min(e, -h),
                pair_id=pair_id or make_pair_id(token="RECOVERED"))
        else:
            self.pair_position.sync(None, 0.0)
        if self.ledger is not None:
            if self.pair_position.is_open and self.ledger.current is None:
                recovered = self.ledger.ensure_pair(
                    pair_id=self.pair_position.pair_id,
                    symbol=self.cfg.symbol, venue_a=self.entropy.name,
                    venue_b=self.hedge.name,
                    direction=self.pair_position.direction.value,
                    entry_time=self.pair_position.opened_at)
                recovered.entry_base = self.pair_position.base_qty
                recovered.remaining_base = self.pair_position.base_qty
                recovered.entry_spread = self.premium_bps()
                recovered.accounting_complete = False
                self.ledger.append_event("PAIR_RECOVERED", recovered.to_dict())
                self._risk_event(
                    "ledger_position_recovered",
                    RiskAction.PAUSE_NEW_ENTRY,
                    "venue positions exist without a complete local Pair ledger",
                    pair_id=recovered.pair_id, persistent=True)
            elif self.ledger.current is not None:
                current = self.ledger.current
                if not self.pair_position.is_open:
                    self.ledger.reconcile_current(
                        0.0, "reconciled venues are flat")
                    self._risk_event(
                        "ledger_position_mismatch", RiskAction.PAUSE_NEW_ENTRY,
                        "persisted Pair does not match reconciled positions",
                        pair_id=current.pair_id, persistent=True)
                elif current.direction != self.pair_position.direction.value:
                    self.ledger.reconcile_current(
                        0.0, "reconciled position direction changed")
                    self._risk_event(
                        "ledger_position_mismatch", RiskAction.PAUSE_NEW_ENTRY,
                        "persisted Pair direction differs from reconciled positions",
                        pair_id=current.pair_id, persistent=True)
                    recovered = self.ledger.ensure_pair(
                        pair_id=self.pair_position.pair_id,
                        symbol=self.cfg.symbol, venue_a=self.entropy.name,
                        venue_b=self.hedge.name,
                        direction=self.pair_position.direction.value,
                        entry_time=self.pair_position.opened_at)
                    recovered.entry_base = self.pair_position.base_qty
                    recovered.remaining_base = self.pair_position.base_qty
                    recovered.entry_spread = self.premium_bps()
                    recovered.accounting_complete = False
                elif abs(current.remaining_base
                         - self.pair_position.base_qty) > tolerance:
                    self.ledger.reconcile_current(
                        self.pair_position.base_qty,
                        "chain/local matched quantity reconciliation")
                    self._risk_event(
                        "ledger_quantity_reconciled",
                        RiskAction.PAUSE_NEW_ENTRY,
                        "Pair quantity changed without a recorded fill",
                        pair_id=current.pair_id, persistent=True)
            self._persist_runtime()

    async def _hedge(self, net: float, pair_id: Optional[str] = None) -> None:
        """Reduce the venue that carries the imbalance back toward net zero
        (reduce-only taker with hedge_slippage_bps price protection)."""
        cfg = self.cfg
        is_sell = net > 0
        sgn = 1.0 if net > 0 else -1.0
        slip = cfg.hedge_slippage_bps / 1e4
        for v in sorted(self.venues.values(),
                        key=lambda x: (self._venue_limited(x), -x.position * sgn)):
            if v.position * sgn <= 0:
                continue
            if v.key in self._venue_down or not self._book_quality(v).ok:
                continue  # unreachable or blind: cannot hedge here
            lk = self._vlock(v.key)
            if lk.locked():
                continue
            qty = floor_step(min(abs(net), abs(v.position)), self._step)
            if qty < v.min_base:
                continue
            ref = v.book.best_bid() if is_sell else v.book.best_ask()
            if ref is None:
                continue
            limit = v.px_round(ref * (1 - slip), False) if is_sell \
                else v.px_round(ref * (1 + slip), True)
            minimum_quote = max(
                v.min_quote, cfg.min_order_notional / self._quote_rate(v))
            if qty * limit < minimum_quote:
                continue
            await lk.acquire()  # verified free, no awaits since: fast path
            try:
                log.warning("[HEDGE] net %+.6g — %s %.6g on %s @%.6g",
                            net, "SELL" if is_sell else "BUY", qty, v.name, limit)
                self.hedges += 1
                self._record_send(v)  # counts toward the budget, never blocked
                try:
                    raw = await self._send_bounded(
                        v,
                        is_buy=not is_sell, qty=qty,
                        limit_px=limit, reduce_only=True)
                except Exception as exc:
                    raw = exc
                info = self._normalize_order_result(
                    raw, qty, venue_key=v.key, pair_id=pair_id)
                if info.get("err") or info.get("unresolved"):
                    log.error("[HEDGE] %s: %s", v.name,
                              info.get("err") or "unresolved")
                    if str(info.get("err", "")).startswith("RATE_LIMITED"):
                        self._mark_limited(v)
                    if info.get("unresolved"):
                        self._risk_event(
                            "unknown_risk_order_outcome",
                            RiskAction.PAUSE_NEW_ENTRY,
                            "reduce-only hedge outcome is unknown",
                            pair_id=pair_id, persistent=True)
                    self._reconcile_evt.set()
                else:
                    fill = info["filled_base"]
                    v.position += -fill if is_sell else fill
                    if fill:
                        px = info.get("avg_px")
                        if px is None:
                            v.accounting_complete = False
                            self._risk_event(
                                "incomplete_fill_accounting",
                                RiskAction.PAUSE_NEW_ENTRY,
                                "hedge fill has no actual average price",
                                pair_id=pair_id, persistent=True)
                        else:
                            fee = v.fee_bps / 1e4
                            v.cash += fill * px * (1 - fee) if is_sell \
                                else -fill * px * (1 + fee)
                            v.volume_usd += self._notional_usd(v, fill, px)
                        context = (self._unmatched_legs.get(pair_id)
                                   if pair_id else None)
                        if (px is not None and context
                                and context.get("venue_key") == v.key
                                and bool(context.get("original_is_buy"))
                                == is_sell):
                            recovered = min(
                                fill, float(context.get("remaining_qty", 0.0)))
                            self._record_recovery_roundtrip(
                                pair_id,
                                PairDirection(context["pair_direction"]), v,
                                bool(context["original_is_buy"]),
                                context.get("original_px"), px, recovered,
                                complete_if_flat=not self.pair_position.is_open,
                                signal_spread_bps=context.get(
                                    "signal_spread_bps"),
                                signal_z_score=context.get("signal_z_score"),
                                signal_midline_bps=context.get(
                                    "signal_midline_bps"))
                            context["remaining_qty"] -= recovered
                            if context["remaining_qty"] <= self._step / 2:
                                self._unmatched_legs.pop(pair_id, None)
                            self._persist_runtime()
                    log.info("[HEDGE SETTLED] %s %s %.6g/%.6g",
                             v.name, info["status"], fill, qty)
                v.last_traded_ts = time.time()
            finally:
                lk.release()
            return
        log.warning("[HEDGE] net %+.6g below hedgeable minimum — carrying "
                    "(next reconcile retries)", net)

    # --------------------------------------------------- reconcile / status

    # Lighter's REST account state lags its ws settlements; overwriting a
    # venue that traded seconds ago "restores" stale positions and triggers
    # phantom hedge oscillations. Grace-guard + venue lock prevent that.
    RECONCILE_GRACE_SEC = 5.0

    async def _reconcile_positions(self, hedge: bool,
                                   strict: bool = False) -> None:
        now = time.time()
        vs = []
        for v in self.venues.values():
            if now - v.last_traded_ts <= self.RECONCILE_GRACE_SEC:
                continue  # just traded: chain read would be stale
            if v.key in self._venue_down \
                    and now < self._venue_probe_at.get(v.key, 0.0):
                continue  # down venue: probe only every venue_probe_sec
            vs.append(v)
        if not vs:
            return
        got = await asyncio.gather(
            *(self._reconcile_venue(v, strict) for v in vs),
            return_exceptions=True)
        for r in got:
            if isinstance(r, BaseException):
                raise r  # strict startup: fail loudly
        if hedge:
            await self._maybe_hedge()
        self._sync_pair_position_from_venues()
        self._settle_recovery_executions()

    async def _reconcile_venue(self, v, strict: bool) -> None:
        async with self._vlock(v.key):
            now = time.time()
            if now - v.last_traded_ts <= self.RECONCILE_GRACE_SEC:
                return  # traded while waiting for the lock
            try:
                r = await v.fetch_position()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if strict:
                    raise RuntimeError(
                        f"[{v.name}] cannot fetch starting position: {e!r}")
                # exchange unreachable (e.g. scheduled maintenance): pause
                # trading and keep probing until it answers again
                n = self._venue_fetch_fails.get(v.key, 0) + 1
                self._venue_fetch_fails[v.key] = n
                self._venue_probe_at[v.key] = now + self.cfg.venue_probe_sec
                if n >= 3 and v.key not in self._venue_down:
                    self._venue_down[v.key] = now
                    log.critical("[%s] API unreachable (%d attempts) — "
                                 "trading PAUSED; probing every %.0fs until "
                                  "it recovers", v.name, n,
                                  self.cfg.venue_probe_sec)
                    self._risk_event(
                        f"venue_disconnect_{v.key}",
                        RiskAction.PAUSE_NEW_ENTRY,
                        f"{v.name} API unreachable after {n} attempts")
                elif v.key not in self._venue_down:
                    log.warning("[%s] position fetch failed (%d): %r",
                                v.name, n, e)
                return
            try:
                r = float(r)
            except (TypeError, ValueError):
                r = float("nan")
            if not math.isfinite(r):
                if strict:
                    raise RuntimeError(
                        f"[{v.name}] starting position is not finite")
                self._risk_event(
                    f"invalid_position_{v.key}",
                    RiskAction.PAUSE_NEW_ENTRY,
                    f"{v.name} returned a non-finite position",
                    persistent=True)
                return
            if v.key in self._venue_down:
                log.warning("[%s] API recovered after %.0fs outage — "
                             "trading RESUMED", v.name,
                             now - self._venue_down.pop(v.key))
                self._clear_transient_risk(f"venue_disconnect_{v.key}")
                self._update_evt.set()
            self._venue_fetch_fails[v.key] = 0
            delta = r - v.position
            if abs(delta) > 1e-12:
                if abs(delta) > self.cfg.net_tolerance_base:
                    log.warning("[%s] reconcile: chain %+.6g vs local %+.6g "
                                "— adopting chain", v.name, r, v.position)
                mid = v.book.mid()
                mismatch_usd = (self._notional_usd(v, delta, mid)
                                if mid is not None else None)
                if (self.cfg.kill_switch.enabled and mismatch_usd is not None
                        and mismatch_usd
                        > self.cfg.kill_switch.max_reconcile_mismatch_usd):
                    self._risk_event(
                        "position_reconcile_mismatch",
                        RiskAction.PAUSE_NEW_ENTRY,
                        f"{v.name} chain/local position mismatch",
                        observed_value=mismatch_usd,
                        threshold=self.cfg.kill_switch.max_reconcile_mismatch_usd,
                        persistent=True)
                # A position query proves quantity, not historical execution
                # price. Never synthesize cash/PnL from the current mid.
                v.accounting_complete = False
                v.position = r

    async def _reconcile_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self._reconcile_evt.wait(),
                                       timeout=self.cfg.reconcile_sec)
                self._reconcile_evt.clear()
                await asyncio.sleep(1.0)
            except asyncio.TimeoutError:
                pass
            if self.stop.is_set():
                break
            try:
                await self._reconcile_positions(hedge=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile failed")

    async def _balance_loop(self) -> None:
        while not self.stop.is_set():
            for v in self.venues.values():
                try:
                    got = await v.fetch_equity()
                    if got is not None:
                        v.equity, v.free = got
                        if v.start_equity is None:
                            v.start_equity = v.equity
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("[%s] equity poll failed: %r", v.name, e)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=BALANCE_POLL_SEC)
            except asyncio.TimeoutError:
                pass

    async def _http_keepalive_loop(self) -> None:
        if self.cfg.http_keepalive_sec <= 0:
            return
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(),
                                       timeout=self.cfg.http_keepalive_sec)
                return
            except asyncio.TimeoutError:
                pass
            await asyncio.gather(*(v.warm_http() for v in self.venues.values()),
                                 return_exceptions=True)

    def account_delta(self) -> Optional[float]:
        """Change in real account equity since start (both venues)."""
        total = 0.0
        now = time.time()
        for v in self.venues.values():
            if v.equity is None or v.start_equity is None:
                return None
            try:
                quote_rate = self.costs.fresh_quote_rate(v.key, now)
            except KeyError:
                return None
            total += (v.equity - v.start_equity) * quote_rate
        return total

    def session_pnl(self) -> Optional[float]:
        total = 0.0
        now = time.time()
        for v in self.venues.values():
            if not getattr(v, "accounting_complete", True):
                return None
            m = v.book.mid()
            if m is None:
                return None
            try:
                quote_rate = self.costs.fresh_quote_rate(v.key, now)
            except KeyError:
                return None
            total += (v.cash + v.position * m) * quote_rate
        if self._mtm_baseline is None:
            self._mtm_baseline = total
        return total - self._mtm_baseline

    def premium_bps(self) -> Optional[float]:
        em, hm = self.entropy.book.mid(), self.hedge.book.mid()
        if not (em and hm):
            return None
        ratio = em / hm
        if self.cfg.threshold_price_basis == "usd":
            try:
                ratio *= (self.costs.fresh_quote_rate("entropy")
                          / self.costs.fresh_quote_rate("hedge"))
            except KeyError:
                return None
        return (ratio - 1.0) * 1e4

    async def _status_loop(self) -> None:
        cfg = self.cfg
        while not self.stop.is_set():
            try:
                await asyncio.sleep(cfg.status_interval_sec)
            except asyncio.CancelledError:
                raise
            now = time.time()
            qualities = {v.key: self._book_quality(v, now)
                         for v in self.venues.values()}
            books = " | ".join(
                f"{v.name} {v.book.best_bid() or '—'}/{v.book.best_ask() or '—'}"
                f" age={qualities[v.key].book_age_ms:.0f}ms"
                if qualities[v.key].book_age_ms is not None else
                f"{v.name} {v.book.best_bid() or '—'}/{v.book.best_ask() or '—'}"
                for v in self.venues.values())
            books += " | " + " ".join(
                f"{v.name}:{qualities[v.key].reason}"
                for v in self.venues.values() if not qualities[v.key].ok)
            books += " " + " ".join(
                f"{v.name}:RATE-LTD" for v in self.venues.values()
                if self._venue_limited(v))
            books += " " + " ".join(
                f"{v.name}:DOWN" for v in self.venues.values()
                if v.key in self._venue_down)
            prem = self.premium_bps()
            prem_s = f"{prem:+.2f}" if prem is not None else "—"
            pos = " ".join(f"{v.name} {v.position:+.6g}"
                           for v in self.venues.values())
            net = sum(v.position for v in self.venues.values())
            pnl = self.session_pnl()
            rec = (f" | rec {self.recorder.rows_written} rows"
                   if self.recorder else "")
            latency = self.latency.summary("market_to_signal_ms")
            latency_s = (f" | mkt->sig p50/p95/p99 "
                         f"{latency.p50_ms:.2f}/{latency.p95_ms:.2f}/"
                         f"{latency.p99_ms:.2f}ms" if latency else "")
            active_midline = self.active_midline_bps()
            stats = self.spread_stats
            stat_s = (f" fast={stats.fast_midline_bps:+.2f} "
                      f"slow={stats.slow_midline_bps:+.2f} "
                      f"vol={stats.volatility_bps:.2f} z={stats.z_score:+.2f}"
                      if stats is not None else "")
            regime_s = self.strategy_pause_reason() or "healthy"
            log.info("[status] %s | session=%s | prem %s bps "
                     "(band %+.2f..%+.2f)%s "
                     "regime=%s | pos %s "
                     "net %+.6g | trades %d hedges %d | MTM %s expEdge $%.4f "
                     "fillEdge $%.4f%s%s",
                     books, self.market_session.session.value,
                     prem_s, active_midline - cfg.lower_bps,
                     active_midline + cfg.upper_bps, stat_s, regime_s,
                     pos, net, self.trades,
                     self.hedges,
                     f"${pnl:+.4f}" if pnl is not None else "—",
                     self.total_exp_edge, self.total_fill_edge, rec + latency_s,
                     " *** HALTED ***" if self.halted else "")
            self._persist_runtime()

    def _log_csv(self, direction, buy, sell, plan: ArbPlan, ok: bool, bfill,
                 sfill, bstatus, sstatus, fill_edge, inv_bps) -> None:
        try:
            path = self.cfg.trades_csv
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(path):
                with open(path) as fh0:
                    if fh0.readline().strip() != ",".join(CSV_HEADER):
                        archive = path + time.strftime(".old.%Y%m%d-%H%M%S")
                        os.replace(path, archive)
            new = not os.path.exists(path)
            with open(path, "a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(CSV_HEADER)
                w.writerow([f"{time.time():.3f}",
                            direction, buy.name, sell.name, f"{plan.qty:.8g}",
                            plan.buy_limit, plan.sell_limit,
                            f"{plan.buy_notional:.2f}", f"{plan.sell_notional:.2f}",
                            f"{plan.exp_edge_usd:.4f}", f"{plan.gross_edge_usd:.4f}",
                            f"{plan.marginal_premium_bps:.3f}",
                            plan.sizing_mode, f"{plan.buy_vwap:.8g}",
                            f"{plan.sell_vwap:.8g}",
                             f"{plan.gross_vwap_edge_bps:.3f}",
                             f"{plan.adjusted_gross_edge_bps:.3f}",
                             f"{plan.funding_cost_bps:.3f}",
                             f"{plan.stablecoin_basis_bps:.3f}",
                             f"{plan.expected_net_edge_bps:.3f}",
                            f"{plan.required_net_edge_bps:.3f}",
                            f"{plan.fee_cost_bps:.3f}",
                            f"{plan.extra_cost_bps:.3f}",
                            f"{plan.max_vwap_slippage_bps:.3f}",
                            f"{plan.max_book_impact_bps:.3f}",
                            "" if plan.signal_spread_bps is None else
                            f"{plan.signal_spread_bps:.3f}",
                            f"{(plan.signal_midline_bps if plan.signal_midline_bps is not None else self.active_midline_bps()):.3f}",
                            "" if plan.signal_fast_midline_bps is None else
                            f"{plan.signal_fast_midline_bps:.3f}",
                            "" if plan.signal_volatility_bps is None else
                            f"{plan.signal_volatility_bps:.3f}",
                            "" if plan.signal_deviation_bps is None else
                            f"{plan.signal_deviation_bps:.3f}",
                            "" if plan.signal_z_score is None else
                            f"{plan.signal_z_score:.3f}", plan.regime_state,
                            plan.pair_id or "", plan.signal_action,
                            self.market_session.session.value,
                            f"{inv_bps:.3f}", int(ok), f"{bfill:.8g}",
                            f"{sfill:.8g}", bstatus, sstatus, f"{fill_edge:.4f}"])
        except Exception:
            log.exception("csv write failed")
