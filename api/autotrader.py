"""Automatic PAPER trader for the weekly track. It never places orders.

Decisions reuse the repo's own code, so behaviour matches the documented strategy:
  * strategy choice  : backtester.weekly_exit_policy.select_weekly_strategy_name
  * entry rule       : WeeklyIronCondor / WeeklyPutCreditSpread .should_enter(...)  (same gates as the CLI)
  * strikes          : strategy.generate_legs(spot, vix, dte)  (strikes only; premiums come from live quotes)
  * expiry           : data.expiry_calendar.get_weekly_expiry_in_range(today, 3, 8)
  * exits            : backtester.weekly_exit_policy.check_weekly_exit  (profit target, stop, breach, DTE limit,
                       iron-condor trailing delta / trend reversal / max hold), evaluated on live prices

Paper fills use live Kite bid/ask, conservatively: short legs fill at the bid, long legs at the ask, on
entry; closing buys shorts back at the ask and sells longs at the bid. Exit DECISIONS use mid prices.

The only external calls are quote reads (KiteFeed). There is no order code anywhere in this module.

Schedule (IST): entry check Mon/Tue 11:05-12:55 (one decision per day; transient problems retry inside the
window), exit checks every minute 09:15-15:30, daily log at 16:30. Everything is written to the ledger and an
event timeline for human review.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from backtester.weekly_exit_policy import (
    WEEKLY_IC, WeeklyExitTracker, build_tracker, check_weekly_exit, select_weekly_strategy_name, update_tracker,
)
from config import CostModel, WeeklyBacktestConfig
from data.expiry_calendar import get_weekly_expiry_in_range, is_trading_day
from starlette.concurrency import run_in_threadpool
from strategies.base import Leg, Trade
from strategies.weekly_strategies import WeeklyIronCondor, WeeklyPutCreditSpread
from utils.market_clock import is_market_hours, market_day_gate

logger = logging.getLogger(__name__)

AUTO_JOURNAL = "AUTO"
DEFAULT_LOTS, MIN_LOTS, MAX_LOTS = 15, 1, 30
ENTRY_FROM, ENTRY_UNTIL = time(11, 5), time(12, 55)
EXPIRY_BACKSTOP = time(15, 15)
DAILY_LOG_AT = time(16, 30)
TICK_SECONDS = 60
MIN_NET_CREDIT = 5.0              # rupees per unit; below this the trade is not worth taking
MAX_LEG_SPREAD_PCT = 0.35         # skip a leg whose (ask - bid) / mid exceeds this when mid >= MIN_MID_FOR_CHECK
MIN_MID_FOR_CHECK = 10.0
DD_PAUSE_PCT = 15.0               # pause new entries at this drawdown (the repo's pause rule)
MAX_CONSECUTIVE_ERRORS = 5
DATA_MAX_AGE_DAYS = 6
RISK_FREE = 0.065

KV_ENABLED, KV_PAUSED, KV_LOTS = "auto.enabled", "auto.paused_reason", "auto.lots"
KV_ERRORS, KV_SETTLED_DAY, KV_DAILY_LOG_DAY = "auto.consecutive_errors", "auto.settled_day", "auto.daily_log_day"
KV_LAST_TICK, KV_TRACKER = "auto.last_tick", "auto.tracker."
KV_NOQUOTE_AT = "auto.noquote_at"

LegKey = Tuple[date, float, str]


class FeedError(Exception):
    """Live quotes are unavailable (not configured / not connected / token expired / API error)."""


# ------------------------------------------------------------------------------------ feeds
class KiteFeed:
    """Read-only quote source backed by Kite Connect (quotes and the instrument list only)."""

    def _adapter(self):
        if not (os.getenv("KITE_API_KEY") and os.getenv("KITE_API_SECRET")):
            raise FeedError("Kite is not configured on the server")
        if not os.getenv("KITE_ACCESS_TOKEN"):
            raise FeedError("Kite is not connected today")
        from data.kite_adapter import KiteFyersAdapter

        return KiteFyersAdapter()

    @staticmethod
    def _wrap(exc: Exception) -> FeedError:
        if exc.__class__.__name__ in ("TokenException", "PermissionException"):
            return FeedError("Kite token expired")
        return FeedError(f"quote request failed ({exc.__class__.__name__})")

    def snapshot(self) -> Dict[str, float]:
        adapter = self._adapter()
        try:
            resp = adapter.quotes({"symbols": "NSE:NIFTY50-INDEX,NSE:INDIAVIX-INDEX"})
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc)
        if resp.get("s") != "ok":
            raise FeedError("Kite token expired" if resp.get("code") == -15 else "index quote failed")
        vals = {i["n"]: i["v"].get("lp") for i in resp["d"]}
        spot, vix = vals.get("NSE:NIFTY50-INDEX"), vals.get("NSE:INDIAVIX-INDEX")
        if not spot or not vix:
            raise FeedError("incomplete index quotes")
        return {"spot": float(spot), "vix": float(vix)}

    def leg_quotes(self, legs: List[LegKey]) -> Dict[str, Any]:
        adapter = self._adapter()
        try:
            return adapter.leg_quotes(legs)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc)


class ServerData:
    """Read-only view of the dashboard's cached daily market data (previous completed session)."""

    def __init__(self, state: Dict[str, Any]):
        self._s = state

    def latest_row(self):
        return self._s.get("latest_row")

    def as_of(self) -> Optional[date]:
        name = getattr(self.latest_row(), "name", None)
        return name.date() if hasattr(name, "date") else None

    def nifty_close_on(self, day: date) -> Optional[float]:
        df = self._s.get("market_df")
        if df is None or "nifty_close" not in df.columns:
            return None
        try:
            idx = df.index.normalize() if hasattr(df.index, "normalize") else df.index
            hit = df.loc[idx == _ts(day), "nifty_close"].dropna()
            return float(hit.iloc[-1]) if len(hit) else None
        except Exception:  # noqa: BLE001
            return None

    def return_5d_fallback(self) -> Optional[float]:
        df = self._s.get("market_df")
        if df is None or "nifty_close" not in df.columns:
            return None
        closes = df["nifty_close"].dropna()
        return float(closes.iloc[-1] / closes.iloc[-6] - 1.0) if len(closes) > 5 else None


