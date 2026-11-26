import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Trade } from "./types";

/* Small hand-drawn SVG charts: no chart library. They draw at the real width of their box so text stays readable on a
   phone. Colours come from CSS variables so dark mode follows the page. */

const money = (n: number): string => {
  const a = Math.abs(n);
  const body = a >= 100000 ? `${(a / 100000).toFixed(a >= 1000000 ? 1 : 2)}L` : a >= 1000 ? `${(a / 1000).toFixed(a >= 10000 ? 0 : 1)}k` : `${Math.round(a)}`;
  return `${n < 0 ? "-" : ""}₹${body}`;
};
const rupees = (n: number): string => `${n < 0 ? "-" : ""}₹${Math.abs(Math.round(n)).toLocaleString("en-IN")}`;
const points = (n: number): string => Math.round(n).toLocaleString("en-IN");

export interface CurvePoint {
  label: string | null;
  date: string | null;
  cum_pnl: number;
  drawdown: number;
}
export interface TradeBar {
  trade_id: string | null;
  date: string | null;
  net: number;
  per_lot: number;
}

/** Measures the box the chart sits in so it can draw at 1:1 scale. */
function Sized({ children }: { children: (w: number) => ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => setW(Math.max(Math.round(el.clientWidth), 240));
    read();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return <div ref={ref} style={{ width: "100%" }}>{w > 0 ? children(w) : null}</div>;
}

function niceTicks(lo: number, hi: number, count = 4): number[] {
  if (hi === lo) return [lo];
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

interface Frame {
  w: number;
  h: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
}
const frame = (w: number, h: number, top = 12, bottom = 22): Frame => ({ w, h, left: w < 420 ? 46 : 56, right: 14, top, bottom });

function Axis({ f, lo, hi, ticks }: { f: Frame; lo: number; hi: number; ticks: number[] }) {
  const y = (v: number) => f.top + ((hi - v) / (hi - lo || 1)) * (f.h - f.top - f.bottom);
  return (
    <g>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={f.left} x2={f.w - f.right} y1={y(t)} y2={y(t)} className={t === 0 ? "ch-base" : "ch-grid"} />
          <text x={f.left - 8} y={y(t) + 4} textAnchor="end" className="ch-tick">{money(t)}</text>
        </g>
      ))}
    </g>
  );
}

function range(vals: number[]): [number, number] {
  const lo = Math.min(0, ...vals);
  const hi = Math.max(0, ...vals);
  const pad = (hi - lo || 1000) * 0.12;
  return [lo - (lo < 0 ? pad : 0), hi + pad];
}

export function EquityChart({ points: pts }: { points: CurvePoint[] }) {
  return (
    <Sized>
      {(w) => {
        const h = 200;
        const f = frame(w, h);
        const [a, b] = range(pts.map((p) => p.cum_pnl));
        const x = (i: number) => f.left + (pts.length === 1 ? 0 : (i / (pts.length - 1)) * (w - f.left - f.right));
        const y = (v: number) => f.top + ((b - v) / (b - a || 1)) * (h - f.top - f.bottom);
        const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.cum_pnl).toFixed(1)}`).join(" ");
        const area = `${line} L${x(pts.length - 1).toFixed(1)},${y(0).toFixed(1)} L${x(0).toFixed(1)},${y(0).toFixed(1)} Z`;
        const last = pts[pts.length - 1];
        return (
          <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`Cumulative profit after fees over ${pts.length - 1} closed trades, now ${rupees(last.cum_pnl)}`}>
            <Axis f={f} lo={a} hi={b} ticks={niceTicks(a, b)} />
            <path d={area} className="ch-area-blue" />
            <path d={line} className="ch-line-blue" />
            {pts.map((p, i) => (
              <circle key={i} cx={x(i)} cy={y(p.cum_pnl)} r={i === pts.length - 1 ? 5 : 10} className={i === pts.length - 1 ? "ch-dot-blue" : "ch-hit"}>
                <title>{`${p.label ?? ""}${p.date ? ` · ${p.date}` : ""} · cumulative ${rupees(p.cum_pnl)}`}</title>
              </circle>
            ))}
            <text x={Math.min(x(pts.length - 1), w - f.right)} y={y(last.cum_pnl) - 11} textAnchor="end" className="ch-label">{rupees(last.cum_pnl)}</text>
          </svg>
        );
      }}
    </Sized>
  );
}

export function DrawdownChart({ points: pts }: { points: CurvePoint[] }) {
  return (
    <Sized>
      {(w) => {
        const h = 110;
        const f = frame(w, h, 8, 8);
        const worst = Math.min(0, ...pts.map((p) => p.drawdown));
        const a = worst === 0 ? -1000 : worst * 1.15;
        const x = (i: number) => f.left + (pts.length === 1 ? 0 : (i / (pts.length - 1)) * (w - f.left - f.right));
        const y = (v: number) => f.top + ((0 - v) / (0 - a)) * (h - f.top - f.bottom);
        const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.drawdown).toFixed(1)}`).join(" ");
        const area = `${line} L${x(pts.length - 1).toFixed(1)},${y(0).toFixed(1)} L${x(0).toFixed(1)},${y(0).toFixed(1)} Z`;
        return (
          <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`Drawdown from the previous peak; worst so far ${rupees(worst)}`}>
            <Axis f={f} lo={a} hi={0} ticks={worst === 0 ? [0] : [0, Math.round(worst / 100) * 100]} />
            <path d={area} className="ch-area-red" />
            <path d={line} className="ch-line-red" />
            {pts.map((p, i) => (
              <circle key={i} cx={x(i)} cy={y(p.drawdown)} r={10} className="ch-hit">
                <title>{`${p.label ?? ""}${p.date ? ` · ${p.date}` : ""} · ${rupees(p.drawdown)} below the peak`}</title>
              </circle>
            ))}
          </svg>
        );
      }}
    </Sized>
  );
}

