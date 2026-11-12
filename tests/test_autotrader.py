"""Automatic paper trader: entry rules, fills, exits via the repo's exit policy, guards, settlement, daily log
and the HTTP routes. Quotes, market data and the clock are simulated; nothing touches the network."""

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api import auth, autotrader as at_mod
from api.app import create_app
from api.autotrader import AUTO_JOURNAL, AutoTrader, FeedError, fees_for, intrinsic, trading_days_held
from api.ledger import Ledger
from api.settings import Settings
from utils.market_clock import IST

H = {"X-Requested-With": "dashboard"}


def ist(y, m, d, hh=11, mm=10):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# 2026-11-17 is a Tuesday (an entry day); the weekly expiry picked from it is Mon 2026-11-23 (Tue 11-24 is a holiday).
TUE = (2026, 11, 17)
EXPIRY = date(2026, 11, 23)


def base_premium(strike, opt_type):
    return max(3.0, 150 - abs(strike - 25000) * 0.1)


class FakeFeed:
    def __init__(self):
        self.spot, self.vix, self.fail, self.scale, self.half = 25000.0, 16.0, None, 1.0, 1.0
        self.missing, self.wide = set(), False

    def snapshot(self):
        if self.fail:
            raise self.fail
        return {"spot": self.spot, "vix": self.vix}

    def leg_quotes(self, legs):
        if self.fail:
            raise self.fail
        out = {}
        for key in legs:
            if key in self.missing:
                continue
            _, strike, t = key
            p = base_premium(strike, t) * self.scale
            half = p * 0.5 if self.wide else self.half
            out[key] = {"lp": p, "bid": p - half, "ask": p + half}
        return {"spot": self.spot, "legs": out}


class FakeData:
    def __init__(self, as_of=date(2026, 11, 16), ret5=0.005, vix_change=0.0):
        self._as_of, self.ret5, self.vix_change, self.closes = as_of, ret5, vix_change, {}

    def latest_row(self):
        return pd.Series({"nifty_return_5d": self.ret5, "vix_change_5d": self.vix_change, "vix": 16.0,
                          "nifty_close": 25000.0}, name=pd.Timestamp(self._as_of))

    def as_of(self):
        return self._as_of

    def nifty_close_on(self, day):
        return self.closes.get(day)

    def return_5d_fallback(self):
        return 0.0


@pytest.fixture()
def env(tmp_path):
    ledger, feed, data = Ledger(tmp_path / "l.db"), FakeFeed(), FakeData()
    at = AutoTrader(ledger, feed, data, lot_size=65, initial_capital=2_500_000.0, regime_fn=lambda v: "TEST")
    at.enable(ist(*TUE, 9, 0))
    return at, ledger, feed, data


def codes(ledger, day="2026-11-17"):
    return [(e["kind"], e["code"]) for e in reversed(ledger.list_events(300, day))]


def open_trades(ledger):
    return ledger.list_trades(AUTO_JOURNAL, "open")


def enter(at, ledger, feed, vix=16.0):
    feed.vix = vix
    at.tick_sync(ist(*TUE, 11, 10))
    trades = open_trades(ledger)
    assert len(trades) == 1, codes(ledger)
    return trades[0]


# ------------------------------------------------------------------ entry
def test_enters_iron_condor_with_conservative_fills(env):
    at, ledger, feed, _ = env
    t = enter(at, ledger, feed)
    assert t["strategy"] == "weekly_ic" and t["lots"] == 15 and t["lot_size"] == 65 and len(t["legs"]) == 4
    assert t["expiry_date"] == EXPIRY.isoformat() and t["entry_time_ist"] == "11:10"
    for leg in t["legs"]:
        p = base_premium(leg["strike"], leg["option_type"])
        assert leg["entry_price"] == pytest.approx(p - 1.0 if leg["action"] == "SELL" else p + 1.0)   # bid / ask
    assert t["entry_credit"] == pytest.approx(sum(l["entry_price"] if l["action"] == "SELL" else -l["entry_price"] for l in t["legs"]))
    assert ("ENTRY", "ENTER") in codes(ledger) and t["capital_deployed"] > 0


def test_picks_put_credit_spread_when_vix_is_high(env):
    at, ledger, feed, _ = env
    t = enter(at, ledger, feed, vix=24.0)
    assert t["strategy"] == "weekly_pcs" and len(t["legs"]) == 2 and {l["option_type"] for l in t["legs"]} == {"PE"}


def test_one_decision_per_day_and_one_position(env):
    at, ledger, feed, _ = env
    enter(at, ledger, feed)
    at.tick_sync(ist(*TUE, 11, 30))
    at.tick_sync(ist(*TUE, 12, 30))
    assert len(ledger.list_trades(AUTO_JOURNAL, "all")) == 1
    assert codes(ledger).count(("ENTRY", "ENTER")) == 1


