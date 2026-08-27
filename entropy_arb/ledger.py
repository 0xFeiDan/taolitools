"""Append-only Pair PnL ledger plus atomic restart snapshot."""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


def _require_finite_tree(value: Any, path: str = "state") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeError(f"non-finite number in persisted {path}")
    if isinstance(value, dict):
        for key, item in value.items():
            _require_finite_tree(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_finite_tree(item, f"{path}[{index}]")


def _require_optional_finite(*values: Optional[float]) -> None:
    if not all(value is None or math.isfinite(float(value))
               for value in values):
        raise ValueError("optional accounting values must be finite")


@dataclass
class PairPnL:
    pair_id: str
    symbol: str
    venue_a: str
    venue_b: str
    direction: str
    entry_time: float
    exit_time: Optional[float] = None
    entry_spread: Optional[float] = None
    exit_spread: Optional[float] = None
    entry_z: Optional[float] = None
    exit_z: Optional[float] = None
    entry_midline: Optional[float] = None
    exit_midline: Optional[float] = None
    entry_session: Optional[str] = None
    exit_session: Optional[str] = None
    leg_a_entry_vwap: Optional[float] = None
    leg_b_entry_vwap: Optional[float] = None
    leg_a_exit_vwap: Optional[float] = None
    leg_b_exit_vwap: Optional[float] = None
    fees: float = 0.0
    expected_funding_cost: float = 0.0
    funding: float = 0.0
    funding_source: str = "pending"
    stablecoin_basis: float = 0.0
    stablecoin_basis_usd: float = 0.0
    entry_slippage: float = 0.0
    exit_slippage: float = 0.0
    recovery_pnl: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    holding_time: Optional[float] = None
    max_adverse_spread: float = 0.0
    max_favorable_spread: float = 0.0
    entry_base: float = 0.0
    exit_base: float = 0.0
    remaining_base: float = 0.0
    complete: bool = False
    accounting_complete: bool = True
    reconciliation_adjustment_base: float = 0.0
    _leg_a_entry_notional: float = field(default=0.0, repr=False)
    _leg_b_entry_notional: float = field(default=0.0, repr=False)
    _leg_a_exit_notional: float = field(default=0.0, repr=False)
    _leg_b_exit_notional: float = field(default=0.0, repr=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PairPnL":
        allowed = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items()
                      if key in allowed})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def apply_fill(self, *, action: str, qty: float, buy_key: str,
                   sell_key: str, buy_px: float, sell_px: float,
                   planned_buy_px: float, planned_sell_px: float,
                   buy_fee_rate: float, sell_fee_rate: float,
                   buy_quote_usd: float, sell_quote_usd: float,
                   spread_bps: Optional[float], z_score: Optional[float],
                   midline_bps: Optional[float], funding_cost_bps: float,
                   stablecoin_basis_bps: float,
                   market_session: Optional[str] = None,
                   at: Optional[float] = None) -> None:
        numeric = (qty, buy_px, sell_px, planned_buy_px, planned_sell_px,
                   buy_fee_rate, sell_fee_rate, buy_quote_usd,
                   sell_quote_usd, funding_cost_bps,
                   stablecoin_basis_bps)
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError("fill accounting values must be finite")
        _require_optional_finite(spread_bps, z_score, midline_bps, at)
        if (qty < 0 or buy_px <= 0 or sell_px <= 0
                or planned_buy_px <= 0 or planned_sell_px <= 0
                or buy_quote_usd <= 0 or sell_quote_usd <= 0
                or buy_fee_rate < 0 or sell_fee_rate < 0):
            raise ValueError("fill accounting values are out of range")
        if qty <= 0:
            return
        now = time.time() if at is None else at
        is_exit = action == "EXIT"
        buy_raw = qty * buy_px
        sell_raw = qty * sell_px
        buy_usd = buy_raw * buy_quote_usd
        sell_usd = sell_raw * sell_quote_usd
        raw_cashflow = sell_raw - buy_raw
        adjusted_cashflow = sell_usd - buy_usd
        fee = buy_usd * buy_fee_rate + sell_usd * sell_fee_rate
        slippage = ((buy_px - planned_buy_px) * qty * buy_quote_usd
                    + (planned_sell_px - sell_px) * qty * sell_quote_usd)

        self.gross_pnl += raw_cashflow
        self.stablecoin_basis_usd += adjusted_cashflow - raw_cashflow
        self.stablecoin_basis = stablecoin_basis_bps
        self.fees += fee
        if not is_exit:
            self.expected_funding_cost += (
                buy_usd * funding_cost_bps / 1e4)
            self.entry_base += qty
            self.remaining_base += qty
            self.entry_slippage += slippage
            if self.entry_spread is None:
                self.entry_spread = spread_bps
                self.entry_z = z_score
                self.entry_midline = midline_bps
                self.entry_session = market_session
            self._accumulate_vwap(buy_key, sell_key, qty, buy_px, sell_px,
                                  entry=True)
        else:
            self.exit_base += qty
            self.remaining_base = max(self.remaining_base - qty, 0.0)
            self.exit_slippage += slippage
            self.exit_spread = spread_bps
            self.exit_z = z_score
            self.exit_midline = midline_bps
            self.exit_session = market_session
            self._accumulate_vwap(buy_key, sell_key, qty, buy_px, sell_px,
                                  entry=False)
            if self.remaining_base <= 1e-12:
                self.complete = True
                self.exit_time = now
                self.holding_time = max(now - self.entry_time, 0.0)
                if self.funding_source == "pending":
                    self.funding = self.expected_funding_cost
                    self.funding_source = "estimated"
        self._recalculate_net()

    def apply_unpriced_fill(self, *, action: str, qty: float,
                            spread_bps: Optional[float],
                            z_score: Optional[float],
                            midline_bps: Optional[float],
                            market_session: Optional[str] = None,
                            at: Optional[float] = None) -> None:
        """Persist matched quantity without inventing an execution price."""
        qty = float(qty)
        if not math.isfinite(qty) or qty <= 0:
            raise ValueError("unpriced fill quantity must be finite and > 0")
        _require_optional_finite(spread_bps, z_score, midline_bps, at)
        now = time.time() if at is None else float(at)
        if not math.isfinite(now):
            raise ValueError("unpriced fill timestamp must be finite")
        self.accounting_complete = False
        if action == "EXIT":
            self.exit_base += qty
            self.remaining_base = max(self.remaining_base - qty, 0.0)
            self.exit_spread = spread_bps
            self.exit_z = z_score
            self.exit_midline = midline_bps
            self.exit_session = market_session
            if self.remaining_base <= 1e-12:
                self.complete = True
                self.exit_time = now
                self.holding_time = max(now - self.entry_time, 0.0)
        else:
            self.entry_base += qty
            self.remaining_base += qty
            if self.entry_spread is None:
                self.entry_spread = spread_bps
                self.entry_z = z_score
                self.entry_midline = midline_bps
                self.entry_session = market_session
        self._recalculate_net()

    def _accumulate_vwap(self, buy_key: str, sell_key: str, qty: float,
                         buy_px: float, sell_px: float, *, entry: bool) -> None:
        a_px = buy_px if buy_key == "entropy" else sell_px
        b_px = buy_px if buy_key == "hedge" else sell_px
        if entry:
            old_qty = max(self.entry_base - qty, 0.0)
            self._leg_a_entry_notional += a_px * qty
            self._leg_b_entry_notional += b_px * qty
            total = old_qty + qty
            self.leg_a_entry_vwap = self._leg_a_entry_notional / total
            self.leg_b_entry_vwap = self._leg_b_entry_notional / total
        else:
            old_qty = max(self.exit_base - qty, 0.0)
            self._leg_a_exit_notional += a_px * qty
            self._leg_b_exit_notional += b_px * qty
            total = old_qty + qty
            self.leg_a_exit_vwap = self._leg_a_exit_notional / total
            self.leg_b_exit_vwap = self._leg_b_exit_notional / total

    def update_market(self, spread_bps: float) -> None:
        if not math.isfinite(float(spread_bps)):
            raise ValueError("market spread must be finite")
        if self.entry_spread is None or self.complete:
            return
        move = (self.entry_spread - spread_bps
                if self.direction == "sell_entropy"
                else spread_bps - self.entry_spread)
        self.max_favorable_spread = max(self.max_favorable_spread, move)
        self.max_adverse_spread = max(self.max_adverse_spread, -move)

    def set_realized_funding(self, cost_usd: float) -> None:
        cost_usd = float(cost_usd)
        if not math.isfinite(cost_usd):
            raise ValueError("realized funding must be finite")
        self.funding = cost_usd
        self.funding_source = "venue"
        self._recalculate_net()

    def _recalculate_net(self) -> None:
        self.net_pnl = (self.gross_pnl + self.stablecoin_basis_usd
                        - self.fees - self.funding)


