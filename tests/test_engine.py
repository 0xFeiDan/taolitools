"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import csv
import os
import sys
import tempfile
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from dataclasses import replace

from entropy_arb.config import MarketDataConfig, load_config  # noqa: E402
from entropy_arb.engine import CSV_HEADER, Engine  # noqa: E402
from entropy_arb.ledger import PairLedger  # noqa: E402
from entropy_arb.midline import (  # noqa: E402
    DynamicMidline,
    RegimeDetector,
    SpreadStats,
)
from entropy_arb.models import (  # noqa: E402
    ExecutionState,
    PairDirection,
    PairExecution,
    RiskAction,
)
from entropy_arb.session import (  # noqa: E402
    MarketSession,
    SessionStatus,
)

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(midline=5.0, upper=4.0, lower=3.0):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {midline}
  upper_bps: {upper}
  lower_bps: {lower}
execution:
  premium_persist_sec: 0.0
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue="lighter-rh")


class StubVenue:
    def __init__(self, key, label, cap=10000.0, fee=0.0):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps = cap, fee
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash = 0.0, 0.0
        self.volume_usd = 0.0
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()
        self.send_delay = 0.0
        self.fill_fraction = 1.0
        self.sent_orders = []

    def ready_to_trade(self):
        return True

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])

    def px_round(self, price, round_up=False):
        return price

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False):
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        now = __import__("time").time()
        filled = qty * self.fill_fraction
        self.sent_orders.append({
            "is_buy": is_buy, "qty": qty, "reduce_only": reduce_only})
        return {
            "status": "filled", "filled_base": filled, "avg_px": limit_px,
            "err": None, "unresolved": False, "order_send_ts": now,
            "order_ack_ts": now, "first_fill_ts": now,
            "final_fill_ts": now,
        }


def make_engine(**thr):
    cfg = make_cfg(**thr)
    eng = Engine(cfg)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    return eng


def enable_vwap(eng, **overrides):
    defaults = dict(
        enabled=True,
        min_order_usd=10.0,
        max_order_usd=10_000.0,
        minimum_net_edge_bps=1.0,
        max_vwap_slippage_bps=100.0,
        max_book_impact_bps=100.0,
        safety_buffer_bps=0.0,
        expected_latency_cost_bps=0.0,
    )
    defaults.update(overrides)
    eng.cfg.vwap_sizing = replace(eng.cfg.vwap_sizing, **defaults)


def enable_dynamic(eng, *, min_samples=3, regime=False,
                   break_persist_seconds=0.0,
                   recovery_persist_seconds=2.0, max_z_score=5.0,
                   max_absolute_spread_bps=50.0,
                   max_fast_slow_difference_bps=8.0,
                   entry_z_score=1.5, exit_z_score=0.5):
    eng.cfg.midline = replace(
        eng.cfg.midline, mode="dynamic", fast_window_seconds=10.0,
        slow_window_seconds=30.0, min_samples=min_samples,
        volatility_window_seconds=30.0, volatility_floor_bps=0.1,
        entry_z_score=entry_z_score, exit_z_score=exit_z_score)
    eng.cfg.regime = replace(
        eng.cfg.regime, enabled=regime,
        break_persist_seconds=break_persist_seconds,
        recovery_persist_seconds=recovery_persist_seconds,
        max_z_score=max_z_score,
        max_absolute_spread_bps=max_absolute_spread_bps,
        max_fast_slow_difference_bps=max_fast_slow_difference_bps)
    mid = eng.cfg.midline
    eng.dynamic_midline = DynamicMidline(
        fast_window_seconds=mid.fast_window_seconds,
        slow_window_seconds=mid.slow_window_seconds,
        min_samples=mid.min_samples,
        volatility_method=mid.volatility_method,
        volatility_window_seconds=mid.volatility_window_seconds,
        volatility_floor_bps=mid.volatility_floor_bps)
    eng.regime_detector = (RegimeDetector(
        max_fast_slow_difference_bps=max_fast_slow_difference_bps,
        max_z_score=max_z_score,
        max_absolute_spread_bps=max_absolute_spread_bps,
        break_persist_seconds=break_persist_seconds,
        recovery_persist_seconds=recovery_persist_seconds)
        if regime else None)
    eng.spread_stats = None
    eng._last_spread_sample_ts = None
    eng._last_spread_book_versions = None


