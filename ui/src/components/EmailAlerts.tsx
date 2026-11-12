import { Mail, Send, X } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { useOperator } from "../lib/operator";
import { REPLAY } from "../replay/player";
import { useVigil } from "../state/vigil";

// Who gets VIGIL's emails. VIGIL runs on a server nobody sits at; the email is how
// it reaches a person. Sending needs an account set once in .env (the person who
// installs VIGIL); who receives is managed here, operator only: otherwise anyone who
// can open the console could add their own address or remove yours.

type Note = { kind: "ok" | "error"; text: string } | null;

export function EmailAlerts() {
  const { health, refresh } = useVigil();
  const run = useOperator();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<Note>(null);
  const alerts = health?.alerts;
  if (!alerts || REPLAY) return null;

  const act = (what: string, call: () => Promise<unknown>, done: string) => run(async () => {
    setBusy(what);
    setNote(null);
    try {
      await call();
      setNote({ kind: "ok", text: done });
      await refresh();
      return;
    } catch (e) {
      setNote({ kind: "error", text: e instanceof Error ? e.message : "something went wrong" });
    } finally {
      setBusy(null);
    }
  });

  const add = (e: FormEvent) => {
    e.preventDefault();
    const address = email.trim();
    if (!address) return;
    act("add", () => api.post("/api/alerts/recipients", { email: address }, true),
      `Added. ${address} gets VIGIL's alerts from now on.`);
    setEmail("");
  };

  return (
    <section className="panel ea" id="email-alerts" aria-labelledby="ea-title">
      <div className="panel-head">
        <h2 id="ea-title"><Mail size={17} aria-hidden="true" /> Email alerts</h2>
        <span className="ea-state" data-on={alerts.email === "on" || undefined}>{alerts.email === "on" ? "on" : "off"}</span>
      </div>
      <p className="ea-lede">
        VIGIL emails when it fixes something on its own, needs your approval, hands over to a human,
        can't see your services, or a service's normal keeps rising. Once per incident each, with every step it took.
      </p>

      {!alerts.sender ? (
        <div className="ea-setup">
          <p>VIGIL needs an account to send from, set once in <code className="cx-inline">.env</code> by whoever runs it:</p>
          <pre><code>{"VIGIL_SMTP_HOST=smtp.gmail.com\nVIGIL_SMTP_USER=you@gmail.com\nVIGIL_SMTP_PASSWORD=<a Gmail app password>"}</code></pre>
          <p>Then restart the engine. Who receives the alerts is added here.</p>
        </div>
      ) : (
        <>
          {alerts.recipients && alerts.recipients.length > 0 ? (
            <ul className="ea-list" aria-label="Who gets alerts">
              {alerts.recipients.map((r) => (
                <li key={r.id}>
                  <span className="ea-addr">{r.masked}</span>
                  {r.source === ".env" ? (
                    <span className="ea-src" title="Set in .env (VIGIL_ALERT_TO); remove it there">from .env</span>
                  ) : (
                    <button type="button" className="ea-remove" aria-label={`Stop alerts to ${r.masked}`} disabled={!!busy}
                      onClick={() => act(`rm-${r.id}`, () => api.post("/api/alerts/recipients/remove", { id: r.id }, true),
                        `Removed ${r.masked}.`)}>
                      <X size={14} aria-hidden="true" />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="ea-empty">Nobody gets alerts yet. Add an address below.</p>
          )}

          <form className="ea-form" onSubmit={add}>
            <label className="sr-only" htmlFor="ea-email">Email address to send alerts to</label>
            <input id="ea-email" type="email" inputMode="email" autoComplete="email" placeholder="you@example.com"
              value={email} onChange={(e) => setEmail(e.target.value)} disabled={!!busy} required maxLength={254} />
            <button type="submit" className="btn" disabled={!!busy || !email.trim()}>{busy === "add" ? "Adding…" : "Add"}</button>
          </form>

          <button type="button" className="btn btn-small ea-test" disabled={!!busy || !alerts.recipients?.length}
            onClick={() => act("test", () => api.post("/api/alerts/test", undefined, true),
              "Test email sent. Check the inbox (and Spam) in a few seconds.")}>
            <Send size={14} aria-hidden="true" /> {busy === "test" ? "Sending…" : "Send a test email"}
          </button>

          {alerts.last_error && <p className="ea-note" data-kind="error">Last send failed: {alerts.last_error}</p>}
          {alerts.email === "on" && alerts.sent ? <p className="ea-sub">{alerts.sent} sent since VIGIL started.</p> : null}
          <p className="ea-sub">Adding or removing an address needs operator mode.</p>
        </>
      )}
      {note && <p className="ea-note" data-kind={note.kind} role="status">{note.text}</p>}
    </section>
  );
}
