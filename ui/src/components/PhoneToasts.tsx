import { Smartphone, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { usePhones, rupees, PRODUCT_NAMES, type PhonePayment } from "../lib/phones";

// A short card in the corner of the console the moment someone pays from a phone,
// so the audience sees their tap land in VIGIL's world. Only payments made while
// the console is open; the full list is on the phone page.

const SHOW_MS = 6000;

export function PhoneToasts() {
  const { status } = usePhones();
  const seen = useRef<number | null>(null);
  const [toasts, setToasts] = useState<PhonePayment[]>([]);

  useEffect(() => {
    if (!status) return;
    const newest = status.payments[0]?.id ?? 0;
    if (seen.current === null) { seen.current = newest; return; }   // don't replay old payments on load
    const fresh = status.payments.filter((p) => p.id > (seen.current ?? 0)).reverse();
    if (!fresh.length) return;
    seen.current = newest;
    setToasts((t) => [...fresh, ...t].slice(0, 3));
    for (const p of fresh) window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== p.id)), SHOW_MS);
  }, [status]);

  if (!toasts.length) return null;
  return (
    <div className="phone-toasts" aria-live="polite">
      {toasts.map((p) => (
        <div key={p.id} className="phone-toast" data-ok={p.ok}>
          <span className="phone-toast-icon" aria-hidden="true"><Smartphone size={18} /></span>
          <div className="phone-toast-body">
            <b>{p.ok ? `Phone payment received · ${rupees(p.amount)}` : "Phone payment failed"}</b>
            <span>
              {p.ok ? `${PRODUCT_NAMES[p.sku] ?? p.sku} · ${(p.ms / 1000).toFixed(2)} s · ${p.device}` : `${p.reason} · ${p.device}`}
            </span>
          </div>
          <button type="button" className="phone-toast-close" aria-label="Dismiss" onClick={() => setToasts((t) => t.filter((x) => x.id !== p.id))}>
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
