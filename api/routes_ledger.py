"""Journal, trade, daily-log, status and CSV-export endpoints backed by the SQLite ledger."""

from __future__ import annotations

import os
import time
from datetime import date
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import live_pnl, server
from api.ledger import Ledger, max_drawdown_pct

router = APIRouter()


def _ledger(request: Request) -> Ledger:
    return request.app.state.ledger


# ---------------------------------------------------------------- request models
class LegIn(BaseModel):
    action: Literal["BUY", "SELL"]
    strike: float = Field(gt=0)
    option_type: Literal["CE", "PE"]
    entry_price: float = Field(ge=0, description="Fill price per unit from your broker")


class JournalCreate(BaseModel):
    journal_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    label: str = Field(min_length=1, max_length=80)
    initial_capital: float = Field(gt=0)
    strategy_track: Literal["weekly", "monthly", "combined"] = "weekly"
    notes: str = Field(default="", max_length=2000)


class JournalUpdate(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=80)
    notes: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[Literal["active", "closed"]] = None


class TradeOpen(BaseModel):
    journal_id: str
    trade_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,40}$")
    strategy: str = Field(min_length=1, max_length=60)
    entry_date: date
    entry_time_ist: str = Field(default="", max_length=8)
    expiry_date: date
    legs: List[LegIn] = Field(min_length=1, max_length=8)
    lots: int = Field(gt=0, le=1000)
    lot_size: Optional[int] = Field(default=None, gt=0)
    entry_credit: Optional[float] = Field(default=None, description="Net credit per unit; computed from legs if omitted")
    entry_vix: Optional[float] = None
    entry_spot: Optional[float] = None
    regime: str = ""
    ml_score: Optional[float] = None
    capital_deployed: float = Field(default=0.0, ge=0)
    notes: str = Field(default="", max_length=2000)


class TradeClose(BaseModel):
    exit_debit: float = Field(ge=0, description="Net debit per unit paid to close the structure")
    exit_reason: str = Field(min_length=1, max_length=60)
    exit_date: Optional[date] = None
    exit_time_ist: str = Field(default="", max_length=8)
    brokerage: float = Field(default=0.0, ge=0)
    notes: str = Field(default="", max_length=2000)


class DailyLogIn(BaseModel):
    log_date: Optional[date] = None
    account_equity: float
    cumulative_pnl: float
    open_trades: int = Field(ge=0)
    vix_close: Optional[float] = None
    regime: str = ""
    account_dd_pct: float = 0.0
    notes: str = Field(default="", max_length=2000)


# ---------------------------------------------------------------- journals
@router.get("/journals")
async def list_journals(request: Request) -> Dict[str, Any]:
    led = _ledger(request)
    return {"journals": [led.summary(j["journal_id"]) for j in led.list_journals()]}


@router.post("/journals")
async def create_journal(body: JournalCreate, request: Request) -> Dict[str, Any]:
    led = _ledger(request)
    led.create_journal(body.journal_id, body.label, body.initial_capital, body.strategy_track, body.notes)
    return led.summary(body.journal_id)


@router.get("/journals/{journal_id}")
async def get_journal(journal_id: str, request: Request) -> Dict[str, Any]:
    return _ledger(request).summary(journal_id)


@router.patch("/journals/{journal_id}")
async def update_journal(journal_id: str, body: JournalUpdate, request: Request) -> Dict[str, Any]:
    led = _ledger(request)
    led.update_journal(journal_id, **body.model_dump(exclude_none=True))
    return led.summary(journal_id)


# ---------------------------------------------------------------- status
@router.get("/status")
async def account_status(request: Request, journal_id: Optional[str] = Query(None)) -> Dict[str, Any]:
    led = _ledger(request)
    if journal_id:
        s = led.summary(journal_id)
        label, initial = s["label"], s["initial_capital"]
        pnl, dd, win = s["total_pnl"], s["account_dd_pct"], s["win_rate_pct"]
        open_n, closed_n = s["open_trades"], s["closed_trades"]
    else:
        initial = float(os.getenv("PAPER_INITIAL_CAPITAL", "2500000"))
        closed = sorted(led.list_trades(None, "closed"), key=lambda t: t["exit_date"] or "")
        pnls = [t["net_pnl"] or 0.0 for t in closed]
        label, pnl, dd = "All journals", round(sum(pnls), 2), max_drawdown_pct(initial, pnls)
        win = round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1) if pnls else None
        open_n, closed_n = len(led.list_trades(None, "open")), len(closed)

    equity = initial + pnl
    row = server._state["latest_row"]
    vix = float(row.get("vix", 0)) if row is not None else None
    return {
        "journal_id": journal_id or "all",
        "label": label,
        "account_equity": equity,
        "initial_capital": initial,
        "cumulative_pnl": pnl,
        "account_dd_pct": dd,
        "open_trades": open_n,
        "closed_trades": closed_n,
        "win_rate_pct": win,
        "available_capital": equity * 0.80,
        "reserve_capital": equity * 0.20,
        "vix_now": vix,
        "spot_now": float(row.get("nifty_close", 0)) if row is not None else None,
        "regime": server._infer_regime(vix) if vix else None,
        "market_date": str(getattr(row, "name", "")) if row is not None else None,
        "data_state": server._state["status"],
        "last_refresh": server._state["last_refresh"],
    }