export function TradeBars({ bars }: { bars: TradeBar[] }) {
  return (
    <Sized>
      {(w) => {
        const h = 190;
        const f = frame(w, h);
        const [a, b] = range(bars.map((t) => t.net));
        const y = (v: number) => f.top + ((b - v) / (b - a || 1)) * (h - f.top - f.bottom);
        const slot = (w - f.left - f.right) / bars.length;
        const bw = Math.min(26, slot * 0.7);
        return (
          <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`Profit or loss for each of ${bars.length} closed trades`}>
            <Axis f={f} lo={a} hi={b} ticks={niceTicks(a, b)} />
            {bars.map((t, i) => {
              const cx = f.left + slot * (i + 0.5);
              const top = Math.min(y(t.net), y(0));
              const hh = Math.max(Math.abs(y(t.net) - y(0)), 1);
              return (
                <g key={`${t.trade_id}-${i}`}>
                  <rect x={cx - bw / 2} y={top} width={bw} height={hh} rx={3} className={t.net >= 0 ? "ch-bar-blue" : "ch-bar-red"}>
                    <title>{`${t.trade_id ?? ""}${t.date ? ` · ${t.date}` : ""} · ${rupees(t.net)} (${rupees(t.per_lot)} per lot)`}</title>
                  </rect>
                  {slot >= 30 ? <text x={cx} y={h - 6} textAnchor="middle" className="ch-tick">{(t.trade_id ?? "").split("-").pop()}</text> : null}
                </g>
              );
            })}
          </svg>
        );
      }}
    </Sized>
  );
}

interface Tag {
  v: number;
  text: string;
  strong: boolean;
}

