"""Hosted dashboard API: auth, ledger P&L, CSRF guard, broker status. No network, no market data."""

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import auth
from api.app import create_app
from api.ledger import compute_pnl, max_drawdown_pct, net_credit_from_legs
from api.settings import Settings

H = {"X-Requested-With": "dashboard"}
LEGS = [
    {"action": "SELL", "strike": 24000, "option_type": "PE", "entry_price": 80.0},
    {"action": "BUY", "strike": 23800, "option_type": "PE", "entry_price": 30.0},
]


def make_settings(tmp: Path, **over) -> Settings:
    base = dict(app_env="development", password="pw-123456", session_secret="s" * 40,
                session_hours=12, cookie_secure=False, storage_dir=tmp, frontend_dist=None,
                enable_docs=False, lot_size=65, load_market_on_start=False)
    base.update(over)
    return Settings(**base)


@pytest.fixture()
def client(tmp_path):
    auth.reset_lockouts()
    with TestClient(create_app(make_settings(tmp_path), load_market=False)) as c:
        yield c


@pytest.fixture()
def authed(client):
    assert client.post("/api/auth/login", json={"password": "pw-123456"}).status_code == 200
    return client


# ------------------------------------------------------------------ auth
def test_health_is_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_protected_routes_require_login(client):
    for path in ("/api/status", "/api/journals", "/api/trades", "/api/signal", "/api/broker/status"):
        assert client.get(path).status_code == 401, path


def test_login_wrong_password_and_lockout(client):
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "pw-123456"}).status_code == 429


