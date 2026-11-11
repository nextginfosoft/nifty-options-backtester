"""
Active Trade Monitor & Exit Strategy Engine.

Supports ANY multi-leg options strategy:
  - Single spreads (put credit spread, bear call spread)
  - Iron Condors (4 legs)
  - Butterflies, Straddles, Strangles, Ratios, etc.

Input format — each leg:
  --leg "SELL 21500 PE 130 @ 361.95"
  --leg "BUY 20500 PE 130 @ 167.65"

Recommendations:
  HOLD, BOOK_PROFIT, EXIT_NOW, TRAIL_STOP, PARTIAL_EXIT
"""

import json
import math
import re
import warnings
from dataclasses import dataclass, asdict, field
from datetime import date, datetime
import datetime as dt_module
import logging
from pathlib import Path
from typing import Optional

import joblib

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent / "data" / ".cache"
EXIT_MODEL_CACHE_PATH = _CACHE_DIR / "exit_model.pkl"
TRADES_FILE = _CACHE_DIR / "active_trades.json"
CLOSED_TRADES_FILE = _CACHE_DIR / "closed_trades.json"

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier

from models.cache_utils import ModelPeriodMetadata, metadata_from_state
from pricing.black_scholes import price_option, iv_from_vix, OptionType
from models.trade_learner import FeatureExtractor, TradeLearner


@dataclass
class TradeLeg:
    """A single leg of an options trade."""
    action: str          # "BUY" or "SELL"
    strike: float
    option_type: str     # "PE" or "CE"
    qty: int
    entry_price: float
    current_ltp: Optional[float] = None

    @property
    def is_short(self) -> bool:
        return self.action == "SELL"

    @property
    def signed_qty(self) -> int:
        return -self.qty if self.is_short else self.qty

    def entry_value(self) -> float:
        """Negative for sells (credit), positive for buys (debit)."""
        return self.entry_price * self.signed_qty

    def current_value(self, ltp: float) -> float:
        return ltp * self.signed_qty

    def leg_pnl(self, ltp: float) -> float:
        return (self.entry_price - ltp) * self.qty if self.is_short else (ltp - self.entry_price) * self.qty