def _ts(day: date):
    import pandas as pd

    return pd.Timestamp(day)


# ------------------------------------------------------------------------------------ helpers
def _isnan(x: Any) -> bool:
    try:
        return x is None or math.isnan(float(x))
    except (TypeError, ValueError):
        return True


def _mid(q: Dict[str, Any]) -> float:
    bid, ask, ltp = float(q.get("bid") or 0), float(q.get("ask") or 0), float(q.get("lp") or 0)
    return (bid + ask) / 2 if bid > 0 and ask > 0 else ltp


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def fees_for(credit: float, n_legs: int, lots: int, lot_size: int) -> float:
    """Statutory charges + brokerage for the round trip (slippage is already in the bid/ask fills)."""
    cm = CostModel()
    qty = lots * lot_size
    notional = abs(credit) * qty
    brokerage = cm.brokerage_per_order * n_legs * 2
    total = (notional * (cm.stt_on_sell_pct + cm.exchange_txn_pct + cm.sebi_turnover_pct + cm.stamp_duty_buy_pct) / 100
             + brokerage + brokerage * cm.gst_on_brokerage_pct / 100)
    return round(total, 2)


def trading_days_held(entry: date, today: date) -> int:
    """Trading sessions after the entry day up to and including today (entry day = 0)."""
    n, d = 0, entry
    while d < today:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


def intrinsic(option_type: str, strike: float, spot: float) -> float:
    return max(strike - spot, 0.0) if option_type == "PE" else max(spot - strike, 0.0)


def _strategy_for(name: str, lots: int, lot_size: int):
    return (WeeklyIronCondor if name == WEEKLY_IC else WeeklyPutCreditSpread)(lots=lots, lot_size=lot_size)