def test_non_entry_day_logs_one_skip(env):
    at, ledger, _, _ = env
    for hh, mm in ((11, 10), (11, 20), (12, 0)):
        at.tick_sync(ist(2026, 11, 18, hh, mm))                    # Wednesday
    ev = [e for e in ledger.list_events(50, "2026-11-18") if e["kind"] == "SKIP"]
    assert [e["code"] for e in ev] == ["WEEKDAY_NOT_ENTRY_DAY"] and not open_trades(ledger)


def test_holiday_and_early_time_do_nothing(env):
    at, ledger, _, _ = env
    at.tick_sync(ist(2026, 11, 24, 11, 10))                        # Tuesday holiday
    assert [e["code"] for e in ledger.list_events(20, "2026-11-24") if e["kind"] == "SKIP"] == ["MARKET_CLOSED"]
    at.tick_sync(ist(*TUE, 10, 0))                                 # before 11:05
    assert not [e for e in ledger.list_events(20, "2026-11-17") if e["kind"] == "SKIP"]


@pytest.mark.parametrize("vix,code", [(26.0, "VIX_TOO_HIGH"), (9.0, "VIX_TOO_LOW")])
def test_vix_gates_are_final_for_the_day(env, vix, code):
    at, ledger, feed, _ = env
    feed.vix = vix
    at.tick_sync(ist(*TUE, 11, 10))
    feed.vix = 16.0
    at.tick_sync(ist(*TUE, 11, 30))                                # conditions improve later: still no entry
    assert not open_trades(ledger) and ("SKIP", code) in codes(ledger)


def test_strategy_declining_entry_is_logged(env):
    at, ledger, feed, data = env
    data.vix_change = 0.30                                         # IC declines when VIX jumped >12% in 5 days
    at.tick_sync(ist(*TUE, 11, 10))
    assert not open_trades(ledger) and ("SKIP", "STRATEGY_DECLINED") in codes(ledger)


def test_low_credit_is_skipped(env):
    at, ledger, feed, _ = env
    feed.half = 40.0                                               # huge half-spread wipes out the credit
    at.tick_sync(ist(*TUE, 11, 10))
    assert not open_trades(ledger)
    assert any(c in ("LOW_CREDIT", "NO_LIQUIDITY", "WIDE_SPREAD") for _, c in codes(ledger))


def test_transient_problems_retry_inside_the_window(env):
    at, ledger, feed, _ = env
    feed.fail = FeedError("Kite is not connected today")
    at.tick_sync(ist(*TUE, 11, 10))
    at.tick_sync(ist(*TUE, 11, 11))
    assert codes(ledger).count(("SKIP", "NO_QUOTES")) == 1 and not open_trades(ledger)   # logged once, not settled
    feed.fail = None
    at.tick_sync(ist(*TUE, 11, 20))
    assert len(open_trades(ledger)) == 1                           # recovers and enters


def test_wide_spread_is_transient(env):
    at, ledger, feed, _ = env
    feed.wide = True
    at.tick_sync(ist(*TUE, 11, 10))
    assert ("SKIP", "WIDE_SPREAD") in codes(ledger) and not open_trades(ledger)
    feed.wide = False
    at.tick_sync(ist(*TUE, 11, 40))
    assert len(open_trades(ledger)) == 1


def test_gives_up_when_the_window_closes_while_blocked(env):
    at, ledger, feed, _ = env
    feed.fail = FeedError("down")
    at.tick_sync(ist(*TUE, 11, 10))
    at.tick_sync(ist(*TUE, 13, 5))
    assert ("SKIP", "GAVE_UP") in codes(ledger)
    feed.fail = None
    at.tick_sync(ist(*TUE, 13, 10))
    assert not open_trades(ledger)


def test_stale_data_blocks_entry(env):
    at, ledger, feed, data = env
    data._as_of = date(2026, 11, 1)
    at.tick_sync(ist(*TUE, 11, 10))
    assert not open_trades(ledger) and ("SKIP", "STALE_DATA") in codes(ledger)


def test_disabled_and_paused_do_not_enter(env):
    at, ledger, feed, _ = env
    at.pause(ist(*TUE, 9, 5), "manual")
    at.tick_sync(ist(*TUE, 11, 10))
    assert not open_trades(ledger) and ("SKIP", "PAUSED_SKIP") in codes(ledger)
    at.resume(ist(*TUE, 9, 6))
    at.disable(ist(*TUE, 9, 7))
    at.tick_sync(ist(*TUE, 11, 20))
    assert not open_trades(ledger)


