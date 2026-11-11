import { useEffect, useState, type FormEvent } from "react";
import { api, useApi } from "./api";
import { num } from "./format";
import type { BrokerStatus, Live } from "./types";
import { Banner, Card, ErrorText, Pill, Row } from "./ui";

export default function Broker() {
  const status = useApi<BrokerStatus>("/api/broker/status", 30000);
  const live = useApi<Live>("/api/broker/live", 15000);
  const [flash, setFlash] = useState<"connected" | "error" | null>(null);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The Kite callback redirects back to /?broker=connected|error.
  useEffect(() => {
    const p = new URLSearchParams(window.location.search).get("broker");
    if (p === "connected" || p === "error") {
      setFlash(p);
      window.history.replaceState({}, "", window.location.pathname + "#broker");
      status.reload();
      live.reload();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function connect() {
    setBusy(true);
    setError(null);
    try {
      const { url } = await api<{ url: string }>("/api/broker/kite/login-url");
      window.location.href = url;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  async function submitToken(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/broker/kite/token", { method: "POST", body: { request_token: token.trim() } });
      setToken("");
      setFlash("connected");
      status.reload();
      live.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const st = status.data;
  const lv = live.data;

  return (
    <>
      {flash === "connected" ? <Banner tone="good">Kite connected for today's session.</Banner> : null}
      {flash === "error" ? <Banner tone="bad">Kite login did not complete. Try again.</Banner> : null}

      <div className="cols">
        <Card title="Zerodha Kite Connect">
          <ErrorText error={status.error} />
          <Row k="Server configured"><Pill tone={st?.configured ? "good" : "warn"}>{st?.configured ? "Yes" : "No"}</Pill></Row>
          <Row k="Session today"><Pill tone={st?.connected ? "good" : "warn"}>{st?.connected ? "Connected" : "Not connected"}</Pill></Row>
          {st?.token_expires_at ? <Row k="Token expires">{new Date(st.token_expires_at).toLocaleString()}</Row> : null}
          {!st?.configured ? (
            <p className="note">
              Set KITE_API_KEY and KITE_API_SECRET in the server environment (deploy/.env), then restart. Secrets are never
              shown or stored in the browser.
            </p>
          ) : null}
          <div className="actions">
            <button className="primary" onClick={connect} disabled={busy || !st?.configured}>
              {st?.connected ? "Reconnect Kite" : "Connect Kite"}
            </button>
          </div>
          <ErrorText error={error} />
          <p className="note">
            Kite tokens expire around 06:00 IST each day, so connect once each trading morning. Set your Kite app's redirect
            URL to <code>https://YOUR-DOMAIN/api/broker/kite/callback</code>.
          </p>
        </Card>

        <Card title="Live index quotes">
          {lv?.ok ? (
            <>
              <Row k="Nifty 50"><span className="num">{num(lv.spot)}</span></Row>
              <Row k="India VIX"><span className="num">{num(lv.vix)}</span></Row>
              <Row k="As of">{lv.as_of?.replace("T", " ")}</Row>
              <p className="note">Outside market hours Kite returns the last close.</p>
            </>
          ) : (
            <p className="muted">{lv?.reason ?? "Loading…"}</p>
          )}
        </Card>
      </div>

      <Card title="Fallback: paste a request token">
        <p className="note" style={{ marginTop: 0 }}>
          If the redirect URL is not routed to this server (for example during local testing), log in at the Kite URL, then
          copy <code>request_token</code> from the address you land on and paste it here. Tokens are single-use.
        </p>
        <form onSubmit={submitToken} className="actions" style={{ marginTop: 0 }}>
          <input
            aria-label="Kite request token"
            placeholder="request_token"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            autoComplete="off"
            style={{ flex: "1 1 260px" }}
          />
          <button disabled={busy || token.trim().length < 8 || !st?.configured}>Exchange token</button>
        </form>
      </Card>
    </>
  );
}
