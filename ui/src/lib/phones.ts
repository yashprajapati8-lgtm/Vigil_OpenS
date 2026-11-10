import { useEffect, useState } from "react";
import { REPLAY } from "../replay/player";

// Phones on the demo checkout (/pay), from the storefront: who is connected and
// what their payments did. Polled, because a phone payment is rare and a 2 s
// delay on a toast is fine; the replay build has no storefront, so it never asks.

export interface PhoneDevice { id: string; label: string; seen_s: number; paid: number; failed: number }
export interface PhonePayment {
  id: number; ts: number; device: string; device_id: string; sku: string; amount: number;
  ok: boolean; ms: number; code: string; reason: string; order_id: string | null;
}
export interface PhoneStatus { now: number; connected: PhoneDevice[]; payments: PhonePayment[]; totals: { paid: number; failed: number } }

type Listener = (s: PhoneStatus | null, error: boolean) => void;
const listeners = new Set<Listener>();
let last: PhoneStatus | null = null;
let failed = false;
let timer: number | undefined;

async function poll() {
  try {
    const r = await fetch("/api/phone/status", { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    last = (await r.json()) as PhoneStatus;
    failed = false;
  } catch {
    failed = true;
  }
  for (const fn of listeners) fn(last, failed);
}

/** One shared 2 s poll for every component that wants phone data. */
export function usePhones(): { status: PhoneStatus | null; error: boolean } {
  const [state, setState] = useState<{ status: PhoneStatus | null; error: boolean }>({ status: last, error: failed });
  useEffect(() => {
    if (REPLAY) return;
    const fn: Listener = (s, e) => setState({ status: s, error: e });
    listeners.add(fn);
    if (listeners.size === 1) {
      void poll();
      timer = window.setInterval(poll, 2000);
    }
    return () => {
      listeners.delete(fn);
      if (listeners.size === 0) window.clearInterval(timer);
    };
  }, []);
  return state;
}

export const PRODUCT_NAMES: Record<string, string> = {
  "SKU-HEADPHONES": "Studio headphones",
  "SKU-KEYBOARD": "Mechanical keyboard",
  "SKU-MOUSE": "Wireless mouse",
};
export const rupees = (n: number) => `₹${n.toLocaleString("en-IN")}`;