def test_drawdown_pause(env):
    at, ledger, feed, _ = env
    t = ledger.open_trade(AUTO_JOURNAL, {"strategy": "weekly_pcs", "entry_date": "2026-11-09", "expiry_date": "2026-11-17",
                                         "legs": [{"action": "SELL", "strike": 24000, "option_type": "PE", "entry_price": 10}],
                                         "lots": 100, "lot_size": 65})
    ledger.close_trade(AUTO_JOURNAL, t["trade_id"], exit_debit=200, exit_reason="manual", exit_date="2026-11-12")
    at.tick_sync(ist(*TUE, 11, 10))
    assert at.paused_reason == "DRAWDOWN" and not open_trades(ledger)


def test_repeated_errors_pause_entries(env):
    at, ledger, feed, _ = env
    feed.fail = RuntimeError("boom")                              # not a FeedError: an unexpected failure
    for i in range(at_mod.MAX_CONSECUTIVE_ERRORS):
        at.tick_sync(ist(*TUE, 11, 10 + i))
    assert at.paused_reason == "ERRORS" and any(k == "ERROR" for k, _ in codes(ledger))


def test_preview_ignores_schedule_and_records_nothing(env):
    at, ledger, feed, _ = env
    before = len(ledger.list_events(500))
    r = at.preview(ist(2026, 11, 18, 16, 0))                       # Wednesday afternoon
    assert r["decision"] == "ENTER" and {b["code"] for b in r["blocks"]} >= {"WEEKDAY_NOT_ENTRY_DAY", "OUTSIDE_WINDOW"}
    assert len(ledger.list_events(500)) == before and not open_trades(ledger)


# ------------------------------------------------------------------ exits (the repo's check_weekly_exit)
def test_pcs_profit_target_needs_the_minimum_hold(env):
    at, ledger, feed, _ = env
    t = enter(at, ledger, feed, vix=24.0)
    feed.scale = 0.1                                               # premiums collapse: >75% of credit captured
    at.tick_sync(ist(2026, 11, 18, 11, 0))                         # 1 session held: too early
    assert len(open_trades(ledger)) == 1
    at.tick_sync(ist(2026, 11, 19, 11, 0))                         # 2 sessions held
    assert not open_trades(ledger)
    closed = ledger.get_trade(AUTO_JOURNAL, t["trade_id"])
    assert closed["exit_reason"] == "profit_target" and closed["net_pnl"] > 0 and closed["status"] == "closed"
    shorts_ask = sum(base_premium(l["strike"], l["option_type"]) * 0.1 + 0.5 * 0 + 1.0 for l in t["legs"] if l["action"] == "SELL")
    # closing buys the short back at the ask (price + 1.0 half-spread) and sells the long at the bid
    longs_bid = sum(base_premium(l["strike"], l["option_type"]) * 0.1 - 1.0 for l in t["legs"] if l["action"] == "BUY")
    assert closed["exit_debit"] == pytest.approx(max(shorts_ask - longs_bid, 0), abs=0.05)
    assert ("EXIT", "PROFIT_TARGET") in codes(ledger, "2026-11-19")


def test_stop_loss_when_premiums_blow_out(env):
    at, ledger, feed, _ = env
    t = enter(at, ledger, feed, vix=24.0)
    feed.scale = 3.0
    at.tick_sync(ist(2026, 11, 18, 11, 0))
    closed = ledger.get_trade(AUTO_JOURNAL, t["trade_id"])
    assert closed["status"] == "closed" and closed["exit_reason"] == "stop_loss" and closed["net_pnl"] < 0


def test_short_strike_breach_exits(env):
    at, ledger, feed, _ = env
    t = enter(at, ledger, feed, vix=24.0)
    short_put = next(l["strike"] for l in t["legs"] if l["action"] == "SELL")
    feed.spot = short_put - 10
    at.tick_sync(ist(2026, 11, 18, 11, 0))
    assert ledger.get_trade(AUTO_JOURNAL, t["trade_id"])["exit_reason"] == "stop_loss"


def test_exit_at_dte_limit(env):
    at, ledger, feed, _ = env
    t = enter(at, ledger, feed, vix=24.0)
    at.tick_sync(ist(2026, 11, 23, 10, 0))                         # expiry day
    assert ledger.get_trade(AUTO_JOURNAL, t["trade_id"])["exit_reason"] == "dte_limit"


def test_holds_when_nothing_triggers(env):
    at, ledger, feed, _ = env
    enter(at, ledger, feed, vix=24.0)
    at.tick_sync(ist(2026, 11, 18, 11, 0))
    at.tick_sync(ist(2026, 11, 18, 11, 1))
    assert len(open_trades(ledger)) == 1 and not [c for c in codes(ledger, "2026-11-18") if c[0] == "EXIT"]


def test_no_quotes_while_holding_is_flagged_once_and_position_stays(env):
    at, ledger, feed, _ = env
    enter(at, ledger, feed, vix=24.0)
    feed.fail = FeedError("Kite token expired")
    at.tick_sync(ist(2026, 11, 18, 11, 0))
    at.tick_sync(ist(2026, 11, 18, 11, 1))
    assert len(open_trades(ledger)) == 1 and codes(ledger, "2026-11-18").count(("ERROR", "NO_QUOTES")) == 1


