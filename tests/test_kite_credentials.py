"""Zerodha credential storage, TOTP, auto-login flow and scheduler limits.
No network: Zerodha is mocked, and every credential value below is an obvious fake."""

import asyncio
import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import auth, autologin, broker_routes, credentials_routes
from api.app import create_app
from api.credentials import CredentialError, CredentialStore, mask_user_id
from api.kite_login import LoginError, login_for_request_token, totp, valid_totp_secret
from api.settings import Settings

H = {"X-Requested-With": "dashboard"}
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890" (RFC 6238 test key)
FAKE = {"user_id": "ZZ9999", "password": "fake-pass-not-real", "api_key": "fakekey123",
        "api_secret": "fakesecret456", "totp_secret": RFC_SECRET}
KEY = "k" * 40


def make_settings(tmp: Path, **over) -> Settings:
    base = dict(app_env="development", password="pw-123456", session_secret="s" * 40, session_hours=12,
                cookie_secure=False, storage_dir=tmp, frontend_dist=None, enable_docs=False, lot_size=65,
                load_market_on_start=False, credentials_key=KEY)
    base.update(over)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for n in ("KITE_API_KEY", "KITE_API_SECRET", "KITE_ACCESS_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    autologin.reset_state()
    auth.reset_lockouts()
    yield
    credentials_routes.restore_environment()
    autologin.reset_state()


@pytest.fixture()
def authed(tmp_path):
    with TestClient(create_app(make_settings(tmp_path), load_market=False)) as c:
        assert c.post("/api/auth/login", json={"password": "pw-123456"}).status_code == 200
        yield c


def put(c, **body):
    return c.put("/api/broker/credentials", json=body, headers=H)


# ------------------------------------------------------------------ TOTP (RFC 6238 vectors)
def test_totp_matches_rfc6238_vectors():
    assert totp(RFC_SECRET, now=59, digits=8) == "94287082"
    assert totp(RFC_SECRET, now=1111111109, digits=8) == "07081804"
    assert totp(RFC_SECRET, now=59) == "287082"
    spaced = " ".join(RFC_SECRET[i:i + 4] for i in range(0, len(RFC_SECRET), 4)).lower()
    assert totp(spaced, now=59) == "287082"  # authenticator apps show spaced, lower-case keys
    assert valid_totp_secret(RFC_SECRET) and not valid_totp_secret("not base32 !!")


# ------------------------------------------------------------------ encrypted store
def test_store_encrypts_at_rest_and_masks(tmp_path):
    store = CredentialStore(tmp_path, KEY, production=True)
    store.update(FAKE, auto_login=True)
    raw = (tmp_path / "kite_credentials.enc").read_bytes()
    for secret in ("fake-pass-not-real", "fakesecret456", RFC_SECRET, "ZZ9999"):
        assert secret.encode() not in raw
    assert store.load()["password"] == "fake-pass-not-real"
    assert CredentialStore(tmp_path, "z" * 40, production=True).public_view()["error"]  # wrong key cannot read
    view = store.public_view()
    assert view["saved"] == {k: True for k in FAKE} and view["user_id_masked"] == "ZZ****"
    assert view["auto_login_enabled"] is True
    assert not any(v in str(view) for v in ("fake-pass-not-real", "fakesecret456", RFC_SECRET, "ZZ9999"))


def test_blank_values_keep_existing_and_delete(tmp_path):
    store = CredentialStore(tmp_path, KEY, production=True)
    store.update(FAKE, None)
    store.update({"api_key": "newkey999", "password": "  "}, None)
    d = store.load()
    assert d["api_key"] == "newkey999" and d["password"] == "fake-pass-not-real"
    store.delete()
    assert store.load() == {}


def test_production_without_key_refuses_to_store(tmp_path):
    store = CredentialStore(tmp_path, None, production=True)
    assert store.encryption_available is False
    with pytest.raises(CredentialError):
        store.update(FAKE, None)
    assert not (tmp_path / ".credentials.key").exists()


def test_dev_without_key_generates_local_key(tmp_path):
    store = CredentialStore(tmp_path, None, production=False)
    store.update({"api_key": "k1"}, None)
    assert (tmp_path / ".credentials.key").is_file()
    assert CredentialStore(tmp_path, None, production=False).load()["api_key"] == "k1"


def test_short_key_rejected(tmp_path):
    with pytest.raises(CredentialError):
        CredentialStore(tmp_path, "short", production=True).update({"api_key": "x"}, None)


def test_mask_user_id():
    assert mask_user_id("AB1234") == "AB****" and mask_user_id("") == ""


# ------------------------------------------------------------------ HTTP routes
def test_routes_require_login(tmp_path):
    with TestClient(create_app(make_settings(tmp_path), load_market=False)) as c:
        assert c.get("/api/broker/credentials").status_code == 401
        assert c.put("/api/broker/credentials", json={}, headers=H).status_code == 401
        assert c.post("/api/broker/kite/auto-login", headers=H).status_code == 401


def test_api_key_and_secret_need_no_risk_ack_and_set_env(authed):
    r = put(authed, api_key="fakekey123", api_secret="fakesecret456")
    assert r.status_code == 200
    assert r.json()["saved"]["api_key"] and not r.json()["saved"]["password"]
    assert os.environ["KITE_API_KEY"] == "fakekey123"
    assert authed.get("/api/broker/status").json()["configured"] is True


def test_login_credentials_require_risk_ack_and_valid_totp(authed):
    assert put(authed, password="x").status_code == 400
    assert put(authed, user_id="ZZ9999", totp_secret=RFC_SECRET).status_code == 400
    assert put(authed, totp_secret="***bad***", accept_risk=True).status_code == 400
    assert put(authed, auto_login=True, accept_risk=True).status_code == 400  # not all five saved


def test_full_save_never_echoes_secrets(authed):
    r = put(authed, **FAKE, auto_login=True, accept_risk=True)
    assert r.status_code == 200 and r.json()["auto_login_enabled"] is True
    for resp in (r, authed.get("/api/broker/credentials"), authed.get("/api/broker/status")):
        text = resp.text
        for secret in ("fake-pass-not-real", "fakesecret456", RFC_SECRET, "ZZ9999", "fakekey123"):
            assert secret not in text


def test_delete_removes_everything_and_restores_env(authed, monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "from-server-env")
    put(authed, **FAKE, auto_login=True, accept_risk=True)
    assert os.environ["KITE_API_KEY"] == "fakekey123"
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "tok")
    r = authed.delete("/api/broker/credentials", headers=H)
    assert r.status_code == 200 and not any(r.json()["saved"].values())
    assert os.environ["KITE_API_KEY"] == "from-server-env"
    assert "KITE_ACCESS_TOKEN" not in os.environ