def set_premium(eng, premium_bps, size=50.0):
    hedge_mid = 100.0
    entropy_mid = hedge_mid * (1.0 + premium_bps / 1e4)
    eng.hedge.set_book(hedge_mid - 0.01, hedge_mid + 0.01, size)
    eng.entropy.set_book(entropy_mid - 0.01, entropy_mid + 0.01, size)


def set_ready_stats(eng, *, z_score, slow=5.0, volatility=2.0):
    now = __import__("time").time()
    spread = slow + z_score * volatility
    set_premium(eng, spread)
    eng.spread_stats = SpreadStats(
        timestamp=now, spread_bps=spread,
        fast_midline_bps=slow, slow_midline_bps=slow,
        volatility_bps=volatility, deviation_bps=z_score * volatility,
        z_score=z_score, sample_count=eng.cfg.midline.min_samples,
        window_span_seconds=eng.cfg.midline.min_samples - 1, ready=True)
    eng._last_spread_sample_ts = now
    eng._last_spread_book_versions = (
        eng.entropy.book.last_update_ts, eng.hedge.book.last_update_ts)
    eng._last_ready_spread_stats = eng.spread_stats


def approx(a, b, tol=1e-9):
    assert abs(a - b) <= tol, f"{a} != {b}"


class FixedSessionClock:
    def __init__(self, session):
        self.set(session)

    def set(self, session):
        session = MarketSession(session)
        self.current = SessionStatus(
            session=session,
            local_time=datetime.now(timezone.utc),
            entry_allowed=session in (
                MarketSession.REGULAR, MarketSession.CRYPTO_24X7),
            sampleable=session is not MarketSession.CLOSED)

    def status(self, timestamp=None):
        return self.current


def enable_stock_sessions(eng, session=MarketSession.REGULAR):
    eng.cfg.session = replace(eng.cfg.session, enabled=True)
    eng.session_clock = FixedSessionClock(session)
    eng._activate_market_session()


def test_eff_threshold_directions():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    e, h = eng.entropy, eng.hedge
    # sell entropy: hurdle = midline + upper = 9
    approx(eng._eff_threshold(buy=h, sell=e), 9.0)
    # buy entropy: hurdle = lower - midline = -2 (unwind side of a positive
    # midline is deliberately cheap — that's what completes the round trip)
    approx(eng._eff_threshold(buy=e, sell=h), -2.0)
    # round trip nets upper + lower regardless of midline sign
    for m in (-7.0, 0.0, 12.5):
        eng.cfg.midline_bps = m
        total = eng._eff_threshold(buy=h, sell=e) + eng._eff_threshold(buy=e, sell=h)
        approx(total, 7.0)


def test_vwap_hurdle_preserves_static_midline_round_trip_semantics():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    enable_vwap(eng, minimum_net_edge_bps=6.0)
    e, h = eng.entropy, eng.hedge
    # The configured minimum is a deviation from the directional midline.
    # It therefore applies symmetrically without making the normal unwind
    # side of a non-zero basis impossible.
    approx(eng._vwap_required_net_edge(buy=h, sell=e), 11.0)
    approx(eng._vwap_required_net_edge(buy=e, sell=h), 1.0)


def test_engine_passes_funding_and_quote_basis_into_vwap_plan():
    eng = make_engine(midline=0.0, upper=4.0, lower=4.0)
    enable_vwap(eng, minimum_net_edge_bps=1.0)
    eng.cfg.funding = replace(eng.cfg.funding, enabled=True)
    eng.cfg.stablecoin = replace(eng.cfg.stablecoin, enabled=True)
    eng.costs.funding_enabled = True
    eng.costs.stablecoin_enabled = True
    eng.costs.set_funding("entropy", 0.0001)
    eng.costs.set_funding("hedge", 0.0003)
    eng.costs.set_quote_usd("USDC", 1.0)
    eng.costs.set_quote_usd("USDG", 0.999)
    set_premium(eng, 20.0)

    buy, sell, plan = run_scan(eng)
    assert buy.key == "hedge" and sell.key == "entropy"
    approx(plan.funding_cost_bps, 2.0)
    assert plan.stablecoin_basis_bps < 0
    assert plan.adjusted_gross_edge_bps > plan.gross_vwap_edge_bps


