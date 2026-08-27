"""V2 domain models shared by strategy, execution, risk, and accounting.

This module is deliberately transport-agnostic: venue adapters keep their
current responsibilities, while later V2 phases attach market observations,
pair execution state, risk actions, and latency measurements to these models.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class PairDirection(str, Enum):
    SELL_ENTROPY = "sell_entropy"
    BUY_ENTROPY = "buy_entropy"


class SignalAction(str, Enum):
    OPEN = "OPEN"
    ADD = "ADD"
    EXIT = "EXIT"


class ExecutionState(str, Enum):
    NEW = "NEW"
    SIGNAL_CONFIRMED = "SIGNAL_CONFIRMED"
    ORDERS_SENT = "ORDERS_SENT"
    PARTIAL = "PARTIAL"
    BOTH_FILLED = "BOTH_FILLED"
    RECOVERY = "RECOVERY"
    HEDGED = "HEDGED"
    UNWINDING = "UNWINDING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in (ExecutionState.COMPLETE, ExecutionState.FAILED)


class RiskAction(str, Enum):
    NONE = "NONE"
    PAUSE_NEW_ENTRY = "PAUSE_NEW_ENTRY"
    EMERGENCY_HEDGE = "EMERGENCY_HEDGE"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"


@dataclass(frozen=True)
class MarketDataTiming:
    """Timestamps for one venue book observation, in epoch seconds."""

    local_receive_ts: float
    exchange_ts: Optional[float] = None

    def book_age_ms(self, now: Optional[float] = None) -> float:
        current = time.time() if now is None else now
        return max((current - self.local_receive_ts) * 1000.0, 0.0)

    def transport_latency_ms(self) -> Optional[float]:
        if self.exchange_ts is None:
            return None
        return max((self.local_receive_ts - self.exchange_ts) * 1000.0, 0.0)


@dataclass
class LatencyTimeline:
    market_data_receive_ts: Optional[float] = None
    signal_generated_ts: Optional[float] = None
    order_send_ts: Optional[float] = None
    order_ack_ts: Optional[float] = None
    first_fill_ts: Optional[float] = None
    final_fill_ts: Optional[float] = None
    leg_a_final_fill_ts: Optional[float] = None
    leg_b_final_fill_ts: Optional[float] = None

    def mark(self, name: str, at: Optional[float] = None) -> None:
        if name not in self.__dataclass_fields__:
            raise ValueError(f"unknown latency timestamp {name!r}")
        setattr(self, name, time.time() if at is None else float(at))

    @staticmethod
    def _elapsed_ms(start: Optional[float], end: Optional[float]) -> Optional[float]:
        if start is None or end is None or end < start:
            return None
        return (end - start) * 1000.0

    def metrics_ms(self) -> Dict[str, Optional[float]]:
        leg_gap = None
        if self.leg_a_final_fill_ts is not None \
                and self.leg_b_final_fill_ts is not None:
            leg_gap = abs(self.leg_a_final_fill_ts
                          - self.leg_b_final_fill_ts) * 1000.0
        return {
            "market_to_signal_ms": self._elapsed_ms(
                self.market_data_receive_ts, self.signal_generated_ts),
            "signal_to_send_ms": self._elapsed_ms(
                self.signal_generated_ts, self.order_send_ts),
            "send_to_ack_ms": self._elapsed_ms(
                self.order_send_ts, self.order_ack_ts),
            "ack_to_fill_ms": self._elapsed_ms(
                self.order_ack_ts, self.first_fill_ts),
            "order_to_final_fill_ms": self._elapsed_ms(
                self.order_send_ts, self.final_fill_ts),
            "leg_fill_gap_ms": leg_gap,
        }


@dataclass(frozen=True)
class PairStateEvent:
    at: float
    from_state: Optional[ExecutionState]
    to_state: ExecutionState
    reason: str
    data: Dict[str, Any] = field(default_factory=dict)


_ALLOWED_TRANSITIONS = {
    ExecutionState.NEW: {
        ExecutionState.SIGNAL_CONFIRMED, ExecutionState.FAILED},
    ExecutionState.SIGNAL_CONFIRMED: {
        ExecutionState.ORDERS_SENT, ExecutionState.FAILED},
    ExecutionState.ORDERS_SENT: {
        ExecutionState.PARTIAL, ExecutionState.BOTH_FILLED,
        ExecutionState.RECOVERY, ExecutionState.FAILED},
    ExecutionState.PARTIAL: {
        ExecutionState.RECOVERY, ExecutionState.FAILED},
    ExecutionState.BOTH_FILLED: {
        ExecutionState.HEDGED, ExecutionState.COMPLETE,
        ExecutionState.RECOVERY},
    ExecutionState.RECOVERY: {
        ExecutionState.HEDGED, ExecutionState.UNWINDING,
        ExecutionState.FAILED},
    ExecutionState.HEDGED: {
        ExecutionState.COMPLETE, ExecutionState.RECOVERY},
    ExecutionState.UNWINDING: {
        ExecutionState.COMPLETE, ExecutionState.FAILED},
    ExecutionState.COMPLETE: set(),
    ExecutionState.FAILED: set(),
}


def make_pair_id(now: Optional[datetime] = None,
                 token: Optional[str] = None) -> str:
    instant = now or datetime.now(timezone.utc)
    suffix = (token or uuid.uuid4().hex[:8]).upper()
    return f"ARB-{instant.astimezone(timezone.utc):%Y%m%d-%H%M%S}-{suffix}"


@dataclass
class PairPosition:
    """Minimal matched two-leg strategy position used by Z-score signals.

    This is not the execution state machine. It only prevents an EXIT signal
    from trading more base than the currently matched pair exposure.
    """

    pair_id: Optional[str] = None
    direction: Optional[PairDirection] = None
    base_qty: float = 0.0
    opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return self.direction is not None and self.base_qty > 0

    def add(self, direction: PairDirection, qty: float, *,
            pair_id: Optional[str] = None, at: Optional[float] = None) -> None:
        qty = float(qty)
        if qty <= 0:
            return
        direction = PairDirection(direction)
        if self.is_open and self.direction is not direction:
            raise ValueError("cannot add the opposite direction to an open pair")
        if not self.is_open:
            self.pair_id = pair_id or make_pair_id()
            self.direction = direction
            self.base_qty = 0.0
            self.opened_at = time.time() if at is None else float(at)
        self.base_qty += qty

    def reduce(self, qty: float) -> float:
        if not self.is_open or qty <= 0:
            return 0.0
        reduced = min(float(qty), self.base_qty)
        self.base_qty -= reduced
        if self.base_qty <= 1e-12:
            self.pair_id = None
            self.direction = None
            self.base_qty = 0.0
            self.opened_at = None
        return reduced

    def sync(self, direction: Optional[PairDirection], qty: float, *,
             pair_id: Optional[str] = None, at: Optional[float] = None) -> None:
        self.pair_id = None
        self.direction = None
        self.base_qty = 0.0
        self.opened_at = None
        if direction is not None and qty > 0:
            self.add(direction, qty, pair_id=pair_id, at=at)


@dataclass
class PairExecution:
    pair_id: str
    symbol: str
    venue_a: str
    venue_b: str
    direction: PairDirection
    state: ExecutionState = ExecutionState.NEW
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    events: list[PairStateEvent] = field(default_factory=list)
    latency: LatencyTimeline = field(default_factory=LatencyTimeline)

    def __post_init__(self) -> None:
        if not self.pair_id:
            raise ValueError("pair_id must not be empty")
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if not self.events:
            self.events.append(PairStateEvent(
                at=self.created_at, from_state=None, to_state=self.state,
                reason="created"))

    @classmethod
    def new(cls, *, symbol: str, venue_a: str, venue_b: str,
            direction: PairDirection, pair_id: Optional[str] = None) -> "PairExecution":
        return cls(pair_id=pair_id or make_pair_id(), symbol=symbol,
                   venue_a=venue_a, venue_b=venue_b, direction=direction)

    def transition(self, to_state: ExecutionState, reason: str, *,
                   at: Optional[float] = None,
                   data: Optional[Dict[str, Any]] = None) -> PairStateEvent:
        if not isinstance(to_state, ExecutionState):
            to_state = ExecutionState(to_state)
        if to_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"invalid execution transition "
                             f"{self.state.value} -> {to_state.value}")
        changed_at = time.time() if at is None else float(at)
        event = PairStateEvent(
            at=changed_at, from_state=self.state, to_state=to_state,
            reason=reason, data=dict(data or {}))
        self.state = to_state
        self.updated_at = changed_at
        self.events.append(event)
        return event


@dataclass(frozen=True)
class RiskEvent:
    trigger: str
    action: RiskAction
    reason: str
    at: float = field(default_factory=time.time)
    pair_id: Optional[str] = None
    observed_value: Optional[float] = None
    threshold: Optional[float] = None
    data: Dict[str, Any] = field(default_factory=dict)