# ------------------------------------------------------------------ login flow (mocked Zerodha)
class FakeResp:
    def __init__(self, data=None, status=200, location=None):
        self._data, self.status_code = data, status
        self.headers = {"Location": location} if location else {}

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeSession:
    def __init__(self, posts, gets):
        self.headers, self.posts, self.gets, self.calls = {}, list(posts), list(gets), []

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, dict(data or {})))
        return self.posts.pop(0)

    def get(self, url, allow_redirects=True, timeout=None):
        assert allow_redirects is False  # we must never follow into the app's redirect URL
        self.calls.append(("GET", url, {}))
        return self.gets.pop(0)


OK_LOGIN = FakeResp({"status": "success", "data": {"request_id": "rid1"}})
OK_2FA = FakeResp({"status": "success"})


def test_login_flow_returns_request_token_and_sends_totp():
    s = FakeSession([OK_LOGIN, OK_2FA], [
        FakeResp(status=302, location="/connect/finish?sess_id=abc"),
        FakeResp(status=302, location="https://example.test/cb?request_token=REQTOK123&action=login&status=success"),
    ])
    assert login_for_request_token("ZZ9999", "pw", RFC_SECRET, "fakekey123", session=s) == "REQTOK123"
    (_, login_url, login_data), (_, twofa_url, twofa_data) = s.calls[0], s.calls[1]
    assert login_url.endswith("/api/login") and login_data == {"user_id": "ZZ9999", "password": "pw"}
    assert twofa_url.endswith("/api/twofa") and twofa_data["request_id"] == "rid1"
    assert twofa_data["twofa_type"] == "totp" and len(twofa_data["twofa_value"]) == 6
    assert "api_key=fakekey123" in s.calls[2][1]


