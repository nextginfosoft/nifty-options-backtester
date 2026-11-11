import { useState, type FormEvent } from "react";
import { api, useApi } from "./api";
import type { CredentialField, CredentialsView, LoginResult } from "./types";
import { Banner, Card, ErrorText, Pill } from "./ui";

const FIELDS: { name: CredentialField; label: string; type: "text" | "password"; autoComplete: string; hint: string }[] = [
  { name: "user_id", label: "Zerodha User ID", type: "text", autoComplete: "off", hint: "e.g. AB1234" },
  { name: "password", label: "Zerodha login password", type: "password", autoComplete: "new-password", hint: "Your Kite web login password" },
  { name: "api_key", label: "Kite Connect API key", type: "password", autoComplete: "off", hint: "From developers.kite.trade" },
  { name: "api_secret", label: "Kite Connect API secret", type: "password", autoComplete: "off", hint: "From developers.kite.trade" },
  { name: "totp_secret", label: "TOTP secret key", type: "password", autoComplete: "off", hint: "The base32 key shown when you set up the authenticator (not the 6-digit code)" },
];

const BLANK: Record<CredentialField, string> = { user_id: "", password: "", api_key: "", api_secret: "", totp_secret: "" };

function LoginOutcome({ r }: { r: LoginResult }) {
  return (
    <div className="note" style={{ marginTop: 8 }}>
      Last login {r.automatic ? "(automatic)" : "(manual)"}: <Pill tone={r.ok ? "good" : "bad"}>{r.ok ? "success" : "failed"}</Pill>{" "}
      {r.message} · {new Date(r.at).toLocaleString()}
    </div>
  );
}

export default function Credentials({ onChanged }: { onChanged: () => void }) {
  const view = useApi<CredentialsView>("/api/broker/credentials", 30000);
  const [values, setValues] = useState<Record<CredentialField, string>>(BLANK);
  const [autoLogin, setAutoLogin] = useState<boolean | null>(null);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState<"save" | "login" | "remove" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const v = view.data;
  const disabled = !v || !v.encryption_available;
  const sensitiveTyped = Boolean(values.user_id || values.password || values.totp_secret);
  const effectiveAuto = autoLogin ?? v?.auto_login_enabled ?? false;
  const needsAck = sensitiveTyped || (autoLogin === true);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy("save");
    setError(null);
    setMessage(null);
    try {
      await api<CredentialsView>("/api/broker/credentials", {
        method: "PUT",
        body: { ...values, auto_login: autoLogin, accept_risk: ack },
      });
      setValues(BLANK); // never keep secrets in page state after saving
      setAck(false);
      setAutoLogin(null);
      setMessage("Saved. Values are encrypted on the server and cannot be shown again.");
      view.reload();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function loginNow() {
    setBusy("login");
    setError(null);
    setMessage(null);
    try {
      const r = await api<LoginResult>("/api/broker/kite/auto-login", { method: "POST" });
      if (r.ok) setMessage("Connected for today's session.");
      else setError(r.message);
      view.reload();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function remove() {
    if (!window.confirm("Remove all saved Zerodha credentials and disconnect Kite on this server?")) return;
    setBusy("remove");
    setError(null);
    setMessage(null);
    try {
      await api<CredentialsView>("/api/broker/credentials", { method: "DELETE" });
      setValues(BLANK);
      setMessage("All saved credentials and today's Kite session were removed.");
      view.reload();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const anySaved = v ? Object.values(v.saved).some(Boolean) : false;
  const allSaved = v ? Object.values(v.saved).every(Boolean) : false;

  return (
    <Card title="Zerodha credentials">
      {v && !v.encryption_available ? (
        <Banner tone="warn">
          Storing credentials is off. Set <code>CREDENTIALS_KEY</code> (32+ characters) in the server environment, then restart.
        </Banner>
      ) : null}
      {v?.error ? <Banner tone="bad">{v.error}</Banner> : null}
      {v?.key_source === "dev key file" ? (
        <p className="note" style={{ marginTop: 0 }}>
          Development mode: the encryption key sits beside the data, so this is not real at-rest protection. In production the
          key comes from the server environment.
        </p>
      ) : null}

      <div className="banner warn" style={{ marginBottom: 12 }}>
        <span className="dot warn" />
        <div>
          <strong>Your password and TOTP secret together give full login access to your Zerodha account.</strong> Anyone who
          compromises this server could use them. Zerodha does not officially support automated login and may block it.
          Failed attempts can lock your account, so the app tries at most twice a day. The API key and secret alone are enough
          for the manual <em>Connect Kite</em> flow above.
        </div>
      </div>

      <form onSubmit={save} autoComplete="off">
        <div className="form-grid">
          {FIELDS.map((f) => (
            <label className="field" key={f.name}>
              <span>
                {f.label} {v?.saved[f.name] ? <Pill tone="good">saved{f.name === "user_id" && v.user_id_masked ? ` · ${v.user_id_masked}` : ""}</Pill> : null}
              </span>
              <input
                type={f.type}
                value={values[f.name]}
                onChange={(e) => setValues((s) => ({ ...s, [f.name]: e.target.value }))}
                placeholder={v?.saved[f.name] ? "Leave blank to keep the saved value" : f.hint}
                autoComplete={f.autoComplete}
                spellCheck={false}
                disabled={disabled}
                maxLength={f.name === "user_id" ? 32 : 128}
              />
            </label>
          ))}
        </div>

        <div style={{ display: "grid", gap: 8, marginTop: 14 }}>
          <label style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
            <input
              type="checkbox"
              checked={effectiveAuto}
              onChange={(e) => setAutoLogin(e.target.checked)}
              disabled={disabled}
              style={{ minHeight: 0, marginTop: 3 }}
            />
            <span>Log in automatically each morning (after 06:30 IST) so Kite stays connected. Needs all five fields saved.</span>
          </label>
          <label style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
            <input
              type="checkbox"
              checked={ack}
              onChange={(e) => setAck(e.target.checked)}
              disabled={disabled}
              style={{ minHeight: 0, marginTop: 3 }}
            />
            <span>
              I understand the risk above and want this server to store my Zerodha login credentials.
              {needsAck && !ack ? <span className="warn"> (required to save these)</span> : null}
            </span>
          </label>
        </div>

        <ErrorText error={error} />
        {message ? <p className="good" role="status" style={{ margin: "10px 0 0" }}>{message}</p> : null}

        <div className="actions">
          <button className="primary" disabled={disabled || busy !== null || (needsAck && !ack)}>
            {busy === "save" ? "Saving…" : "Save credentials"}
          </button>
          <button type="button" onClick={loginNow} disabled={disabled || busy !== null || !allSaved}>
            {busy === "login" ? "Logging in…" : "Log in now"}
          </button>
          <button type="button" className="danger" onClick={remove} disabled={busy !== null || !anySaved}>
            Remove credentials
          </button>
        </div>
      </form>

      {v?.auto_login_blocked ? (
        <p className="error-text" style={{ marginBottom: 0 }}>
          Automatic login is paused after Zerodha rejected the credentials. Update them to resume.
        </p>
      ) : null}
      {v?.last_login ? <LoginOutcome r={v.last_login} /> : null}
      {v?.updated_at ? <p className="note" style={{ marginBottom: 0 }}>Credentials last updated {new Date(v.updated_at).toLocaleString()}.</p> : null}
    </Card>
  );
}