LEG_PATTERN = re.compile(
    r"(BUY|SELL)\s+(\d+(?:\.\d+)?)\s+(PE|CE)\s+(\d+)\s*@\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def parse_leg(leg_str: str) -> TradeLeg:
    """
    Parse: "SELL 21500 PE 130 @ 361.95"
    """
    m = LEG_PATTERN.match(leg_str.strip())
    if not m:
        raise ValueError(
            f"Invalid leg format: '{leg_str}'\n"
            f"Expected: 'SELL 21500 PE 130 @ 361.95' or 'BUY 20500 CE 75 @ 85.5'"
        )
    return TradeLeg(
        action=m.group(1).upper(),
        strike=float(m.group(2)),
        option_type=m.group(3).upper(),
        qty=int(m.group(4)),
        entry_price=float(m.group(5)),
    )


@dataclass
class ActiveTrade:
    """A live trade with one or more legs."""
    trade_id: str
    entry_date: str
    expiry_date: str
    legs: list[dict]
    current_pnl: Optional[float] = None
    notes: str = ""
    strategy_code: Optional[str] = None

    # Entry-time market context, persisted so exit checks compare against the
    # VIX/spot at entry rather than today's values. None for trades added before
    # these fields existed (monitor falls back to current values with a warning).
    entry_vix: Optional[float] = None
    entry_spot: Optional[float] = None

    # Persisted peak P&L for 85% max profit booking (survives across monitor runs)
    peak_pnl_per_unit: float = 0.0

    # Legacy fields for backward compat with old JSON
    direction: Optional[str] = None
    short_strike: Optional[float] = None
    long_strike: Optional[float] = None
    entry_credit: Optional[float] = None
    lots: Optional[int] = None
    lot_size: Optional[int] = None

    def get_legs(self) -> list[TradeLeg]:
        """Return legs, converting from legacy format if needed."""
        if self.legs:
            return [TradeLeg(**lg) for lg in self.legs]
        if self.short_strike is not None and self.long_strike is not None:
            ot = "PE" if self.direction == "put" else "CE"
            qty = (self.lots or 1) * (self.lot_size or 75)
            sell_price = (self.entry_credit or 0)
            return [
                TradeLeg("SELL", self.short_strike, ot, qty, sell_price),
                TradeLeg("BUY", self.long_strike, ot, qty, 0),
            ]
        return []

    @property
    def strategy_name(self) -> str:
        legs = self.get_legs()
        sells = [l for l in legs if l.is_short]
        buys = [l for l in legs if not l.is_short]
        put_sells = [l for l in sells if l.option_type == "PE"]
        call_sells = [l for l in sells if l.option_type == "CE"]
        put_buys = [l for l in buys if l.option_type == "PE"]
        call_buys = [l for l in buys if l.option_type == "CE"]

        if len(legs) == 4 and put_sells and call_sells and put_buys and call_buys:
            return "Iron Condor"
        if len(legs) == 2 and len(put_sells) == 1 and len(put_buys) == 1:
            return "Put Credit Spread"
        if len(legs) == 2 and len(call_sells) == 1 and len(call_buys) == 1:
            return "Bear Call Spread"
        if len(legs) == 3:
            return "Butterfly"
        if len(legs) == 2 and len(sells) == 2:
            return "Short Strangle/Straddle"
        return f"{len(legs)}-Leg Strategy"

    @property
    def total_qty(self) -> int:
        legs = self.get_legs()
        if legs:
            return max(l.qty for l in legs)
        return (self.lots or 1) * (self.lot_size or 75)

    @property
    def net_entry_credit(self) -> float:
        """Positive = net credit received, negative = net debit paid."""
        legs = self.get_legs()
        if not legs:
            return self.entry_credit or 0
        credit = sum(l.entry_price * l.qty for l in legs if l.is_short)
        debit = sum(l.entry_price * l.qty for l in legs if not l.is_short)
        return credit - debit

    @property
    def net_entry_credit_per_unit(self) -> float:
        return self.net_entry_credit / self.total_qty if self.total_qty > 0 else 0

    @property
    def max_profit_total(self) -> float:
        legs = self.get_legs()
        if not legs:
            return (self.entry_credit or 0) * (self.lots or 1) * (self.lot_size or 75)
        return self.net_entry_credit

    @property
    def max_loss_total(self) -> float:
        legs = self.get_legs()
        if not legs:
            ml = (abs(self.short_strike - self.long_strike) - self.entry_credit) if self.short_strike and self.long_strike else 0
            return ml * (self.lots or 1) * (self.lot_size or 75)
        spreads = self._identify_spreads()
        if spreads:
            return sum(s["max_loss"] for s in spreads)
        return self.net_entry_credit * 10

    @property
    def dte(self) -> int:
        exp = date.fromisoformat(self.expiry_date)
        return (exp - date.today()).days

    @property
    def days_in_trade(self) -> int:
        entry = date.fromisoformat(self.entry_date)
        return (date.today() - entry).days

    def _identify_spreads(self) -> list[dict]:
        """Group legs into spreads for max-loss calculation."""
        legs = self.get_legs()
        spreads = []
        for ot in ("PE", "CE"):
            sells = sorted([l for l in legs if l.is_short and l.option_type == ot],
                           key=lambda l: l.strike)
            buys = sorted([l for l in legs if not l.is_short and l.option_type == ot],
                          key=lambda l: l.strike)
            for s in sells:
                paired_buy = None
                for b in buys:
                    if b.qty > 0:
                        paired_buy = b
                        break
                if paired_buy:
                    width = abs(s.strike - paired_buy.strike)
                    qty = min(s.qty, paired_buy.qty)
                    credit_per = s.entry_price - paired_buy.entry_price
                    max_loss = (width - max(credit_per, 0)) * qty
                    spreads.append({
                        "type": ot,
                        "sell_strike": s.strike,
                        "buy_strike": paired_buy.strike,
                        "width": width,
                        "credit_per": credit_per,
                        "max_loss": max_loss,
                        "qty": qty,
                    })
                    paired_buy.qty -= qty
        return spreads

    def short_strikes(self) -> list[float]:
        return [l.strike for l in self.get_legs() if l.is_short]

    def compute_bs_pnl(self, spot: float, vix: float) -> float:
        """Compute current P&L using Black-Scholes."""
        legs = self.get_legs()
        dte = max(self.dte, 1)
        total_pnl = 0.0
        for leg in legs:
            ot = OptionType.PUT if leg.option_type == "PE" else OptionType.CALL
            iv = iv_from_vix(vix, leg.strike, spot, ot)
            current_premium = price_option(spot, leg.strike, dte, iv, 0.065, ot).premium
            total_pnl += leg.leg_pnl(current_premium)
        return total_pnl


@dataclass
class ExitRecommendation:
    trade_id: str
    action: str           # HOLD, BOOK_PROFIT, EXIT_NOW, TRAIL_STOP, PARTIAL_EXIT
    confidence: float
    current_pnl_pct: float
    pnl_rupees: float
    days_in_trade: int
    dte_remaining: int
    reasoning: list[str]
    risk_score: float     # 0 (safe) to 1 (danger)
    theta_per_day: float
    per_leg_pnl: Optional[list[dict]] = None
    partial_exit_legs: Optional[list[str]] = None


@dataclass
class ClosedTradeRecord:
    """Archived realized trade used for actual-trade training inputs."""
    trade_id: str
    entry_date: str
    exit_date: str
    expiry_date: str
    strategy: str
    total_pnl: float
    pnl_pct: float
    max_profit_total: float
    max_loss_total: float
    source: str = "actual"
    exit_reason: str = "manual_close"
    notes: str = ""
    legs: list[dict] = field(default_factory=list)


def _infer_strategy_code(trade: ActiveTrade) -> Optional[str]:
    """Best-effort mapping from live trade naming to learner strategy labels."""
    if trade.strategy_code:
        return trade.strategy_code

    strategy_name = trade.strategy_name.lower()
    if "iron condor" in strategy_name:
        return "iron_condor"
    if "put credit spread" in strategy_name:
        return "put_credit_spread"
    return None


def _is_supported_training_strategy(strategy_code: Optional[str]) -> bool:
    return strategy_code in TradeLearner.STRATEGY_LABELS


def load_closed_trades() -> list[ClosedTradeRecord]:
    if not CLOSED_TRADES_FILE.exists():
        return []
    with open(CLOSED_TRADES_FILE) as f:
        data = json.load(f)
    return [ClosedTradeRecord(**t) for t in data.get("trades", [])]


def save_closed_trades(trades: list[ClosedTradeRecord]):
    CLOSED_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "updated_at": str(datetime.now()),
        "trades": [asdict(t) for t in trades],
    }
    with open(CLOSED_TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_actual_training_trades() -> tuple[list[ClosedTradeRecord], int]:
    closed_trades = load_closed_trades()
    supported = [t for t in closed_trades if _is_supported_training_strategy(t.strategy)]
    skipped = len(closed_trades) - len(supported)
    return supported, skipped


class ExitStrategyEngine:
    """ML-driven exit decision engine for any multi-leg options trade."""

    def __init__(self, data: pd.DataFrame):
        self.data = data
        self.feature_extractor = FeatureExtractor(data)
        self.exit_classifier = None
        self.is_trained = False
        self.period_metadata: ModelPeriodMetadata | None = None

    def train_from_simulations(self, evolved_strategies: dict, verbose: bool = True):
        """
        Train exit model using CAUSAL labels (no future information leakage).

        Two labeling signals, both avoiding full-path future peek:

        1. Observable danger signals (zero future info):
           - Strike proximity: spot within 2% of short strike while losing
           - VIX spike: VIX jumped >25% since entry
           - Hard stop: P&L below -60% of credit
           - Theta erosion stall: >60% of hold time elapsed, <15% credit captured

        2. Fixed 3-day forward return (bounded, standard in quant finance):
           - If holding 3 more days loses >10% of entry credit → EXIT
           - This is a fixed, short horizon — NOT variable-length lookahead

        The model learns: "Given current observable state, will the next 3 days
        deteriorate enough to justify exiting?" — a question it CAN answer
        in production using learned market patterns.
        """
        snapshots = []
        n_rows = len(self.data)
        forward_horizon = 3

        for regime, strat in evolved_strategies.items():
            direction = strat.direction
            sd = strat.sd
            spread_width = strat.spread_width
            hold_days = strat.hold_days
            opt_type = OptionType.PUT if direction == "put" else OptionType.CALL

            for entry_idx in range(50, n_rows, 5):
                if entry_idx + hold_days >= n_rows:
                    continue

                row = self.data.iloc[entry_idx]
                spot = row.get("nifty_close", 0)
                vix = row.get("vix", 15)
                if pd.isna(spot) or spot == 0 or pd.isna(vix):
                    continue
                if vix < strat.min_vix or vix >= strat.max_vix:
                    continue

                annual_vol = vix / 100.0
                period_vol = annual_vol / (252**0.5) * (hold_days**0.5)

                if direction == "put":
                    short_strike = round((spot - spot * period_vol * sd) / 50) * 50
                    long_strike = short_strike - spread_width
                else:
                    short_strike = round((spot + spot * period_vol * sd) / 50) * 50
                    long_strike = short_strike + spread_width

                s_iv = iv_from_vix(vix, short_strike, spot, opt_type)
                l_iv = iv_from_vix(vix, long_strike, spot, opt_type)
                s_prem = price_option(spot, short_strike, hold_days, s_iv, 0.065, opt_type).premium
                l_prem = price_option(spot, long_strike, hold_days, l_iv, 0.065, opt_type).premium
                entry_credit = s_prem - l_prem
                if entry_credit <= 0:
                    continue

                max_loss_unit = spread_width - entry_credit

                # Phase 1: compute FULL P&L path (needed for feature extraction)
                full_pnls = [0.0]
                for d in range(1, hold_days + 1):
                    idx2 = entry_idx + d
                    if idx2 >= n_rows:
                        full_pnls.append(full_pnls[-1])
                        continue
                    dr = self.data.iloc[idx2]
                    ds = dr.get("nifty_close", spot)
                    dv = dr.get("vix", vix)
                    dte_r = max(hold_days - d, 1)
                    if pd.isna(ds) or pd.isna(dv):
                        full_pnls.append(full_pnls[-1])
                        continue
                    si2 = iv_from_vix(dv, short_strike, ds, opt_type)
                    li2 = iv_from_vix(dv, long_strike, ds, opt_type)
                    sn = price_option(ds, short_strike, dte_r, si2, 0.065, opt_type).premium
                    ln = price_option(ds, long_strike, dte_r, li2, 0.065, opt_type).premium
                    full_pnls.append(entry_credit - (sn - ln))

                if len(full_pnls) < forward_horizon + 2:
                    continue

                # Phase 2: CAUSAL labeling — no full-path future peek
                peak_pnl = 0.0
                for d in range(1, len(full_pnls) - forward_horizon):
                    idx2 = entry_idx + d
                    if idx2 >= n_rows:
                        break

                    current_pnl = full_pnls[d]
                    if current_pnl > peak_pnl:
                        peak_pnl = current_pnl
                    pnl_from_peak = current_pnl - peak_pnl

                    pnl_pct = (current_pnl / entry_credit * 100) if entry_credit > 0 else 0

                    dr = self.data.iloc[idx2]
                    ds = dr.get("nifty_close", spot)
                    dv = dr.get("vix", vix)

                    dte_remaining = hold_days - d
                    if direction == "put":
                        dist_short = (ds - short_strike) / ds * 100
                    else:
                        dist_short = (short_strike - ds) / ds * 100

                    vix_change = (dv - vix) / vix if vix > 0 else 0

                    # --- CAUSAL LABEL: observable danger + bounded 3-day forward ---
                    should_exit = 0

                    vix_vs_sma_val = dr.get("vix_vs_sma_ratio", 1.0) if hasattr(dr, "get") else 1.0
                    if pd.isna(vix_vs_sma_val):
                        vix_vs_sma_val = 1.0

                    # Signal A: Observable danger (zero future info)
                    if dist_short < 2.0 and pnl_pct < 0:
                        should_exit = 1
                    elif vix_change > 0.25 and pnl_pct < -10:
                        should_exit = 1
                    elif vix_vs_sma_val > 1.20 and pnl_pct < -15:
                        should_exit = 1
                    elif pnl_pct < -60:
                        should_exit = 1
                    elif d > hold_days * 0.6 and pnl_pct < 15 and pnl_pct > -30:
                        should_exit = 1

                    # Signal B: Fixed 3-day forward return (bounded horizon)
                    if should_exit == 0:
                        fwd_idx = min(d + forward_horizon, len(full_pnls) - 1)
                        pnl_3d_later = full_pnls[fwd_idx]
                        forward_return = pnl_3d_later - current_pnl
                        if forward_return < -entry_credit * 0.10:
                            should_exit = 1

                    # --- Feature extraction (all observable at time d) ---
                    market_features = self.feature_extractor.extract(dr)
                    if market_features is None:
                        continue

                    pnl_3d_ago = full_pnls[max(d - 3, 0)]
                    pnl_velocity_3d = (current_pnl - pnl_3d_ago) / 3 if d >= 3 else current_pnl / max(d, 1)

                    vix_vs_sma = dr.get("vix_vs_sma_ratio", 1.0) if hasattr(dr, "get") else 1.0
                    vix_1d_chg = dr.get("vix_change_1d", 0) if hasattr(dr, "get") else 0

                    trade_features = {
                        "pnl_pct": pnl_pct,
                        "dte_remaining": dte_remaining,
                        "days_in_trade": d,
                        "dist_to_short_strike_pct": dist_short,
                        "vix_now": dv,
                        "vix_change_since_entry": vix_change,
                        "vix_vs_sma_ratio_now": vix_vs_sma if not pd.isna(vix_vs_sma) else 1.0,
                        "vix_1d_change_now": vix_1d_chg if not pd.isna(vix_1d_chg) else 0.0,
                        "spot_change_since_entry": (ds - spot) / spot * 100,
                        "pnl_velocity": current_pnl / d if d > 0 else 0,
                        "credit_captured_pct": pnl_pct,
                        "max_loss_proximity": abs(current_pnl) / max_loss_unit if current_pnl < 0 and max_loss_unit > 0 else 0,
                        "theta_estimate": entry_credit / hold_days,
                        "pnl_from_peak": pnl_from_peak,
                        "pnl_velocity_3d": pnl_velocity_3d,
                        "days_since_peak": d - full_pnls[:d + 1].index(peak_pnl) if peak_pnl > 0 else 0,
                    }

                    snapshot = {**market_features, **trade_features}
                    snapshot["_label_should_exit"] = should_exit
                    snapshots.append(snapshot)

        if verbose:
            print(f"  Generated {len(snapshots)} exit training snapshots")

        if len(snapshots) < 50:
            if verbose:
                print("  Too few snapshots for training")
            return

        df = pd.DataFrame(snapshots)
        feature_cols = [c for c in df.columns if not c.startswith("_label_")]

        X = df[feature_cols].fillna(0).replace([np.inf, -np.inf], 0)
        y_exit = df["_label_should_exit"].values

        self.exit_feature_cols = feature_cols

        from sklearn.utils.class_weight import compute_sample_weight
        sample_weights = compute_sample_weight("balanced", y_exit)

        self.exit_classifier = LGBMClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.08,
            num_leaves=20,  # < 2^5=32; prevents leaf-wise overfit on exit snapshots
            subsample=0.8, colsample_bytree=0.8, min_child_samples=8,
            random_state=42, n_jobs=-1, verbosity=-1,
        )
        self.exit_classifier.fit(X, y_exit, sample_weight=sample_weights)

        self.exit_feature_importance = dict(
            sorted(
                zip(feature_cols, self.exit_classifier.feature_importances_),
                key=lambda x: x[1], reverse=True,
            )
        )

        if verbose:
            exits = int(y_exit.sum())
            total = len(y_exit)
            print(f"  Labels: {exits}/{total} exit signals ({exits/total*100:.0f}% exit, {100-exits/total*100:.0f}% hold)")
            from sklearn.model_selection import TimeSeriesSplit
            tscv = TimeSeriesSplit(n_splits=5)
            from sklearn.model_selection import cross_val_score
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                cv = cross_val_score(self.exit_classifier, X, y_exit, cv=tscv,
                                     scoring="f1", error_score=0.0)
            print(f"  Exit model TimeSeriesCV F1: {cv.mean():.1%} ± {cv.std():.1%}")
            top5 = list(self.exit_feature_importance.items())[:5]
            print(f"  Top exit features: {', '.join(f'{k} ({v:.1%})' for k,v in top5)}")

        self.is_trained = True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(
        self,
        path: Path | str = EXIT_MODEL_CACHE_PATH,
        *,
        period_metadata: ModelPeriodMetadata | None = None,
    ) -> None:
        """Persist the fitted exit classifier to disk (no DataFrames)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.period_metadata = period_metadata
        state = {
            "_saved_at": dt_module.datetime.now().isoformat(),
            "exit_classifier": self.exit_classifier,
            "exit_feature_importance": getattr(self, "exit_feature_importance", {}),
            "exit_feature_cols": list(getattr(self, "exit_feature_cols", []) or []),
            "is_trained": self.is_trained,
            "period_metadata": period_metadata.__dict__ if period_metadata else None,
        }
        joblib.dump(state, path)
        logger.info("Exit model saved to %s", path)

    @classmethod
    def load(cls, market_data: pd.DataFrame,
             path: Path | str = EXIT_MODEL_CACHE_PATH,
             *,
             expected_metadata: ModelPeriodMetadata | None = None,
             signal_date: date | None = None,
             require_period_metadata: bool = False) -> "ExitStrategyEngine":
        """
        Load a previously saved exit model.  Raises FileNotFoundError if missing.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Exit model cache not found at {path}.\n"
                "Run  python main.py --mode evolve  to train and save models first."
            )
        state = joblib.load(path)
        period_metadata = metadata_from_state(state)
        if require_period_metadata and period_metadata is None:
            raise ValueError(f"Exit model cache at {path} is missing period metadata.")
        if expected_metadata is not None:
            if period_metadata is None:
                raise ValueError(f"Exit model cache at {path} cannot be verified against expected metadata.")
            period_metadata.assert_matches(expected_metadata)
        if signal_date is not None:
            if period_metadata is None:
                raise ValueError(f"Exit model cache at {path} cannot be proven out-of-sample for {signal_date}.")
            period_metadata.assert_usable_for(signal_date)
        engine = cls(market_data)
        engine.exit_classifier = state["exit_classifier"]
        engine.exit_feature_importance = state.get("exit_feature_importance", {})
        engine.is_trained = state["is_trained"]
        # Older caches did not store the feature columns; LightGBM keeps them on the fitted model.
        engine.exit_feature_cols = list(
            state.get("exit_feature_cols")
            or getattr(engine.exit_classifier, "feature_name_", None)
            or []
        )
        engine.period_metadata = period_metadata
        return engine

    @staticmethod
    def cache_age_days(path: Path | str = EXIT_MODEL_CACHE_PATH) -> int | None:
        path = Path(path)
        if not path.exists():
            return None
        try:
            state = joblib.load(path)
            saved_at = dt_module.datetime.fromisoformat(state["_saved_at"])
            return (dt_module.datetime.now() - saved_at).days
        except Exception:
            return None

    def analyze_trade(
        self, trade: ActiveTrade, latest_market: pd.Series,
        live_snapshot=None,
        fyers_ltp: Optional[dict] = None,
        fyers_oi_vol: Optional[dict] = None,
        live_spot_override: float = 0.0,
    ) -> ExitRecommendation:
        """Analyze any multi-leg trade and recommend action.

        Args:
            live_snapshot: Optional OptionChainSnapshot (NSE/Groww fallback path).
            fyers_ltp: Dict of {(strike_int, opt_type) -> ltp} fetched directly
                       from Fyers for the exact trade legs. When present, this
                       takes precedence over snapshot-based lookup — no expiry
                       string matching needed.
            fyers_oi_vol: Dict of {(strike_int, opt_type) -> {"oi": ..., "volume": ..., "bid": ..., "ask": ...}}
                          for displaying market depth and activity.
            live_spot_override: Live Nifty spot from Fyers; overrides stale Yahoo close.
        """
        market_features = self.feature_extractor.extract(latest_market)
        if market_features is None:
            market_features = {f: 0 for f in FeatureExtractor.FEATURE_NAMES}

        spot = latest_market.get("nifty_close", 0)
        vix = latest_market.get("vix", 15)
        legs = trade.get_legs()
        dte = max(trade.dte, 1)

        # Prefer Fyers live spot > snapshot spot > stale Yahoo close
        live_spot = (live_spot_override if live_spot_override > 0
                     else (live_snapshot.spot_price
                           if live_snapshot and live_snapshot.spot_price > 0
                           else spot))

        # Build LTP lookup — fyers_ltp (direct) wins over snapshot-derived
        _live_ltp: dict[tuple, float] = {}
        has_fyers_direct = bool(fyers_ltp)

        if fyers_ltp:
            # Already keyed as (strike_int, opt_type) — use directly
            _live_ltp.update(fyers_ltp)
        elif live_snapshot:
            # Fallback: derive from snapshot, but requires expiry to match
            try:
                trade_exp = datetime.strptime(trade.expiry_date, "%Y-%m-%d").date()
                snap_exp_str = live_snapshot.selected_expiry
                for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
                    try:
                        snap_exp = datetime.strptime(snap_exp_str, fmt).date()
                        break
                    except ValueError:
                        continue
                else:
                    snap_exp = None
                if snap_exp and abs((trade_exp - snap_exp).days) <= 3:
                    for sd in (live_snapshot.calls or []):
                        _live_ltp[(int(sd.strike), "CE")] = sd.ltp
                    for sd in (live_snapshot.puts or []):
                        _live_ltp[(int(sd.strike), "PE")] = sd.ltp
            except Exception:
                pass

        # Per-leg P&L — use live LTP when available, else Black-Scholes
        per_leg_pnl = []
        total_pnl = 0.0
        for leg in legs:
            ot = OptionType.PUT if leg.option_type == "PE" else OptionType.CALL
            # Normalise to int key for lookup
            leg_key = (int(leg.strike), leg.option_type)
            live_price = _live_ltp.get(leg_key)
            if live_price is not None and live_price > 0:
                ltp = live_price
                price_src = "LIVE"
            else:
                iv = iv_from_vix(vix, leg.strike, live_spot, ot)
                ltp = price_option(live_spot, leg.strike, dte, iv, 0.065, ot).premium
                price_src = "BS*" if (has_fyers_direct or live_snapshot) else "BS"
            lpnl = leg.leg_pnl(ltp)
            total_pnl += lpnl
            
            # Get OI and volume data if available from Fyers
            oi_vol_data = fyers_oi_vol.get(leg_key) if fyers_oi_vol else None
            leg_info = {
                "leg": f"{'SELL' if leg.is_short else 'BUY'} {leg.strike:.0f} {leg.option_type}",
                "entry": leg.entry_price,
                "current_bs": round(ltp, 2),
                "pnl": round(lpnl, 0),
                "qty": leg.qty,
                "source": price_src,
            }
            
            # Add OI/volume/bid-ask if available
            if oi_vol_data:
                leg_info.update({
                    "oi": oi_vol_data.get("oi", 0),
                    "volume": oi_vol_data.get("volume", 0),
                    "bid": oi_vol_data.get("bid", 0),
                    "ask": oi_vol_data.get("ask", 0),
                })
            
            per_leg_pnl.append(leg_info)

        if trade.current_pnl is not None:
            total_pnl = trade.current_pnl

        max_profit = trade.max_profit_total
        pnl_pct = (total_pnl / max_profit * 100) if max_profit > 0 else 0

        # Closest short strike to spot (most at-risk leg)
        short_legs = [l for l in legs if l.is_short]
        min_dist_short = 999
        closest_short = None
        for sl in short_legs:
            if sl.option_type == "PE":
                dist = (live_spot - sl.strike) / live_spot * 100
            else:
                dist = (sl.strike - live_spot) / live_spot * 100
            if dist < min_dist_short:
                min_dist_short = dist
                closest_short = sl

        # For Iron Condors, check which side is at risk
        put_shorts = [l for l in short_legs if l.option_type == "PE"]
        call_shorts = [l for l in short_legs if l.option_type == "CE"]
        partial_exit_side = None
        if put_shorts and call_shorts:
            put_dist = min((spot - l.strike) / spot * 100 for l in put_shorts)
            call_dist = min((l.strike - spot) / spot * 100 for l in call_shorts)
            if put_dist < 2 and call_dist > 5:
                partial_exit_side = "put"
            elif call_dist < 2 and put_dist > 5:
                partial_exit_side = "call"

        net_credit = trade.net_entry_credit
        theta_per_day = net_credit / max(dte + trade.days_in_trade, 1)

        vix_vs_sma = market_features.get("vix_vs_sma_ratio", 1.0)
        vix_1d_chg = market_features.get("vix_1d_change_pct", 0.0)

        trade_features = {
            "pnl_pct": pnl_pct,
            "dte_remaining": dte,
            "days_in_trade": trade.days_in_trade,
            "dist_to_short_strike_pct": min_dist_short,
            "vix_now": vix,
            "vix_change_since_entry": 0,
            "vix_vs_sma_ratio_now": vix_vs_sma if vix_vs_sma else 1.0,
            "vix_1d_change_now": vix_1d_chg if vix_1d_chg else 0.0,
            "spot_change_since_entry": 0,
            "pnl_velocity": total_pnl / max(trade.days_in_trade, 1),
            "credit_captured_pct": pnl_pct,
            "max_loss_proximity": abs(total_pnl) / trade.max_loss_total if total_pnl < 0 and trade.max_loss_total > 0 else 0,
            "theta_estimate": theta_per_day,
        }

        all_features = {**market_features, **trade_features}

        risk_score = 0.0
        reasoning = []

        if dte <= 3:
            risk_score += 0.3
            reasoning.append(f"DTE={dte} — expiry imminent, gamma risk high")
        elif dte <= 7:
            risk_score += 0.15
            reasoning.append(f"DTE={dte} — approaching expiry")

        if pnl_pct >= 50:
            reasoning.append(f"P&L at +{pnl_pct:.0f}% of max profit — strong profit zone")
        elif pnl_pct >= 30:
            reasoning.append(f"P&L at +{pnl_pct:.0f}% of max profit — profit building")
        elif pnl_pct >= 0:
            reasoning.append(f"P&L at +{pnl_pct:.0f}% — marginal, theta needs to work")
        elif pnl_pct > -30:
            risk_score += 0.15
            reasoning.append(f"P&L at {pnl_pct:.0f}% — underwater but recoverable")
        elif pnl_pct > -60:
            risk_score += 0.3
            reasoning.append(f"P&L at {pnl_pct:.0f}% — significant loss")
        else:
            risk_score += 0.5
            reasoning.append(f"P&L at {pnl_pct:.0f}% — DEEP LOSS")

        if min_dist_short < 1:
            risk_score += 0.3
            side = closest_short.option_type if closest_short else "?"
            reasoning.append(f"Short {closest_short.strike:.0f} {side} only {min_dist_short:.1f}% away — DANGER ZONE")
        elif min_dist_short < 2:
            risk_score += 0.15
            reasoning.append(f"Closest short strike {min_dist_short:.1f}% away — getting close")
        elif min_dist_short > 5:
            reasoning.append(f"Closest short strike {min_dist_short:.1f}% away — comfortable")

        if vix > 25:
            risk_score += 0.1
            reasoning.append(f"VIX at {vix:.1f} — elevated volatility")
        if vix_vs_sma > 1.15:
            risk_score += 0.15
            reasoning.append(f"VIX surging ({vix_vs_sma:.2f}x its 10d avg) — momentum risk")
        elif vix_vs_sma < 0.85:
            risk_score = max(risk_score - 0.1, 0)
            reasoning.append(f"VIX collapsing ({vix_vs_sma:.2f}x its 10d avg) — favorable trend")
        if abs(vix_1d_chg) > 0.08:
            direction_word = "spike" if vix_1d_chg > 0 else "drop"
            risk_score += 0.05 if vix_1d_chg > 0 else 0
            reasoning.append(f"VIX {direction_word} {abs(vix_1d_chg)*100:.1f}% intraday — sharp move")
        contagion = all_features.get("global_contagion_score", 0)
        if contagion > 0.3:
            risk_score += 0.15
            reasoning.append(f"Global contagion score {contagion:.2f} — cross-geo stress")
        crude_inr = all_features.get("crude_inr_composite", 0)
        if crude_inr > 2.0:
            risk_score += 0.1
            reasoning.append(f"Crude+INR stress at {crude_inr:.1f}")

        ml_exit_prob = 0.5
        if self.is_trained:
            X = pd.DataFrame([all_features]).reindex(
                columns=self.exit_feature_cols, fill_value=0
            ).fillna(0).replace([float("inf"), float("-inf")], 0)
            probs = self.exit_classifier.predict_proba(X)[0]
            classes = list(self.exit_classifier.classes_)
            ml_exit_prob = probs[classes.index(1)] if 1 in classes else 0.5
            reasoning.append(f"ML exit signal: {ml_exit_prob:.0%} probability")

        risk_score = min(risk_score, 1.0)

        # Decision logic
        action = "HOLD"
        partial_legs = None

        if pnl_pct >= 70:
            action = "BOOK_PROFIT"
            reasoning.insert(0, "70%+ of max profit captured — diminishing returns to hold")
        elif pnl_pct >= 50 and dte <= 7:
            action = "BOOK_PROFIT"
            reasoning.insert(0, "50%+ profit with <=7 DTE — gamma risk outweighs remaining theta")
        elif pnl_pct >= 40 and risk_score > 0.5:
            action = "BOOK_PROFIT"
            reasoning.insert(0, "40%+ profit but elevated risk — lock in gains")
        elif partial_exit_side and pnl_pct > 0:
            action = "PARTIAL_EXIT"
            side_label = "put spread" if partial_exit_side == "put" else "call spread"
            partial_legs = [f"{l.action} {l.strike:.0f} {l.option_type}"
                            for l in legs
                            if l.option_type == ("PE" if partial_exit_side == "put" else "CE")]
            reasoning.insert(0, f"One side under pressure — close the {side_label}, keep the profitable side")
        elif risk_score > 0.7:
            action = "EXIT_NOW"
            reasoning.insert(0, f"Risk score {risk_score:.0%} — conditions too dangerous")
        elif pnl_pct < -50:
            action = "EXIT_NOW"
            reasoning.insert(0, "Loss exceeding 50% of max — cut and reassess")
        elif pnl_pct >= 30 and ml_exit_prob > 0.6:
            action = "BOOK_PROFIT"
            reasoning.insert(0, "ML exit signal high — take profits before deterioration")
        elif pnl_pct >= 20 and dte <= 5:
            action = "TRAIL_STOP"
            reasoning.insert(0, "Profit with few DTE left — tighten stop to lock in gains")
        elif pnl_pct < -20 and ml_exit_prob > 0.65:
            action = "EXIT_NOW"
            reasoning.insert(0, "Underwater + ML sees continued deterioration ahead")
        elif min_dist_short < 1.5 and pnl_pct < 0:
            action = "EXIT_NOW"
            reasoning.insert(0, "Strike breached/nearly breached — exit to limit damage")
        else:
            reasoning.insert(0, "Trade on track — theta working, no immediate risk")

        confidence = abs(ml_exit_prob - 0.5) * 2 if self.is_trained else max(0.3, 1 - risk_score)

        return ExitRecommendation(
            trade_id=trade.trade_id,
            action=action,
            confidence=confidence,
            current_pnl_pct=pnl_pct,
            pnl_rupees=total_pnl,
            days_in_trade=trade.days_in_trade,
            dte_remaining=dte,
            reasoning=reasoning,
            risk_score=risk_score,
            theta_per_day=theta_per_day,
            per_leg_pnl=per_leg_pnl,
            partial_exit_legs=partial_legs,
        )


