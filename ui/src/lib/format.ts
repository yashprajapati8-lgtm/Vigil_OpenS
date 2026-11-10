export const CATEGORY_LABEL: Record<string, string> = {
  crashed: "crashed",
  resource_leak: "memory leak",
  stuck_process: "stuck process",
  load_saturation: "load saturation",
  regression_after_change: "regression after a deploy",
  unknown: "unknown cause",
};

export function ms(v: number | null | undefined): string {
  if (v == null) return "–";
  return v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`;
}

export function pct(v: number | null | undefined, digits = 0): string {
  if (v == null) return "–";
  const p = v * 100;
  return `${p < 10 && digits === 0 && p > 0 ? p.toFixed(1) : p.toFixed(digits)}%`;
}

export function num(v: number | null | undefined, digits = 0): string {
  if (v == null) return "–";
  return v.toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false });
}

export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(s % 60).padStart(2, "0")}s`;
}

export function since(ts: number, now = Date.now() / 1000): string {
  return `${duration(now - ts)} ago`;
}

/** Sentence-case a fragment the engine wrote in lower case. */
export function cap(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}
