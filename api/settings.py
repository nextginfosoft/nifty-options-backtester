"""Runtime configuration for the hosted dashboard API, read from environment variables.

Secrets (DASHBOARD_PASSWORD, SESSION_SECRET, KITE_*) only ever come from the
environment on the server; none is sent to the browser or written to logs.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    app_env: str                 # "development" | "production"
    password: str                # single-user dashboard password
    session_secret: str          # HMAC key for the session cookie
    session_hours: float
    cookie_secure: bool
    storage_dir: Path            # ledger DB, Kite token file
    frontend_dist: Optional[Path]
    enable_docs: bool
    lot_size: int
    load_market_on_start: bool
    credentials_key: Optional[str] = None   # encrypts stored Zerodha credentials

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def load_settings() -> Settings:
    app_env = (os.getenv("APP_ENV") or "development").strip().lower()
    production = app_env == "production"

    password = os.getenv("DASHBOARD_PASSWORD") or ""
    secret = os.getenv("SESSION_SECRET") or ""
    if production:
        missing = [n for n, v in (("DASHBOARD_PASSWORD", password), ("SESSION_SECRET", secret)) if not v]
        if missing:
            raise RuntimeError(f"APP_ENV=production requires {', '.join(missing)} to be set")
        if len(secret) < 32:
            raise RuntimeError("SESSION_SECRET must be at least 32 characters")
    else:
        if not password:
            password = "dev-password"
            logger.warning("DASHBOARD_PASSWORD not set: using the development password")
        if not secret:
            secret = secrets.token_urlsafe(32)
            logger.warning("SESSION_SECRET not set: using a random one (sessions reset on restart)")

    storage = Path(os.getenv("STORAGE_DIR") or (ROOT / "data" / ".cache"))
    dist_env = os.getenv("FRONTEND_DIST")
    dist = Path(dist_env) if dist_env else ROOT / "frontend" / "dist"

    return Settings(
        app_env=app_env,
        password=password,
        session_secret=secret,
        session_hours=float(os.getenv("SESSION_HOURS") or 12),
        cookie_secure=_flag("COOKIE_SECURE", production),
        storage_dir=storage,
        frontend_dist=dist if (dist / "index.html").is_file() else None,
        enable_docs=_flag("ENABLE_DOCS", not production),
        lot_size=int(os.getenv("LOT_SIZE") or 65),
        load_market_on_start=_flag("LOAD_MARKET_ON_START", True),
        credentials_key=(os.getenv("CREDENTIALS_KEY") or None),
    )
