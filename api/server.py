"""Market-data / model state and the signal, monitor and market-refresh endpoints.

Heavy loading (Yahoo-backed market data, pickled models) runs in a background
thread started by the app lifespan, so the server answers /api/health immediately
and the UI can show a "loading" state instead of the process failing to start.
"""

import logging
import os
import traceback
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from backtester.position_sizer import PositionSizer, _regime_from_vix
from config import BacktestConfig
from data.market_data import MarketDataFetcher
from models.regime_aware_learner import RegimeAwareLearner
from models.trade_monitor import ActiveTrade, ExitStrategyEngine
from strategies.multi_strategy import RegimeAdaptiveStrategy

from api.signal_wrapper import (
    wrap_entry_signal_for_track, wrap_exit_recommendation, wrap_full_signal_response,
    wrap_position_sizing, wrap_regime_aware_signal,
)

logger = logging.getLogger(__name__)

router = APIRouter()          # protected routes (mounted under /api with auth)
public_router = APIRouter()   # /api/health only

_state: Dict[str, Any] = {
    "market_df": None,
    "latest_row": None,
    "models": {},
    "market_fetcher": None,
    "status": "idle",         # idle | loading | ready | degraded
    "errors": [],
}


def load_market_data() -> None:
    fetcher = MarketDataFetcher(start_date=date(2009, 1, 1), end_date=date.today())
    _state["market_fetcher"] = fetcher
    _state["market_df"] = fetcher.build_combined_dataset()
    _state["latest_row"] = _state["market_df"].iloc[-1]


def load_models() -> None:
    df = _state["market_df"]
    _state["models"]["entry_model"] = RegimeAwareLearner.load(df)
    _state["models"]["exit_model"] = ExitStrategyEngine.load(df)


def load_all() -> None:
    """Blocking loader; run it in a worker thread. Failures degrade, never crash the app."""
    _state["status"] = "loading"
    _state["errors"] = []
    for name, fn in (("market data", load_market_data), ("models", load_models)):
        try:
            logger.info("Loading %s...", name)
            fn()
        except Exception as exc:  # noqa: BLE001
            logger.error("Loading %s failed: %s\n%s", name, exc, traceback.format_exc())
            _state["errors"].append(f"{name}: {exc}")
    _state["status"] = "ready" if not _state["errors"] else "degraded"
    logger.info("Startup load finished: %s", _state["status"])


def _require_data() -> None:
    if _state["latest_row"] is None:
        raise HTTPException(status_code=503, detail="Market data is still loading or failed to load; see /api/health")


# ============================================================================
# Signal Endpoint
# ============================================================================

