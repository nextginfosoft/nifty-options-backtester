import { Fragment, useMemo, useState, type FormEvent } from "react";
import { api, useApi } from "./api";
import { humanize, inr, nowIST, num, pct, todayISO, tone } from "./format";
import type { Journal, Leg, LiveTrades, Recommendation, Trade } from "./types";
import { Card, Empty, ErrorText, Pill, Row, Stat } from "./ui";

const STRATEGIES = ["put_credit_spread", "iron_condor", "call_credit_spread", "other"];
const EXIT_REASONS = ["profit_target", "stop_loss_100pct", "short_strike_breach", "max_hold", "expiry", "manual"];

const actionTone = (a: string): "good" | "bad" | "warn" | "" =>
  a === "HOLD" ? "good" : a === "EXIT_NOW" ? "bad" : a === "UNAVAILABLE" ? "" : "warn";

function legText(l: Leg): string {
  return `${l.action} ${l.strike}${l.option_type} @ ${l.entry_price}`;
}

// ---------------------------------------------------------------- create journal
function CreateJournal({ onCreated }: { onCreated: (id: string) => void }) {
  const [id, setId] = useState("P1");
  const [label, setLabel] = useState("Phase 1 weekly");
  const [capital, setCapital] = useState("2500000");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/journals", {
        method: "POST",
        body: { journal_id: id.trim(), label: label.trim(), initial_capital: Number(capital), strategy_track: "weekly" },
      });
      onCreated(id.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="New journal">
      <form onSubmit={submit}>
        <div className="form-grid">
          <label className="field">Journal id<input value={id} onChange={(e) => setId(e.target.value)} pattern="[A-Za-z0-9_\-]{1,40}" required /></label>
          <label className="field">Label<input value={label} onChange={(e) => setLabel(e.target.value)} required maxLength={80} /></label>
          <label className="field">Starting capital (₹)<input type="number" min="1" value={capital} onChange={(e) => setCapital(e.target.value)} required /></label>
        </div>
        <ErrorText error={error} />
        <div className="actions"><button className="primary" disabled={busy}>{busy ? "Creating…" : "Create journal"}</button></div>
      </form>
    </Card>
  );
}

// ---------------------------------------------------------------- log a new trade
interface LegDraft { action: "BUY" | "SELL"; strike: string; option_type: "CE" | "PE"; price: string }
const blankLeg = (action: "BUY" | "SELL", option_type: "CE" | "PE"): LegDraft => ({ action, strike: "", option_type, price: "" });
const PRESETS: Record<string, LegDraft[]> = {
  put_credit_spread: [blankLeg("SELL", "PE"), blankLeg("BUY", "PE")],
  call_credit_spread: [blankLeg("SELL", "CE"), blankLeg("BUY", "CE")],
  iron_condor: [blankLeg("SELL", "PE"), blankLeg("BUY", "PE"), blankLeg("SELL", "CE"), blankLeg("BUY", "CE")],
  other: [blankLeg("SELL", "PE")],
};

