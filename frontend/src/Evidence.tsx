import { useApi } from "./api";
import { inr, num, pct, humanize, tone } from "./format";
import type { EvidenceView } from "./types";
import { Banner, Card, Empty, ErrorText, Pill, Row } from "./ui";

const VERDICT_TONE: Record<string, "good" | "bad" | "warn"> = {
  POSITIVE: "good",
  NEGATIVE: "bad",
  INCONCLUSIVE: "warn",
  EARLY: "warn",
  TOO_EARLY: "warn",
  NO_DATA: "warn",
};

export default function Evidence() {
  const ev = useApi<EvidenceView>("/api/auto/evidence", 60000);
  const e = ev.data;
  if (ev.error) return <ErrorText error={ev.error} />;
  if (!e) return <div className="muted">Loading…</div>;

  const pl = e.per_lot;
  return (
    <>
      <Banner tone={VERDICT_TONE[e.verdict.code] ?? "warn"}>{e.verdict.text}</Banner>

      <div className="cols">
        <Card title="Auto paper trades so far">
          {e.n === 0 ? (
            <Empty>Nothing closed yet. This page fills in as the auto trader closes trades.</Empty>
          ) : (
            <>
              <Row k="Closed trades">{e.n}</Row>
              <Row k="Win rate">{pct(e.win_rate_pct)} ({e.wins} of {e.n})</Row>
              <Row k="Net P&L after fees"><span className={tone(e.total_net)}>{inr(e.total_net)}</span></Row>
              <Row k="Fees paid">{inr(e.fees_total)}</Row>
              <Row k="Average per trade"><span className={tone(e.avg_per_trade)}>{inr(e.avg_per_trade)}</span></Row>
              <Row k="Average win / loss">{inr(e.avg_win)} / {inr(e.avg_loss)}</Row>
              <Row k="Best / worst trade">{inr(e.best_trade)} / {inr(e.worst_trade)}</Row>
              <Row k="Profit factor">{e.profit_factor == null ? "–" : num(e.profit_factor)}</Row>
              <Row k="Average per lot">
                <span className={tone(pl?.mean)}>{inr(pl?.mean)}</span>
                {pl?.ci95 ? <span className="muted"> (95% range {inr(pl.ci95[0])} to {inr(pl.ci95[1])})</span> : <span className="muted"> (no range yet)</span>}
              </Row>
              {e.trades_needed_for_precision?.trades ? (
                <p className="note">
                  At the current spread, about {e.trades_needed_for_precision.trades} closed trades are needed to pin the average down to
                  within ±{inr(e.trades_needed_for_precision.target_rs_per_lot)} per lot.
                </p>
              ) : null}
            </>
          )}
        </Card>

        <Card title="What the backtests claimed (per lot)">
          {e.references.map((r) => (
            <Row k={r.name} key={r.name}>
              <span className={tone(r.rs_per_lot)}>{inr(r.rs_per_lot)}</span>{" "}
              <Pill tone={r.weight === "weak" ? "warn" : ""}>{r.weight === "weak" ? "weak evidence" : "stronger evidence"}</Pill>
              {r.note ? <div className="muted">{r.note}</div> : null}
            </Row>
          ))}
          <p className="note">
            Only the real-price backtests used actual NSE prices. They found no edge. Live paper results above are the first independent check.
          </p>
        </Card>
      </div>

      {e.n > 0 ? (
        <div className="cols">
          <Card title="By strategy">
            {e.by_strategy.map((s) => (
              <Row k={humanize(s.strategy)} key={s.strategy}>{s.n} trade(s), <span className={tone(s.net)}>{inr(s.net)}</span></Row>
            ))}
          </Card>
          <Card title="How trades ended">
            {e.by_exit_reason.map((r) => (
              <Row k={humanize(r.reason)} key={r.reason}>{r.n}</Row>
            ))}
          </Card>
        </div>
      ) : null}

      <Card title="Read this before trusting any number">
        <ul className="note" style={{ margin: 0, paddingLeft: 18 }}>
          {e.caveats.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      </Card>
    </>
  );
}
