"""Endpoints for the automatic PAPER trader: status, on/off, pause, lots, decision timeline, preview."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import server
from api.autotrader import AutoTrader, MAX_LOTS, MIN_LOTS

router = APIRouter(prefix="/auto", tags=["auto"])


def _at(request: Request) -> AutoTrader:
    return request.app.state.autotrader


class EnableBody(BaseModel):
    lots: Optional[int] = Field(default=None, ge=MIN_LOTS, le=MAX_LOTS)


class LotsBody(BaseModel):
    lots: int = Field(ge=MIN_LOTS, le=MAX_LOTS)


class PauseBody(BaseModel):
    reason: str = Field(default="manual", min_length=1, max_length=60)


@router.get("/status")
async def status(request: Request) -> Dict[str, Any]:
    return _at(request).status(server.now_ist())


@router.post("/enable")
async def enable(body: EnableBody, request: Request) -> Dict[str, Any]:
    at = _at(request)
    at.enable(server.now_ist(), body.lots)
    return at.status(server.now_ist())


@router.post("/disable")
async def disable(request: Request) -> Dict[str, Any]:
    at = _at(request)
    at.disable(server.now_ist())
    return at.status(server.now_ist())


@router.post("/pause")
async def pause(body: PauseBody, request: Request) -> Dict[str, Any]:
    at = _at(request)
    at.pause(server.now_ist(), body.reason)
    return at.status(server.now_ist())


@router.post("/resume")
async def resume(request: Request) -> Dict[str, Any]:
    at = _at(request)
    at.resume(server.now_ist())
    return at.status(server.now_ist())


@router.put("/lots")
async def set_lots(body: LotsBody, request: Request) -> Dict[str, Any]:
    at = _at(request)
    try:
        at.set_lots(server.now_ist(), body.lots)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return at.status(server.now_ist())


@router.get("/events")
async def events(request: Request, limit: int = Query(200, ge=1, le=1000), day: Optional[str] = Query(None)) -> Dict[str, Any]:
    rows = request.app.state.ledger.list_events(limit, day)
    return {"count": len(rows), "events": rows}


@router.get("/preview")
async def preview(request: Request) -> Dict[str, Any]:
    """What the trader would decide right now (schedule gates reported but ignored). Records nothing."""
    return await run_in_threadpool(_at(request).preview, server.now_ist())
