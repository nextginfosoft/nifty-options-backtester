"""Automatic data refresh (end-date rule, atomic swap, scheduler) and live-price P&L with rule alerts.
No network: Yahoo and Kite are replaced by fakes."""

import asyncio
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api import auth, datarefresh, live_pnl, routes_ledger, server
from api.app import create_app
from api.settings import Settings
from data.kite_adapter import KiteFyersAdapter

IST = server.IST
H = {"X-Requested-With": "dashboard"}


def ist(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# 2026-11-11 is a Wednesday, 2026-11-13 a Friday, 2026-11-14 a Saturday.
def test_data_end_date_is_todays_ist_date_never_tomorrow():
    # The fetcher never includes today's bar, so the end date must not be pushed past today (an earlier
    # design bumped it to tomorrow after 16:00 IST, which the fetcher normalises away anyway).
    for h in (0, 10, 15, 16, 23):
        assert server.data_end_date(ist(2026, 11, 11, h)) == date(2026, 11, 11)
    assert server.data_end_date(ist(2026, 11, 14, 17)) == date(2026, 11, 14)


# ------------------------------------------------------------------ refresh_blocking
@pytest.fixture()
def restore_state():
    saved = {k: server._state[k] for k in ("market_df", "latest_row", "models", "market_fetcher", "status", "errors", "last_refresh")}
    saved["models"] = dict(saved["models"])
    yield
    server._state.update(saved)


def fake_market(day, rows=10):
    df = pd.DataFrame({"x": range(rows)}, index=pd.date_range(end=day, periods=rows))
    return object(), df, df.iloc[-1]


def test_refresh_swaps_data_and_models_together(restore_state, monkeypatch):
    server._state.update(market_df=None, latest_row=fake_market("2026-11-09")[2], status="ready", errors=[])
    monkeypatch.setattr(server, "_build_market", lambda: fake_market("2026-11-11", 12))
    monkeypatch.setattr(server, "_build_models", lambda df: {"entry_model": "E2", "exit_model": "X2"})
    r = server.refresh_blocking("manual")
    assert r["ok"] and r["changed"] and r["latest_date"] == "2026-11-11" and r["rows"] == 12
    assert server._state["models"]["entry_model"] == "E2" and server._state["last_refresh"] == r
    again = server.refresh_blocking("manual")
    assert again["ok"] and again["changed"] is False


def test_failed_refresh_keeps_previous_data(restore_state, monkeypatch):
    old_row = fake_market("2026-11-09")[2]
    server._state.update(latest_row=old_row, status="ready", errors=[])
    server._state["models"].update(entry_model="E1", exit_model="X1")

    def boom():
        raise ConnectionError("yahoo down")

    monkeypatch.setattr(server, "_build_market", boom)
    r = server.refresh_blocking("manual")
    assert r["ok"] is False and "keeping previous data" in r["message"] and "yahoo down" not in r["message"]
    assert server._state["latest_row"] is old_row and server._state["models"]["entry_model"] == "E1"

    monkeypatch.setattr(server, "_build_market", lambda: fake_market("2026-11-11"))

    def bad_models(df):
        raise ValueError("pickle mismatch")

    monkeypatch.setattr(server, "_build_models", bad_models)
    assert server.refresh_blocking("manual")["ok"] is False
    assert server._state["latest_row"] is old_row  # data is not swapped without matching models


def test_only_one_refresh_at_a_time(restore_state):
    assert server._refresh_lock.acquire(blocking=False)
    try:
        with pytest.raises(server.RefreshBusy):
            server.refresh_blocking("manual")
    finally:
        server._refresh_lock.release()


# ------------------------------------------------------------------ scheduler rules
@pytest.fixture(autouse=True)
def _reset_refresh_state():
    datarefresh.reset_state()
    yield
    datarefresh.reset_state()


def test_due_slot_windows_and_weekends():
    assert datarefresh.due_slot(ist(2026, 11, 11, 8, 44)) is None
    assert datarefresh.due_slot(ist(2026, 11, 11, 8, 45)) == "morning"
    assert datarefresh.due_slot(ist(2026, 11, 11, 16, 15)) == "morning"  # still pending later in the day
    assert datarefresh.due_slot(ist(2026, 11, 14, 9, 0)) is None          # Saturday
    assert datarefresh.due_slot(ist(2026, 11, 15, 9, 0)) is None          # Sunday
    assert datarefresh.due_slot(ist(2026, 11, 16, 9, 0)) == "morning"     # Monday


def test_done_slots_and_startup_marking():
    now = ist(2026, 11, 11, 10, 0)
    datarefresh.mark_passed_slots_done(now)
    assert datarefresh.due_slot(now) is None                               # startup load covered the morning
    assert datarefresh.due_slot(ist(2026, 11, 11, 16, 20)) is None
    assert datarefresh.due_slot(ist(2026, 11, 12, 9, 0)) == "morning"      # next day runs again
    datarefresh.reset_state()
    datarefresh.mark_passed_slots_done(ist(2026, 11, 11, 8, 0))            # started before 08:45: still due
    assert datarefresh.due_slot(ist(2026, 11, 11, 8, 50)) == "morning"


def test_failed_slot_retries_three_times_with_gap(monkeypatch):
    now = ist(2026, 11, 11, 9, 0)
    results = []

    def fake_refresh(trigger):
        results.append(trigger)
        return {"ok": False, "message": "x"}

    monkeypatch.setattr(server, "refresh_blocking", fake_refresh)
    for attempt in range(datarefresh.MAX_TRIES_PER_SLOT):
        assert datarefresh.due_slot(now) == "morning"
        asyncio.run(datarefresh.run_slot("morning", now))
        datarefresh.state["last_try"] -= datarefresh.RETRY_GAP_SECONDS + 1   # skip the wait
    assert results == ["morning"] * 3
    assert datarefresh.due_slot(now) is None                                 # gave up for today
    assert datarefresh.due_slot(ist(2026, 11, 12, 9, 0)) == "morning"        # tomorrow still runs


def test_retry_waits_for_gap_and_success_completes_slot(monkeypatch):
    now = ist(2026, 11, 11, 9, 0)
    monkeypatch.setattr(server, "refresh_blocking", lambda t: {"ok": False, "message": "x"})
    asyncio.run(datarefresh.run_slot("morning", now))
    assert datarefresh.due_slot(now) is None                                 # too soon to retry
    datarefresh.state["last_try"] -= datarefresh.RETRY_GAP_SECONDS + 1
    monkeypatch.setattr(server, "refresh_blocking", lambda t: {"ok": True, "latest_date": "2026-11-10"})
    asyncio.run(datarefresh.run_slot("morning", now))
    assert datarefresh.due_slot(now) is None


def test_busy_refresh_does_not_burn_a_try(monkeypatch):
    now = ist(2026, 11, 11, 9, 0)

    def busy(trigger):
        raise server.RefreshBusy("busy")

    monkeypatch.setattr(server, "refresh_blocking", busy)
    asyncio.run(datarefresh.run_slot("morning", now))
    assert datarefresh.state["tries"][datarefresh._key(now, "morning")] == 0


# ------------------------------------------------------------------ live P&L maths and alerts
def trade(strategy="put_credit_spread", entry="2026-11-09", expiry="2026-11-17"):
    legs = [{"action": "SELL", "strike": 24800, "option_type": "PE", "entry_price": 62.0},
            {"action": "BUY", "strike": 24600, "option_type": "PE", "entry_price": 31.0}]
    return {"trade_id": "J-001", "journal_id": "J", "strategy": strategy, "entry_date": entry, "expiry_date": expiry,
            "legs": legs, "lots": 2, "lot_size": 65, "entry_credit": 31.0}


def quotes(sell_ltp, sell_ask, buy_ltp, buy_bid, expiry=date(2026, 11, 17)):
    return {(expiry, 24800.0, "PE"): {"lp": sell_ltp, "bid": sell_ltp - 1, "ask": sell_ask, "oi": 1, "volume": 1},
            (expiry, 24600.0, "PE"): {"lp": buy_ltp, "bid": buy_bid, "ask": buy_ltp + 1, "oi": 1, "volume": 1}}


TODAY = date(2026, 11, 11)


def codes(r):
    return [a["code"] for a in r["alerts"]]


def test_mark_and_exit_estimates():
    r = live_pnl.evaluate_trade(trade(), quotes(30, 31, 8, 7), 25000, TODAY)
    assert r["ok"] and r["mark_debit"] == 22.0 and r["exit_debit_est"] == 24.0
    assert r["unrealized_mark"] == 1170.0 and r["unrealized_exit"] == 910.0   # x 2 lots x 65
    assert r["pct_credit_captured"] == 22.6 and r["alerts"] == []


def test_profit_target_only_for_credit_spreads():
    r = live_pnl.evaluate_trade(trade(), quotes(6, 7, 2, 2), 25000, TODAY)        # exit debit 5 -> 83.9% captured
    assert r["pct_credit_captured"] == 83.9 and codes(r) == ["profit_target"]
    ic = live_pnl.evaluate_trade(trade("iron_condor", entry="2026-11-10"), quotes(6, 7, 2, 2), 25000, TODAY)
    assert "profit_target" not in codes(ic)                                       # iron condor has no profit target


def test_stop_at_100_percent_of_credit():
    r = live_pnl.evaluate_trade(trade(), quotes(68, 70, 5, 5), 25000, TODAY)      # exit debit 65 -> loss 34 >= 31
    assert "stop_loss" in codes(r)
    ok = live_pnl.evaluate_trade(trade(), quotes(50, 52, 8, 8), 25000, TODAY)     # exit debit 44 -> loss 13
    assert "stop_loss" not in codes(ok)


def test_short_strike_breach_uses_spot():
    assert "strike_breach" in codes(live_pnl.evaluate_trade(trade(), quotes(30, 31, 8, 7), 24790, TODAY))
    assert "strike_breach" in codes(live_pnl.evaluate_trade(trade(), quotes(30, 31, 8, 7), 24800, TODAY))
    assert "strike_breach" not in codes(live_pnl.evaluate_trade(trade(), quotes(30, 31, 8, 7), 24801, TODAY))
    assert "strike_breach" not in codes(live_pnl.evaluate_trade(trade(), quotes(30, 31, 8, 7), None, TODAY))
    call = trade()
    call["legs"] = [{"action": "SELL", "strike": 25200, "option_type": "CE", "entry_price": 40.0},
                    {"action": "BUY", "strike": 25400, "option_type": "CE", "entry_price": 10.0}]
    q = {(date(2026, 11, 17), 25200.0, "CE"): {"lp": 30, "bid": 29, "ask": 31}, (date(2026, 11, 17), 25400.0, "CE"): {"lp": 8, "bid": 7, "ask": 9}}
    assert "strike_breach" in codes(live_pnl.evaluate_trade(call, q, 25250, TODAY))


def test_expiry_day_and_iron_condor_max_hold():
    assert "expiry_day" in codes(live_pnl.evaluate_trade(trade(expiry="2026-11-11"), quotes(30, 31, 8, 7, expiry=date(2026, 11, 11)), 25000, TODAY))
    long_ic = live_pnl.evaluate_trade(trade("iron_condor", entry="2026-11-04"), quotes(30, 31, 8, 7), 25000, TODAY)
    assert "max_hold" in codes(long_ic)                                           # 5 weekdays held
    fresh_ic = live_pnl.evaluate_trade(trade("iron_condor", entry="2026-11-09"), quotes(30, 31, 8, 7), 25000, TODAY)
    assert "max_hold" not in codes(fresh_ic)                                      # 2 weekdays held
    assert "max_hold" not in codes(live_pnl.evaluate_trade(trade(entry="2026-11-02"), quotes(30, 31, 8, 7), 25000, TODAY))


def test_missing_quote_and_no_depth_fallback():
    r = live_pnl.evaluate_trade(trade(), {}, 25000, TODAY)
    assert r["ok"] is False and "No live quote" in r["reason"]
    thin = {(date(2026, 11, 17), 24800.0, "PE"): {"lp": 30, "bid": 0, "ask": 0},
            (date(2026, 11, 17), 24600.0, "PE"): {"lp": 8, "bid": 0, "ask": 0}}
    r2 = live_pnl.evaluate_trade(trade(), thin, 25000, TODAY)
    assert r2["exit_debit_est"] == 22.0                                           # falls back to LTP on both legs


# ------------------------------------------------------------------ Kite adapter leg quotes
class FakeKite:
    def __init__(self):
        self.exp = date(2026, 11, 17)
        self.rows = [{"name": "NIFTY", "instrument_type": t, "expiry": self.exp, "strike": float(k),
                      "tradingsymbol": f"NIFTY26N17{k}{t}"} for k in (24600, 24800) for t in ("CE", "PE")]
        self.asked = []

    def instruments(self, ex):
        return self.rows

    def quote(self, keys):
        self.asked.append(list(keys))
        out = {}
        for k in keys:
            out[k] = {"last_price": 25000.0} if k == "NSE:NIFTY 50" else {
                "last_price": 12.5, "oi": 10, "volume": 5, "depth": {"buy": [{"price": 12.0}], "sell": [{"price": 13.0}]}}
        return out


def test_leg_quotes_maps_exact_instruments():
    from data import kite_adapter
    kite_adapter._instrument_cache.update(day=None, rows=[])
    k = FakeKite()
    a = KiteFyersAdapter(kite=k)
    res = a.leg_quotes([(k.exp, 24800.0, "PE"), (k.exp, 24600.0, "PE"), (k.exp, 99999.0, "CE"), (date(2020, 1, 1), 24800.0, "PE")])
    assert res["spot"] == 25000.0
    assert set(res["legs"]) == {(k.exp, 24800.0, "PE"), (k.exp, 24600.0, "PE")}  # unknown / expired legs are absent
    assert res["legs"][(k.exp, 24800.0, "PE")] == {"lp": 12.5, "bid": 12.0, "ask": 13.0, "oi": 10, "volume": 5}
    assert sorted(k.asked[0]) == ["NFO:NIFTY26N1724600PE", "NFO:NIFTY26N1724800PE", "NSE:NIFTY 50"]


# ------------------------------------------------------------------ HTTP endpoints
def make_settings(tmp: Path) -> Settings:
    return Settings(app_env="development", password="pw-123456", session_secret="s" * 40, session_hours=12,
                    cookie_secure=False, storage_dir=tmp, frontend_dist=None, enable_docs=False, lot_size=65,
                    load_market_on_start=False, credentials_key="k" * 40)


@pytest.fixture()
def authed(tmp_path, monkeypatch):
    for n in ("KITE_API_KEY", "KITE_API_SECRET", "KITE_ACCESS_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    auth.reset_lockouts()
    routes_ledger._live_cache.clear()
    with TestClient(create_app(make_settings(tmp_path), load_market=False)) as c:
        assert c.post("/api/auth/login", json={"password": "pw-123456"}).status_code == 200
        yield c


def open_trade(c):
    c.post("/api/journals", json={"journal_id": "J", "label": "x", "initial_capital": 1000000}, headers=H)
    body = {"journal_id": "J", "strategy": "put_credit_spread", "entry_date": "2026-11-09", "expiry_date": "2026-11-17",
            "lots": 2, "legs": [{"action": "SELL", "strike": 24800, "option_type": "PE", "entry_price": 62},
                                {"action": "BUY", "strike": 24600, "option_type": "PE", "entry_price": 31}]}
    return c.post("/api/trades", json=body, headers=H)


def test_refresh_endpoint_status_codes(authed, monkeypatch):
    monkeypatch.setattr(server, "refresh_blocking", lambda t: {"ok": True, "latest_date": "2026-11-11", "message": "ok"})
    assert authed.post("/api/market/refresh", headers=H).json()["latest_date"] == "2026-11-11"
    monkeypatch.setattr(server, "refresh_blocking", lambda t: {"ok": False, "message": "Refresh failed (X); keeping previous data"})
    assert authed.post("/api/market/refresh", headers=H).status_code == 502

    def busy(t):
        raise server.RefreshBusy("A data refresh is already running")

    monkeypatch.setattr(server, "refresh_blocking", busy)
    assert authed.post("/api/market/refresh", headers=H).status_code == 409
    assert authed.post("/api/market/refresh").status_code == 403           # CSRF header required


def test_health_reports_data_date_and_last_refresh(authed):
    h = authed.get("/api/health").json()
    assert "data_as_of" in h and "last_refresh" in h


def test_live_endpoint_without_trades_and_without_kite(authed):
    assert authed.get("/api/trades/live").json()["trades"] == []
    open_trade(authed)
    r = authed.get("/api/trades/live").json()
    assert r["ok"] is False and "not configured" in r["reason"]


def test_live_endpoint_with_quotes_and_cache_invalidation(authed, monkeypatch):
    open_trade(authed)
    calls = []

    def fake_fetch(trades):
        calls.append(1)
        return {"spot": 25000.0, "legs": quotes(30, 31, 8, 7)}

    monkeypatch.setattr(live_pnl, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(server, "now_ist", lambda: ist(2026, 11, 11, 11, 30))
    r = authed.get("/api/trades/live?journal_id=J").json()
    t = r["trades"][0]
    assert r["ok"] and r["spot"] == 25000.0 and t["exit_debit_est"] == 24.0 and t["unrealized_exit"] == 910.0
    authed.get("/api/trades/live?journal_id=J")
    assert len(calls) == 1                                                  # second call served from the 5 s cache
    authed.post("/api/trades/J-001/close?journal_id=J", headers=H, json={"exit_debit": 20, "exit_reason": "manual"})
    assert authed.get("/api/trades/live?journal_id=J").json()["trades"] == []   # closed trade no longer live