def test_expired_position_settles_at_intrinsic(env):
    at, ledger, feed, data = env
    t = enter(at, ledger, feed, vix=24.0)
    feed.fail = FeedError("expired contracts have no quotes")
    at.tick_sync(ist(2026, 11, 24, 9, 30))                         # after expiry, no close known yet
    assert len(open_trades(ledger)) == 1 and ("INFO", "WAITING_SETTLEMENT") in codes(ledger, "2026-11-24")
    data.closes[EXPIRY] = 25100.0                                  # expiry-day close arrives with the morning refresh
    at.tick_sync(ist(2026, 11, 25, 9, 30))
    closed = ledger.get_trade(AUTO_JOURNAL, t["trade_id"])
    assert closed["exit_reason"] == "expiry_settlement" and closed["exit_debit"] == 0.0 and closed["exit_date"] == "2026-11-23"
    assert closed["net_pnl"] > 0


# ------------------------------------------------------------------ daily log and helpers
def test_daily_log_once_after_1630(env):
    at, ledger, feed, _ = env
    enter(at, ledger, feed)
    at.tick_sync(ist(*TUE, 16, 20))
    assert ledger.list_daily_log(AUTO_JOURNAL) == []
    at.tick_sync(ist(*TUE, 16, 35))
    at.tick_sync(ist(*TUE, 16, 40))
    log = ledger.list_daily_log(AUTO_JOURNAL)
    assert len(log) == 1 and log[0]["log_date"] == "2026-11-17" and log[0]["open_trades"] == 1
    assert "ENTRY ENTER" in log[0]["notes"] and log[0]["regime"] == "TEST"


def test_helpers():
    assert intrinsic("PE", 24000, 23900) == 100 and intrinsic("PE", 24000, 24100) == 0
    assert intrinsic("CE", 25000, 25150) == 150 and intrinsic("CE", 25000, 24900) == 0
    assert trading_days_held(date(2026, 11, 17), date(2026, 11, 19)) == 2
    assert trading_days_held(date(2026, 11, 20), date(2026, 11, 23)) == 1            # weekend not counted
    assert trading_days_held(date(2026, 11, 23), date(2026, 11, 25)) == 1            # 11-24 holiday not counted
    assert 100 < fees_for(40.0, 4, 15, 65) < 1000


# ------------------------------------------------------------------ routes
def make_settings(tmp: Path) -> Settings:
    return Settings(app_env="development", password="pw-123456", session_secret="s" * 40, session_hours=12,
                    cookie_secure=False, storage_dir=tmp, frontend_dist=None, enable_docs=False, lot_size=65,
                    load_market_on_start=False, credentials_key="k" * 40)


@pytest.fixture()
def api(tmp_path):
    auth.reset_lockouts()
    app = create_app(make_settings(tmp_path), load_market=False)
    app.state.autotrader.feed, app.state.autotrader.data = FakeFeed(), FakeData()
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"password": "pw-123456"}).status_code == 200
        yield c


def test_routes_require_login_and_csrf(api, tmp_path):
    anon = TestClient(api.app)
    assert anon.get("/api/auto/status").status_code == 401
    assert api.post("/api/auto/enable", json={}).status_code == 403


def test_status_enable_lots_pause_disable(api):
    s = api.get("/api/auto/status").json()
    assert s["enabled"] is False and s["paper_only"] is True and s["lots"] == 15 and s["journal"] is None
    s = api.post("/api/auto/enable", json={"lots": 10}, headers=H).json()
    assert s["enabled"] is True and s["lots"] == 10 and s["journal"]["journal_id"] == "AUTO"
    assert api.put("/api/auto/lots", json={"lots": 99}, headers=H).status_code == 422
    assert api.put("/api/auto/lots", json={"lots": 12}, headers=H).json()["lots"] == 12
    assert api.post("/api/auto/pause", json={"reason": "manual"}, headers=H).json()["paused_reason"] == "manual"
    assert api.post("/api/auto/resume", headers=H).json()["paused_reason"] == ""
    assert api.post("/api/auto/disable", headers=H).json()["enabled"] is False
    kinds = [e["code"] for e in api.get("/api/auto/events").json()["events"]]
    assert {"ENABLED", "LOTS", "PAUSED" if False else "manual", "RESUMED", "DISABLED"} <= set(kinds)


def test_preview_route(api):
    r = api.get("/api/auto/preview").json()
    assert r["decision"] in ("ENTER", "SKIP") and "blocks" in r
    assert api.get("/api/trades?journal_id=AUTO").json()["count"] == 0         # a preview never records a trade
