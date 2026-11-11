import { useCallback, useEffect, useRef, useState } from "react";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d
        .map((e) => {
          const loc = Array.isArray(e?.loc) ? e.loc.slice(1).join(".") : "";
          return loc ? `${loc}: ${e?.msg}` : String(e?.msg ?? e);
        })
        .join("; ");
    }
  }
  return fallback;
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = init.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-Requested-With"] = "dashboard";
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth/")) onUnauthorized();
    throw new ApiError(res.status, detailOf(body, `${res.status} ${res.statusText}`));
  }
  return body as T;
}

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Fetch a GET endpoint; refetches when `path` changes and every `refreshMs` (if given). */
export function useApi<T>(path: string | null, refreshMs?: number): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const [tick, setTick] = useState(0);
  const seq = useRef(0);

  const reload = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    if (path === null) {
      setData(null);
      setLoading(false);
      return;
    }
    const mine = ++seq.current;
    setLoading(true);
    api<T>(path)
      .then((d) => {
        if (seq.current === mine) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (seq.current === mine) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (seq.current === mine) setLoading(false);
      });
  }, [path, tick]);

  useEffect(() => {
    if (!refreshMs || path === null) return;
    const id = setInterval(reload, refreshMs);
    return () => clearInterval(id);
  }, [path, refreshMs, reload]);

  return { data, error, loading, reload };
}
