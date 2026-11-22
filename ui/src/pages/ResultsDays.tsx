import { CalendarDays, Check, X } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "../api/client";
import { useOperator } from "../lib/operator";
import { useSite } from "../lib/site";
import { REPLAY } from "../replay/player";

// Results days: rushes the college knows are coming. The admin adds the date here
// (no code, no restart); VIGIL starts the spare copy a few minutes before and keeps
// it through the rush. vigil.yaml `events` show here too, read-only.

interface PlannedEvent {
  id: string;
  source: "console" | "vigil.yaml";
  name: string;
  at: number;
  prepare_from: number;
  until: number;
  services: string[];
  ready: string[];
  status: "upcoming" | "getting ready" | "over";
}

type Note = { kind: "ok" | "error"; text: string } | null;

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

function timeOnly(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** The value a datetime-local input wants, for "now plus some minutes", in local time. */
function localInputValue(minutesFromNow: number): string {
  const d = new Date(Date.now() + minutesFromNow * 60000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const STATUS_TEXT: Record<PlannedEvent["status"], string> = {
  upcoming: "Upcoming",
  "getting ready": "Getting ready",
  over: "Over",
};

export function ResultsDays() {
  const site = useSite();
  const run = useOperator();
  const [events, setEvents] = useState<PlannedEvent[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [name, setName] = useState("Results declared");
  const [at, setAt] = useState(() => localInputValue(10));
  const [before, setBefore] = useState("10");
  const [after, setAfter] = useState("60");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<Note>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ events: PlannedEvent[] }>("/api/results-days");
      setEvents(data.events);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "could not load");
    }
  }, []);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(t);
  }, [load]);

  const add = (e: FormEvent) => {
    e.preventDefault();
    const when = new Date(at);                 // the admin's local date and time
    if (Number.isNaN(when.getTime())) {
      setNote({ kind: "error", text: "Pick a date and time." });
      return;
    }
    run(async () => {
      setBusy("add");
      setNote(null);
      try {
        await api.post("/api/results-days", {
          name: name.trim(), at: when.getTime() / 1000,
          prepare_min: Number(before), lasts_min: Number(after),
        }, true);
        setNote({ kind: "ok", text: `Added. VIGIL starts the spare copy ${before} min before ${timeOnly(when.getTime() / 1000)}.` });
        await load();
      } catch (err) {
        setNote({ kind: "error", text: err instanceof Error ? err.message : "Could not add it" });
      } finally {
        setBusy(null);
      }
    });
  };

  const remove = (ev: PlannedEvent) => run(async () => {
    setBusy(ev.id);
    setNote(null);
    try {
      await api.post(`/api/results-days/${encodeURIComponent(ev.id)}/remove`, undefined, true);
      await load();
    } catch (err) {
      setNote({ kind: "error", text: err instanceof Error ? err.message : "Could not remove it" });
    } finally {
      setBusy(null);
    }
  });

  return (
    <div className="rd">
      <section className="panel" aria-labelledby="rd-title">
        <h1 id="rd-title" className="page-title"><CalendarDays size={22} aria-hidden="true" /> Results days</h1>
        <p className="rd-lede">
          Add the days you know {site.name} will be busy, like results being declared. A few minutes before, VIGIL
          starts the spare copy so students never see an error page. After the rush, once traffic is calm, it stops it.
          If the rush is bigger than planned, VIGIL still detects and fixes it as usual.
        </p>

        <form className="rd-form" onSubmit={add}>
          <label className="rd-field rd-name">
            <span>What</span>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} required />
          </label>
          <label className="rd-field">
            <span>Date and time</span>
            <input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} required />
          </label>
          <label className="rd-field rd-num">
            <span>Get ready (min before)</span>
            <input type="number" min={0} max={1440} value={before} onChange={(e) => setBefore(e.target.value)} required />
          </label>
          <label className="rd-field rd-num">
            <span>Keep ready (min after)</span>
            <input type="number" min={0} max={1440} value={after} onChange={(e) => setAfter(e.target.value)} required />
          </label>
          <button type="submit" className="btn" disabled={busy === "add"}>{busy === "add" ? "Adding…" : "Add results day"}</button>
        </form>
        {note && <p className="ea-note" data-kind={note.kind} role="status">{note.text}</p>}
        <p className="ea-sub">{REPLAY
          ? "Replay: days you add are kept in this browser only. On the live stack, VIGIL starts the spare copy when a day reaches \"Getting ready\" and the event log shows it."
          : "Adding or removing needs your operator token. No restart needed."}</p>
      </section>

      <section className="panel" aria-labelledby="rd-list-title">
        <div className="panel-head"><h2 id="rd-list-title">Planned</h2></div>
        {loadError && <p className="ea-note" data-kind="error">Could not load: {loadError}</p>}
        {events && events.length === 0 && <p className="ea-empty">Nothing planned yet. Add the next results day above.</p>}
        {events && events.length > 0 && (
          <ul className="rd-list">
            {events.map((ev) => (
              <li key={ev.id} data-status={ev.status}>
                <div className="rd-main">
                  <span className="rd-what">{ev.name}</span>
                  <span className="rd-when">{when(ev.at)}</span>
                  <span className="rd-plan">
                    Spare copy from {timeOnly(ev.prepare_from)} until at least {timeOnly(ev.until)} · {ev.services.join(", ")}
                  </span>
                </div>
                <span className="rd-status" data-status={ev.status}>
                  {ev.ready.length > 0 && ev.status === "getting ready" && <Check size={13} aria-hidden="true" />}
                  {ev.ready.length > 0 && ev.status === "getting ready" ? "Ready" : STATUS_TEXT[ev.status]}
                </span>
                {ev.source === "console" ? (
                  <button type="button" className="ea-remove" onClick={() => remove(ev)} disabled={busy === ev.id}
                    aria-label={`Remove ${ev.name} on ${when(ev.at)}`} title="Remove">
                    <X size={15} />
                  </button>
                ) : (
                  <span className="ea-src" title="Change it in vigil.yaml">vigil.yaml</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
