import { Headphones, Keyboard, Monitor, Mouse, Webcam, type LucideIcon } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { REPLAY } from "../replay/player";

// A tiny shop that sends real checkouts through payment -> inventory, the same
// path the load generator uses. When VIGIL's services break, these orders fail.

interface Product { sku: string; name: string; price: number; icon: LucideIcon }
const PRODUCTS: Product[] = [
  { sku: "SKU-HEADPHONES", name: "Studio headphones", price: 4999, icon: Headphones },
  { sku: "SKU-KEYBOARD", name: "Mechanical keyboard", price: 3499, icon: Keyboard },
  { sku: "SKU-MONITOR", name: "27-inch monitor", price: 18999, icon: Monitor },
  { sku: "SKU-MOUSE", name: "Wireless mouse", price: 1299, icon: Mouse },
  { sku: "SKU-WEBCAM", name: "HD webcam", price: 2799, icon: Webcam },
];

interface Order { id: number; name: string; ok: boolean; text: string; ms: number; at: string }

export function Store() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  const buy = async (p: Product) => {
    setBusy(p.sku);
    const started = performance.now();
    let order: Omit<Order, "id">;
    try {
      const r = await api.post<{ order_id: string }>("/api/store/pay", { sku: p.sku, qty: 1 });
      order = { name: p.name, ok: true, text: `Order ${r.order_id} placed`, ms: performance.now() - started, at: new Date().toLocaleTimeString("en-GB") };
    } catch (e) {
      order = { name: p.name, ok: false, text: `Checkout failed: ${e instanceof Error ? e.message : "unknown error"}`, ms: performance.now() - started, at: new Date().toLocaleTimeString("en-GB") };
    }
    setOrders((o) => [{ id: Date.now(), ...order }, ...o].slice(0, 8));
    setBusy(null);
  };

  const last = orders[0];
  return (
    <div style={{ maxWidth: 1080, margin: "0 auto", padding: "28px 20px 48px" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16, flexWrap: "wrap", marginBottom: 24 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 28, fontWeight: 600 }}>Lumen Goods</h1>
          <p style={{ margin: "4px 0 0", color: "var(--text-2)" }}>A demo shop. Every purchase is a real checkout through payment and inventory.</p>
        </div>
        <a className="btn" href="/console" target="_blank" rel="noreferrer">Open VIGIL</a>
      </header>

      {REPLAY && (
        <div className="banner banner-warn" role="status" style={{ marginBottom: 18 }}>
          This is the replay build of VIGIL. The shop sends real checkouts through payment and inventory, so it only works on the live stack.
        </div>
      )}
      {last && (
        <div className={`banner ${last.ok ? "" : "banner-bad"}`} role="status"
          style={last.ok ? { background: "#12301f", border: "1px solid var(--ok-mid)", color: "#d6f0e1", marginBottom: 18 } : { marginBottom: 18 }}>
          {last.name}: {last.text} in {Math.round(last.ms)} ms
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))", gap: 16 }}>
        {PRODUCTS.map((p) => {
          const Icon = p.icon;
          return (
            <article key={p.sku} className="panel" style={{ display: "flex", flexDirection: "column", gap: 14, padding: 18 }}>
              <div style={{ height: 120, borderRadius: 14, background: "linear-gradient(160deg, #2a2421, #151211)", display: "grid", placeItems: "center", color: "#d9cfc8" }}>
                <Icon size={46} strokeWidth={1.4} aria-hidden="true" />
              </div>
              <div>
                <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>{p.name}</h2>
                <p className="num" style={{ margin: "2px 0 0", color: "var(--text-2)" }}>₹{p.price.toLocaleString("en-IN")}</p>
              </div>
              <button type="button" className="btn btn-primary" disabled={REPLAY || busy === p.sku} onClick={() => buy(p)}>
                {busy === p.sku ? "Placing order" : "Buy now"}
              </button>
            </article>
          );
        })}
      </div>

      <section className="panel" style={{ marginTop: 22 }} aria-labelledby="orders-title">
        <div className="panel-head"><h2 id="orders-title">Your orders</h2><span className="panel-sub">this page only</span></div>
        {orders.length === 0 ? (
          <p className="empty">No orders yet. Buy something, then break inventory in Chaos Lab and try again.</p>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
            {orders.map((o) => (
              <li key={o.id} style={{ display: "grid", gridTemplateColumns: "70px 1fr auto", gap: 12, fontSize: 14, padding: "8px 4px", borderBottom: "1px solid var(--line)" }}>
                <span className="mono" style={{ color: "var(--text-3)", fontSize: 12.5 }}>{o.at}</span>
                <span style={{ color: o.ok ? "var(--text)" : "#f9a39b" }}>{o.name}: {o.text}</span>
                <span className="num" style={{ color: "var(--text-3)" }}>{Math.round(o.ms)} ms</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