def test_login_sets_httponly_cookie_and_me(client):
    r = client.post("/api/auth/login", json={"password": "pw-123456"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/auth/me").json() == {"authenticated": True}


def test_logout_clears_session(authed):
    authed.post("/api/auth/logout", headers=H)
    assert authed.get("/api/journals").status_code == 401


def test_tampered_and_expired_tokens_rejected():
    secret = "k" * 40
    good = auth.make_token(secret, 1)
    assert auth.verify_token(secret, good)
    payload, sig = good.split(".")
    assert not auth.verify_token(secret, payload + "." + ("A" * len(sig)))
    assert not auth.verify_token("other" * 10, good)
    assert not auth.verify_token(secret, auth.make_token(secret, 1, now=time.time() - 7200))
    assert not auth.verify_token(secret, None) and not auth.verify_token(secret, "garbage")


def test_state_changing_requests_need_csrf_header(authed):
    body = {"journal_id": "J1", "label": "x", "initial_capital": 100000}
    assert authed.post("/api/journals", json=body).status_code == 403
    assert authed.post("/api/journals", json=body, headers=H).status_code == 200


def test_production_requires_secrets(monkeypatch):
    from api.settings import load_settings
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        load_settings()
    monkeypatch.setenv("DASHBOARD_PASSWORD", "x")
    monkeypatch.setenv("SESSION_SECRET", "short")
    with pytest.raises(RuntimeError, match="32"):
        load_settings()


# ------------------------------------------------------------------ pure ledger maths
def test_pnl_maths():
    assert net_credit_from_legs([dict(action=l["action"], entry_price=l["entry_price"]) for l in LEGS]) == 50.0
    assert compute_pnl(50, 20, 2, 65, 100) == {"gross_pnl": 3900.0, "net_pnl": 3800.0}
    assert compute_pnl(50, 80, 1, 65, 0)["net_pnl"] == -1950.0
    assert max_drawdown_pct(100, [10, -22, 5]) == 20.0  # peak 110 -> 88
    assert max_drawdown_pct(100, []) == 0.0


# ------------------------------------------------------------------ journals / trades
def _open(client, **over):
    body = {"journal_id": "J1", "strategy": "put_credit_spread", "entry_date": "2026-11-09",
            "expiry_date": "2026-11-17", "legs": LEGS, "lots": 2, "entry_vix": 17.4, **over}
    return client.post("/api/trades", json=body, headers=H)


def _journal(client, jid="J1", capital=100000):
    return client.post("/api/journals", json={"journal_id": jid, "label": "Phase 1", "initial_capital": capital},
                       headers=H)


def test_trade_lifecycle_and_summary(authed):
    assert _journal(authed, capital=2500000).status_code == 200
    r = _open(authed)
    assert r.status_code == 200
    t = r.json()
    assert t["trade_id"] == "J1-001" and t["entry_credit"] == 50.0 and t["lot_size"] == 65 and t["status"] == "open"

    assert authed.get("/api/trades?journal_id=J1&status=open").json()["count"] == 1
    assert authed.get("/api/journals/J1").json()["open_trades"] == 1

    c = authed.post("/api/trades/J1-001/close?journal_id=J1", headers=H,
                    json={"exit_debit": 20, "exit_reason": "profit_target", "exit_date": "2026-11-12",
                          "brokerage": 100})
    assert c.status_code == 200
    closed = c.json()
    assert closed["gross_pnl"] == 3900.0 and closed["net_pnl"] == 3800.0 and closed["days_held"] == 3

    s = authed.get("/api/journals/J1").json()
    assert (s["closed_trades"], s["open_trades"], s["total_pnl"], s["win_rate_pct"]) == (1, 0, 3800.0, 100.0)
    assert s["account_equity"] == 2503800.0

    again = authed.post("/api/trades/J1-001/close?journal_id=J1", headers=H,
                        json={"exit_debit": 10, "exit_reason": "manual"})
    assert again.status_code == 400  # already closed


def test_validation_and_not_found(authed):
    _journal(authed)
    assert _journal(authed).status_code == 400  # duplicate id
    bad_id = authed.post("/api/journals", json={"journal_id": "no spaces!", "label": "x", "initial_capital": 1},
                         headers=H)
    assert bad_id.status_code == 422
    assert _open(authed, journal_id="MISSING").status_code == 404
    assert _open(authed, legs=[]).status_code == 422
    assert _open(authed, lots=0).status_code == 422
    assert authed.get("/api/journals/NOPE").status_code == 404
    assert authed.post("/api/trades/X/close?journal_id=J1", headers=H,
                       json={"exit_debit": 1, "exit_reason": "manual"}).status_code == 404


def test_delete_trade_and_duplicate_id(authed):
    _journal(authed)
    assert _open(authed, trade_id="T1").status_code == 200
    assert _open(authed, trade_id="T1").status_code == 400
    assert authed.delete("/api/trades/T1?journal_id=J1", headers=H).status_code == 200
    assert authed.get("/api/trades").json()["count"] == 0


def test_daily_log_upsert_and_csv_exports(authed):
    _journal(authed)
    _open(authed)
    authed.post("/api/trades/J1-001/close?journal_id=J1", headers=H,
                json={"exit_debit": 20, "exit_reason": "profit_target", "exit_date": "2026-11-12"})
    for eq in (100000, 101000):
        r = authed.post("/api/journals/J1/daily-log", headers=H,
                        json={"log_date": "2026-11-12", "account_equity": eq, "cumulative_pnl": eq - 100000,
                              "open_trades": 0, "vix_close": 17.4})
        assert r.status_code == 200
    assert authed.get("/api/journals/J1/daily-log").json()["count"] == 1  # same date -> upsert

    csv_text = authed.get("/api/export/trades.csv?journal_id=J1").text
    header, row = csv_text.strip().splitlines()[:2]
    assert header.startswith("Trade_ID,Date_Entry,Entry_Time_IST,Strike_Entry")
    assert row.startswith("J1-001,2026-11-09") and ",W," in row and "24000PE" in row
    assert authed.get("/api/export/daily-log.csv?journal_id=J1").text.splitlines()[1].startswith(
        "2026-11-12,Thursday")


def test_status_without_market_data_reports_state(authed):
    from api import server
    saved = server._state["latest_row"]
    server._state["latest_row"] = None
    try:
        s = authed.get("/api/status").json()
    finally:
        server._state["latest_row"] = saved
    assert s["spot_now"] is None and s["data_state"] in ("idle", "loading", "ready", "degraded")
    assert s["label"] == "All journals"


def test_signal_and_monitor_503_while_data_unavailable(authed):
    from api import server
    saved = server._state["latest_row"]
    server._state["latest_row"] = None
    try:
        assert authed.get("/api/signal").status_code == 503
        assert authed.get("/api/monitor").status_code == 503
    finally:
        server._state["latest_row"] = saved


# ------------------------------------------------------------------ broker
def test_broker_status_unconfigured(authed, monkeypatch):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    assert authed.get("/api/broker/status").json() == {
        "broker": "none", "configured": False, "connected": False, "token_expires_at": None}
    assert authed.get("/api/broker/kite/login-url").status_code == 400
    assert authed.get("/api/broker/live").json()["ok"] is False


def test_kite_callback_requires_login_and_failure_redirects(client, authed):
    anon = TestClient(client.app)
    assert anon.get("/api/broker/kite/callback?request_token=abc&status=success").status_code == 401
    r = authed.get("/api/broker/kite/callback?status=failure", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?broker=error"


def test_kite_token_exchange_persists_and_restores(authed, monkeypatch):
    from api import broker_routes
    monkeypatch.setenv("KITE_API_KEY", "key")
    monkeypatch.setenv("KITE_API_SECRET", "secret")
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(broker_routes, "_exchange", lambda rt: "tok-" + rt)
    r = authed.post("/api/broker/kite/token", json={"request_token": "abcdefgh"}, headers=H)
    assert r.status_code == 200 and r.json()["connected"] is True
    assert os.environ["KITE_ACCESS_TOKEN"] == "tok-abcdefgh"
    status_text = authed.get("/api/broker/status").text
    assert "tok-" not in status_text  # the token is never returned to the browser
    assert authed.get("/api/broker/status").json()["connected"] is True
    monkeypatch.delenv("KITE_ACCESS_TOKEN")
    assert broker_routes.load_saved_token(authed.app.state.settings.storage_dir) is True


# ------------------------------------------------------------------ static / SPA
def test_spa_served_with_traversal_guard(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "a.js").write_text("x=1")
    (tmp_path / "secret.txt").write_text("nope")
    s = make_settings(tmp_path / "store", frontend_dist=dist)
    with TestClient(create_app(s, load_market=False)) as c:
        assert c.get("/").text == "<html>app</html>"
        assert c.get("/some/spa/route").text == "<html>app</html>"
        assert c.get("/assets/a.js").text == "x=1"
        assert c.get("/api/nope").status_code == 404
        assert "nope" not in c.get("/..%2Fsecret.txt").text
        h = c.get("/").headers
        assert h["x-frame-options"] == "DENY" and "default-src 'self'" in h["content-security-policy"]


# ------------------------------------------------------------------ exit model regression
def test_exit_model_load_restores_feature_columns():
    """Seed caches predate exit_feature_cols; load() must recover it from the LightGBM model."""
    import pandas as pd
    from models.trade_monitor import ExitStrategyEngine

    seed = Path(__file__).resolve().parent.parent / "data" / ".cache_seed" / "exit_model.pkl"
    if not seed.is_file():
        pytest.skip("seed exit model not present")
    engine = ExitStrategyEngine.load(pd.DataFrame(), path=seed)
    assert engine.is_trained and len(engine.exit_feature_cols) == engine.exit_classifier.n_features_in_
