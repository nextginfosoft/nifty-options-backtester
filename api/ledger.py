"""SQLite paper-trading ledger owned by the API server.

Replaces the old CSV-append flow (whose schema did not match the tracked
TRADES.csv, and whose trades never showed up in listings). One DB file lives in
STORAGE_DIR. A CSV export in the repo's TRADES.csv / DAILY_LOG.csv column order
is provided so the manual-CSV workflow keeps working.

P&L model (credit structures): gross = (entry_credit - exit_debit) per unit
* lot_size * lots, where entry_credit / exit_debit are the NET per-unit prices of
the whole structure; net = gross - brokerage.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS journals (
    journal_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    strategy_track TEXT NOT NULL DEFAULT 'weekly',
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    journal_id TEXT NOT NULL REFERENCES journals(journal_id),
    trade_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    entry_time_ist TEXT NOT NULL DEFAULT '',
    expiry_date TEXT NOT NULL,
    legs_json TEXT NOT NULL,
    lots INTEGER NOT NULL,
    lot_size INTEGER NOT NULL,
    entry_credit REAL NOT NULL,
    entry_vix REAL,
    entry_spot REAL,
    regime TEXT NOT NULL DEFAULT '',
    ml_score REAL,
    capital_deployed REAL NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    exit_date TEXT,
    exit_time_ist TEXT,
    exit_debit REAL,
    exit_reason TEXT,
    brokerage REAL,
    gross_pnl REAL,
    net_pnl REAL,
    days_held INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (journal_id, trade_id)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    trade_id TEXT,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_auto_events_day ON auto_events(day, id);
CREATE TABLE IF NOT EXISTS daily_log (
    journal_id TEXT NOT NULL REFERENCES journals(journal_id),
    log_date TEXT NOT NULL,
    account_equity REAL NOT NULL,
    cumulative_pnl REAL NOT NULL,
    open_trades INTEGER NOT NULL,
    vix_close REAL,
    regime TEXT NOT NULL DEFAULT '',
    account_dd_pct REAL NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (journal_id, log_date)
);
"""

TRADES_CSV_COLUMNS = [
    "Trade_ID", "Date_Entry", "Entry_Time_IST", "Strike_Entry", "Bid_Ask_Entry", "Entry_Price_Fill",
    "Lots_Size", "Capital_Deployed_₹", "Strategy", "Signal_VIX", "Signal_Regime", "ML_Score", "Expiry_DTE",
    "Date_Exit", "Exit_Time_IST", "Exit_Price_Fill", "Exit_Reason", "Days_Held", "Gross_P&L_₹",
    "Brokerage_₹", "Slippage_vs_Backtest_bp", "Net_P&L_₹", "Win_Loss", "P&L_vs_Max_Profit_%", "Notes",
    "Backtest_Expected_P&L_₹", "Backtest_Deviation_%",
]
DAILY_CSV_COLUMNS = [
    "Date", "Day_of_Week", "Account_Equity_₹", "Cumulative_P&L_₹", "Open_Trades_Count", "VIX_Close",
    "Market_Regime", "Weekly_Opportunities_Eligible", "Weekly_Trades_Executed", "Monthly_Eligible_Signals",
    "Monthly_Trades_Paused_Count", "Account_DD_%", "Win_Rate_%_YTD", "Avg_Trade_P&L_₹_YTD", "Notes",
]


class LedgerError(ValueError):
    """Bad input or a missing/duplicate record; the API maps this to a 4xx."""


class NotFound(LedgerError):
    pass


def net_credit_from_legs(legs: List[Dict[str, Any]]) -> float:
    """Net credit per unit: sold premium minus bought premium."""
    total = 0.0
    for leg in legs:
        price = float(leg["entry_price"])
        total += price if leg["action"] == "SELL" else -price
    return round(total, 4)


