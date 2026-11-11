"""Live-price P&L and exit-rule alerts for open paper trades, from Kite quotes.

Replaces the Black-Scholes estimate with real market prices:
  * mark      : every leg valued at its last traded price.
  * exit est. : what closing now would likely cost, i.e. buy back short legs at the ask and sell long
                legs at the bid (falls back to LTP when a side has no depth).
Alerts apply the paper-trading rules from paper_trading/QUICK_REFERENCE.md as guidance only; nothing
is ever placed or closed automatically:
  * credit spreads: exit at 75% of max profit
  * stop: loss reaches 100% of the credit collected
  * short strike breached by the Nifty spot
  * expiry day: exit by 15:15 IST
  * iron condor: max hold 4 trading days (weekday count, ignores exchange holidays)
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

LegKey = Tuple[date, float, str]


class LiveUnavailable(Exception):
    """Live prices cannot be fetched (not configured / not connected / token expired / API error)."""


def _price(leg: Dict[str, Any], q: Dict[str, Any]) -> float:
    ltp = float(q.get("lp") or 0)
    if leg["action"] == "SELL":          # closing a short leg means buying it back at the ask
        ask = float(q.get("ask") or 0)
        return ask if ask > 0 else ltp
    bid = float(q.get("bid") or 0)       # closing a long leg means selling it at the bid
    return bid if bid > 0 else ltp


def trading_days_held(entry: date, today: date) -> int:
    return int(np.busday_count(entry.isoformat(), today.isoformat()))


def evaluate_trade(trade: Dict[str, Any], quotes: Dict[LegKey, Dict[str, Any]], spot: Optional[float],
                   today: date) -> Dict[str, Any]:
    expiry = date.fromisoformat(trade["expiry_date"])
    legs_out: List[Dict[str, Any]] = []
    missing = 0
    mark_sold = mark_bought = exit_sold = exit_bought = 0.0
    for leg in trade["legs"]:
        q = quotes.get((expiry, float(leg["strike"]), leg["option_type"]))
        row = {"action": leg["action"], "strike": leg["strike"], "option_type": leg["option_type"],
               "entry_price": leg["entry_price"], "ltp": None, "bid": None, "ask": None}
        if q is None:
            missing += 1
        else:
            ltp = float(q.get("lp") or 0)
            row.update(ltp=ltp, bid=float(q.get("bid") or 0), ask=float(q.get("ask") or 0))
            exit_price = _price(leg, q)
            if leg["action"] == "SELL":
                mark_sold += ltp
                exit_sold += exit_price
            else:
                mark_bought += ltp
                exit_bought += exit_price
        legs_out.append(row)

    base = {"trade_id": trade["trade_id"], "journal_id": trade["journal_id"], "strategy": trade["strategy"],
            "legs": legs_out}
    if missing:
        return {**base, "ok": False,
                "reason": f"No live quote for {missing} leg(s) (expired or not listed)", "alerts": []}

    credit = float(trade["entry_credit"])
    units = int(trade["lots"]) * int(trade["lot_size"])
    mark_debit = mark_sold - mark_bought
    exit_debit = max(exit_sold - exit_bought, 0.0)
    captured = (credit - exit_debit) / credit * 100 if credit > 0 else None

    alerts: List[Dict[str, str]] = []
    if "credit_spread" in trade["strategy"] and captured is not None and captured >= 75:
        alerts.append({"code": "profit_target", "message": "Profit target: 75% of max profit reached. Rule says exit."})
    if credit > 0 and exit_debit - credit >= credit:
        alerts.append({"code": "stop_loss", "message": "Stop: loss has reached 100% of the credit collected. Exit now."})
    if spot:
        for leg in trade["legs"]:
            if leg["action"] != "SELL":
                continue
            if (leg["option_type"] == "PE" and spot <= leg["strike"]) or (leg["option_type"] == "CE" and spot >= leg["strike"]):
                alerts.append({"code": "strike_breach",
                               "message": f"Nifty {spot:,.0f} has breached the short {int(leg['strike'])}{leg['option_type']}. Rule says exit."})
    if today == expiry:
        alerts.append({"code": "expiry_day", "message": "Expiry day: exit by 15:15 IST."})
    if trade["strategy"] == "iron_condor":
        held = trading_days_held(date.fromisoformat(trade["entry_date"]), today)
        if held >= 4:
            alerts.append({"code": "max_hold", "message": f"Iron condor max hold of 4 trading days reached ({held}, approx.)."})

    return {
        **base, "ok": True,
        "mark_debit": round(mark_debit, 2),
        "exit_debit_est": round(exit_debit, 2),
        "unrealized_mark": round((credit - mark_debit) * units, 2),
        "unrealized_exit": round((credit - exit_debit) * units, 2),
        "pct_credit_captured": None if captured is None else round(captured, 1),
        "alerts": alerts,
    }


def fetch_quotes(open_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Blocking: live quotes for every open leg plus the spot. Raises LiveUnavailable with a user-facing reason."""
    if not (os.getenv("KITE_API_KEY") and os.getenv("KITE_API_SECRET")):
        raise LiveUnavailable("Kite is not configured on the server")
    if not os.getenv("KITE_ACCESS_TOKEN"):
        raise LiveUnavailable("Kite is not connected today. Use Connect Kite.")
    from data.kite_adapter import KiteFyersAdapter

    legs: List[LegKey] = []
    for t in open_trades:
        exp = date.fromisoformat(t["expiry_date"])
        legs.extend((exp, float(l["strike"]), l["option_type"]) for l in t["legs"])
    try:
        return KiteFyersAdapter().leg_quotes(legs)
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ in ("TokenException", "PermissionException"):
            raise LiveUnavailable("Kite token expired. Use Connect Kite.")
        raise LiveUnavailable(f"Could not fetch live prices ({exc.__class__.__name__})")
