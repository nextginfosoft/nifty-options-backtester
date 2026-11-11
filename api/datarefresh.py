"""Automatic market-data refresh on trading days.

One slot per weekday (IST): 08:45, so the previous completed session is loaded before the 11:00-13:00
entry window. (The fetcher never includes today's bar by design, so there is nothing to gain from an
evening refresh; the next morning's run picks up the day's close.)

A slot whose time already passed when the app started is treated as done (the startup load already
fetched data). A failed slot retries up to 3 times, 30 minutes apart, then waits for the next day. The
previous data stays in service whenever a refresh fails.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

from starlette.concurrency import run_in_threadpool

from api import server

logger = logging.getLogger(__name__)

SLOTS: Dict[str, Tuple[int, int]] = {"morning": (8, 45)}
MAX_TRIES_PER_SLOT = 3
RETRY_GAP_SECONDS = 30 * 60
CHECK_INTERVAL_SECONDS = 120

state: Dict[str, object] = {"done": set(), "tries": {}, "last_try": 0.0}


def reset_state() -> None:
    state.update(done=set(), tries={}, last_try=0.0)


def _key(now: datetime, slot: str) -> str:
    return f"{now.date().isoformat()}:{slot}"


def mark_passed_slots_done(now: Optional[datetime] = None) -> None:
    """Call at startup: the startup load already covers every slot whose time has passed."""
    now = now or server.now_ist()
    for slot, (h, m) in SLOTS.items():
        if (now.hour, now.minute) >= (h, m):
            state["done"].add(_key(now, slot))


def due_slot(now: Optional[datetime] = None) -> Optional[str]:
    """The slot that should run now, or None."""
    now = now or server.now_ist()
    if now.weekday() >= 5:
        return None
    for slot, (h, m) in sorted(SLOTS.items(), key=lambda kv: kv[1]):
        key = _key(now, slot)
        if (now.hour, now.minute) < (h, m) or key in state["done"]:
            continue
        tries = int(state["tries"].get(key, 0))
        if tries >= MAX_TRIES_PER_SLOT:
            continue
        if tries and time.time() - float(state["last_try"]) < RETRY_GAP_SECONDS:
            continue
        return slot
    return None


async def run_slot(slot: str, now: Optional[datetime] = None) -> Dict[str, object]:
    now = now or server.now_ist()
    key = _key(now, slot)
    state["tries"][key] = int(state["tries"].get(key, 0)) + 1
    state["last_try"] = time.time()
    try:
        result = await run_in_threadpool(server.refresh_blocking, slot)
    except server.RefreshBusy:
        state["tries"][key] -= 1  # someone else is refreshing; do not burn a try
        return {"ok": False, "message": "busy"}
    if result["ok"]:
        state["done"].add(key)
    return result


async def scheduler() -> None:
    mark_passed_slots_done()
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        try:
            if server._state["status"] == "loading":
                continue
            slot = due_slot()
            if slot:
                result = await run_slot(slot)
                logger.info("Scheduled data refresh (%s): ok=%s %s", slot, result.get("ok"), result.get("latest_date", ""))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Data refresh scheduler error: %s", exc.__class__.__name__)
