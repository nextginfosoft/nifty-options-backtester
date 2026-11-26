import { useApi } from "./api";
import { humanize, inr, num, pct, tone } from "./format";
import type { AccountStatus, AlertStatus, AutoEvent, AutoStatus, BackupStatus, BrokerStatus, Health, Live, LiveTrades } from "./types";
import { Card, Empty, Pill, Row, Stat } from "./ui";

type Level = "bad" | "warn";
interface Attention {
  level: Level;
  text: string;
  to?: string;
  action?: string;
}

const kindTone = (k: AutoEvent["kind"]): "good" | "bad" | "warn" | "" =>
  k === "ENTRY" || k === "EXIT" ? "good" : k === "ERROR" ? "bad" : k === "PAUSE" || k === "SKIP" ? "warn" : "";

/** When the next scheduled entry check is (Mon/Tue 11:05-12:55 IST). Ignores holidays, so it says "scheduled". */
function nextEntryCheck(): string {
  const ist = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
  const day = ist.getDay(); // 0 Sun .. 6 Sat
  const mins = ist.getHours() * 60 + ist.getMinutes();
  if ((day === 1 || day === 2) && mins < 12 * 60 + 55) return "today, 11:05 to 12:55 IST";
  if (day === 0) return "tomorrow (Monday), 11:05 to 12:55 IST";
  if (day === 1) return "tomorrow (Tuesday), 11:05 to 12:55 IST";
  return "next Monday, 11:05 to 12:55 IST";
}

function attention(
  s: AutoStatus | null,
  h: Health | null,
  broker: BrokerStatus | null,
  alerts: AlertStatus | null,
  backup: BackupStatus | null,
): Attention[] {
  const out: Attention[] = [];
  if (h?.scheduler?.stalled)
    out.push({ level: "bad", text: `The background loop has stopped (last tick ${h.scheduler.auto_tick_age_s ?? "?"} s ago). The auto trader is not running.` });
  if (h?.ledger_ok === false) out.push({ level: "bad", text: "The trade ledger cannot be read." });
  if (h?.state === "degraded") out.push({ level: "bad", text: `Some data failed to load: ${h.errors.join("; ")}` });
  if (backup?.stale) out.push({ level: "bad", text: "The daily ledger backup is overdue.", to: "settings", action: "Backup" });
  if (s && !s.enabled) out.push({ level: "warn", text: "The auto trader is off, so nothing is entered or managed.", to: "auto", action: "Turn on" });
  if (s?.enabled && s.paused_reason) out.push({ level: "warn", text: `New entries are paused (${s.paused_reason}). Open positions are still managed.`, to: "auto", action: "Resume" });
  if (s && s.consecutive_errors > 0) out.push({ level: "warn", text: `${s.consecutive_errors} error(s) in a row in the last checks.`, to: "auto", action: "Timeline" });
  if (broker && !broker.connected) out.push({ level: "warn", text: "Kite is not connected, so there are no live quotes for entries or exits.", to: "settings", action: "Connect" });
  if (h?.state === "loading") out.push({ level: "warn", text: "Market data and models are still loading." });
  if (alerts && !alerts.configured) out.push({ level: "warn", text: "Telegram is not set up: no alerts and no off-server backup.", to: "settings", action: "Set up" });
  if (h?.calendar?.low)
    out.push({ level: "warn", text: `The NSE holiday list only covers through ${h.calendar.covered_through ?? "an unknown date"}.` });
  return out;
}

