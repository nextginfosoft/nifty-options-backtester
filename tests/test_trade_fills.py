"""Per-leg buy/sell price log (api.ledger.Ledger.fills_for_trade / fills_csv)."""
from __future__ import annotations

import csv
import io

import pytest

from api.ledger import Ledger

LEGS = [
    {"action": "SELL", "strike": 24800.0, "option_type": "PE", "entry_price": 50.3},
    {"action": "BUY", "strike": 24400.0, "option_type": "PE", "entry_price": 18.1},
]
ENTRY_DATA = {
    "inputs": {"spot": 25536.05, "vix": 18.21},
    "legs": [
        {**LEGS[0], "bid": 50.3, "ask": 50.5, "ltp": 50.25},
        {**LEGS[1], "bid": 18.05, "ask": 18.1, "ltp": 18.1},
    ],
}


@pytest.fixture()
def ledger(tmp_path):
    lg = Ledger(tmp_path / "t.db")
    lg.create_journal("AUTO", "Auto", 500000)
    return lg


def _open(lg):
    t = lg.open_trade("AUTO", {"strategy": "weekly_pcs", "entry_date": "2026-11-23", "entry_time_ist": "11:05",
                               "expiry_date": "2026-12-01", "legs": LEGS, "lots": 15, "lot_size": 65,
                               "entry_vix": 18.21, "entry_spot": 25536.05})
    lg.add_event("2026-11-23T11:05:00", "ENTRY", "ENTER", "entered", t["trade_id"], ENTRY_DATA)
    return t["trade_id"]


def test_engine_close_has_per_leg_exit_prices(ledger):
    tid = _open(ledger)
    ledger.close_trade("AUTO", tid, exit_debit=12.0, exit_reason="profit_target", exit_date="2026-11-25",
                       exit_time_ist="10:30", brokerage=800.0)
    ledger.add_event("2026-11-25T10:30:00", "EXIT", "PROFIT_TARGET", "closed", tid, {
        "legs": [{"action": "SELL", "strike": 24800.0, "option_type": "PE", "exit_price": 20.0, "bid": 19.5, "ask": 20.0},
                 {"action": "BUY", "strike": 24400.0, "option_type": "PE", "exit_price": 8.0, "bid": 8.0, "ask": 8.2}],
        "spot": 25700.0, "vix": 16.0})
    f = ledger.fills_for_trade("AUTO", tid)
    short, long_ = f["legs"]
    assert short["entry_bid"] == 50.3 and short["entry_ask"] == 50.5 and short["exit_price"] == 20.0
    assert short["leg_pnl"] == round((50.3 - 20.0) * 975, 2)      # short gains when it is bought back cheaper
    assert long_["leg_pnl"] == round((8.0 - 18.1) * 975, 2)       # long loses when it is sold cheaper
    assert short["entry_spread_cost"] == round(abs(50.3 - 50.4) * 975, 2)
    assert f["exit"]["per_leg_recorded"] is True and f["exit"]["time_ist"] == "10:30"
    assert f["money"]["fees"] == 800.0 and f["source"]["exit"] == "engine"


def test_manual_close_flags_missing_per_leg_detail(ledger):
    tid = _open(ledger)
    ledger.close_trade("AUTO", tid, exit_debit=12.0, exit_reason="profit_target", exit_date="2026-11-25")
    f = ledger.fills_for_trade("AUTO", tid)
    assert f["exit"]["per_leg_recorded"] is False and f["source"]["exit"] == "manual close"
    assert all(l["exit_price"] is None for l in f["legs"])
    assert f["money"]["gross_pnl"] == round((32.2 - 12.0) * 975, 2)


def test_fills_csv_rows(ledger):
    tid = _open(ledger)
    ledger.close_trade("AUTO", tid, exit_debit=12.0, exit_reason="profit_target", exit_date="2026-11-25")
    rows = list(csv.DictReader(io.StringIO(ledger.fills_csv("AUTO"))))
    assert [r["Phase"] for r in rows] == ["ENTRY", "ENTRY", "EXIT_NET"]
    assert rows[0]["Bid"] == "50.3" and rows[0]["Ask"] == "50.5" and rows[0]["Fill_Price"] == "50.3"