@router.get("/signal")
async def get_entry_signal() -> Dict[str, Any]:
    """Get ML-driven entry signals for weekly and monthly tracks."""
    try:
        _require_data()

        latest_row = _state["latest_row"]
        spot = float(latest_row.get("nifty_close", 25000))
        vix = float(latest_row.get("vix", 20))
        regime = _infer_regime(vix)

        # Check entry window (11:00–13:00 IST)
        from zoneinfo import ZoneInfo
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        within_window = 11 <= now_ist.hour < 13

        # Get eligibile strategies
        entry_model = _state["models"].get("entry_model")
        if not entry_model:
            raise HTTPException(status_code=503, detail="Entry model not loaded; see /api/health")

        try:
            eligible_strats = RegimeAdaptiveStrategy.get_eligible_strategies(
                spot=spot,
                vix=vix,
                market_data_dict=latest_row.to_dict() if hasattr(latest_row, "to_dict") else dict(latest_row)
            )
        except:
            eligible_strats = ["put_credit_spread", "iron_condor"]  # Fallback

        # Get ML prediction for each track
        try:
            pred = entry_model.predict(latest_row, eligible_strats)
            quality_score = float(pred.get("quality_score", 0.0))
            signal_str = pred.get("signal", "AVOID")
            recommended_strategy = pred.get("strategy")
            macro_context = pred.get("macro_context", [])
            reasoning = pred.get("reasoning", [])
        except Exception as e:
            logger.warning(f"ML prediction error: {e}")
            quality_score = 0.0
            signal_str = "AVOID"
            recommended_strategy = None
            macro_context = ["ML model unavailable"]
            reasoning = [str(e)]

        # Get position sizing
        equity = float(os.getenv("PAPER_INITIAL_CAPITAL", "2500000"))
        try:
            position_sizer = PositionSizer(BacktestConfig())
            sizing = position_sizer.compute_lots(
                equity=equity,
                vix=vix,
                regime=_regime_from_vix(vix),
                win_prob=quality_score,
                drawdown_pct=0.0
            )
            sizing_dict = wrap_position_sizing(sizing, equity * 0.80, equity)
        except Exception as e:
            logger.warning(f"Position sizing error: {e}")
            sizing_dict = {
                "suggested_lots": 0,
                "available_capital": equity * 0.80,
                "capital_to_deploy": 0
            }

        # Wrap signals for each track
        weekly_pred = wrap_regime_aware_signal(
            {"quality_score": quality_score, "signal": signal_str, "strategy": recommended_strategy,
             "macro_context": macro_context, "reasoning": reasoning},
            spot, vix, regime, eligible_strats
        )
        weekly_signal = wrap_entry_signal_for_track(
            "weekly", weekly_pred, sizing_dict, equity * 0.80,
            0.0,  # weekly track is rule-based: no ML entry threshold
            dte_window="3–8"
        )

        # Monthly signal (currently disabled in Phase 1)
        monthly_pred = wrap_regime_aware_signal(
            {"quality_score": 0.0, "signal": "AVOID", "strategy": None,
             "macro_context": ["Monthly disabled in Phase 1"], "reasoning": []},
            spot, vix, regime, []
        )
        monthly_signal = wrap_entry_signal_for_track(
            "monthly", monthly_pred, {"suggested_lots": 0, "available_capital": 0, "capital_to_deploy": 0},
            0, BacktestConfig.monthly_entry_threshold,
            skip_reason="monthly_paused_phase1"
        )

        return wrap_full_signal_response(
            spot=spot,
            vix=vix,
            regime=regime,
            weekly_signal=weekly_signal,
            monthly_signal=monthly_signal,
            macro_context=macro_context,
            reasoning=reasoning,
            within_entry_window=within_window
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting signal: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Monitor Endpoint
# ============================================================================

def _ledger_trade_to_active(t: Dict[str, Any]) -> ActiveTrade:
    units = int(t["lots"]) * int(t["lot_size"])
    return ActiveTrade(
        trade_id=t["trade_id"],
        entry_date=t["entry_date"],
        expiry_date=t["expiry_date"],
        legs=[{"action": l["action"], "strike": float(l["strike"]), "option_type": l["option_type"],
               "qty": units, "entry_price": float(l["entry_price"])} for l in t["legs"]],
        notes=t.get("notes", ""),
        strategy_code=t.get("strategy"),
        entry_vix=t.get("entry_vix"),
        entry_spot=t.get("entry_spot"),
    )


@router.get("/monitor")
async def monitor_open_trades(request: Request, journal_id: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Exit recommendations (ML + rules) for open ledger trades."""
    _require_data()
    exit_model = _state["models"].get("exit_model")
    if not exit_model:
        raise HTTPException(status_code=503, detail="Exit model not loaded; see /api/health")
    open_trades = request.app.state.ledger.list_trades(journal_id, "open")
    recommendations: List[Dict[str, Any]] = []
    for t in open_trades:
        try:
            rec = exit_model.analyze_trade(_ledger_trade_to_active(t), _state["latest_row"])
            item = wrap_exit_recommendation(rec, t["trade_id"])
            item["journal_id"] = t["journal_id"]
            recommendations.append(item)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not analyze trade %s: %s", t["trade_id"], exc)
            recommendations.append({"trade_id": t["trade_id"], "journal_id": t["journal_id"],
                                    "action": "UNAVAILABLE", "reasoning": [str(exc)]})
    return {"count": len(recommendations), "timestamp": datetime.now().isoformat(timespec="seconds"),
            "recommendations": recommendations}


# ============================================================================
# Market Refresh Endpoint
# ============================================================================

@router.post("/market/refresh")
async def refresh_market_data() -> Dict[str, Any]:
    """Force reload of market data cache."""
    try:
        logger.info("Refreshing market data...")
        market_fetcher = _state.get("market_fetcher")
        if not market_fetcher:
            market_fetcher = MarketDataFetcher(start_date=date(2009, 1, 1), end_date=date.today())
            _state["market_fetcher"] = market_fetcher

        _state["market_df"] = market_fetcher.build_combined_dataset()
        _state["latest_row"] = _state["market_df"].iloc[-1]

        latest_date = getattr(_state["latest_row"], "name", date.today())
        num_rows = len(_state["market_df"])

        logger.info(f"✓ Market data refreshed: {num_rows} rows, latest date {latest_date}")

        return {
            "status": "refreshed",
            "latest_date": str(latest_date),
            "rows": num_rows
        }

    except Exception as e:
        logger.error(f"Error refreshing market data: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Health Check Endpoint
# ============================================================================

# ============================================================================
# Health (public)
# ============================================================================

@public_router.get("/health")
async def health_check() -> Dict[str, Any]:
    return {
        "status": "ok",
        "state": _state["status"],
        "market_data_loaded": _state["latest_row"] is not None,
        "models_loaded": len(_state["models"]) == 2,
        "errors": _state["errors"],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


# ============================================================================
# Helpers
# ============================================================================

def _infer_regime(vix: float) -> str:
    """Infer market regime from VIX level."""
    if vix < 14:
        return "VERY_LOW_VOL"
    elif vix < 18:
        return "LOW_VOL"
    elif vix < 22:
        return "NORMAL"
    elif vix < 28:
        return "ELEVATED"
    elif vix < 35:
        return "HIGH"
    else:
        return "CRISIS"