def test_dynamic_warmup_blocks_then_zscore_ignores_static_bps_band():
    eng = make_engine(midline=0.0, upper=1000.0, lower=1000.0)
    enable_dynamic(eng, min_samples=3)
    now = __import__("time").time()
    for offset, premium in ((0.0, 5.0), (1.0, 5.0)):
        set_premium(eng, premium)
        eng._update_spread_state(now + offset, force=True)
    assert eng.strategy_pause_reason() == "dynamic_warmup"
    assert eng._scan(now + 1.1) is None

    set_premium(eng, 15.0)
    eng._update_spread_state(now + 2.0, force=True)
    assert eng.strategy_pause_reason() is None
    approx(eng.active_midline_bps(), 5.0, 1e-6)
    best = run_scan(eng)
    assert best is not None and best[1].key == "entropy"
    approx(best[2].signal_midline_bps, 5.0, 1e-6)
    assert best[2].signal_z_score > 1.0
    assert best[2].signal_action == "OPEN"


def test_session_switch_is_off_for_crypto_and_blocks_stock_open_outside_rth():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    set_premium(eng, 20.0)
    assert eng.strategy_pause_reason() is None
    assert run_scan(eng) is not None       # default crypto remains 24/7

    enable_stock_sessions(eng, MarketSession.PRE_MARKET)
    assert eng.strategy_pause_reason() == "session:pre_market"
    assert run_scan(eng) is None

    eng.session_clock.set(MarketSession.REGULAR)
    assert eng.strategy_pause_reason() is None
    assert run_scan(eng) is not None


def test_dynamic_midline_samples_are_isolated_by_stock_session():
    eng = make_engine(midline=0.0, upper=1000.0, lower=1000.0)
    enable_dynamic(eng, min_samples=1)
    enable_stock_sessions(eng, MarketSession.PRE_MARKET)
    now = __import__("time").time()
    set_premium(eng, 5.0)
    eng._update_spread_state(now, force=True)
    pre_estimator = eng.dynamic_midline
    approx(eng.spread_stats.slow_midline_bps, 5.0, 0.01)

    eng.session_clock.set(MarketSession.REGULAR)
    set_premium(eng, 20.0)
    eng._update_spread_state(now + 1.0, force=True)
    regular_estimator = eng.dynamic_midline
    assert regular_estimator is not pre_estimator
    approx(eng.spread_stats.slow_midline_bps, 20.0, 0.01)

    eng.session_clock.set(MarketSession.PRE_MARKET)
    eng._activate_market_session(now + 2.0)
    assert eng.dynamic_midline is pre_estimator
    approx(eng.spread_stats.slow_midline_bps, 5.0, 0.01)


def test_session_pause_still_allows_dynamic_pair_exit_during_warmup():
    eng = make_engine(midline=0.0, upper=1000.0, lower=1000.0)
    enable_dynamic(eng, min_samples=3, entry_z_score=2.5,
                   exit_z_score=0.5)
    enable_vwap(eng, min_order_usd=10.0)
    enable_stock_sessions(eng, MarketSession.REGULAR)
    eng.pair_position.sync(
        PairDirection.SELL_ENTROPY, 1.0, pair_id="ARB-SESSION-EXIT")
    eng.entropy.position, eng.hedge.position = -1.0, 1.0
    set_ready_stats(eng, z_score=-1.0)

    eng.session_clock.set(MarketSession.AFTER_HOURS)
    buy, sell, plan = run_scan(eng)
    assert eng.strategy_pause_reason() == "session:after_hours"
    assert buy.key == "entropy" and sell.key == "hedge"
    assert plan.signal_action == "EXIT"
    assert plan.pair_id == "ARB-SESSION-EXIT"


