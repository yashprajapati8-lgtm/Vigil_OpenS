import qrcode from "qrcode-generator";
import { Copy, ExternalLink, FolderGit2, Globe, Play, Smartphone } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { clock } from "../lib/format";
import { usePhones, rupees, PRODUCT_NAMES } from "../lib/phones";
import { REPLAY } from "../replay/player";
import { GITHUB_URL, LIVE_VIDEO_URL, PHONE_README_URL } from "../lib/links";

// Let anyone pay from a phone. The QR code opens the demo checkout (/pay); each
// phone on it says hello every 5 s, and every payment lands here with how long it
// took and, if it failed, why. No money moves.

const LINK_KEY = "vigil.phone.link";

function readLink(): string {
  try {
    return localStorage.getItem(LINK_KEY) ?? "";
  } catch {
    return "";
  }
}

function QR({ text }: { text: string }) {
  const cells = useMemo(() => {
    const qr = qrcode(0, "M");
    qr.addData(text);
    qr.make();
    const n = qr.getModuleCount();
    let d = "";
    for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (qr.isDark(r, c)) d += `M${c},${r}h1v1h-1z`;
    return { n, d };
  }, [text]);
  return (
    <svg className="phones-qr" viewBox={`-2 -2 ${cells.n + 4} ${cells.n + 4}`} role="img" aria-label={`QR code for ${text}`} shapeRendering="crispEdges">
      <rect x="-2" y="-2" width={cells.n + 4} height={cells.n + 4} fill="#fff" />
      <path d={cells.d} fill="#111" />
    </svg>
  );
}

/** The public link, when a Cloudflare tunnel is running (docker compose --profile public). */
function useTunnel(): string | null {
  const [host, setHost] = useState<string | null>(null);
  useEffect(() => {
    if (REPLAY) return;
    let alive = true;
    const check = async () => {
      try {
        const r = await fetch("/api/tunnel", { cache: "no-store" });
        const data = r.ok ? await r.json() : null;
        const h = typeof data?.hostname === "string" && /^[a-z0-9.-]+$/i.test(data.hostname) ? data.hostname : null;
        if (alive) setHost(h);
      } catch {
        if (alive) setHost(null);
      }
    };
    void check();
    const t = window.setInterval(check, 5000);
    return () => { alive = false; window.clearInterval(t); };
  }, []);
  return host ? `https://${host}` : null;
}

