import json

from entropy_arb.ledger import PairLedger


def fill(action, *, buy_key, sell_key, buy_px, sell_px, qty=1.0,
         at=1000.0):
    return dict(
        action=action, qty=qty, buy_key=buy_key, sell_key=sell_key,
        buy_px=buy_px, sell_px=sell_px,
        planned_buy_px=buy_px, planned_sell_px=sell_px,
        buy_fee_rate=0.001, sell_fee_rate=0.001,
        buy_quote_usd=1.0, sell_quote_usd=1.0,
        spread_bps=10.0 if action != "EXIT" else 1.0,
        z_score=3.0 if action != "EXIT" else 0.1,
        midline_bps=5.0, funding_cost_bps=2.0,
        stablecoin_basis_bps=0.0, market_session="regular", at=at)


def test_pair_ledger_persists_open_pair_runtime_and_completed_pnl(tmp_path):
    events = tmp_path / "pairs.jsonl"
    state = tmp_path / "state.json"
    ledger = PairLedger(str(events), str(state))
    pair = ledger.ensure_pair(
        pair_id="ARB-TEST", symbol="SNDK", venue_a="ENTROPY",
        venue_b="RH", direction="sell_entropy", entry_time=1000.0)
    ledger.record_fill(pair, fill(
        "OPEN", buy_key="hedge", sell_key="entropy",
        buy_px=100.0, sell_px=101.0))
    pair.update_market(7.0)
    ledger.snapshot({"entry_pause_reasons": ["test_pause"]})

    restored = PairLedger(str(events), str(state))
    assert restored.current is not None
    assert restored.current.pair_id == "ARB-TEST"
    assert restored.current.remaining_base == 1.0
    assert restored.current.max_favorable_spread == 3.0
    assert restored.runtime["entry_pause_reasons"] == ["test_pause"]

    pair = restored.current
    restored.record_fill(pair, fill(
        "EXIT", buy_key="entropy", sell_key="hedge",
        buy_px=100.4, sell_px=100.2, at=4600.0))
    restored.snapshot()
    assert restored.current is None
    complete = restored.completed[-1]
    assert complete.complete and complete.holding_time == 3600.0
    assert complete.entry_spread == 10.0 and complete.exit_spread == 1.0
    assert complete.entry_session == "regular"
    assert complete.exit_session == "regular"
    assert complete.funding_source == "estimated"
    assert complete.fees > 0
    assert abs(complete.gross_pnl - 0.8) < 1e-12
    assert complete.net_pnl < complete.gross_pnl
    lines = events.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["type"] for line in lines] == [
        "PAIR_OPENED", "PAIR_FILL", "PAIR_FILL", "PAIR_COMPLETED"]


def test_quote_basis_is_included_in_pair_net_pnl(tmp_path):
    ledger = PairLedger(str(tmp_path / "pairs.jsonl"),
                        str(tmp_path / "state.json"))
    pair = ledger.ensure_pair(
        pair_id="ARB-BASIS", symbol="SNDK", venue_a="ENTROPY",
        venue_b="RH", direction="buy_entropy", entry_time=1000.0)
    data = fill("OPEN", buy_key="entropy", sell_key="hedge",
                buy_px=100.0, sell_px=100.5)
    data["sell_quote_usd"] = 0.997
    data["stablecoin_basis_bps"] = 30.0
    ledger.record_fill(pair, data)
    assert pair.stablecoin_basis_usd < 0
    assert pair.net_pnl < pair.gross_pnl


def test_reconciliation_is_additive_and_marks_accounting_incomplete(tmp_path):
    ledger = PairLedger(str(tmp_path / "pairs.jsonl"),
                        str(tmp_path / "state.json"))
    pair = ledger.ensure_pair(
        pair_id="ARB-RECON", symbol="SNDK", venue_a="ENTROPY",
        venue_b="RH", direction="sell_entropy", entry_time=1000.0)
    ledger.record_fill(pair, fill(
        "OPEN", buy_key="hedge", sell_key="entropy",
        buy_px=100.0, sell_px=101.0, qty=2.0))
    ledger.reconcile_current(1.5, "chain correction")
    assert ledger.current.remaining_base == 1.5
    assert ledger.current.reconciliation_adjustment_base == -0.5
    assert not ledger.current.accounting_complete
    assert "PAIR_RECONCILED" in (tmp_path / "pairs.jsonl").read_text()
