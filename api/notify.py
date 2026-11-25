"""Push alerts for the auto paper trader (Telegram). Optional: with no credentials every call is a no-op.

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the server environment. The token is read from the environment at
send time, never stored in the ledger, never logged, and never returned by any endpoint.

Delivery is best effort and runs in a daemon thread, so a slow or failing Telegram can never delay or break the
trading loop. Noisy event types are throttled so a persistent problem sends a message every few hours, not every minute.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 8

# kind -> minimum seconds between two alerts with the same code (0 = always send)
THROTTLE: Dict[str, int] = {"ENTRY": 0, "EXIT": 0, "PAUSE": 0, "ERROR": 30 * 60, "SKIP": 6 * 3600}
INFO_CODES = {"DISABLED"}        # INFO events are noise except these


def _credentials() -> Optional[Tuple[str, str]]:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return (token, chat) if token and chat else None


class Notifier:
    def __init__(self) -> None:
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.last_ok: Optional[bool] = None
        self.last_error: Optional[str] = None
        self.last_sent_at: Optional[float] = None

    @property
    def configured(self) -> bool:
        return _credentials() is not None

    def status(self) -> Dict[str, Any]:
        return {"configured": self.configured, "channel": "telegram", "last_ok": self.last_ok,
                "last_error": self.last_error, "last_sent_at": self.last_sent_at}

    # ------------------------------------------------------------------ policy
    def wants(self, kind: str, code: str, now: Optional[float] = None) -> bool:
        if kind == "INFO":
            return code in INFO_CODES
        if kind not in THROTTLE:
            return False
        gap = THROTTLE[kind]
        if gap == 0:
            return True
        now = time.time() if now is None else now
        key = f"{kind}:{code}"
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < gap:
                return False
            self._last[key] = now
            return True

    # ------------------------------------------------------------------ delivery
    def send(self, text: str) -> Tuple[bool, str]:
        """Blocking send. Returns (ok, detail); detail never contains the token."""
        creds = _credentials()
        if creds is None:
            return False, "Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"
        token, chat = creds
        body = json.dumps({"chat_id": chat, "text": text[:3800], "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(API.format(token=token), data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:   # noqa: S310 - fixed https host
                ok, detail = 200 <= resp.status < 300, f"HTTP {resp.status}"
        except Exception as exc:  # noqa: BLE001 - the message of an HTTP error can echo the URL, so keep only the type
            ok, detail = False, f"{exc.__class__.__name__}" + (f" {exc.code}" if hasattr(exc, "code") else "")
        self.last_ok, self.last_error = ok, (None if ok else detail)
        if ok:
            self.last_sent_at = time.time()
        else:
            logger.warning("Alert delivery failed: %s", detail)
        return ok, detail

    def notify_event(self, at: str, kind: str, code: str, message: str, trade_id: Optional[str] = None) -> None:
        """Ledger hook: called for every timeline event. Never raises, never blocks."""
        try:
            if not self.configured or not self.wants(kind, code):
                return
            tag = {"ENTRY": "ENTRY", "EXIT": "EXIT", "PAUSE": "PAUSED", "ERROR": "PROBLEM", "SKIP": "NO TRADE", "INFO": "INFO"}[kind]
            text = f"[Nifty paper] {tag}: {code.replace('_', ' ')}\n{message}\n{at.replace('T', ' ')[:16]} IST"
            threading.Thread(target=self.send, args=(text,), daemon=True).start()
        except Exception:  # noqa: BLE001
            logger.warning("Alert hook failed")
