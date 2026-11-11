"""Single-user session auth for the dashboard.

Login exchanges the shared password for an HMAC-signed, expiring cookie. Every
API route except /api/health and /api/auth/* requires that cookie. Browser
state-changing requests must also send ``X-Requested-With: dashboard`` (a simple
CSRF guard on top of SameSite=Lax cookies).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

COOKIE_NAME = "session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "dashboard"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60

_failures: Dict[str, Deque[float]] = defaultdict(deque)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, payload: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def make_token(secret: str, hours: float, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    payload = _b64(json.dumps({"iat": int(now), "exp": int(now + hours * 3600)}).encode())
    return f"{payload}.{_sign(secret, payload)}"


def verify_token(secret: str, token: Optional[str], now: Optional[float] = None) -> bool:
    if not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return False
    try:
        exp = json.loads(_unb64(payload))["exp"]
    except (ValueError, KeyError, TypeError):
        return False
    return (time.time() if now is None else now) < exp


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_lockout(ip: str) -> None:
    window = _failures[ip]
    cutoff = time.time() - LOCKOUT_SECONDS
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= MAX_FAILURES:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 15 minutes.")


def require_auth(request: Request) -> None:
    """Dependency applied to every protected route."""
    settings = request.app.state.settings
    if not verify_token(settings.session_secret, request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403, detail="Missing CSRF header")


router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    settings = request.app.state.settings
    ip = _client_ip(request)
    _check_lockout(ip)
    if not hmac.compare_digest(body.password.encode(), settings.password.encode()):
        _failures[ip].append(time.time())
        raise HTTPException(status_code=401, detail="Incorrect password")
    _failures.pop(ip, None)
    response.set_cookie(
        COOKIE_NAME,
        make_token(settings.session_secret, settings.session_hours),
        max_age=int(settings.session_hours * 3600),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"authenticated": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"authenticated": False}


@router.get("/me")
async def me(request: Request):
    settings = request.app.state.settings
    ok = verify_token(settings.session_secret, request.cookies.get(COOKIE_NAME))
    return {"authenticated": ok}


def reset_lockouts() -> None:
    """Test helper."""
    _failures.clear()


__all__ = ["router", "require_auth", "make_token", "verify_token", "reset_lockouts", "Depends"]
