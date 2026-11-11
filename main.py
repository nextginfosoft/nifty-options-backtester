#!/usr/bin/env python3
"""
Nifty Options Backtester — Regime-Aware ML Pipeline

Pipeline: 17yr data → regime classifier → per-regime ML models → multi-expiry
          optimisation → backtest / live signal (shared select_optimal_entry flow)

Capital: 5L INR | Regimes: LOW_VOL, HIGH_VOL, CRASH, TRENDING
Strategies: put_credit_spread, put_credit_wide, broken_wing_butterfly,
            calendar_spread, ratio_put_spread

Modes:
    python main.py                                   # Backtest (default)
    python main.py --run-label "v3 baseline"         # Tag run in changelog
    python main.py --start 2019-01-01 --end 2026-04-08  # Custom date range
    python main.py --capital 1000000 --lots 25       # Override capital / lots
    python main.py --mode signal                     # Today's live trade signal
    python main.py --mode evolve                     # Evolve params per VIX regime
    python main.py --mode monitor                    # Monitor active trades + exits
    python main.py --mode validate                   # Walk-forward + permutation tests
    python main.py --mode add-trade  --trade-id ...  # Add trade to active journal
    python main.py --mode close-trade --trade-id .. --realized-pnl ...  # Close/archive trade
    python main.py --mode remove-trade --trade-id .. # Remove trade without archiving
    python main.py --mode list-trades                # List active trades

Every backtest run auto-appends results to:
    BACKTEST_CHANGELOG.md  — human-readable log (metrics, strategy breakdown)
    backtest_runs.jsonl    — machine-readable JSONL (one record per run)
"""

import argparse
import json
import logging
import os
import sys
import random
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["PYTHONHASHSEED"] = "42"
random.seed(42)

import pandas as pd
import numpy as np

np.random.seed(42)

from config import BacktestConfig
from training_config import TRAINING_FLOW
from data.market_data import MarketDataFetcher
from data.expiry_calendar import (
    get_best_expiry_for_dte, get_upcoming_expiries, format_expiry_label,
)
from utils.env_loader import load_env
from utils.market_clock import now_ist, is_market_hours, market_day_gate
from data.news_sentiment import (
    score_headlines, compute_price_action_sentiment,
    compute_geopolitical_risk_index,
)
from data.option_chain import (
    NSEOptionChainFetcher, find_best_iron_condor_strikes,
    find_liquid_strikes, print_oi_analysis,
)
from strategies.multi_strategy import (
    RegimeAdaptiveStrategy, PutCreditSpreadStrategy,
    BrokenWingButterflyStrategy,
    CalendarSpreadStrategy,
    IronCondorStrategy,
    STRATEGY_DISPLAY,
)
from strategies.expiry_selector import select_optimal_entry
from strategies.weekly_strategies import WeeklyIronCondor, WeeklyPutCreditSpread
from backtester.engine import SmartBacktestEngine
from signals.generator import SignalGenerator
from analysis.reporter import BacktestReporter
from models.trade_learner import TradeLearner, FeatureExtractor, STRATEGY_DISPLAY as ML_STRATEGY_DISPLAY
from models.trade_monitor import (
    ExitStrategyEngine, ActiveTrade, TradeLeg,
    load_active_trades, save_active_trades, add_trade, add_trade_from_legs,
    remove_trade, close_trade, load_actual_training_trades, parse_leg,
)

CHANGELOG_PATH = Path(__file__).parent / "BACKTEST_CHANGELOG.md"
RUNS_JSON_PATH = Path(__file__).parent / "backtest_runs.jsonl"


def _check_fyers_token_and_warn() -> tuple[bool, bool]:
    """
    Check if Fyers token is valid during market hours and show helpful warning if expired.
    
    Returns:
        (is_market_hours, token_valid) tuple
    """
    import os as _os

    load_env()

    _market_hours = is_market_hours(now_ist())

    if not _market_hours:
        return False, True
    
    # Market is open - check if token is valid
    try:
        from data.broker import fyersModel as _fm
        
        _client_id = _os.getenv("FYERS_CLIENT_ID")
        _token = _os.getenv("FYERS_ACCESS_TOKEN")
        
        if not _client_id or not _token:
            return True, False
        
        _fyers = _fm.FyersModel(client_id=_client_id, token=_token, log_path="")
        _resp = _fyers.quotes(data={"symbols": "NSE:NIFTY50-INDEX"})
        
        if _resp.get("s") == "ok":
            return True, True
        
        if _resp.get("code") == -15:
            print("\n" + "=" * 80)
            print("  ⚠️  FYERS TOKEN EXPIRED — LIVE PRICES UNAVAILABLE")
            print("=" * 80)
            print("\n  Your Fyers access token has expired. The monitor will use:")
            print("    • Stale data from Yahoo Finance (last market close)")
            print("    • Black-Scholes estimates for option prices")
            print("\n  TO GET LIVE PRICES DURING MARKET HOURS:")
            print("\n  1. Refresh your Fyers token by running:")
            print("     cd /Users/shivam.gupta/cursor/dsp-repos/nifty-options-backtester")
            print("     source .venv/bin/activate")
            print("     python scripts/generate_fyers_token.py")
            print("\n  2. This will:")
            print("     • Open Fyers authorization in your browser")
            print("     • Ask you to login and authorize the app")
            print("     • Redirect you to a URL with an auth_code")
            print("     • Paste the URL or just the auth_code back")
            print("     • Generate a fresh token and update your .env file")
            print("\n  3. Then run this monitor command again to get live prices!")
            print("\n" + "=" * 80)
            print("  Continuing with stale data / Black-Scholes estimates...\n")
            return True, False
            
    except Exception:
        pass
    
    return _market_hours, False