# ── Active Trade File Management ──

def load_active_trades() -> list[ActiveTrade]:
    if not TRADES_FILE.exists():
        return []
    with open(TRADES_FILE) as f:
        data = json.load(f)
    trades = []
    for t in data.get("trades", []):
        if "legs" not in t:
            t["legs"] = []
        # Ensure backward compatibility for peak_pnl_per_unit field
        if "peak_pnl_per_unit" not in t or t["peak_pnl_per_unit"] is None:
            t["peak_pnl_per_unit"] = 0.0
        trades.append(ActiveTrade(**t))
    return trades


def save_active_trades(trades: list[ActiveTrade]):
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "updated_at": str(datetime.now()),
        "trades": [asdict(t) for t in trades],
    }
    with open(TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def add_trade_from_legs(
    trade_id: str,
    entry_date: str,
    expiry_date: str,
    leg_strings: list[str],
    current_pnl: Optional[float] = None,
    notes: str = "",
    strategy_code: Optional[str] = None,
    entry_vix: Optional[float] = None,
    entry_spot: Optional[float] = None,
) -> ActiveTrade:
    """Add a multi-leg trade from --leg format strings."""
    parsed_legs = [parse_leg(s) for s in leg_strings]
    trades = load_active_trades()
    trade = ActiveTrade(
        trade_id=trade_id,
        entry_date=entry_date,
        expiry_date=expiry_date,
        legs=[asdict(l) for l in parsed_legs],
        current_pnl=current_pnl,
        notes=notes,
        strategy_code=strategy_code,
        entry_vix=entry_vix,
        entry_spot=entry_spot,
    )
    trades = [t for t in trades if t.trade_id != trade_id]
    trades.append(trade)
    save_active_trades(trades)
    return trade


def add_trade(
    trade_id: str,
    direction: str,
    entry_date: str,
    expiry_date: str,
    short_strike: float,
    long_strike: float,
    entry_credit: float,
    lots: int = 2,
    lot_size: int = 75,
    notes: str = "",
    strategy_code: Optional[str] = None,
    entry_vix: Optional[float] = None,
    entry_spot: Optional[float] = None,
) -> ActiveTrade:
    """Legacy: add a single-spread trade."""
    ot = "PE" if direction == "put" else "CE"
    qty = lots * lot_size
    sell_price = entry_credit + 0  # placeholder; we use entry_credit as net
    buy_price = 0
    legs = [
        asdict(TradeLeg("SELL", short_strike, ot, qty, sell_price)),
        asdict(TradeLeg("BUY", long_strike, ot, qty, buy_price)),
    ]
    trades = load_active_trades()
    trade = ActiveTrade(
        trade_id=trade_id,
        entry_date=entry_date,
        expiry_date=expiry_date,
        legs=legs,
        direction=direction,
        short_strike=short_strike,
        long_strike=long_strike,
        entry_credit=entry_credit,
        lots=lots,
        lot_size=lot_size,
        notes=notes,
        strategy_code=strategy_code,
        entry_vix=entry_vix,
        entry_spot=entry_spot,
    )
    trades = [t for t in trades if t.trade_id != trade_id]
    trades.append(trade)
    save_active_trades(trades)
    return trade


def remove_trade(trade_id: str):
    trades = load_active_trades()
    trades = [t for t in trades if t.trade_id != trade_id]
    save_active_trades(trades)


def close_trade(
    trade_id: str,
    realized_pnl: float,
    exit_date: Optional[str] = None,
    exit_reason: str = "manual_close",
    notes: str = "",
    strategy_code: Optional[str] = None,
) -> ClosedTradeRecord:
    active_trades = load_active_trades()
    trade = next((t for t in active_trades if t.trade_id == trade_id), None)
    if trade is None:
        raise ValueError(f"Trade not found: {trade_id}")

    resolved_strategy = strategy_code or _infer_strategy_code(trade) or "unknown"
    closed_record = ClosedTradeRecord(
        trade_id=trade.trade_id,
        entry_date=trade.entry_date,
        exit_date=exit_date or str(date.today()),
        expiry_date=trade.expiry_date,
        strategy=resolved_strategy,
        total_pnl=realized_pnl,
        pnl_pct=(realized_pnl / trade.max_loss_total * 100) if trade.max_loss_total > 0 else 0.0,
        max_profit_total=trade.max_profit_total,
        max_loss_total=trade.max_loss_total,
        exit_reason=exit_reason,
        notes=notes or trade.notes,
        legs=trade.legs or [],
    )

    closed_trades = [t for t in load_closed_trades() if t.trade_id != trade_id]
    closed_trades.append(closed_record)
    closed_trades.sort(key=lambda t: (t.exit_date, t.trade_id))
    save_closed_trades(closed_trades)

    remaining_trades = [t for t in active_trades if t.trade_id != trade_id]
    save_active_trades(remaining_trades)
    return closed_record
