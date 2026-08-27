"""Config loading: example file, validation, CLI-selected markets.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
EXAMPLE = os.path.join(ROOT, "config.example.yaml")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


MINIMAL = """
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
"""


def load(yaml_text: str, symbol="SNDK", hedge="lighter-rh"):
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, hedge_venue=hedge)


def test_example_config_loads():
    cfg = load_config(EXAMPLE, NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_csv
    assert cfg.dashboard and cfg.log_file
    assert cfg.midline.mode == "dynamic"
    assert cfg.regime.enabled is True
    assert cfg.market_data.enforce_book_age
    assert cfg.market_data.max_book_age_ms == 300.0
    assert cfg.vwap_sizing.enabled
    assert cfg.execution_risk.enabled is True
    assert cfg.kill_switch.enabled is True
    assert cfg.accounting.enabled is True
    assert cfg.funding.enabled is True
    assert cfg.stablecoin.enabled is True
    assert cfg.entropy.quote_asset == "USDC"
    assert cfg.hedge.quote_asset == "USDG"


def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.hedge.label == "LIGHTER"
    assert cfg.hedge.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True
    assert cfg.midline.mode == "static"      # V1 behavior is unchanged
    assert cfg.midline.fast_window_seconds == 300.0
    assert cfg.midline.entry_z_score == 2.5
    assert cfg.midline.exit_z_score == 0.5
    assert cfg.regime.enabled is False
    assert cfg.market_data.enforce_book_age is False
    assert cfg.session.enabled is False       # crypto remains 24/7 by default


def test_v2_config_contract():
    cfg = load(MINIMAL + """
midline:
  mode: dynamic
  fast_method: ema
  fast_window_seconds: 120
  slow_method: median
  slow_window_seconds: 900
  min_samples: 100
  volatility_method: mad
  volatility_window_seconds: 600
  volatility_floor_bps: 0.25
regime:
  enabled: true
  max_fast_slow_difference_bps: 7
  max_z_score: 4.5
  max_absolute_spread_bps: 45
  break_persist_seconds: 2
  recovery_persist_seconds: 20
market_data:
  enforce_book_age: true
  max_book_age_ms: 250
session:
  enabled: true
sizing:
  vwap_enabled: true
  min_order_usd: 500
  max_order_usd: 25000
  minimum_net_edge_bps: 7
  max_vwap_slippage_bps: 4
  max_book_impact_bps: 3
  safety_buffer_bps: 2
  expected_latency_cost_bps: 1
execution:
  risk_recovery_enabled: true
  hedge_timeout_ms: 200
  max_unhedged_delta_usd: 3000
kill_switch:
  enabled: true
  max_unhedged_duration_ms: 750
  max_consecutive_partial_fills: 2
  max_reconcile_mismatch_usd: 500
  max_session_loss_usd: 1000
  emergency_flatten_enabled: false
