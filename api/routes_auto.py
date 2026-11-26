"""Endpoints for the automatic PAPER trader: status, on/off, pause, lots, decision timeline, preview."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import server
from api.autotrader import AutoTrader, MAX_LOTS, MIN_LOTS
from api.credentials import CredentialError
from api.notify import valid_chat_id, valid_token

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


@router.get("/exit-preview")
async def exit_preview(request: Request) -> Dict[str, Any]:
    """What the exit rules would do to each open position right now, on live prices. Records and closes nothing."""
    return await run_in_threadpool(_at(request).exit_preview, server.now_ist())


class AlertsIn(BaseModel):
    bot_token: Optional[str] = Field(default=None, max_length=120)
    chat_id: Optional[str] = Field(default=None, max_length=40)


@router.get("/alerts")
async def alerts_status(request: Request) -> Dict[str, Any]:
    return request.app.state.notifier.status()


@router.put("/alerts")
async def alerts_save(body: AlertsIn, request: Request) -> Dict[str, Any]:
    """Save the Telegram bot token / chat id encrypted. Blank keeps the stored value; nothing is ever returned."""
    token, chat = (body.bot_token or "").strip(), (body.chat_id or "").strip()
    if token and not valid_token(token):
        raise HTTPException(status_code=400, detail="That does not look like a bot token (expected 123456:ABC... from @BotFather)")
    if chat and not valid_chat_id(chat):
        raise HTTPException(status_code=400, detail="Chat id must be a number such as 123456789 (or @channelname)")
    if not token and not chat:
        raise HTTPException(status_code=400, detail="Enter a bot token and/or a chat id")
    try:
        request.app.state.alert_creds.merge({"bot_token": token, "chat_id": chat})
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return request.app.state.notifier.status()


@router.get("/backup")
async def backup_status(request: Request) -> Dict[str, Any]:
    return request.app.state.backup.status(server.now_ist())


@router.post("/backup/run")
async def backup_run(request: Request) -> Dict[str, Any]:
    """Back up the ledger now (encrypted, sent to Telegram)."""
    svc = request.app.state.backup
    result = await run_in_threadpool(svc.run, server.now_ist())
    return {**result, **svc.status(server.now_ist())}


@router.delete("/alerts")
async def alerts_remove(request: Request) -> Dict[str, Any]:
    request.app.state.alert_creds.delete()
    return request.app.state.notifier.status()


@router.post("/alerts/detect-chat")
async def alerts_detect_chat(request: Request) -> Dict[str, Any]:
    """Look up the chat id of the person who messaged the saved bot, and save it."""
    n = request.app.state.notifier
    chat, detail = await run_in_threadpool(n.detect_chat_id)
    if chat is None:
        return {"ok": False, "detail": detail, **n.status()}
    try:
        request.app.state.alert_creds.merge({"chat_id": chat})
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "detail": "Chat id found and saved", **n.status()}


@router.post("/alerts/test")
async def alerts_test(request: Request) -> Dict[str, Any]:
    ok, detail = await run_in_threadpool(request.app.state.notifier.send, "[Nifty paper] Test alert: alerts are working.")
    return {"ok": ok, "detail": detail, **request.app.state.notifier.status()}


@router.get("/preview")
async def preview(request: Request) -> Dict[str, Any]:
    """What the trader would decide right now (schedule gates reported but ignored). Records nothing."""
    return await run_in_threadpool(_at(request).preview, server.now_ist())