def test_dynamic_flat_position_does_not_open_inside_entry_z():
    eng = make_engine(midline=0.0, upper=0.1, lower=0.1)
    enable_dynamic(eng, min_samples=1, entry_z_score=2.5)
    set_ready_stats(eng, z_score=2.0)
    assert run_scan(eng) is None


def test_dynamic_exit_is_capped_to_remaining_pair_and_cannot_reverse(tmp_path):
    eng = make_engine(midline=0.0, upper=1000.0, lower=1000.0)
    enable_dynamic(eng, min_samples=1, entry_z_score=2.5,
                   exit_z_score=0.5)
    enable_vwap(eng, min_order_usd=10.0, minimum_net_edge_bps=6.0)
    eng.cfg.trades_csv = str(tmp_path / "exit-trades.csv")
    eng.pair_position.sync(
        PairDirection.SELL_ENTROPY, 1.2345, pair_id="ARB-OPEN-1")
    eng.entropy.position = -1.2345
    eng.hedge.position = 1.2345
    # A sell-Entropy pair exits by buying Entropy after Z returns through the
    # +0.5 zone. Z=-1 also leaves enough executable room for the bid/ask cost.
    set_ready_stats(eng, z_score=-1.0)
    buy, sell, plan = run_scan(eng)
    assert buy.key == "entropy" and sell.key == "hedge"
    assert plan.signal_action == "EXIT"
    assert plan.pair_id == "ARB-OPEN-1"
    assert plan.qty <= 1.2345
    assert plan.required_net_edge_bps == -6.0
    asyncio.run(eng._execute(buy, sell, plan))
    assert not eng.pair_position.is_open
    assert abs(eng.entropy.position) < 1e-9
    assert abs(eng.hedge.position) < 1e-9


def test_startup_position_sync_recovers_only_matched_pair_quantity():
    eng = make_engine()
    eng.entropy.position = -2.0
    eng.hedge.position = 1.5
    eng._sync_pair_position_from_venues()
    assert eng.pair_position.direction is PairDirection.SELL_ENTROPY
    assert eng.pair_position.base_qty == 1.5
    assert "RECOVERED" in eng.pair_position.pair_id
    recovered_id = eng.pair_position.pair_id
    eng.entropy.position = -1.0
    eng.hedge.position = 1.0
    eng._sync_pair_position_from_venues()
    assert eng.pair_position.pair_id == recovered_id


def test_matched_open_fill_updates_minimal_pair_position(tmp_path):
    eng = make_engine(midline=0.0, upper=1000.0, lower=1000.0)
    enable_dynamic(eng, min_samples=1, entry_z_score=1.5)
    eng.cfg.trades_csv = str(tmp_path / "trades.csv")
    set_ready_stats(eng, z_score=3.0, slow=0.0, volatility=5.0)
    buy, sell, plan = run_scan(eng)
    assert plan.signal_action == "OPEN"
    asyncio.run(eng._execute(buy, sell, plan))
    assert eng.pair_position.direction is PairDirection.SELL_ENTROPY
    approx(eng.pair_position.base_qty, plan.qty)
    assert eng.pair_position.pair_id == plan.pair_id
    assert [event.to_state.value for event in eng.execution_history[-1].events] == [
        "NEW", "SIGNAL_CONFIRMED", "ORDERS_SENT", "BOTH_FILLED", "COMPLETE"]


def test_hedge_timeout_unwinds_known_open_leg_and_finishes_recovery(tmp_path):
    eng = make_engine(midline=0.0, upper=1000.0, lower=1000.0)
    enable_dynamic(eng, min_samples=1, entry_z_score=1.5)
    eng.cfg.execution_risk = replace(
        eng.cfg.execution_risk, enabled=True, hedge_timeout_ms=10.0,
        max_unhedged_delta_usd=100.0)
    eng.cfg.trades_csv = str(tmp_path / "timeout-trades.csv")
    eng.ledger = PairLedger(str(tmp_path / "pairs.jsonl"),
                            str(tmp_path / "state.json"))
    set_ready_stats(eng, z_score=3.0, slow=0.0, volatility=5.0)
    buy, sell, plan = run_scan(eng)
    sell.send_delay = 0.05

    async def execute_locked():
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, plan)

    asyncio.run(execute_locked())
    states = [event.to_state.value for event in eng.execution_history[-1].events]
    assert states == ["NEW", "SIGNAL_CONFIRMED", "ORDERS_SENT", "RECOVERY",
                      "HEDGED", "COMPLETE"]
    assert any(order["reduce_only"] for order in buy.sent_orders)
    assert any(order["reduce_only"] for order in sell.sent_orders)
    assert abs(buy.position + sell.position) < eng.cfg.net_tolerance_base
    assert eng.ledger.current is None
    assert len(eng.ledger.completed) == 1
    assert eng.ledger.completed[0].pair_id == plan.pair_id
    assert eng.ledger.completed[0].recovery_pnl <= 0


