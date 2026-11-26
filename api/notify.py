"""Push alerts for the auto paper trader (Telegram). Optional: with no credentials every call is a no-op.

The bot token and chat id come from the dashboard (saved encrypted, write-only, same protection as the Zerodha
credentials) or, as a fallback, from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in the server environment. The token is
never stored in the ledger, never logged, and never returned by any endpoint.

Delivery is best effort and runs in a daemon thread, so a slow or failing Telegram can never delay or break the
trading loop. Noisy event types are throttled so a persistent problem sends a message every few hours, not every minute.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT_SECONDS = 8
UPLOAD_TIMEOUT_SECONDS = 60
MAX_DOCUMENT_BYTES = 45 * 1024 * 1024        # Telegram bots may upload up to 50 MB
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
CHAT_RE = re.compile(r"^(-?\d{3,20}|@[A-Za-z][A-Za-z0-9_]{4,})$")

# kind -> minimum seconds between two alerts with the same code (0 = always send)
THROTTLE: Dict[str, int] = {"ENTRY": 0, "EXIT": 0, "PAUSE": 0, "ERROR": 30 * 60, "SKIP": 6 * 3600}
INFO_CODES = {"DISABLED"}        # INFO events are noise except these


def valid_token(token: str) -> bool:
    return bool(TOKEN_RE.match(token))


def valid_chat_id(chat: str) -> bool:
    return bool(CHAT_RE.match(chat))


def _env_credentials() -> Optional[Tuple[str, str]]:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return (token, chat) if token and chat else None


def mask_chat(chat: str) -> str:
    return ("*" * max(len(chat) - 3, 0) + chat[-3:]) if chat else ""


class Notifier:
    def __init__(self, store: Any = None) -> None:
        self._store = store                       # optional CredentialStore holding {"bot_token", "chat_id"}
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.last_ok: Optional[bool] = None
        self.last_error: Optional[str] = None
        self.last_sent_at: Optional[float] = None

    # ------------------------------------------------------------------ credentials
    def _stored(self) -> Dict[str, Any]:
        if self._store is None:
            return {}
        try:
            return self._store.load()
        except Exception:  # noqa: BLE001 - undecryptable store: fall back to the environment
            return {}

    def _credentials(self) -> Optional[Tuple[str, str]]:
        d = self._stored()
        token, chat = (d.get("bot_token") or "").strip(), (d.get("chat_id") or "").strip()
        return (token, chat) if token and chat else _env_credentials()

    @property
    def configured(self) -> bool:
        return self._credentials() is not None

    def settings_view(self) -> Dict[str, Any]:
        """Flags and a masked chat id only. Never the token."""
        d = self._stored()
        env = _env_credentials()
        source = "dashboard" if (d.get("bot_token") and d.get("chat_id")) else ("environment" if env else None)
        return {"saved": {"bot_token": bool(d.get("bot_token")), "chat_id": bool(d.get("chat_id"))},
                "chat_id_masked": mask_chat(d.get("chat_id") or ""), "source": source,
                "encryption_available": bool(self._store is not None and self._store.encryption_available)}

    def status(self) -> Dict[str, Any]:
        return {"configured": self.configured, "channel": "telegram", "last_ok": self.last_ok,
                "last_error": self.last_error, "last_sent_at": self.last_sent_at, **self.settings_view()}

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
    @staticmethod
    def _call(token: str, method: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any, str]:
        """One Telegram API call. Returns (ok, parsed json or None, detail). detail never contains the token."""
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(API.format(token=token, method=method), data=body,
                                     headers={"Content-Type": "application/json"} if body else {})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:   # noqa: S310 - fixed https host
                status = resp.status
                raw = resp.read() if hasattr(resp, "read") else b""
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = None
            return 200 <= status < 300, parsed, f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 - the message of an HTTP error can echo the URL, so keep only the type
            return False, None, f"{exc.__class__.__name__}" + (f" {exc.code}" if hasattr(exc, "code") else "")

    def send(self, text: str) -> Tuple[bool, str]:
        """Blocking send. Returns (ok, detail)."""
        creds = self._credentials()
        if creds is None:
            return False, "Telegram is not set up yet: save a bot token and chat id"
        token, chat = creds
        ok, _, detail = self._call(token, "sendMessage",
                                   {"chat_id": chat, "text": text[:3800], "disable_web_page_preview": True})
        self.last_ok, self.last_error = ok, (None if ok else detail)
        if ok:
            self.last_sent_at = time.time()
        else:
            logger.warning("Alert delivery failed: %s", detail)
        return ok, detail

    def send_document(self, filename: str, data: bytes, caption: str = "") -> Tuple[bool, str]:
        """Blocking upload of a file to the saved chat (used for the encrypted ledger backup). Never raises."""
        creds = self._credentials()
        if creds is None:
            return False, "Telegram is not set up yet: save a bot token and chat id"
        if len(data) > MAX_DOCUMENT_BYTES:
            return False, "File is too large for Telegram"
        token, chat = creds
        boundary = "----niftysell" + os.urandom(8).hex()
        parts = [("chat_id", chat), ("caption", caption[:900])]
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in parts)
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(API.format(token=token, method="sendDocument"), data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_SECONDS) as resp:   # noqa: S310 - fixed https host
                ok, detail = 200 <= resp.status < 300, f"HTTP {resp.status}"
        except Exception as exc:  # noqa: BLE001 - never echo the exception text: it can contain the URL with the token
            ok, detail = False, f"{exc.__class__.__name__}" + (f" {exc.code}" if hasattr(exc, "code") else "")
        self.last_ok, self.last_error = ok, (None if ok else detail)
        if ok:
            self.last_sent_at = time.time()
        else:
            logger.warning("Document delivery failed: %s", detail)
        return ok, detail

    def detect_chat_id(self) -> Tuple[Optional[str], str]:
        """Find the chat id of whoever last messaged the saved bot (the user presses Start / sends 'hi' first)."""
        token = (self._stored().get("bot_token") or "").strip()
        if not token:
            return None, "Save the bot token first"
        ok, parsed, detail = self._call(token, "getUpdates")
        if not ok or not isinstance(parsed, dict):
            return None, f"Telegram did not answer ({detail}). Check the bot token."
        for upd in reversed(parsed.get("result") or []):
            chat = ((upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}).get("chat") or {})
            if chat.get("id") is not None and chat.get("type") in ("private", "group", "supergroup", "channel"):
                return str(chat["id"]), "found"
        return None, "No message found yet. Open your bot in Telegram, press Start (or send any message), then try again."

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
