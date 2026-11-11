"""Encrypted-at-rest storage for Zerodha credentials entered through the dashboard.

What is stored: Kite Connect API key and secret, and (only if the owner opts in to
automatic daily login) the Zerodha user id, login password and TOTP secret.

Safeguards:
  * Fernet (AES-128-CBC + HMAC) encryption; the key comes from CREDENTIALS_KEY in the
    server environment, so the encrypted file alone (e.g. a stolen volume backup) is useless.
  * In production the feature is OFF unless CREDENTIALS_KEY is set. In development a
    random key file is generated next to the data so the form can be tried locally.
  * Values are write-only: nothing here is ever returned to the browser except
    "is saved" flags and a masked user id. Nothing is logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

FIELDS = ("user_id", "password", "api_key", "api_secret", "totp_secret")
SENSITIVE_FOR_AUTOLOGIN = ("user_id", "password", "totp_secret")
_SALT = b"nifty-dashboard-credentials-v1"


class CredentialError(ValueError):
    pass


def _derive(passphrase: str) -> bytes:
    raw = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), _SALT, 200_000, dklen=32)
    return base64.urlsafe_b64encode(raw)


def mask_user_id(user_id: str) -> str:
    return user_id[:2] + "*" * max(len(user_id) - 2, 0) if user_id else ""


class CredentialStore:
    def __init__(self, storage_dir: Path, key: Optional[str], production: bool):
        self._dir = Path(storage_dir)
        self._file = self._dir / "kite_credentials.enc"
        self._key_file = self._dir / ".credentials.key"
        self._production = production
        self._key = key
        self._fernet: Optional[Fernet] = None

    # ------------------------------------------------------------------ key handling
    def _get_fernet(self, create: bool) -> Optional[Fernet]:
        if self._fernet:
            return self._fernet
        if self._key:
            if len(self._key) < 32:
                raise CredentialError("CREDENTIALS_KEY must be at least 32 characters")
            self._fernet = Fernet(_derive(self._key))
        elif not self._production:
            # Dev convenience only: key file lives beside the data, so this is NOT at-rest protection.
            if self._key_file.is_file():
                self._fernet = Fernet(_derive(self._key_file.read_text().strip()))
            elif create:
                self._dir.mkdir(parents=True, exist_ok=True)
                self._key_file.write_text(secrets.token_hex(32))
                try:
                    self._key_file.chmod(0o600)
                except OSError:
                    pass
                self._fernet = Fernet(_derive(self._key_file.read_text().strip()))
        return self._fernet

    @property
    def encryption_available(self) -> bool:
        return bool(self._key) or not self._production

    # ------------------------------------------------------------------ persistence
    def load(self) -> Dict[str, Any]:
        """Decrypted record: field values plus ``auto_login`` and ``updated_at``. Empty if none."""
        if not self._file.is_file():
            return {}
        f = self._get_fernet(create=False)
        if f is None:
            return {}
        try:
            data = json.loads(f.decrypt(self._file.read_bytes()))
        except (InvalidToken, ValueError):
            raise CredentialError("Stored credentials cannot be decrypted (was CREDENTIALS_KEY changed?)")
        return data if isinstance(data, dict) else {}

    def _write(self, data: Dict[str, Any]) -> None:
        f = self._get_fernet(create=True)
        if f is None:
            raise CredentialError("Set CREDENTIALS_KEY on the server to store credentials")
        self._dir.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._file.write_bytes(f.encrypt(json.dumps(data).encode()))
        try:
            self._file.chmod(0o600)
        except OSError:
            pass

    def update(self, updates: Dict[str, Optional[str]], auto_login: Optional[bool]) -> Dict[str, Any]:
        """Merge non-empty values over the stored record (blank = keep the existing value)."""
        if not self.encryption_available:
            raise CredentialError("Set CREDENTIALS_KEY on the server to store credentials")
        data = self.load()
        for name in FIELDS:
            value = (updates.get(name) or "").strip()
            if value:
                data[name] = value
        if auto_login is not None:
            data["auto_login"] = bool(auto_login)
        self._write(data)
        return data

    def delete(self) -> None:
        for f in (self._file,):
            try:
                f.unlink()
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------ views
    def has_all_for_autologin(self, data: Optional[Dict[str, Any]] = None) -> bool:
        d = self.load() if data is None else data
        return all(d.get(k) for k in FIELDS)

    def public_view(self) -> Dict[str, Any]:
        """Safe-to-return summary: flags and a masked user id only."""
        try:
            d = self.load()
            error = None
        except CredentialError as exc:
            d, error = {}, str(exc)
        return {
            "encryption_available": self.encryption_available,
            "key_source": "environment" if self._key else ("dev key file" if not self._production else "missing"),
            "saved": {name: bool(d.get(name)) for name in FIELDS},
            "user_id_masked": mask_user_id(d.get("user_id", "")),
            "auto_login_enabled": bool(d.get("auto_login")) and self.has_all_for_autologin(d),
            "updated_at": d.get("updated_at"),
            "error": error,
        }
