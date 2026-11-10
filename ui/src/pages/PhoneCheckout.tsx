import { Check, Headphones, Keyboard, LoaderCircle, Mouse, RotateCcw, X, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { CATEGORY_LABEL, duration } from "../lib/format";
import { REPLAY } from "../replay/player";

// The phone checkout (/pay). A person on a phone is one more customer of the demo
// shop: "Pay" sends a real checkout down storefront -> payment -> inventory, the
// path the load generator uses, so when VIGIL's services break this order breaks
// too. No money moves. The storefront answers with how it went and why it failed;
// the strip at the bottom shows what VIGIL is doing about it at that moment.

interface Product { sku: string; name: string; price: number; icon: LucideIcon }
const PRODUCTS: Product[] = [
  { sku: "SKU-HEADPHONES", name: "Studio headphones", price: 4999, icon: Headphones },
  { sku: "SKU-KEYBOARD", name: "Mechanical keyboard", price: 3499, icon: Keyboard },
  { sku: "SKU-MOUSE", name: "Wireless mouse", price: 1299, icon: Mouse },
];

interface PayResult {
  id: number; ok: boolean; ms: number; code: string; reason: string;
  order_id: string | null; amount: number; sku: string;
}
interface VigilSummary {
  online: boolean; autonomy: string | null; paused: boolean | null;
  incident: null | { id: string; status: string; root: string | null; category: string | null; title: string; mttr_s: number | null; closed_at: number | null };
}

const rupees = (n: number) => `₹${n.toLocaleString("en-IN")}`;

function deviceId(): string {
  const make = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(36).padStart(2, "0")).join("").slice(0, 20);
  try {
    const saved = localStorage.getItem("vigil.phone.id");
    if (saved && /^[A-Za-z0-9_-]{8,40}$/.test(saved)) return saved;
    const id = make();
    localStorage.setItem("vigil.phone.id", id);
    return id;
  } catch {
    return make();   // private mode: a fresh id for this visit
  }
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `The shop answered HTTP ${r.status}`);
  return data as T;
}

/** What the customer's order went through, from where the storefront said it failed. */
function steps(r: PayResult): Array<{ label: string; who: string; state: "ok" | "fail" | "skip" }> {
  const inv = r.ok ? "ok" : r.code.startsWith("inventory") ? "fail" : r.code === "payment_down" || r.code === "payment_timeout" ? "skip" : "ok";
  const pay = r.ok ? "ok" : r.code.startsWith("inventory") ? "skip" : "fail";
  return [
    { label: "Order placed", who: "storefront", state: "ok" },
    { label: "Stock reserved", who: "inventory", state: inv },
    { label: "Card charged", who: "payment", state: pay },
  ];
}

function vigilLine(v: VigilSummary | null): { tone: "ok" | "warn" | "bad" | "idle"; text: string } {
  if (!v) return { tone: "idle", text: "Connecting to VIGIL…" };
  if (!v.online) return { tone: "idle", text: "VIGIL is not reachable right now." };
  const i = v.incident;
  const cat = i?.category ? (CATEGORY_LABEL[i.category] ?? i.category).toLowerCase() : "";
  if (i && i.status === "RESOLVED") return { tone: "ok", text: `Fixed by VIGIL in ${duration(i.mttr_s ?? 0)}: ${i.root} (${cat}).` };
  if (i && i.status === "SELF_RESOLVED") return { tone: "ok", text: "The problem cleared up on its own. All services normal." };
  if (i && i.status === "ESCALATED") return { tone: "bad", text: `${i.id} was handed to a human engineer.` };
  if (i) {
    const what: Record<string, string> = {
      OPEN: "noticed customers failing, investigating",
      INVESTIGATING: "investigating which service is at fault",
      DIAGNOSED: v.paused ? `found the cause (${i.root}, ${cat}), but actions are paused` : `found the cause: ${i.root} (${cat})`,
      AWAITING_APPROVAL: `found the cause (${i.root}) and is waiting for a human to approve the fix`,
      REMEDIATING: `fixing ${i.root} now`,
      VERIFYING: "fix applied, checking that checkouts recover",
    };
    return { tone: i.status === "VERIFYING" ? "warn" : "bad", text: `${i.id}: ${what[i.status] ?? i.status.toLowerCase()}.` };
  }
  if (v.paused) return { tone: "warn", text: "All services normal. Actions are paused: VIGIL watches but won't fix anything." };
  return { tone: "ok", text: "All services normal. VIGIL is watching." };
}