def test_login_rejections_are_fatal_and_message_has_no_secrets():
    with pytest.raises(LoginError) as e1:
        login_for_request_token("ZZ9999", "hunter2-secret", RFC_SECRET, "k",
                                session=FakeSession([FakeResp({"status": "error"})], []))
    assert e1.value.fatal and "hunter2-secret" not in str(e1.value) and "ZZ9999" not in str(e1.value)
    with pytest.raises(LoginError) as e2:
        login_for_request_token("ZZ9999", "pw", RFC_SECRET, "k",
                                session=FakeSession([OK_LOGIN, FakeResp({"status": "error"})], []))
    assert e2.value.fatal


def test_login_without_token_is_not_fatal():
    s = FakeSession([OK_LOGIN, OK_2FA], [FakeResp(status=200)])
    with pytest.raises(LoginError) as e:
        login_for_request_token("ZZ9999", "pw", RFC_SECRET, "k", session=s)
    assert not e.value.fatal and "first run" in str(e.value)


# ------------------------------------------------------------------ run_login + scheduler rules
def _app(tmp_path):
    return create_app(make_settings(tmp_path), load_market=False)


def test_run_login_success_saves_token_without_leaking(tmp_path, monkeypatch):
    app = _app(tmp_path)
    app.state.creds.update(FAKE, True)
    monkeypatch.setattr("api.autologin.login_for_request_token", lambda *a, **k: "reqtok")
    monkeypatch.setattr(broker_routes, "_exchange", lambda rt: "access-" + rt)
    r = asyncio.run(autologin.run_login(app, automatic=False))
    assert r["ok"] and os.environ["KITE_ACCESS_TOKEN"] == "access-reqtok"
    assert broker_routes.load_saved_token(tmp_path)
    assert "access-" not in str(r)


def test_fatal_failure_blocks_automatic_retries(tmp_path, monkeypatch):
    app = _app(tmp_path)
    app.state.creds.update(FAKE, True)

    def boom(*a, **k):
        raise LoginError("Zerodha rejected the user id or password (HTTP 200)", fatal=True)

    monkeypatch.setattr("api.autologin.login_for_request_token", boom)
    r = asyncio.run(autologin.run_login(app, automatic=True))
    assert not r["ok"] and autologin.state["blocked"]
    at_0700 = datetime(2026, 11, 12, 7, 0, tzinfo=broker_routes.IST)
    autologin.state["last_at"] = 0
    assert autologin.is_due(app, at_0700) is False  # blocked until credentials change
    autologin.clear_block()
    assert autologin.is_due(app, at_0700) is True


def test_is_due_rules(tmp_path):
    app = _app(tmp_path)
    at = lambda h, m=0: datetime(2026, 11, 12, h, m, tzinfo=broker_routes.IST)  # noqa: E731
    assert autologin.is_due(app, at(7)) is False  # nothing saved
    app.state.creds.update(FAKE, False)
    assert autologin.is_due(app, at(7)) is False  # auto-login not enabled
    app.state.creds.update({}, True)
    assert autologin.is_due(app, at(6, 29)) is False and autologin.is_due(app, at(6, 30)) is True
    autologin.state["attempts"] = autologin.MAX_AUTO_ATTEMPTS_PER_DAY
    assert autologin.is_due(app, at(7)) is False  # daily cap
    autologin.state["attempts"] = 0
    broker_routes._save_token(tmp_path, "already")
    assert autologin.is_due(app, at(7)) is False  # already connected today


def test_manual_attempts_have_a_cooldown(tmp_path, monkeypatch):
    app = _app(tmp_path)
    app.state.creds.update(FAKE, True)
    monkeypatch.setattr("api.autologin.login_for_request_token", lambda *a, **k: "t")
    monkeypatch.setattr(broker_routes, "_exchange", lambda rt: "acc")
    assert asyncio.run(autologin.run_login(app, automatic=False))["ok"]
    again = asyncio.run(autologin.run_login(app, automatic=False))
    assert not again["ok"] and "wait" in again["message"].lower()


def test_auto_login_endpoint_reports_missing_credentials(authed):
    r = authed.post("/api/broker/kite/auto-login", headers=H)
    assert r.status_code == 200 and r.json()["ok"] is False and "five" in r.json()["message"]
