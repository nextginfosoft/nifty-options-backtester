"""Zerodha Kite Connect session handling and live index quotes.

The Kite API secret stays on the server (environment only). The browser only ever
sees: whether Kite is configured / connected, the public login URL, and quotes.
Kite access tokens expire at ~06:00 IST the next day, so the dashboard offers a
one-click "Connect Kite" each morning; the callback stores the token on disk
(STORAGE_DIR/kite_token.json, owner-only) and in the process environment.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/broker", tags=["broker"])

IST = ZoneInfo("Asia/Kolkata")
_live_cache: Dict[str, Any] = {"at": 0.0, "value": None}
LIVE_TTL_SECONDS = 5.0


def _now() -> datetime:
    return datetime.now(IST)


def _expiry_after(saved_at: datetime) -> datetime:
    """Kite tokens die at the next 06:00 IST."""
    six = saved_at.replace(hour=6, minute=0, second=0, microsecond=0)
    return six if saved_at < six else six + timedelta(days=1)


def _token_file(storage_dir: Path) -> Path:
    return Path(storage_dir) / "kite_token.json"


def configured() -> bool:
    return bool(os.getenv("KITE_API_KEY") and os.getenv("KITE_API_SECRET"))


def load_saved_token(storage_dir: Path) -> bool:
    """Restore today's token from disk into the environment. Returns True if usable."""
    f = _token_file(storage_dir)
    if not f.is_file():
        return False
    try:
        saved = json.loads(f.read_text(encoding="utf-8"))
        saved_at = datetime.fromisoformat(saved["saved_at"])
        if _now() >= _expiry_after(saved_at):
            return False
        os.environ["KITE_ACCESS_TOKEN"] = saved["access_token"]
        return True
    except (ValueError, KeyError, OSError):
        return False


def _save_token(storage_dir: Path, access_token: str) -> datetime:
    f = _token_file(storage_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    now = _now()
    f.write_text(json.dumps({"access_token": access_token, "saved_at": now.isoformat()}), encoding="utf-8")
    try:
        f.chmod(0o600)
    except OSError:
        pass
    os.environ["KITE_ACCESS_TOKEN"] = access_token
    _live_cache["value"] = None
    return now


def forget_token(storage_dir: Path) -> None:
    """Disconnect: delete the stored token and clear it from the environment."""
    try:
        _token_file(storage_dir).unlink()
    except FileNotFoundError:
        pass
    os.environ.pop("KITE_ACCESS_TOKEN", None)
    _live_cache["value"] = None


def _token_expires_at(storage_dir: Path) -> Optional[str]:
    f = _token_file(storage_dir)
    try:
        saved_at = datetime.fromisoformat(json.loads(f.read_text(encoding="utf-8"))["saved_at"])
    except (ValueError, KeyError, OSError):
        return None
    exp = _expiry_after(saved_at)
    return exp.isoformat() if _now() < exp else None


def _exchange(request_token: str) -> str:
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
    session = kite.generate_session(request_token, api_secret=os.environ["KITE_API_SECRET"])
    return session["access_token"]


def _fetch_live() -> Dict[str, Any]:
    if not configured():
        return {"ok": False, "source": None, "reason": "Kite is not configured on the server"}
    if not os.getenv("KITE_ACCESS_TOKEN"):
        return {"ok": False, "source": "kite", "reason": "Kite is not connected today. Use Connect Kite."}
    from data.kite_adapter import KiteFyersAdapter

    resp = KiteFyersAdapter().quotes({"symbols": "NSE:NIFTY50-INDEX,NSE:INDIAVIX-INDEX"})
    if resp.get("s") != "ok":
        reason = "Kite token expired. Use Connect Kite." if resp.get("code") == -15 else str(resp.get("message"))
        return {"ok": False, "source": "kite", "reason": reason}
    vals = {i["n"]: i["v"] for i in resp["d"]}
    return {
        "ok": True, "source": "kite",
        "spot": vals.get("NSE:NIFTY50-INDEX", {}).get("lp"),
        "vix": vals.get("NSE:INDIAVIX-INDEX", {}).get("lp"),
        "as_of": _now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- endpoints
@router.get("/status")
async def status(request: Request) -> Dict[str, Any]:
    storage = request.app.state.settings.storage_dir
    expires = _token_expires_at(storage)
    from api import autologin  # local import: autologin imports this module

    creds = request.app.state.creds.public_view()
    return {
        "broker": "kite" if configured() else "none",
        "configured": configured(),
        "connected": bool(expires and os.getenv("KITE_ACCESS_TOKEN")),
        "token_expires_at": expires,
        "auto_login_enabled": creds["auto_login_enabled"],
        "last_login": autologin.last_result(),
    }


@router.get("/kite/login-url")
async def login_url() -> Dict[str, str]:
    if not configured():
        raise HTTPException(status_code=400, detail="Set KITE_API_KEY and KITE_API_SECRET on the server first")
    from kiteconnect import KiteConnect

    return {"url": KiteConnect(api_key=os.environ["KITE_API_KEY"]).login_url()}


@router.get("/kite/callback")
async def callback(request: Request, request_token: str = Query(""), status: str = Query("")):
    """Kite redirects here after login (set this URL as the app's redirect URL)."""
    if status != "success" or not request_token:
        return RedirectResponse("/?broker=error", status_code=303)
    try:
        token = await run_in_threadpool(_exchange, request_token)
        _save_token(request.app.state.settings.storage_dir, token)
    except Exception as exc:  # noqa: BLE001 - never echo secrets; log class only
        logger.error("Kite token exchange failed: %s", exc.__class__.__name__)
        return RedirectResponse("/?broker=error", status_code=303)
    return RedirectResponse("/?broker=connected", status_code=303)


class TokenBody(BaseModel):
    request_token: str = Field(min_length=8, max_length=128)


@router.post("/kite/token")
async def manual_token(body: TokenBody, request: Request) -> Dict[str, Any]:
    """Fallback when the redirect URL is not routed to this server: paste the request_token."""
    if not configured():
        raise HTTPException(status_code=400, detail="Set KITE_API_KEY and KITE_API_SECRET on the server first")
    try:
        token = await run_in_threadpool(_exchange, body.request_token.strip())
    except Exception as exc:  # noqa: BLE001
        logger.error("Kite token exchange failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=400, detail="Kite rejected the request token (it is single-use and short-lived)")
    saved = _save_token(request.app.state.settings.storage_dir, token)
    return {"connected": True, "token_expires_at": _expiry_after(saved).isoformat()}


@router.get("/live")
async def live() -> Dict[str, Any]:
    """Live Nifty spot and India VIX from Kite (cached for a few seconds)."""
    if time.time() - _live_cache["at"] < LIVE_TTL_SECONDS and _live_cache["value"] is not None:
        return _live_cache["value"]
    value = await run_in_threadpool(_fetch_live)
    _live_cache.update(at=time.time(), value=value)
    return value
