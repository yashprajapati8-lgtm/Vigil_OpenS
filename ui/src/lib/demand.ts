import type { Verdict } from "../api/types";

/** Busy because of more traffic, not broken: requests way up, CPU per request normal.
 *  Same rule the engine uses to avoid opening incidents for real demand. */
export function isDemand(v: Verdict | undefined): boolean {
  if (!v) return false;
  if (v.demand) return true;   // the engine sees a surge of customers and every checkout served
  const z = v.z ?? {};
  return (z.rps ?? 0) > 3 && (z.cpu_per_req ?? 0) < 2;
}

/** "5×": current requests per second over the learned normal. */
export function trafficMultiple(v: Verdict | undefined, rps: number | null | undefined): string | null {
  const base = v?.baseline?.medians?.rps;
  if (!base || rps == null) return null;
  return `${Math.max(1, Math.round(rps / base))}×`;
}