def test_consecutive_partial_fill_kill_switch_pauses_new_entries(tmp_path):
    eng = make_engine(midline=0.0, upper=1000.0, lower=1000.0)
    enable_dynamic(eng, min_samples=1, entry_z_score=1.5)
    eng.cfg.kill_switch = replace(
        eng.cfg.kill_switch, enabled=True,
        max_consecutive_partial_fills=1)
    eng.cfg.trades_csv = str(tmp_path / "partial-trades.csv")
    set_ready_stats(eng, z_score=3.0, slow=0.0, volatility=5.0)
    buy, sell, plan = run_scan(eng)
    sell.fill_fraction = 0.5

    async def execute_locked():
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, plan)

    asyncio.run(execute_locked())
    assert eng.consecutive_partial_fills == 1
    assert "consecutive_partial_fills" in eng._entry_pause_reasons
    assert eng.strategy_pause_reason().startswith("kill:")
    assert eng.risk_events[-1].action.value == "PAUSE_NEW_ENTRY"


def test_persistent_unhedged_delta_triggers_kill_pause():
    eng = make_engine()
    eng.cfg.execution_risk = replace(
        eng.cfg.execution_risk, enabled=True,
        max_unhedged_delta_usd=10.0)
    eng.cfg.kill_switch = replace(
        eng.cfg.kill_switch, enabled=True,
        max_unhedged_duration_ms=100.0,
        emergency_flatten_enabled=False)
    eng.entropy.set_book(99.9, 100.1)
    eng.hedge.set_book(99.9, 100.1)
    eng.entropy.position = 1.0
    eng._venue_down["entropy"] = __import__("time").time()

    asyncio.run(eng._risk_check_once())
    eng._unhedged_since = __import__("time").time() - 1.0
    asyncio.run(eng._risk_check_once())
    assert "net_delta_duration" in eng._entry_pause_reasons
    actions = [event.action.value for event in eng.risk_events]
    assert "EMERGENCY_HEDGE" in actions
    assert "PAUSE_NEW_ENTRY" in actions


def test_emergency_flatten_uses_reduce_only_and_updates_local_accounting():
    eng = make_engine()
    eng.entropy.set_book(99.9, 100.1)
    eng.hedge.set_book(99.9, 100.1)
    eng.entropy.position = 1.0
    eng.hedge.position = -1.0

    asyncio.run(eng._emergency_flatten())

    assert abs(eng.entropy.position) < 1e-12
    assert abs(eng.hedge.position) < 1e-12
    assert eng.entropy.volume_usd > 0 and eng.hedge.volume_usd > 0
    assert all(order["reduce_only"] for order in
               eng.entropy.sent_orders + eng.hedge.sent_orders)


def test_emergency_flatten_failure_is_retained_and_retried():
    eng = make_engine()
    eng.cfg.kill_switch = replace(
        eng.cfg.kill_switch, enabled=True,
        emergency_flatten_enabled=True,
        emergency_flatten_retry_sec=0.01,
        emergency_flatten_max_attempts=0)
    eng.entropy.set_book(99.9, 100.1)
    eng.hedge.set_book(99.9, 100.1)
    eng.entropy.position = 1.0
    eng.entropy.fill_fraction = 0.0

    assert asyncio.run(eng._emergency_flatten()) is False
    assert eng._flatten_required and eng._flatten_attempts == 1
    eng.entropy.fill_fraction = 1.0
    eng._next_flatten_at = 0.0
    asyncio.run(eng._risk_check_once())
    assert not eng._flatten_required
    assert eng._flatten_attempts == 2
    assert abs(eng.entropy.position) < 1e-12