class PairLedger:
    VERSION = 1

    def __init__(self, ledger_jsonl: str, state_json: str) -> None:
        self.ledger_jsonl = ledger_jsonl
        self.state_json = state_json
        self.current: Optional[PairPnL] = None
        self.completed: list[PairPnL] = []
        self.runtime: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        temporary = self.state_json + ".tmp"
        if os.path.exists(temporary):
            raise RuntimeError(
                f"incomplete snapshot {temporary!r} exists; manual review "
                "is required before live restart")
        try:
            with open(self.state_json, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot restore runtime state "
                               f"{self.state_json!r}: {exc}") from exc
        _require_finite_tree(data)
        if data.get("version") != self.VERSION:
            raise RuntimeError("unsupported pair ledger state version")
        if data.get("current"):
            self.current = PairPnL.from_dict(data["current"])
        self.completed = [PairPnL.from_dict(item)
                          for item in data.get("completed", [])][-200:]
        self.runtime = dict(data.get("runtime") or {})

    def append_event(self, event_type: str, data: Dict[str, Any]) -> None:
        event = {"version": self.VERSION, "ts": time.time(),
                 "type": event_type, "data": data}
        directory = os.path.dirname(self.ledger_jsonl)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.ledger_jsonl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False,
                                separators=(",", ":"), allow_nan=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def ensure_pair(self, *, pair_id: str, symbol: str, venue_a: str,
                    venue_b: str, direction: str,
                    entry_time: Optional[float] = None) -> PairPnL:
        if self.current is not None:
            if self.current.pair_id != pair_id:
                raise RuntimeError("cannot open a second Pair while one is active")
            return self.current
        self.current = PairPnL(
            pair_id=pair_id, symbol=symbol, venue_a=venue_a, venue_b=venue_b,
            direction=direction,
            entry_time=time.time() if entry_time is None else entry_time)
        # State is the recovery source. Persist it before the audit append so
        # a power loss can never restore an older exposure merely because two
        # separate files cannot be atomically committed together.
        self.snapshot()
        self.append_event("PAIR_OPENED", self.current.to_dict())
        return self.current

    def record_fill(self, pair: PairPnL, fill: Dict[str, Any]) -> None:
        pair.apply_fill(**fill)
        if pair.complete:
            self.completed.append(pair)
            self.completed = self.completed[-200:]
            self.current = None
        self.snapshot()
        self.append_event("PAIR_FILL", {"pair_id": pair.pair_id, **fill})
        if pair.complete:
            self.append_event("PAIR_COMPLETED", pair.to_dict())

    def record_unpriced_fill(self, pair: PairPnL,
                             fill: Dict[str, Any]) -> None:
        pair.apply_unpriced_fill(**fill)
        if pair.complete:
            self.completed.append(pair)
            self.completed = self.completed[-200:]
            self.current = None
        self.snapshot()
        self.append_event("PAIR_UNPRICED_FILL", {
            "pair_id": pair.pair_id, **fill,
            "accounting_complete": False})
        if pair.complete:
            self.append_event("PAIR_COMPLETED", pair.to_dict())

    def reconcile_current(self, remaining_base: float, reason: str) -> None:
        if self.current is None:
            return
        pair = self.current
        remaining_base = float(remaining_base)
        if not math.isfinite(remaining_base):
            raise ValueError("reconciled quantity must be finite")
        new_remaining = max(remaining_base, 0.0)
        pair.reconciliation_adjustment_base += new_remaining - pair.remaining_base
        pair.remaining_base = new_remaining
        pair.accounting_complete = False
        if new_remaining <= 1e-12:
            pair.complete = True
            pair.exit_time = time.time()
            pair.holding_time = max(pair.exit_time - pair.entry_time, 0.0)
            if pair.funding_source == "pending":
                pair.funding = pair.expected_funding_cost
                pair.funding_source = "estimated"
            pair._recalculate_net()
            self.completed.append(pair)
            self.completed = self.completed[-200:]
            self.current = None
        self.snapshot()
        self.append_event("PAIR_RECONCILED", {
            "pair_id": pair.pair_id, "remaining_base": new_remaining,
            "reason": reason, "accounting_complete": False})

    def record_recovery(self, pair: PairPnL, *, gross_cashflow_usd: float,
                        fees_usd: float, reason: str,
                        complete_if_flat: bool = False) -> None:
        gross_cashflow_usd = float(gross_cashflow_usd)
        fees_usd = float(fees_usd)
        if (not math.isfinite(gross_cashflow_usd)
                or not math.isfinite(fees_usd) or fees_usd < 0):
            raise ValueError("recovery accounting values must be finite and "
                             "fees non-negative")
        net = gross_cashflow_usd - fees_usd
        pair.gross_pnl += gross_cashflow_usd
        pair.fees += fees_usd
        pair.recovery_pnl += net
        if complete_if_flat and pair.remaining_base <= 1e-12:
            pair.complete = True
            pair.exit_time = time.time()
            pair.holding_time = max(pair.exit_time - pair.entry_time, 0.0)
            if pair.funding_source == "pending":
                pair.funding = pair.expected_funding_cost
                pair.funding_source = "estimated"
            self.completed.append(pair)
            self.completed = self.completed[-200:]
            self.current = None
        pair._recalculate_net()
        self.snapshot()
        self.append_event("PAIR_RECOVERY", {
            "pair_id": pair.pair_id,
            "gross_cashflow_usd": gross_cashflow_usd,
            "fees_usd": fees_usd, "reason": reason,
            "complete": pair.complete})

    def snapshot(self, runtime: Optional[Dict[str, Any]] = None) -> None:
        if runtime is not None:
            self.runtime = runtime
        data = {
            "version": self.VERSION,
            "current": self.current.to_dict() if self.current else None,
            "completed": [pair.to_dict() for pair in self.completed[-200:]],
            "runtime": self.runtime,
        }
        directory = os.path.dirname(self.state_json)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = self.state_json + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, self.state_json)
