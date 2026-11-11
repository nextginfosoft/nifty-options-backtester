"""Automated Zerodha Kite Connect login (opt-in) and a stdlib TOTP generator.

IMPORTANT: Zerodha does not publish or officially support automating the web login
(``kite.zerodha.com/api/login`` and ``/api/twofa`` are the same endpoints the website
uses). This can stop working without notice, and the account holder is responsible for
complying with Zerodha's terms. Failed attempts count against the account, so callers
must keep attempts rare (see ``api/autologin.py``).

Secrets are only held in local variables here: they are never logged, and error
messages contain generic text and HTTP status codes only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests

LOGIN_URL = "https://kite.zerodha.com/api/login"
TWOFA_URL = "https://kite.zerodha.com/api/twofa"
CONNECT_URL = "https://kite.zerodha.com/connect/login"
TIMEOUT = 20
MAX_HOPS = 8


class LoginError(Exception):
    """Auto-login failed. ``fatal`` means retrying cannot help until credentials change."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def totp(secret_b32: str, now: Optional[float] = None, digits: int = 6, step: int = 30) -> str:
    """RFC 6238 TOTP (SHA-1), the scheme Zerodha's authenticator uses."""
    key = base64.b32decode(secret_b32.replace(" ", "").upper() + "=" * (-len(secret_b32.replace(" ", "")) % 8))
    counter = int((time.time() if now is None else now) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def valid_totp_secret(secret_b32: str) -> bool:
    try:
        totp(secret_b32)
        return True
    except (ValueError, TypeError):
        return False


def _json(resp: requests.Response, what: str) -> dict:
    try:
        return resp.json()
    except ValueError:
        raise LoginError(f"{what}: unexpected response (HTTP {resp.status_code})")


def login_for_request_token(user_id: str, password: str, totp_secret: str, api_key: str,
                            session: Optional[requests.Session] = None) -> str:
    """Run the web login + 2FA, then authorise the Connect app and return its request_token."""
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; nifty-dashboard)")

    r = s.post(LOGIN_URL, data={"user_id": user_id, "password": password}, timeout=TIMEOUT)
    body = _json(r, "Login")
    if body.get("status") != "success":
        raise LoginError(f"Zerodha rejected the user id or password (HTTP {r.status_code})", fatal=True)
    request_id = (body.get("data") or {}).get("request_id")
    if not request_id:
        raise LoginError("Zerodha login did not return a request id")

    r = s.post(TWOFA_URL, timeout=TIMEOUT, data={
        "user_id": user_id, "request_id": request_id, "twofa_value": totp(totp_secret),
        "twofa_type": "totp", "skip_session": "",
    })
    body = _json(r, "2FA")
    if body.get("status") != "success":
        # A wrong/expired code can be a transient clock issue, but repeated TOTP failures usually
        # mean the secret is wrong; treat as fatal so we do not hammer the account.
        raise LoginError(f"Zerodha rejected the TOTP code (HTTP {r.status_code}); check the TOTP secret and server clock",
                         fatal=True)

    url = f"{CONNECT_URL}?v=3&api_key={api_key}"
    for _ in range(MAX_HOPS):
        r = s.get(url, allow_redirects=False, timeout=TIMEOUT)
        location = r.headers.get("Location", "")
        if "request_token=" in location:
            token = parse_qs(urlparse(location).query).get("request_token", [""])[0]
            if token:
                return token
        if not location:
            break
        url = urljoin(url, location)
    raise LoginError(
        "Could not obtain a request token. If this is the first run, authorise the app once with Connect Kite, "
        "and check the app's redirect URL and API key",
    )