def test_runtime_state_restores_execution_risk_and_open_pair(tmp_path):
    cfg = make_cfg()
    cfg.accounting = replace(
        cfg.accounting, enabled=True,
        ledger_jsonl=str(tmp_path / "pairs.jsonl"),
        state_json=str(tmp_path / "state.json"))
    eng = Engine(cfg)
    pair = eng.ledger.ensure_pair(
        pair_id="ARB-RESTORE", symbol="SNDK", venue_a="ENTROPY",
        venue_b="RH", direction="sell_entropy", entry_time=123.0)
    pair.entry_base = pair.remaining_base = 2.0
    eng.pair_position.sync(
        PairDirection.SELL_ENTROPY, 2.0,
        pair_id="ARB-RESTORE", at=123.0)
    execution = PairExecution.new(
        pair_id="ARB-EXEC", symbol="SNDK", venue_a="ENTROPY",
        venue_b="RH", direction=PairDirection.SELL_ENTROPY)
    eng.execution_history.append(execution)
    eng._transition_execution(
        execution, ExecutionState.SIGNAL_CONFIRMED, "restore test")
    eng._risk_event(
        "restore_pause", RiskAction.PAUSE_NEW_ENTRY,
        "restore test", persistent=True)
    eng._flatten_required = True
    eng._persist_runtime()

    restored = Engine(cfg)
    assert restored.pair_position.pair_id == "ARB-RESTORE"
    assert restored.pair_position.base_qty == 2.0
    assert restored.execution_history[-1].events[-1].reason == "restore test"
    assert restored.risk_events[-1].trigger == "restore_pause"
    assert "restore_pause" in restored._entry_pause_reasons
    assert restored._flatten_required


def test_operator_can_clear_persisted_pause_only_when_flat(tmp_path):
    cfg = make_cfg()
    cfg.accounting = replace(
        cfg.accounting, enabled=True,
        ledger_jsonl=str(tmp_path / "pairs.jsonl"),
        state_json=str(tmp_path / "state.json"))
    eng = Engine(cfg, clear_risk_pause=True)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._entry_pause_reasons.add("old_failure")
    eng._persist_runtime()

    eng.entropy.position = 1.0
    with pytest.raises(RuntimeError, match="non-flat"):
        eng._clear_persistent_risk_if_safe()
    eng.entropy.position = 0.0
    eng._clear_persistent_risk_if_safe()
    assert not eng._entry_pause_reasons
    restored = Engine(cfg)
    assert not restored._entry_pause_reasons


def test_dynamic_sampler_does_not_count_same_book_snapshot_twice():
    eng = make_engine()
    enable_dynamic(eng, min_samples=2)
    set_premium(eng, 5.0)
    now = __import__("time").time()
    eng._update_spread_state(now)
    eng._update_spread_state(now + 1.1)
    assert eng.spread_stats.sample_count == 1
    assert eng.strategy_pause_reason() == "dynamic_warmup"


def test_regime_extreme_spread_pauses_and_disarms_strategy():
    eng = make_engine(midline=0.0, upper=4.0, lower=4.0)
    enable_dynamic(eng, min_samples=1, regime=True,
                   max_absolute_spread_bps=50.0,
                   max_z_score=100.0,
                   max_fast_slow_difference_bps=100.0)
    set_premium(eng, 60.0)
    now = __import__("time").time()
    assert eng._scan(now) is None
    assert eng.regime_detector.status.paused
    assert eng.strategy_pause_reason() == "regime:absolute_spread"
    assert eng._armed == {"sell_entropy": None, "buy_entropy": None}
    assert eng.risk_events[-1].trigger == "regime_break"


