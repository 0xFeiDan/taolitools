"""Configuration: strategy from a YAML file, credentials from .env, market
selection (symbol + hedge venue) from the command line.

The split is deliberate: config.yaml IS the strategy (thresholds, sizing,
risk) and is safe to share/commit as an example; .env holds only secrets;
which markets to trade is stated explicitly on every start (--symbol,
--hedge). Every YAML key is validated against the schema below, so a typo
is an error rather than a setting that silently does nothing.

Threshold model (fixed numbers the user derives from recorded minute data):

    price_basis=usd:
        premium_bps = (entropy_price * entropy_quote_usd
                       / (hedge_price * hedge_quote_usd) - 1) * 10_000
    price_basis=raw (legacy default):
        premium_bps = (entropy_price / hedge_price - 1) * 10_000

    SELL entropy / BUY hedge  fires when the executable premium
        (entropy bid over hedge ask) >= midline_bps + upper_bps
    BUY entropy / SELL hedge  fires when the executable premium
        (entropy ask under hedge bid) <= midline_bps - lower_bps

    Both directional hurdles include both venues' taker fees.  They define
    signal distance in ratio/bps space; they are not an unconditional USD
    round-trip profit guarantee because the common price level, fills,
    funding, and quote/USD rates can change between entry and exit.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

HL_API_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"   # official ws — the only HL feed used

HEDGE_VENUES = ("lighter", "lighter-rh", "tradexyz")
MAX_CONFIG_BPS = 100_000.0
MAX_CONFIG_USD = 1_000_000_000_000.0
MAX_CONFIG_SECONDS = 365.0 * 24.0 * 60.0 * 60.0


@dataclass(frozen=True)
class LighterProfile:
    name: str
    api_url: str
    ws_url: str
    chain_id: int


# Endpoint profiles for the two supported zkLighter deployments (these match
# lighter-python's lighter.endpoint_profiles, duplicated here so --record-only
# data collection works without the SDK installed).
LIGHTER_PROFILES: Dict[str, LighterProfile] = {
    "lighter": LighterProfile(
        "mainnet", "https://mainnet.zklighter.elliot.ai",
        "wss://mainnet.zklighter.elliot.ai/stream", 304),
    "lighter-rh": LighterProfile(
        "robinhood", "https://api.rh.lighter.xyz",
        "wss://api.rh.lighter.xyz/stream", 466324),
}


@dataclass
class LighterCreds:
    account_index: Optional[int]
    api_key_index: Optional[int]
    api_private_key: Optional[str]

    @property
    def complete(self) -> bool:
        return (self.account_index is not None and self.api_key_index is not None
                and bool(self.api_private_key))


@dataclass
class HLCreds:
    private_key: Optional[str]
    account_address: Optional[str]

    @property
    def complete(self) -> bool:
        return bool(self.private_key)


@dataclass
class VenueConf:
    key: str                  # "entropy" | "hedge"
    kind: str                 # "hl" | "lighter"
    label: str                # human name for logs, e.g. "ENTROPY", "RH"
    symbol: str
    fee_bps: float
    cap_usd: float
    orders_per_min: int
    quote_asset: str = "USD"
    # hl
    hl_dex: str = ""
    hl_creds: Optional[HLCreds] = None
    # lighter
    lighter_profile: Optional[LighterProfile] = None
    lighter_creds: Optional[LighterCreds] = None


@dataclass(frozen=True)
class MidlineConfig:
    """V2 midline contract.

    ``thresholds.midline_bps`` remains the static value and warm-up display
    seed. Dynamic trading fails closed until the estimator is ready.
    """

    mode: str
    fast_method: str
    fast_window_seconds: float
    slow_method: str
    slow_window_seconds: float
    min_samples: int
    volatility_method: str
    volatility_window_seconds: float
    volatility_floor_bps: float
    entry_z_score: float
    exit_z_score: float


@dataclass(frozen=True)
class RegimeConfig:
    enabled: bool
    max_fast_slow_difference_bps: float
    max_z_score: float
    max_absolute_spread_bps: float
    break_persist_seconds: float
    recovery_persist_seconds: float


@dataclass(frozen=True)
class MarketDataConfig:
    enforce_book_age: bool
    max_book_age_ms: float


@dataclass(frozen=True)
class VwapSizingConfig:
    enabled: bool
    min_order_usd: float
    max_order_usd: float
    minimum_net_edge_bps: float
    max_vwap_slippage_bps: float
    max_book_impact_bps: float
    safety_buffer_bps: float
    expected_latency_cost_bps: float


@dataclass(frozen=True)
class ExecutionRiskConfig:
    enabled: bool
    hedge_timeout_ms: float
    max_unhedged_delta_usd: float


@dataclass(frozen=True)
class KillSwitchConfig:
    enabled: bool
    max_unhedged_duration_ms: float
    max_consecutive_partial_fills: int
    max_reconcile_mismatch_usd: float
    max_session_loss_usd: float
    emergency_flatten_enabled: bool
    emergency_flatten_retry_sec: float
    emergency_flatten_max_attempts: int


@dataclass(frozen=True)
class AccountingConfig:
    enabled: bool
    ledger_jsonl: str
    state_json: str


@dataclass(frozen=True)
class FundingConfig:
    enabled: bool
    expected_holding_hours: float
    refresh_seconds: float
    max_age_seconds: float


@dataclass(frozen=True)
class StablecoinConfig:
    enabled: bool
    provider: str
    source_url: str
    refresh_seconds: float
    max_age_seconds: float
    max_spread_bps: float
    warning_deviation_bps: float
    halt_deviation_bps: float


@dataclass(frozen=True)
class SessionConfig:
    """One-switch market-session contract.

    Disabled is one 24/7 crypto statistics pool. Enabled selects four
    independent stock-perpetual statistics regimes without gating trading.
    """

    enabled: bool


@dataclass
class Config:
    symbol: str
    hedge_venue: str
    entropy: VenueConf
    hedge: VenueConf
    # thresholds (the whole signal)
    midline_bps: float
    upper_bps: float
    lower_bps: float
    threshold_price_basis: str
    # sizing
    take_fraction: float
    max_order_notional: float
    min_order_notional: float
    # inventory ladder
    inventory_scale_bps: float
    inventory_floor_frac: float
    # execution
    premium_persist_sec: float
    cooldown_sec: float
    settle_timeout_sec: float
    leg_slippage_bps: float
    hedge_slippage_bps: float
    net_tolerance_base: float
    max_consecutive_errors: int
    rate_limit_pause_sec: float
    staleness_sec: float
    reconcile_sec: float
    venue_probe_sec: float
    http_keepalive_sec: float
    # recorder
    recorder_enabled: bool
    recorder_csv: str
    # logging
    log_level: str
    status_interval_sec: float
    trades_csv: str
    dashboard: bool
    log_file: str
    # V2 contracts.  All are opt-in so an existing config keeps V1 behavior.
    midline: MidlineConfig
    regime: RegimeConfig
    market_data: MarketDataConfig
    vwap_sizing: VwapSizingConfig
    execution_risk: ExecutionRiskConfig
    kill_switch: KillSwitchConfig
    accounting: AccountingConfig
    funding: FundingConfig
    stablecoin: StablecoinConfig
    session: SessionConfig
    # runtime
    hl_api_url: str = HL_API_URL
    hl_ws_url: str = HL_WS_URL

    @property
    def creds_complete(self) -> bool:
        for v in (self.entropy, self.hedge):
            if v.kind == "hl" and not (v.hl_creds and v.hl_creds.complete):
                return False
            if v.kind == "lighter" and not (v.lighter_creds
                                            and v.lighter_creds.complete):
                return False
        return True

    @property
    def pending_v2_features(self) -> tuple[str, ...]:
        """Compatibility hook for rejecting future config-only features.

        Every option currently exposed by the V2 schema is wired into the
        runtime, so the list is intentionally empty.
        """
        return ()

    def require_runtime_supported(self) -> None:
        pending = self.pending_v2_features
        if pending:
            raise ConfigError(
                "V2 features are configured but not active in the engine yet: "
                + ", ".join(pending)
                + ". Keep them disabled until their implementation phase / "
                  "这些 V2 功能目前只有配置契约，尚未接入引擎，请暂时关闭")


# ----------------------------------------------------------------- YAML layer

# Schema: nested dict of key -> type (or nested dict). Unknown keys are errors.
_SCHEMA: Dict[str, Any] = {
    "thresholds": {
        "midline_bps": float,
        "upper_bps": float,
        "lower_bps": float,
        "price_basis": str,
    },
    "midline": {
        "mode": str,
        "fast_method": str,
        "fast_window_seconds": float,
        "slow_method": str,
        "slow_window_seconds": float,
        "min_samples": int,
        "volatility_method": str,
        "volatility_window_seconds": float,
        "volatility_floor_bps": float,
        "entry_z_score": float,
        "exit_z_score": float,
    },
    "regime": {
        "enabled": bool,
        "max_fast_slow_difference_bps": float,
        "max_z_score": float,
        "max_absolute_spread_bps": float,
        "break_persist_seconds": float,
        "recovery_persist_seconds": float,
    },
    "market_data": {
        "enforce_book_age": bool,
        "max_book_age_ms": float,
    },
    "entropy": {
        "dex": str,
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
        "quote_asset": str,
    },
    "hedge": {
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
        "quote_asset": str,
    },
    "sizing": {
        "take_fraction": float,
        "max_order_notional_usd": float,
        "min_order_notional_usd": float,
        "vwap_enabled": bool,
        "min_order_usd": float,
        "max_order_usd": float,
        "minimum_net_edge_bps": float,
        "max_vwap_slippage_bps": float,
        "max_book_impact_bps": float,
        "safety_buffer_bps": float,
        "expected_latency_cost_bps": float,
    },
    "inventory": {
        "scale_bps": float,
        "floor_frac": float,
    },
    "execution": {
        "premium_persist_sec": float,
        "cooldown_sec": float,
        "settle_timeout_sec": float,
        "leg_slippage_bps": float,
        "hedge_slippage_bps": float,
        "net_tolerance_base": float,
        "max_consecutive_errors": int,
        "rate_limit_pause_sec": float,
        "staleness_sec": float,
        "reconcile_sec": float,
        "venue_probe_sec": float,
        "http_keepalive_sec": float,
        "risk_recovery_enabled": bool,
        "hedge_timeout_ms": float,
        "max_unhedged_delta_usd": float,
    },
    "kill_switch": {
        "enabled": bool,
        "max_unhedged_duration_ms": float,
        "max_consecutive_partial_fills": int,
        "max_reconcile_mismatch_usd": float,
        "max_session_loss_usd": float,
        "emergency_flatten_enabled": bool,
        "emergency_flatten_retry_sec": float,
        "emergency_flatten_max_attempts": int,
    },
    "accounting": {
        "enabled": bool,
        "ledger_jsonl": str,
        "state_json": str,
    },
    "funding": {
        "enabled": bool,
        "expected_holding_hours": float,
        "refresh_seconds": float,
        "max_age_seconds": float,
    },
    "stablecoin": {
        "enabled": bool,
        "provider": str,
        "source_url": str,
        "refresh_seconds": float,
        "max_age_seconds": float,
        "max_spread_bps": float,
        "warning_deviation_bps": float,
        "halt_deviation_bps": float,
    },
    "session": {
        "enabled": bool,
    },
    "recorder": {
        "enabled": bool,
        "csv": str,
    },
    "logging": {
        "level": str,
        "status_interval_sec": float,
        "trades_csv": str,
        "dashboard": bool,
        "file": str,
    },
}


class ConfigError(ValueError):
    pass


def _validate(node: Any, schema: Dict[str, Any], path: str = "") -> None:
    if not isinstance(node, dict):
        raise ConfigError(f"'{path or '<root>'}' must be a mapping")
    for key, val in node.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in schema:
            raise ConfigError(f"unknown config key '{here}' "
                              f"(valid: {', '.join(sorted(schema))})")
        want = schema[key]
        if isinstance(want, dict):
            _validate(val, want, here)
        elif want is float:
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be a number, got {val!r}")
            if not math.isfinite(float(val)):
                raise ConfigError(f"'{here}' must be finite, got {val!r}")
        elif want is int:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be an integer, got {val!r}")
        elif want is bool:
            if not isinstance(val, bool):
                raise ConfigError(f"'{here}' must be true/false, got {val!r}")
        elif want is str:
            if not isinstance(val, str):
                raise ConfigError(f"'{here}' must be a string, got {val!r}")


def _get(d: dict, section: str, key: str, default):
    return (d.get(section) or {}).get(key, default)


def _choice(value: str, valid: tuple[str, ...], path: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in valid:
        raise ConfigError(f"'{path}' must be one of {list(valid)}, got "
                          f"{value!r}")
    return normalized


def _positive(value: Any, path: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ConfigError(f"'{path}' must be > 0, got {value!r}")
    return result


def _nonnegative(value: Any, path: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ConfigError(f"'{path}' must be >= 0, got {value!r}")
    return result


def _less_than(value: Any, ceiling: float, path: str) -> float:
    result = _nonnegative(value, path)
    if result >= ceiling:
        raise ConfigError(f"'{path}' must be < {ceiling:g}, got {value!r}")
    return result


def _at_most(value: Any, ceiling: float, path: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result > ceiling:
        raise ConfigError(f"'{path}' must be <= {ceiling:g}, got {value!r}")
    return result


# ------------------------------------------------------------------ env layer

def _env_s(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if v not in (None, "") else None


def _env_i(name: str) -> Optional[int]:
    v = os.getenv(name)
    if v in (None, ""):
        return None
    try:
        result = int(v)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a non-negative integer") from exc
    if result < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
    return result


# -------------------------------------------------------------------- loading

def load_config(config_file: str = "config.yaml", env_file: str = ".env", *,
                symbol: str, hedge_venue: str) -> Config:
    load_dotenv(env_file)
    try:
        with open(config_file, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"config file '{config_file}' not found — copy config.example.yaml "
            f"to config.yaml and edit it / 未找到配置文件，请先复制 "
            f"config.example.yaml 为 config.yaml 并修改")
    _validate(raw, _SCHEMA)

    symbol = (symbol or "").strip()
    if not symbol:
        raise ConfigError("--symbol is required, e.g. --symbol SNDK / "
                          "必须用 --symbol 指定交易品种")
    if hedge_venue not in HEDGE_VENUES:
        raise ConfigError(
            f"--hedge must be one of {list(HEDGE_VENUES)}, got "
            f"{hedge_venue!r} / --hedge 必须是 {list(HEDGE_VENUES)} 之一")

    thr = raw.get("thresholds") or {}
    for k in ("midline_bps", "upper_bps", "lower_bps"):
        if k not in thr:
            raise ConfigError(f"'thresholds.{k}' is required — derive it from "
                              f"recorded minute data / 必须填写，请用采集的分钟"
                              f"数据计算后填入")
    upper = _positive(thr["upper_bps"], "thresholds.upper_bps")
    lower = _positive(thr["lower_bps"], "thresholds.lower_bps")
    threshold_price_basis = _choice(
        thr.get("price_basis", "raw"), ("raw", "usd"),
        "thresholds.price_basis")
    _at_most(upper, MAX_CONFIG_BPS, "thresholds.upper_bps")
    _at_most(lower, MAX_CONFIG_BPS, "thresholds.lower_bps")
    static_midline = float(thr["midline_bps"])
    if abs(static_midline) > MAX_CONFIG_BPS:
        raise ConfigError(f"'thresholds.midline_bps' must be within "
                          f"+/-{MAX_CONFIG_BPS:g}")
    if static_midline <= -10000.0:
        raise ConfigError("'thresholds.midline_bps' must be > -10000 so it "
                          "represents a positive price ratio")
    if static_midline - lower <= -10000.0:
        raise ConfigError("'thresholds.midline_bps - lower_bps' must be "
                          "> -10000 so the lower price-ratio boundary is "
                          "positive")

    take_fraction = float(_get(raw, "sizing", "take_fraction", 0.5))
    if not 0.0 < take_fraction <= 1.0:
        raise ConfigError("sizing.take_fraction must be in (0, 1] — taking "
                          "more than the profitable depth loses money on the "
                          "tail / 必须在 (0, 1] 之间")

    max_order_notional = _positive(
        _get(raw, "sizing", "max_order_notional_usd", 500.0),
        "sizing.max_order_notional_usd")
    _at_most(max_order_notional, MAX_CONFIG_USD,
             "sizing.max_order_notional_usd")
    min_order_notional = _positive(
        _get(raw, "sizing", "min_order_notional_usd", 10.0),
        "sizing.min_order_notional_usd")
    _at_most(min_order_notional, MAX_CONFIG_USD,
             "sizing.min_order_notional_usd")
    if min_order_notional > max_order_notional:
        raise ConfigError("sizing.min_order_notional_usd must be <= "
                          "max_order_notional_usd")
    inventory_scale_bps = _nonnegative(
        _get(raw, "inventory", "scale_bps", 10.0),
        "inventory.scale_bps")
    inventory_floor_frac = _nonnegative(
        _get(raw, "inventory", "floor_frac", 0.5),
        "inventory.floor_frac")
    if inventory_floor_frac > 1.0:
        raise ConfigError("'inventory.floor_frac' must be <= 1")

    premium_persist_sec = _nonnegative(
        _get(raw, "execution", "premium_persist_sec", 0.3),
        "execution.premium_persist_sec")
    cooldown_sec = _nonnegative(
        _get(raw, "execution", "cooldown_sec", 0.0),
        "execution.cooldown_sec")
    settle_timeout_sec = _positive(
        _get(raw, "execution", "settle_timeout_sec", 5.0),
        "execution.settle_timeout_sec")
    leg_slippage_bps = _less_than(
        _get(raw, "execution", "leg_slippage_bps", 50.0), 10000.0,
        "execution.leg_slippage_bps")
    hedge_slippage_bps = _less_than(
        _get(raw, "execution", "hedge_slippage_bps", 20.0), 10000.0,
        "execution.hedge_slippage_bps")
    net_tolerance_base = _positive(
        _get(raw, "execution", "net_tolerance_base", 0.001),
        "execution.net_tolerance_base")
    max_consecutive_errors = int(_get(
        raw, "execution", "max_consecutive_errors", 3))
    if max_consecutive_errors <= 0:
        raise ConfigError("'execution.max_consecutive_errors' must be > 0")
    rate_limit_pause_sec = _nonnegative(
        _get(raw, "execution", "rate_limit_pause_sec", 10.0),
        "execution.rate_limit_pause_sec")
    staleness_sec = _positive(
        _get(raw, "execution", "staleness_sec", 10.0),
        "execution.staleness_sec")
    _at_most(staleness_sec, MAX_CONFIG_SECONDS,
             "execution.staleness_sec")
    reconcile_sec = _positive(
        _get(raw, "execution", "reconcile_sec", 15.0),
        "execution.reconcile_sec")
    venue_probe_sec = _positive(
        _get(raw, "execution", "venue_probe_sec", 30.0),
        "execution.venue_probe_sec")
    http_keepalive_sec = _nonnegative(
        _get(raw, "execution", "http_keepalive_sec", 10.0),
        "execution.http_keepalive_sec")
    status_interval_sec = _positive(
        _get(raw, "logging", "status_interval_sec", 30.0),
        "logging.status_interval_sec")

    midline = MidlineConfig(
        mode=_choice(_get(raw, "midline", "mode", "static"),
                     ("static", "dynamic"), "midline.mode"),
        fast_method=_choice(_get(raw, "midline", "fast_method", "ema"),
                            ("ema",), "midline.fast_method"),
        fast_window_seconds=_positive(
            _get(raw, "midline", "fast_window_seconds", 300.0),
            "midline.fast_window_seconds"),
        slow_method=_choice(_get(raw, "midline", "slow_method", "median"),
                            ("median",), "midline.slow_method"),
        slow_window_seconds=_positive(
            _get(raw, "midline", "slow_window_seconds", 1800.0),
            "midline.slow_window_seconds"),
        min_samples=int(_get(raw, "midline", "min_samples", 300)),
        volatility_method=_choice(
            _get(raw, "midline", "volatility_method", "std"),
            ("std", "mad"), "midline.volatility_method"),
        volatility_window_seconds=_positive(
            _get(raw, "midline", "volatility_window_seconds", 1800.0),
            "midline.volatility_window_seconds"),
        volatility_floor_bps=_positive(
            _get(raw, "midline", "volatility_floor_bps", 0.1),
            "midline.volatility_floor_bps"),
        entry_z_score=_positive(
            _get(raw, "midline", "entry_z_score", 2.5),
            "midline.entry_z_score"),
        exit_z_score=_nonnegative(
            _get(raw, "midline", "exit_z_score", 0.5),
            "midline.exit_z_score"),
    )
    if midline.min_samples <= 0:
        raise ConfigError("'midline.min_samples' must be > 0")
    if midline.slow_window_seconds < midline.fast_window_seconds:
        raise ConfigError("midline.slow_window_seconds must be >= "
                          "midline.fast_window_seconds")
    if midline.exit_z_score >= midline.entry_z_score:
        raise ConfigError("midline.exit_z_score must be < entry_z_score")
    sample_capacity = int(min(midline.slow_window_seconds,
                              midline.volatility_window_seconds)) + 1
    if midline.min_samples > sample_capacity:
        raise ConfigError(
            "midline.min_samples exceeds the 1Hz capacity of the shortest "
            "rolling window; increase the window or reduce min_samples")

    regime = RegimeConfig(
        enabled=bool(_get(raw, "regime", "enabled", False)),
        max_fast_slow_difference_bps=_positive(
            _get(raw, "regime", "max_fast_slow_difference_bps", 8.0),
            "regime.max_fast_slow_difference_bps"),
        max_z_score=_positive(_get(raw, "regime", "max_z_score", 5.0),
                              "regime.max_z_score"),
        max_absolute_spread_bps=_positive(
            _get(raw, "regime", "max_absolute_spread_bps", 50.0),
            "regime.max_absolute_spread_bps"),
        break_persist_seconds=_nonnegative(
            _get(raw, "regime", "break_persist_seconds", 1.0),
            "regime.break_persist_seconds"),
        recovery_persist_seconds=_nonnegative(
            _get(raw, "regime", "recovery_persist_seconds", 30.0),
            "regime.recovery_persist_seconds"),
    )

    market_data = MarketDataConfig(
        enforce_book_age=bool(_get(raw, "market_data", "enforce_book_age", False)),
        max_book_age_ms=_positive(
            _get(raw, "market_data", "max_book_age_ms", 300.0),
            "market_data.max_book_age_ms"),
    )

    vwap_sizing = VwapSizingConfig(
        enabled=bool(_get(raw, "sizing", "vwap_enabled", False)),
        min_order_usd=_positive(_get(raw, "sizing", "min_order_usd", 1000.0),
                                "sizing.min_order_usd"),
        max_order_usd=_positive(_get(raw, "sizing", "max_order_usd", 50000.0),
                                "sizing.max_order_usd"),
        minimum_net_edge_bps=_nonnegative(
            _get(raw, "sizing", "minimum_net_edge_bps", 6.0),
            "sizing.minimum_net_edge_bps"),
        max_vwap_slippage_bps=_nonnegative(
            _get(raw, "sizing", "max_vwap_slippage_bps", 5.0),
            "sizing.max_vwap_slippage_bps"),
        max_book_impact_bps=_nonnegative(
            _get(raw, "sizing", "max_book_impact_bps", 5.0),
            "sizing.max_book_impact_bps"),
        safety_buffer_bps=_nonnegative(
            _get(raw, "sizing", "safety_buffer_bps", 2.0),
            "sizing.safety_buffer_bps"),
        expected_latency_cost_bps=_nonnegative(
            _get(raw, "sizing", "expected_latency_cost_bps", 0.0),
            "sizing.expected_latency_cost_bps"),
    )
    if vwap_sizing.min_order_usd > vwap_sizing.max_order_usd:
        raise ConfigError("sizing.min_order_usd must be <= sizing.max_order_usd")
    _at_most(vwap_sizing.min_order_usd, MAX_CONFIG_USD,
             "sizing.min_order_usd")
    _at_most(vwap_sizing.max_order_usd, MAX_CONFIG_USD,
             "sizing.max_order_usd")

    execution_risk = ExecutionRiskConfig(
        enabled=bool(_get(raw, "execution", "risk_recovery_enabled", False)),
        hedge_timeout_ms=_positive(
            _get(raw, "execution", "hedge_timeout_ms", 250.0),
            "execution.hedge_timeout_ms"),
        max_unhedged_delta_usd=_positive(
            _get(raw, "execution", "max_unhedged_delta_usd", 5000.0),
            "execution.max_unhedged_delta_usd"),
    )

    kill_switch = KillSwitchConfig(
        enabled=bool(_get(raw, "kill_switch", "enabled", False)),
        max_unhedged_duration_ms=_positive(
            _get(raw, "kill_switch", "max_unhedged_duration_ms", 1000.0),
            "kill_switch.max_unhedged_duration_ms"),
        max_consecutive_partial_fills=int(_get(
            raw, "kill_switch", "max_consecutive_partial_fills", 3)),
        max_reconcile_mismatch_usd=_positive(
            _get(raw, "kill_switch", "max_reconcile_mismatch_usd", 1000.0),
            "kill_switch.max_reconcile_mismatch_usd"),
        max_session_loss_usd=_nonnegative(
            _get(raw, "kill_switch", "max_session_loss_usd", 0.0),
            "kill_switch.max_session_loss_usd"),
        emergency_flatten_enabled=bool(_get(
            raw, "kill_switch", "emergency_flatten_enabled", False)),
        emergency_flatten_retry_sec=_positive(_get(
            raw, "kill_switch", "emergency_flatten_retry_sec", 2.0),
            "kill_switch.emergency_flatten_retry_sec"),
        emergency_flatten_max_attempts=int(_get(
            raw, "kill_switch", "emergency_flatten_max_attempts", 0)),
    )
    if kill_switch.max_consecutive_partial_fills <= 0:
        raise ConfigError(
            "'kill_switch.max_consecutive_partial_fills' must be > 0")
    if kill_switch.emergency_flatten_max_attempts < 0:
        raise ConfigError(
            "'kill_switch.emergency_flatten_max_attempts' must be >= 0")

    accounting = AccountingConfig(
        enabled=bool(_get(raw, "accounting", "enabled", False)),
        ledger_jsonl=str(_get(
            raw, "accounting", "ledger_jsonl", "logs/pair-ledger.jsonl")),
        state_json=str(_get(
            raw, "accounting", "state_json", "logs/runtime-state.json")),
    )
    if accounting.enabled and (not accounting.ledger_jsonl
                               or not accounting.state_json):
        raise ConfigError("accounting paths must not be empty when enabled")
    if (accounting.enabled
            and os.path.abspath(accounting.ledger_jsonl)
            == os.path.abspath(accounting.state_json)):
        raise ConfigError("accounting.ledger_jsonl and accounting.state_json "
                          "must be different files")

    funding = FundingConfig(
        enabled=bool(_get(raw, "funding", "enabled", False)),
        expected_holding_hours=_positive(_get(
            raw, "funding", "expected_holding_hours", 1.0),
            "funding.expected_holding_hours"),
        refresh_seconds=_positive(_get(
            raw, "funding", "refresh_seconds", 60.0),
            "funding.refresh_seconds"),
        max_age_seconds=_positive(_get(
            raw, "funding", "max_age_seconds", 180.0),
            "funding.max_age_seconds"),
    )
    stablecoin = StablecoinConfig(
        enabled=bool(_get(raw, "stablecoin", "enabled", False)),
        provider=str(_get(
            raw, "stablecoin", "provider", "kraken")).strip().lower(),
        source_url=str(_get(
            raw, "stablecoin", "source_url",
            "https://api.kraken.com")).strip().rstrip("/"),
        refresh_seconds=_positive(_get(
            raw, "stablecoin", "refresh_seconds", 30.0),
            "stablecoin.refresh_seconds"),
        max_age_seconds=_positive(_get(
            raw, "stablecoin", "max_age_seconds", 90.0),
            "stablecoin.max_age_seconds"),
        max_spread_bps=_positive(_get(
            raw, "stablecoin", "max_spread_bps", 10.0),
            "stablecoin.max_spread_bps"),
        warning_deviation_bps=_nonnegative(_get(
            raw, "stablecoin", "warning_deviation_bps", 10.0),
            "stablecoin.warning_deviation_bps"),
        halt_deviation_bps=_positive(_get(
            raw, "stablecoin", "halt_deviation_bps", 30.0),
            "stablecoin.halt_deviation_bps"),
    )
    if stablecoin.warning_deviation_bps > stablecoin.halt_deviation_bps:
        raise ConfigError("stablecoin.warning_deviation_bps must be <= "
                          "halt_deviation_bps")
    if stablecoin.enabled and not stablecoin.source_url.strip():
        raise ConfigError("stablecoin.source_url must not be empty when enabled")
    if stablecoin.provider != "kraken":
        raise ConfigError("stablecoin.provider must be 'kraken'")
    if (stablecoin.enabled
            and stablecoin.source_url != "https://api.kraken.com"):
        raise ConfigError("stablecoin.source_url must be "
                          "https://api.kraken.com for provider 'kraken'")
    if (funding.enabled or stablecoin.enabled) and not vwap_sizing.enabled:
        raise ConfigError("funding/stablecoin cost modeling requires "
                          "sizing.vwap_enabled: true")
    if threshold_price_basis == "usd" and not stablecoin.enabled:
        raise ConfigError("thresholds.price_basis: usd requires "
                          "stablecoin.enabled: true")

    for value, path in (
        (premium_persist_sec, "execution.premium_persist_sec"),
        (cooldown_sec, "execution.cooldown_sec"),
        (settle_timeout_sec, "execution.settle_timeout_sec"),
        (rate_limit_pause_sec, "execution.rate_limit_pause_sec"),
        (reconcile_sec, "execution.reconcile_sec"),
        (venue_probe_sec, "execution.venue_probe_sec"),
        (http_keepalive_sec, "execution.http_keepalive_sec"),
        (status_interval_sec, "logging.status_interval_sec"),
        (funding.expected_holding_hours, "funding.expected_holding_hours"),
        (funding.refresh_seconds, "funding.refresh_seconds"),
        (funding.max_age_seconds, "funding.max_age_seconds"),
        (stablecoin.refresh_seconds, "stablecoin.refresh_seconds"),
        (stablecoin.max_age_seconds, "stablecoin.max_age_seconds"),
    ):
        _at_most(value, MAX_CONFIG_SECONDS, path)
    _at_most(stablecoin.max_spread_bps, MAX_CONFIG_BPS,
             "stablecoin.max_spread_bps")
    for value, path in (
        (execution_risk.max_unhedged_delta_usd,
         "execution.max_unhedged_delta_usd"),
        (kill_switch.max_reconcile_mismatch_usd,
         "kill_switch.max_reconcile_mismatch_usd"),
        (kill_switch.max_session_loss_usd,
         "kill_switch.max_session_loss_usd"),
    ):
        _at_most(value, MAX_CONFIG_USD, path)

    session = SessionConfig(
        enabled=bool(_get(raw, "session", "enabled", False)))

    entropy_dex = _get(raw, "entropy", "dex", "io")
    if entropy_dex != "io":
        raise ConfigError("'entropy.dex' must be 'io' for the fixed Entropy "
                          "leg")
    if hedge_venue == "tradexyz" and entropy_dex == "xyz":
        raise ConfigError("entropy.dex 'xyz' with hedge_venue 'tradexyz' is "
                          "the same market on both legs / 两条腿是同一个市场")

    entropy_hl_creds = HLCreds(_env_s("HL_PRIVATE_KEY"),
                               _env_s("HL_ACCOUNT_ADDRESS"))
    entropy_fee_bps = _less_than(
        _get(raw, "entropy", "taker_fee_bps", 0.0), 10000.0,
        "entropy.taker_fee_bps")
    entropy_cap_usd = _positive(
        _get(raw, "entropy", "max_position_usd", 1000.0),
        "entropy.max_position_usd")
    _at_most(entropy_cap_usd, MAX_CONFIG_USD,
             "entropy.max_position_usd")
    entropy_orders_per_min = int(_get(
        raw, "entropy", "max_orders_per_min", 120))
    if entropy_orders_per_min <= 0:
        raise ConfigError("'entropy.max_orders_per_min' must be > 0")
    hedge_fee_bps = _less_than(
        _get(raw, "hedge", "taker_fee_bps",
             1.0 if hedge_venue == "tradexyz" else 0.0),
        10000.0, "hedge.taker_fee_bps")
    hedge_cap_usd = _positive(
        _get(raw, "hedge", "max_position_usd", 1000.0),
        "hedge.max_position_usd")
    _at_most(hedge_cap_usd, MAX_CONFIG_USD,
             "hedge.max_position_usd")
    hedge_orders_per_min = int(_get(
        raw, "hedge", "max_orders_per_min",
        120 if hedge_venue == "tradexyz" else 30))
    if hedge_orders_per_min <= 0:
        raise ConfigError("'hedge.max_orders_per_min' must be > 0")
    entropy = VenueConf(
        key="entropy", kind="hl", label="ENTROPY",
        symbol=symbol,
        fee_bps=entropy_fee_bps,
        cap_usd=entropy_cap_usd,
        orders_per_min=entropy_orders_per_min,
        quote_asset=str(_get(raw, "entropy", "quote_asset", "USDC")).upper(),
        hl_dex=entropy_dex,
        hl_creds=entropy_hl_creds,
    )

    if hedge_venue == "tradexyz":
        hedge = VenueConf(
            key="hedge", kind="hl", label="XYZ",
            symbol=symbol,
            fee_bps=hedge_fee_bps,
            cap_usd=hedge_cap_usd,
            orders_per_min=hedge_orders_per_min,
            quote_asset=str(_get(raw, "hedge", "quote_asset", "USDC")).upper(),
            hl_dex="xyz",
            hl_creds=HLCreds(
                _env_s("HL_PRIVATE_KEY_XYZ") or _env_s("HL_PRIVATE_KEY"),
                _env_s("HL_ACCOUNT_ADDRESS_XYZ") or _env_s("HL_ACCOUNT_ADDRESS")),
        )
    else:
        hedge = VenueConf(
            key="hedge", kind="lighter",
            label="LIGHTER" if hedge_venue == "lighter" else "RH",
            symbol=symbol,
            fee_bps=hedge_fee_bps,
            cap_usd=hedge_cap_usd,
            orders_per_min=hedge_orders_per_min,
            quote_asset=str(_get(
                raw, "hedge", "quote_asset",
                "USDG" if hedge_venue == "lighter-rh" else "USDC")).upper(),
            lighter_profile=LIGHTER_PROFILES[hedge_venue],
            lighter_creds=LighterCreds(_env_i("LIGHTER_ACCOUNT_INDEX"),
                                       _env_i("LIGHTER_API_KEY_INDEX"),
                                       _env_s("LIGHTER_API_PRIVATE_KEY")),
        )
    if not entropy.quote_asset.strip() or not hedge.quote_asset.strip():
        raise ConfigError("venue quote_asset must not be empty")
    for venue in (entropy, hedge):
        if not re.fullmatch(r"[A-Z0-9]{2,16}", venue.quote_asset):
            raise ConfigError("venue quote_asset must contain only 2-16 "
                              "uppercase letters or digits")

    return Config(
        symbol=symbol,
        hedge_venue=hedge_venue,
        entropy=entropy,
        hedge=hedge,
        midline_bps=static_midline,
        upper_bps=upper,
        lower_bps=lower,
        threshold_price_basis=threshold_price_basis,
        take_fraction=take_fraction,
        max_order_notional=max_order_notional,
        min_order_notional=min_order_notional,
        inventory_scale_bps=inventory_scale_bps,
        inventory_floor_frac=inventory_floor_frac,
        premium_persist_sec=premium_persist_sec,
        cooldown_sec=cooldown_sec,
        settle_timeout_sec=settle_timeout_sec,
        leg_slippage_bps=leg_slippage_bps,
        hedge_slippage_bps=hedge_slippage_bps,
        net_tolerance_base=net_tolerance_base,
        max_consecutive_errors=max_consecutive_errors,
        rate_limit_pause_sec=rate_limit_pause_sec,
        staleness_sec=staleness_sec,
        reconcile_sec=reconcile_sec,
        venue_probe_sec=venue_probe_sec,
        http_keepalive_sec=http_keepalive_sec,
        recorder_enabled=bool(_get(raw, "recorder", "enabled", True)),
        recorder_csv=_get(raw, "recorder", "csv", "logs/minutes.csv"),
        log_level=str(_get(raw, "logging", "level", "INFO")).upper(),
        status_interval_sec=status_interval_sec,
        trades_csv=_get(raw, "logging", "trades_csv", "logs/trades.csv"),
        dashboard=bool(_get(raw, "logging", "dashboard", True)),
        log_file=_get(raw, "logging", "file", "logs/engine.log"),
        midline=midline,
        regime=regime,
        market_data=market_data,
        vwap_sizing=vwap_sizing,
        execution_risk=execution_risk,
        kill_switch=kill_switch,
        accounting=accounting,
        funding=funding,
        stablecoin=stablecoin,
        session=session,
    )
