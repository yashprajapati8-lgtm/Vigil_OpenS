import { useMemo } from "react";
import type { Health } from "../api/types";
import { useVigil } from "../state/vigil";

// What the dashboard is watching, from vigil.yaml via /api/health. An older engine
// or a recorded replay has no `site`: that is always the demo shop.

export interface Site {
  name: string;
  entry: string;
  unit: string;            // "checkout", "result lookup"
  units: string;           // "checkouts", "result lookups"
  services: string[];      // watched, in vigil.yaml order
  edges: Array<[string, string]>;
  about: Record<string, string>;
  allowed: Record<string, string[]>;
  demo: boolean;
}

export const DEMO_SITE: Site = {
  name: "Demo shop",
  entry: "storefront",
  unit: "checkout",
  units: "checkouts",
  services: ["payment", "inventory"],
  edges: [["storefront", "payment"], ["payment", "inventory"]],
  about: { storefront: "Where customers check out", payment: "Charges the card", inventory: "Reserves the stock" },
  allowed: {},
  demo: true,
};

export function siteFrom(health: Health | null): Site {
  const s = health?.site;
  if (!s) return DEMO_SITE;
  const edges = (health?.edges ?? [])
    .map((e) => e.split("->") as [string, string])
    .filter((e) => e.length === 2 && e[0] && e[1]);
  return {
    name: s.name,
    entry: s.entry,
    unit: s.unit,
    units: s.units,
    services: s.services,
    edges: edges.length ? edges : DEMO_SITE.edges,
    about: s.about ?? {},
    allowed: s.allowed ?? {},
    demo: s.entry === DEMO_SITE.entry && s.services.join() === DEMO_SITE.services.join(),
  };
}

export function useSite(): Site {
  const { health } = useVigil();
  return useMemo(() => siteFrom(health), [health]);
}

/** One colour per watched service, stable by its position in vigil.yaml. The first
 *  two are the demo's payment (sand) and inventory (blue), so the demo looks as it did. */
const PALETTE = ["#e3c29b", "#8fb8ff", "#c6a7ef", "#9fd8a8", "#f2a7c3", "#9fd6e8", "#e8d48f", "#f0b48c"];

export function serviceColor(site: Site, service: string): string {
  const i = site.services.indexOf(service);
  return PALETTE[(i < 0 ? 0 : i) % PALETTE.length];
}

/** "checkout" -> "Checkout". */
export function upper(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** Call depth from the entry: column 0 is the entry, then each hop. Services the
 *  entry cannot reach (calls not seen yet) go in a last column, never dropped. */
export function columns(site: Site): string[][] {
  const depth = new Map<string, number>([[site.entry, 0]]);
  const queue = [site.entry];
  while (queue.length) {
    const from = queue.shift()!;
    for (const [a, b] of site.edges) {
      if (a === from && !depth.has(b) && (b === site.entry || site.services.includes(b))) {
        depth.set(b, depth.get(from)! + 1);
        queue.push(b);
      }
    }
  }
  const max = Math.max(0, ...depth.values());
  const cols: string[][] = Array.from({ length: max + 1 }, () => []);
  cols[0].push(site.entry);
  for (const svc of site.services) {
    const d = depth.get(svc);
    if (d != null) cols[d].push(svc);
  }
  const unreached = site.services.filter((s) => !depth.has(s));
  if (unreached.length) cols.push(unreached);
  return cols.filter((c) => c.length);
}
