"""Strategy-evidence summary, the holiday-list cover reminder and the Christmas 2026 closure."""
from datetime import date

import pytest

from api import evidence
from data.expiry_calendar import holidays_covered_through, is_trading_day


def trades(pnls, lots=1):
    return [{"net_pnl": p * lots, "lots": lots, "strategy": "Weekly Put Credit Spread" if i % 2 else "Weekly Iron Condor",
             "exit_reason": "PROFIT_TARGET" if p > 0 else "STOP_LOSS", "brokerage": 50.0} for i, p in enumerate(pnls)]


def test_no_trades_is_no_data():
    r = evidence.build([])
    assert r["n"] == 0 and r["verdict"]["code"] == "NO_DATA" and r["references"]


def test_a_few_trades_never_produce_a_verdict():
    r = evidence.build(trades([1200, 900]))
    assert r["verdict"]["code"] == "TOO_EARLY" and r["per_lot"]["ci95"] is None and r["win_rate_pct"] == 100.0
    r = evidence.build(trades([1000, -5000, 800, 700, 900, 1100, -400, 600, 500, 800, 700, 300]))
    assert r["verdict"]["code"] == "EARLY" and r["per_lot"]["ci95"] is None


def test_thirty_trades_give_an_error_bar_and_a_verdict():
    good = evidence.build(trades([900, 1100, 1000, 950, 1050] * 6))          # tight, positive
    assert good["verdict"]["code"] == "POSITIVE" and good["per_lot"]["ci95"][0] > 0
    bad = evidence.build(trades([-900, -1100, -1000, -950, -1050] * 6))
    assert bad["verdict"]["code"] == "NEGATIVE" and bad["per_lot"]["ci95"][1] < 0
    noisy = evidence.build(trades([3000, -3500, 2500, -2800, 2000, -2200] * 5))
    assert noisy["verdict"]["code"] == "INCONCLUSIVE" and noisy["per_lot"]["ci95"][0] < 0 < noisy["per_lot"]["ci95"][1]


def test_numbers_add_up_and_group():
    r = evidence.build(trades([1000, -400, 600, -200]))
    assert r["total_net"] == 1000 and r["wins"] == 2 and r["fees_total"] == 200.0
    assert r["best_trade"] == 1000 and r["worst_trade"] == -400 and r["profit_factor"] == pytest.approx(2.67, abs=0.01)
    assert {s["strategy"]: s["n"] for s in r["by_strategy"]} == {"Weekly Iron Condor": 2, "Weekly Put Credit Spread": 2}
    assert {x["reason"]: x["n"] for x in r["by_exit_reason"]} == {"PROFIT_TARGET": 2, "STOP_LOSS": 2}


def test_per_lot_normalises_position_size():
    r = evidence.build(trades([1000, 1000], lots=3))
    assert r["per_lot"]["mean"] == 1000.0 and r["total_net"] == 6000


def test_trades_without_a_pnl_are_ignored():
    r = evidence.build([{"net_pnl": None, "lots": 1}, *trades([500])])
    assert r["n"] == 1


def test_endpoint_and_health_calendar(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api import auth
    from api.app import create_app
    from tests.test_autotrader import make_settings
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    auth.reset_lockouts()
    with TestClient(create_app(make_settings(tmp_path), load_market=False)) as c:
        assert c.get("/api/auto/evidence").status_code == 401
        assert c.post("/api/auth/login", json={"password": "pw-123456"}).status_code == 200
        r = c.get("/api/auto/evidence").json()
        assert r["verdict"]["code"] in ("NO_DATA", "TOO_EARLY", "EARLY")
        cal = c.get("/api/health").json()["calendar"]
        assert cal["covered_through"] == "2026-12-31" and isinstance(cal["low"], bool)


def test_holiday_list_cover_and_christmas_2026():
    assert holidays_covered_through() == date(2026, 12, 31)
    assert is_trading_day(date(2026, 12, 25)) is False          # Friday, NSE closed
    assert is_trading_day(date(2026, 12, 24)) is True
