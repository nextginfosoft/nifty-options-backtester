import { useState } from "react";
import { api, useApi } from "./api";
import { humanize, inr, num, pct, tone } from "./format";
import type { AccountStatus, Health, RefreshResult, Signal, Track } from "./types";
import { Banner, Card, Empty, Pill, Row, Stat } from "./ui";

function TrackCard({ title, track }: { title: string; track: Track }) {
  return (
    <Card title={title}>
      <Row k="Decision">
        <Pill tone={track.should_enter && track.recommended_strategy ? "good" : "warn"}>
          {!track.should_enter ? "No entry" : track.recommended_strategy ? "Entry candidate" : "Score passes, no strategy chosen"}
        </Pill>
      </Row>
      <Row k="Signal">{humanize(track.signal)}</Row>
      <Row k="Quality score">{track.quality_score == null ? "–" : pct(track.quality_score * 100)}</Row>
      <Row k="Strategy">{humanize(track.recommended_strategy) === "–" ? "none selected" : humanize(track.recommended_strategy)}</Row>
      <Row k="Sizer lots (indicative)">{track.suggested_lots ?? "–"}</Row>
      <Row k="DTE window">{track.dte_window ?? "–"}</Row>
      {track.skip_reason ? <Row k="Skip reason">{humanize(track.skip_reason)}</Row> : null}
      <Row k="Eligible">
        {track.eligible_strategies.length ? (
          <span className="chips">
            {track.eligible_strategies.map((s) => (
              <Pill key={s}>{humanize(s)}</Pill>
            ))}
          </span>
        ) : (
          "none"
        )}
      </Row>
    </Card>
  );
}

function DataCard({ health, onRefreshed }: { health: Health | null; onRefreshed: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<RefreshResult | null>(null);
  const last = done ?? health?.last_refresh ?? null;

  async function refresh() {
    setBusy(true);
    setError(null);
    try {
      setDone(await api<RefreshResult>("/api/market/refresh", { method: "POST" }));
      onRefreshed();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title="Market data"
      actions={<button onClick={refresh} disabled={busy || health?.state === "loading"}>{busy ? "Refreshing…" : "Refresh data"}</button>}
    >
      <Row k="Data as of (last completed session)">{health?.data_as_of ?? "–"}</Row>
      <Row k="Last refresh">
        {last ? (
          <>
            <Pill tone={last.ok ? "good" : "bad"}>{last.ok ? (last.changed ? "new data" : "up to date") : "failed"}</Pill>{" "}
            <span className="muted">{last.trigger} · {new Date(last.at).toLocaleString()}</span>
          </>
        ) : (
          <span className="muted">since the server started</span>
        )}
      </Row>
      <p className="note" style={{ marginBottom: 0 }}>
        Refreshes automatically each trading day at about 08:45 IST, before the 11:00–13:00 entry window, and loads the previous
        completed session. Today's own bar is never used. A failed refresh keeps the previous data.
      </p>
      {error ? <div className="error-text" role="alert">{error}</div> : null}
    </Card>
  );
}

export default function Dashboard({ journalId, health }: { journalId: string; health: Health | null }) {
  const q = journalId ? `?journal_id=${encodeURIComponent(journalId)}` : "";
  const status = useApi<AccountStatus>(`/api/status${q}`, 60000);
  const signal = useApi<Signal>("/api/signal", 60000);
  const s = status.data;
  const sig = signal.data;

  return (
    <>
      {health?.state === "loading" ? (
        <Banner tone="warn">Market data and models are still loading. Signals appear when this finishes.</Banner>
      ) : null}
      {health?.state === "degraded" ? (
        <Banner tone="bad">
          Some data failed to load: {health.errors.join("; ")}. Signals may be unavailable.
        </Banner>
      ) : null}
      {signal.error && health?.state !== "loading" ? <Banner tone="bad">Signal unavailable: {signal.error}</Banner> : null}
      {sig ? (
        <Banner tone={sig.within_entry_window ? "good" : "warn"}>
          {sig.within_entry_window
            ? "Inside the 11:00–13:00 IST entry window. Take real broker bid/ask before logging any fill."
            : "Outside the 11:00–13:00 IST entry window. Signals are informational only."}
        </Banner>
      ) : null}

      <DataCard health={health} onRefreshed={() => { status.reload(); signal.reload(); }} />

      <section className="stats">
        <Stat label="Nifty spot" value={num(s?.spot_now)} sub={s?.market_date ? `as of ${s.market_date.slice(0, 10)}` : "waiting for data"} />
        <Stat label="India VIX" value={num(s?.vix_now)} sub={s?.regime ? `Regime: ${humanize(s.regime)}` : undefined} />
        <Stat label="Account equity" value={inr(s?.account_equity)} sub={`Start ${inr(s?.initial_capital)}`} />
        <Stat label="Cumulative P&L" value={inr(s?.cumulative_pnl)} tone={tone(s?.cumulative_pnl)} sub={`Max drawdown ${pct(s?.account_dd_pct)}`} />
        <Stat
          label="Trades"
          value={`${s?.open_trades ?? 0} open / ${s?.closed_trades ?? 0} closed`}
          sub={`Win rate ${s?.win_rate_pct == null ? "n/a" : pct(s.win_rate_pct)}`}
        />
        <Stat label="Available capital" value={inr(s?.available_capital)} sub={`Reserve ${inr(s?.reserve_capital)}`} />
      </section>

      {sig ? (
        <>
          <div className="cols">
            <TrackCard title="Weekly track" track={sig.weekly} />
            <TrackCard title="Monthly track (paused in phase 1)" track={sig.monthly} />
          </div>
          <div className="cols">
            <Card title="Macro context">
              {sig.macro_context.length ? (
                <ul className="plain">{sig.macro_context.map((m) => <li key={m}>{m}</li>)}</ul>
              ) : (
                <Empty>None</Empty>
              )}
            </Card>
            <Card title="Model reasoning">
              {sig.reasoning.filter((r) => !/importance/.test(r)).length ? (
                <ul className="plain">
                  {sig.reasoning.filter((r) => !/importance/.test(r)).slice(0, 8).map((m) => <li key={m}>{m}</li>)}
                </ul>
              ) : (
                <Empty>None</Empty>
              )}
            </Card>
          </div>
        </>
      ) : null}

      <p className="note">
        Signals use Black-Scholes estimates and an ML score, not broker prices. The weekly strategy is not a validated
        candidate (see BACKTEST_CHANGELOG.md in the repo). This tool records paper trades only and never places orders.
      </p>
    </>
  );
}
