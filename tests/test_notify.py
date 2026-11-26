"""Alert policy and delivery (Telegram). No network: the HTTP call is replaced."""
import json

import pytest

from api import notify
from api.ledger import Ledger
from api.notify import Notifier


def test_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    n = Notifier()
    assert n.configured is False and n.send("x")[0] is False
    n.notify_event("2026-11-17T11:10:00", "ENTRY", "ENTER", "m")          # must be a silent no-op
    assert n.last_ok is None


def test_policy_sends_trades_and_throttles_noise():
    n = Notifier()
    assert n.wants("ENTRY", "ENTER") and n.wants("EXIT", "STOP_LOSS") and n.wants("PAUSE", "DRAWDOWN")
    assert n.wants("ERROR", "NO_QUOTES", now=1000.0) is True
    assert n.wants("ERROR", "NO_QUOTES", now=1000.0 + 60) is False         # repeats are held back
    assert n.wants("ERROR", "EXCEPTION", now=1000.0 + 60) is True           # a different problem still gets through
    assert n.wants("ERROR", "NO_QUOTES", now=1000.0 + 31 * 60) is True
    assert n.wants("INFO", "LOTS") is False and n.wants("INFO", "DISABLED") is True
    assert n.wants("SKIP", "VIX_TOO_HIGH", now=5.0) is True and n.wants("SKIP", "VIX_TOO_HIGH", now=6.0) is False


class _Resp:
    def __init__(self, status): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_send_posts_to_telegram_and_never_leaks_the_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRETTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    seen = {}

    def fake_open(req, timeout):
        seen["url"], seen["body"] = req.full_url, req.data
        return _Resp(200)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_open)
    n = Notifier()
    ok, detail = n.send("hello")
    assert ok and "SECRETTOKEN" not in detail and seen["url"].startswith("https://api.telegram.org/bot123:SECRETTOKEN/")
    assert b'"chat_id": "42"' in seen["body"]

    def boom(req, timeout):
        raise OSError(f"cannot reach {req.full_url}")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    ok, detail = n.send("hello")
    assert ok is False and "SECRETTOKEN" not in detail and "SECRETTOKEN" not in str(n.status())


def test_ledger_hook_fires_and_cannot_break_the_write(tmp_path):
    lg = Ledger(tmp_path / "l.db")
    got = []
    lg.on_event = lambda *a: got.append(a)
    lg.add_event("2026-11-17T11:10:00", "EXIT", "STOP_LOSS", "closed", "AUTO-001")
    assert got and got[0][1:4] == ("EXIT", "STOP_LOSS", "closed")

    def bad(*a):
        raise RuntimeError("telegram down")

    lg.on_event = bad
    lg.add_event("2026-11-17T11:11:00", "INFO", "X", "still stored")
    assert [e["code"] for e in lg.list_events(5)][0] == "X"


# ------------------------------------------------------------------ dashboard-saved settings
TOKEN = "123456789:AAH-" + "x" * 30
H = {"X-Requested-With": "dashboard"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api import auth
    from api.app import create_app
    from tests.test_autotrader import make_settings
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    auth.reset_lockouts()
    with TestClient(create_app(make_settings(tmp_path), load_market=False)) as c:
        assert c.post("/api/auth/login", json={"password": "pw-123456"}).status_code == 200
        c.tmp = tmp_path
        yield c


def test_settings_are_saved_encrypted_write_only_and_used(client, monkeypatch):
    c = client
    assert c.get("/api/auto/alerts").json()["configured"] is False
    assert c.put("/api/auto/alerts", json={"bot_token": "nonsense"}, headers=H).status_code == 400
    assert c.put("/api/auto/alerts", json={"chat_id": "abc"}, headers=H).status_code == 400
    assert c.put("/api/auto/alerts", json={}, headers=H).status_code == 400
    r = c.put("/api/auto/alerts", json={"bot_token": TOKEN, "chat_id": "987654321"}, headers=H).json()
    assert r["configured"] is True and r["source"] == "dashboard" and r["saved"] == {"bot_token": True, "chat_id": True}
    assert r["chat_id_masked"].endswith("321") and TOKEN not in str(r) and "987654321" not in str(r)
    assert TOKEN not in c.get("/api/auto/alerts").text
    assert TOKEN.encode() not in (c.tmp / "telegram_alerts.enc").read_bytes()          # encrypted at rest
    seen = {}

    def fake_open(req, timeout):
        seen["url"], seen["body"] = req.full_url, req.data
        return _Resp(200)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_open)
    assert c.post("/api/auto/alerts/test", headers=H).json()["ok"] is True
    assert TOKEN in seen["url"] and b"987654321" in seen["body"]
    r = c.put("/api/auto/alerts", json={"chat_id": "555666777"}, headers=H).json()          # blank token keeps the saved one
    assert r["saved"]["bot_token"] is True and r["chat_id_masked"].endswith("777")
    r = c.delete("/api/auto/alerts", headers=H).json()
    assert r["configured"] is False and not (c.tmp / "telegram_alerts.enc").exists()


def test_detect_chat_id_saves_the_chat_that_messaged_the_bot(client, monkeypatch):
    c = client
    r = c.post("/api/auto/alerts/detect-chat", headers=H).json()
    assert r["ok"] is False and "token" in r["detail"].lower()
    c.put("/api/auto/alerts", json={"bot_token": TOKEN}, headers=H)

    def answer(payload):
        class R(_Resp):
            def read(self):
                return json.dumps(payload).encode()
        return lambda req, timeout: R(200)

    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        answer({"ok": True, "result": [{"message": {"chat": {"id": 424242, "type": "private"}}}]}))
    r = c.post("/api/auto/alerts/detect-chat", headers=H).json()
    assert r["ok"] is True and r["configured"] is True and r["chat_id_masked"].endswith("242")
    c.delete("/api/auto/alerts", headers=H)
    c.put("/api/auto/alerts", json={"bot_token": TOKEN}, headers=H)
    monkeypatch.setattr(notify.urllib.request, "urlopen", answer({"ok": True, "result": []}))
    r = c.post("/api/auto/alerts/detect-chat", headers=H).json()
    assert r["ok"] is False and "Start" in r["detail"]
