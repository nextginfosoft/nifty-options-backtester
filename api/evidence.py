"""What the auto paper trader's results say about the strategy, set against what the backtests claimed.

A handful of paper trades cannot show an edge either way: one weekly trade has a spread of thousands of rupees, so the
page says "too early" until there are enough closed trades to put a real error bar on the average. It never turns a
small sample into a verdict.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Dict, List

MIN_EARLY = 10                 # below this: nothing can be said
MIN_READABLE = 30              # from here a 95% error bar is shown and a verdict is possible
PRECISION_TARGET = 500.0       # Rs per lot: how tightly we would like to know the average

# What the backtests said, from the repo's own notes (CLAUDE.md "Key Gotchas"). Reference only; nothing here is live.
REFERENCES: List[Dict[str, Any]] = [
    {"name": "Model-priced weekly backtest (Black-Scholes premiums)", "rs_per_lot": 334.0,
     "weight": "weak", "note": "The model overstates weekly credit (median 31 Rs/unit vs 21 real). Not evidence of an edge."},
    {"name": "Real-price backtest, 1.0x slippage (NSE F&O archive, 2019-2026)", "rs_per_lot": -177.0,
     "weight": "stronger", "note": "End-of-day settle marks, no bid/ask. Pre-registered verdict: not supported."},
    {"name": "Real-price backtest, 1.5x slippage", "rs_per_lot": -238.0, "weight": "stronger", "note": ""},
    {"name": "Real-price backtest, 2.0x slippage", "rs_per_lot": -348.0, "weight": "stronger", "note": ""},
]

CAVEATS = [
    "Paper fills use the quoted bid/ask but cannot show the queue, partial fills or market impact of a real order.",
    "The first trades were closed by hand, before the auto trader managed exits on its own.",
    "One lot of a short-option spread can lose several times what it usually wins, so a run of wins proves little.",
]


def _t_crit(n: int) -> float:
    return 2.05 if n < 30 else (2.0 if n < 60 else 1.96)


def series(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Chart data: cumulative net P&L with the drawdown from its running peak (both Rs, starting at 0), and per-trade bars."""
    ordered = sorted(trades, key=lambda t: (t.get("exit_date") or "", t.get("trade_id") or ""))
    cum = peak = 0.0
    curve = [{"label": "Start", "date": None, "cum_pnl": 0.0, "drawdown": 0.0}]
    bars = []
    for t in ordered:
        net = float(t["net_pnl"])
        cum += net
        peak = max(peak, cum)
        curve.append({"label": t.get("trade_id"), "date": t.get("exit_date"), "cum_pnl": round(cum, 2), "drawdown": round(cum - peak, 2)})
        bars.append({"trade_id": t.get("trade_id"), "date": t.get("exit_date"), "net": round(net, 2),
                     "per_lot": round(net / int(t["lots"]), 1)})
    return {"curve": curve, "trade_bars": bars}


def build(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    trades = [t for t in closed if t.get("net_pnl") is not None and t.get("lots")]
    n = len(trades)
    out: Dict[str, Any] = {"n": n, "references": REFERENCES, "caveats": CAVEATS,
                           "thresholds": {"early": MIN_EARLY, "readable": MIN_READABLE}, **series(trades)}
    if n == 0:
        return {**out, "verdict": {"code": "NO_DATA", "text": "No closed auto trades yet."}}

    pnls = [float(t["net_pnl"]) for t in trades]
    per_lot = [float(t["net_pnl"]) / int(t["lots"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    mean_lot = statistics.fmean(per_lot)
    sd_lot = statistics.stdev(per_lot) if n > 1 else None
    se = sd_lot / math.sqrt(n) if sd_lot is not None else None
    ci = [round(mean_lot - _t_crit(n) * se, 1), round(mean_lot + _t_crit(n) * se, 1)] if (se is not None and n >= MIN_READABLE) else None

    gross_win, gross_loss = sum(wins), -sum(losses)
    by_strategy: Dict[str, List[float]] = defaultdict(list)
    by_exit: Dict[str, int] = defaultdict(int)
    for t, p in zip(trades, pnls):
        by_strategy[t.get("strategy") or "?"].append(p)
        by_exit[t.get("exit_reason") or "?"] += 1

    needed = math.ceil((1.96 * sd_lot / PRECISION_TARGET) ** 2) if sd_lot else None

    if n < MIN_EARLY:
        verdict = {"code": "TOO_EARLY", "text": f"Too early to say anything: {n} closed trade(s). Needs at least {MIN_EARLY} just to look, and about {MIN_READABLE} for an error bar."}
    elif n < MIN_READABLE:
        verdict = {"code": "EARLY", "text": f"Still early: {n} closed trades. Treat the average as a rough hint only; an error bar appears at {MIN_READABLE}."}
    elif ci and ci[0] > 0:
        verdict = {"code": "POSITIVE", "text": "The average per lot is above zero after costs, with the 95% error bar also above zero. Compare with the real-price backtest before trusting it."}
    elif ci and ci[1] < 0:
        verdict = {"code": "NEGATIVE", "text": "The average per lot is below zero after costs, with the 95% error bar also below zero. This agrees with the real-price backtest."}
    else:
        verdict = {"code": "INCONCLUSIVE", "text": "The error bar includes zero, so these results cannot tell an edge from luck yet."}

    return {
        **out, "verdict": verdict,
        "wins": len(wins), "win_rate_pct": round(len(wins) / n * 100, 1),
        "total_net": round(sum(pnls), 2), "fees_total": round(sum(float(t.get("brokerage") or 0) for t in trades), 2),
        "avg_per_trade": round(statistics.fmean(pnls), 2),
        "avg_win": round(statistics.fmean(wins), 2) if wins else None,
        "avg_loss": round(statistics.fmean(losses), 2) if losses else None,
        "worst_trade": round(min(pnls), 2), "best_trade": round(max(pnls), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "per_lot": {"mean": round(mean_lot, 1), "stdev": round(sd_lot, 1) if sd_lot is not None else None, "ci95": ci},
        "trades_needed_for_precision": {"target_rs_per_lot": PRECISION_TARGET, "trades": needed},
        "by_strategy": [{"strategy": k, "n": len(v), "net": round(sum(v), 2)} for k, v in sorted(by_strategy.items())],
        "by_exit_reason": [{"reason": k, "n": v} for k, v in sorted(by_exit.items(), key=lambda kv: -kv[1])],
    }