# ---------------------------------------------------------------- trades
@router.get("/trades")
async def list_trades(request: Request, journal_id: Optional[str] = Query(None),
                      status: Literal["open", "closed", "all"] = Query("all")) -> Dict[str, Any]:
    trades = _ledger(request).list_trades(journal_id, status)
    return {"count": len(trades), "trades": trades}


_live_cache: Dict[str, Any] = {}
LIVE_TTL_SECONDS = 5.0


@router.get("/trades/live")
async def live_trades(request: Request, journal_id: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Real-price P&L, exit-at-market estimate and exit-rule alerts for open trades (Kite quotes)."""
    key = journal_id or "*"
    hit = _live_cache.get(key)
    if hit and time.time() - hit["at"] < LIVE_TTL_SECONDS:
        return hit["value"]
    open_trades = _ledger(request).list_trades(journal_id, "open")
    now = server.now_ist()
    if not open_trades:
        value: Dict[str, Any] = {"ok": True, "source": "kite", "as_of": now.isoformat(timespec="seconds"),
                                 "spot": None, "trades": []}
    else:
        try:
            data = await run_in_threadpool(live_pnl.fetch_quotes, open_trades)
        except live_pnl.LiveUnavailable as exc:
            return {"ok": False, "reason": str(exc), "trades": []}
        value = {
            "ok": True, "source": "kite", "as_of": now.isoformat(timespec="seconds"), "spot": data["spot"],
            "trades": [live_pnl.evaluate_trade(t, data["legs"], data["spot"], now.date()) for t in open_trades],
        }
    _live_cache[key] = {"at": time.time(), "value": value}
    return value


@router.post("/trades")
async def open_trade(body: TradeOpen, request: Request) -> Dict[str, Any]:
    data = body.model_dump(mode="json")
    data["lot_size"] = body.lot_size or request.app.state.settings.lot_size
    data["legs"] = [{"action": l.action, "strike": l.strike, "option_type": l.option_type,
                     "entry_price": l.entry_price} for l in body.legs]
    _live_cache.clear()
    return _ledger(request).open_trade(body.journal_id, data)


@router.post("/trades/{trade_id}/close")
async def close_trade(trade_id: str, body: TradeClose, request: Request,
                      journal_id: str = Query(...)) -> Dict[str, Any]:
    _live_cache.clear()
    return _ledger(request).close_trade(
        journal_id, trade_id, exit_debit=body.exit_debit, exit_reason=body.exit_reason,
        exit_date=(body.exit_date or date.today()).isoformat(), exit_time_ist=body.exit_time_ist,
        brokerage=body.brokerage, notes=body.notes,
    )


@router.delete("/trades/{trade_id}")
async def delete_trade(trade_id: str, request: Request, journal_id: str = Query(...)) -> Dict[str, Any]:
    _live_cache.clear()
    _ledger(request).delete_trade(journal_id, trade_id)
    return {"deleted": trade_id}


# ---------------------------------------------------------------- daily log
@router.get("/journals/{journal_id}/daily-log")
async def list_daily_log(journal_id: str, request: Request) -> Dict[str, Any]:
    rows = _ledger(request).list_daily_log(journal_id)
    return {"count": len(rows), "entries": rows}


@router.post("/journals/{journal_id}/daily-log")
async def upsert_daily_log(journal_id: str, body: DailyLogIn, request: Request) -> Dict[str, Any]:
    entry = body.model_dump(mode="json")
    entry["log_date"] = (body.log_date or date.today()).isoformat()
    return _ledger(request).upsert_daily_log(journal_id, entry)


# ---------------------------------------------------------------- exports
def _csv(text: str, filename: str) -> Response:
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export/trades.csv")
async def export_trades(request: Request, journal_id: Optional[str] = Query(None)) -> Response:
    return _csv(_ledger(request).trades_csv(journal_id), "TRADES.csv")


@router.get("/export/daily-log.csv")
async def export_daily_log(request: Request, journal_id: str = Query(...)) -> Response:
    return _csv(_ledger(request).daily_log_csv(journal_id), "DAILY_LOG.csv")
