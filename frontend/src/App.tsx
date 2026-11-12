import { useCallback, useEffect, useState } from "react";
import { api, setUnauthorizedHandler, useApi } from "./api";
import Auto from "./Auto";
import Broker from "./Broker";
import Dashboard from "./Dashboard";
import DailyLog from "./DailyLog";
import Login from "./Login";
import Trades from "./Trades";
import { num } from "./format";
import type { Health, Journal, Live } from "./types";
import { Pill } from "./ui";

type Tab = "dashboard" | "trades" | "auto" | "log" | "broker";
const TABS: { id: Tab; label: string }[] = [
  { id: "dashboard", label: "Dashboard" },
  { id: "trades", label: "Trades" },
  { id: "auto", label: "Auto trader" },
  { id: "log", label: "Daily log" },
  { id: "broker", label: "Broker" },
];

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
  const h = window.location.hash.replace("#", "") as Tab;
  return TABS.some((t) => t.id === h) ? h : "dashboard";
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
  const [journalId, setJournalId] = useState<string>(() => readStored("journal"));

  const journals = useApi<{ journals: Journal[] }>("/api/journals");
  const health = useApi<Health>("/api/health", 10000);
  const live = useApi<Live>("/api/broker/live", 15000);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

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

  async function logout() {
    await api("/api/auth/logout", { method: "POST" }).catch(() => undefined);
    onLogout();
  }

  const state = health.data?.state;
  const stateTone = state === "ready" ? "good" : state === "degraded" ? "bad" : "warn";

  return (
    <>
      <header className="app-header">
        <span className="brand">Nifty Paper Trading</span>
        {health.data ? (
          <Pill tone={stateTone}>{state === "ready" ? "Data ready" : state === "loading" ? "Loading data…" : state}</Pill>
        ) : null}
        {live.data?.ok ? (
          <span className="num muted" title={`Live via Kite at ${live.data.as_of ?? ""}`}>
            Nifty {num(live.data.spot)} · VIX {num(live.data.vix)}
          </span>
        ) : null}
        <span className="spacer" />
        <label className="muted" htmlFor="journal-select">Journal</label>
        <select id="journal-select" value={current?.journal_id ?? ""} onChange={(e) => select(e.target.value)}>
          {list.length === 0 ? <option value="">No journals yet</option> : null}
          {list.map((j) => (
            <option key={j.journal_id} value={j.journal_id}>
              {j.label} ({j.journal_id})
            </option>
          ))}
        </select>
        <button onClick={logout}>Sign out</button>
      </header>

      <nav className="tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            className="tab"
            aria-selected={tab === t.id}
            onClick={() => {
              window.location.hash = t.id;
              setTab(t.id);
            }}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <main className="page">
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
        {tab === "log" && <DailyLog journal={current} />}
        {tab === "broker" && <Broker />}
      </main>
    </>
  );
}