def test_regime_zscore_break_blocks_large_persistent_outlier():
    eng = make_engine(midline=0.0, upper=4.0, lower=4.0)
    enable_dynamic(eng, min_samples=5, regime=True,
                   max_absolute_spread_bps=100.0,
                   max_z_score=2.0,
                   max_fast_slow_difference_bps=100.0)
    now = __import__("time").time()
    for i in range(4):
        set_premium(eng, 5.0)
        eng._update_spread_state(now + i, force=True)
    set_premium(eng, 20.0)
    eng._update_spread_state(now + 4.0, force=True)
    assert eng.spread_stats.z_score > 2.0
    assert eng.regime_detector.status.paused
    assert "z_score" in eng.regime_detector.status.reasons
    assert eng._scan(now + 4.1) is None


def test_regime_pause_blocks_add_but_still_allows_pair_exit():
    eng = make_engine(midline=0.0, upper=1000.0, lower=1000.0)
    enable_dynamic(eng, min_samples=1, regime=True,
                   entry_z_score=2.5, exit_z_score=0.5)
    enable_vwap(eng, min_order_usd=10.0)
    eng.pair_position.sync(
        PairDirection.SELL_ENTROPY, 1.0, pair_id="ARB-EXIT-RISK")
    eng.entropy.position, eng.hedge.position = -1.0, 1.0
    set_ready_stats(eng, z_score=-1.0)
    healthy = eng.spread_stats
    abnormal = replace(
        healthy, spread_bps=60.0, fast_midline_bps=20.0,
        z_score=6.0, deviation_bps=55.0)
    assert eng.regime_detector.update(abnormal).paused
    eng.spread_stats = healthy

    buy, sell, plan = run_scan(eng)
    assert eng.strategy_pause_reason().startswith("regime:")
    assert buy.key == "entropy" and sell.key == "hedge"
    assert plan.signal_action == "EXIT"


def test_inventory_ladder():
    eng = make_engine()
    eng.cfg.inventory_scale_bps, eng.cfg.inventory_floor_frac = 10.0, 0.5
    e, h = eng.entropy, eng.hedge
    e.set_book(99.9, 100.1)   # mid 100
    h.set_book(99.9, 100.1)
    approx(eng._inv_add_bps(e, h), 0.0)          # flat: dead zone
    e.position = 90.0                             # long $9k of $10k cap
    v = eng._inv_add_bps(e, h)                    # buying entropy adds long
    assert 7.5 < v < 8.5, v                       # u=0.9 -> ~+8
    approx(eng._inv_add_bps(h, e), 0.0)           # selling entropy reduces
    h.position = -90.0                            # hedge short $9k too
    v2 = eng._inv_add_bps(e, h)                   # both legs add -> max()
    assert abs(v2 - v) < 0.6, (v, v2)             # max, not sum


def run_scan(eng):
    async def go():
        # first pass arms the direction, second passes the persistence gate
        # (premium_persist_sec is 0 in the test config)
        eng._scan(__import__("time").time())
        return eng._scan(__import__("time").time())
    return asyncio.run(go())


def test_scan_fires_sell_entropy_above_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 15 bps rich vs hedge: above midline+upper=9 -> sell entropy
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert sell.key == "entropy" and buy.key == "hedge"
    assert plan.exp_edge_usd > 0


def test_scan_quiet_inside_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 5 bps rich = exactly on the midline: inside the band, no trade
    eng.entropy.set_book(100.04, 100.06)
    eng.hedge.set_book(99.99, 100.01)
    assert run_scan(eng) is None


def test_scan_fires_buy_entropy_below_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 5 bps CHEAP (premium -5): below midline-lower=+2 -> buy entropy
    eng.entropy.set_book(99.94, 99.96)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert buy.key == "entropy" and sell.key == "hedge"


def test_vwap_scan_uses_current_depth_and_auto_sizes():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    enable_vwap(eng, max_order_usd=1_000.0,
                minimum_net_edge_bps=5.0)
    eng.entropy.set_book(100.20, 100.22, sz=50.0)
    eng.hedge.set_book(99.98, 100.00, sz=50.0)

    buy, sell, plan = run_scan(eng)
    assert buy.key == "hedge" and sell.key == "entropy"
    assert plan.sizing_mode == "vwap"
    assert 990.0 < plan.buy_notional <= 1_000.0
    assert plan.expected_net_edge_bps >= plan.required_net_edge_bps
    approx(plan.buy_vwap, 100.0)
    approx(plan.sell_vwap, 100.2)