/** Where Nifty sits against the strikes of an open credit spread / iron condor, with break-evens. */
export function PositionRange({ trade, spot }: { trade: Trade; spot: number | null | undefined }) {
  const pick = (type: "PE" | "CE", action: "SELL" | "BUY") => trade.legs.find((l) => l.option_type === type && l.action === action)?.strike;
  const shortP = pick("PE", "SELL");
  const longP = pick("PE", "BUY");
  const shortC = pick("CE", "SELL");
  const longC = pick("CE", "BUY");
  const credit = trade.entry_credit;
  const beLow = shortP != null ? shortP - credit : undefined;
  const beHigh = shortC != null ? shortC + credit : undefined;
  const marks = [shortP, longP, shortC, longC, beLow, beHigh, spot ?? undefined].filter((v): v is number => v != null);
  if (shortP == null && shortC == null) return null;

  const span = Math.max(Math.max(...marks) - Math.min(...marks), 300);
  const dMin = Math.min(...marks) - span * 0.12;
  const dMax = Math.max(...marks) + span * 0.12;

  const zones: { from: number; to: number; cls: string }[] = [{ from: shortP ?? dMin, to: shortC ?? dMax, cls: "ch-zone-keep" }];
  if (shortP != null) {
    zones.push({ from: longP ?? dMin, to: shortP, cls: "ch-zone-part" });
    if (longP != null) zones.push({ from: dMin, to: longP, cls: "ch-zone-loss" });
  }
  if (shortC != null) {
    zones.push({ from: shortC, to: longC ?? dMax, cls: "ch-zone-part" });
    if (longC != null) zones.push({ from: longC, to: dMax, cls: "ch-zone-loss" });
  }

  const tags: Tag[] = [];
  if (longP != null) tags.push({ v: longP, text: `Long ${points(longP)}`, strong: true });
  if (shortP != null) tags.push({ v: shortP, text: `Short ${points(shortP)}`, strong: true });
  if (shortC != null) tags.push({ v: shortC, text: `Short ${points(shortC)}`, strong: true });
  if (longC != null) tags.push({ v: longC, text: `Long ${points(longC)}`, strong: true });
  if (beLow != null) tags.push({ v: beLow, text: `Break-even ${points(beLow)}`, strong: false });
  if (beHigh != null) tags.push({ v: beHigh, text: `Break-even ${points(beHigh)}`, strong: false });

  let sentence = "Waiting for a live Nifty price.";
  if (spot != null) {
    const sides: { gap: number; strike: number; dir: string }[] = [];
    if (shortP != null) sides.push({ gap: spot - shortP, strike: shortP, dir: "above" });
    if (shortC != null) sides.push({ gap: shortC - spot, strike: shortC, dir: "below" });
    const near = sides.reduce((m, s) => (Math.abs(s.gap) < Math.abs(m.gap) ? s : m));
    const pct = (Math.abs(near.gap) / spot) * 100;
    const beBreached = (beLow != null && spot < beLow) || (beHigh != null && spot > beHigh);
    sentence =
      near.gap >= 0
        ? `Nifty ${points(spot)} is ${points(Math.abs(near.gap))} points (${pct.toFixed(1)}%) ${near.dir} the short strike ${points(near.strike)}.`
        : `Nifty ${points(spot)} has crossed the short strike ${points(near.strike)} by ${points(Math.abs(near.gap))} points (${pct.toFixed(1)}%).`;
    if (beBreached) sentence += " It is beyond the break-even, so the trade is losing at expiry prices.";
  }

  return (
    <div>
      <Sized>
        {(w) => {
          const x0 = 14;
          const x1 = w - 14;
          const x = (v: number) => x0 + ((v - dMin) / (dMax - dMin)) * (x1 - x0);
          const bar = { y: 44, h: 18 };
          // place each label on the first row where it does not overlap an earlier one
          const rows: number[] = [];
          const placed = [...tags].sort((p, q) => p.v - q.v).map((t) => {
            const half = t.text.length * 3.4 + 5;
            const cx = Math.min(Math.max(x(t.v), half), w - half);
            let r = 0;
            while (rows[r] !== undefined && cx - half < rows[r]) r += 1;
            rows[r] = cx + half;
            return { ...t, cx, row: r };
          });
          const h = bar.y + bar.h + 18 + rows.length * 16 + 4;
          return (
            <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`Position range. ${sentence}`}>
              {zones.map((z, i) => (
                <rect key={i} x={x(z.from)} y={bar.y} width={Math.max(x(z.to) - x(z.from), 0)} height={bar.h} className={z.cls} />
              ))}
              {tags.map((t, i) => (
                <line key={`l${i}`} x1={x(t.v)} x2={x(t.v)} y1={bar.y - 5} y2={bar.y + bar.h + 5} className={t.strong ? "ch-strike" : "ch-be"} />
              ))}
              {placed.map((t, i) => (
                <text key={`t${i}`} x={t.cx} y={bar.y + bar.h + 18 + t.row * 16} textAnchor="middle" className={t.strong ? "ch-tick ch-tick-strong" : "ch-tick"}>{t.text}</text>
              ))}
              {spot != null ? (
                <g>
                  <circle cx={x(spot)} cy={bar.y + bar.h / 2} r={7} className="ch-dot-spot" />
                  <text x={Math.min(Math.max(x(spot), 56), w - 56)} y={bar.y - 14} textAnchor="middle" className="ch-label">{`Nifty ${points(spot)}`}</text>
                </g>
              ) : null}
            </svg>
          );
        }}
      </Sized>
      <div className="note" style={{ marginTop: 0 }}>{sentence}</div>
      <div className="legend">
        <span><i className="sw ch-zone-keep" />Keeps the full credit</span>
        <span><i className="sw ch-zone-part" />Partial loss</span>
        <span><i className="sw ch-zone-loss" />Maximum loss</span>
      </div>
    </div>
  );
}
