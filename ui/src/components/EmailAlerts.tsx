import { Mail, Send, X } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { useOperator } from "../lib/operator";
import type { VigilEvent } from "../api/types";
import { clock, duration } from "../lib/format";
import { useSite } from "../lib/site";
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
  if (REPLAY) return <ReplayEmailAlerts />;
  if (!alerts) return null;

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

// The replay can't send email, so it shows the email VIGIL would send for the run
// being played: built from that recording's own incident and steps, the same way
// the engine builds it (notify.py). The form is shown but off: adding an address
// needs the live stack.

const ACTION_TEXT: Record<string, (t: string) => string> = {
  restart: (t) => `restart ${t}`,
  rollback: (t) => `roll back ${t}`,
  scale_out: (t) => `add a ${t} replica`,
  wait: () => "wait",
};

function actionText(action?: string | null, target?: string | null): string {
  if (!action) return "decide what to do";
  const f = ACTION_TEXT[action];
  return f ? f(target ?? "") : `${action} ${target ?? ""}`.trim();
}

function ReplayEmailAlerts() {
  const { focus, incidents, events } = useVigil();
  const site = useSite();
  let inc = focus;
  if (!inc) {
    for (const i of Object.values(incidents)) {
      if (!inc || i.seq > inc.seq) inc = i;
    }
  }
  let subject = "";
  let headline = "";
  if (inc) {
    const d = inc.data.decision;
    const act = actionText(d?.action, d?.target);
    const mttr = inc.data.metrics?.mttr_s;
    if (inc.status === "RESOLVED") {
      subject = `[VIGIL] ${site.name}: fixed ${inc.id}${mttr ? ` in ${duration(mttr)}` : ""}`;
      headline = `VIGIL fixed ${inc.title} and checked that real ${site.units} go through again.`;
    } else if (inc.status === "ESCALATED") {
      subject = `[VIGIL] ${site.name}: ${inc.id} needs you`;
      headline = `VIGIL could not fix ${inc.title} safely and handed it to a human.`;
    } else if (inc.status === "AWAITING_APPROVAL") {
      subject = `[VIGIL] ${site.name}: approval needed for ${inc.id} (${act})`;
      headline = `VIGIL wants to ${act} and is waiting for a human to approve.`;
    } else if (inc.status === "REMEDIATING" || inc.status === "VERIFYING") {
      subject = `[VIGIL] ${site.name}: fixing ${inc.id} on its own (${act})`;
      headline = `VIGIL found the cause and is fixing it: ${act}.`;
    }
  }
  const steps: VigilEvent[] = [];
  if (inc && subject) {
    for (const e of events) {
      if (e.incident_id === inc.id) steps.push(e);
    }
    steps.sort((a, b) => a.ts - b.ts);
  }
  const shown = steps.slice(-10);

  return (
    <section className="panel ea" id="email-alerts" aria-labelledby="ea-title">
      <div className="panel-head">
        <h2 id="ea-title"><Mail size={17} aria-hidden="true" /> Email alerts</h2>
        <span className="ea-state">live app</span>
      </div>
      <p className="ea-lede">
        VIGIL emails when it fixes something on its own, needs your approval, hands over to a human,
        can't see your services, or a service's normal keeps rising. Once per incident each, with every step it took.
      </p>
      <form className="ea-form" onSubmit={(e) => e.preventDefault()}>
        <label className="sr-only" htmlFor="ea-email">Email address to send alerts to</label>
        <input id="ea-email" type="email" placeholder="you@example.com" disabled />
        <button type="submit" className="btn" disabled>Add</button>
      </form>
      <p className="ea-sub">In the live app you type an email here and the alerts go to that inbox. This recorded run can't send email, so here is the one VIGIL would send for the run you're watching.</p>

      {subject ? (
        <article className="ea-mail" aria-label="Example email for this run">
          <p className="ea-mail-subject">{subject}</p>
          <p className="ea-mail-head">{headline}</p>
          <ol className="ea-mail-steps">
            {shown.map((e) => (
              <li key={e.id}><time>{clock(e.ts)}</time> {e.summary}</li>
            ))}
          </ol>
        </article>
      ) : (
        <p className="ea-empty">Nothing to email yet. Pick a fault in the Chaos Lab: the email appears here the moment VIGIL starts fixing it, and changes to "fixed" when it's done.</p>
      )}
    </section>
  );
}