export function Phones() {
  const { status, error } = usePhones();
  const tunnel = useTunnel();
  const [custom, setCustom] = useState(readLink);
  const [copied, setCopied] = useState(false);

  const base = (custom.trim() || tunnel || window.location.origin).replace(/\/+$/, "").replace(/\/pay$/, "");
  const link = `${base}/pay`;
  const localOnly = /^https?:\/\/(localhost|127\.|\[::1\])/.test(base);

  const save = (v: string) => {
    setCustom(v);
    try {
      if (v.trim()) localStorage.setItem(LINK_KEY, v.trim());
      else localStorage.removeItem(LINK_KEY);
    } catch {
      /* fine: remembered for this visit only */
    }
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked: the link is on screen to type */
    }
  };

  if (REPLAY) {
    return (
      <div className="stack">
        <section className="panel" aria-labelledby="phones-title">
          <div className="panel-head">
            <h1 id="phones-title" className="page-title">Pay from your phone</h1>
            <span className="chip">Live stack only</span>
          </div>
          <p className="panel-caption phones-why">
            <b>Why it isn't on this link:</b> this site is hosted on Vercel, which serves web pages, not long-running
            servers. Live VIGIL is a whole system that runs in Docker around the clock: the shop's storefront, payment and
            inventory services, Prometheus, the VIGIL engine, and the actuator that restarts real containers. None of
            that can run on Vercel, so this link replays a run recorded on our laptop instead. With no shop behind it, a
            payment from your phone would have nowhere to go, and faking one would prove nothing.
          </p>
          <p className="panel-caption phones-why">
            <b>What it does live:</b> your phone pays through the same services VIGIL watches. Break inventory and your
            payment fails, then you watch VIGIL fix it and your next payment goes through. No real money moves.
          </p>
          <div className="phones-actions">
            <a className="btn btn-primary" href={LIVE_VIDEO_URL} target="_blank" rel="noreferrer"><Play size={16} /> Watch a phone pay live (3 min)</a>
            <a className="btn" href={PHONE_README_URL} target="_blank" rel="noreferrer"><FolderGit2 size={16} /> Run it yourself from GitHub</a>
          </div>
        </section>

        <section className="panel" aria-labelledby="phones-run">
          <div className="panel-head"><h2 id="phones-run">Try it live in three steps</h2><span className="panel-sub">needs Docker Desktop</span></div>
          <ol className="guide-steps">
            <li className="guide-step">
              <span className="guide-num" aria-hidden="true">1</span>
              <b>Get the code</b>
              <span>Clone the repo, then copy <code>.env.example</code> to <code>.env</code> and set the two tokens in it.</span>
              <code className="fault-cmd">git clone {GITHUB_URL}</code>
            </li>
            <li className="guide-step">
              <span className="guide-num" aria-hidden="true">2</span>
              <b>Start VIGIL</b>
              <span>Every service starts in Docker. VIGIL learns what normal looks like for about 3 minutes.</span>
              <code className="fault-cmd">docker compose up --build -d</code>
            </li>
            <li className="guide-step">
              <span className="guide-num" aria-hidden="true">3</span>
              <b>Open the phone link</b>
              <span>Start a public link, open <code>localhost:3000/phones</code> and scan the QR code with any phone.</span>
              <code className="fault-cmd">docker compose --profile public up -d cloudflared</code>
            </li>
          </ol>
        </section>
      </div>
    );
  }

  const totals = status?.totals ?? { paid: 0, failed: 0 };
  const phones = status?.connected ?? [];
  const payments = status?.payments ?? [];
  return (
    <div className="stack">
      <section className="panel phones-hero" aria-labelledby="phones-title">
        <div className="phones-connect">
          <div className="phones-copy">
            <h1 id="phones-title" className="page-title">Pay from your phone</h1>
            <p className="panel-caption">
              Scan the code and pay in the demo shop. No real money moves. Your order takes the same path as every other
              customer, storefront to payment to inventory, so when VIGIL's services break, your payment breaks too, and
              you see it recover.
            </p>
            <label className="field phones-field">
              Link the phone opens
              <input value={custom} placeholder={tunnel ?? window.location.origin} onChange={(e) => save(e.target.value)}
                spellCheck={false} autoCapitalize="off" autoCorrect="off" inputMode="url" />
            </label>
            {tunnel && !custom.trim() ? (
              <p className="phones-ok"><Globe size={15} aria-hidden="true" /> Public link is live: any phone on the internet can open it. It shows only the checkout, never this console.</p>
            ) : localOnly && (
              <p className="phones-warn">
                <b>localhost</b> only opens on this computer. For any phone, start a public link with{" "}
                <code>docker compose --profile public up -d cloudflared</code> and it appears here by itself. Or use this
                laptop's Wi-Fi address (like <code>http://192.168.1.5:3000</code>) with the phone on the same network.
              </p>
            )}
            <div className="phones-actions">
              <button type="button" className="btn btn-small" onClick={copy}><Copy size={15} /> {copied ? "Copied" : "Copy link"}</button>
              <a className="btn btn-small" href="/pay" target="_blank" rel="noreferrer"><ExternalLink size={15} /> Open here</a>
            </div>
          </div>
          <figure className="phones-qr-wrap">
            <QR text={link} />
            <figcaption>{link}</figcaption>
          </figure>
        </div>
      </section>

      <div className="phones-grid">
        <section className="panel" aria-labelledby="phones-connected">
          <div className="panel-head">
            <h2 id="phones-connected">Connected phones</h2>
            <span className="panel-sub">{error ? "storefront not reachable" : `${phones.length} on the checkout page`}</span>
          </div>
          {phones.length === 0 ? (
            <p className="phones-empty">No phone yet. Scan the code to connect one.</p>
          ) : (
            <ul className="phones-list">
              {phones.map((d) => (
                <li key={d.id}>
                  <span className="phones-dot" aria-hidden="true" />
                  <Smartphone size={16} aria-hidden="true" />
                  <span className="phones-label">{d.label}</span>
                  <span className="phones-meta">{d.seen_s < 6 ? "active now" : `seen ${Math.round(d.seen_s)} s ago`} · {d.paid} paid · {d.failed} failed</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel" aria-labelledby="phones-totals">
          <div className="panel-head"><h2 id="phones-totals">Phone payments</h2><span className="panel-sub">since the storefront started</span></div>
          <div className="phones-totals">
            <div><b className="pg-good">{totals.paid}</b><span>paid</span></div>
            <div><b style={{ color: totals.failed ? "#f9a39b" : undefined }}>{totals.failed}</b><span>failed</span></div>
          </div>
        </section>
      </div>

      <section className="panel" aria-labelledby="phones-recent">
        <div className="panel-head"><h2 id="phones-recent">Latest payments</h2><span className="panel-sub">newest first</span></div>
        {payments.length === 0 ? (
          <p className="phones-empty">Payments from phones appear here the moment they happen.</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Time</th><th>Phone</th><th>Item</th><th>Result</th><th>Took</th></tr></thead>
              <tbody>
                {payments.map((p) => (
                  <tr key={p.id}>
                    <td className="num">{clock(p.ts)}</td>
                    <td>{p.device}</td>
                    <td>{PRODUCT_NAMES[p.sku] ?? p.sku} · {rupees(p.amount)}</td>
                    <td>{p.ok ? <span className="chip chip-ok">Paid</span> : <><span className="chip chip-bad">Failed</span> <span className="phones-reason">{p.reason}</span></>}</td>
                    <td className="num">{(p.ms / 1000).toFixed(2)} s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