export default function Today({ health }: { health: Health | null }) {
  const status = useApi<AutoStatus>("/api/auto/status", 30000);
  const events = useApi<{ events: AutoEvent[] }>("/api/auto/events?limit=6", 30000);
  const account = useApi<AccountStatus>("/api/status?journal_id=AUTO", 60000);
  const broker = useApi<BrokerStatus>("/api/broker/status", 60000);
  const alerts = useApi<AlertStatus>("/api/auto/alerts", 120000);
  const backup = useApi<BackupStatus>("/api/auto/backup", 120000);
  const quotes = useApi<Live>("/api/broker/live", 30000);

  const s = status.data;
  const open = s?.open_trades ?? [];
  const live = useApi<LiveTrades>(open.length > 0 ? "/api/trades/live?journal_id=AUTO" : null, 30000);

  const loaded = !!(s && health);
  const items = loaded ? attention(s, health, broker.data, alerts.data, backup.data) : [];
  const worst: "good" | "warn" | "bad" = items.some((i) => i.level === "bad") ? "bad" : items.length ? "warn" : "good";
  const a = account.data;

  return (
    <>
      <section className={`hero ${loaded ? worst : ""}`} aria-live="polite">
        <div className="hero-title">
          {!loaded
            ? "Loading…"
            : worst === "good"
              ? "All good. Nothing needs you."
              : worst === "bad"
                ? "Needs your attention"
                : items.length === 1
                  ? "One thing to look at"
                  : `${items.length} things to look at`}
        </div>
        {loaded && worst === "good" ? (
          <div className="hero-sub">
            The auto trader is on and checking every minute. Next entry check: {nextEntryCheck()} (scheduled; market holidays not applied here).
          </div>
        ) : null}
        {items.length > 0 ? (
          <ul className="attn">
            {items.map((i) => (
              <li key={i.text} className={i.level}>
                <span className={`dot ${i.level}`} />
                <span>{i.text}</span>
                {i.to ? <a className="attn-link" href={`#${i.to}`}>{i.action ?? "Open"}</a> : null}
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <section className="stats">
        <Stat
          label="Auto trader"
          value={!s ? "–" : !s.enabled ? "Off" : s.paused_reason ? "Paused" : "On"}
          tone={!s ? "" : !s.enabled ? "warn" : s.paused_reason ? "warn" : "good"}
          sub={s?.last_tick ? `last check ${new Date(s.last_tick).toLocaleTimeString()}` : undefined}
        />
        <Stat
          label="Nifty / VIX"
          value={quotes.data?.ok ? `${num(quotes.data.spot, 0)} / ${num(quotes.data.vix)}` : "–"}
          sub={quotes.data?.ok ? `as of ${quotes.data.as_of?.replace("T", " ").slice(0, 16) ?? ""}` : (quotes.data?.reason ?? undefined)}
        />
        <Stat
          label="Results so far"
          value={<span className={tone(a?.cumulative_pnl)}>{inr(a?.cumulative_pnl)}</span>}
          sub={a ? `${a.closed_trades} closed · win rate ${a.win_rate_pct == null ? "n/a" : pct(a.win_rate_pct)}` : undefined}
        />
        <Stat label="Open positions" value={String(open.length)} sub={s ? `${s.lots} lots per trade` : undefined} />
      </section>

      <Card title={`Open position (${open.length})`} actions={<a className="button" href="#auto">Details</a>}>
        {open.length === 0 ? (
          <Empty>No open position. The next entry check is {nextEntryCheck()}.</Empty>
        ) : (
          open.map((t) => {
            const lv = live.data?.trades.find((x) => x.trade_id === t.trade_id);
            const captured = lv?.ok ? lv.pct_credit_captured : null;
            return (
              <div key={t.trade_id} className="position">
                <div className="position-head">
                  <strong>{t.trade_id}</strong> <span className="muted">{humanize(t.strategy)} · entered {t.entry_date} · expires {t.expiry_date}</span>
                </div>
                {lv?.ok ? (
                  <>
                    <div className="position-pnl">
                      <span className={`big num ${tone(lv.unrealized_exit)}`}>{inr(lv.unrealized_exit)}</span>
                      <span className="muted"> if closed now · mark {inr(lv.unrealized_mark)}</span>
                    </div>
                    {captured != null ? (
                      <div className="meter-wrap" title={`${pct(captured)} of the credit captured`}>
                        <div className="meter"><div className={`meter-fill ${captured < 0 ? "bad" : ""}`} style={{ width: `${Math.min(Math.max(captured, 0), 100)}%` }} /></div>
                        <span className="muted">{pct(captured)} of credit captured (exit target is set by the rules)</span>
                      </div>
                    ) : null}
                  </>
                ) : (
                  <div className="muted">{lv?.reason ?? live.data?.reason ?? "Waiting for live quotes…"}</div>
                )}
              </div>
            );
          })
        )}
      </Card>

      <Card title="Latest decisions" actions={<a className="button" href="#auto">Full timeline</a>}>
        {(events.data?.events.length ?? 0) === 0 ? (
          <Empty>No decisions yet.</Empty>
        ) : (
          <ul className="feed">
            {events.data!.events.map((e) => (
              <li key={e.id}>
                <span className="muted num">{e.at.replace("T", " ").slice(5, 16)}</span>
                <Pill tone={kindTone(e.kind)}>{e.kind}</Pill>
                <span>{e.message}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Is the strategy working?" actions={<a className="button" href="#evidence">Evidence</a>}>
        <Row k="Closed auto trades">{a?.closed_trades ?? "–"}</Row>
        <p className="note" style={{ marginBottom: 0 }}>
          A handful of trades cannot show an edge either way. The Evidence tab compares your results with the backtests and says when there is enough data to judge.
        </p>
      </Card>
    </>
  );
}