function LogTrade({ journal, onSaved }: { journal: Journal; onSaved: () => void }) {
  const [strategy, setStrategy] = useState("put_credit_spread");
  const [legs, setLegs] = useState<LegDraft[]>(PRESETS.put_credit_spread);
  const [entryDate, setEntryDate] = useState(todayISO());
  const [entryTime, setEntryTime] = useState(nowIST());
  const [expiry, setExpiry] = useState("");
  const [lots, setLots] = useState("15");
  const [vix, setVix] = useState("");
  const [spot, setSpot] = useState("");
  const [capital, setCapital] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const credit = useMemo(
    () => legs.reduce((sum, l) => sum + (Number(l.price) || 0) * (l.action === "SELL" ? 1 : -1), 0),
    [legs],
  );

  function setLeg(i: number, patch: Partial<LegDraft>) {
    setLegs((ls) => ls.map((l, idx) => (idx === i ? { ...l, ...patch } : l)));
  }
  function chooseStrategy(s: string) {
    setStrategy(s);
    setLegs(PRESETS[s] ?? PRESETS.other);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/trades", {
        method: "POST",
        body: {
          journal_id: journal.journal_id,
          strategy,
          entry_date: entryDate,
          entry_time_ist: entryTime,
          expiry_date: expiry,
          lots: Number(lots),
          legs: legs.map((l) => ({ action: l.action, strike: Number(l.strike), option_type: l.option_type, entry_price: Number(l.price) })),
          entry_vix: vix ? Number(vix) : null,
          entry_spot: spot ? Number(spot) : null,
          capital_deployed: capital ? Number(capital) : 0,
          notes,
        },
      });
      setNotes("");
      setLegs(PRESETS[strategy]);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Log a paper trade">
      <form onSubmit={submit}>
        <div className="form-grid">
          <label className="field">Strategy
            <select value={strategy} onChange={(e) => chooseStrategy(e.target.value)}>
              {STRATEGIES.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}
            </select>
          </label>
          <label className="field">Entry date<input type="date" value={entryDate} onChange={(e) => setEntryDate(e.target.value)} required /></label>
          <label className="field">Entry time (IST)<input value={entryTime} onChange={(e) => setEntryTime(e.target.value)} placeholder="11:30" maxLength={8} /></label>
          <label className="field">Expiry date<input type="date" value={expiry} onChange={(e) => setExpiry(e.target.value)} required /></label>
          <label className="field">Lots<input type="number" min="1" value={lots} onChange={(e) => setLots(e.target.value)} required /></label>
          <label className="field">VIX at entry<input type="number" step="0.01" value={vix} onChange={(e) => setVix(e.target.value)} /></label>
          <label className="field">Nifty at entry<input type="number" step="0.01" value={spot} onChange={(e) => setSpot(e.target.value)} /></label>
          <label className="field">Margin / capital (₹)<input type="number" min="0" value={capital} onChange={(e) => setCapital(e.target.value)} /></label>
        </div>

        <h3 style={{ margin: "16px 0 8px", fontSize: 13 }}>Legs — use the real broker fill per unit</h3>
        <div style={{ display: "grid", gap: 8 }}>
          {legs.map((l, i) => (
            <div className="leg-row" key={i}>
              <label className="field">Action
                <select value={l.action} onChange={(e) => setLeg(i, { action: e.target.value as "BUY" | "SELL" })}>
                  <option>SELL</option><option>BUY</option>
                </select>
              </label>
              <label className="field">Strike<input type="number" min="0" step="50" value={l.strike} onChange={(e) => setLeg(i, { strike: e.target.value })} required /></label>
              <label className="field">Type
                <select value={l.option_type} onChange={(e) => setLeg(i, { option_type: e.target.value as "CE" | "PE" })}>
                  <option>PE</option><option>CE</option>
                </select>
              </label>
              <label className="field">Price / unit<input type="number" min="0" step="0.05" value={l.price} onChange={(e) => setLeg(i, { price: e.target.value })} required /></label>
              <button type="button" aria-label="Remove leg" disabled={legs.length <= 1} onClick={() => setLegs((ls) => ls.filter((_, idx) => idx !== i))}>×</button>
            </div>
          ))}
        </div>
        <div className="actions">
          <button type="button" disabled={legs.length >= 8} onClick={() => setLegs((ls) => [...ls, blankLeg("SELL", "PE")])}>Add leg</button>
          <span className="muted">Net credit: <strong className={`num ${tone(credit)}`}>{num(credit)}</strong> per unit · {inr(credit * Number(lots || 0) * 65)} at 65/lot</span>
        </div>

        <label className="field" style={{ marginTop: 12 }}>Notes<textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={2000} /></label>
        <ErrorText error={error} />
        <div className="actions"><button className="primary" disabled={busy}>{busy ? "Saving…" : "Log trade"}</button></div>
      </form>
    </Card>
  );
}

