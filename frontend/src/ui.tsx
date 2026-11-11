import type { ReactNode } from "react";

export function Card({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      <div className="card-head">
        <h2>{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className={`value ${tone ?? ""}`}>{value}</div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  );
}

export function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="row">
      <span className="k">{k}</span>
      <span className="v">{children}</span>
    </div>
  );
}

export function Pill({ tone, children }: { tone?: "good" | "bad" | "warn" | ""; children: ReactNode }) {
  return <span className={`pill ${tone ?? ""}`}>{children}</span>;
}

export function Banner({ tone, children }: { tone?: "good" | "bad" | "warn"; children: ReactNode }) {
  return (
    <div className={`banner ${tone ?? ""}`} role={tone === "bad" ? "alert" : "status"}>
      <span className={`dot ${tone ?? ""}`} />
      <div>{children}</div>
    </div>
  );
}

export function ErrorText({ error }: { error: string | null }) {
  return error ? <div className="error-text" role="alert">{error}</div> : null;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
