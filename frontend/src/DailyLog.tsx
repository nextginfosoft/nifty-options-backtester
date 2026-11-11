import { useEffect, useState, type FormEvent } from "react";
import { api, useApi } from "./api";
import { inr, num, pct, todayISO } from "./format";
import type { AccountStatus, DailyEntry, Journal } from "./types";
import { Card, Empty, ErrorText } from "./ui";

export default function DailyLog({ journal }: { journal: Journal | null }) {
  const id = journal?.journal_id ?? null;
  const q = id ? encodeURIComponent(id) : "";
  const log = useApi<{ entries: DailyEntry[] }>(id ? `/api/journals/${q}/daily-log` : null);
  const status = useApi<AccountStatus>(id ? `/api/status?journal_id=${q}` : null);

  const [date, setDate] = useState(todayISO());
  const [equity, setEquity] = useState("");
  const [pnl, setPnl] = useState("");
  const [openCount, setOpenCount] = useState("0");
  const [vix, setVix] = useState("");
  const [regime, setRegime] = useState("");
  const [dd, setDd] = useState("0");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Prefill from the live account snapshot whenever it (re)loads.
  const s = status.data;
  useEffect(() => {
    if (!s) return;
    setEquity(String(Math.round(s.account_equity)));
    setPnl(String(Math.round(s.cumulative_pnl)));
    setOpenCount(String(s.open_trades));
    setVix(s.vix_now != null ? s.vix_now.toFixed(2) : "");
    setRegime(s.regime ?? "");
    setDd(String(s.account_dd_pct));
  }, [s]);

  if (!journal || !id) {
    return <Card title="Daily log"><Empty>Create a journal on the Trades tab first.</Empty></Card>;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/api/journals/${q}/daily-log`, {
        method: "POST",
        body: {
          log_date: date,
          account_equity: Number(equity),
          cumulative_pnl: Number(pnl),
          open_trades: Number(openCount),
          vix_close: vix ? Number(vix) : null,
          regime,
          account_dd_pct: Number(dd || 0),
          notes,
        },
      });
      setNotes("");
      log.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const entries = log.data?.entries ?? [];

  return (
    <>
      <Card title={`Log the day — ${journal.label}`}>
        <form onSubmit={submit}>
          <div className="form-grid">
            <label className="field">Date<input type="date" value={date} onChange={(e) => setDate(e.target.value)} required /></label>
            <label className="field">Account equity (₹)<input type="number" value={equity} onChange={(e) => setEquity(e.target.value)} required /></label>
            <label className="field">Cumulative P&amp;L (₹)<input type="number" value={pnl} onChange={(e) => setPnl(e.target.value)} required /></label>
            <label className="field">Open trades<input type="number" min="0" value={openCount} onChange={(e) => setOpenCount(e.target.value)} required /></label>
            <label className="field">VIX close<input type="number" step="0.01" value={vix} onChange={(e) => setVix(e.target.value)} /></label>
            <label className="field">Regime<input value={regime} onChange={(e) => setRegime(e.target.value)} /></label>
            <label className="field">Drawdown %<input type="number" step="0.01" value={dd} onChange={(e) => setDd(e.target.value)} /></label>
            <label className="field span-2">Notes (skip reasons such as PCS_DECLINED go here)<input value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={2000} /></label>
          </div>
          <ErrorText error={error} />
          <div className="actions">
            <button className="primary" disabled={busy}>{busy ? "Saving…" : "Save entry"}</button>
            <span className="note">Saving the same date again updates that day's entry.</span>
          </div>
        </form>
      </Card>

      <Card title={`History (${entries.length})`} actions={<a href={`/api/export/daily-log.csv?journal_id=${q}`} download>Export CSV</a>}>
        <ErrorText error={log.error} />
        {entries.length === 0 ? <Empty>No entries yet.</Empty> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Date</th><th className="right">Equity</th><th className="right">Cum. P&amp;L</th><th>Open</th><th>VIX</th><th>Regime</th><th>DD</th><th>Notes</th></tr></thead>
              <tbody>
                {entries.map((r) => (
                  <tr key={r.log_date}>
                    <td>{r.log_date}</td>
                    <td className="num right">{inr(r.account_equity)}</td>
                    <td className="num right">{inr(r.cumulative_pnl)}</td>
                    <td>{r.open_trades}</td>
                    <td className="num">{num(r.vix_close)}</td>
                    <td>{r.regime || "–"}</td>
                    <td className="num">{pct(r.account_dd_pct)}</td>
                    <td className="wrap">{r.notes}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