def _get_git_hash() -> str:
    """Return short git commit hash, or 'unknown' if not in a repo."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).parent,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _build_strategy_breakdown(trades) -> list[dict]:
    """Aggregate per-strategy stats from trade list."""
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for t in trades:
        name = getattr(t, "strategy", None) or getattr(t, "strategy_name", "unknown")
        buckets[name].append(t)

    rows = []
    for name, ts in sorted(buckets.items(), key=lambda x: -len(x[1])):
        pnls = [t.total_pnl for t in ts]
        wins = sum(1 for p in pnls if p > 0)
        rows.append({
            "strategy": name,
            "trades": len(ts),
            "win_rate_pct": round(100 * wins / len(ts), 1) if ts else 0,
            "total_pnl": round(sum(pnls)),
            "avg_pnl": round(sum(pnls) / len(ts)) if ts else 0,
        })
    return rows


def _summarise_sizing(engine) -> dict:
    """Aggregate sizing decisions from the engine for logging."""
    decisions = getattr(engine, "sizing_decisions", [])
    if not decisions:
        return {"count": 0, "avg_lots": 0, "avg_base_lots": 0, "avg_confidence_scale": 0,
                "avg_regime_scale": 0, "avg_dd_scale": 0}
    n = len(decisions)
    return {
        "count": n,
        "avg_lots": round(sum(d.lots for d in decisions) / n, 1),
        "avg_base_lots": round(sum(d.base_lots for d in decisions) / n, 1),
        "avg_confidence_scale": round(sum(d.confidence_scale for d in decisions) / n, 3),
        "avg_regime_scale": round(sum(d.regime_scale for d in decisions) / n, 3),
        "avg_dd_scale": round(sum(d.dd_scale for d in decisions) / n, 3),
    }


def _summarise_monthly_diagnostics(result, engine) -> dict:
    diagnostics = dict(getattr(result, "diagnostics", {}) or {})
    diagnostics.update({
        "ml_entries": getattr(engine, "ml_entry_count", 0),
        "ml_skips": getattr(engine, "ml_skip_count", 0),
        "circuit_breaker_blocks": getattr(engine, "circuit_breaker_blocks", 0),
        "smart_exits": getattr(engine, "ml_exit_count", 0),
        "rule_exits": getattr(engine, "rule_exit_count", 0),
        "multi_expiry_selections": getattr(engine, "multi_expiry_selections", 0),
    })
    return diagnostics


def log_backtest_run(
    result,
    engine,
    config,
    run_label: str = "",
    is_combined: bool = False,
) -> None:
    """Append a structured entry to BACKTEST_CHANGELOG.md and backtest_runs.jsonl."""
    now = datetime.now()
    git_hash = _get_git_hash()
    weekly_trade_stats = _build_weekly_trade_stats(getattr(result, "all_trades", []))

    # Handle both SmartBacktestEngine and CombinedBacktestEngine results
    if is_combined:
        strategy_rows = []  # Combined engine doesn't have per-strategy breakdown yet
        engine_stats = {
            "monthly_trades": result.monthly_trades,
            "weekly_trades": result.weekly_trades,
            "cross_track_dd_blocks": result.cross_track_dd_blocks,
            "weekly_vix_gate_blocks": result.weekly_vix_gate_blocks,
            "weekly_open_cap_blocks": result.weekly_open_cap_blocks,
            "emergency_weekly_exits": result.emergency_weekly_exits,
            "capital_utilization_pct": result.capital_utilization_pct,
            "dd_kill_switch_activations": result.dd_kill_switch_activations,
            "dd_kill_weekly_closes": result.dd_kill_weekly_closes,
            "dd_kill_entries_blocked": result.dd_kill_entries_blocked,
            "dd_lock_releases": result.dd_lock_releases,
            "event_calendar_blocks": result.event_calendar_blocks,
            "weekly_etl_skips": result.weekly_etl_skips,
            "weekly_etl_gate_skips": result.weekly_etl_gate_skips,
            "weekly_fill_dte_drops": result.weekly_fill_dte_drops,
            "weekly_expiry_past_drops": result.weekly_expiry_past_drops,
            "weekly_regime_skips": result.weekly_regime_skips,
            "weekly_risk_score_avg": result.weekly_risk_score_avg,
            "weekly_dynamic_exit_count": result.weekly_dynamic_exit_count,
            "weekly_dynamic_exit_reasons": result.weekly_dynamic_exit_reasons,
            "weekly_regime_distribution": result.weekly_regime_distribution,
            "weekly_trade_distribution": weekly_trade_stats["distribution"],
            "weekly_top_winners": weekly_trade_stats["top_winners"],
            "weekly_top_losers": weekly_trade_stats["top_losers"],
        }
    else:
        strategy_rows = _build_strategy_breakdown(result.trades)
        monthly_diag = _summarise_monthly_diagnostics(result, engine)
        engine_stats = {
            "ml_entries": monthly_diag.get("ml_entries", 0),
            "ml_skips": monthly_diag.get("ml_skips", 0),
            "circuit_breaker_blocks": monthly_diag.get("circuit_breaker_blocks", 0),
            "smart_exits": monthly_diag.get("smart_exits", 0),
            "rule_exits": monthly_diag.get("rule_exits", 0),
            "multi_expiry_selections": monthly_diag.get("multi_expiry_selections", 0),
            "capital_utilization_pct": monthly_diag.get("capital_utilization_pct", 0),
            "flat_days_pct": monthly_diag.get("flat_days_pct", 0),
            "avg_lots": monthly_diag.get("avg_lots", 0),
            "avg_lots_by_regime": monthly_diag.get("avg_lots_by_regime", {}),
            "avg_sizing_components": monthly_diag.get("avg_sizing_components", {}),
            "gate_rejections": monthly_diag.get("gate_rejections", {}),
            "opportunity_waterfall": monthly_diag.get("opportunity_waterfall", {}),
        }

    run_record = {
        "timestamp": now.isoformat(),
        "git_hash": git_hash,
        "label": run_label,
        "mode": "backtest-combined" if is_combined else "backtest",
        "params": {
            "start": str(config.start_date),
            "end": str(config.end_date),
            "capital": config.initial_capital,
            "max_lots": config.max_lots,
        },
        "metrics": {
            "cagr_pct": round(result.cagr_pct, 2),
            "total_pnl": round(result.total_pnl),
            "sharpe": round(result.sharpe_ratio, 2),
            "sortino": round(result.sortino_ratio, 2),
            "calmar": round(result.calmar_ratio, 2),
            "max_drawdown_pct": round(result.max_drawdown_pct, 1),
            "win_rate_pct": round(result.win_rate, 1),
            "total_trades": result.total_trades,
            "profit_factor": round(result.profit_factor, 2),
            "best_trade": round(result.best_trade_pnl),
            "worst_trade": round(result.worst_trade_pnl),
        },
        "engine_stats": engine_stats,
        "strategy_breakdown": strategy_rows,
    }
    
    # Add SmartBacktestEngine-specific metrics if not combined
    if not is_combined:
        run_record["metrics"]["total_return_pct"] = round(result.total_return_pct, 2)
        run_record["metrics"]["avg_pnl_per_trade"] = round(result.avg_pnl_per_trade)
        run_record["metrics"]["max_consec_wins"] = result.max_consecutive_wins
        run_record["metrics"]["max_consec_losses"] = result.max_consecutive_losses
    else:
        run_record["metrics"]["monthly_pnl"] = round(result.monthly_pnl)
        run_record["metrics"]["weekly_pnl"] = round(result.weekly_pnl)
        run_record["metrics"]["monthly_win_rate"] = round(result.monthly_win_rate, 1)
        run_record["metrics"]["weekly_win_rate"] = round(result.weekly_win_rate, 1)

    with open(RUNS_JSON_PATH, "a") as f:
        f.write(json.dumps(run_record) + "\n")

    _append_changelog_entry(run_record, now, git_hash, run_label)
    print(f"\n  [Auto-logged] Run saved to backtest_runs.jsonl and BACKTEST_CHANGELOG.md")


def _append_changelog_entry(rec: dict, now, git_hash: str, label: str) -> None:
    """Append a human-readable markdown section to the changelog."""
    m = rec["metrics"]
    es = rec["engine_stats"]
    p = rec["params"]
    rows = rec["strategy_breakdown"]

    run_num = 1
    if CHANGELOG_PATH.exists():
        import re
        for line in open(CHANGELOG_PATH):
            m_run = re.match(r"^## Run #(\d+)", line)
            if m_run:
                run_num = max(run_num, int(m_run.group(1)) + 1)
    elif RUNS_JSON_PATH.exists():
        run_num = sum(1 for _ in open(RUNS_JSON_PATH)) + 1

    title_parts = [f"Run #{run_num}"]
    if label:
        title_parts.append(label)
    if rec.get("mode") == "backtest-combined":
        title_parts.append("[COMBINED]")
    title = " — ".join(title_parts)

    lines = [
        f"\n---\n",
        f"## {title}\n",
        f"**Date**: {now.strftime('%Y-%m-%d %H:%M')}  \n",
        f"**Git**: `{git_hash}`  \n",
        f"**Params**: {p['start']} to {p['end']} | Capital ₹{p['capital']:,.0f} | "
        f"Lots {p['max_lots']}\n",
        f"\n### Key Metrics\n",
        f"| Metric | Value |\n",
        f"|--------|------:|\n",
        f"| CAGR | {m['cagr_pct']:.2f}% |\n",
    ]
    
    # Handle SmartBacktest vs Combined metrics
    if 'total_return_pct' in m:
        lines.append(f"| Total Return | {m['total_return_pct']:.1f}% |\n")
    if 'monthly_pnl' in m:
        lines.append(f"| Monthly P&L | ₹{m['monthly_pnl']:,} |\n")
        lines.append(f"| Weekly P&L | ₹{m['weekly_pnl']:,} |\n")
        lines.append(f"| Monthly Win Rate | {m['monthly_win_rate']:.1f}% |\n")
        lines.append(f"| Weekly Win Rate | {m['weekly_win_rate']:.1f}% |\n")
    
    lines.extend([
        f"| Total P&L | ₹{m['total_pnl']:,} |\n",
        f"| Sharpe | {m['sharpe']:.2f} |\n",
        f"| Sortino | {m['sortino']:.2f} |\n",
        f"| Calmar | {m['calmar']:.2f} |\n",
        f"| Max Drawdown | {m['max_drawdown_pct']:.1f}% |\n",
        f"| Win Rate | {m['win_rate_pct']:.1f}% |\n",
        f"| Total Trades | {m['total_trades']} |\n",
        f"| Profit Factor | {m['profit_factor']:.2f} |\n",
    ])
    
    # Add optional metrics only if they exist
    if 'avg_pnl_per_trade' in m:
        lines.append(f"| Avg P&L/Trade | ₹{m['avg_pnl_per_trade']:,} |\n")
    if 'best_trade' in m:
        lines.append(f"| Best Trade | ₹{m['best_trade']:,} |\n")
        lines.append(f"| Worst Trade | ₹{m['worst_trade']:,} |\n")
    if 'max_consec_wins' in m:
        lines.append(f"| Max Consec Wins | {m['max_consec_wins']} |\n")
        lines.append(f"| Max Consec Losses | {m['max_consec_losses']} |\n")
    
    lines.append(f"\n### Engine Stats\n")
    lines.append(f"| Stat | Count |\n")
    lines.append(f"|------|------:|\n")
    
    # Handle SmartBacktest vs Combined engine stats
    if 'ml_entries' in es:
        lines.extend([
            f"| ML Entries | {es['ml_entries']} |\n",
            f"| ML Skips | {es['ml_skips']} |\n",
            f"| Circuit Breaker Blocks | {es['circuit_breaker_blocks']} |\n",
            f"| Smart Exits | {es['smart_exits']} |\n",
            f"| Rule Exits | {es['rule_exits']} |\n",
            f"| Multi-expiry Selections | {es['multi_expiry_selections']} |\n",
        ])
    elif 'monthly_trades' in es:  # Combined engine stats
        lines.extend([
            f"| Monthly Trades | {es['monthly_trades']} |\n",
            f"| Weekly Trades | {es['weekly_trades']} |\n",
            f"| Cross-track DD Blocks | {es['cross_track_dd_blocks']} |\n",
            f"| Weekly VIX Gate Blocks | {es['weekly_vix_gate_blocks']} |\n",
            f"| Weekly Open Cap Blocks | {es['weekly_open_cap_blocks']} |\n",
            f"| Emergency Weekly Exits | {es['emergency_weekly_exits']} |\n",
            f"| Capital Utilization | {es['capital_utilization_pct']:.1f}% |\n",
        ])
        weekly_dist = es.get("weekly_trade_distribution", {})
        if weekly_dist:
            lines.append(f"\n### Weekly Trade Distribution\n")
            lines.append(f"| Bucket | Count | Share | Avg P&L |\n")
            lines.append(f"|--------|------:|------:|--------:|\n")
            for row in weekly_dist.get("buckets", []):
                lines.append(
                    f"| {row['label']} | {row['count']} | {row['share_pct']:.1f}% | ₹{row['avg_pnl']:,} |\n"
                )
            lines.append(
                f"| Median | - | - | ₹{weekly_dist.get('median_pnl', 0):,} |\n"
            )
            lines.append(
                f"| P10 / P90 | - | - | ₹{weekly_dist.get('p10_pnl', 0):,} / ₹{weekly_dist.get('p90_pnl', 0):,} |\n"
            )

        if es.get("weekly_top_winners"):
            lines.append(f"\n### Weekly Top Winners\n")
            lines.append(f"| Exit Date | Strategy | Exit Reason | P&L |\n")
            lines.append(f"|-----------|----------|-------------|----:|\n")
            for row in es["weekly_top_winners"]:
                lines.append(
                    f"| {row['exit_date']} | {row['strategy']} | {row['exit_reason']} | ₹{row['total_pnl']:,} |\n"
                )

        if es.get("weekly_top_losers"):
            lines.append(f"\n### Weekly Top Losers\n")
            lines.append(f"| Exit Date | Strategy | Exit Reason | P&L |\n")
            lines.append(f"|-----------|----------|-------------|----:|\n")
            for row in es["weekly_top_losers"]:
                lines.append(
                    f"| {row['exit_date']} | {row['strategy']} | {row['exit_reason']} | ₹{row['total_pnl']:,} |\n"
                )

    sz = rec.get("sizing_stats", {})
    if sz and sz.get("count", 0) > 0:
        lines.append(f"\n### Position Sizing (3-layer)\n")
        lines.append(f"| Metric | Value |\n")
        lines.append(f"|--------|------:|\n")
        lines.append(f"| Sizing Decisions | {sz['count']} |\n")
        lines.append(f"| Avg Final Lots | {sz['avg_lots']} |\n")
        lines.append(f"| Avg Base Lots | {sz['avg_base_lots']} |\n")
        lines.append(f"| Avg Confidence Scale | {sz['avg_confidence_scale']}x |\n")
        lines.append(f"| Avg Regime Scale | {sz['avg_regime_scale']}x |\n")
        lines.append(f"| Avg DD Scale | {sz['avg_dd_scale']}x |\n")

    if rows:
        lines.append(f"\n### Strategy Breakdown\n")
        lines.append(f"| Strategy | Trades | Win Rate | Total P&L | Avg P&L |\n")
        lines.append(f"|----------|-------:|---------:|----------:|--------:|\n")
        for r in rows:
            lines.append(
                f"| {r['strategy']} | {r['trades']} | {r['win_rate_pct']:.1f}% | "
                f"₹{r['total_pnl']:,} | ₹{r['avg_pnl']:,} |\n"
            )

    lines.append(f"\n")

    with open(CHANGELOG_PATH, "a") as f:
        f.writelines(lines)


def _normalize_trade_date(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _build_weekly_trade_stats(all_trades: list[dict], top_n: int = 5) -> dict:
    weekly_trades = [t for t in all_trades if t.get("track") == "weekly"]
    if not weekly_trades:
        return {"distribution": {}, "top_winners": [], "top_losers": []}

    pnls = np.array([float(t.get("total_pnl", 0.0)) for t in weekly_trades], dtype=float)
    total = len(weekly_trades)

    def _bucket(label: str, mask) -> dict:
        bucket_trades = [t for t, keep in zip(weekly_trades, mask) if keep]
        bucket_pnls = [t["total_pnl"] for t in bucket_trades]
        return {
            "label": label,
            "count": len(bucket_trades),
            "share_pct": round(len(bucket_trades) / total * 100, 1) if total else 0.0,
            "avg_pnl": round(float(np.mean(bucket_pnls))) if bucket_pnls else 0,
        }

    bucket_rows = [
        _bucket("Large Loss (< -10k)", pnls < -10000),
        _bucket("Loss (-10k to 0)", (pnls >= -10000) & (pnls < 0)),
        _bucket("Small Win (0 to 10k)", (pnls >= 0) & (pnls < 10000)),
        _bucket("Medium Win (10k to 25k)", (pnls >= 10000) & (pnls < 25000)),
        _bucket("Large Win (>= 25k)", pnls >= 25000),
    ]

    def _serialize_trade(trade: dict) -> dict:
        return {
            "entry_date": _normalize_trade_date(trade.get("entry_date")),
            "exit_date": _normalize_trade_date(trade.get("exit_date")),
            "strategy": trade.get("strategy", ""),
            "exit_reason": trade.get("exit_reason", ""),
            "holding_days": int(trade.get("holding_days", 0)),
            "total_pnl": round(float(trade.get("total_pnl", 0.0))),
        }

    top_winners = [
        _serialize_trade(t)
        for t in sorted(
            [t for t in weekly_trades if t.get("total_pnl", 0) > 0],
            key=lambda t: t.get("total_pnl", 0),
            reverse=True,
        )[:top_n]
    ]
    top_losers = [
        _serialize_trade(t)
        for t in sorted(
            [t for t in weekly_trades if t.get("total_pnl", 0) < 0],
            key=lambda t: t.get("total_pnl", 0),
        )[:top_n]
    ]

    distribution = {
        "count": total,
        "median_pnl": round(float(np.median(pnls))),
        "p10_pnl": round(float(np.percentile(pnls, 10))),
        "p90_pnl": round(float(np.percentile(pnls, 90))),
        "buckets": bucket_rows,
    }
    return {
        "distribution": distribution,
        "top_winners": top_winners,
        "top_losers": top_losers,
    }


def run_backtest(config: BacktestConfig, target_dte: int = 21, run_label: str = "") -> None:
    """
    Full regime-aware backtest pipeline (default mode).

    Pipeline:
      1. Fetch 17yr market data (2009–2026)
      2. Walk-forward split: 60% train / 40% out-of-sample
      3. Load cached walk-forward models only
      4. Run backtest with regime-aware ML entry if caches exist, otherwise
         rule-based fallback + smart exits + compounding
      6. Generate full report
    """
    from backtester.walk_forward import WalkForwardManager

    print("\n" + "=" * 80)
    print("  NIFTY OPTIONS BACKTESTER — REGIME-AWARE ML PIPELINE")
    print(f"  Period: {config.start_date} to {config.end_date}")
    print(f"  Capital: ₹{config.initial_capital:,.0f} | Max Lots: {config.max_lots}")
    print("=" * 80)

    print("\n[1/5] Fetching market data...")
    fetcher = MarketDataFetcher(config.start_date, config.end_date)
    data = fetcher.build_combined_dataset()
    print(f"  Loaded {len(data)} trading days")
    print(f"  Nifty range: {data['nifty_close'].min():.0f} – {data['nifty_close'].max():.0f}")
    if "vix" in data.columns:
        print(f"  VIX range: {data['vix'].min():.1f} – {data['vix'].max():.1f}")

    train_cutoff = int(len(data) * 0.6)
    train_data = data.iloc[:train_cutoff]
    train_end = train_data.index[-1]
    print(f"  Walk-forward split: train {train_cutoff} days (to {train_end.date()}) | "
          f"test {len(data) - train_cutoff} days")

    print(f"\n[2/5] Building walk-forward model schedule (cache-only)...")
    wf_manager = WalkForwardManager(
        data,
        monthly_config=config,
        include_monthly=True,
        train_on_cache_miss=False,
        retrain_interval_bars=21,
    )
    print(f"  Walk-forward windows: {len(wf_manager.windows)}")

    # --- Run backtest ---
    entry_threshold = config.monthly_entry_threshold
    print(f"\n[3/5] Running backtest (cached ML when available + multi-expiry + smart exits + compounding)...")
    print(f"  Entry model: v4 | quality threshold: {entry_threshold}")
    strategy = RegimeAdaptiveStrategy(
        lots=config.max_lots,
        lot_size=config.lot_size,
        allow_bwb=not config.monthly_disable_bwb,
    )
    engine = SmartBacktestEngine(
        strategy, data, config, target_dte,
        exit_engine=None,
        entry_model=None,
        entry_threshold=entry_threshold,
        compound=True,
        walk_forward_manager=wf_manager,
    )
    result = engine.run()

    print(f"  Trades: {result.total_trades} | Win Rate: {result.win_rate:.0f}% | "
          f"P&L ₹{result.total_pnl:,.0f} | CAGR {result.cagr_pct:.2f}%")
    print(f"  Sharpe: {result.sharpe_ratio:.2f} | Max DD: {result.max_drawdown_pct:.1f}% | "
          f"Calmar: {result.calmar_ratio:.2f}")
    print(f"  ML entries: {engine.ml_entry_count} | ML skips: {engine.ml_skip_count} | "
          f"Circuit breaker blocks: {engine.circuit_breaker_blocks}")
    print(f"  Smart exits: {engine.ml_exit_count} | Rule exits: {engine.rule_exit_count}")
    print(f"  Multi-expiry selections: {engine.multi_expiry_selections}")
    monthly_diag = _summarise_monthly_diagnostics(result, engine)
    print(f"  Capital utilization: {monthly_diag.get('capital_utilization_pct', 0):.1f}% | "
          f"Flat days: {monthly_diag.get('flat_days_pct', 0):.1f}%")
    print(f"  Avg lots: {monthly_diag.get('avg_lots', 0):.1f} | "
          f"Avg sizing: base {monthly_diag.get('avg_sizing_components', {}).get('base_lots', 0):.2f}, "
          f"regime {monthly_diag.get('avg_sizing_components', {}).get('regime_scale', 0):.3f}, "
          f"dd {monthly_diag.get('avg_sizing_components', {}).get('dd_scale', 0):.3f}, "
          f"conf {monthly_diag.get('avg_sizing_components', {}).get('confidence_scale', 0):.3f}")
    gate_rejections = monthly_diag.get("gate_rejections", {})
    if gate_rejections:
        print(f"  Gate rejections: {gate_rejections}")
    waterfall = monthly_diag.get("opportunity_waterfall", {})
    if waterfall:
        print(f"  Opportunity waterfall: {waterfall}")

    # --- Full report ---
    print(f"\n[4/5] Full report...")
    reporter = BacktestReporter(result, data)
    reporter.print_summary()

    try:
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report.html")
        reporter.plot_equity_curve(html_path)
    except Exception as e:
        print(f"\n  Could not generate chart: {e}")

    log_backtest_run(result, engine, config, run_label=run_label)

    return result, data


def run_backtest_combined(config: BacktestConfig, target_dte: int = 21, run_label: str = "",
                          weekly_start: str | None = None) -> None:
    """
    Combined monthly + weekly backtest pipeline using final evolve artifacts only.

    Pipeline:
      1. Fetch 17yr market data (2009–2026)
      2. Load final evolved entry / exit / weekly-risk caches
      3. Run CombinedBacktestEngine with 50% monthly / 50% weekly budget split
      4. Generate full report with per-track and combined metrics
    """
    from config import WeeklyBacktestConfig
    from backtester.combined_engine import CombinedBacktestEngine
    from strategies.multi_strategy import RegimeAdaptiveStrategy
    from models.regime_aware_learner import RegimeAwareLearner
    from models.trade_monitor import ExitStrategyEngine
    from models.weekly_risk_engine import load_risk_engine

    print("\n" + "=" * 80)
    print("  COMBINED MONTHLY + WEEKLY BACKTEST — ML PIPELINE")
    print(f"  Period: {config.start_date} to {config.end_date}")
    print(f"  Budget: Monthly 50% (₹{config.initial_capital * 0.50:,.0f}) + Weekly 50% (₹{config.initial_capital * 0.50:,.0f})")
    print("=" * 80)

    print("\n[1/5] Fetching market data...")
    fetcher = MarketDataFetcher(config.start_date, config.end_date)
    data = fetcher.build_combined_dataset()
    if getattr(fetcher, 'integrity_summary', ''):  # set by build only after its macro check passed
        print(f"  Cache integrity OK: {getattr(fetcher, 'integrity_summary', '')}")
    print(f"  Loaded {len(data)} trading days")

    print(f"\n[2/5] Loading final evolve caches (cache-only)...")
    wc = WeeklyBacktestConfig(weekly_trade_start_date=weekly_start)
    exit_engine = None
    entry_model = None
    weekly_risk_engine = None
    try:
        exit_engine = ExitStrategyEngine.load(data)
    except FileNotFoundError as e:
        print(f"  WARNING: exit model cache missing — {e}")
        print("  Run `python main.py --mode evolve` to rebuild. Running rule-based exits.")
    except Exception as e:
        print(f"  WARNING: exit model cache incompatible ({type(e).__name__}: {e})")
        print("  Running with rule-based exits only.")
    try:
        entry_model = RegimeAwareLearner.load(data)
    except FileNotFoundError as e:
        print(f"  WARNING: entry model cache missing — {e}")
        print("  Run `python main.py --mode evolve` to rebuild. Running rule-based entries.")
    except Exception as e:
        print(f"  WARNING: entry model cache incompatible ({type(e).__name__}: {e})")
        print("  Running with rule-based entries only.")
    try:
        weekly_risk_engine = load_risk_engine(data)
    except FileNotFoundError as e:
        print(f"  WARNING: weekly risk engine cache missing — {e}")
    except Exception as e:
        print(f"  WARNING: weekly risk engine cache incompatible ({type(e).__name__}: {e})")

    loaded = [n for n, v in [("exit", exit_engine), ("entry", entry_model), ("weekly-risk", weekly_risk_engine)] if v is not None]
    print(f"  Artifacts loaded: {', '.join(loaded) if loaded else 'none (rule-based mode)'}")

    # ── Run combined backtest ──
    _monthly_pct = 0.50   # v8 (2026-08-23): 50/50 baseline; exp1 75/25 showed ETL gate too strict → retrain
    _weekly_pct  = 0.50   # v8 (2026-08-23): see etl_skip_multiplier in WeeklyBacktestConfig for tuning
    print(f"\n[3/5] Running combined backtest "
          f"(monthly {_monthly_pct*100:.0f}% + weekly {_weekly_pct*100:.0f}%)...")
    strategy = RegimeAdaptiveStrategy(
        lots=config.max_lots,
        lot_size=config.lot_size,
        allow_bwb=not config.monthly_disable_bwb,
    )

    from backtester.production_rules import ProductionRulesConfig

    engine = CombinedBacktestEngine(
        data=data,
        monthly_config=config,
        weekly_config=wc,
        monthly_strategy=strategy,
        exit_engine=exit_engine,
        entry_model=entry_model,
        weekly_risk_engine=weekly_risk_engine,
        entry_threshold=config.monthly_entry_threshold,
        monthly_budget_pct=_monthly_pct,
        weekly_budget_pct=_weekly_pct,
        cross_track_dd_pct=0.15,
        vix_simultaneous_cap=25.0,
        production_rules=ProductionRulesConfig(
            dd_kill_pct=0.20,
            dd_recovery_pct=0.16,
            dd_cooldown_days=3,
            block_events=True,
            enforce_no_naked=True,
            enforce_hedge_ratio=True,
            max_spread_width_pct=3.0,
            # Opt-in lock release (Phase 2.2). 0 = locks stay absorbing (current behaviour).
            cross_dd_rearm_days=0,
            kill_switch_rearm_days=0,
        ),
        walk_forward_manager=None,
    )
    
    result = engine.run()
    weekly_trade_stats = _build_weekly_trade_stats(result.all_trades)

    # ── Print results ──
    print(f"\n  {'=' * 80}")
    print(f"  COMBINED BACKTEST RESULTS")
    print(f"  {'=' * 80}")
    print(f"\n  Monthly Track ({_monthly_pct*100:.0f}% budget):")
    print(f"    Trades: {result.monthly_trades} | Win Rate: {result.monthly_win_rate:.0f}% | "
          f"P&L: ₹{result.monthly_pnl:,.0f} | Avg Hold: {result.avg_holding_days_monthly:.0f}d")

    print(f"\n  Weekly Track ({_weekly_pct*100:.0f}% budget):")
    print(f"    Trades: {result.weekly_trades} | Win Rate: {result.weekly_win_rate:.0f}% | "
          f"P&L: ₹{result.weekly_pnl:,.0f} | Avg Hold: {result.avg_holding_days_weekly:.0f}d")
    
    print(f"\n  Combined Results:")
    print(f"    Total Trades: {result.total_trades} | Overall Win Rate: {result.win_rate:.0f}%")
    print(f"    Total P&L: ₹{result.total_pnl:,.0f} | CAGR: {result.cagr_pct:.2f}%")
    print(f"    Max DD: {result.max_drawdown_pct:.1f}% | Sharpe: {result.sharpe_ratio:.2f} | "
          f"Calmar: {result.calmar_ratio:.2f}")
    print(f"    Profit Factor: {result.profit_factor:.2f}")
    
    print(f"\n  Risk Management Gates:")
    print(f"    Cross-track DD blocks: {result.cross_track_dd_blocks}")
    print(f"    Weekly VIX gate blocks: {result.weekly_vix_gate_blocks}")
    print(f"    Weekly open position cap blocks: {result.weekly_open_cap_blocks}")
    print(f"    Emergency weekly exits: {result.emergency_weekly_exits}")
    
    print(f"\n  Weekly Risk Engine:")
    print(f"    ETL > credit skips: {result.weekly_etl_gate_skips}")
    print(f"    Fill-time DTE drops (expiry outside [min,max] at fill): {result.weekly_fill_dte_drops}")
    if result.weekly_expiry_past_drops:
        print(f"    Expiry-already-passed drops at fill: {result.weekly_expiry_past_drops}")
    print(f"    Regime skips: {result.weekly_regime_skips}")
    print(f"    Avg risk score: {result.weekly_risk_score_avg:.3f}")
    print(f"    Dynamic exits: {result.weekly_dynamic_exit_count}")
    if result.weekly_dynamic_exit_reasons:
        for reason, count in sorted(result.weekly_dynamic_exit_reasons.items(),
                                     key=lambda x: -x[1]):
            print(f"      {reason}: {count}")
    if result.weekly_regime_distribution:
        print(f"    Regime distribution:")
        for regime, count in sorted(result.weekly_regime_distribution.items(),
                                     key=lambda x: -x[1]):
            print(f"      {regime}: {count}")

    if weekly_trade_stats["distribution"]:
        dist = weekly_trade_stats["distribution"]
        print(f"\n  Weekly Trade Distribution:")
        print(f"    Median P&L: ₹{dist['median_pnl']:,} | P10: ₹{dist['p10_pnl']:,} | P90: ₹{dist['p90_pnl']:,}")
        for row in dist["buckets"]:
            print(
                f"    {row['label']}: {row['count']} trades ({row['share_pct']:.1f}%), "
                f"avg P&L ₹{row['avg_pnl']:,}"
            )

    if weekly_trade_stats["top_winners"]:
        print(f"\n  Weekly Top Winners:")
        for trade in weekly_trade_stats["top_winners"][:3]:
            print(
                f"    {trade['exit_date']} | {trade['strategy']} | "
                f"{trade['exit_reason']} | ₹{trade['total_pnl']:+,}"
            )

    if weekly_trade_stats["top_losers"]:
        print(f"\n  Weekly Top Losers:")
        for trade in weekly_trade_stats["top_losers"][:3]:
            print(
                f"    {trade['exit_date']} | {trade['strategy']} | "
                f"{trade['exit_reason']} | ₹{trade['total_pnl']:+,}"
            )

    print(f"\n  Capital Utilization: {result.capital_utilization_pct:.1f}%")

    # Monthly entry funnel — shows which gate blocked how many days
    print(engine._monthly_funnel_report(
        start_date=config.start_date,
        end_date=config.end_date,
    ))

    print(f"\n  {'=' * 80}\n")

    log_backtest_run(result, engine, config, run_label=run_label, is_combined=True)

    return result, data


def _generate_sim_trades(market_data, strategy, config):
    """Rolling-window simulated trades for ML training; filtered to strategies enabled on ``strategy``."""
    from backtester.rolling_simulator import RollingWindowSimulator, SimConfig
    sim_cfg = SimConfig(
        lots=config.max_lots,
        lot_size=config.lot_size,
        entry_every_n_days=TRAINING_FLOW.monthly_entry_every_n_days,
    )
    sim = RollingWindowSimulator(market_data, config=sim_cfg)
    trades = sim.simulate_all()
    allowed = set(strategy._all_strategies.keys())
    return [t for t in trades if getattr(t, "strategy", None) in allowed]


def run_ablation(config, args):
    """v4: Ablation study — measure contribution of each strategy and ML exit."""
    print("\n" + "=" * 70)
    print("  ABLATION STUDY — v4")
    print("=" * 70)

    fetcher = MarketDataFetcher(config.start_date, config.end_date)
    market_data = fetcher.build_combined_dataset()
    if market_data.empty:
        print("  No data. Exiting.")
        return

    model_version = "v4"
    entry_threshold = 0.48

    strategies_to_test = [
        "iron_condor", "put_credit_spread", "calendar_spread",
        "broken_wing_butterfly",
    ]

    # Full run baseline
    print("\n  [Baseline] All strategies enabled...")
    from backtester.engine import SmartBacktestEngine
    from strategies.multi_strategy import RegimeAdaptiveStrategy
    from models.regime_aware_learner import RegimeAwareLearner

    strategy = RegimeAdaptiveStrategy(
        lots=config.max_lots,
        lot_size=config.lot_size,
        allow_bwb=not config.monthly_disable_bwb,
    )

    learner = RegimeAwareLearner(model_version=model_version)
    trades_sim = _generate_sim_trades(market_data, strategy, config)
    learner.train(trades_sim, market_data, verbose=False)

    baseline_engine = SmartBacktestEngine(
        strategy, market_data, config, args.dte,
        exit_engine=None,
        entry_model=learner,
        entry_threshold=entry_threshold,
        compound=True,
    )
    baseline_result = baseline_engine.run()
    baseline_cagr = baseline_result.cagr_pct
    baseline_sharpe = baseline_result.sharpe_ratio
    baseline_trades = baseline_result.total_trades

    print(f"  Baseline: CAGR={baseline_cagr:.2f}% | Sharpe={baseline_sharpe:.2f} | Trades={baseline_trades}")

    # Drop each strategy one at a time
    print("\n  [Drop-one-out] Testing strategy contributions...\n")
    print(f"  {'Strategy':<28} {'CAGR':>8} {'Δ CAGR':>8} {'Sharpe':>8} {'Trades':>8}")
    print("  " + "-" * 62)

    for drop in strategies_to_test:
        strategy_d = RegimeAdaptiveStrategy(
            lots=config.max_lots,
            lot_size=config.lot_size,
            allow_bwb=not config.monthly_disable_bwb,
        )
        # Disable the dropped strategy
        if drop in strategy_d._all_strategies:
            del strategy_d._all_strategies[drop]

        learner_d = RegimeAwareLearner(model_version=model_version)
        trades_d = _generate_sim_trades(market_data, strategy_d, config)
        learner_d.train(trades_d, market_data, verbose=False)

        engine_d = SmartBacktestEngine(
            strategy_d, market_data, config, args.dte,
            exit_engine=None,
            entry_model=learner_d,
            entry_threshold=entry_threshold,
            compound=True,
        )
        result_d = engine_d.run()

        delta_cagr = result_d.cagr_pct - baseline_cagr
        print(f"  w/o {drop:<23} {result_d.cagr_pct:>7.2f}% {delta_cagr:>+7.2f}% {result_d.sharpe_ratio:>7.2f} {result_d.total_trades:>8}")

    print("\n  (Negative Δ CAGR = strategy was contributing; positive = strategy was dragging)")


def run_stress_test(config, args):
    """v4: Stress test — replay crisis periods with conservative fills."""
    print("\n" + "=" * 70)
    print("  STRESS TEST — v4")
    print("=" * 70)

    stress_periods = [
        ("COVID Crash", date(2020, 2, 1), date(2020, 5, 31)),
        ("2022 Rate Shock", date(2022, 1, 1), date(2022, 6, 30)),
        ("Oct-Nov 2024 Drawdown", date(2024, 9, 1), date(2024, 12, 31)),
        ("2025 Tariff Turmoil", date(2025, 2, 1), date(2025, 5, 31)),
    ]

    from backtester.engine import SmartBacktestEngine
    from strategies.multi_strategy import RegimeAdaptiveStrategy
    from models.regime_aware_learner import RegimeAwareLearner
    from config import CostModel

    model_version = "v4"
    entry_threshold = 0.48

    # Train on full history first
    fetcher = MarketDataFetcher(config.start_date, config.end_date)
    full_data = fetcher.build_combined_dataset()
    if full_data.empty:
        print("  No data. Exiting.")
        return

    strategy = RegimeAdaptiveStrategy(
        lots=config.max_lots,
        lot_size=config.lot_size,
        allow_bwb=not config.monthly_disable_bwb,
    )
    learner = RegimeAwareLearner(model_version=model_version)
    trades_sim = _generate_sim_trades(full_data, strategy, config)
    learner.train(trades_sim, full_data, verbose=False)

    # Conservative cost model for stress (2x slippage)
    stress_cost = CostModel(base_slippage_per_unit=0.60, slippage_vix_scale=0.08)

    print(f"\n  {'Period':<28} {'Trades':>7} {'P&L':>12} {'MaxDD%':>8} {'Win%':>6}")
    print("  " + "-" * 65)

    for name, start, end in stress_periods:
        stress_config = BacktestConfig(
            start_date=start, end_date=end,
            initial_capital=config.initial_capital,
            max_lots=config.max_lots, lot_size=config.lot_size,
            entry_model_version=model_version,
            cost_model=stress_cost, apply_costs=True,
        )
        period_data = full_data[(full_data.index >= pd.Timestamp(start)) & (full_data.index <= pd.Timestamp(end))]
        if len(period_data) < 10:
            print(f"  {name:<28} — insufficient data")
            continue

        engine = SmartBacktestEngine(
            strategy, period_data, stress_config, args.dte,
            exit_engine=None,
            entry_model=learner,
            entry_threshold=entry_threshold,
            compound=True,
        )
        result = engine.run()

        print(f"  {name:<28} {result.total_trades:>7} ₹{result.total_pnl:>+11,.0f} {result.max_drawdown_pct:>7.1f}% {result.win_rate:>5.1f}%")

    print()


def run_signal(config: BacktestConfig) -> None:
    """Generate ML-powered multi-strategy trade signal."""
    print("\n" + "=" * 75)
    print("  ML-POWERED MULTI-STRATEGY TRADE SIGNAL")
    print("  (Regime Adaptive + Macro + Sentiment)")
    print("=" * 75)

    # Check Fyers token if market is open
    _check_fyers_token_and_warn()

    lookback_start = config.start_date
    lookback_years = round((config.end_date - lookback_start).days / 365, 1)
    print(f"\n  Training on {lookback_start} to {config.end_date} ({lookback_years}-year window)...")

    fetcher = MarketDataFetcher(lookback_start, config.end_date)
    data = fetcher.build_combined_dataset()

    if len(data) == 0:
        print("  ERROR: Could not fetch market data.")
        return

    print(f"  Loaded {len(data)} trading days across {len(data.columns)} features")

    # Step 1: Load evolved strategies from cache (required for consistent ML training)
    from models.strategy_evolver import StrategyEvolver

    cached_evolved = StrategyEvolver.load_from_cache()
    cache_age = StrategyEvolver.cache_age_days()

    if not cached_evolved:
        print("\n  No evolved strategy cache found.")
        print("  Signal mode requires evolved strategies for consistent ML training")
        print("  (same data source used by backtest — ensures no divergence).")
        print("\n  Run this first, then re-run signal:")
        print("    python main.py --mode evolve")
        return

    print(f"  Loaded {len(cached_evolved)} evolved strategies from cache ({cache_age}d old)")
    for regime, strat in sorted(cached_evolved.items()):
        print(f"    {regime}: {strat.direction} sd={strat.sd} w={strat.spread_width} "
              f"pt={strat.profit_target_pct:.0f}% sl={strat.stop_loss_pct:.0f}% "
              f"h={strat.hold_days}d (Sharpe={strat.sharpe:.2f})")
    if cache_age and cache_age > 7:
        print(f"  ⚠ Cache is {cache_age} days old. Run --mode evolve to refresh.")

    evolver = StrategyEvolver(data, lots=config.max_lots, lot_size=config.lot_size)
    evolver.evolved_strategies = cached_evolved
    print("  Generating training data from evolved strategies...")
    sim_trades = evolver.generate_training_trades(
        entry_every_n_days=TRAINING_FLOW.monthly_entry_every_n_days
    )

    wins = sum(1 for t in sim_trades if t.total_pnl > 0)
    strat_counts = {}
    for t in sim_trades:
        strat_counts[t.strategy] = strat_counts.get(t.strategy, 0) + 1
    print(f"  Evolved trades: {len(sim_trades)} samples ({wins} wins, {wins/len(sim_trades)*100:.0f}%)")
    print(f"  Strategies: {', '.join(f'{k}: {v}' for k, v in sorted(strat_counts.items()))}")

    # Step 2: Load saved regime-aware ML model (trained once in --mode evolve)
    from models.regime_aware_learner import RegimeAwareLearner
    try:
        learner = RegimeAwareLearner.load(data)
        age = RegimeAwareLearner.cache_age_days()
        print(f"\n  Loaded entry model v4 from cache ({age}d old)")
        if age and age > 30:
            print(f"  ⚠ Model is {age} days old — consider re-running --mode evolve")
    except FileNotFoundError as e:
        print(f"\n  ERROR: {e}")
        return

    # Step 3: Current market data
    latest = data.iloc[-1]
    data_date = data.index[-1]
    data_date_str = data_date.date() if hasattr(data_date, "date") else data_date
    today = now_ist().date()  # IST calendar date, not host (UTC) date
    spot = latest.get("nifty_close", 0)
    vix = latest.get("vix", 0)

    stale_note = ""
    if data_date_str < today:
        stale_days = (today - data_date_str).days
        stale_note = f"  (data is {stale_days}d old — market may not have closed yet today)"

    print(f"\n  {'='*75}")
    print(f"  CURRENT MARKET SNAPSHOT")
    print(f"  {'='*75}")
    print(f"  Today:      {today}")
    print(f"  Data as of: {data_date_str}{stale_note}")
    
    # Try to supplement with live Fyers data if available
    live_spot_available = False
    live_vix_available = False
    if data_date_str < today:
        try:
            from data.broker import fyersModel as _fyersModel
            import os as _os_live
            load_env()

            _market_hours = is_market_hours(now_ist())

            if _market_hours:
                _fyers_live = _fyersModel.FyersModel(
                    client_id=_os_live.getenv("FYERS_CLIENT_ID"),
                    token=_os_live.getenv("FYERS_ACCESS_TOKEN"),
                    log_path=""
                )
                _idx_resp_live = _fyers_live.quotes(
                    data={"symbols": "NSE:NIFTY50-INDEX,NSE:INDIAVIX-INDEX"}
                )
                if _idx_resp_live.get("s") == "ok":
                    for _item_live in _idx_resp_live.get("d", []):
                        _lp_live = _item_live["v"].get("lp", 0)
                        if "NIFTY50" in _item_live["n"] and _lp_live > 0:
                            spot = _lp_live
                            live_spot_available = True
                        elif "INDIAVIX" in _item_live["n"] and _lp_live > 0:
                            vix = _lp_live
                            live_vix_available = True
        except Exception:
            pass
    
    if live_spot_available or live_vix_available:
        live_tag = " [LIVE from Fyers]" if live_spot_available else ""
        print(f"  Nifty:      {spot:,.2f}{live_tag}")
        live_tag = " [LIVE from Fyers]" if live_vix_available else ""
        print(f"  India VIX:  {vix:.2f}{live_tag}")
    else:
        print(f"  Nifty:      {spot:,.2f}")
        print(f"  India VIX:  {vix:.2f}")

    us_vix = latest.get("us_vix", 0)
    crude = latest.get("crude", 0)
    usdinr = latest.get("usdinr", 0)
    gold = latest.get("gold", 0)
    us_10y = latest.get("us_10y", 0)
    sp500 = latest.get("sp500", 0)
    bank_nifty = latest.get("nifty_bank", 0)

    print(f"\n  GLOBAL MACRO:")
    print(f"    US VIX: {us_vix:.2f} | S&P 500: {sp500:,.2f} | US 10Y: {us_10y:.2f}%")
    print(f"    Crude: ${crude:,.2f} | Gold: ${gold:,.2f}")
    print(f"\n  INDIA MACRO:")
    print(f"    USD/INR: {usdinr:.2f} | Bank Nifty: {bank_nifty:,.2f}")

    # Cross-geo indicators
    em_etf = latest.get("em_etf", 0)
    hang_seng = latest.get("hang_seng", 0)
    europe = latest.get("europe", 0)
    dxy = latest.get("dxy", 0)
    print(f"\n  CROSS-GEO:")
    print(f"    DXY: {dxy:.2f} | EEM (EM ETF): ${em_etf:,.2f}")
    print(f"    Hang Seng: {hang_seng:,.0f} | Euro Stoxx: {europe:,.0f}")

    crude_inr = latest.get("crude_inr_composite", 0)
    sentiment = latest.get("sentiment_proxy", 0)
    rsi = latest.get("nifty_rsi_14", 50)
    bb_width = latest.get("nifty_bb_width", 3)
    contagion = latest.get("global_contagion_score", 0)
    dxy_crude = latest.get("dxy_crude_composite", 0)
    vix_prem = latest.get("vix_premium_over_us", 0)
    fii = latest.get("fii_flow_proxy", 0)
    print(f"\n  DERIVED INDICATORS:")
    print(f"    Crude+INR Stress: {crude_inr:+.2f} | DXY+Crude: {dxy_crude:+.2f}")
    print(f"    Sentiment: {sentiment:+.3f} | Contagion Score: {contagion:+.3f}")
    print(f"    RSI(14): {rsi:.1f} | BB Width: {bb_width:.2f}%")
    print(f"    VIX Premium vs US: {vix_prem:+.0%} | FII Flow Proxy: {fii:+.3f}")

    # Step 4: Hybrid entry decision via EntryDecisionEngine (identical logic to backtest)
    from backtester.entry_engine import EntryDecisionEngine
    regime_strat = RegimeAdaptiveStrategy(
        lots=config.max_lots,
        lot_size=config.lot_size,
        allow_bwb=not config.monthly_disable_bwb,
    )
    market_data_dict = latest.to_dict() if hasattr(latest, "to_dict") else dict(latest)

    if learner.is_trained:
        entry_engine = EntryDecisionEngine(
            entry_model=learner,
            strategy=regime_strat,
            entry_threshold=config.monthly_entry_threshold,
            cooldown_days=0,     # single-point check — no cooldown in signal mode
            max_loss_streak=999, # no loss-streak throttle in signal mode
            loss_streak_bump=0.0,
        )
        entry_decision = entry_engine.check(latest, spot, vix, market_data_dict)
        eligible = entry_decision.eligible_strategies
        prediction = entry_decision.ml_prediction
        strat_rec = entry_decision.strat_rec
        win_prob = entry_decision.win_prob
        detected_regime = prediction.get("regime", "UNKNOWN")
        signal_val = prediction.get("signal", "AVOID")

        # ── CIRCUIT BREAKER ──
        if entry_decision.skip_reason == "circuit_breaker":
            print(f"\n  {'='*75}")
            print(f"  CIRCUIT BREAKER ACTIVE — NO TRADE TODAY")
            print(f"  {'='*75}")
            cb_reasons = getattr(regime_strat, "_circuit_breaker_reasons", [])
            crash_sigs = getattr(regime_strat, "_crash_signals", {})
            if cb_reasons:
                print(f"\n  Why the circuit breaker fired:")
                for reason in cb_reasons:
                    print(f"    X  {reason}")
            if crash_sigs:
                print(f"\n  Crash-detection dashboard:")
                print(f"    VIX:                  {crash_sigs.get('vix', 0):.1f}")
                print(f"    Crash Risk Score:     {crash_sigs.get('crash_risk_score', 0):.2f}  (threshold: 0.67)")
                print(f"    Consecutive Down Days:{crash_sigs.get('consec_down_days', 0):.0f}  (threshold: 4)")
                print(f"    Correction from High: {crash_sigs.get('correction_depth_pct', 0):.1f}%  (threshold: -5%)")
                print(f"    Crude Shock (10d):    {crash_sigs.get('crude_shock_10d_pct', 0):+.1f}%  (threshold: ±20%)")
                print(f"    VIX Acceleration (3d):{crash_sigs.get('vix_accel_3d_pct', 0):+.1f}%  (threshold: 30%)")
            print(f"\n  Action: SIT ON HANDS. Wait for crash signals to normalize.")
            print(f"  Re-run tomorrow: python3 main.py --mode signal")
            print(f"  {'='*75}")
            learner.print_training_report()
            return

        # ── ML ASSESSMENT DISPLAY ──
        print(f"\n  {'='*75}")
        print(f"  REGIME-AWARE ML ASSESSMENT  |  Circuit Breaker: CLEAR")
        print(f"  {'='*75}")
        print(f"  Detected Regime:  {detected_regime}")
        regime_probas = prediction.get("regime_probas", {})
        if regime_probas:
            from models.regime_classifier import REGIME_NAMES
            proba_str = " | ".join(f"{REGIME_NAMES.get(r, str(r))}: {p:.0%}" for r, p in
                                    sorted(regime_probas.items(), key=lambda x: x[1], reverse=True))
            print(f"  Regime Confidence: {proba_str}")

        from strategies.multi_strategy import STRATEGY_DISPLAY as SD
        eligible_display = [SD.get(s, s) for s in eligible]
        print(f"  Regime-eligible strategies: {', '.join(eligible_display)}")

        print(f"  Overall Signal:      {signal_val}")
        print(f"  Quality Score:       {prediction.get('quality_score', win_prob):.1%}")
        print(f"  Expected Return:     {prediction.get('expected_return', 0):+.1f}% (risk-adj)")
        print(f"  Composite Score:     {prediction.get('composite_score', 0):.4f}")
        print(f"  Model Confidence:    {prediction.get('confidence', 0):.1%}")

        # ── AVOID / CAUTION — entry engine already applied PCS floor & quality gate ──
        if not entry_decision.should_enter:
            skip = entry_decision.skip_reason or "quality_threshold"
            if signal_val in ("AVOID", "CAUTION_ENTRY") or skip in (
                "quality_threshold", "pcs_floor_no_alternative", "no_strategy_activated"
            ):
                print(f"\n  ML says: {signal_val} — regime is eligible but ML sees elevated risk.")
                if prediction.get("macro_context"):
                    print(f"\n  MACRO ALERTS (why ML is cautious):")
                    for ctx in prediction["macro_context"]:
                        print(f"    !  {ctx}")
            else:
                print(f"\n  Entry blocked ({skip}).")
            print(f"\n  Action: SKIP TODAY. ML probability or macro signals are unfavorable.")
            print(f"  Re-run tomorrow: python3 main.py --mode signal")
            print(f"  {'='*75}")
            learner.print_training_report()
            return

        # ── ENTRY APPROVED ──
        rec_strat = entry_decision.recommended_strategy or (eligible[0] if eligible else "")
        rec_display = strat_rec.get("display_name", STRATEGY_DISPLAY.get(rec_strat, rec_strat))
        print(f"\n  RECOMMENDED STRATEGY: {rec_display}")

        scores = strat_rec.get("strategy_scores", {})
        if scores:
            print(f"\n  Strategy Scores (composite = quality × expected return):")
            for sname, sinfo in sorted(scores.items(), key=lambda x: x[1].get("composite_score", 0), reverse=True):
                display = ML_STRATEGY_DISPLAY.get(sname, sname)
                in_eligible = " [eligible]" if sname in eligible else " [filtered out]"
                marker = " <- BEST" if sname == rec_strat else ""
                print(f"    {display:<35} E[R]: {sinfo.get('expected_return', 0):+.1f}% | "
                      f"Hist: {sinfo.get('historical_wr', 0):.0%} ({sinfo.get('historical_trades', 0)} trades) | "
                      f"Score: {sinfo.get('composite_score', 0):.4f}{in_eligible}{marker}")

        if strat_rec.get("reasoning"):
            print(f"\n  Strategy Reasoning:")
            for r in strat_rec["reasoning"]:
                print(f"    * {r}")

        if prediction.get("macro_context"):
            print(f"\n  MACRO ALERTS:")
            for ctx in prediction["macro_context"]:
                print(f"    !  {ctx}")

        optimal_params = learner.predict_optimal_params(latest)
        if "recommendation" in optimal_params:
            print(f"\n  ! {optimal_params['recommendation']}")
            learner.print_training_report()
            return

    else:
        # Rule-based fallback when ML model is not trained
        eligible = regime_strat.get_eligible_strategies(spot, vix, market_data_dict)
        if not eligible:
            print(f"\n  CIRCUIT BREAKER ACTIVE — NO TRADE TODAY (rule-based)")
            return
        prediction = {"signal": "RULE_BASED"}
        optimal_params = {}
        strat_rec = {"strategy": eligible[0], "params": {}}
        rec_strat = eligible[0]
        rec_display = STRATEGY_DISPLAY.get(rec_strat, rec_strat)
        win_prob = 0.60
        detected_regime = ""

    # Step 5: Rule-based regime classification
    signal_gen = SignalGenerator(data)
    signal = signal_gen.generate_signal(latest)

    print(f"\n  {'='*75}")
    print(f"  MARKET REGIME: {signal.regime.vix_level} VIX | {signal.regime.trend}")
    print(f"  {'='*75}")
    print(f"  Risk Score: {signal.regime.risk_score:.2f} | "
          f"Crude Impact: {signal.regime.crude_impact} | Event Risk: {signal.regime.event_risk}")

    # Step 6: Multi-expiry selection (shared code with backtest engine)
    ml_params = strat_rec.get("params", {})

    decision = select_optimal_entry(
        spot=spot,
        vix=vix,
        strategy_name=rec_strat,
        strategies=regime_strat._strategies,
        entry_date=today,
        lots=config.max_lots,
        lot_size=config.lot_size,
        risk_free_rate=config.risk_free_rate,
        brokerage_per_lot=config.brokerage_per_lot,
    )

    if decision.found:
        expiry_date = decision.expiry
        actual_dte = decision.dte

        print(f"\n  {'='*75}")
        print(f"  MULTI-EXPIRY OPTIMISATION (shared flow with backtest engine)")
        print(f"  {'='*75}")
        print(f"  Strategy: {rec_strat} | Evaluated {len(decision.all_candidates)} expiry candidates")
        print(f"\n  {'Expiry':<18} {'DTE':>4} {'Credit':>8} {'MaxLoss':>8} {'WinProb':>8} {'EV':>10} {'Score':>7}  Reasoning")
        print(f"  {'-'*100}")
        for i, c in enumerate(decision.all_candidates):
            marker = " <- BEST" if i == 0 else ""
            reasons_short = "; ".join(c.reasoning[:2]) if c.reasoning else ""
            print(f"  {str(c.expiry):<18} {c.dte:>4} {c.net_credit:>8.1f} {c.max_loss:>8.1f} "
                  f"{c.win_prob_estimate:>7.0%} {c.ev:>10.1f} {c.score:>7.2f}  {reasons_short}{marker}")
    else:
        target_dte = ml_params.get("target_dte", optimal_params.get("target_dte", 21))
        expiry_date, actual_dte = get_best_expiry_for_dte(today, target_dte)
        print(f"\n  ExpirySelector returned no candidates — falling back to DTE-based selection.")

    upcoming = get_upcoming_expiries(today, count=4)
    print(f"\n  Selected Expiry: {format_expiry_label(expiry_date)} (DTE: {actual_dte} days)")
    for exp in upcoming:
        marker = " <- SELECTED" if exp["expiry"] == expiry_date else ""
        print(f"    {exp['label']:>15}  (DTE: {exp['dte']:>3} days){marker}")

    # Step 7: Fetch LIVE option chain for the selected expiry
    print(f"\n  {'='*75}")
    print(f"  FETCHING LIVE OPTION CHAIN FOR {format_expiry_label(expiry_date)}...")
    print(f"  {'='*75}")

    snapshot = None
    live_spot = spot
    
    # Try Fyers first during market hours (9:15 AM - 3:30 PM IST)
    try:
        from data.broker import fyersModel as _fyersModel
        from data.option_chain import OptionChainSnapshot, StrikeData
        from datetime import datetime as _dt2
        import os as _os2
        load_env()

        # Market hours in IST regardless of system TZ (holiday-aware)
        _is_market_hours = is_market_hours(now_ist())

        if _is_market_hours:
            print(f"  Market hours detected — attempting Fyers API...")
            
            _fyers2 = _fyersModel.FyersModel(
                client_id=_os2.getenv("FYERS_CLIENT_ID"),
                token=_os2.getenv("FYERS_ACCESS_TOKEN"),
                log_path=""
            )
            
            # Get live spot and VIX
            _idx_resp2 = _fyers2.quotes(data={"symbols": "NSE:NIFTY50-INDEX,NSE:INDIAVIX-INDEX"})
            if _idx_resp2.get("s") == "ok":
                for _item2 in _idx_resp2.get("d", []):
                    _lp2 = _item2["v"].get("lp", 0)
                    if "NIFTY50" in _item2["n"]:
                        live_spot = _lp2
                print(f"  Fyers LIVE: Nifty {live_spot:,.2f}")
                
                # Get valid expiries and find timestamp for our target
                _oc_meta2 = _fyers2.optionchain(
                    data={"symbol": "NSE:NIFTY50-INDEX", "strikecount": 1, "timestamp": ""}
                )
                _ts_for_expiry = ""
                _closest_exp = None
                _min_gap = 999
                for _e2 in _oc_meta2.get("data", {}).get("expiryData", []):
                    _parts2 = _e2["date"].split("-")
                    _ed2 = _dt2.strptime(f"{_parts2[2]}-{_parts2[1]}-{_parts2[0]}", "%Y-%m-%d").date()
                    _gap2 = abs((_ed2 - expiry_date).days)
                    if _gap2 < _min_gap:
                        _min_gap = _gap2
                        _closest_exp = _ed2
                        _ts_for_expiry = _e2["expiry"]
                
                if _ts_for_expiry:
                    print(f"  Fetching Fyers option chain for {_closest_exp} (gap {_min_gap}d from target)...")
                    _oc_resp2 = _fyers2.optionchain(
                        data={"symbol": "NSE:NIFTY50-INDEX", "strikecount": 40, "timestamp": _ts_for_expiry}
                    )
                    
                    _calls2, _puts2 = [], []
                    for _o2 in _oc_resp2.get("data", {}).get("optionsChain", []):
                        _sp2 = _o2.get("strike_price", -1)
                        _ot2 = _o2.get("option_type", "")
                        if _sp2 <= 0 or not _ot2:
                            continue
                        _sd2 = StrikeData(
                            strike=int(_sp2),
                            option_type=_ot2,
                            ltp=_o2.get("ltp", 0),
                            bid=_o2.get("bid", 0),
                            ask=_o2.get("ask", 0),
                            oi=_o2.get("oi", 0),
                            oi_change=0,
                            volume=_o2.get("volume", 0),
                            iv=0.0,
                            expiry=str(_closest_exp),
                        )
                        if _ot2 == "CE":
                            _calls2.append(_sd2)
                        else:
                            _puts2.append(_sd2)
                    
                    if _calls2 or _puts2:
                        snapshot = OptionChainSnapshot(
                            timestamp=f"Fyers LIVE ({now_ist().strftime('%H:%M:%S')})",
                            spot_price=live_spot,
                            expiry_dates=[str(_closest_exp)],
                            selected_expiry=_closest_exp.strftime("%d-%b-%Y"),
                            calls=_calls2,
                            puts=_puts2,
                            pcr_oi=0.0,
                            pcr_volume=0.0,
                            max_pain=0,
                            total_call_oi=sum(c.oi for c in _calls2),
                            total_put_oi=sum(p.oi for p in _puts2),
                            atm_strike=round(live_spot / 50) * 50,
                        )
                        print(f"  ✓ Fyers chain: {len(_calls2)} CE, {len(_puts2)} PE | "
                              f"Spot: {live_spot:,.2f} | Expiry: {_closest_exp}")
        else:
            print(f"  Outside market hours — skipping Fyers, using NSE/Groww fallback")
            
    except Exception as _fyers_err:
        print(f"  Fyers fetch failed ({_fyers_err})")
    
    # Fallback to NSE/Groww if Fyers didn't provide snapshot
    if snapshot is None:
        print(f"  Falling back to NSE/Groww for option chain...")
        nse_fetcher = NSEOptionChainFetcher()
        nse_expiry_fmt = expiry_date.strftime("%d-%b-%Y")
        snapshot = nse_fetcher.fetch(target_expiry=nse_expiry_fmt)
        live_spot = snapshot.spot_price if snapshot and snapshot.spot_price > 0 else spot

    from backtester.position_sizer import PositionSizer, _regime_from_vix as _vix_regime
    sizer = PositionSizer(config, max_lots_cap=config.max_lots * 2)
    _sig_win_prob = win_prob if win_prob else 0.60
    _sig_regime = detected_regime if learner.is_trained else _vix_regime(vix)
    sizing = sizer.compute_lots(
        equity=config.initial_capital,
        vix=vix,
        regime=_sig_regime,
        win_prob=_sig_win_prob,
        drawdown_pct=0.0,
    )
    lots = sizing.lots
    print(f"\n  POSITION SIZING (base * confidence * regime * DD):")
    print(f"    Base lots: {sizing.base_lots:.1f} | Confidence scale: {sizing.confidence_scale:.1f}x")
    print(f"    Regime scale: {sizing.regime_scale}x | DD scale: {sizing.dd_scale}x")
    print(f"    Final lots: {sizing.lots}  ({sizing.reason})")

    lot_size = config.lot_size
    profit_target = ml_params.get("profit_target_pct", 50)
    spread_width = ml_params.get("spread_width", 500)

    if snapshot and snapshot.calls and snapshot.puts:
        print(f"  Live Spot: {live_spot:,.2f} | Data: {snapshot.timestamp}")
        print(f"  Live Expiry: {snapshot.selected_expiry}")
        print_oi_analysis(snapshot, live_spot)

        live_expiry_matches = _expiry_matches(snapshot.selected_expiry, expiry_date)

        # Generate strategy-specific trade
        _print_strategy_trade(
            rec_strat, rec_display, snapshot, live_spot, vix,
            actual_dte, ml_params, lots, lot_size, profit_target,
            spread_width, expiry_date, live_expiry_matches,
        )
    else:
        print(f"\n  ⚠ Could not fetch live option chain.")
        _print_strategy_bs_trade(
            rec_strat, rec_display, live_spot, vix, actual_dte,
            ml_params, lots, lot_size, profit_target, spread_width, expiry_date,
        )

    # Step 8: Training report
    if learner.is_trained:
        learner.print_training_report()

    # Step 9: Regime history
    print(f"\n  Regime Performance History:")
    regime_perf = signal_gen.analyze_regime_performance()
    if len(regime_perf) > 0:
        print(regime_perf.to_string(index=False))


def _print_strategy_trade(
    strategy_name, display_name, snapshot, live_spot, vix,
    actual_dte, params, lots, lot_size, profit_target,
    spread_width, expiry_date, live_expiry_matches,
):
    """Generate a strategy-specific trade using live OI data + BS pricing."""
    from pricing.black_scholes import price_option, iv_from_vix, OptionType

    annual_vol = vix / 100.0
    period_vol = annual_vol / (252**0.5) * (actual_dte**0.5)

    top_call_oi = sorted(snapshot.calls, key=lambda c: c.oi, reverse=True)[:5]
    top_put_oi = sorted(snapshot.puts, key=lambda p: p.oi, reverse=True)[:5]
    resistance_levels = [c.strike for c in top_call_oi if c.strike > live_spot]
    support_levels = [p.strike for p in top_put_oi if p.strike < live_spot]

    def snap_to_oi(target, levels, max_dist=300):
        if levels:
            best = min(levels, key=lambda l: abs(l - target))
            if abs(best - target) < max_dist:
                return best
        return target

    def get_oi_info(strike, opt_type):
        pool = snapshot.calls if opt_type == "CE" else snapshot.puts
        hit = next((s for s in pool if s.strike == strike), None)
        if hit:
            return f"OI: {hit.oi:>10,}  Chg: {hit.oi_change:>+8,}  [{hit.buildup}]"
        return "OI: N/A"

    def bs_price(strike, opt_type):
        ot = OptionType.CALL if opt_type == "CE" else OptionType.PUT
        iv = iv_from_vix(vix, strike, live_spot, ot)
        p = price_option(live_spot, strike, actual_dte, iv, 0.065, ot)
        return p.premium, iv

    def live_price(strike, opt_type):
        pool = snapshot.calls if opt_type == "CE" else snapshot.puts
        hit = next((s for s in pool if s.strike == strike), None)
        if hit and hit.ltp > 0:
            return hit.ltp, hit.iv
        return None, None

    def get_price(strike, opt_type):
        if live_expiry_matches:
            ltp, iv = live_price(strike, opt_type)
            if ltp:
                return ltp, iv, "LIVE"
        premium, iv = bs_price(strike, opt_type)
        return premium, iv, "BS est"

    expiry_label = format_expiry_label(expiry_date)
    price_note = "LIVE PRICES" if live_expiry_matches else "BS ESTIMATED (verify on broker)"

    print(f"\n  {'='*75}")
    print(f"  SUGGESTED TRADE: {display_name}")
    print(f"  Expiry: {expiry_label} ({actual_dte} DTE) | {price_note}")
    print(f"  {'='*75}")
    print(f"  Spot: {live_spot:,.2f} | VIX: {vix:.1f}")

    if strategy_name in ("put_credit_spread", "put_credit_wide"):
        put_sd = params.get("put_sd", 1.0)
        target_sp = round((live_spot - live_spot * period_vol * put_sd) / 50) * 50
        target_sp = snap_to_oi(target_sp, support_levels)
        lp_strike = target_sp - spread_width

        sp_price, sp_iv, sp_src = get_price(target_sp, "PE")
        lp_price, lp_iv, lp_src = get_price(lp_strike, "PE")

        credit = sp_price - lp_price
        max_loss = spread_width - credit

        print(f"\n  PUT SPREAD (bullish bias, defined risk):")
        print(f"    SELL {target_sp} PE  @ ₹{sp_price:>8.1f}  (IV: {sp_iv:.1f}%)  [{sp_src}]  {get_oi_info(target_sp, 'PE')}")
        print(f"    BUY  {lp_strike} PE  @ ₹{lp_price:>8.1f}  (IV: {lp_iv:.1f}%)  [{lp_src}]  {get_oi_info(lp_strike, 'PE')}")

    elif strategy_name == "broken_wing_butterfly":
        inner = params.get("inner_wing", 300)
        outer = params.get("outer_wing", 800)
        center = round((live_spot - live_spot * 0.01) / 50) * 50
        center = snap_to_oi(center, support_levels, max_dist=200)
        upper = center + inner
        lower = center - outer

        u_price, u_iv, u_src = get_price(upper, "PE")
        c_price, c_iv, c_src = get_price(center, "PE")
        l_price, l_iv, l_src = get_price(lower, "PE")

        credit = u_price - 2 * c_price + l_price
        max_loss = inner - credit if credit > 0 else inner + abs(credit)

        print(f"\n  BROKEN WING BUTTERFLY (asymmetric, defined risk):")
        print(f"    BUY  {upper} PE  @ ₹{u_price:>8.1f}  (IV: {u_iv:.1f}%)  [{u_src}]  {get_oi_info(upper, 'PE')}")
        print(f"    SELL {center} PE x2 @ ₹{c_price:>8.1f}  (IV: {c_iv:.1f}%)  [{c_src}]  {get_oi_info(center, 'PE')}")
        print(f"    BUY  {lower} PE  @ ₹{l_price:>8.1f}  (IV: {l_iv:.1f}%)  [{l_src}]  {get_oi_info(lower, 'PE')}")

    elif strategy_name == "calendar_spread":
        dte_gap = params.get("dte_gap", 30)
        max_rolls = params.get("max_rolls", 2)
        atm_strike = round(live_spot / 50) * 50
        atm_strike = snap_to_oi(atm_strike, support_levels, max_dist=200)

        near_dte = actual_dte
        far_dte = actual_dte + dte_gap

        near_price, near_iv, near_src = get_price(atm_strike, "PE")
        far_price, far_iv, far_src = bs_price(atm_strike, "PE")
        far_price_val = far_price if isinstance(far_price, (int, float)) else far_price
        far_iv_val = far_iv if isinstance(far_iv, (int, float)) else far_iv

        from pricing.black_scholes import price_option as _po, iv_from_vix as _iv, OptionType as _OT
        far_iv_calc = _iv(vix, atm_strike, live_spot, _OT.PUT)
        far_p = _po(live_spot, atm_strike, far_dte, far_iv_calc, 0.065, _OT.PUT)

        debit = far_p.premium - near_price
        credit = near_price
        max_loss = abs(debit)

        print(f"\n  CALENDAR SPREAD (sell near, buy far — same strike, theta play):")
        print(f"    SELL {atm_strike} PE {near_dte}DTE  @ ₹{near_price:>8.1f}  (IV: {near_iv:.1f}%)  [{near_src}]  {get_oi_info(atm_strike, 'PE')}")
        print(f"    BUY  {atm_strike} PE {far_dte}DTE   @ ₹{far_p.premium:>8.1f}  (IV: {far_iv_calc:.1f}%)  [BS est]")
        print(f"\n    Net Debit: ₹{debit:.1f} | Short leg credit: ₹{credit:.1f}")
        print(f"    Max rolls: {max_rolls} (roll short leg at 3 DTE → next month)")
        print(f"    Total hold with rolls: ~{near_dte + max_rolls * 28}d")

    else:
        # Fallback: Iron Condor style
        call_sd = params.get("call_sd", 0.7)
        put_sd = params.get("put_sd", 1.2)
        target_sc = round((live_spot + live_spot * period_vol * call_sd) / 50) * 50
        target_sp = round((live_spot - live_spot * period_vol * put_sd) / 50) * 50
        target_sc = snap_to_oi(target_sc, resistance_levels)
        target_sp = snap_to_oi(target_sp, support_levels)
        lc_strike = target_sc + spread_width
        lp_strike = target_sp - spread_width

        sc_p, sc_iv, sc_src = get_price(target_sc, "CE")
        lc_p, lc_iv, lc_src = get_price(lc_strike, "CE")
        sp_p, sp_iv, sp_src = get_price(target_sp, "PE")
        lp_p, lp_iv, lp_src = get_price(lp_strike, "PE")

        call_credit = sc_p - lc_p
        put_credit = sp_p - lp_p
        credit = call_credit + put_credit
        max_loss = max(lc_strike - target_sc, target_sp - lp_strike) - credit

        print(f"\n  CALL SPREAD:")
        print(f"    SELL {target_sc} CE  @ ₹{sc_p:>8.1f}  [{sc_src}]  {get_oi_info(target_sc, 'CE')}")
        print(f"    BUY  {lc_strike} CE  @ ₹{lc_p:>8.1f}  [{lc_src}]  {get_oi_info(lc_strike, 'CE')}")
        print(f"  PUT SPREAD:")
        print(f"    SELL {target_sp} PE  @ ₹{sp_p:>8.1f}  [{sp_src}]  {get_oi_info(target_sp, 'PE')}")
        print(f"    BUY  {lp_strike} PE  @ ₹{lp_p:>8.1f}  [{lp_src}]  {get_oi_info(lp_strike, 'PE')}")

    # Summary
    expiry_label = format_expiry_label(expiry_date)
    print(f"\n  TRADE SUMMARY ({lots} lots × {lot_size} qty):")
    print(f"    Expiry:         {expiry_label} ({actual_dte} DTE)")
    if credit > 0:
        print(f"    Net Credit:     ₹{credit:.1f}/unit = ₹{credit * lots * lot_size:,.0f} total")
    else:
        print(f"    Net Debit:      ₹{abs(credit):.1f}/unit = ₹{abs(credit) * lots * lot_size:,.0f} total")
    print(f"    Max Loss:       ₹{max_loss:.1f}/unit = ₹{max_loss * lots * lot_size:,.0f} total")
    if credit > 0:
        print(f"    R:R Ratio:      1:{max_loss/credit:.1f}")
        print(f"    Profit Target:  ₹{credit * profit_target/100 * lots * lot_size:,.0f} ({profit_target}% of credit)")
    print(f"    Est Margin:     ~₹{spread_width * lot_size * lots:,}")

    # OI context
    print(f"\n  OI CONTEXT:")
    print(f"    Max Pain: {snapshot.max_pain:,} "
          f"({'above' if snapshot.max_pain > live_spot else 'below'} spot by "
          f"{abs(snapshot.max_pain - live_spot):,.0f} pts)")
    print(f"    PCR (OI): {snapshot.pcr_oi:.3f} "
          f"({'bullish' if snapshot.pcr_oi > 1.0 else 'bearish' if snapshot.pcr_oi < 0.7 else 'neutral'})")
    if resistance_levels:
        print(f"    Resistance (Call OI): {', '.join(str(r) for r in sorted(resistance_levels)[:3])}")
    if support_levels:
        print(f"    Support (Put OI):     {', '.join(str(s) for s in sorted(support_levels, reverse=True)[:3])}")


def _print_strategy_bs_trade(
    strategy_name, display_name, spot, vix, actual_dte,
    params, lots, lot_size, profit_target, spread_width, expiry_date,
):
    """Fallback: BS-only pricing when live data unavailable."""
    from pricing.black_scholes import price_option, iv_from_vix, OptionType

    annual_vol = vix / 100.0
    period_vol = annual_vol / (252**0.5) * (actual_dte**0.5)
    expiry_label = format_expiry_label(expiry_date)

    print(f"\n  {'='*75}")
    print(f"  ESTIMATED TRADE: {display_name} (Black-Scholes)")
    print(f"  Expiry: {expiry_label} ({actual_dte} DTE)")
    print(f"  ⚠ THEORETICAL prices. Verify on broker before trading.")
    print(f"  {'='*75}")

    if strategy_name in ("put_credit_spread", "put_credit_wide"):
        put_sd = params.get("put_sd", 1.0)
        sp_strike = round((spot - spot * period_vol * put_sd) / 50) * 50
        lp_strike = sp_strike - spread_width
        sp_iv = iv_from_vix(vix, sp_strike, spot, OptionType.PUT)
        lp_iv = iv_from_vix(vix, lp_strike, spot, OptionType.PUT)
        sp = price_option(spot, sp_strike, actual_dte, sp_iv, 0.065, OptionType.PUT)
        lp = price_option(spot, lp_strike, actual_dte, lp_iv, 0.065, OptionType.PUT)
        credit = sp.premium - lp.premium
        max_loss = spread_width - credit
        print(f"\n  SELL {sp_strike} PE @ ₹{sp.premium:.1f}")
        print(f"  BUY  {lp_strike} PE @ ₹{lp.premium:.1f}")
        print(f"  Credit: ₹{credit:.1f} | Max Loss: ₹{max_loss:.1f}")

    elif strategy_name == "broken_wing_butterfly":
        inner = params.get("inner_wing", 300)
        outer = params.get("outer_wing", 800)
        center = round((spot - spot * 0.01) / 50) * 50
        upper = center + inner
        lower = center - outer
        u_iv = iv_from_vix(vix, upper, spot, OptionType.PUT)
        c_iv = iv_from_vix(vix, center, spot, OptionType.PUT)
        l_iv = iv_from_vix(vix, lower, spot, OptionType.PUT)
        u = price_option(spot, upper, actual_dte, u_iv, 0.065, OptionType.PUT)
        c = price_option(spot, center, actual_dte, c_iv, 0.065, OptionType.PUT)
        l = price_option(spot, lower, actual_dte, l_iv, 0.065, OptionType.PUT)
        credit = u.premium - 2 * c.premium + l.premium
        print(f"\n  BUY  {upper} PE @ ₹{u.premium:.1f}")
        print(f"  SELL {center} PE x2 @ ₹{c.premium:.1f}")
        print(f"  BUY  {lower} PE @ ₹{l.premium:.1f}")
        print(f"  Net: ₹{credit:+.1f}")

    elif strategy_name == "calendar_spread":
        dte_gap = params.get("dte_gap", 30)
        max_rolls = params.get("max_rolls", 2)
        atm = round(spot / 50) * 50
        near_dte = actual_dte
        far_dte = actual_dte + dte_gap
        near_iv = iv_from_vix(vix, atm, spot, OptionType.PUT)
        far_iv = iv_from_vix(vix, atm, spot, OptionType.PUT)
        near_p = price_option(spot, atm, near_dte, near_iv, 0.065, OptionType.PUT)
        far_p = price_option(spot, atm, far_dte, far_iv, 0.065, OptionType.PUT)
        debit = far_p.premium - near_p.premium
        print(f"\n  SELL {atm} PE {near_dte}DTE @ ₹{near_p.premium:.1f}")
        print(f"  BUY  {atm} PE {far_dte}DTE  @ ₹{far_p.premium:.1f}")
        print(f"  Net Debit: ₹{debit:.1f} | Max rolls: {max_rolls}")

    print(f"\n  ({lots} lots × {lot_size} qty) | Expiry: {expiry_label} ({actual_dte} DTE) | Est margin: ~₹{spread_width * lot_size * lots:,}")


def _expiry_matches(live_expiry: str, target_expiry: date) -> bool:
    """Check if the live option chain expiry matches our target."""
    from datetime import datetime
    target_str1 = target_expiry.strftime("%d-%b-%Y")  # 30-Apr-2026
    target_str2 = target_expiry.strftime("%Y-%m-%d")  # 2026-04-30
    return live_expiry in (target_str1, target_str2)


def _print_live_trade(
    snapshot, live_spot, vix, actual_dte, call_sd, put_sd,
    hedge_width, profit_target, lots, lot_size,
):
    """Print trade using actual live prices from option chain."""
    from data.option_chain import find_best_iron_condor_strikes

    annual_vol = vix / 100.0
    period_vol = annual_vol / (252**0.5) * (actual_dte**0.5)
    call_dist_pct = period_vol * call_sd
    put_dist_pct = period_vol * put_sd

    ic = find_best_iron_condor_strikes(
        snapshot, live_spot,
        call_distance_pct=call_dist_pct,
        put_distance_pct=put_dist_pct,
        hedge_width=hedge_width,
    )

    if not ic or "error" in ic:
        error = ic.get("error", "Unknown") if ic else "No data"
        print(f"\n  ⚠ Could not find liquid strikes: {error}")
        return

    sc, lc, sp_opt, lp = ic["short_call"], ic["long_call"], ic["short_put"], ic["long_put"]
    credit = ic["net_credit"]
    max_loss = ic["max_loss"]

    print(f"\n  {'='*70}")
    print(f"  SUGGESTED TRADE — LIVE PRICES — Expiry: {snapshot.selected_expiry}")
    print(f"  {'='*70}")
    print(f"  Strategy: Iron Condor V2 (Asymmetric, Liquid Strikes Only)")
    print(f"  DTE: {actual_dte} days | Profit Target: {profit_target}%")
    print(f"  Spot: {live_spot:,.2f}")

    _print_leg_details("CALL SPREAD (sell near, buy far)", sc, lc, ic["call_credit"])
    _print_leg_details("PUT SPREAD (sell near, buy far)", sp_opt, lp, ic["put_credit"])
    _print_trade_summary(credit, max_loss, lots, lot_size, profit_target, live_spot, ic, snapshot)


def _print_oi_guided_trade(
    snapshot, live_spot, vix, actual_dte, call_sd, put_sd,
    hedge_width, profit_target, lots, lot_size, expiry_date,
):
    """Use live OI data for strike selection, BS for pricing on target expiry."""
    from pricing.black_scholes import price_option, iv_from_vix, OptionType

    # Identify key support/resistance from OI
    top_call_oi = sorted(snapshot.calls, key=lambda c: c.oi, reverse=True)[:5]
    top_put_oi = sorted(snapshot.puts, key=lambda p: p.oi, reverse=True)[:5]

    resistance_levels = [c.strike for c in top_call_oi if c.strike > live_spot]
    support_levels = [p.strike for p in top_put_oi if p.strike < live_spot]

    annual_vol = vix / 100.0
    period_vol = annual_vol / (252**0.5) * (actual_dte**0.5)

    # Use OI-guided strikes: sell near highest OI resistance/support
    target_sc = round((live_spot + live_spot * period_vol * call_sd) / 50) * 50
    target_sp = round((live_spot - live_spot * period_vol * put_sd) / 50) * 50

    # Snap to nearest high-OI resistance for short call
    if resistance_levels:
        best_res = min(resistance_levels, key=lambda r: abs(r - target_sc))
        if abs(best_res - target_sc) < 300:
            target_sc = best_res

    # Snap to nearest high-OI support for short put
    if support_levels:
        best_sup = min(support_levels, key=lambda s: abs(s - target_sp))
        if abs(best_sup - target_sp) < 300:
            target_sp = best_sup

    lc_strike = target_sc + hedge_width
    lp_strike = target_sp - hedge_width

    # Get OI/volume info for the selected strikes from live data
    sc_live = next((c for c in snapshot.calls if c.strike == target_sc), None)
    lc_live = next((c for c in snapshot.calls if c.strike == lc_strike), None)
    sp_live = next((p for p in snapshot.puts if p.strike == target_sp), None)
    lp_live = next((p for p in snapshot.puts if p.strike == lp_strike), None)

    # Price using BS for target expiry
    sc_iv = iv_from_vix(vix, target_sc, live_spot, OptionType.CALL)
    lc_iv = iv_from_vix(vix, lc_strike, live_spot, OptionType.CALL)
    sp_iv = iv_from_vix(vix, target_sp, live_spot, OptionType.PUT)
    lp_iv = iv_from_vix(vix, lp_strike, live_spot, OptionType.PUT)

    sc = price_option(live_spot, target_sc, actual_dte, sc_iv, 0.065, OptionType.CALL)
    lc = price_option(live_spot, lc_strike, actual_dte, lc_iv, 0.065, OptionType.CALL)
    sp = price_option(live_spot, target_sp, actual_dte, sp_iv, 0.065, OptionType.PUT)
    lp = price_option(live_spot, lp_strike, actual_dte, lp_iv, 0.065, OptionType.PUT)

    call_credit = sc.premium - lc.premium
    put_credit = sp.premium - lp.premium
    credit = call_credit + put_credit
    max_loss = max(lc_strike - target_sc, target_sp - lp_strike) - credit

    expiry_label = format_expiry_label(expiry_date)

    print(f"\n  {'='*70}")
    print(f"  SUGGESTED TRADE — OI-GUIDED + BS PRICING — Expiry: {expiry_label}")
    print(f"  {'='*70}")
    print(f"  Strategy: Iron Condor V2 (OI-optimized strike selection)")
    print(f"  DTE: {actual_dte} days | Profit Target: {profit_target}%")
    print(f"  Spot: {live_spot:,.2f}")
    print(f"  ⚠ Premiums are BS estimates for {expiry_label}. Verify on broker.")

    # Call spread
    sc_oi = f"OI: {sc_live.oi:>10,}  Chg: {sc_live.oi_change:>+8,}  [{sc_live.buildup}]" if sc_live else "OI: N/A"
    lc_oi = f"OI: {lc_live.oi:>10,}  Chg: {lc_live.oi_change:>+8,}  [{lc_live.buildup}]" if lc_live else "OI: N/A"
    print(f"\n  CALL SPREAD (sell near, buy far):")
    print(f"    SELL {target_sc} CE  @ ₹{sc.premium:>8.1f}  (est. IV: {sc_iv:.1f}%)  {sc_oi}")
    print(f"    BUY  {lc_strike} CE  @ ₹{lc.premium:>8.1f}  (est. IV: {lc_iv:.1f}%)  {lc_oi}")
    print(f"    Call Spread Credit: ₹{call_credit:.1f}")

    # Put spread
    sp_oi = f"OI: {sp_live.oi:>10,}  Chg: {sp_live.oi_change:>+8,}  [{sp_live.buildup}]" if sp_live else "OI: N/A"
    lp_oi = f"OI: {lp_live.oi:>10,}  Chg: {lp_live.oi_change:>+8,}  [{lp_live.buildup}]" if lp_live else "OI: N/A"
    print(f"\n  PUT SPREAD (sell near, buy far):")
    print(f"    SELL {target_sp} PE  @ ₹{sp.premium:>8.1f}  (est. IV: {sp_iv:.1f}%)  {sp_oi}")
    print(f"    BUY  {lp_strike} PE  @ ₹{lp.premium:>8.1f}  (est. IV: {lp_iv:.1f}%)  {lp_oi}")
    print(f"    Put Spread Credit: ₹{put_credit:.1f}")

    print(f"\n  SUMMARY ({lots} lots × {lot_size} contracts):")
    print(f"    Net Credit:    ₹{credit:.1f}/unit = ₹{credit * lots * lot_size:,.0f} total")
    print(f"    Max Loss:      ₹{max_loss:.1f}/unit = ₹{max_loss * lots * lot_size:,.0f} total")
    if credit > 0:
        print(f"    R:R Ratio:     1:{max_loss/credit:.1f}")
    print(f"    Profit Target: ₹{credit * profit_target/100 * lots * lot_size:,.0f} "
          f"({profit_target}% of credit)")

    upper_be = target_sc + credit
    lower_be = target_sp - credit
    print(f"\n  BREAKEVEN POINTS:")
    print(f"    Upper: {upper_be:,.0f} ({(upper_be/live_spot - 1)*100:+.1f}% from spot)")
    print(f"    Lower: {lower_be:,.0f} ({(lower_be/live_spot - 1)*100:+.1f}% from spot)")
    print(f"    Profit Zone Width: {upper_be - lower_be:,.0f} points")

    print(f"\n  OI CONTEXT:")
    print(f"    Max Pain: {snapshot.max_pain:,} "
          f"({'above' if snapshot.max_pain > live_spot else 'below'} spot by "
          f"{abs(snapshot.max_pain - live_spot):,.0f} pts)")
    print(f"    PCR (OI): {snapshot.pcr_oi:.3f} "
          f"({'bullish' if snapshot.pcr_oi > 1.0 else 'bearish' if snapshot.pcr_oi < 0.7 else 'neutral'})")
    if resistance_levels:
        print(f"    Key Resistance (Call OI walls): {', '.join(str(r) for r in sorted(resistance_levels)[:3])}")
    if support_levels:
        print(f"    Key Support (Put OI walls):     {', '.join(str(s) for s in sorted(support_levels, reverse=True)[:3])}")


def _print_leg_details(title, sell_leg, buy_leg, spread_credit):
    """Print leg details with live data."""
    print(f"\n  {title}:")
    print(f"    SELL {sell_leg.strike} {sell_leg.option_type}  @ ₹{sell_leg.ltp:>8.2f}  "
          f"IV: {sell_leg.iv:.1f}%  OI: {sell_leg.oi:>10,}  Vol: {sell_leg.volume:>8,}  "
          f"[{sell_leg.buildup}]  Liq: {sell_leg.liquidity_score:.0f}/100")
    print(f"    BUY  {buy_leg.strike} {buy_leg.option_type}  @ ₹{buy_leg.ltp:>8.2f}  "
          f"IV: {buy_leg.iv:.1f}%  OI: {buy_leg.oi:>10,}  Vol: {buy_leg.volume:>8,}  "
          f"[{buy_leg.buildup}]  Liq: {buy_leg.liquidity_score:.0f}/100")
    print(f"    Spread Credit: ₹{spread_credit:.2f}")


def _print_trade_summary(credit, max_loss, lots, lot_size, profit_target, live_spot, ic, snapshot):
    """Print trade summary, breakevens, and warnings."""
    print(f"\n  SUMMARY ({lots} lots × {lot_size} contracts):")
    print(f"    Net Credit:    ₹{credit:.2f}/unit = ₹{credit * lots * lot_size:,.0f} total")
    print(f"    Max Loss:      ₹{max_loss:.2f}/unit = ₹{max_loss * lots * lot_size:,.0f} total")
    if credit > 0:
        print(f"    R:R Ratio:     1:{max_loss/credit:.1f}")
    print(f"    Profit Target: ₹{credit * profit_target/100 * lots * lot_size:,.0f} "
          f"({profit_target}% of credit)")

    upper_be = ic["upper_breakeven"]
    lower_be = ic["lower_breakeven"]
    print(f"\n  BREAKEVEN POINTS:")
    print(f"    Upper: {upper_be:,.0f} ({(upper_be/live_spot - 1)*100:+.1f}% from spot)")
    print(f"    Lower: {lower_be:,.0f} ({(lower_be/live_spot - 1)*100:+.1f}% from spot)")
    print(f"    Profit Zone Width: {upper_be - lower_be:,.0f} points")

    print(f"\n  OI CONTEXT:")
    print(f"    Max Pain: {snapshot.max_pain:,} "
          f"({'above' if snapshot.max_pain > live_spot else 'below'} spot by "
          f"{abs(snapshot.max_pain - live_spot):,.0f} pts)")
    print(f"    PCR (OI): {snapshot.pcr_oi:.3f} "
          f"({'bullish' if snapshot.pcr_oi > 1.0 else 'bearish' if snapshot.pcr_oi < 0.7 else 'neutral'})")

    all_legs = [ic["short_call"], ic["long_call"], ic["short_put"], ic["long_put"]]
    low_liq = [(s.strike, s.option_type, s.liquidity_score) for s in all_legs if s.liquidity_score < 30]
    if low_liq:
        print(f"\n  ⚠ LIQUIDITY WARNING:")
        for strike, otype, score in low_liq:
            print(f"    {strike} {otype} has low liquidity score ({score:.0f}/100) — use limit orders")


def _print_bs_fallback_trade(
    spot, vix, actual_dte, call_sd, put_sd, hedge_width,
    profit_target, lots, lot_size, expiry_date,
):
    """Fallback: print BS-estimated trade when live data unavailable."""
    from pricing.black_scholes import price_option, iv_from_vix, OptionType

    annual_vol = vix / 100.0
    period_vol = annual_vol / (252**0.5) * (actual_dte**0.5)

    sc_strike = round((spot + spot * period_vol * call_sd) / 50) * 50
    sp_strike = round((spot - spot * period_vol * put_sd) / 50) * 50
    lc_strike = sc_strike + hedge_width
    lp_strike = sp_strike - hedge_width

    sc_iv = iv_from_vix(vix, sc_strike, spot, OptionType.CALL)
    lc_iv = iv_from_vix(vix, lc_strike, spot, OptionType.CALL)
    sp_iv = iv_from_vix(vix, sp_strike, spot, OptionType.PUT)
    lp_iv = iv_from_vix(vix, lp_strike, spot, OptionType.PUT)

    sc = price_option(spot, sc_strike, actual_dte, sc_iv, 0.065, OptionType.CALL)
    lc = price_option(spot, lc_strike, actual_dte, lc_iv, 0.065, OptionType.CALL)
    sp = price_option(spot, sp_strike, actual_dte, sp_iv, 0.065, OptionType.PUT)
    lp = price_option(spot, lp_strike, actual_dte, lp_iv, 0.065, OptionType.PUT)

    credit = (sc.premium - lc.premium) + (sp.premium - lp.premium)
    max_loss = hedge_width - credit
    expiry_label = format_expiry_label(expiry_date)

    print(f"\n  {'='*70}")
    print(f"  ESTIMATED TRADE (Black-Scholes) — Expiry: {expiry_label}")
    print(f"  ⚠ These are THEORETICAL prices. Verify on broker before trading.")
    print(f"  {'='*70}")
    print(f"\n  CALL SPREAD:")
    print(f"    SELL {sc_strike} CE  @ ₹{sc.premium:.1f}  (est. IV: {sc_iv:.1f}%)")
    print(f"    BUY  {lc_strike} CE  @ ₹{lc.premium:.1f}  (est. IV: {lc_iv:.1f}%)")
    print(f"    Call Credit: ₹{sc.premium - lc.premium:.1f}")
    print(f"\n  PUT SPREAD:")
    print(f"    SELL {sp_strike} PE  @ ₹{sp.premium:.1f}  (est. IV: {sp_iv:.1f}%)")
    print(f"    BUY  {lp_strike} PE  @ ₹{lp.premium:.1f}  (est. IV: {lp_iv:.1f}%)")
    print(f"    Put Credit: ₹{sp.premium - lp.premium:.1f}")
    print(f"\n  Net Credit: ₹{credit:.1f}/unit = ₹{credit * lots * lot_size:,.0f} total")
    print(f"  Max Loss:   ₹{max_loss:.1f}/unit = ₹{max_loss * lots * lot_size:,.0f} total")


def _safe_baseline_comparison(evolver, baseline_configs) -> dict:
    """Run the informational baseline comparison without ever aborting evolve.

    The comparison only feeds a printed table, so any failure is reported as a
    one-line warning and replaced by an empty comparison.
    """
    try:
        return evolver.compare_with_baseline(baseline_configs)
    except Exception as e:
        print(f"  WARNING: baseline comparison skipped: {type(e).__name__}: {e}")
        return {"baseline": {}, "evolved": {}}


def run_evolve(config: BacktestConfig):
    """
    Evolve strategy parameters: exhaustive search over 1080 combos × 4 regimes.
    Discovers optimal direction, SD, width, profit target, stop loss, hold days per VIX regime.
    Then trains ML on evolved data and compares vs baseline.
    """
    from models.strategy_evolver import StrategyEvolver, VIX_REGIMES
    from backtester.rolling_simulator import RollingWindowSimulator, SimConfig, STRATEGY_CONFIGS

    print("\n" + "=" * 90)
    print("  STRATEGY EVOLUTION ENGINE")
    print(f"  Optimizing for: {TRAINING_FLOW.evolve_target.upper()}")
    print("=" * 90)

    fetcher = MarketDataFetcher(config.start_date, config.end_date)
    data = fetcher.build_combined_dataset()
    if getattr(fetcher, 'integrity_summary', ''):  # set by build only after its macro check passed
        print(f"  Cache integrity OK: {getattr(fetcher, 'integrity_summary', '')}")
    print(f"\n[1/5] Data: {len(data)} days, {len(data.columns)} columns")

    print(f"\n[2/5] Evolving strategies (parameter grid search)...")
    evolver = StrategyEvolver(data, lots=config.max_lots, lot_size=config.lot_size)
    evolved = evolver.evolve(
        target=TRAINING_FLOW.evolve_target,
        entry_every_n_days=TRAINING_FLOW.strategy_evolve_entry_every_n_days,
        train_pct=TRAINING_FLOW.layered_train_pct,
        min_trades=TRAINING_FLOW.layered_min_trades,
        verbose=True,
    )

    print(f"\n  {'=' * 85}")
    print(f"  EVOLVED STRATEGIES (optimized for {TRAINING_FLOW.evolve_target.upper()})")
    print(f"  {'=' * 85}")
    print(f"  {'Regime':<22} {'Dir':<6} {'SD':>4} {'Width':>6} {'PT%':>5} {'SL%':>5} "
          f"{'Days':>5} {'Sharpe':>7} {'WR%':>6} {'P&L':>12} {'Trades':>7} {'MaxDD':>10}")
    print(f"  {'-' * 85}")
    for regime, strat in sorted(evolved.items()):
        print(f"  {regime:<22} {strat.direction:<6} {strat.sd:>4.1f} {strat.spread_width:>6} "
              f"{strat.profit_target_pct:>5.0f} {strat.stop_loss_pct:>5.0f} "
              f"{strat.hold_days:>5} {strat.sharpe:>7.2f} {strat.win_rate:>5.0f}% "
              f"₹{strat.total_pnl:>+11,.0f} {strat.num_trades:>7} ₹{strat.max_drawdown:>9,.0f}")

    # Evolve multi-expiry strategies (calendar/diagonal)
    print(f"\n  Evolving multi-expiry strategies (calendar/diagonal with rolls)...")
    multi_evolved = evolver.evolve_multi_expiry(
        target=TRAINING_FLOW.evolve_target,
        entry_every_n_days=TRAINING_FLOW.multi_expiry_entry_every_n_days,
        verbose=True,
    )
    if multi_evolved:
        print(f"\n  {'=' * 85}")
        print(f"  EVOLVED MULTI-EXPIRY STRATEGIES")
        print(f"  {'=' * 85}")
        print(f"  {'Regime':<22} {'Type':<12} {'SD':>4} {'Hold':>5} {'Sharpe':>7} {'WR%':>6} {'P&L':>12} {'Trades':>7}")
        print(f"  {'-' * 85}")
        for regime, strat in sorted(multi_evolved.items()):
            stype = "calendar" if "calendar" in strat.name else "diagonal"
            print(f"  {regime:<22} {stype:<12} {strat.sd:>4.1f} {strat.hold_days:>5}d "
                  f"{strat.sharpe:>7.2f} {strat.win_rate:>5.0f}% "
                  f"₹{strat.total_pnl:>+11,.0f} {strat.num_trades:>7}")

    # Show top 3 per regime
    print(f"\n[3/5] Top 3 configurations per regime:")
    top = evolver.top_n_per_regime(n=3, target=TRAINING_FLOW.evolve_target)
    for regime, results in sorted(top.items()):
        print(f"\n  {regime}:")
        for i, r in enumerate(results):
            p = r["params"]
            print(f"    #{i+1} {p['direction']:<5} sd={p['sd']:.1f} w={p['spread_width']} "
                  f"pt={p['profit_target_pct']:.0f}% sl={p['stop_loss_pct']:.0f}% h={p['hold_days']}d "
                  f"| Sharpe={r['sharpe']:.2f} WR={r['win_rate']:.0f}% P&L=₹{r['total_pnl']:>+,.0f}")

    # Compare evolved vs baseline
    print(f"\n[4/5] Comparing evolved vs baseline (hand-tuned)...")
    comparison = _safe_baseline_comparison(evolver, STRATEGY_CONFIGS)

    print(f"\n  {'=' * 85}")
    print(f"  BASELINE vs EVOLVED COMPARISON")
    print(f"  {'=' * 85}")
    print(f"  {'Regime':<22} {'Version':<10} {'Sharpe':>7} {'WR%':>6} {'P&L':>12} {'Trades':>7} {'MaxDD':>10}")
    print(f"  {'-' * 85}")
    for regime in sorted(VIX_REGIMES.keys()):
        base = comparison["baseline"].get(regime)
        evo = comparison["evolved"].get(regime)
        if base:
            print(f"  {regime:<22} {'BASELINE':<10} {base['sharpe']:>7.2f} {base['win_rate']:>5.0f}% "
                  f"₹{base['total_pnl']:>+11,.0f} {base['num_trades']:>7} ₹{base['max_drawdown']:>9,.0f}")
        if evo:
            better = "↑" if (evo.get("sharpe", 0) > (base or {}).get("sharpe", -999)) else "↓"
            print(f"  {'':<22} {'EVOLVED':<10} {evo['sharpe']:>7.2f} {evo['win_rate']:>5.0f}% "
                  f"₹{evo['total_pnl']:>+11,.0f} {evo['num_trades']:>7} ₹{evo['max_drawdown']:>9,.0f} {better}")

    # Train and save all ML models on layered walk-forward stages
    print(f"\n[5/5] Layered walk-forward retraining on evolved strategies...")
    from models.layered_evolve import run_layered_training
    from models.regime_aware_learner import RegimeAwareLearner
    from models.trade_monitor import ExitStrategyEngine
    from models.weekly_risk_engine import save_risk_engine

    layered = run_layered_training(
        data=data,
        config=config,
        evolved_strategies=evolved,
        verbose=True,
    )

    entry_model = layered.entry_model
    exit_model = layered.exit_model
    risk_eng = layered.weekly_risk_engine
    entry_model.save()
    exit_model.save()
    print(f"  Saved final entry model → data/.cache/entry_model_v4.pkl")
    print(f"  Saved final exit model → data/.cache/exit_model.pkl")
    if risk_eng is not None:
        save_risk_engine(risk_eng)
        print(f"  Saved final weekly risk engine → data/.cache/weekly_risk_engine_v2.pkl")
    print(f"  Final layered entry samples: {len(layered.entry_training_trades)}")
    print(f"  Final layered weekly samples: {len(layered.weekly_training_trades)}")
    if layered.stage_stats:
        last = layered.stage_stats[-1]
        print(f"  Final stage AUC: {last.entry_cv_auc:.3f}")

    # Live signal from evolved entry model
    latest = data.iloc[-1]
    pred = entry_model.predict(latest)
    print(f"\n  EVOLVED MODEL LIVE SIGNAL:")
    print(f"    Signal: {pred['signal']}")
    print(f"    Quality Score: {pred.get('quality_score', pred.get('probability_profitable', 0)):.1%}")
    print(f"    Expected Return: {pred.get('expected_return', 0):+.1f}% (risk-adj)")
    print(f"    Confidence: {pred.get('confidence', 0):.1%}")
    print(f"    Recommended: {pred.get('recommended_strategy', '??')}")
    print(f"    Composite: {pred.get('composite_score', 0):.4f}")

    # Print evolved configs for copy-paste into rolling_simulator
    print(f"\n  {'=' * 85}")
    print(f"  EVOLVED STRATEGY_CONFIGS (copy to rolling_simulator.py):")
    print(f"  {'=' * 85}")
    print("  EVOLVED_CONFIGS = [")
    for regime, strat in sorted(evolved.items()):
        print(f'      {{"name": "{strat.name}", "direction": "{strat.direction}", '
              f'"sd": {strat.sd}, "spread_width": {strat.spread_width}, '
              f'"min_vix": {strat.min_vix}, "max_vix": {strat.max_vix}}},')
    print("  ]")




_monitor_log = logging.getLogger(__name__)


def _monitor_strategy_for_trade(trade, config: BacktestConfig):
    """Fresh exit-strategy instance for one trade.

    RegimeAdaptiveStrategy pins the sub-strategy it selects in ``_active_strategy``
    (evaluate_exit only selects it while that is None), so one shared instance prices
    every later trade with the first trade's sub-strategy. Weekly trades are identified
    by ``strategy_code`` (the journal has no other strategy field) and use the weekly
    strategy they were entered with.
    """
    code = (trade.strategy_code or "").lower()
    if "weekly_ic" in code:
        return WeeklyIronCondor(lots=config.max_lots, lot_size=config.lot_size)
    if "weekly_pcs" in code:
        return WeeklyPutCreditSpread(lots=config.max_lots, lot_size=config.lot_size)
    return RegimeAdaptiveStrategy(
        lots=config.max_lots,
        lot_size=config.lot_size,
        allow_bwb=not config.monthly_disable_bwb,
    )


def _entry_context_for_trade(trade, fallback_spot: float, fallback_vix: float) -> tuple[float, float]:
    """(entry_vix, entry_spot) from the journal; falls back to current values with a warning."""
    entry_vix = trade.entry_vix if trade.entry_vix else None
    entry_spot = trade.entry_spot if trade.entry_spot else None
    if entry_vix is None:
        _monitor_log.warning("Trade %s has no stored entry_vix; using current VIX %.2f",
                             trade.trade_id, fallback_vix)
        entry_vix = fallback_vix
    if entry_spot is None:
        _monitor_log.warning("Trade %s has no stored entry_spot; using current spot %.2f",
                             trade.trade_id, fallback_spot)
        entry_spot = fallback_spot
    return entry_vix, entry_spot


def run_monitor(config: BacktestConfig) -> None:
    """Monitor active trades and provide ML-driven exit recommendations."""

    print("\n" + "=" * 80)
    print("  ACTIVE TRADE MONITOR & EXIT STRATEGY ENGINE")
    print("=" * 80)

    # Check Fyers token if market is open
    _check_fyers_token_and_warn()

    trades = load_active_trades()
    if not trades:
        print("\n  No active trades found.")
        print("  Add a trade first:")
        print("    python main.py --mode add-trade \\")
        print('      --trade-id "put_spread_apr" \\')
        print('      --direction put \\')
        print('      --entry-date 2026-04-07 \\')
        print('      --expiry-date 2026-04-30 \\')
        print("      --short-strike 23500 \\")
        print("      --long-strike 23000 \\")
        print("      --entry-credit 45.5 \\")
        print("      --lots 2 --lot-size 75")
        return

    print(f"\n  Found {len(trades)} active trade(s)")

    print("\n  [1/3] Fetching market data...")
    fetcher = MarketDataFetcher(config.start_date, config.end_date)
    data = fetcher.build_combined_dataset()
    if len(data) == 0:
        print("  ERROR: Could not fetch market data.")
        return
    print(f"  Loaded {len(data)} trading days")

    latest = data.iloc[-1]
    spot = latest.get("nifty_close", 0)
    vix = latest.get("vix", 0)
    data_date = data.index[-1]
    data_date_str = data_date.date() if hasattr(data_date, "date") else data_date
    today_actual = date.today()

    stale = f" (data {(today_actual - data_date_str).days}d old)" if data_date_str < today_actual else ""
    print(f"\n  Market: Nifty {spot:,.2f} | VIX {vix:.2f} | Today: {today_actual} | Data: {data_date_str}{stale}")

    print("\n  [2/3] Loading exit model...")
    try:
        engine = ExitStrategyEngine.load(data)
        age = ExitStrategyEngine.cache_age_days()
        print(f"  Loaded exit model from cache ({age}d old)")
        if age and age > 30:
            print(f"  ⚠ Exit model is {age} days old — consider re-running --mode evolve")
    except FileNotFoundError:
        print("  No exit model cache found. Run --mode evolve first.")
        print("  Falling back to rule-based exit analysis (no ML).")
        engine = ExitStrategyEngine(data)

    # Fetching live prices directly for active trade legs from Fyers
    print("\n  Fetching live prices for active trades...")
    live_snapshot = None
    _fyers_live_ltp: dict[tuple, float] = {}  # (strike_int, opt_type) -> ltp
    _fyers_live_oi_vol: dict[tuple, dict] = {}  # (strike_int, opt_type) -> {"oi": ..., "volume": ..., "bid": ..., "ask": ...}
    _fyers_spot = 0.0
    _fyers_vix  = 0.0

    # Try Fyers first — fetch ONLY the exact option legs we hold (fast, accurate)
    try:
        from data.broker import fyersModel as _fyersModel
        from data.option_chain import OptionChainSnapshot, StrikeData
        from datetime import datetime as _dt
        import os as _os
        load_env()  # always re-read .env / .env.local so fresh token is picked up

        _fyers = _fyersModel.FyersModel(
            client_id=_os.getenv("FYERS_CLIENT_ID"),
            token=_os.getenv("FYERS_ACCESS_TOKEN"),
            log_path=""
        )

        print("  Attempting to fetch live data from Fyers...")

        # Get spot and VIX in a single batched call
        _idx_resp = _fyers.quotes(data={"symbols": "NSE:NIFTY50-INDEX,NSE:INDIAVIX-INDEX"})
        if _idx_resp.get("s") != "ok":
            raise ConnectionError(f"Fyers index quote failed: {_idx_resp}")

        for _item in _idx_resp.get("d", []):
            _lp = _item["v"].get("lp", 0)
            if "NIFTY50" in _item["n"]:
                _fyers_spot = _lp
            elif "INDIAVIX" in _item["n"]:
                _fyers_vix = _lp

        print(f"  Fyers LIVE: Nifty {_fyers_spot:,.2f} | VIX {_fyers_vix:.2f}")

        # ── Resolve valid Fyers symbols using optionchain API ─────────────────
        # Step 1: Get list of valid expiry dates from Fyers
        _oc_meta = _fyers.optionchain(
            data={"symbol": "NSE:NIFTY50-INDEX", "strikecount": 1, "timestamp": ""}
        )
        _valid_expiry_map = {}  # date -> expiry_flag ("W"/"M")
        _ts_map = {}            # date -> unix timestamp string (for optionchain fetch)
        for _e in _oc_meta.get("data", {}).get("expiryData", []):
            _parts = _e["date"].split("-")
            _ed = _dt.strptime(f"{_parts[2]}-{_parts[1]}-{_parts[0]}", "%Y-%m-%d").date()
            _valid_expiry_map[_ed] = _e.get("expiry_flag", "W")
            _ts_map[_ed] = _e["expiry"]

        # Step 2: For each trade expiry, find nearest valid Fyers expiry
        _trade_expiry_map = {}   # trade.expiry_date str -> resolved valid date
        for _t in trades:
            _req_date = _dt.strptime(_t.expiry_date, "%Y-%m-%d").date()
            if _req_date in _valid_expiry_map:
                _trade_expiry_map[_t.expiry_date] = _req_date
            else:
                # Find nearest valid expiry
                _nearest = min(_valid_expiry_map.keys(),
                               key=lambda d: abs((d - _req_date).days))
                _gap = abs((_nearest - _req_date).days)
                print(f"  ⚠ Trade expiry {_t.expiry_date} not in Fyers — "
                      f"nearest valid: {_nearest} (gap {_gap}d)")
                _trade_expiry_map[_t.expiry_date] = _nearest

        # Step 3: For each resolved expiry, fetch the option chain to get real symbols
        #  Then match our legs by strike + option_type
        _sym_to_leg = {}   # fyers_symbol -> (strike_int, opt_type, expiry_str)
        for _t in trades:
            _resolved = _trade_expiry_map[_t.expiry_date]
            _ts = _ts_map.get(_resolved, "")
            _flag = _valid_expiry_map.get(_resolved, "W")

            _oc_resp = _fyers.optionchain(
                data={"symbol": "NSE:NIFTY50-INDEX", "strikecount": 40, "timestamp": _ts}
            )
            # Build a lookup: (strike_int, opt_type) -> fyers_symbol from chain
            _chain_sym = {}
            for _o in _oc_resp.get("data", {}).get("optionsChain", []):
                _sp = _o.get("strike_price", -1)
                _ot = _o.get("option_type", "")
                if _sp > 0 and _ot:
                    _chain_sym[(int(_sp), _ot)] = _o["symbol"]

            for _lg in _t.get_legs():
                _key = (int(_lg.strike), _lg.option_type)
                if _key in _chain_sym:
                    _fyers_sym = _chain_sym[_key]
                    _sym_to_leg[_fyers_sym] = (int(_lg.strike), _lg.option_type, _t.expiry_date)
                else:
                    print(f"  ⚠ Strike {_lg.strike} {_lg.option_type} not found in Fyers chain "
                          f"for {_resolved} — will use BS estimate")

        _symbols = list(_sym_to_leg.keys())
        print(f"  Fetching live LTP for {len(_symbols)} option leg(s):")
        for _s in _symbols:
            print(f"    {_s}")

        if not _symbols:
            raise ValueError("No valid Fyers symbols found for trade legs")

        _opt_resp = _fyers.quotes(data={"symbols": ",".join(_symbols)})
        if _opt_resp.get("s") != "ok":
            raise ConnectionError(f"Fyers option quote failed: {_opt_resp}")

        # Parse response — key on (strike_int, opt_type)
        # Also store OI and volume for display
        _fyers_live_oi_vol = {}  # (strike_int, opt_type) -> {"oi": ..., "volume": ..., "bid": ..., "ask": ...}
        _opt_calls, _opt_puts = [], []
        for _item in _opt_resp.get("d", []):
            _info = _sym_to_leg.get(_item["n"])
            if not _info:
                continue
            _strike_int, _opt_type, _exp = _info
            _v   = _item["v"]
            _ltp = _v.get("lp", 0)   # last traded price
            # Fallback: use mid-price if ltp is 0 (can happen at day open)
            if _ltp == 0:
                _bid = _v.get("bid", 0)
                _ask = _v.get("ask", 0)
                if _bid > 0 and _ask > 0:
                    _ltp = round((_bid + _ask) / 2, 2)
            _fyers_live_ltp[(_strike_int, _opt_type)] = _ltp
            _fyers_live_oi_vol[(_strike_int, _opt_type)] = {
                "oi": _v.get("oi", 0),
                "volume": _v.get("volume", 0),
                "bid": _v.get("bid", 0),
                "ask": _v.get("ask", 0),
            }

            _sd = StrikeData(
                strike=_strike_int,
                option_type=_opt_type,
                ltp=_ltp,
                bid=_v.get("bid", 0),
                ask=_v.get("ask", 0),
                oi=_v.get("oi", 0),
                oi_change=0,
                volume=_v.get("volume", 0),
                iv=0.0,
                expiry=_exp,
            )
            if _opt_type == "CE":
                _opt_calls.append(_sd)
            else:
                _opt_puts.append(_sd)

            print(f"    ✓ {_item['n']}: LTP=₹{_ltp:.2f}  "
                  f"bid=₹{_v.get('bid',0):.2f}  ask=₹{_v.get('ask',0):.2f}  "
                  f"OI={_v.get('oi',0):,}  vol={_v.get('volume',0):,}")

        if not _fyers_live_ltp:
            raise ValueError("Fyers returned no option data")

        # Primary expiry = the one with the most legs (usually all same)
        from collections import Counter as _Counter
        _primary_expiry = _Counter(e for _, _, e in _sym_to_leg.values()).most_common(1)[0][0]

        live_snapshot = OptionChainSnapshot(
            timestamp=str(_dt.now()),
            spot_price=_fyers_spot,
            expiry_dates=[_primary_expiry],
            selected_expiry=_primary_expiry,
            calls=[s for s in _opt_calls if s.expiry == _primary_expiry],
            puts=[s  for s in _opt_puts  if s.expiry == _primary_expiry],
            pcr_oi=0.0,
            pcr_volume=0.0,
            max_pain=0,
            total_call_oi=sum(s.oi for s in _opt_calls),
            total_put_oi=sum(s.oi  for s in _opt_puts),
            atm_strike=round(_fyers_spot / 50) * 50,
        )

        print(f"\n  ✓ Fyers data ready — {len(_opt_calls)} CE, {len(_opt_puts)} PE "
              f"| Expiry: {_primary_expiry} | Spot: {_fyers_spot:,.2f}")
        print(f"  ✓ Trade expiry {_primary_expiry} — LIVE Fyers LTP will be used")
            
    except Exception as fyers_error:
        print(f"  Fyers fetch failed ({fyers_error})")
        print("  Falling back to NSE/Groww for live chain...")
        
        # Fallback to NSE/Groww
        try:
            from data.option_chain import NSEOptionChainFetcher
            nse_fetcher = NSEOptionChainFetcher()
            snapshot = nse_fetcher.fetch()
            if snapshot and (snapshot.calls or snapshot.puts):
                live_spot = snapshot.spot_price if snapshot.spot_price > 0 else spot
                chain_expiry = snapshot.selected_expiry
                trade_expiries = sorted({t.expiry_date for t in trades})
                print(f"  NSE/Groww chain: {len(snapshot.calls or [])} calls, {len(snapshot.puts or [])} puts | "
                      f"Spot: {live_spot:,.2f} | Chain expiry: {chain_expiry}")

                # Check expiry alignment for each trade
                from datetime import datetime as _dt
                for texp in trade_expiries:
                    trade_exp_dt = _dt.strptime(texp, "%Y-%m-%d").date()
                    try:
                        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
                            try:
                                snap_exp_dt = _dt.strptime(chain_expiry, fmt).date()
                                break
                            except ValueError:
                                continue
                        else:
                            snap_exp_dt = None
                        gap = abs((trade_exp_dt - snap_exp_dt).days) if snap_exp_dt else 999
                        if gap <= 3:
                            print(f"  Trade exp {texp} matches chain — LIVE prices will be used")
                        else:
                            print(f"  Trade exp {texp} != chain {chain_expiry} (gap {gap}d) — BS with live spot")
                    except Exception:
                        print(f"  Trade exp {texp} — expiry match check skipped")

                live_snapshot = snapshot
            else:
                print("  Could not fetch live chain — using BS estimates with closing data.")
        except Exception as e:
            print(f"  Live chain fetch failed ({e}) — using BS estimates.")

    print(f"\n  [3/3] Analyzing {len(trades)} active trade(s)...")

    # Prefer Fyers live spot over stale Yahoo close when available
    _effective_spot = _fyers_spot if _fyers_spot > 0 else spot
    _effective_vix  = _fyers_vix  if _fyers_vix  > 0 else vix

    # ── Unified exit: use SAME strategy + engine logic as backtest ──
    from exits import evaluate_exit, TradeAdapter, ExitDecision
    from models.trade_monitor import ExitRecommendation

    for trade in trades:
        # One strategy instance per trade (see _monitor_strategy_for_trade)
        monitor_strategy = _monitor_strategy_for_trade(trade, config)
        entry_vix, entry_spot = _entry_context_for_trade(trade, spot, vix)

        # Step 1: Compute live P&L using Fyers LTP (or BS fallback)
        legs = trade.get_legs()
        dte = max(trade.dte, 1)
        live_spot_for_trade = _effective_spot if _effective_spot > 0 else spot

        per_leg_pnl = []
        total_pnl = 0.0
        for leg in legs:
            from pricing.black_scholes import price_option as _pp, iv_from_vix as _iv, OptionType as _OT
            ot = _OT.PUT if leg.option_type == "PE" else _OT.CALL
            leg_key = (int(leg.strike), leg.option_type)
            live_price = _fyers_live_ltp.get(leg_key)
            if live_price is not None and live_price > 0:
                ltp = live_price
                price_src = "LIVE"
            else:
                iv_val = _iv(_effective_vix, leg.strike, live_spot_for_trade, ot)
                ltp = _pp(live_spot_for_trade, leg.strike, dte, iv_val, 0.065, ot).premium
                price_src = "BS"
            lpnl = leg.leg_pnl(ltp)
            total_pnl += lpnl

            oi_vol_data = _fyers_live_oi_vol.get(leg_key) if _fyers_live_oi_vol else None
            leg_info = {
                "leg": f"{'SELL' if leg.is_short else 'BUY'} {leg.strike:.0f} {leg.option_type}",
                "entry": leg.entry_price, "current_bs": round(ltp, 2),
                "pnl": round(lpnl, 0), "qty": leg.qty, "source": price_src,
            }
            if oi_vol_data:
                leg_info.update({"oi": oi_vol_data.get("oi", 0), "volume": oi_vol_data.get("volume", 0),
                                 "bid": oi_vol_data.get("bid", 0), "ask": oi_vol_data.get("ask", 0)})
            per_leg_pnl.append(leg_info)

        if trade.current_pnl is not None:
            total_pnl = trade.current_pnl

        total_qty = trade.total_qty
        pnl_per_unit = total_pnl / total_qty if total_qty > 0 else 0

        # Step 2: Load peak P&L (persisted in active_trades.json across runs)
        # If undefined, initialize with current P&L as baseline
        peak_pnl = trade.peak_pnl_per_unit
        if peak_pnl is None:
            peak_pnl = pnl_per_unit
            trade.peak_pnl_per_unit = peak_pnl
            print(f"  📈 Initialized peak P&L tracking at ₹{peak_pnl:.2f} per unit")

        # Step 3: Build adapter so strategy.should_exit() can evaluate
        adapter = TradeAdapter(trade, total_pnl, pnl_per_unit, entry_vix=entry_vix,
                               peak_pnl_per_unit=peak_pnl)
        
        # Step 4: Update peak if current P&L is higher
        if pnl_per_unit > peak_pnl:
            peak_pnl = pnl_per_unit
            trade.peak_pnl_per_unit = peak_pnl

        # Step 4: Call UNIFIED exit logic — same as backtest
        decision = evaluate_exit(
            strategy=monitor_strategy,
            trade_adapter=adapter,
            spot=live_spot_for_trade,
            vix=_effective_vix,
            dte=dte,
            market_data=latest,
            equity=config.initial_capital,
            exit_engine=engine if engine.is_trained else None,
            entry_spot=entry_spot,
            entry_vix=entry_vix,
            peak_pnl_per_unit=peak_pnl,
        )

        # Step 5: Convert unified ExitDecision → ExitRecommendation (for display)
        max_profit = trade.max_profit_total
        pnl_pct = (total_pnl / max_profit * 100) if max_profit > 0 else 0
        net_credit = trade.net_entry_credit
        theta_per_day = net_credit / max(dte + trade.days_in_trade, 1)

        rec = ExitRecommendation(
            trade_id=trade.trade_id,
            action=decision.action,
            confidence=decision.confidence,
            current_pnl_pct=pnl_pct,
            pnl_rupees=total_pnl,
            days_in_trade=trade.days_in_trade,
            dte_remaining=dte,
            reasoning=decision.reasoning,
            risk_score=decision.risk_score,
            theta_per_day=theta_per_day,
            per_leg_pnl=per_leg_pnl,
        )
        _print_exit_recommendation(trade, rec, _effective_spot, _effective_vix)

    # Persist updated peak P&L so 85% tracking survives across monitor runs
    save_active_trades(trades)

    print(f"\n{'=' * 80}")
    print(f"  MANAGEMENT COMMANDS")
    print(f"{'=' * 80}")
    print(f"  Re-check:    python main.py --mode monitor")
    print(f"  Add trade:   python main.py --mode add-trade --trade-id <id> ...")
    print(f"  Close trade: python main.py --mode close-trade --trade-id <id> --realized-pnl <₹>")
    print(f"  List trades: python main.py --mode list-trades")


def _print_exit_recommendation(trade: ActiveTrade, rec, spot: float, vix: float):
    """Pretty-print exit recommendation for any multi-leg trade."""
    action_icons = {
        "HOLD": "🟢", "BOOK_PROFIT": "🟡", "TRAIL_STOP": "🟠",
        "EXIT_NOW": "🔴", "PARTIAL_EXIT": "🟠",
    }
    icon = action_icons.get(rec.action, "⚪")
    legs = trade.get_legs()

    print(f"\n  {'─' * 75}")
    print(f"  TRADE: {trade.trade_id}")
    print(f"  {'─' * 75}")
    print(f"  Strategy: {trade.strategy_name}")
    print(f"  Entry:    {trade.entry_date} | Expiry: {trade.expiry_date} ({trade.dte} DTE)")
    print(f"  Days in trade: {trade.days_in_trade}")

    sources = {lp.get("source", "BS") for lp in (rec.per_leg_pnl or [])}
    any_live = "LIVE" in sources
    price_label = "Current" if any_live else "Current (est)"
    has_oi_data = any(lp.get("oi") is not None for lp in (rec.per_leg_pnl or []))
    
    print(f"\n  LEGS:")
    if has_oi_data:
        # Enhanced display with OI and volume
        print(f"    {'Action':<6} {'Strike':>8} {'Type':<4} {'Qty':>6} {'Entry':>10} {price_label:>14} {'Leg P&L':>12} {'OI':>10} {'Volume':>10} {'Src':>5}")
        print(f"    {'─' * 110}")
        for lp in (rec.per_leg_pnl or []):
            src = lp.get("source", "BS")
            oi = lp.get("oi", 0)
            vol = lp.get("volume", 0)
            oi_str = f"{oi:,}" if oi > 0 else "-"
            vol_str = f"{vol:,}" if vol > 0 else "-"
            print(f"    {lp['leg']:<20} {lp['qty']:>6} ₹{lp['entry']:>8.2f} ₹{lp['current_bs']:>8.2f} ₹{lp['pnl']:>+10,.0f} {oi_str:>10} {vol_str:>10}  [{src}]")
    else:
        # Standard display without OI
        print(f"    {'Action':<6} {'Strike':>8} {'Type':<4} {'Qty':>6} {'Entry':>10} {price_label:>14} {'Leg P&L':>12} {'Src':>5}")
        print(f"    {'─' * 75}")
        for lp in (rec.per_leg_pnl or []):
            src = lp.get("source", "BS")
            print(f"    {lp['leg']:<20} {lp['qty']:>6} ₹{lp['entry']:>8.2f} ₹{lp['current_bs']:>8.2f} ₹{lp['pnl']:>+10,.0f}  [{src}]")
    
    if "BS*" in sources:
        print(f"    [BS*] = Black-Scholes estimate using live spot/VIX (chain expiry mismatch)")
    elif "BS" in sources:
        print(f"    [BS]  = Black-Scholes estimate (no live chain available)")
    
    # Show bid-ask spread if available
    if has_oi_data and any(lp.get("bid") is not None and lp.get("ask") is not None for lp in (rec.per_leg_pnl or [])):
        print(f"\n  BID-ASK SPREADS:")
        for lp in (rec.per_leg_pnl or []):
            bid = lp.get("bid", 0)
            ask = lp.get("ask", 0)
            if bid > 0 and ask > 0:
                spread = ask - bid
                spread_pct = (spread / ((bid + ask) / 2)) * 100 if (bid + ask) > 0 else 0
                print(f"    {lp['leg']:<20} Bid: ₹{bid:>7.2f}  Ask: ₹{ask:>7.2f}  Spread: ₹{spread:>5.2f} ({spread_pct:.1f}%)")

    net_credit = trade.net_entry_credit
    print(f"\n  Net Credit:  ₹{net_credit:,.0f}")
    print(f"  Max Profit:  ₹{trade.max_profit_total:,.0f}")
    print(f"  Max Loss:    ₹{trade.max_loss_total:,.0f}")

    print(f"\n  {icon} RECOMMENDATION: {rec.action}")
    print(f"  {'─' * 50}")
    print(f"  Current P&L:    {rec.current_pnl_pct:+.1f}% of max profit = ₹{rec.pnl_rupees:+,.0f}")
    risk_label = 'SAFE' if rec.risk_score < 0.3 else 'CAUTION' if rec.risk_score < 0.6 else 'DANGER'
    print(f"  Risk Score:     {rec.risk_score:.0%} ({risk_label})")
    print(f"  Confidence:     {rec.confidence:.0%}")
    print(f"  Theta/day:      ₹{rec.theta_per_day:,.0f}")

    for sl in [l for l in legs if l.is_short]:
        if sl.option_type == "PE":
            dist = (spot - sl.strike) / spot * 100
        else:
            dist = (sl.strike - spot) / spot * 100
        status = 'SAFE' if dist > 3 else 'CLOSE' if dist > 1 else 'BREACHED'
        print(f"  Short {sl.strike:.0f} {sl.option_type}:  {dist:+.1f}% from spot — {status}")

    print(f"\n  Analysis:")
    for r in rec.reasoning:
        print(f"    • {r}")

    if rec.action == "BOOK_PROFIT":
        print(f"\n  >>> ACTION: Close ALL legs now to lock in ₹{rec.pnl_rupees:+,.0f} profit")
    elif rec.action == "EXIT_NOW":
        print(f"\n  >>> ACTION: Exit ALL legs immediately. Current: ₹{rec.pnl_rupees:+,.0f}")
        print(f"      Remaining max loss if held: ₹{trade.max_loss_total:,.0f}")
    elif rec.action == "PARTIAL_EXIT":
        print(f"\n  >>> ACTION: Close these legs (at-risk side):")
        for pl in (rec.partial_exit_legs or []):
            print(f"      - {pl}")
        profitable_legs = [lp for lp in (rec.per_leg_pnl or []) if lp["pnl"] > 0]
        if profitable_legs:
            print(f"      Keep profitable side running:")
            for pl in profitable_legs:
                print(f"      - {pl['leg']} (P&L: ₹{pl['pnl']:+,.0f})")
    elif rec.action == "TRAIL_STOP":
        trail_stop = rec.pnl_rupees * 0.5
        print(f"\n  >>> ACTION: Set trailing stop to lock in ₹{trail_stop:,.0f} minimum")
    elif rec.action == "HOLD":
        remaining_theta = rec.theta_per_day * rec.dte_remaining
        print(f"\n  >>> ACTION: Continue holding. Expected remaining theta: ₹{remaining_theta:,.0f}")


def run_add_trade(args) -> None:
    """Add a trade — supports both --leg format and legacy single-spread format."""
    if args.leg:
        trade = add_trade_from_legs(
            trade_id=args.trade_id,
            entry_date=args.entry_date,
            expiry_date=args.expiry_date,
            leg_strings=args.leg,
            current_pnl=args.current_pnl,
            notes=args.notes or "",
            strategy_code=args.strategy_code,
            entry_vix=args.entry_vix,
            entry_spot=args.entry_spot,
        )
        legs = trade.get_legs()
        print(f"\n  Trade added: {trade.trade_id}")
        print(f"  Strategy: {trade.strategy_name} ({len(legs)} legs)")
        print(f"  Expiry: {trade.expiry_date} ({trade.dte} DTE)")
        print(f"\n  Legs:")
        for lg in legs:
            print(f"    {lg.action:<5} {lg.strike:>8.0f} {lg.option_type:<3} "
                  f"× {lg.qty} @ ₹{lg.entry_price:.2f}")
        print(f"\n  Net Credit:  ₹{trade.net_entry_credit:,.0f}")
        print(f"  Max Profit:  ₹{trade.max_profit_total:,.0f}")
        print(f"  Max Loss:    ₹{trade.max_loss_total:,.0f}")
    else:
        trade = add_trade(
            trade_id=args.trade_id,
            direction=args.direction,
            entry_date=args.entry_date,
            expiry_date=args.expiry_date,
            short_strike=args.short_strike,
            long_strike=args.long_strike,
            entry_credit=args.entry_credit,
            lots=args.lots,
            lot_size=args.lot_size,
            notes=args.notes or "",
            strategy_code=args.strategy_code,
            entry_vix=args.entry_vix,
            entry_spot=args.entry_spot,
        )
        print(f"\n  Trade added: {trade.trade_id}")
        print(f"  {trade.strategy_name}")
        print(f"  SELL {trade.short_strike} / BUY {trade.long_strike}")
        print(f"  Credit: ₹{trade.entry_credit:.1f}/unit | {trade.lots} lots × {trade.lot_size}")
        print(f"  Max Profit: ₹{trade.max_profit_total:,.0f} | Max Loss: ₹{trade.max_loss_total:,.0f}")
        print(f"  Expiry: {trade.expiry_date} ({trade.dte} DTE)")

    print(f"\n  Run 'python main.py --mode monitor' to get exit analysis")


def run_remove_trade(args) -> None:
    """Remove a trade, or archive+close it when realized P&L is provided."""
    if args.realized_pnl is not None:
        closed = close_trade(
            trade_id=args.trade_id,
            realized_pnl=args.realized_pnl,
            exit_date=args.exit_date,
            exit_reason=args.exit_reason or "manual_close",
            notes=args.notes or "",
            strategy_code=args.strategy_code,
        )
        print(f"\n  Closed trade: {closed.trade_id}")
        print(f"  Archived as actual trade for training")
        print(f"  Strategy: {closed.strategy}")
        print(f"  Realized P&L: ₹{closed.total_pnl:+,.0f} | Return on risk: {closed.pnl_pct:+.1f}%")
        if closed.strategy == "unknown":
            print("  Note: strategy is not mapped to the learner yet, so this record will be skipped in training.")
    else:
        remove_trade(args.trade_id)
        print(f"\n  Removed trade: {args.trade_id}")

    remaining = load_active_trades()
    if remaining:
        print(f"  Remaining active trades: {len(remaining)}")
        for t in remaining:
            print(f"    - {t.trade_id}: {t.strategy_name} ({t.dte} DTE)")
    else:
        print("  No active trades remaining.")


def run_close_trade(args) -> None:
    """Explicit close/archive wrapper around the trade journal."""
    closed = close_trade(
        trade_id=args.trade_id,
        realized_pnl=args.realized_pnl,
        exit_date=args.exit_date,
        exit_reason=args.exit_reason or "manual_close",
        notes=args.notes or "",
        strategy_code=args.strategy_code,
    )
    print(f"\n  Closed trade: {closed.trade_id}")
    print(f"  Archived as actual trade for training")
    print(f"  Strategy: {closed.strategy}")
    print(f"  Realized P&L: ₹{closed.total_pnl:+,.0f} | Return on risk: {closed.pnl_pct:+.1f}%")
    if closed.strategy == "unknown":
        print("  Note: strategy is not mapped to the learner yet, so this record will be skipped in training.")

    remaining = load_active_trades()
    if remaining:
        print(f"  Remaining active trades: {len(remaining)}")
        for t in remaining:
            print(f"    - {t.trade_id}: {t.strategy_name} ({t.dte} DTE)")
    else:
        print("  No active trades remaining.")


def run_list_trades() -> None:
    """List all active trades with leg details."""
    trades = load_active_trades()
    if not trades:
        print("\n  No active trades.")
        return
    print(f"\n  {'=' * 85}")
    print(f"  ACTIVE TRADES ({len(trades)})")
    print(f"  {'=' * 85}")
    for t in trades:
        legs = t.get_legs()
        print(f"\n  {t.trade_id}")
        print(f"    Strategy: {t.strategy_name} | Expiry: {t.expiry_date} ({t.dte} DTE) | "
              f"Days: {t.days_in_trade}")
        for lg in legs:
            print(f"      {lg.action:<5} {lg.strike:>8.0f} {lg.option_type:<3} × {lg.qty:>4} "
                  f"@ ₹{lg.entry_price:>8.2f}")
        print(f"    Net Credit: ₹{t.net_entry_credit:,.0f} | "
              f"Max P: ₹{t.max_profit_total:,.0f} | Max L: ₹{t.max_loss_total:,.0f}")


def main():
    parser = argparse.ArgumentParser(description="Nifty Options Backtester + ML Learner")
    parser.add_argument(
        "--mode",
        choices=[
            "backtest", "backtest-combined", "signal", "signal-combined", "evolve", "monitor", "validate",
            "add-trade", "close-trade", "remove-trade", "list-trades",
            "ablation", "stress", "refresh-data", "retrain-models",
        ],
        default="backtest",
        help="Run mode",
    )
    parser.add_argument("--start", type=str, default="2009-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=str(date.today()), help="End date (YYYY-MM-DD, default: today)")
    parser.add_argument(
        "--weekly-start", type=str, default=None,
        help="backtest-combined: first date the weekly track may trade (YYYY-MM-DD); metrics start here. Features/models still use full history from --start.",
    )
    parser.add_argument("--capital", type=float, default=500000, help="Initial capital (INR)")
    parser.add_argument("--lots", type=int, default=15, help="Max lots per trade")
    parser.add_argument("--dte", type=int, default=21, help="Target DTE for entry")
    parser.add_argument(
        "--entry-model", type=str, default="v4",
        help="Entry model version (default: v4, the production-optimized version)",
    )
    parser.add_argument(
        "--run-label", type=str, default="",
        help="Human-readable label for this backtest run (logged to changelog)",
    )

    # Trade management args — multi-leg format (preferred)
    parser.add_argument(
        "--leg", action="append", default=None,
        help='Add a leg: "SELL 21500 PE 130 @ 361.95" (repeatable for multi-leg)',
    )
    # Trade management args — common
    parser.add_argument("--trade-id", type=str, help="Unique trade identifier")
    parser.add_argument("--entry-date", type=str, help="Trade entry date (YYYY-MM-DD)")
    parser.add_argument("--expiry-date", type=str, help="Option expiry date (YYYY-MM-DD)")
    parser.add_argument("--exit-date", type=str, help="Trade exit date when closing (YYYY-MM-DD)")
    parser.add_argument("--exit-reason", type=str, default="", help="Optional realized exit reason")
    parser.add_argument("--notes", type=str, default="", help="Optional notes for the trade")
    parser.add_argument("--current-pnl", type=float, help="Override P&L from broker (₹)")
    parser.add_argument("--realized-pnl", type=float, help="Realized P&L in ₹ when closing a trade")
    parser.add_argument(
        "--strategy-code", type=str,
        help="Optional learner strategy code for training, e.g. put_credit_spread or iron_condor. "
             "Use weekly_ic or weekly_pcs for weekly trades (monitor exit logic keys off this)",
    )
    parser.add_argument("--entry-vix", type=float, help="India VIX at entry (monitor exit checks)")
    parser.add_argument("--entry-spot", type=float, help="Nifty spot at entry (monitor exit checks)")
    # Legacy single-spread args (still supported)
    parser.add_argument("--direction", type=str, choices=["put", "call"], help="Spread direction (legacy)")
    parser.add_argument("--short-strike", type=float, help="Short strike price (legacy)")
    parser.add_argument("--long-strike", type=float, help="Long strike price (legacy)")
    parser.add_argument("--entry-credit", type=float, help="Credit received per unit (legacy)")
    parser.add_argument("--lot-size", type=int, default=65, help="Lot size (default 65)")

    args = parser.parse_args()

    config = BacktestConfig(
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
        initial_capital=args.capital,
        max_lots=args.lots,
        entry_model_version=args.entry_model,
    )

    if args.mode == "backtest":
        run_backtest(config, args.dte, run_label=args.run_label)
    elif args.mode == "backtest-combined":
        run_backtest_combined(config, args.dte, run_label=args.run_label,
                              weekly_start=args.weekly_start)
    elif args.mode == "signal":
        run_signal(config)
    elif args.mode == "signal-combined":
        run_signal_combined(config)
    elif args.mode == "evolve":
        run_evolve(config)
    elif args.mode == "monitor":
        run_monitor(config)
    elif args.mode == "add-trade":
        if not all([args.trade_id, args.entry_date, args.expiry_date]):
            parser.error("add-trade requires: --trade-id, --entry-date, --expiry-date")
        if not args.leg and not all([args.direction, args.short_strike,
                                      args.long_strike, args.entry_credit]):
            parser.error(
                "add-trade needs either:\n"
                '  --leg "SELL 21500 PE 130 @ 361.95" --leg "BUY 20500 PE 130 @ 167.65"\n'
                "  OR: --direction put --short-strike 21500 --long-strike 20500 --entry-credit 194.3"
            )
        run_add_trade(args)
    elif args.mode == "remove-trade":
        if not args.trade_id:
            parser.error("remove-trade requires: --trade-id")
        run_remove_trade(args)
    elif args.mode == "close-trade":
        if not args.trade_id or args.realized_pnl is None:
            parser.error("close-trade requires: --trade-id, --realized-pnl")
        run_close_trade(args)
    elif args.mode == "list-trades":
        run_list_trades()
    elif args.mode == "ablation":
        run_ablation(config, args)
    elif args.mode == "stress":
        run_stress_test(config, args)
    elif args.mode == "validate":
        run_validate(config)
    elif args.mode == "refresh-data":
        run_data_refresh(config)
    elif args.mode == "retrain-models":
        run_retrain_models(config)


def _purge_short_parquets(required_start: date) -> int:
    """
    Delete per-ticker parquet caches whose earliest data row starts more than
    7 calendar days after required_start (the first trading day may legitimately
    fall a few days later, e.g. a weekend start).  These caches are too short to
    serve a full-history backtest and would silently truncate data if left in place.

    Only files listed in the cache manifest are considered; each removed file's
    manifest entry is dropped too.  Unlisted files are never touched.
    """
    from datetime import timedelta
    from data import market_data as md
    manifest = md.load_manifest()
    cleared = 0
    for name in list(manifest):
        p = md.CACHE_DIR / name
        if not p.exists():
            manifest.pop(name, None)
            continue
        try:
            df = pd.read_parquet(p)
            if df.empty:
                continue
            earliest = pd.Timestamp(df.index.min()).date()
            if earliest > required_start + timedelta(days=7):
                p.unlink()
                manifest.pop(name, None)
                cleared += 1
        except Exception:
            pass
    md.save_manifest(manifest)
    return cleared


def run_data_refresh(config: BacktestConfig) -> pd.DataFrame:
    """
    Rebuild the market data feature store (88 columns, full date range) WITHOUT
    touching the ML model caches (.pkl files).

    Steps:
      [1/2] Purge stale per-ticker parquet caches that start after config.start_date
      [2/2] Re-fetch all tickers from Yahoo Finance and cache fresh parquets

    The backtest can be run immediately after this without --mode evolve
    (existing .pkl models are still used; run evolve later to retrain on new data).
    """
    from data.market_data import MarketDataFetcher

    print(f"\n{'=' * 70}")
    print(f"  MARKET DATA REFRESH")
    print(f"  Period : {config.start_date} → {config.end_date}")
    print(f"{'=' * 70}\n")

    print("[1/2] Clearing parquet caches with incomplete date coverage...")
    cleared = _purge_short_parquets(config.start_date)
    print(f"  Cleared {cleared} stale cache file(s) (earliest row > {config.start_date})")

    print("\n[2/2] Fetching full market data from Yahoo Finance...")
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fetcher = MarketDataFetcher(config.start_date, config.end_date)
        data = fetcher.build_combined_dataset()

    actual_start = data.index.min().date() if not data.empty else config.start_date
    actual_end   = data.index.max().date() if not data.empty else config.end_date

    print(f"\n  ✓ Feature store rebuilt")
    print(f"    Rows     : {len(data):,}  trading days")
    print(f"    Columns  : {len(data.columns)}  features")
    print(f"    Coverage : {actual_start} → {actual_end}")

    if actual_start > config.start_date:
        gap_days = (actual_start - config.start_date).days
        print(f"\n  ⚠  WARNING: Data starts {gap_days} days later than requested.")
        print(f"     Requested : {config.start_date}")
        print(f"     Got       : {actual_start}")
        print(f"     Some tickers (BZ=F, CL=F, ^VIX) may not have pre-{actual_start} history")
        print(f"     available via Yahoo Finance at this time.")

    print(f"\n  ML model caches (.pkl) were NOT modified.")
    print(f"  Run --mode evolve to retrain models on the refreshed data if needed.\n")
    return data


def run_retrain_models(config: BacktestConfig) -> None:
    """
    Retrain ML models (entry, exit, weekly-risk) on current feature data using
    existing evolved strategy parameters — skips the 5-15 min strategy grid search.

    Use this when:
    - .pkl files are incompatible with the installed numpy/sklearn version
    - A few months of new market data have accumulated since last --mode evolve
    - Strategy logic has NOT changed (otherwise run --mode evolve to re-grid-search)

    Prerequisites:
    - data/.cache/evolved_strategies.json must exist (run --mode evolve once first)
    - Feature data should be fresh (run --mode refresh-data --start 2009-01-01 first)

    Time: ~10-20 min (vs 25-50 min for --mode evolve)
    """
    from data.market_data import MarketDataFetcher
    from models.strategy_evolver import StrategyEvolver
    from models.layered_evolve import run_layered_training
    from models.weekly_risk_engine import save_risk_engine

    print(f"\n{'=' * 70}")
    print(f"  ML MODEL RETRAIN  (skip grid search, reuse evolved params)")
    print(f"  Period : {config.start_date} → {config.end_date}")
    print(f"{'=' * 70}\n")

    # Step 1: Load existing evolved strategy params from cache
    print("[1/3] Loading evolved strategy params from cache...", flush=True)
    cached_evolved = StrategyEvolver.load_from_cache()
    cache_age = StrategyEvolver.cache_age_days()
    if not cached_evolved:
        print("  ERROR: data/.cache/evolved_strategies.json not found.")
        print("  Run --mode evolve first to generate strategy parameters, then retry.")
        return
    print(f"  Loaded {len(cached_evolved)} evolved strategies "
          f"(evolved {cache_age}d ago)")
    for regime, strat in sorted(cached_evolved.items()):
        print(f"    {regime}: {strat.direction} sd={strat.sd} w={strat.spread_width} "
              f"pt={strat.profit_target_pct:.0f}% sl={strat.stop_loss_pct:.0f}%")
    if cache_age and cache_age > 90:
        print(f"  ⚠  Cache is {cache_age} days old — consider --mode evolve to re-optimise.")

    # Step 2: Load feature data (from parquet cache, no Yahoo re-fetch needed)
    print("\n[2/3] Loading feature data...", flush=True)
    import warnings
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        fetcher = MarketDataFetcher(config.start_date, config.end_date)
        data = fetcher.build_combined_dataset()
    print(f"  {len(data):,} trading days | {len(data.columns)} features "
          f"| {data.index.min().date()} → {data.index.max().date()}")

    # Step 3: Layered walk-forward ML retraining (same as evolve steps 4+5)
    print("\n[3/3] Retraining ML models (layered walk-forward)...", flush=True)
    layered = run_layered_training(
        data=data,
        config=config,
        evolved_strategies=cached_evolved,
        verbose=True,
    )

    entry_model = layered.entry_model
    exit_model  = layered.exit_model
    risk_eng    = layered.weekly_risk_engine

    entry_model.save()
    exit_model.save()
    print(f"\n  Saved entry model  → data/.cache/entry_model_v4.pkl")
    print(f"  Saved exit model   → data/.cache/exit_model.pkl")
    if risk_eng is not None:
        save_risk_engine(risk_eng)
        print(f"  Saved weekly risk  → data/.cache/weekly_risk_engine_v2.pkl")

    print(f"\n  Training samples   : {len(layered.entry_training_trades)} entry | "
          f"{len(layered.weekly_training_trades)} weekly")
    if layered.stage_stats:
        last = layered.stage_stats[-1]
        print(f"  Final stage AUC   : {last.entry_cv_auc:.3f}")

    # Quick live signal from freshly-retrained model
    latest = data.iloc[-1]
    pred = entry_model.predict(latest)
    print(f"\n  LIVE SIGNAL (retrained model):")
    print(f"    Signal   : {pred['signal']}")
    print(f"    Quality  : {pred.get('quality_score', pred.get('probability_profitable', 0)):.1%}")
    print(f"    Strategy : {pred.get('recommended_strategy', '??')}")
    print(f"\n  ✓ Models retrained. Run --mode backtest-combined to validate.\n")


def run_signal_combined(config: BacktestConfig) -> None:
    """
    Combined monthly + weekly trade signal — mirrors CombinedBacktestEngine.

    Architecture (identical to the backtester):
      Monthly track (50% budget): regime-aware ML, 15-60 DTE, monthly expiries
      Weekly track  (50% budget): rule-based VIX-gated, 3-8 DTE, weekly expiry
      Cross-track risk: DD circuit breaker, simultaneous VIX cap at 22
    """
    # Capital split — must match CombinedBacktestEngine constructor in run_backtest_combined()
    _monthly_pct = 0.50   # v8 (2026-08-23): shifted from 0.70
    _weekly_pct  = 0.50   # v8 (2026-08-23): shifted from 0.30
    from config import WeeklyBacktestConfig
    from strategies.weekly_strategies import WeeklyPutCreditSpread, WeeklyIronCondor
    from data.expiry_calendar import get_weekly_expiry_in_range, get_all_expiries
    from backtester.position_sizer import PositionSizer, _regime_from_vix as _vix_regime
    from models.strategy_evolver import StrategyEvolver
    from backtester.rolling_simulator import RollingWindowSimulator, SimConfig
    from models.regime_aware_learner import RegimeAwareLearner

    wc = WeeklyBacktestConfig()

    print("\n" + "=" * 75)
    print("  ML-POWERED COMBINED SIGNAL  (Monthly 50% + Weekly 50%,  11:00–13:00 IST)")
    print("  Mirrors CombinedBacktestEngine — regime-adaptive + weekly gamma")
    print("=" * 75)

    # ── 0. Trading-day gate (IST date; weekends and NSE holidays) ───────────
    _gate_day = now_ist().date()
    _closed_reason = market_day_gate(_gate_day)
    if _closed_reason:
        print(f"\n  {_closed_reason} — {_gate_day.strftime('%a %d %b %Y')}.")
        print("  No signal generated. Next run on the next trading day.\n")
        return

    # ── 1. Market data ──────────────────────────────────────────────────────
    lookback_start = config.start_date
    fetcher = MarketDataFetcher(lookback_start, config.end_date)
    data = fetcher.build_combined_dataset()
    if len(data) == 0:
        print("  ERROR: Could not fetch market data.")
        return
    print(f"\n  Training on {lookback_start} to {config.end_date} "
          f"({round((config.end_date - lookback_start).days / 365, 1)}-year window) | "
          f"{len(data)} trading days")

    latest = data.iloc[-1]
    today = now_ist().date()  # IST calendar date, not host (UTC) date
    spot = latest.get("nifty_close", 0)
    vix  = latest.get("vix", 0)
    data_date_str = data.index[-1].date() if hasattr(data.index[-1], "date") else data.index[-1]

    stale_days = (today - data_date_str).days
    stale_note = f"  (data {stale_days}d old)" if stale_days > 0 else ""

    print(f"\n  {'='*75}")
    print(f"  CURRENT MARKET SNAPSHOT")
    print(f"  {'='*75}")
    print(f"  Today:      {today}  |  Data as of: {data_date_str}{stale_note}")
    
    # Try to supplement with live Fyers data if available
    live_spot_available = False
    live_vix_available = False
    if stale_days > 0:
        try:
            from data.broker import fyersModel as _fyersModel
            import os as _os_live
            load_env()

            _market_hours = is_market_hours(now_ist())

            if _market_hours:
                _fyers_live = _fyersModel.FyersModel(
                    client_id=_os_live.getenv("FYERS_CLIENT_ID"),
                    token=_os_live.getenv("FYERS_ACCESS_TOKEN"),
                    log_path=""
                )
                _idx_resp_live = _fyers_live.quotes(
                    data={"symbols": "NSE:NIFTY50-INDEX,NSE:INDIAVIX-INDEX"}
                )
                if _idx_resp_live.get("s") == "ok":
                    for _item_live in _idx_resp_live.get("d", []):
                        _lp_live = _item_live["v"].get("lp", 0)
                        if "NIFTY50" in _item_live["n"] and _lp_live > 0:
                            spot = _lp_live
                            live_spot_available = True
                        elif "INDIAVIX" in _item_live["n"] and _lp_live > 0:
                            vix = _lp_live
                            live_vix_available = True
        except Exception:
            pass
    
    if live_spot_available or live_vix_available:
        live_tag_spot = " [LIVE from Fyers]" if live_spot_available else ""
        live_tag_vix = " [LIVE from Fyers]" if live_vix_available else ""
        print(f"  Nifty:      {spot:,.2f}{live_tag_spot}  |  India VIX: {vix:.2f}{live_tag_vix}")
    else:
        print(f"  Nifty:      {spot:,.2f}  |  India VIX: {vix:.2f}")
    print(f"\n  GLOBAL MACRO:")
    print(f"    US VIX: {latest.get('us_vix', 0):.2f} | S&P 500: {latest.get('sp500', 0):,.2f} "
          f"| US 10Y: {latest.get('us_10y', 0):.2f}%")
    print(f"    Crude: ${latest.get('crude', 0):,.2f} | Gold: ${latest.get('gold', 0):,.2f}")
    print(f"    USD/INR: {latest.get('usdinr', 0):.2f} | Bank Nifty: {latest.get('nifty_bank', 0):,.2f}")

    # ── Entry window gate: 11:00–13:00 IST (permanent baseline) ─────────────
    # Nifty options spreads are tightest and spot is most stable in mid-session.
    # Both fill model and backtest use this window. Signal always enforces it.
    from datetime import time as _time_gate
    _now_gate = now_ist()

    _win_start_h = config.entry_window_start_hour
    _win_start_m = config.entry_window_start_minute
    _win_end_h   = config.entry_window_end_hour
    _win_end_m   = config.entry_window_end_minute
    _t           = _now_gate.time().replace(tzinfo=None)
    _in_window   = _time_gate(_win_start_h, _win_start_m) <= _t <= _time_gate(_win_end_h, _win_end_m)
    _is_weekday  = _now_gate.weekday() < 5
    _window_str  = f"{_win_start_h:02d}:{_win_start_m:02d}–{_win_end_h:02d}:{_win_end_m:02d} IST"
    if not (_in_window and _is_weekday):
        print(f"\n  ⏰  ENTRY WINDOW GATE: outside {_window_str} "
              f"(now {_now_gate.strftime('%H:%M')} IST"
              f"{'  [weekend]' if not _is_weekday else ''})")
        print(f"  No entry signals generated. Re-run between {_window_str} on a weekday.\n")
        return
    print(f"\n  ✓  Entry window: {_now_gate.strftime('%H:%M')} IST within {_window_str} — proceeding")

    # ── 2. ML models (monthly + weekly) ─────────────────────────────────────
    train_cutoff = int(len(data) * 0.6)
    train_data = data.iloc[:train_cutoff]
    
    # Monthly entry model — must use evolved cache (same source as backtest)
    cached_evolved = StrategyEvolver.load_from_cache()
    if not cached_evolved:
        print("\n  No evolved strategy cache found.")
        print("  signal-combined requires evolved strategies for consistent ML training")
        print("  (same data source used by backtest — ensures no divergence).")
        print("\n  Run this first, then re-run signal-combined:")
        print("    python main.py --mode evolve")
        return

    evolver = StrategyEvolver(train_data, lots=config.max_lots, lot_size=config.lot_size)
    evolver.evolved_strategies = cached_evolved
    sim_trades = evolver.generate_training_trades(
        entry_every_n_days=TRAINING_FLOW.monthly_entry_every_n_days
    )

    # Load saved entry model (trained once in --mode evolve)
    try:
        learner = RegimeAwareLearner.load(train_data)
        age = RegimeAwareLearner.cache_age_days()
        print(f"  Loaded entry model v4 from cache ({age}d old)")
        if age and age > 30:
            print(f"  ⚠ Entry model is {age} days old — consider re-running --mode evolve")
    except FileNotFoundError as e:
        print(f"\n  ERROR: {e}")
        return

    # Load weekly risk engine (replaces old WeeklyEntryLearner)
    from models.weekly_risk_engine import (
        WeeklyRiskEngine as _WRE, load_risk_engine as _load_re,
        risk_engine_cache_age_days as _re_age,
    )
    weekly_risk_engine = None
    try:
        _w_age = _re_age()
        if _w_age is not None and _w_age <= 30:
            weekly_risk_engine = _load_re(train_data)
            print(f"  Loaded weekly risk engine from cache ({_w_age}d old)  "
                  f"AUC={weekly_risk_engine.tail_scorer.training_stats.get('cv_auc', 0):.3f}")
        else:
            raise FileNotFoundError("stale or missing")
    except Exception:
        print(f"  No weekly risk engine cache — using rule-based weekly gate")

    # ── 3. Regime & ML assessment — via EntryDecisionEngine (identical to backtest) ──
    from backtester.entry_engine import EntryDecisionEngine
    regime_strat = RegimeAdaptiveStrategy(
        lots=config.max_lots,
        lot_size=config.lot_size,
        allow_bwb=not config.monthly_disable_bwb,
    )
    market_data_dict = latest.to_dict() if hasattr(latest, "to_dict") else dict(latest)

    print(f"\n  {'='*75}")
    print(f"  REGIME & ML ASSESSMENT")
    print(f"  {'='*75}")

    if learner.is_trained:
        entry_engine = EntryDecisionEngine(
            entry_model=learner,
            strategy=regime_strat,
            entry_threshold=config.monthly_entry_threshold,
            cooldown_days=0,     # single-point check — no cooldown in signal mode
            max_loss_streak=999, # no loss-streak throttle in signal mode
            loss_streak_bump=0.0,
        )
        entry_decision = entry_engine.check(latest, spot, vix, market_data_dict)
        eligible = entry_decision.eligible_strategies
        prediction = entry_decision.ml_prediction
        strat_rec = entry_decision.strat_rec
        win_prob = entry_decision.win_prob
        detected_regime = prediction.get("regime", "UNKNOWN")

        if entry_decision.skip_reason == "circuit_breaker":
            print(f"  CIRCUIT BREAKER ACTIVE — NO MONTHLY TRADE TODAY")
            cb_reasons = getattr(regime_strat, "_circuit_breaker_reasons", [])
            for r in cb_reasons:
                print(f"    X  {r}")
            monthly_signal = "BLOCKED"
            rec_strat = None
            optimal_params = {}
        elif not entry_decision.should_enter:
            monthly_signal = prediction.get("signal", "AVOID")
            if entry_decision.skip_reason == "pcs_floor_no_alternative":
                monthly_signal = "AVOID"
            rec_strat = None
            optimal_params = learner.predict_optimal_params(latest)
            print(f"  Detected Regime:  {detected_regime}")
            print(f"  Monthly Signal:   {monthly_signal}  "
                  f"(quality {prediction.get('quality_score', win_prob):.1%}  "
                  f"conf {prediction.get('confidence', 0):.1%})")
        else:
            monthly_signal = prediction.get("signal", "MODERATE_ENTRY")
            rec_strat = entry_decision.recommended_strategy
            optimal_params = learner.predict_optimal_params(latest)
            print(f"  Detected Regime:  {detected_regime}")
            print(f"  Monthly Signal:   {monthly_signal}  "
                  f"(quality {prediction.get('quality_score', win_prob):.1%}  "
                  f"conf {prediction.get('confidence', 0):.1%})")
            print(f"  Recommended Strategy: {strat_rec.get('display_name', rec_strat)}")
    else:
        eligible = regime_strat.get_eligible_strategies(spot, vix, market_data_dict)
        if not eligible:
            print(f"  CIRCUIT BREAKER ACTIVE — NO MONTHLY TRADE TODAY (rule-based)")
            monthly_signal = "BLOCKED"
            rec_strat = None
        else:
            monthly_signal = "RULE_BASED"
            rec_strat = eligible[0]
            print(f"  ML model not trained — using rule-based signal")
        prediction = {}
        optimal_params = {}
        strat_rec = {"strategy": rec_strat or "", "params": {}}
        win_prob = 0.60
        detected_regime = _vix_regime(vix)

    # ── 4. Monthly track — expiry selection (all expiries, not monthly-only) ─
    print(f"\n  {'='*75}")
    print(f"  ── TRACK 1 · MONTHLY ({_monthly_pct*100:.0f}% budget = ₹{config.initial_capital * _monthly_pct:,.0f}) ──")
    print(f"  {'='*75}")

    if monthly_signal in ("BLOCKED", "AVOID", "CAUTION_ENTRY") or not rec_strat:
        print(f"  Monthly track: SKIP  ({monthly_signal})")
        if prediction.get("macro_context"):
            for ctx in prediction["macro_context"]:
                print(f"    !  {ctx}")
        monthly_expiry = None
        monthly_dte = 0
    else:
        ml_params = strat_rec.get("params", {})
        decision = select_optimal_entry(
            spot=spot, vix=vix,
            strategy_name=rec_strat,
            strategies=regime_strat._strategies,
            entry_date=today,
            lots=config.max_lots,
            lot_size=config.lot_size,
            risk_free_rate=config.risk_free_rate,
            brokerage_per_lot=config.brokerage_per_lot,
        )

        # Show all candidates (monthly expiries) evaluated
        if decision.found:
            monthly_expiry = decision.expiry
            monthly_dte    = decision.dte
            print(f"  Strategy: {ML_STRATEGY_DISPLAY.get(rec_strat, rec_strat)}")
            print(f"  Evaluated {len(decision.all_candidates)} monthly expiry candidates:\n")
            print(f"  {'Expiry':<18} {'DTE':>4} {'Credit':>8} {'MaxLoss':>8} "
                  f"{'WinProb':>8} {'EV':>10} {'Score':>7}  Reasoning")
            print(f"  {'-'*90}")
            for i, c in enumerate(decision.all_candidates):
                marker = " <- SELECTED" if i == 0 else ""
                reasons_short = "; ".join(c.reasoning[:2]) if c.reasoning else ""
                print(f"  {str(c.expiry):<18} {c.dte:>4} {c.net_credit:>8.1f} {c.max_loss:>8.1f} "
                      f"{c.win_prob_estimate:>7.0%} {c.ev:>10.1f} {c.score:>7.2f}  "
                      f"{reasons_short}{marker}")

            print(f"\n  Selected: {format_expiry_label(monthly_expiry)} ({monthly_dte} DTE)")
        else:
            target_dte = ml_params.get("target_dte", optimal_params.get("target_dte", 21))
            monthly_expiry, monthly_dte = get_best_expiry_for_dte(today, target_dte)
            print(f"  No viable candidates — DTE fallback: {format_expiry_label(monthly_expiry)} "
                  f"({monthly_dte} DTE)")

        # Position sizing for monthly
        sizer = PositionSizer(config, max_lots_cap=config.max_lots * 2)
        _sig_regime = detected_regime if learner.is_trained else _vix_regime(vix)
        sizing = sizer.compute_lots(
            equity=config.initial_capital * _monthly_pct,
            vix=vix, regime=_sig_regime, win_prob=win_prob, drawdown_pct=0.0,
        )
        monthly_lots = sizing.lots
        print(f"  Position sizing ({_monthly_pct*100:.0f}% budget): {monthly_lots} lots  "
              f"(base {sizing.base_lots:.1f} × conf {sizing.confidence_scale:.1f}x "
              f"× regime {sizing.regime_scale}x)")

        # Fetch live chain and show suggested monthly trade
        _m_snapshot = None
        _m_live_spot = spot
        try:
            from data.broker import fyersModel as _fm
            from data.option_chain import OptionChainSnapshot, StrikeData
            from datetime import datetime as _dt3
            import os as _os3
            load_env()
            _now3 = now_ist()
            if is_market_hours(_now3):
                _fy3 = _fm.FyersModel(
                    client_id=_os3.getenv("FYERS_CLIENT_ID"),
                    token=_os3.getenv("FYERS_ACCESS_TOKEN"), log_path=""
                )
                _idx3 = _fy3.quotes(data={"symbols": "NSE:NIFTY50-INDEX"})
                if _idx3.get("s") == "ok":
                    _m_live_spot = _idx3["d"][0]["v"].get("lp", spot)

                _oc_meta3 = _fy3.optionchain(
                    data={"symbol": "NSE:NIFTY50-INDEX", "strikecount": 1, "timestamp": ""}
                )
                _ts3, _min3 = "", 999
                for _e3 in _oc_meta3.get("data", {}).get("expiryData", []):
                    _parts3 = _e3["date"].split("-")
                    _ed3 = _dt3.strptime(
                        f"{_parts3[2]}-{_parts3[1]}-{_parts3[0]}", "%Y-%m-%d"
                    ).date()
                    _gap3 = abs((_ed3 - monthly_expiry).days)
                    if _gap3 < _min3:
                        _min3, _ts3 = _gap3, _e3["expiry"]

                if _ts3:
                    _oc3 = _fy3.optionchain(
                        data={"symbol": "NSE:NIFTY50-INDEX", "strikecount": 40, "timestamp": _ts3}
                    )
                    _calls3, _puts3 = [], []
                    for _o3 in _oc3.get("data", {}).get("optionsChain", []):
                        _sp3 = _o3.get("strike_price", -1)
                        _ot3 = _o3.get("option_type", "")
                        if _sp3 <= 0 or not _ot3:
                            continue
                        _sd3 = StrikeData(
                            strike=int(_sp3), option_type=_ot3,
                            ltp=_o3.get("ltp", 0), bid=_o3.get("bid", 0),
                            ask=_o3.get("ask", 0), oi=_o3.get("oi", 0),
                            oi_change=0, volume=_o3.get("volume", 0),
                            iv=0.0, expiry=str(monthly_expiry),
                        )
                        (_calls3 if _ot3 == "CE" else _puts3).append(_sd3)

                    if _calls3 or _puts3:
                        _m_snapshot = OptionChainSnapshot(
                            timestamp=f"Fyers LIVE ({_now3.strftime('%H:%M:%S')})",
                            spot_price=_m_live_spot,
                            expiry_dates=[str(monthly_expiry)],
                            selected_expiry=monthly_expiry.strftime("%d-%b-%Y"),
                            calls=_calls3, puts=_puts3,
                            pcr_oi=0.0, pcr_volume=0.0, max_pain=0,
                            total_call_oi=sum(c.oi for c in _calls3),
                            total_put_oi=sum(p.oi for p in _puts3),
                            atm_strike=round(_m_live_spot / 50) * 50,
                        )
                        print(f"  Live chain: Fyers ({len(_calls3)} CE, {len(_puts3)} PE) | "
                              f"Spot: {_m_live_spot:,.2f}")
        except Exception as _fe3:
            print(f"  Fyers fetch failed ({_fe3}) — using BS estimates")

        live_expiry_matches = (
            _expiry_matches(_m_snapshot.selected_expiry, monthly_expiry)
            if _m_snapshot else False
        )
        print()
        _print_strategy_trade(
            rec_strat, ML_STRATEGY_DISPLAY.get(rec_strat, rec_strat),
            _m_snapshot, _m_live_spot, vix, monthly_dte, ml_params,
            monthly_lots, config.lot_size,
            ml_params.get("profit_target_pct", 50),
            ml_params.get("spread_width", 500),
            monthly_expiry, live_expiry_matches,
        ) if _m_snapshot else _print_strategy_bs_trade(
            rec_strat, ML_STRATEGY_DISPLAY.get(rec_strat, rec_strat),
            _m_live_spot, vix, monthly_dte, ml_params,
            monthly_lots, config.lot_size,
            ml_params.get("profit_target_pct", 50),
            ml_params.get("spread_width", 500),
            monthly_expiry,
        )

    # ── 5. Weekly track ─────────────────────────────────────────────────────
    print(f"\n  {'='*75}")
    print(f"  ── TRACK 2 · WEEKLY  ({_weekly_pct*100:.0f}% budget = ₹{config.initial_capital * _weekly_pct:,.0f}) ──")
    print(f"  {'='*75}")

    weekday_name = today.strftime("%A")
    weekly_lots = max(1, int(config.max_lots * _weekly_pct))

    # Replicate CombinedBacktestEngine._weekly_entry_blocked() rules
    weekly_eligible = True
    weekly_blocks: list[str] = []

    if today.weekday() not in (0, 1):  # only Mon or Tue
        weekly_eligible = False
        weekly_blocks.append(f"Entry window: Mon/Tue only — today is {weekday_name}")
    if vix < wc.min_vix_entry:
        weekly_eligible = False
        weekly_blocks.append(f"VIX {vix:.1f} too low (min {wc.min_vix_entry})")
    if vix > wc.max_vix_entry:
        weekly_eligible = False
        weekly_blocks.append(f"VIX {vix:.1f} too high (max {wc.max_vix_entry})")
    if vix > 22.0 and monthly_signal not in ("BLOCKED", "AVOID"):
        weekly_eligible = False
        weekly_blocks.append(f"VIX {vix:.1f} > 22 — simultaneous cap blocks weekly when monthly is live")

    w_exp, w_dte = get_weekly_expiry_in_range(today, wc.min_dte_entry, wc.max_dte_entry)
    if w_exp is None:
        weekly_eligible = False
        weekly_blocks.append(
            f"No weekly expiry fits entry window [{wc.min_dte_entry}–{wc.max_dte_entry}] DTE"
        )

    if not weekly_eligible:
        print(f"  Weekly track: SKIP  — {' | '.join(weekly_blocks)}")
    else:
        # Strategy selection: same rule as the backtest engines (weekly_exit_policy.select_weekly_strategy_name).
        # The 5-day Nifty return is the nifty_return_5d column that build_combined_dataset() produces
        # (market_data.py: nifty_close.pct_change(5)); recompute it from nifty_close only if missing.
        from backtester.weekly_exit_policy import WEEKLY_IC, select_weekly_strategy_name
        _ret_5d = latest.get("nifty_return_5d", None)
        if _ret_5d is None or pd.isna(_ret_5d):
            _closes = data["nifty_close"].dropna() if "nifty_close" in data.columns else None
            if _closes is not None and len(_closes) > 5:
                _ret_5d = float(_closes.iloc[-1] / _closes.iloc[-6] - 1.0)
        if _ret_5d is None or pd.isna(_ret_5d):
            print("  WARNING: 5-day Nifty return unavailable — weekly strategy defaults to PCS")
            _ret_5d = None
        _w_rule = select_weekly_strategy_name(vix, _ret_5d)
        if _w_rule == WEEKLY_IC:
            w_strategy_obj = WeeklyIronCondor(lots=weekly_lots, lot_size=config.lot_size)
            w_strategy_name = "Weekly Iron Condor"
        else:
            w_strategy_obj = WeeklyPutCreditSpread(lots=weekly_lots, lot_size=config.lot_size)
            w_strategy_name = "Weekly Put Credit Spread"

        w_action = w_strategy_obj.should_enter(spot, vix, latest.to_dict()
                                                if hasattr(latest, "to_dict") else dict(latest))

        if w_action.value != "ENTER":
            print(f"  Weekly track: SKIP  — strategy {w_strategy_name} declined entry "
                  f"(VIX={vix:.1f}, 5d={latest.get('nifty_return_5d', 0):.1%})")
        else:
            w_legs = w_strategy_obj.generate_legs(spot, vix, w_dte, config.risk_free_rate)

            # Risk engine assessment (regime + ETL + tail risk)
            risk_sizing = None
            if weekly_risk_engine is not None:
                from pricing.black_scholes import OptionType as _OT
                _short_k, _long_k = 0.0, 0.0
                _net_cr = sum(l.entry_premium if l.is_short else -l.entry_premium for l in w_legs)
                _opt_t = _OT.PUT
                for _wl in w_legs:
                    if _wl.is_short and _short_k == 0:
                        _short_k = _wl.strike
                        _opt_t = _OT.PUT if _wl.option_type in ("PUT", "PE") else _OT.CALL
                    elif not _wl.is_short and _long_k == 0:
                        _long_k = _wl.strike

                risk_sizing = weekly_risk_engine.compute_entry(
                    row=latest, spot=spot, vix=vix,
                    short_strike=_short_k, long_strike=_long_k,
                    entry_credit=_net_cr, dte=w_dte,
                    base_lots=weekly_lots, option_type=_opt_t,
                )
                print(f"\n  RISK ENGINE ASSESSMENT:")
                print(f"    Regime:     {risk_sizing.regime}")
                print(f"    Risk score: {risk_sizing.risk_score:.2f} → "
                      f"scale {risk_sizing.risk_scale:.0%}")
                print(f"    ETL:        {risk_sizing.etl:.1f} vs credit {risk_sizing.credit_collected:.1f}")
                if risk_sizing.skip:
                    print(f"    BLOCKED:    {risk_sizing.skip_reason}")
                    w_action = TradeAction.NO_TRADE
                else:
                    weekly_lots = risk_sizing.lots
                    print(f"    Lots:       {risk_sizing.lots} "
                          f"(regime {risk_sizing.regime_scale:.0%} × "
                          f"risk {risk_sizing.risk_scale:.0%})")

            if w_action.value == "ENTER":
                print(f"\n  Strategy:  {w_strategy_name}")
                print(f"  Entry day: {weekday_name} | Expiry: {format_expiry_label(w_exp)} "
                      f"({w_dte} DTE) | Lots: {weekly_lots}")

            # Summarise legs
            net_credit_w = sum(
                l.entry_premium if l.is_short else -l.entry_premium for l in w_legs
            )
            max_loss_w = 0.0
            short_legs_w = [l for l in w_legs if l.is_short]
            long_legs_w  = [l for l in w_legs if not l.is_short]
            for sl in short_legs_w:
                for ll in long_legs_w:
                    if sl.option_type == ll.option_type:
                        max_loss_w = max(max_loss_w, abs(sl.strike - ll.strike) - net_credit_w)

            print(f"\n  {'='*65}")
            print(f"  SUGGESTED WEEKLY TRADE: {w_strategy_name}")
            print(f"  Expiry: {format_expiry_label(w_exp)} ({w_dte} DTE) | "
                  f"BS estimated — verify on broker")
            print(f"  {'='*65}")
            print(f"  Spot: {spot:,.2f} | VIX: {vix:.1f}")
            print()
            for leg in w_legs:
                action_str = "SELL" if leg.is_short else "BUY "
                print(f"    {action_str} {leg.strike:>6.0f} {leg.option_type}  "
                      f"@ ₹{leg.entry_premium:>7.2f}  [BS est]")

            total_net = net_credit_w * weekly_lots * config.lot_size
            total_risk = max_loss_w * weekly_lots * config.lot_size
            print(f"\n  TRADE SUMMARY ({weekly_lots} lots × {config.lot_size} qty):")
            print(f"    Expiry:       {format_expiry_label(w_exp)} ({w_dte} DTE)")
            print(f"    Net Credit:   ₹{net_credit_w:.1f}/unit = ₹{total_net:,.0f} total")
            print(f"    Max Risk:     ₹{max_loss_w:.1f}/unit = ₹{total_risk:,.0f} total")
            if w_strategy_name == "Weekly Put Credit Spread":
                print(f"    Profit Target: {wc.engine_a_profit_target_pct:.0f}% = "
                      f"₹{total_net * wc.engine_a_profit_target_pct / 100:,.0f}")
                print(f"    Min Hold:     {wc.engine_a_min_hold_days} trading days")
                print(f"    Stop Loss:    {wc.stop_loss_pct:.0f}% of credit or breach of short strike")
                print(f"    Dynamic Exit: distance/strike risk breach only")
            else:
                print(f"    Profit Target: none")
                print(f"    Max Hold:     {wc.engine_b_max_hold_days} trading days")
                print(f"    Stop Loss:    {wc.stop_loss_pct:.0f}% of credit or breach of short strike")
                print(f"    Dynamic Exit: trailing delta, trend reversal, max hold")

    # ── 6. Combined risk dashboard ──────────────────────────────────────────
    print(f"\n  {'='*75}")
    print(f"  COMBINED RISK DASHBOARD  (mirrors CombinedBacktestEngine gates)")
    print(f"  {'='*75}")
    print(f"  Budget split:        Monthly {_monthly_pct*100:.0f}% = ₹{config.initial_capital * _monthly_pct:,.0f}  |  "
          f"Weekly {_weekly_pct*100:.0f}% = ₹{config.initial_capital * _weekly_pct:,.0f}")
    print(f"  Simultaneous VIX cap: 22.0  (current {vix:.1f} → "
          f"{'BOTH tracks allowed' if vix <= 22 else 'weekly blocked'})")
    print(f"  Cross-track DD limit: 15% of capital = ₹{config.initial_capital * 0.15:,.0f}")
    print(f"  Emergency weekly exit: combined open loss > 3% of equity")
    print(f"  Monthly track: {monthly_signal}")
    print(f"  Weekly track:  {'ELIGIBLE' if weekly_eligible else 'BLOCKED'}")
    if not weekly_eligible:
        for b in weekly_blocks:
            print(f"    X  {b}")

    print(f"\n  UPCOMING EXPIRIES (all weekly + monthly):")
    for exp_info in get_all_expiries(today, count=6):
        tag = "(M)" if exp_info["is_monthly"] else "(W)"
        print(f"    {tag}  {exp_info['label']:>15}  DTE: {exp_info['dte']:>3}")

    if learner.is_trained:
        learner.print_training_report()


def run_validate(config: BacktestConfig) -> None:
    """Run full production model validation: walk-forward + permutation tests for ALL models."""
    from models.model_validator import run_full_validation
    from models.strategy_evolver import StrategyEvolver
    from config import WeeklyBacktestConfig

    wc = WeeklyBacktestConfig()

    print("\n  Fetching market data...")
    fetcher = MarketDataFetcher(config.start_date, config.end_date)
    data = fetcher.build_combined_dataset()
    print(f"  Loaded {len(data)} trading days ({data.index[0].date()} to {data.index[-1].date()})")

    cached_evolved = StrategyEvolver.load_from_cache()
    if not cached_evolved:
        print("\n  No evolved strategies cached — running evolution first...")
        train_cutoff = int(len(data) * TRAINING_FLOW.layered_train_pct)
        train_data = data.iloc[:train_cutoff]
        se = StrategyEvolver(train_data, lots=config.max_lots, lot_size=config.lot_size)
        cached_evolved = se.evolve(
            target=TRAINING_FLOW.evolve_target,
            entry_every_n_days=TRAINING_FLOW.strategy_evolve_entry_every_n_days,
            train_pct=TRAINING_FLOW.layered_train_pct,
            min_trades=TRAINING_FLOW.layered_min_trades,
            verbose=False,
        )

    run_full_validation(
        data,
        evolved_strategies=cached_evolved,
        lots=config.max_lots,
        lot_size=config.lot_size,
        weekly_lots=wc.max_lots,
        weekly_lot_size=wc.lot_size,
        n_permutations=20,
        verbose=True,
    )


if __name__ == "__main__":
    main()
