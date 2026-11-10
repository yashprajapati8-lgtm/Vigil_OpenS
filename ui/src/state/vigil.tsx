import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from "react";
import { api } from "../api/client";
import type { Control, Health, Incident, Point, StateResponse, StreamMessage, Verdict, VigilEvent } from "../api/types";
import { TERMINAL } from "../api/types";
import { player, REPLAY } from "../replay/player";

// One live picture of VIGIL: a snapshot on load, then the SSE stream keeps it
// current. On reconnect the snapshot is fetched again, so a dropped message can
// never leave the screen out of date.

const HISTORY_POINTS = 180; // 15 minutes of 5 s ticks
const EVENT_LIMIT = 300;

interface VigilState {
  connected: boolean;
  loaded: boolean;
  health: Health | null;
  latest: Point | null;
  history: Point[];
  verdicts: Record<string, Verdict>;
  incidents: Record<string, Incident>;
  events: VigilEvent[];
  error: string | null;
}

type Action =
  | { type: "snapshot"; state: StateResponse; history: Point[]; events: VigilEvent[]; incidents: Incident[] }
  | { type: "message"; msg: StreamMessage }
  | { type: "health"; health: Health }
  | { type: "control"; control: Control }
  | { type: "connected"; value: boolean }
  | { type: "error"; error: string | null };

const initial: VigilState = {
  connected: false, loaded: false, health: null, latest: null, history: [],
  verdicts: {}, incidents: {}, events: [], error: null,
};

function mergeEvents(current: VigilEvent[], incoming: VigilEvent[]): VigilEvent[] {
  const byId = new Map(current.map((e) => [e.id, e]));
  for (const e of incoming) byId.set(e.id, e);
  return [...byId.values()].sort((a, b) => a.id - b.id).slice(-EVENT_LIMIT);
}

function reducer(s: VigilState, a: Action): VigilState {
  switch (a.type) {
    case "snapshot": {
      const incidents: Record<string, Incident> = {};
      for (const i of [...a.incidents, ...a.state.open_incidents]) incidents[i.id] = i;
      return {
        ...s, loaded: true, error: null, health: a.state.health, latest: a.state.latest,
        verdicts: a.state.verdicts, history: a.history.slice(-HISTORY_POINTS),
        events: mergeEvents([], a.events), incidents,
      };
    }
    case "message": {
      const m = a.msg;
      if (m.type === "tick") {
        const history = [...s.history, m.point].slice(-HISTORY_POINTS);
        return { ...s, latest: m.point, verdicts: m.verdicts, history };
      }
      if (m.type === "event") {
        let health = s.health;
        if (health && m.event.kind === "control_changed") {
          health = { ...health, control: m.event.payload as unknown as Control };
        }
        if (health && (m.event.kind === "telemetry_degraded" || m.event.kind === "telemetry_ok")) {
          health = { ...health, telemetry_ok: m.event.kind === "telemetry_ok", problem: (m.event.payload.problem as string) ?? null };
        }
        return { ...s, health, events: mergeEvents(s.events, [m.event]) };
      }
      return { ...s, incidents: { ...s.incidents, [m.incident.id]: m.incident } };
    }
    case "health":
      return { ...s, health: a.health };
    case "control":
      return s.health ? { ...s, health: { ...s.health, control: a.control } } : s;
    case "connected":
      return { ...s, connected: a.value };
    case "error":
      return { ...s, error: a.error };
  }
}

interface VigilContextValue extends VigilState {
  focus: Incident | null;
  refresh: () => Promise<void>;
  setControl: (control: Control) => void;
}

const VigilContext = createContext<VigilContextValue | null>(null);

export function VigilProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initial);
  const sourceRef = useRef<EventSource | null>(null);

  const refresh = useCallback(async () => {
    if (REPLAY) {
      try {
        const s = await player.snapshot();
        dispatch({ type: "snapshot", state: s.state, history: s.history, events: s.events, incidents: s.incidents });
      } catch (e) {
        dispatch({ type: "error", error: e instanceof Error ? e.message : "Can't load the recording" });
      }
      return;
    }
    try {
      const [snap, hist, ev, inc] = await Promise.all([
        api.get<StateResponse>("/api/state"),
        api.get<{ points: Point[] }>("/api/history?minutes=15"),
        api.get<{ events: VigilEvent[] }>("/api/events?limit=200"),
        api.get<{ incidents: Incident[] }>("/api/incidents?limit=40"),
      ]);
      dispatch({ type: "snapshot", state: snap, history: hist.points, events: ev.events, incidents: inc.incidents });
    } catch (e) {
      dispatch({ type: "error", error: e instanceof Error ? e.message : "Can't load VIGIL's state" });
    }
  }, []);

  useEffect(() => {
    if (!REPLAY) return;
    // Replay: the recording is the stream. A new recording replaces the whole picture.
    dispatch({ type: "connected", value: true });
    const offMsg = player.onMessage((msg) => dispatch({ type: "message", msg }));
    const offSnap = player.onSnapshot((snap) =>
      dispatch({ type: "snapshot", state: snap.state, history: snap.history, events: snap.events, incidents: snap.incidents }));
    refresh().then(() => player.autoplay());
    return () => { offMsg(); offSnap(); };
  }, [refresh]);

  useEffect(() => {
    if (REPLAY) return;
    let stopped = false;
    let hadError = false;
    refresh();
    const src = new EventSource("/api/stream");
    sourceRef.current = src;
    src.onopen = () => {
      dispatch({ type: "connected", value: true });
      if (hadError) refresh();
      hadError = false;
    };
    src.onerror = () => {
      hadError = true;
      dispatch({ type: "connected", value: false });
    };
    src.onmessage = (e) => {
      if (stopped) return;
      try {
        dispatch({ type: "message", msg: JSON.parse(e.data) as StreamMessage });
      } catch {
        /* a malformed message is skipped; the next snapshot corrects it */
      }
    };
    const poll = window.setInterval(async () => {
      try {
        dispatch({ type: "health", health: await api.get<Health>("/api/health") });
      } catch {
        /* the connection banner already says the engine is unreachable */
      }
    }, 10_000);
    return () => {
      stopped = true;
      src.close();
      window.clearInterval(poll);
    };
  }, [refresh]);

  const focus = useMemo(() => pickFocus(state.incidents), [state.incidents]);
  const setControl = useCallback((control: Control) => dispatch({ type: "control", control }), []);

  const value = useMemo(() => ({ ...state, focus, refresh, setControl }), [state, focus, refresh, setControl]);
  return <VigilContext.Provider value={value}>{children}</VigilContext.Provider>;
}

/** The incident the screen is about: the newest open one, else one that closed in the last 10 minutes. */
function pickFocus(incidents: Record<string, Incident>): Incident | null {
  const all = Object.values(incidents).sort((a, b) => b.seq - a.seq);
  const open = all.find((i) => !TERMINAL.includes(i.status));
  if (open) return open;
  const recent = all.find((i) => i.closed_at && Date.now() / 1000 - i.closed_at < 600);
  return recent ?? null;
}

export function useVigil(): VigilContextValue {
  const ctx = useContext(VigilContext);
  if (!ctx) throw new Error("useVigil must be used inside VigilProvider");
  return ctx;
}
