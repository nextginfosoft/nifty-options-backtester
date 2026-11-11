const inrFmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const numFmt = (d: number) => new Intl.NumberFormat("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });

export const inr = (n: number | null | undefined): string =>
  n == null ? "–" : (n < 0 ? "-₹" : "₹") + inrFmt.format(Math.abs(Math.round(n)));

export const num = (n: number | null | undefined, d = 2): string => (n == null ? "–" : numFmt(d).format(n));

export const pct = (n: number | null | undefined, d = 1): string => (n == null ? "–" : `${n.toFixed(d)}%`);

export const humanize = (s: string | null | undefined): string => (s ? s.replace(/_/g, " ") : "–");

export const todayISO = (): string => {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

export const nowIST = (): string =>
  new Date().toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" });

export const tone = (n: number | null | undefined): "good" | "bad" | "" =>
  n == null || n === 0 ? "" : n > 0 ? "good" : "bad";
