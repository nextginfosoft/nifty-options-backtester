import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, setUnauthorizedHandler, useApi } from "./api";
import Auto from "./Auto";
import Dashboard from "./Dashboard";
import DailyLog from "./DailyLog";
import Evidence from "./Evidence";
import Login from "./Login";
import Settings from "./Settings";
import Today from "./Today";
import Trades from "./Trades";
import { num } from "./format";
import type { Health, Journal, Live } from "./types";
import { Banner, Pill } from "./ui";

type Tab = "today" | "auto" | "trades" | "evidence" | "dashboard" | "log" | "settings";

const TABS: { id: Tab; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "auto", label: "Auto trader" },
  { id: "trades", label: "Trades" },
  { id: "evidence", label: "Evidence" },
  { id: "dashboard", label: "Market" },
  { id: "log", label: "Daily log" },
  { id: "settings", label: "Settings" },
];

/** Phone bottom bar: four main places plus a "More" menu for the rest. */
const PRIMARY: Tab[] = ["today", "auto", "trades", "evidence"];
const MORE: Tab[] = ["dashboard", "log", "settings"];
/** Only these screens read the journal picked in the header; the others always use the auto trader's journal. */
const JOURNAL_TABS: Tab[] = ["trades", "dashboard", "log"];

const ICONS: Record<string, ReactNode> = {
  today: <path d="M3 11l9-8 9 8v10a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z" />,
  auto: <path d="M13 2L4 14h7l-1 8 9-12h-7z" />,
  trades: <path d="M4 6h16M4 12h16M4 18h10" />,
  evidence: <path d="M5 21V10M12 21V4M19 21v-8" />,
  more: <path d="M5 12h.01M12 12h.01M19 12h.01" strokeWidth="3.2" />,
};

function Icon({ name }: { name: string }) {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {ICONS[name]}
    </svg>
  );
}

function readStored(key: string): string {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}
function writeStored(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage may be unavailable; the selection just won't persist */
  }
}

function tabFromHash(): Tab {
  const h = window.location.hash.replace("#", "");
  if (h === "broker" || (!h && new URLSearchParams(window.location.search).get("broker"))) return "settings"; // old bookmarks and the Kite redirect
  return (TABS.find((t) => t.id === h)?.id ?? "today") as Tab;
}

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);

  useEffect(() => {
    setUnauthorizedHandler(() => setAuthed(false));
    api<{ authenticated: boolean }>("/api/auth/me")
      .then((r) => setAuthed(r.authenticated))
      .catch(() => setAuthed(false));
  }, []);

  if (authed === null) return <div className="login-wrap muted">Loading…</div>;
  if (!authed) return <Login onSuccess={() => setAuthed(true)} />;
  return <Shell onLogout={() => setAuthed(false)} />;
}

function Shell({ onLogout }: { onLogout: () => void }) {
  const [tab, setTab] = useState<Tab>(tabFromHash);
  const [moreOpen, setMoreOpen] = useState(false);
  const [journalId, setJournalId] = useState<string>(() => readStored("journal"));

  const journals = useApi<{ journals: Journal[] }>("/api/journals");
  const health = useApi<Health>("/api/health", 10000);
  const live = useApi<Live>("/api/broker/live", 15000);

  useEffect(() => {
    const onHash = () => {
      setTab(tabFromHash());
      setMoreOpen(false);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (!moreOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMoreOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [moreOpen]);

  const list = journals.data?.journals ?? [];
  const current = list.find((j) => j.journal_id === journalId) ?? null;

  // Fall back to the first journal when the stored one no longer exists.
  useEffect(() => {
    if (journals.data && !current && list.length > 0) setJournalId(list[0].journal_id);
  }, [journals.data, current, list]);

  const select = useCallback((id: string) => {
    setJournalId(id);
    writeStored("journal", id);
  }, []);

  const go = (t: Tab) => {
    window.location.hash = t;
    setTab(t);
    setMoreOpen(false);
  };

  async function logout() {
    await api("/api/auth/logout", { method: "POST" }).catch(() => undefined);
    onLogout();
  }

  const state = health.data?.state;
  const stateTone = state === "ready" ? "good" : state === "degraded" ? "bad" : "warn";
  const showJournal = JOURNAL_TABS.includes(tab);
  const label = (t: Tab) => TABS.find((x) => x.id === t)!.label;

  return (
    <>
      <header className="app-header">
        <span className="brand">Nifty Paper Trading</span>
        {health.data ? (
          <Pill tone={stateTone}>{state === "ready" ? "Data ready" : state === "loading" ? "Loading data…" : state}</Pill>
        ) : null}
        {live.data?.ok ? (
          <span className="num muted quote" title={`Live via Kite at ${live.data.as_of ?? ""}`}>
            Nifty {num(live.data.spot)} · VIX {num(live.data.vix)}
          </span>
        ) : null}
        <span className="spacer" />
        {showJournal ? (
          <>
            <label className="muted" htmlFor="journal-select">Journal</label>
            <select id="journal-select" value={current?.journal_id ?? ""} onChange={(e) => select(e.target.value)}>
              {list.length === 0 ? <option value="">No journals yet</option> : null}
              {list.map((j) => (
                <option key={j.journal_id} value={j.journal_id}>
                  {j.label} ({j.journal_id})
                </option>
              ))}
            </select>
          </>
        ) : null}
        <button onClick={logout}>Sign out</button>
      </header>

      <nav className="tabs" role="tablist" aria-label="Sections">
        {TABS.map((t) => (
          <button key={t.id} role="tab" className="tab" aria-selected={tab === t.id} onClick={() => go(t.id)}>
            {t.label}
          </button>
        ))}
      </nav>

      <main className="page">
        {health.data?.calendar?.low && tab !== "today" ? (
          <Banner tone="warn">
            The NSE holiday list is only covered through {health.data.calendar.covered_through ?? "an unknown date"}. Add next year&apos;s
            list to data/nse_holidays.json when NSE publishes it (usually late December), or the auto trader will treat holidays as trading days.
          </Banner>
        ) : null}
        {tab === "today" && <Today health={health.data} />}
        {tab === "dashboard" && <Dashboard journalId={current?.journal_id ?? ""} health={health.data} />}
        {tab === "trades" && (
          <Trades
            journal={current}
            onJournalsChanged={journals.reload}
            onCreated={(id) => {
              select(id);
              journals.reload();
            }}
          />
        )}
        {tab === "auto" && <Auto />}
        {tab === "evidence" && <Evidence />}
        {tab === "log" && <DailyLog journal={current} />}
        {tab === "settings" && <Settings />}
      </main>

      {moreOpen ? <div className="more-backdrop" onClick={() => setMoreOpen(false)} /> : null}
      {moreOpen ? (
        <div className="more-menu" role="menu">
          {MORE.map((t) => (
            <button key={t} role="menuitem" className={tab === t ? "active" : ""} onClick={() => go(t)}>
              {label(t)}
            </button>
          ))}
        </div>
      ) : null}
      <nav className="bottom-nav" aria-label="Main">
        {PRIMARY.map((t) => (
          <button key={t} className={tab === t ? "active" : ""} aria-current={tab === t ? "page" : undefined} onClick={() => go(t)}>
            <Icon name={t} />
            <span>{t === "auto" ? "Auto" : label(t)}</span>
          </button>
        ))}
        <button className={MORE.includes(tab) ? "active" : ""} aria-haspopup="menu" aria-expanded={moreOpen} onClick={() => setMoreOpen((v) => !v)}>
          <Icon name="more" />
          <span>{MORE.includes(tab) ? label(tab) : "More"}</span>
        </button>
      </nav>
    </>
  );
}
