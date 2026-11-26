"""Ledger backup (encrypted, sent off-server) and the liveness fields the outside uptime check reads. No network."""
import gzip
import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from api import notify
from api.backup import KEEP_LOCAL, SUFFIX, BackupService
from api.credentials import CredentialStore
from api.ledger import Ledger
from api.notify import Notifier

IST = timezone(timedelta(hours=5, minutes=30))
KEY = "k" * 40
H = {"X-Requested-With": "dashboard"}
REPO = Path(__file__).resolve().parent.parent


class _Resp:
    def __init__(self, status=200): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture()
def telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:SECRETTOKEN" + "x" * 20)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent = []

    def fake_open(req, timeout):
        sent.append((req.full_url, req.data))
        return _Resp(200)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_open)
    return sent


def make(tmp_path, key=KEY, production=False):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    store = CredentialStore(tmp_path, key, production, filename="telegram_alerts.enc")
    return ledger, BackupService(ledger, store, Notifier(store), tmp_path / "backups")


def at(day=26, hour=16, minute=5):
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def test_backup_is_encrypted_restorable_and_sent(tmp_path, telegram):
    ledger, svc = make(tmp_path)
    ledger.add_event("2026-11-26T11:10:00", "ENTRY", "ENTER", "marker-event-123", "AUTO-001")
    r = svc.run(at())
    assert r["ok"] is True and r["file"].endswith(SUFFIX)
    url, body = telegram[0]
    assert url.endswith("/sendDocument") and b"marker-event-123" not in body and b"SQLite format" not in body
    assert r["file"].encode() in body and b'name="chat_id"' in body
    local = next((tmp_path / "backups").glob("*" + SUFFIX))
    assert local.read_bytes() in body                      # the uploaded bytes are the stored encrypted copy

    spec = importlib.util.spec_from_file_location("restore", REPO / "scripts" / "restore_ledger_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "restored.sqlite3"
    assert mod.restore(local, out, KEY) == 0
    with sqlite3.connect(out) as c:
        assert c.execute("SELECT message FROM auto_events").fetchone()[0] == "marker-event-123"
    with pytest.raises(SystemExit):
        mod.restore(local, tmp_path / "other.sqlite3", "w" * 40)           # wrong key is refused
    with pytest.raises(SystemExit):
        mod.restore(local, out, KEY)                                       # never overwrites


def test_no_key_means_no_backup_rather_than_a_plain_one(tmp_path, telegram):
    ledger, svc = make(tmp_path, key=None, production=True)
    r = svc.run(at())
    assert r["ok"] is False and telegram == [] and not list((tmp_path / "backups").glob("*"))
    assert ledger.kv_get("backup.last_error")


def test_failed_send_is_recorded_and_never_leaks_the_token(tmp_path, monkeypatch, telegram):
    def boom(req, timeout):
        raise OSError(f"cannot reach {req.full_url}")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    ledger, svc = make(tmp_path)
    r = svc.run(at())
    assert r["ok"] is False and "SECRETTOKEN" not in str(r) and "SECRETTOKEN" not in str(svc.status(at()))
    assert svc.status(at())["stale"] is True and svc.status(at())["local_copies"] == 1      # still kept locally


def test_schedule_retries_a_failed_day_and_stops_after_success(tmp_path, telegram):
    ledger, svc = make(tmp_path)
    assert svc.due(at(hour=15, minute=59)) is False               # before the close
    assert svc.due(at()) is True
    assert svc.run(at())["ok"] is True
    assert svc.due(at(hour=18)) is False                          # done today
    assert svc.due(at(day=27)) is False                           # a retry gap of 30 minutes applies after any attempt
    svc._last_try -= 3600                                         # (an hour later)
    assert svc.due(at(day=27)) is True                            # new day, new backup


def test_failed_days_retry_every_30_minutes_up_to_six_times(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:SECRETTOKEN" + "x" * 20)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout: (_ for _ in ()).throw(OSError("down")))
    ledger, svc = make(tmp_path)
    for n in range(6):
        assert svc.due(at()) is True
        svc.run(at())
        assert svc.due(at()) is False                             # too soon
        svc._last_try -= 3600
    assert svc.due(at()) is False                                 # gave up for today; tomorrow is a fresh start


def test_without_telegram_one_local_copy_a_day_and_not_stale(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    ledger, svc = make(tmp_path)
    assert svc.due(at()) is True and svc.run(at())["ok"] is False
    assert svc.due(at(hour=17)) is False                          # no retry storm
    assert svc.status(at())["stale"] is False and svc.status(at())["offsite_configured"] is False


def test_only_the_newest_local_copies_are_kept(tmp_path, telegram):
    ledger, svc = make(tmp_path)
    for d in range(1, KEEP_LOCAL + 4):
        svc.run(at(day=d))
    names = sorted(p.name for p in (tmp_path / "backups").glob("*" + SUFFIX))
    assert len(names) == KEEP_LOCAL and "20261101" not in names[0]


def test_overdue_backup_is_stale_after_36_hours(tmp_path, telegram):
    ledger, svc = make(tmp_path)
    svc.run(at())
    assert svc.status(at() + timedelta(hours=35))["stale"] is False
    assert svc.status(at() + timedelta(hours=37))["stale"] is True


def test_snapshot_matches_the_live_ledger(tmp_path):
    ledger, svc = make(tmp_path)
    ledger.kv_set("a", "b")
    raw = gzip.decompress(svc.snapshot())
    p = tmp_path / "copy.db"
    p.write_bytes(raw)
    with sqlite3.connect(p) as c:
        assert c.execute("SELECT value FROM kv WHERE key='a'").fetchone()[0] == "b"


# ------------------------------------------------------------------ app wiring + liveness
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
        yield c


def test_backup_endpoints_need_login_and_report_status(client, monkeypatch):
    assert client.get("/api/auto/backup").json()["offsite_configured"] is False
    r = client.post("/api/auto/backup/run", headers=H).json()
    assert r["ok"] is False and "Telegram" in r["detail"] and r["local_copies"] == 1
    client.post("/api/auth/logout", headers=H)
    assert client.get("/api/auto/backup").status_code == 401


def test_health_reports_a_stalled_trader_loop(client):
    from api import server
    h = client.get("/api/health").json()
    assert h["ledger_ok"] is True and h["scheduler"]["running"] is False and h["scheduler"]["stalled"] is False
    app = client.app
    app.state.scheduler_running = True
    app.state.ledger.kv_set("auto.last_tick", (server.now_ist() - timedelta(minutes=2)).isoformat(timespec="seconds"))
    assert client.get("/api/health").json()["scheduler"]["stalled"] is False
    app.state.ledger.kv_set("auto.last_tick", (server.now_ist() - timedelta(minutes=20)).isoformat(timespec="seconds"))
    h = client.get("/api/health").json()
    assert h["scheduler"]["stalled"] is True and h["healthy"] is False and h["scheduler"]["auto_tick_age_s"] >= 1200
    assert "SECRET" not in str(h) and "trade" not in str(h).lower()


def test_outside_check_judges_health_json():
    spec = importlib.util.spec_from_file_location("chk", REPO / "scripts" / "check_live_health.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    good = {"status": "ok", "state": "ready", "healthy": True, "ledger_ok": True,
            "scheduler": {"running": True, "auto_tick_age_s": 30, "stalled": False}, "backup": {"stale": False}}
    assert mod.problems(good) == []
    assert any("stalled" in p for p in mod.problems({**good, "healthy": False, "scheduler": {"stalled": True, "auto_tick_age_s": 900}}))
    assert any("backup" in p for p in mod.problems({**good, "healthy": False, "backup": {"stale": True}}))
    assert any("loading" in p for p in mod.problems({**good, "state": "loading"}))
    assert any("old version" in p for p in mod.problems({"status": "ok", "state": "ready"}))