// ---------------------------------------------------------------- close a trade
function CloseForm({ trade, suggested, onDone, onCancel }: { trade: Trade; suggested: number | null; onDone: () => void; onCancel: () => void }) {
  const [debit, setDebit] = useState("");
  const [reason, setReason] = useState("profit_target");
  const [date, setDate] = useState(todayISO());
  const [brokerage, setBrokerage] = useState("0");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const preview = debit === "" ? null : (trade.entry_credit - Number(debit)) * trade.lots * trade.lot_size - Number(brokerage || 0);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/api/trades/${encodeURIComponent(trade.trade_id)}/close?journal_id=${encodeURIComponent(trade.journal_id)}`, {
        method: "POST",
        body: { exit_debit: Number(debit), exit_reason: reason, exit_date: date, brokerage: Number(brokerage || 0), notes },
      });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <form className="inline-panel" onSubmit={submit}>
      <div className="form-grid">
        <label className="field">Net debit to close / unit<input type="number" min="0" step="0.05" value={debit} onChange={(e) => setDebit(e.target.value)} required autoFocus /></label>
        <label className="field">Reason
          <select value={reason} onChange={(e) => setReason(e.target.value)}>
            {EXIT_REASONS.map((r) => <option key={r} value={r}>{humanize(r)}</option>)}
          </select>
        </label>
        <label className="field">Exit date<input type="date" value={date} onChange={(e) => setDate(e.target.value)} required /></label>
        <label className="field">Brokerage (₹)<input type="number" min="0" value={brokerage} onChange={(e) => setBrokerage(e.target.value)} /></label>
        <label className="field span-2">Notes<input value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={2000} /></label>
      </div>
      <ErrorText error={error} />
      <div className="actions">
        <button className="primary" disabled={busy}>{busy ? "Closing…" : "Close trade"}</button>
        <button type="button" onClick={onCancel}>Cancel</button>
        {suggested != null ? (
          <button type="button" onClick={() => setDebit(suggested.toFixed(2))}>Use live exit price ({num(suggested)})</button>
        ) : null}
        {preview != null ? <span className="muted">Net P&amp;L: <strong className={`num ${tone(preview)}`}>{inr(preview)}</strong></span> : null}
      </div>
    </form>
  );
}

// ---------------------------------------------------------------- page
export default function Trades({
  journal,
  onCreated,
  onJournalsChanged,
}: {
  journal: Journal | null;
  onCreated: (id: string) => void;
  onJournalsChanged: () => void;
}) {
  const jq = journal ? `journal_id=${encodeURIComponent(journal.journal_id)}` : "";
  const trades = useApi<{ trades: Trade[] }>(journal ? `/api/trades?${jq}&status=all` : null);
  const monitor = useApi<{ recommendations: Recommendation[] }>(journal ? `/api/monitor?${jq}` : null, 60000);
  const hasOpen = (trades.data?.trades ?? []).some((t) => t.status === "open");
  const live = useApi<LiveTrades>(journal && hasOpen ? `/api/trades/live?${jq}` : null, 15000);
  const [closing, setClosing] = useState<string | null>(null);
  const [showLog, setShowLog] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);

  if (!journal) {
    return (
      <>
        <Card title="Get started"><Empty>Create a journal to start logging paper trades.</Empty></Card>
        <CreateJournal onCreated={onCreated} />
      </>
    );
  }

  const all = trades.data?.trades ?? [];
  const open = all.filter((t) => t.status === "open");
  const closed = all.filter((t) => t.status === "closed");
  const recFor = (id: string) => monitor.data?.recommendations.find((r) => r.trade_id === id);
  const liveFor = (id: string) => live.data?.trades.find((t) => t.trade_id === id);

  function refresh() {
    trades.reload();
    monitor.reload();
    onJournalsChanged();
  }

  async function remove(t: Trade) {
    if (!window.confirm(`Delete trade ${t.trade_id}? This cannot be undone.`)) return;
    setRowError(null);
    try {
      await api(`/api/trades/${encodeURIComponent(t.trade_id)}?journal_id=${encodeURIComponent(t.journal_id)}`, { method: "DELETE" });
      refresh();
    } catch (err) {
      setRowError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <>
      <section className="stats">
        <Stat label="Equity" value={inr(journal.account_equity)} sub={`Start ${inr(journal.initial_capital)}`} />
        <Stat label="Net P&L" value={inr(journal.total_pnl)} tone={tone(journal.total_pnl)} sub={`Avg ${inr(journal.avg_trade_pnl)} / trade`} />
        <Stat label="Win rate" value={journal.win_rate_pct == null ? "n/a" : `${journal.win_rate_pct}%`} sub={`${journal.closed_trades} closed`} />
        <Stat label="Max drawdown" value={`${journal.account_dd_pct}%`} sub="Pause rule at 15%" tone={journal.account_dd_pct >= 15 ? "bad" : ""} />
      </section>

      <Card
        title={`Open trades (${open.length})`}
        actions={<button onClick={() => setShowLog((v) => !v)}>{showLog ? "Hide form" : "Log a trade"}</button>}
      >
        <ErrorText error={trades.error ?? rowError} />
        {monitor.error && open.length > 0 ? <div className="note">Exit monitor unavailable: {monitor.error}</div> : null}
        {live.data?.ok && open.length > 0 ? (
          <div className="note">Live prices via Kite · {live.data.as_of?.slice(11, 19)} IST · Nifty {num(live.data.spot)}. Exit rules are guidance only; nothing closes automatically.</div>
        ) : null}
        {live.data && !live.data.ok && open.length > 0 ? (
          <div className="note">Live prices unavailable ({live.data.reason}). Showing estimates only.</div>
        ) : null}
        {open.length === 0 ? <Empty>No open trades.</Empty> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Trade</th><th>Strategy</th><th>Legs</th><th>Entry</th><th>Credit / lots</th><th>Live (Kite)</th><th>ML exit view (BS estimate)</th><th /></tr></thead>
              <tbody>
                {open.map((t) => {
                  const r = recFor(t.trade_id);
                  const lv = liveFor(t.trade_id);
                  return (
                    <Fragment key={t.trade_id}>
                      <tr>
                        <td>{t.trade_id}</td>
                        <td>{humanize(t.strategy)}</td>
                        <td className="wrap">{t.legs.map(legText).join(" · ")}</td>
                        <td>{t.entry_date}<br /><span className="muted">exp {t.expiry_date}</span></td>
                        <td className="num">{num(t.entry_credit)} × {t.lots}</td>
                        <td className="wrap">
                          {lv?.ok ? (
                            <>
                              <div>Mark <strong className={`num ${tone(lv.unrealized_mark)}`}>{inr(lv.unrealized_mark)}</strong></div>
                              <div>If closed now <strong className={`num ${tone(lv.unrealized_exit)}`}>{inr(lv.unrealized_exit)}</strong></div>
                              <div className="muted">{pct(lv.pct_credit_captured)} of credit captured</div>
                              {lv.alerts.map((a) => <div key={a.code} className="bad">⚠ {a.message}</div>)}
                            </>
                          ) : (
                            <span className="muted">{lv ? lv.reason : live.loading ? "…" : "–"}</span>
                          )}
                        </td>
                        <td className="wrap">
                          {r ? (
                            <>
                              <Pill tone={actionTone(r.action)}>{humanize(r.action)}</Pill>{" "}
                              {r.pnl_rupees != null ? <span className={`num ${tone(r.pnl_rupees)}`}>{inr(r.pnl_rupees)} (BS est.)</span> : null}
                              <ul className="plain" style={{ marginTop: 4 }}>{(r.reasoning ?? []).slice(0, 3).map((x) => <li key={x}>{x}</li>)}</ul>
                            </>
                          ) : <span className="muted">{monitor.loading ? "…" : "–"}</span>}
                        </td>
                        <td>
                          <button onClick={() => setClosing(closing === t.trade_id ? null : t.trade_id)}>Close</button>{" "}
                          <button className="danger" onClick={() => remove(t)} aria-label={`Delete ${t.trade_id}`}>Delete</button>
                        </td>
                      </tr>
                      {closing === t.trade_id ? (
                        <tr key={`${t.trade_id}-close`}><td colSpan={8}>
                          <CloseForm trade={t} suggested={liveFor(t.trade_id)?.exit_debit_est ?? null} onCancel={() => setClosing(null)} onDone={() => { setClosing(null); refresh(); }} />
                        </td></tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {showLog ? <LogTrade journal={journal} onSaved={() => { setShowLog(false); refresh(); }} /> : null}

      <Card
        title={`Closed trades (${closed.length})`}
        actions={<a href={`/api/export/trades.csv?${jq}`} download>Export CSV</a>}
      >
        {closed.length === 0 ? <Empty>No closed trades yet.</Empty> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Trade</th><th>Strategy</th><th>Entry → Exit</th><th>Credit → Debit</th><th>Reason</th><th className="right">Net P&amp;L</th></tr></thead>
              <tbody>
                {closed.map((t) => (
                  <tr key={t.trade_id}>
                    <td>{t.trade_id}</td>
                    <td>{humanize(t.strategy)}</td>
                    <td>{t.entry_date} → {t.exit_date}<br /><span className="muted">{t.days_held}d held</span></td>
                    <td className="num">{num(t.entry_credit)} → {num(t.exit_debit)}</td>
                    <td>{humanize(t.exit_reason)}</td>
                    <td className={`num right ${tone(t.net_pnl)}`}>{inr(t.net_pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <CreateJournal onCreated={onCreated} />
      <Card title="Journal">
        <Row k="Id">{journal.journal_id}</Row>
        <Row k="Created">{journal.created_at.replace("T", " ")}</Row>
        <Row k="Status"><Pill tone={journal.status === "active" ? "good" : ""}>{journal.status}</Pill></Row>
      </Card>
    </>
  );
}
