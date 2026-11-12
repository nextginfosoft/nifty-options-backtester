import { useState } from "react";
import { api, useApi } from "./api";
import { humanize, inr, num, pct, tone } from "./format";
import type { AutoEvent, AutoPreview, AutoStatus, LiveTrades } from "./types";
import { Banner, Card, Empty, ErrorText, Pill, Row } from "./ui";

const kindTone = (k: AutoEvent["kind"]): "good" | "bad" | "warn" | "" =>
  k === "ENTRY" || k === "EXIT" ? "good" : k === "ERROR" ? "bad" : k === "PAUSE" || k === "SKIP" ? "warn" : "";

function Preview({ p }: { p: AutoPreview }) {
  return (
    <div>
      <Row k="Would decide"><Pill tone={p.decision === "ENTER" ? "good" : "warn"}>{p.decision === "ENTER" ? "Enter" : "Skip"}</Pill></Row>
      <Row k="Reason">{p.message}</Row>
      {p.inputs?.spot != null ? (
        <Row k="Inputs">Nifty {num(p.inputs.spot)} · VIX {num(p.inputs.vix)} · 5d {p.inputs.ret_5d == null ? "n/a" : pct(p.inputs.ret_5d * 100)}</Row>
      ) : null}
      {p.strategy ? <Row k="Strategy">{humanize(p.strategy)} · {p.lots} lots · expiry {p.expiry} ({p.dte} DTE)</Row> : null}
      {p.net_credit != null ? <Row k="Net credit">{num(p.net_credit)} per unit</Row> : null}
      {p.legs?.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Leg</th><th>Paper fill</th><th>Bid</th><th>Ask</th></tr></thead>
            <tbody>
              {p.legs.map((l, i) => (
                <tr key={i}>
                  <td>{l.action} {l.strike}{l.option_type}</td>
                  <td className="num">{num(l.entry_price)}</td>
                  <td className="num">{num(l.bid)}</td>
                  <td className="num">{num(l.ask)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {p.blocks.length ? (
        <p className="note">Schedule gates that would block a real entry right now: {p.blocks.map((b) => b.message).join("; ")}.</p>
      ) : null}
    </div>
  );
}

export default function Auto() {
  const status = useApi<AutoStatus>("/api/auto/status", 15000);
  const events = useApi<{ events: AutoEvent[] }>("/api/auto/events?limit=150", 15000);
  const s = status.data;
  const hasOpen = (s?.open_trades.length ?? 0) > 0;
  const live = useApi<LiveTrades>(hasOpen ? "/api/trades/live?journal_id=AUTO" : null, 15000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lots, setLots] = useState("");
  const [preview, setPreview] = useState<AutoPreview | null>(null);

  async function act(path: string, method = "POST", body: unknown = {}) {
    setBusy(true);
    setError(null);
    try {
      await api(path, { method, body });
      status.reload();
      events.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function runPreview() {
    setBusy(true);
    setError(null);
    try {
      setPreview(await api<AutoPreview>("/api/auto/preview"));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function turnOff() {
    const warn = hasOpen
      ? "An open position will stop being managed automatically. Turn off anyway?"
      : "Turn off the auto paper trader?";
    if (window.confirm(warn)) void act("/api/auto/disable");
  }

  const openTrades = s?.open_trades ?? [];

  return (
    <>
      <Banner tone={s?.enabled ? (s.paused_reason ? "warn" : "good") : "warn"}>
        {!s
          ? "Loading…"
          : !s.enabled
            ? "The auto paper trader is OFF. Turn it on once and it runs by itself; you only review."
            : s.paused_reason
              ? `Running, but new entries are paused (${s.paused_reason}). Open positions are still managed.`
              : "ON. It enters and exits paper trades by itself. It only reads quotes and never places orders."}
      </Banner>

      <div className="cols">
        <Card title="Control">
          <ErrorText error={error ?? status.error} />
          <Row k="Status"><Pill tone={s?.enabled ? "good" : "warn"}>{s?.enabled ? "On" : "Off"}</Pill></Row>
          <Row k="Lots per trade">{s?.lots ?? "–"} ({s ? s.lots * s.lot_size : "–"} units)</Row>
          <Row k="Last check">{s?.last_tick ? new Date(s.last_tick).toLocaleTimeString() : "–"}</Row>
          <Row k="Errors in a row">{s?.consecutive_errors ?? 0}</Row>
          <div className="actions">
            {s?.enabled ? (
              <button className="danger" disabled={busy} onClick={turnOff}>Turn off</button>
            ) : (
              <button className="primary" disabled={busy || !s} onClick={() => void act("/api/auto/enable", "POST", {})}>Turn on</button>
            )}
            {s?.enabled && !s.paused_reason ? (
              <button disabled={busy} onClick={() => void act("/api/auto/pause", "POST", { reason: "manual" })}>Pause new entries</button>
            ) : null}
            {s?.enabled && s.paused_reason ? (
              <button disabled={busy} onClick={() => void act("/api/auto/resume")}>Resume entries</button>
            ) : null}
          </div>
          <div className="actions">
            <input
              aria-label="Lots per trade"
              type="number"
              min="1"
              max="30"
              placeholder={String(s?.lots ?? 15)}
              value={lots}
              onChange={(e) => setLots(e.target.value)}
              style={{ width: 110 }}
            />
            <button
              disabled={busy || !lots}
              onClick={() => {
                void act("/api/auto/lots", "PUT", { lots: Number(lots) });
                setLots("");
              }}
            >
              Set lots
            </button>
          </div>
          <p className="note">
            Paper only. Entry check Mon/Tue 11:05–12:55 IST (one decision a day), exit checks every minute 09:15–15:30, daily log at
            16:30. It pauses itself at a 15% drawdown or after repeated errors. The strategy is not a validated candidate (see the repo notes).
          </p>
        </Card>

        <Card title="Preview today's decision" actions={<button disabled={busy} onClick={runPreview}>Run preview</button>}>
          {preview ? <Preview p={preview} /> : <Empty>Shows what it would do right now with live prices, ignoring the schedule. Nothing is recorded.</Empty>}
        </Card>
      </div>

      <Card title={`Open position (${openTrades.length})`}>
        {openTrades.length === 0 ? (
          <Empty>No open auto position.</Empty>
        ) : (
          openTrades.map((t) => {
            const lv = live.data?.trades.find((x) => x.trade_id === t.trade_id);
            return (
              <div key={t.trade_id}>
                <Row k={t.trade_id}>
                  {humanize(t.strategy)} · entered {t.entry_date} · expiry {t.expiry_date} · credit {num(t.entry_credit)}
                </Row>
                {lv?.ok ? (
                  <Row k="Live P&L">
                    <span className={`num ${tone(lv.unrealized_mark)}`}>{inr(lv.unrealized_mark)}</span> mark ·{" "}
                    <span className={`num ${tone(lv.unrealized_exit)}`}>{inr(lv.unrealized_exit)}</span> if closed now ·{" "}
                    {pct(lv.pct_credit_captured)} of credit captured
                  </Row>
                ) : (
                  <Row k="Live P&L"><span className="muted">{lv?.reason ?? live.data?.reason ?? "…"}</span></Row>
                )}
              </div>
            );
          })
        )}
        <p className="note">Full trade history and CSV export are on the Trades tab, journal “Auto paper trader”.</p>
      </Card>

      <Card title="Decision timeline (newest first)">
        {(events.data?.events.length ?? 0) === 0 ? (
          <Empty>No decisions yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Time (IST)</th><th>Type</th><th>Code</th><th>What happened</th></tr></thead>
              <tbody>
                {events.data!.events.map((e) => (
                  <tr key={e.id}>
                    <td>{e.at.replace("T", " ").slice(0, 16)}</td>
                    <td><Pill tone={kindTone(e.kind)}>{e.kind}</Pill></td>
                    <td>{humanize(e.code)}</td>
                    <td className="wrap">{e.message}</td>
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
