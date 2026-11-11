"""Daily automatic Kite login, with strict attempt limits.

Zerodha can lock an account after repeated failed logins, so this is deliberately
conservative: at most 2 automatic attempts per IST day, 15 minutes apart, and no further
automatic tries after a credential rejection until the credentials are updated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

from starlette.concurrency import run_in_threadpool

from api import broker_routes
from api.credentials import CredentialError
from api.kite_login import LoginError, login_for_request_token

logger = logging.getLogger(__name__)

MAX_AUTO_ATTEMPTS_PER_DAY = 2
MIN_GAP_SECONDS = 15 * 60
MANUAL_COOLDOWN_SECONDS = 60
EARLIEST_HOUR_IST = 6          # Kite tokens expire ~06:00 IST
CHECK_INTERVAL_SECONDS = 300

state: Dict[str, Any] = {"day": None, "attempts": 0, "last_at": 0.0, "blocked": False, "last": None}


def reset_state() -> None:
    state.update(day=None, attempts=0, last_at=0.0, blocked=False, last=None)


def _today_ist() -> str:
    return broker_routes._now().date().isoformat()


def _roll_day() -> None:
    today = _today_ist()
    if state["day"] != today:
        state.update(day=today, attempts=0, last_at=0.0)


def last_result() -> Optional[Dict[str, Any]]:
    return state["last"]


def _record(ok: bool, message: str, automatic: bool) -> Dict[str, Any]:
    result = {"at": broker_routes._now().isoformat(timespec="seconds"), "ok": ok,
              "message": message, "automatic": automatic}
    state["last"] = result
    return result


def _exchange_and_save(creds: Dict[str, Any], storage_dir) -> None:
    os.environ["KITE_API_KEY"] = creds["api_key"]
    os.environ["KITE_API_SECRET"] = creds["api_secret"]
    request_token = login_for_request_token(creds["user_id"], creds["password"], creds["totp_secret"],
                                            creds["api_key"])
    access_token = broker_routes._exchange(request_token)
    broker_routes._save_token(storage_dir, access_token)


async def run_login(app, automatic: bool) -> Dict[str, Any]:
    """One login attempt. Never raises; returns {at, ok, message, automatic}. Never includes secrets."""
    _roll_day()
    store = app.state.creds
    try:
        creds = store.load()
    except CredentialError as exc:
        return _record(False, str(exc), automatic)
    if not store.has_all_for_autologin(creds):
        return _record(False, "Save all five credentials first", automatic)

    now = time.time()
    if not automatic and now - state["last_at"] < MANUAL_COOLDOWN_SECONDS:
        return _record(False, "Please wait a minute between login attempts", automatic)
    state["attempts"] += 1
    state["last_at"] = now

    try:
        await run_in_threadpool(_exchange_and_save, creds, app.state.settings.storage_dir)
    except LoginError as exc:
        if exc.fatal:
            state["blocked"] = True
        logger.warning("Kite auto-login failed (fatal=%s)", exc.fatal)
        return _record(False, str(exc), automatic)
    except Exception as exc:  # noqa: BLE001 - class name only; message may echo request data
        logger.warning("Kite auto-login error: %s", exc.__class__.__name__)
        return _record(False, f"Login request failed ({exc.__class__.__name__}); will retry later", automatic)

    state["blocked"] = False
    return _record(True, "Connected for today's session", automatic)


def clear_block() -> None:
    """Call when credentials change so a fresh attempt is allowed."""
    state["blocked"] = False
    state["attempts"] = 0


def is_due(app, now_ist: Optional[datetime] = None) -> bool:
    _roll_day()
    now_ist = now_ist or broker_routes._now()
    store = app.state.creds
    try:
        data = store.load()
    except CredentialError:
        return False
    if not (data.get("auto_login") and store.has_all_for_autologin(data)):
        return False
    if state["blocked"] or state["attempts"] >= MAX_AUTO_ATTEMPTS_PER_DAY:
        return False
    if (now_ist.hour, now_ist.minute) < (EARLIEST_HOUR_IST, 30):  # not before 06:30 IST
        return False
    if time.time() - state["last_at"] < MIN_GAP_SECONDS:
        return False
    if broker_routes._token_expires_at(app.state.settings.storage_dir) and os.getenv("KITE_ACCESS_TOKEN"):
        return False  # already connected for this session
    return True


async def scheduler(app) -> None:
    """Background loop started by the app lifespan."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        try:
            if is_due(app):
                result = await run_login(app, automatic=True)
                logger.info("Kite auto-login attempt: ok=%s", result["ok"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Auto-login scheduler error: %s", exc.__class__.__name__)
