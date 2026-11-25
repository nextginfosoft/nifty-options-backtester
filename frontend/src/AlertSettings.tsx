import { useState, type FormEvent } from "react";
import { api, useApi } from "./api";
import type { AlertStatus } from "./types";
import { Card, ErrorText, Pill } from "./ui";

type Reply = AlertStatus & { ok?: boolean; detail?: string };

export default function AlertSettings() {
  const view = useApi<AlertStatus>("/api/auto/alerts", 30000);
  const [token, setToken] = useState("");
  const [chat, setChat] = useState("");
  const [busy, setBusy] = useState<"save" | "detect" | "test" | "remove" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const v = view.data;
  const disabled = !v || !v.encryption_available;

  async function run(kind: NonNullable<typeof busy>, path: string, method: string, body: unknown, done: (r: Reply) => string | null) {
    setBusy(kind);
    setError(null);
    setMessage(null);
    try {
      const r = await api<Reply>(path, { method, body });
      const text = done(r);
      if (r.ok === false) setError(r.detail ?? "Failed");
      else if (text) setMessage(text);
      view.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  function save(e: FormEvent) {
    e.preventDefault();
    void run("save", "/api/auto/alerts", "PUT", { bot_token: token, chat_id: chat }, () => {
      setToken(""); // never keep the secret in page state after saving
      setChat("");
      return "Saved. The token is encrypted on the server and cannot be shown again.";
    });
  }

  function remove() {
    if (!window.confirm("Remove the saved Telegram bot token and chat id?")) return;
    void run("remove", "/api/auto/alerts", "DELETE", undefined, () => "Removed.");
  }

  return (
    <Card title="Telegram alerts">
      <p className="note" style={{ marginTop: 0 }}>
        Get a message for each entry, exit, pause and problem. Setup: in Telegram open <strong>@BotFather</strong> → /newbot → copy the
        token it gives you and paste it below → Save. Then open your new bot, press <strong>Start</strong>, and click <strong>Find my chat ID</strong>.
      </p>
      <div>
        <Pill tone={v?.configured ? "good" : "warn"}>{v?.configured ? "On" : "Off"}</Pill>{" "}
        {v?.configured ? (
          <span className="muted">
            {v.source === "environment" ? "using the server environment" : "using the saved settings"}
            {v.chat_id_masked ? ` · chat ${v.chat_id_masked}` : ""}
            {v.last_ok === false ? ` · last send failed (${v.last_error})` : v.last_ok ? " · last send ok" : ""}
          </span>
        ) : (
          <span className="muted">
            {v && !v.encryption_available ? "Saving is off: set CREDENTIALS_KEY on the server." : "Needs a bot token and a chat id."}
          </span>
        )}
      </div>

      <form onSubmit={save} autoComplete="off">
        <div className="form-grid">
          <label className="field">
            <span>Bot token {v?.saved.bot_token ? <Pill tone="good">saved</Pill> : null}</span>
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder={v?.saved.bot_token ? "Leave blank to keep the saved token" : "123456789:AA… from @BotFather"}
              autoComplete="off"
              spellCheck={false}
              disabled={disabled}
              maxLength={120}
            />
          </label>
          <label className="field">
            <span>Chat ID {v?.saved.chat_id ? <Pill tone="good">saved · {v.chat_id_masked}</Pill> : null}</span>
            <input
              value={chat}
              onChange={(e) => setChat(e.target.value)}
              placeholder={v?.saved.chat_id ? "Leave blank to keep the saved chat" : "Number, or use “Find my chat ID”"}
              autoComplete="off"
              spellCheck={false}
              disabled={disabled}
              maxLength={40}
            />
          </label>
        </div>
        <ErrorText error={error} />
        {message ? <p className="good" role="status" style={{ margin: "10px 0 0" }}>{message}</p> : null}
        <div className="actions">
          <button className="primary" disabled={disabled || busy !== null || (!token && !chat)}>{busy === "save" ? "Saving…" : "Save"}</button>
          <button
            type="button"
            disabled={disabled || busy !== null || !v?.saved.bot_token}
            onClick={() => void run("detect", "/api/auto/alerts/detect-chat", "POST", {}, (r) => (r.ok ? "Found your chat and saved it." : null))}
          >
            {busy === "detect" ? "Looking…" : "Find my chat ID"}
          </button>
          <button
            type="button"
            disabled={busy !== null || !v?.configured}
            onClick={() => void run("test", "/api/auto/alerts/test", "POST", {}, (r) => (r.ok ? "Test alert sent. Check Telegram." : null))}
          >
            {busy === "test" ? "Sending…" : "Send test alert"}
          </button>
          <button type="button" className="danger" disabled={busy !== null || !(v?.saved.bot_token || v?.saved.chat_id)} onClick={remove}>
            Remove
          </button>
        </div>
      </form>
    </Card>
  );
}
