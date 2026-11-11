"""Dashboard endpoints for saving Zerodha credentials (write-only) and running the auto-login.

Nothing here ever returns a stored secret. GET returns only "saved" flags and a masked user id.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api import autologin, broker_routes
from api.credentials import CredentialError, CredentialStore, FIELDS
from api.kite_login import valid_totp_secret

router = APIRouter(prefix="/broker", tags=["broker"])

_ENV_FOR = {"api_key": "KITE_API_KEY", "api_secret": "KITE_API_SECRET"}
_original_env: Dict[str, Optional[str]] = {}


def apply_to_environment(data: Dict[str, Any]) -> None:
    """Make the stored API key/secret visible to the Kite adapter (which reads the environment)."""
    for field, env in _ENV_FOR.items():
        if data.get(field):
            _original_env.setdefault(env, os.environ.get(env))
            os.environ[env] = data[field]


def restore_environment() -> None:
    for env, original in _original_env.items():
        if original is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = original
    _original_env.clear()


def _store(request: Request) -> CredentialStore:
    return request.app.state.creds


def _view(request: Request) -> Dict[str, Any]:
    view = _store(request).public_view()
    view["last_login"] = autologin.last_result()
    view["auto_login_attempts_today"] = autologin.state["attempts"]
    view["auto_login_blocked"] = autologin.state["blocked"]
    return view


class CredentialsIn(BaseModel):
    user_id: Optional[str] = Field(default=None, max_length=32)
    password: Optional[str] = Field(default=None, max_length=128)
    api_key: Optional[str] = Field(default=None, max_length=64)
    api_secret: Optional[str] = Field(default=None, max_length=128)
    totp_secret: Optional[str] = Field(default=None, max_length=128)
    auto_login: Optional[bool] = None
    accept_risk: bool = False


@router.get("/credentials")
async def get_credentials(request: Request) -> Dict[str, Any]:
    return _view(request)


@router.put("/credentials")
async def save_credentials(body: CredentialsIn, request: Request) -> Dict[str, Any]:
    store = _store(request)
    sensitive_given = any((getattr(body, f) or "").strip() for f in ("user_id", "password", "totp_secret"))
    if (sensitive_given or body.auto_login) and not body.accept_risk:
        raise HTTPException(status_code=400, detail="Tick the risk acknowledgement to store login credentials")
    if body.totp_secret and not valid_totp_secret(body.totp_secret.strip()):
        raise HTTPException(status_code=400, detail="TOTP secret must be the base32 key shown when you set up 2FA")
    try:
        existing = store.load()
        merged = {**existing, **{f: (getattr(body, f) or "").strip() for f in FIELDS if (getattr(body, f) or "").strip()}}
        if body.auto_login and not store.has_all_for_autologin(merged):
            raise HTTPException(status_code=400, detail="Auto-login needs all five credentials saved")
        data = store.update({f: getattr(body, f) for f in FIELDS}, body.auto_login)
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    apply_to_environment(data)
    autologin.clear_block()
    return _view(request)


@router.delete("/credentials")
async def delete_credentials(request: Request) -> Dict[str, Any]:
    """Remove stored credentials and today's Kite session from this server."""
    _store(request).delete()
    restore_environment()
    broker_routes.forget_token(request.app.state.settings.storage_dir)
    autologin.reset_state()
    return _view(request)


@router.post("/kite/auto-login")
async def run_auto_login(request: Request) -> Dict[str, Any]:
    """Run the login now (same flow the daily scheduler uses)."""
    return await autologin.run_login(request.app, automatic=False)