def test_vwap_scan_binary_searches_below_configured_cap():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    enable_vwap(eng, max_order_usd=2_000.0,
                minimum_net_edge_bps=10.0)
    eng.hedge.book.apply_hl([
        [{"px": "99.9", "sz": "20"}],
        [{"px": "100.0", "sz": "10"},
         {"px": "100.1", "sz": "10"}],
    ])
    eng.entropy.book.apply_hl([
        [{"px": "100.2", "sz": "10"},
         {"px": "100.0", "sz": "10"}],
        [{"px": "100.3", "sz": "20"}],
    ])

    buy, sell, plan = run_scan(eng)
    assert buy.key == "hedge" and sell.key == "entropy"
    assert 14.0 < plan.qty < 15.0
    assert plan.buy_notional < 1_501.0
    assert plan.expected_net_edge_bps >= 10.0


def test_vwap_scan_rejects_edge_consumed_by_safety_buffer():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    enable_vwap(eng, minimum_net_edge_bps=5.0,
                safety_buffer_bps=6.0)
    # Visible edge is about 10 bps, but 6 bps safety leaves <5 bps.
    eng.entropy.set_book(100.10, 100.12)
    eng.hedge.set_book(99.98, 100.00)
    assert run_scan(eng) is None


def test_vwap_trade_csv_has_named_cost_and_depth_metrics(tmp_path):
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    enable_vwap(eng, max_order_usd=1_000.0,
                minimum_net_edge_bps=5.0)
    eng.cfg.trades_csv = str(tmp_path / "trades.csv")
    eng.entropy.set_book(100.20, 100.22, sz=50.0)
    eng.hedge.set_book(99.98, 100.00, sz=50.0)
    buy, sell, plan = run_scan(eng)

    eng._log_csv("sell_entropy", buy, sell, plan, True,
                 plan.qty, plan.qty, "filled", "filled", 1.0, 0.0)
    with open(eng.cfg.trades_csv, newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == CSV_HEADER
    assert len(rows[0]) == len(rows[1])
    record = dict(zip(rows[0], rows[1]))
    assert record["sizing_mode"] == "vwap"
    assert record["market_session"] == "crypto_24x7"
    assert float(record["expected_net_edge_bps"]) >= 5.0
    assert float(record["buy_vwap"]) == 100.0


def test_scan_respects_position_caps():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng.entropy.position = -100.0   # entropy already short at its cap
    eng.entropy.cap_usd = 10000.0
    eng.hedge.position = 100.0
    eng.hedge.cap_usd = 10000.0
    assert run_scan(eng) is None


def test_scan_rejects_old_book_even_when_heartbeat_is_live():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    eng.cfg.market_data = MarketDataConfig(
        enforce_book_age=True, max_book_age_ms=300.0)
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)

    now = __import__("time").time()
    eng.entropy.book.last_update_ts = now - 1.0
    eng.entropy.book.touch(now)  # websocket heartbeat is current
    assert eng._scan(now) is None
    assert eng._armed["sell_entropy"] is None


def test_scan_keeps_legacy_connection_freshness_when_guard_off():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    now = __import__("time").time()
    eng.entropy.book.last_update_ts = now - 60.0
    eng.entropy.book.touch(now)
    assert run_scan(eng) is not None


def test_market_to_signal_latency_is_recorded():
    eng = make_engine()
    eng.entropy.set_book(100.0, 100.1)
    eng.hedge.set_book(99.9, 100.0)
    eng.entropy.book.last_update_ts = 100.000
    eng.hedge.book.last_update_ts = 99.980
    eng.entropy.book.exchange_ts = 99.990
    eng._record_market_latency(eng.entropy, eng.hedge, signal_ts=100.004)

    assert round(eng.latency.summary("market_to_signal_ms").p50_ms, 6) == 4.0
    assert round(eng.latency.summary("book_update_skew_ms").p50_ms, 6) == 20.0
    assert round(eng.latency.summary(
        "entropy_exchange_to_local_ms").p50_ms, 6) == 10.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