# ------------------------------------------------------------------------------------ engine
class AutoTrader:
    def __init__(self, ledger, feed, data, *, lot_size: int = 65, initial_capital: float = 2_500_000.0,
                 regime_fn: Optional[Callable[[float], str]] = None):
        self.ledger, self.feed, self.data = ledger, feed, data
        self.lot_size, self.initial_capital = lot_size, initial_capital
        self.regime_fn = regime_fn or (lambda vix: "")
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- settings
    def _kv(self, key: str, default: str = "") -> str:
        return self.ledger.kv_get(key, default) or default

    @property
    def enabled(self) -> bool:
        return self._kv(KV_ENABLED, "0") == "1"

    @property
    def paused_reason(self) -> str:
        return self._kv(KV_PAUSED, "")

    @property
    def lots(self) -> int:
        try:
            return max(MIN_LOTS, min(MAX_LOTS, int(self._kv(KV_LOTS, str(DEFAULT_LOTS)))))
        except ValueError:
            return DEFAULT_LOTS

    def _ensure_journal(self) -> None:
        try:
            self.ledger.get_journal(AUTO_JOURNAL)
        except Exception:  # noqa: BLE001 - NotFound
            self.ledger.create_journal(AUTO_JOURNAL, "Auto paper trader", self.initial_capital, "weekly",
                                       "Managed by the automatic paper trader. Do not edit by hand.")

    def _event(self, now: datetime, kind: str, code: str, message: str, trade_id: Optional[str] = None,
               data: Optional[Dict[str, Any]] = None) -> None:
        self.ledger.add_event(now.isoformat(timespec="seconds"), kind, code, message, trade_id, _jsonable(data) if data else None)

    def enable(self, now: datetime, lots: Optional[int] = None) -> None:
        with self._lock:
            self._ensure_journal()
            if lots is not None:
                self.set_lots(now, lots)
            self.ledger.kv_set(KV_ENABLED, "1")
            self.ledger.kv_set(KV_PAUSED, "")
            self.ledger.kv_set(KV_ERRORS, "0")
            self._event(now, "INFO", "ENABLED", f"Auto paper trader turned on ({self.lots} lots, journal {AUTO_JOURNAL})")

    def disable(self, now: datetime) -> None:
        with self._lock:
            self.ledger.kv_set(KV_ENABLED, "0")
            open_n = len(self.ledger.list_trades(AUTO_JOURNAL, "open")) if self._journal_exists() else 0
            self._event(now, "INFO", "DISABLED", "Auto paper trader turned off" + (
                f". {open_n} open position(s) are no longer managed automatically" if open_n else ""))

    def pause(self, now: datetime, reason: str, message: Optional[str] = None) -> None:
        with self._lock:
            self.ledger.kv_set(KV_PAUSED, reason)
            self._event(now, "PAUSE", reason, message or f"New entries paused ({reason}). Open positions are still managed.")

    def resume(self, now: datetime) -> None:
        with self._lock:
            self.ledger.kv_set(KV_PAUSED, "")
            self.ledger.kv_set(KV_ERRORS, "0")
            self._event(now, "INFO", "RESUMED", "New entries resumed")

    def set_lots(self, now: datetime, lots: int) -> None:
        if not MIN_LOTS <= lots <= MAX_LOTS:
            raise ValueError(f"lots must be between {MIN_LOTS} and {MAX_LOTS}")
        self.ledger.kv_set(KV_LOTS, str(lots))
        self._event(now, "INFO", "LOTS", f"Lots per trade set to {lots}")

    def _journal_exists(self) -> bool:
        try:
            self.ledger.get_journal(AUTO_JOURNAL)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---------------------------------------------------------------- the tick
    def tick_sync(self, now: datetime) -> None:
        """One pass of the schedule. Safe to call every minute; each step is guarded independently."""
        with self._lock:
            self.ledger.kv_set(KV_LAST_TICK, now.isoformat(timespec="seconds"))
            if not self.enabled:
                return
            self._ensure_journal()
            for name, step in (("settle", self._settle_expired), ("exits", self._manage_exits),
                               ("entry", self._try_entry), ("daily_log", self._daily_log)):
                try:
                    step(now)
                except Exception as exc:  # noqa: BLE001
                    self._record_error(now, name, exc)
                else:
                    if name == "exits":
                        self.ledger.kv_set(KV_ERRORS, "0")

    def _record_error(self, now: datetime, step: str, exc: Exception) -> None:
        n = int(self._kv(KV_ERRORS, "0") or 0) + 1
        self.ledger.kv_set(KV_ERRORS, str(n))
        logger.error("Auto trader step %s failed: %s", step, exc.__class__.__name__)
        self._event(now, "ERROR", "EXCEPTION", f"{step}: {exc.__class__.__name__}: {str(exc)[:160]}")
        if n >= MAX_CONSECUTIVE_ERRORS and not self.paused_reason:
            self.pause(now, "ERRORS", f"{n} errors in a row. New entries paused; open positions are still managed.")

    def _throttled_event(self, now: datetime, kind: str, code: str, message: str, minutes: int = 10) -> None:
        last = self._kv(KV_NOQUOTE_AT + "." + code, "")
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(minutes=minutes):
                    return
            except ValueError:
                pass
        self.ledger.kv_set(KV_NOQUOTE_AT + "." + code, now.isoformat(timespec="seconds"))
        self._event(now, kind, code, message)

    # ---------------------------------------------------------------- entry
    def plan_entry(self, now: datetime, *, enforce_schedule: bool = True) -> Dict[str, Any]:
        """Work out what an entry would be right now. No side effects: used for the real entry and for the preview."""
        today = now.date()
        blocks: List[Dict[str, str]] = []

        def skip(code: str, message: str, transient: bool = False, **extra: Any) -> Dict[str, Any]:
            return {"decision": "SKIP", "code": code, "message": message, "transient": transient,
                    "blocks": blocks, **extra}

        closed = market_day_gate(today)
        if closed:
            blocks.append({"code": "MARKET_CLOSED", "message": closed})
        elif today.weekday() not in (0, 1):
            blocks.append({"code": "WEEKDAY_NOT_ENTRY_DAY",
                           "message": f"Weekly entries are Mon/Tue only (today is {today:%A})"})
        if not (ENTRY_FROM <= now.time() <= ENTRY_UNTIL):
            blocks.append({"code": "OUTSIDE_WINDOW", "message": "Entry checks run 11:05-12:55 IST"})
        if self.ledger.list_trades(AUTO_JOURNAL, "open") if self._journal_exists() else []:
            blocks.append({"code": "POSITION_OPEN", "message": "One open position at a time (phase 1 rule)"})
        if enforce_schedule and blocks:
            return skip(blocks[0]["code"], blocks[0]["message"], transient=blocks[0]["code"] == "OUTSIDE_WINDOW")

        row, as_of = self.data.latest_row(), self.data.as_of()
        if row is None or as_of is None:
            return skip("DATA_LOADING", "Market data is not loaded yet", transient=True)
        if (today - as_of).days > DATA_MAX_AGE_DAYS:
            return skip("STALE_DATA", f"Market data is as of {as_of}, older than {DATA_MAX_AGE_DAYS} days", transient=True)

        try:
            snap = self.feed.snapshot()
        except FeedError as exc:
            return skip("NO_QUOTES", f"Live index quotes unavailable: {exc}", transient=True)
        spot, vix = snap["spot"], snap["vix"]
        inputs = {"spot": spot, "vix": vix, "data_as_of": as_of}

        wc = WeeklyBacktestConfig()
        if vix < wc.min_vix_entry:
            return skip("VIX_TOO_LOW", f"VIX {vix:.2f} is below the minimum {wc.min_vix_entry}", inputs=inputs)
        if vix > wc.max_vix_entry:
            return skip("VIX_TOO_HIGH", f"VIX {vix:.2f} is above the maximum {wc.max_vix_entry}", inputs=inputs)
        expiry, dte = get_weekly_expiry_in_range(today, wc.min_dte_entry, wc.max_dte_entry)
        if expiry is None:
            return skip("NO_EXPIRY_IN_WINDOW", f"No weekly expiry within {wc.min_dte_entry}-{wc.max_dte_entry} DTE", inputs=inputs)

        ret5 = row.get("nifty_return_5d", None)
        if _isnan(ret5):
            ret5 = self.data.return_5d_fallback()
        name = select_weekly_strategy_name(vix, None if _isnan(ret5) else ret5)
        inputs.update(ret_5d=None if _isnan(ret5) else float(ret5), strategy=name, expiry=expiry, dte=dte)
        lots = self.lots
        strat = _strategy_for(name, lots, self.lot_size)
        row_dict = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        if strat.should_enter(spot, vix, row_dict).value != "ENTER":
            return skip("STRATEGY_DECLINED", f"{name} declined entry (VIX {vix:.2f}, 5-day return "
                        f"{'n/a' if _isnan(ret5) else format(float(ret5), '.1%')})", inputs=inputs)

        legs = strat.generate_legs(spot, vix, dte, RISK_FREE)
        keys: List[LegKey] = [(expiry, float(l.strike), l.option_type) for l in legs]
        try:
            quotes = self.feed.leg_quotes(keys)
        except FeedError as exc:
            return skip("NO_QUOTES", f"Option quotes unavailable: {exc}", transient=True, inputs=inputs)

        fills: List[Dict[str, Any]] = []
        for leg, key in zip(legs, keys):
            q = quotes["legs"].get(key)
            label = f"{'SELL' if leg.is_short else 'BUY'} {int(leg.strike)}{leg.option_type}"
            if q is None:
                return skip("NO_LIQUIDITY", f"No live quote for {label}", transient=True, inputs=inputs)
            bid, ask = float(q.get("bid") or 0), float(q.get("ask") or 0)
            price = bid if leg.is_short else ask
            if price <= 0:
                return skip("NO_LIQUIDITY", f"No {'bid' if leg.is_short else 'ask'} for {label}", transient=True, inputs=inputs)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                if mid >= MIN_MID_FOR_CHECK and (ask - bid) / mid > MAX_LEG_SPREAD_PCT:
                    return skip("WIDE_SPREAD", f"{label} spread {bid}/{ask} is wider than {MAX_LEG_SPREAD_PCT:.0%} of mid",
                                transient=True, inputs=inputs)
            fills.append({"action": "SELL" if leg.is_short else "BUY", "strike": float(leg.strike),
                          "option_type": leg.option_type, "entry_price": round(price, 2), "bid": bid, "ask": ask,
                          "ltp": float(q.get("lp") or 0)})

        credit = round(sum(f["entry_price"] if f["action"] == "SELL" else -f["entry_price"] for f in fills), 2)
        if credit < MIN_NET_CREDIT:
            return skip("LOW_CREDIT", f"Net credit {credit:.2f} at bid/ask is below the minimum {MIN_NET_CREDIT}",
                        inputs=inputs, legs=fills)
        return {"decision": "ENTER", "code": "ENTER", "message": f"{name} {lots} lots, credit {credit:.2f} per unit",
                "blocks": blocks, "strategy": name, "expiry": expiry, "dte": dte, "lots": lots,
                "net_credit": credit, "legs": fills, "inputs": inputs, "transient": False}

    def preview(self, now: datetime) -> Dict[str, Any]:
        """What the trader would do right now, ignoring the schedule gates. Records nothing."""
        with self._lock:
            return _jsonable(self.plan_entry(now, enforce_schedule=False))

    def _try_entry(self, now: datetime) -> None:
        today, day = now.date(), now.date().isoformat()
        if self._kv(KV_SETTLED_DAY) == day:
            return
        t = now.time()
        if t < ENTRY_FROM:
            return
        if t > ENTRY_UNTIL:
            # The window closed while a transient problem kept blocking the entry: say so once and stop for today.
            transient = ("NO_QUOTES", "NO_LIQUIDITY", "WIDE_SPREAD", "DATA_LOADING", "STALE_DATA")
            if not self.ledger.has_event(day, "ENTER") and any(self.ledger.has_event(day, c) for c in transient):
                self._event(now, "SKIP", "GAVE_UP", "Entry window closed with the entry still blocked by a transient problem")
                self.ledger.kv_set(KV_SETTLED_DAY, day)
            return
        if self.paused_reason:
            if not self.ledger.has_event(day, "PAUSED_SKIP"):
                self._event(now, "SKIP", "PAUSED_SKIP", f"Entries are paused ({self.paused_reason})")
            self.ledger.kv_set(KV_SETTLED_DAY, day)
            return
        summary = self.ledger.summary(AUTO_JOURNAL)
        if summary["account_dd_pct"] >= DD_PAUSE_PCT:
            self.pause(now, "DRAWDOWN", f"Drawdown {summary['account_dd_pct']}% reached the {DD_PAUSE_PCT:.0f}% pause rule")
            self.ledger.kv_set(KV_SETTLED_DAY, day)
            return

        plan = self.plan_entry(now, enforce_schedule=True)
        if plan["decision"] == "ENTER":
            self._open_position(plan, now)
            self.ledger.kv_set(KV_SETTLED_DAY, day)
            return
        code = plan["code"]
        if plan.get("transient"):
            if not self.ledger.has_event(day, code):
                self._event(now, "SKIP", code, plan["message"] + " (will retry until 12:55)", data=plan.get("inputs"))
            return
        self._event(now, "SKIP", code, plan["message"], data={"inputs": plan.get("inputs"), "legs": plan.get("legs")})
        self.ledger.kv_set(KV_SETTLED_DAY, day)

    def _open_position(self, plan: Dict[str, Any], now: datetime) -> None:
        inputs, legs, lots = plan["inputs"], plan["legs"], plan["lots"]
        units = lots * self.lot_size
        widths = [abs(a["strike"] - b["strike"]) for a in legs if a["action"] == "SELL"
                  for b in legs if b["action"] == "BUY" and b["option_type"] == a["option_type"]]
        risk = max(0.0, (max(widths) if widths else 0.0) - plan["net_credit"]) * units
        trade = self.ledger.open_trade(AUTO_JOURNAL, {
            "strategy": plan["strategy"], "entry_date": now.date().isoformat(),
            "entry_time_ist": now.strftime("%H:%M"), "expiry_date": plan["expiry"].isoformat(),
            "legs": [{k: f[k] for k in ("action", "strike", "option_type", "entry_price")} for f in legs],
            "lots": lots, "lot_size": self.lot_size, "entry_vix": inputs["vix"], "entry_spot": inputs["spot"],
            "regime": plan["strategy"], "capital_deployed": round(risk, 2),
            "notes": "AUTO paper entry: shorts filled at bid, longs at ask"})
        obj, strat = self._trade_obj(trade, inputs["spot"], inputs["vix"], plan["dte"], None)
        self._save_tracker(trade["trade_id"], build_tracker(obj))
        self._event(now, "ENTRY", "ENTER", f"Entered {plan['strategy']} {trade['trade_id']}: {lots} lots, credit "
                    f"{plan['net_credit']:.2f}/unit, expiry {plan['expiry']}", trade["trade_id"],
                    {"inputs": inputs, "legs": legs, "max_loss_rupees": round(risk, 2)})

    # ---------------------------------------------------------------- position objects for the repo's exit policy
    def _trade_obj(self, t: Dict[str, Any], spot: float, vix: float, dte: int,
                   marks: Optional[Dict[LegKey, float]]) -> Tuple[Trade, Any]:
        expiry = date.fromisoformat(t["expiry_date"])
        legs = [Leg(l["option_type"], float(l["strike"]), l["action"] == "SELL", float(l["entry_price"]),
                    float(l["entry_price"]), t["lots"], t["lot_size"]) for l in t["legs"]]
        obj = Trade(strategy_name=t["strategy"], entry_date=date.fromisoformat(t["entry_date"]), legs=legs,
                    entry_spot=float(t["entry_spot"] or spot), entry_vix=float(t["entry_vix"] or vix),
                    lots=t["lots"], lot_size=t["lot_size"])
        strat = _strategy_for(t["strategy"], t["lots"], t["lot_size"])
        try:
            strat.update_premiums(obj, spot, vix, max(dte, 1), RISK_FREE)    # model Greeks (the backtest used the same)
        except Exception:  # noqa: BLE001
            pass
        for leg in obj.legs:                                                   # then price from the market
            if marks is not None:
                leg.current_premium = marks[(expiry, leg.strike, leg.option_type)]
            else:
                leg.current_premium = leg.entry_premium
        return obj, strat

    def _save_tracker(self, trade_id: str, tracker: WeeklyExitTracker) -> None:
        self.ledger.kv_set(KV_TRACKER + trade_id, json.dumps(asdict(tracker)))

    def _load_tracker(self, trade_id: str, fallback: Trade) -> WeeklyExitTracker:
        raw = self.ledger.kv_get(KV_TRACKER + trade_id)
        try:
            return WeeklyExitTracker(**json.loads(raw)) if raw else build_tracker(fallback)
        except (TypeError, ValueError):
            return build_tracker(fallback)

    # ---------------------------------------------------------------- exits
    def _manage_exits(self, now: datetime) -> None:
        trades = self.ledger.list_trades(AUTO_JOURNAL, "open")
        if not trades:
            return
        today = now.date()
        live = [t for t in trades if date.fromisoformat(t["expiry_date"]) >= today]
        if not live:
            return
        keys: List[LegKey] = []
        for t in live:
            exp = date.fromisoformat(t["expiry_date"])
            keys += [(exp, float(l["strike"]), l["option_type"]) for l in t["legs"]]
        try:
            snap, quotes = self.feed.snapshot(), self.feed.leg_quotes(keys)
        except FeedError as exc:
            self._throttled_event(now, "ERROR", "NO_QUOTES",
                                  f"Cannot manage the open position: live quotes unavailable ({exc}). Exits are paused until quotes return.")
            return
        for t in live:
            self._evaluate_exit(t, now, snap, quotes["legs"])

    def _evaluate_exit(self, t: Dict[str, Any], now: datetime, snap: Dict[str, float], q: Dict[LegKey, Dict[str, Any]]) -> None:
        today, expiry = now.date(), date.fromisoformat(t["expiry_date"])
        keys = [(expiry, float(l["strike"]), l["option_type"]) for l in t["legs"]]
        missing = [k for k in keys if k not in q]
        if missing:
            self._throttled_event(now, "ERROR", "NO_QUOTES", f"No live quote for {len(missing)} leg(s) of {t['trade_id']}")
            return
        marks = {k: _mid(q[k]) for k in keys}
        dte = (expiry - today).days
        obj, _ = self._trade_obj(t, snap["spot"], snap["vix"], dte, marks)
        tracker = self._load_tracker(t["trade_id"], obj)
        update_tracker(tracker, obj, snap["spot"])
        self._save_tracker(t["trade_id"], tracker)
        equity = self.ledger.summary(AUTO_JOURNAL)["account_equity"]
        should_exit, reason = check_weekly_exit(
            config=WeeklyBacktestConfig(), trade=obj, tracker=tracker, spot=snap["spot"], vix=snap["vix"], dte=dte,
            entry_vix=float(t["entry_vix"] or snap["vix"]), current_equity=equity,
            holding_days=trading_days_held(date.fromisoformat(t["entry_date"]), today))
        name = reason.name.lower() if reason is not None else ""
        if not should_exit and today == expiry and now.time() >= EXPIRY_BACKSTOP:
            should_exit, name = True, "expiry_backstop"
        if should_exit:
            self._close(t, now, snap, q, keys, name)

    def _close(self, t: Dict[str, Any], now: datetime, snap: Dict[str, float], q: Dict[LegKey, Dict[str, Any]],
               keys: List[LegKey], reason: str) -> None:
        sells = longs = 0.0
        legs_out = []
        for leg, k in zip(t["legs"], keys):
            quote = q[k]
            bid, ask, ltp = float(quote.get("bid") or 0), float(quote.get("ask") or 0), float(quote.get("lp") or 0)
            price = (ask if ask > 0 else ltp) if leg["action"] == "SELL" else (bid if bid > 0 else ltp)
            if leg["action"] == "SELL":
                sells += price
            else:
                longs += price
            legs_out.append({"action": leg["action"], "strike": leg["strike"], "option_type": leg["option_type"],
                             "exit_price": round(price, 2), "bid": bid, "ask": ask})
        debit = round(max(sells - longs, 0.0), 2)
        self._finish(t, now, debit, reason, "closed at live bid/ask", {"legs": legs_out, "spot": snap["spot"], "vix": snap["vix"]},
                     exit_day=now.date(), exit_time=now.strftime("%H:%M"))

    def _finish(self, t: Dict[str, Any], now: datetime, debit: float, reason: str, how: str, data: Dict[str, Any],
                exit_day: date, exit_time: str) -> None:
        fees = fees_for(t["entry_credit"], len(t["legs"]), t["lots"], t["lot_size"])
        closed = self.ledger.close_trade(AUTO_JOURNAL, t["trade_id"], exit_debit=debit, exit_reason=reason,
                                         exit_date=exit_day.isoformat(), exit_time_ist=exit_time, brokerage=fees,
                                         notes=f"AUTO exit ({how})")
        self.ledger.kv_delete(KV_TRACKER + t["trade_id"])
        self._event(now, "EXIT", reason.upper(),
                    f"Closed {t['trade_id']} ({reason.replace('_', ' ')}): exit debit {debit:.2f} vs credit "
                    f"{t['entry_credit']:.2f}, net P&L {closed['net_pnl']:,.0f}", t["trade_id"],
                    {**data, "exit_debit": debit, "gross_pnl": closed["gross_pnl"], "net_pnl": closed["net_pnl"], "fees": fees})

    def _settle_expired(self, now: datetime) -> None:
        """Safety net: a position still open after its expiry is settled at the Nifty close on expiry day (intrinsic)."""
        for t in self.ledger.list_trades(AUTO_JOURNAL, "open"):
            expiry = date.fromisoformat(t["expiry_date"])
            if expiry >= now.date():
                continue
            close = self.data.nifty_close_on(expiry)
            if close is None:
                self._throttled_event(now, "INFO", "WAITING_SETTLEMENT",
                                      f"{t['trade_id']} expired {expiry}; waiting for the Nifty close to settle it", minutes=720)
                continue
            sells = sum(intrinsic(l["option_type"], float(l["strike"]), close) for l in t["legs"] if l["action"] == "SELL")
            longs = sum(intrinsic(l["option_type"], float(l["strike"]), close) for l in t["legs"] if l["action"] == "BUY")
            self._finish(t, now, round(max(sells - longs, 0.0), 2), "expiry_settlement",
                         f"settled at intrinsic value, Nifty close {close:,.2f}", {"nifty_close": close},
                         exit_day=expiry, exit_time="15:30")

    # ---------------------------------------------------------------- daily log + status
    def _daily_log(self, now: datetime) -> None:
        today, day = now.date(), now.date().isoformat()
        if now.time() < DAILY_LOG_AT or not is_trading_day(today) or self._kv(KV_DAILY_LOG_DAY) == day:
            return
        s = self.ledger.summary(AUTO_JOURNAL)
        try:
            vix = self.feed.snapshot()["vix"]
        except FeedError:
            row = self.data.latest_row()
            vix = float(row.get("vix")) if row is not None and not _isnan(row.get("vix")) else None
        events = [e for e in self.ledger.list_events(200, day) if e["kind"] in ("ENTRY", "EXIT", "SKIP", "ERROR")]
        note = "AUTO: " + ("; ".join(f"{e['kind']} {e['code']}" for e in reversed(events)) if events else "no activity")
        self.ledger.upsert_daily_log(AUTO_JOURNAL, {
            "log_date": day, "account_equity": s["account_equity"], "cumulative_pnl": s["total_pnl"],
            "open_trades": s["open_trades"], "vix_close": vix, "regime": self.regime_fn(vix) if vix else "",
            "account_dd_pct": s["account_dd_pct"], "notes": note[:900]})
        self.ledger.kv_set(KV_DAILY_LOG_DAY, day)

    def status(self, now: datetime) -> Dict[str, Any]:
        exists = self._journal_exists()
        summary = self.ledger.summary(AUTO_JOURNAL) if exists else None
        open_trades = self.ledger.list_trades(AUTO_JOURNAL, "open") if exists else []
        return _jsonable({
            "enabled": self.enabled, "paused_reason": self.paused_reason, "lots": self.lots,
            "lot_size": self.lot_size, "journal_id": AUTO_JOURNAL, "journal": summary,
            "open_trades": [{"trade_id": t["trade_id"], "strategy": t["strategy"], "expiry_date": t["expiry_date"],
                             "entry_date": t["entry_date"], "entry_credit": t["entry_credit"]} for t in open_trades],
            "consecutive_errors": int(self._kv(KV_ERRORS, "0") or 0),
            "last_tick": self._kv(KV_LAST_TICK, "") or None,
            "data_as_of": self.data.as_of(),
            "schedule": {"entry_days": "Mon, Tue", "entry_window": "11:05-12:55 IST", "exit_checks": "every minute 09:15-15:30 IST",
                         "daily_log": "16:30 IST", "dd_pause_pct": DD_PAUSE_PCT, "max_lots": MAX_LOTS},
            "paper_only": True,
        })


# ------------------------------------------------------------------------------------ scheduler
async def scheduler(app) -> None:
    from api.server import now_ist

    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await run_in_threadpool(app.state.autotrader.tick_sync, now_ist())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Auto trader scheduler error: %s", exc.__class__.__name__)
