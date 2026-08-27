"""V2 foundation models: IDs, state transitions, timing, and risk events."""
from datetime import datetime, timezone

from entropy_arb.models import (
    ExecutionState,
    LatencyTimeline,
    MarketDataTiming,
    PairDirection,
    PairExecution,
    PairPosition,
    RiskAction,
    RiskEvent,
    SignalAction,
    make_pair_id,
)


def test_pair_id_is_utc_and_traceable():
    pair_id = make_pair_id(
        datetime(2026, 8, 27, 1, 2, 3, tzinfo=timezone.utc), "abc123")
    assert pair_id == "ARB-20260827-010203-ABC123"


def test_pair_state_machine_happy_path_records_events():
    pair = PairExecution.new(
        pair_id="ARB-TEST-1", symbol="SNDK", venue_a="ENTROPY",
        venue_b="RH", direction=PairDirection.SELL_ENTROPY)
    pair.transition(ExecutionState.SIGNAL_CONFIRMED, "edge persisted", at=2.0)
    pair.transition(ExecutionState.ORDERS_SENT, "both legs dispatched", at=3.0)
    pair.transition(ExecutionState.BOTH_FILLED, "both legs filled", at=4.0)
    pair.transition(ExecutionState.COMPLETE, "delta neutral", at=5.0)

    assert pair.state is ExecutionState.COMPLETE
    assert [event.to_state for event in pair.events] == [
        ExecutionState.NEW,
        ExecutionState.SIGNAL_CONFIRMED,
        ExecutionState.ORDERS_SENT,
        ExecutionState.BOTH_FILLED,
        ExecutionState.COMPLETE,
    ]
    assert pair.events[-1].from_state is ExecutionState.BOTH_FILLED


def test_pair_state_machine_partial_recovery_path():
    pair = PairExecution.new(
        pair_id="ARB-TEST-2", symbol="SNDK", venue_a="ENTROPY",
        venue_b="LIGHTER", direction=PairDirection.BUY_ENTROPY)
    for state in (ExecutionState.SIGNAL_CONFIRMED,
                  ExecutionState.ORDERS_SENT,
                  ExecutionState.PARTIAL,
                  ExecutionState.RECOVERY,
                  ExecutionState.HEDGED,
                  ExecutionState.COMPLETE):
        pair.transition(state, state.value.lower())
    assert pair.state.terminal


def test_invalid_or_post_terminal_transition_is_rejected():
    pair = PairExecution.new(
        pair_id="ARB-TEST-3", symbol="SNDK", venue_a="ENTROPY",
        venue_b="RH", direction=PairDirection.SELL_ENTROPY)
    try:
        pair.transition(ExecutionState.BOTH_FILLED, "skipped send")
    except ValueError as exc:
        assert "NEW -> BOTH_FILLED" in str(exc)
    else:
        raise AssertionError("invalid transition should fail")

    pair.transition(ExecutionState.FAILED, "signal rejected")
    try:
        pair.transition(ExecutionState.COMPLETE, "cannot revive")
    except ValueError as exc:
        assert "FAILED -> COMPLETE" in str(exc)
    else:
        raise AssertionError("terminal transition should fail")


def test_latency_metrics_and_leg_gap():
    timeline = LatencyTimeline(
        market_data_receive_ts=10.000,
        signal_generated_ts=10.004,
        order_send_ts=10.006,
        order_ack_ts=10.036,
        first_fill_ts=10.076,
        final_fill_ts=10.101,
        leg_a_final_fill_ts=10.090,
        leg_b_final_fill_ts=10.101,
    )
    metrics = timeline.metrics_ms()
    assert round(metrics["market_to_signal_ms"], 6) == 4.0
    assert round(metrics["signal_to_send_ms"], 6) == 2.0
    assert round(metrics["send_to_ack_ms"], 6) == 30.0
    assert round(metrics["ack_to_fill_ms"], 6) == 40.0
    assert round(metrics["leg_fill_gap_ms"], 6) == 11.0


def test_market_data_timing_and_risk_event():
    timing = MarketDataTiming(exchange_ts=10.100, local_receive_ts=10.125)
    assert round(timing.transport_latency_ms(), 6) == 25.0
    assert round(timing.book_age_ms(now=10.300), 6) == 175.0

    event = RiskEvent(
        trigger="net_delta_limit", action=RiskAction.EMERGENCY_HEDGE,
        reason="unhedged delta exceeded threshold", pair_id="ARB-TEST-4",
        observed_value=5200.0, threshold=5000.0)
    assert event.action is RiskAction.EMERGENCY_HEDGE
    assert event.observed_value > event.threshold


def test_pair_position_add_and_exit_never_reverses():
    pair = PairPosition()
    pair.add(PairDirection.SELL_ENTROPY, 2.0, pair_id="ARB-POS-1", at=1.0)
    pair.add(PairDirection.SELL_ENTROPY, 1.0)
    assert pair.is_open and pair.base_qty == 3.0
    assert pair.pair_id == "ARB-POS-1"
    assert SignalAction.EXIT.value == "EXIT"
    assert pair.reduce(2.5) == 2.5
    assert pair.base_qty == 0.5
    assert pair.reduce(10.0) == 0.5
    assert not pair.is_open and pair.direction is None


def test_pair_position_rejects_opposite_add():
    pair = PairPosition()
    pair.add(PairDirection.BUY_ENTROPY, 1.0)
    try:
        pair.add(PairDirection.SELL_ENTROPY, 1.0)
    except ValueError as exc:
        assert "opposite direction" in str(exc)
    else:
        raise AssertionError("opposite add must be rejected")
