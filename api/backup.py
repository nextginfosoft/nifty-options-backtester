"""Daily off-server backup of the trade ledger.

Every day after the market closes (16:00 IST) the ledger (trades, timeline, journals, settings) is snapshotted with
SQLite's online backup, compressed, ENCRYPTED with the server's CREDENTIALS_KEY and sent to the owner's Telegram chat.
A few encrypted copies also stay on the server volume. The file never leaves the server unencrypted: with no key the
backup is refused rather than sent in plain text. Restore with scripts/restore_ledger_backup.py.

The ledger holds no passwords or API keys (those live in separate encrypted files), but it is the only record of the
paper-trading history, so it is the one thing that must survive losing the server.
"""

from __future__ import annotations

import gzip
import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BACKUP_HOUR_IST = 16           # after the 15:30 close, once the day's exits are recorded
KEEP_LOCAL = 7
RETRY_SECONDS = 30 * 60
MAX_TRIES_PER_DAY = 6
STALE_AFTER = timedelta(hours=36)
CHECK_SECONDS = 60
SUFFIX = ".sqlite3.gz.enc"

KV_OK, KV_ERROR, KV_LOCAL = "backup.last_ok_at", "backup.last_error", "backup.last_local_at"


class BackupService:
    def __init__(self, ledger: Any, store: Any, notifier: Any, directory: Path):
        self.ledger = ledger
        self.store = store                 # CredentialStore used only for its encryption key
        self.notifier = notifier
        self.dir = Path(directory)
        self._lock = threading.Lock()
        self._tries: Dict[str, int] = {}
        self._last_try = 0.0
        self._local_day: Optional[str] = None

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> bytes:
        """Consistent compressed copy of the live ledger (safe while the app is writing)."""
        fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        Path(tmp).unlink(missing_ok=True)
        src = sqlite3.connect(self.ledger.db_path, timeout=10)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode = DELETE")
        finally:
            dst.close()
            src.close()
        try:
            return gzip.compress(Path(tmp).read_bytes())
        finally:
            Path(tmp).unlink(missing_ok=True)

    # ------------------------------------------------------------------ schedule
    def due(self, now: datetime) -> bool:
        if now.hour < BACKUP_HOUR_IST:
            return False
        day = now.date().isoformat()
        if (self.ledger.kv_get(KV_OK, "") or "")[:10] == day:
            return False
        if not self.notifier.configured:
            return self._local_day != day          # keep one local copy a day, nothing to retry
        if self._tries.get(day, 0) >= MAX_TRIES_PER_DAY:
            return False
        return time.monotonic() - self._last_try >= RETRY_SECONDS

    def run(self, now: datetime) -> Dict[str, Any]:
        """Take, store and send one backup. Never raises."""
        with self._lock:
            day = now.date().isoformat()
            self._tries[day] = self._tries.get(day, 0) + 1
            self._last_try = time.monotonic()
            try:
                blob = self.store.encrypt_bytes(self.snapshot())
            except Exception as exc:  # noqa: BLE001
                return self._failed(f"{exc.__class__.__name__}: {str(exc)[:120]}")
            name = f"niftysell-ledger-{now:%Y%m%d-%H%M}{SUFFIX}"
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                (self.dir / name).write_bytes(blob)
                for old in self._copies()[KEEP_LOCAL:]:
                    old.unlink(missing_ok=True)
                self._local_day = day
                self.ledger.kv_set(KV_LOCAL, now.isoformat(timespec="seconds"))
            except OSError as exc:
                logger.error("Local backup copy failed: %s", exc.__class__.__name__)
            ok, detail = self.notifier.send_document(
                name, blob, f"[Nifty paper] ledger backup {now:%Y-%m-%d %H:%M} IST. Encrypted: restore needs CREDENTIALS_KEY.")
            if not ok:
                return self._failed(f"not sent off-server ({detail})")
            self.ledger.kv_set(KV_OK, now.isoformat(timespec="seconds"))
            self.ledger.kv_set(KV_ERROR, "")
            return {"ok": True, "detail": "Backup sent", "file": name, "bytes": len(blob)}

    def _failed(self, detail: str) -> Dict[str, Any]:
        self.ledger.kv_set(KV_ERROR, detail)
        logger.warning("Ledger backup failed: %s", detail)
        return {"ok": False, "detail": detail}

    def _copies(self) -> List[Path]:
        return sorted(self.dir.glob(f"niftysell-ledger-*{SUFFIX}"), reverse=True) if self.dir.is_dir() else []

    # ------------------------------------------------------------------ status
    def status(self, now: datetime) -> Dict[str, Any]:
        ok_at = self.ledger.kv_get(KV_OK, "") or None
        error = self.ledger.kv_get(KV_ERROR, "") or None
        stale = False
        if self.notifier.configured:
            if ok_at:
                try:
                    stale = now - datetime.fromisoformat(ok_at) > STALE_AFTER
                except ValueError:
                    stale = True
            else:
                stale = error is not None          # a send was tried and never worked
        return {"offsite_configured": self.notifier.configured, "last_offsite_at": ok_at, "last_error": error,
                "last_local_at": self.ledger.kv_get(KV_LOCAL, "") or None, "local_copies": len(self._copies()),
                "stale": stale}


async def scheduler(app) -> None:
    import asyncio

    from starlette.concurrency import run_in_threadpool

    from api.server import now_ist

    svc: BackupService = app.state.backup
    while True:
        await asyncio.sleep(CHECK_SECONDS)
        try:
            now = now_ist()
            if svc.due(now):
                result = await run_in_threadpool(svc.run, now)
                logger.info("Scheduled ledger backup: ok=%s", result.get("ok"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Backup scheduler error: %s", exc.__class__.__name__)