export function PhoneCheckout() {
  const [product, setProduct] = useState<Product>(PRODUCTS[0]);
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<PayResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [vigil, setVigil] = useState<VigilSummary | null>(null);
  const id = useRef<string>("");
  if (!id.current) id.current = deviceId();

  useEffect(() => { document.title = "Lumen Goods | Checkout"; }, []);

  // Say hello (so the console lists this phone) and keep VIGIL's status fresh.
  useEffect(() => {
    if (REPLAY) return;
    let alive = true;
    const hello = () => { if (document.visibilityState === "visible") void postJson("/api/phone/hello", { device_id: id.current }).catch(() => {}); };
    const status = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const r = await fetch("/api/phone/vigil", { cache: "no-store" });
        if (alive && r.ok) setVigil(await r.json());
      } catch {
        if (alive) setVigil({ online: false, autonomy: null, paused: null, incident: null });
      }
    };
    hello(); void status();
    const h = window.setInterval(hello, 5000);
    const s = window.setInterval(status, 3000);
    return () => { alive = false; window.clearInterval(h); window.clearInterval(s); };
  }, []);

  useEffect(() => {
    if (!busy) return;
    const started = performance.now();
    const t = window.setInterval(() => setElapsed((performance.now() - started) / 1000), 100);
    return () => window.clearInterval(t);
  }, [busy]);

  const pay = useCallback(async () => {
    setBusy(true); setElapsed(0); setError(null); setResult(null);
    try {
      setResult(await postJson<PayResult>("/api/phone/pay", { device_id: id.current, sku: product.sku, amount: product.price }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't reach the shop.");
    } finally {
      setBusy(false);
    }
  }, [product]);

  if (REPLAY) {
    return (
      <main className="ph">
        <header className="ph-top"><span className="ph-brand">Lumen Goods</span><span className="ph-demo">Demo checkout</span></header>
        <section className="ph-card ph-note">
          <h1>Phone checkout runs on the live stack</h1>
          <p>This link plays back a recorded run, so there is no shop to pay. Run VIGIL with <code>docker compose up</code> and open this page from the console's phone panel.</p>
          <a className="btn btn-primary" href="/console">Open the console</a>
        </section>
      </main>
    );
  }

  const line = vigilLine(vigil);
  const Icon = product.icon;
  return (
    <main className="ph">
      <header className="ph-top">
        <span className="ph-brand">Lumen Goods</span>
        <span className="ph-demo" title="No real money moves. The order goes through VIGIL's demo services.">Demo · no real money</span>
      </header>

      {!result && !error && (
        <section className="ph-card" aria-labelledby="ph-title">
          <h1 id="ph-title" className="ph-title">Checkout</h1>
          <div className="ph-products" role="radiogroup" aria-label="Choose a product">
            {PRODUCTS.map((p) => {
              const PIcon = p.icon;
              return (
                <button key={p.sku} type="button" role="radio" aria-checked={p.sku === product.sku} className="ph-product"
                  disabled={busy} onClick={() => setProduct(p)}>
                  <span className="ph-product-icon" aria-hidden="true"><PIcon size={20} /></span>
                  <span className="ph-product-name">{p.name}</span>
                  <span className="ph-product-price">{rupees(p.price)}</span>
                </button>
              );
            })}
          </div>
          <div className="ph-total"><span>Total</span><b>{rupees(product.price)}</b></div>
          <button type="button" className="ph-pay" onClick={pay} disabled={busy} aria-live="polite">
            {busy ? <><LoaderCircle size={20} className="ph-spin" aria-hidden="true" /> Paying… {elapsed.toFixed(1)} s</> : <>Pay {rupees(product.price)}</>}
          </button>
          <p className="ph-fine"><Icon size={14} aria-hidden="true" /> Goes through storefront → payment → inventory, like every other customer.</p>
        </section>
      )}

      {result && (
        <section className={`ph-card ph-result ${result.ok ? "is-ok" : "is-bad"}`} role="status" aria-live="assertive">
          <div className="ph-mark" aria-hidden="true">
            {result.ok
              ? <svg viewBox="0 0 52 52"><circle cx="26" cy="26" r="24" /><path d="M15 27l7 7 15-16" /></svg>
              : <X size={34} />}
          </div>
          <h1 className="ph-result-title">{result.ok ? `Paid ${rupees(result.amount)}` : "Payment failed"}</h1>
          <p className="ph-result-sub">
            {result.ok ? <>Order {result.order_id} · {(result.ms / 1000).toFixed(2)} s</> : <>{result.reason} <span className="ph-ms">({(result.ms / 1000).toFixed(1)} s)</span></>}
          </p>
          <ol className="ph-steps">
            {steps(result).map((s) => (
              <li key={s.who} data-state={s.state}>
                <span className="ph-step-dot" aria-hidden="true">{s.state === "ok" ? <Check size={13} /> : s.state === "fail" ? <X size={13} /> : null}</span>
                <span>{s.label}</span><span className="ph-step-who">{s.who}</span>
              </li>
            ))}
          </ol>
          <button type="button" className="ph-pay ph-again" onClick={() => setResult(null)}>
            <RotateCcw size={18} aria-hidden="true" /> {result.ok ? "Buy again" : "Try again"}
          </button>
        </section>
      )}

      {error && (
        <section className="ph-card ph-result is-bad" role="alert">
          <div className="ph-mark" aria-hidden="true"><X size={34} /></div>
          <h1 className="ph-result-title">Not sent</h1>
          <p className="ph-result-sub">{error}</p>
          <button type="button" className="ph-pay ph-again" onClick={() => setError(null)}><RotateCcw size={18} aria-hidden="true" /> Back</button>
        </section>
      )}

      <footer className="ph-vigil" data-tone={line.tone} aria-live="polite">
        <span className="ph-vigil-label">Behind the scenes</span>
        <p><b>VIGIL</b> {line.text}</p>
        {vigil?.online && <span className="ph-vigil-meta">Autonomy {vigil.autonomy} · Actions {vigil.paused ? "paused" : "armed"}</span>}
      </footer>
    </main>
  );
}