""")
    assert cfg.midline.mode == "dynamic"
    assert cfg.midline.volatility_method == "mad"
    assert cfg.regime.enabled and cfg.regime.max_z_score == 4.5
    assert cfg.market_data.enforce_book_age
    assert cfg.session.enabled
    assert cfg.vwap_sizing.enabled and cfg.vwap_sizing.max_order_usd == 25000
    assert cfg.execution_risk.hedge_timeout_ms == 200
    assert cfg.kill_switch.max_consecutive_partial_fills == 2
    assert cfg.pending_v2_features == ()
    cfg.require_runtime_supported()
    # Legacy fields remain available to the current engine.
    assert cfg.midline_bps == 5.0 and cfg.max_order_notional == 500.0


def test_tradexyz_hedge():
    cfg = load(MINIMAL, hedge="tradexyz")
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"
    assert cfg.hedge.label == "XYZ"


def expect_error(yaml_text: str, needle: str, **kw):
    try:
        load(yaml_text, **kw)
    except ConfigError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"expected ConfigError containing {needle!r}")


def test_unknown_key_rejected():
    expect_error(MINIMAL + "\nthresholdz:\n  x: 1\n",
                 "unknown config key 'thresholdz'")
    expect_error(MINIMAL + "\nsizing:\n  take_fractionn: 0.5\n",
                 "sizing.take_fractionn")


def test_markets_no_longer_config_keys():
    # symbol / hedge_venue moved to --symbol / --hedge: leftovers in the
    # YAML must fail loudly, not silently override the flags
    expect_error("symbol: SNDK\n" + MINIMAL, "unknown config key 'symbol'")
    expect_error("hedge_venue: tradexyz\n" + MINIMAL,
                 "unknown config key 'hedge_venue'")


def test_bad_cli_markets():
    expect_error(MINIMAL, "--hedge", hedge="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


def test_missing_thresholds():
    expect_error("recorder:\n  enabled: true\n", "thresholds.")


def test_nonpositive_band():
    expect_error("thresholds:\n"
                 "  midline_bps: 5\n  upper_bps: 0\n  lower_bps: 3\n",
                 "must be > 0")


def test_v2_validation():
    expect_error(MINIMAL + "\nmidline:\n  mode: adaptive\n",
                 "midline.mode")
    expect_error(MINIMAL + "\nmidline:\n  fast_window_seconds: 600\n"
                 "  slow_window_seconds: 300\n", "slow_window_seconds")
    expect_error(MINIMAL + "\nmidline:\n  min_samples: 500\n"
                 "  slow_window_seconds: 300\n"
                 "  volatility_window_seconds: 300\n", "1Hz capacity")
    expect_error(MINIMAL + "\nmidline:\n  entry_z_score: 1\n"
                 "  exit_z_score: 1\n", "exit_z_score")
    expect_error(MINIMAL + "\nsizing:\n  vwap_enabled: true\n"
                 "  min_order_usd: 5000\n  max_order_usd: 1000\n",
                 "min_order_usd")
    expect_error(MINIMAL + "\nmarket_data:\n  max_book_age_ms: 0\n",
                 "max_book_age_ms")
    expect_error(MINIMAL + "\nsizing:\n  safety_buffer_bps: -1\n",
                 "safety_buffer_bps")
    expect_error(MINIMAL + "\nsizing:\n  expected_latency_cost_bps: -1\n",
                 "expected_latency_cost_bps")
    expect_error(MINIMAL + "\nkill_switch:\n"
                 "  max_consecutive_partial_fills: 0\n",
                 "max_consecutive_partial_fills")
    expect_error(MINIMAL + "\nfunding:\n  enabled: true\n",
                 "vwap_enabled")
    expect_error(MINIMAL + "\nsizing:\n  vwap_enabled: true\n"
                 "stablecoin:\n  enabled: true\n"
                 "  warning_deviation_bps: 40\n"
                 "  halt_deviation_bps: 30\n",
                 "warning_deviation_bps")
    expect_error(MINIMAL + "\naccounting:\n  enabled: true\n"
                 "  ledger_jsonl: logs/state.json\n"
                 "  state_json: logs/state.json\n", "must be different")
    expect_error(MINIMAL + "\nsizing:\n  vwap_enabled: true\n"
                 "stablecoin:\n  enabled: true\n  source_url: ''\n",
                 "source_url")
    expect_error(MINIMAL + "\nentropy:\n  quote_asset: ''\n",
                 "quote_asset")


def test_phase2_market_data_guard_is_runtime_supported():
    cfg = load(MINIMAL + "\nmarket_data:\n"
               "  enforce_book_age: true\n  max_book_age_ms: 250\n")
    assert cfg.pending_v2_features == ()
    cfg.require_runtime_supported()


def test_phase3_vwap_sizing_is_runtime_supported():
    cfg = load(MINIMAL + "\nsizing:\n"
               "  vwap_enabled: true\n  min_order_usd: 10\n"
               "  max_order_usd: 500\n  minimum_net_edge_bps: 4\n"
               "  max_vwap_slippage_bps: 5\n"
               "  max_book_impact_bps: 5\n")
    assert cfg.vwap_sizing.enabled
    assert cfg.pending_v2_features == ()
    cfg.require_runtime_supported()


def test_dynamic_midline_and_regime_are_runtime_supported():
    cfg = load(MINIMAL + "\nmidline:\n  mode: dynamic\n"
               "  min_samples: 3\nregime:\n  enabled: true\n")
    assert cfg.midline.mode == "dynamic" and cfg.regime.enabled
    assert cfg.pending_v2_features == ()
    cfg.require_runtime_supported()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