def compute_pnl(entry_credit: float, exit_debit: float, lots: int, lot_size: int,
                brokerage: float) -> Dict[str, float]:
    gross = round((entry_credit - exit_debit) * lots * lot_size, 2)
    return {"gross_pnl": gross, "net_pnl": round(gross - brokerage, 2)}


def max_drawdown_pct(initial: float, pnls: List[float]) -> float:
    equity = peak = initial
    worst = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak * 100)
    return round(worst, 2)


class Ledger:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ journals
    def create_journal(self, journal_id: str, label: str, initial_capital: float,
                       strategy_track: str = "weekly", notes: str = "") -> Dict[str, Any]:
        with self._conn() as c:
            if c.execute("SELECT 1 FROM journals WHERE journal_id=?", (journal_id,)).fetchone():
                raise LedgerError(f"Journal '{journal_id}' already exists")
            c.execute(
                "INSERT INTO journals VALUES (?,?,?,?,?,?,?)",
                (journal_id, label, initial_capital, strategy_track, "active", notes,
                 datetime.now().isoformat(timespec="seconds")),
            )
        return self.get_journal(journal_id)

    def get_journal(self, journal_id: str) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM journals WHERE journal_id=?", (journal_id,)).fetchone()
        if not row:
            raise NotFound(f"Journal '{journal_id}' not found")
        return dict(row)

    def list_journals(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM journals ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def update_journal(self, journal_id: str, **fields: Any) -> Dict[str, Any]:
        allowed = {k: v for k, v in fields.items() if k in ("label", "notes", "status") and v is not None}
        self.get_journal(journal_id)
        if allowed:
            sets = ", ".join(f"{k}=?" for k in allowed)
            with self._conn() as c:
                c.execute(f"UPDATE journals SET {sets} WHERE journal_id=?", (*allowed.values(), journal_id))
        return self.get_journal(journal_id)

    def summary(self, journal_id: str) -> Dict[str, Any]:
        j = self.get_journal(journal_id)
        closed = [t for t in self.list_trades(journal_id, "closed")]
        closed.sort(key=lambda t: (t["exit_date"] or "", t["trade_id"]))
        pnls = [t["net_pnl"] or 0.0 for t in closed]
        wins = sum(1 for p in pnls if p > 0)
        total = round(sum(pnls), 2)
        return {
            **j,
            "open_trades": len(self.list_trades(journal_id, "open")),
            "closed_trades": len(closed),
            "total_pnl": total,
            "account_equity": round(j["initial_capital"] + total, 2),
            "win_rate_pct": round(wins / len(closed) * 100, 1) if closed else None,
            "avg_trade_pnl": round(total / len(closed), 2) if closed else None,
            "account_dd_pct": max_drawdown_pct(j["initial_capital"], pnls),
        }

    # ------------------------------------------------------------------ trades
    def _next_trade_id(self, c: sqlite3.Connection, journal_id: str) -> str:
        n = c.execute("SELECT COUNT(*) FROM trades WHERE journal_id=?", (journal_id,)).fetchone()[0] + 1
        while c.execute("SELECT 1 FROM trades WHERE journal_id=? AND trade_id=?",
                        (journal_id, f"{journal_id}-{n:03d}")).fetchone():
            n += 1
        return f"{journal_id}-{n:03d}"

    def open_trade(self, journal_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        self.get_journal(journal_id)
        legs = data["legs"]
        if not legs:
            raise LedgerError("A trade needs at least one leg")
        credit = data.get("entry_credit")
        if credit is None:
            credit = net_credit_from_legs(legs)
        with self._conn() as c:
            trade_id = (data.get("trade_id") or "").strip() or self._next_trade_id(c, journal_id)
            if c.execute("SELECT 1 FROM trades WHERE journal_id=? AND trade_id=?",
                         (journal_id, trade_id)).fetchone():
                raise LedgerError(f"Trade '{trade_id}' already exists in journal '{journal_id}'")
            c.execute(
                """INSERT INTO trades (journal_id, trade_id, strategy, entry_date, entry_time_ist,
                   expiry_date, legs_json, lots, lot_size, entry_credit, entry_vix, entry_spot, regime,
                   ml_score, capital_deployed, notes, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
                (journal_id, trade_id, data["strategy"], data["entry_date"],
                 data.get("entry_time_ist", ""), data["expiry_date"], json.dumps(legs),
                 data["lots"], data["lot_size"], credit, data.get("entry_vix"), data.get("entry_spot"),
                 data.get("regime", ""), data.get("ml_score"), data.get("capital_deployed", 0.0),
                 data.get("notes", ""), datetime.now().isoformat(timespec="seconds")),
            )
        return self.get_trade(journal_id, trade_id)

    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["legs"] = json.loads(d.pop("legs_json"))
        return d

    def get_trade(self, journal_id: str, trade_id: str) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM trades WHERE journal_id=? AND trade_id=?",
                            (journal_id, trade_id)).fetchone()
        if not row:
            raise NotFound(f"Trade '{trade_id}' not found in journal '{journal_id}'")
        return self._row_to_trade(row)

    def list_trades(self, journal_id: Optional[str] = None, status: str = "all") -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM trades WHERE 1=1", []
        if journal_id:
            q += " AND journal_id=?"
            args.append(journal_id)
        if status in ("open", "closed"):
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY entry_date DESC, created_at DESC"
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def close_trade(self, journal_id: str, trade_id: str, *, exit_debit: float, exit_reason: str,
                    exit_date: str, exit_time_ist: str = "", brokerage: float = 0.0,
                    notes: str = "") -> Dict[str, Any]:
        t = self.get_trade(journal_id, trade_id)
        if t["status"] == "closed":
            raise LedgerError(f"Trade '{trade_id}' is already closed")
        pnl = compute_pnl(t["entry_credit"], exit_debit, t["lots"], t["lot_size"], brokerage)
        days = max((date.fromisoformat(exit_date) - date.fromisoformat(t["entry_date"])).days, 0)
        merged_notes = (t["notes"] + (" | " if t["notes"] and notes else "") + notes).strip()
        with self._conn() as c:
            c.execute(
                """UPDATE trades SET status='closed', exit_date=?, exit_time_ist=?, exit_debit=?,
                   exit_reason=?, brokerage=?, gross_pnl=?, net_pnl=?, days_held=?, notes=?
                   WHERE journal_id=? AND trade_id=?""",
                (exit_date, exit_time_ist, exit_debit, exit_reason, brokerage, pnl["gross_pnl"],
                 pnl["net_pnl"], days, merged_notes, journal_id, trade_id),
            )
        return self.get_trade(journal_id, trade_id)

    def delete_trade(self, journal_id: str, trade_id: str) -> None:
        self.get_trade(journal_id, trade_id)
        with self._conn() as c:
            c.execute("DELETE FROM trades WHERE journal_id=? AND trade_id=?", (journal_id, trade_id))

    # ------------------------------------------------------------------ daily log
    def upsert_daily_log(self, journal_id: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        self.get_journal(journal_id)
        with self._conn() as c:
            c.execute(
                """INSERT INTO daily_log VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(journal_id, log_date) DO UPDATE SET
                   account_equity=excluded.account_equity, cumulative_pnl=excluded.cumulative_pnl,
                   open_trades=excluded.open_trades, vix_close=excluded.vix_close,
                   regime=excluded.regime, account_dd_pct=excluded.account_dd_pct, notes=excluded.notes""",
                (journal_id, entry["log_date"], entry["account_equity"], entry["cumulative_pnl"],
                 entry["open_trades"], entry.get("vix_close"), entry.get("regime", ""),
                 entry.get("account_dd_pct", 0.0), entry.get("notes", "")),
            )
        return self.list_daily_log(journal_id)[0]

    def list_daily_log(self, journal_id: str) -> List[Dict[str, Any]]:
        self.get_journal(journal_id)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM daily_log WHERE journal_id=? ORDER BY log_date DESC",
                             (journal_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ settings + auto-trader timeline
    def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def kv_delete(self, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM kv WHERE key=?", (key,))

    def add_event(self, at: str, kind: str, code: str, message: str, trade_id: Optional[str] = None,
                  data: Optional[Dict[str, Any]] = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO auto_events (at, day, kind, code, message, trade_id, data_json) VALUES (?,?,?,?,?,?,?)",
                (at, at[:10], kind, code, message, trade_id, json.dumps(data, default=str) if data else None),
            )

    def list_events(self, limit: int = 200, day: Optional[str] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM auto_events", []
        if day:
            q += " WHERE day=?"
            args.append(day)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d.pop("data_json")) if d.get("data_json") else None
            out.append(d)
        return out

    def has_event(self, day: str, code: str, since_id: int = 0) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM auto_events WHERE day=? AND code=? AND id>? LIMIT 1",
                            (day, code, since_id)).fetchone()
        return row is not None

    # ------------------------------------------------------------------ CSV export
    def trades_csv(self, journal_id: Optional[str] = None) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=TRADES_CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        for t in reversed(self.list_trades(journal_id, "all")):
            strikes = "/".join(f"{int(l['strike'])}{l['option_type']}" for l in t["legs"] if l["action"] == "SELL")
            legs_txt = "; ".join(f"{l['action']} {int(l['strike'])}{l['option_type']}={l['entry_price']}"
                                 for l in t["legs"])
            try:
                dte = (date.fromisoformat(t["expiry_date"]) - date.fromisoformat(t["entry_date"])).days
            except ValueError:
                dte = ""
            net = t["net_pnl"]
            w.writerow({
                "Trade_ID": t["trade_id"], "Date_Entry": t["entry_date"], "Entry_Time_IST": t["entry_time_ist"],
                "Strike_Entry": strikes, "Bid_Ask_Entry": legs_txt, "Entry_Price_Fill": t["entry_credit"],
                "Lots_Size": t["lots"], "Capital_Deployed_₹": t["capital_deployed"], "Strategy": t["strategy"],
                "Signal_VIX": t["entry_vix"] if t["entry_vix"] is not None else "",
                "Signal_Regime": t["regime"], "ML_Score": t["ml_score"] if t["ml_score"] is not None else "NA",
                "Expiry_DTE": dte, "Date_Exit": t["exit_date"] or "", "Exit_Time_IST": t["exit_time_ist"] or "",
                "Exit_Price_Fill": t["exit_debit"] if t["exit_debit"] is not None else "",
                "Exit_Reason": t["exit_reason"] or "", "Days_Held": t["days_held"] if t["days_held"] is not None else "",
                "Gross_P&L_₹": t["gross_pnl"] if t["gross_pnl"] is not None else "",
                "Brokerage_₹": t["brokerage"] if t["brokerage"] is not None else "",
                "Net_P&L_₹": net if net is not None else "",
                "Win_Loss": "" if net is None else ("W" if net > 0 else "L"),
                "Notes": t["notes"],
            })
        return buf.getvalue()

    def daily_log_csv(self, journal_id: str) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=DAILY_CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in reversed(self.list_daily_log(journal_id)):
            try:
                dow = date.fromisoformat(r["log_date"]).strftime("%A")
            except ValueError:
                dow = ""
            w.writerow({
                "Date": r["log_date"], "Day_of_Week": dow, "Account_Equity_₹": r["account_equity"],
                "Cumulative_P&L_₹": r["cumulative_pnl"], "Open_Trades_Count": r["open_trades"],
                "VIX_Close": r["vix_close"] if r["vix_close"] is not None else "",
                "Market_Regime": r["regime"], "Account_DD_%": r["account_dd_pct"], "Notes": r["notes"],
            })
        return buf.getvalue()
