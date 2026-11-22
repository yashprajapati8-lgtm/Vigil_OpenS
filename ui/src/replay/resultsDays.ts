import { ApiError } from "../api/errors";
import { player } from "./player";

// Results days in the replay: the same rules as the engine (engine/vigil/schedule.py),
// kept in this browser only. Nothing here is recorded: on the live stack VIGIL starts
// the spare copy when a day reaches "getting ready" and the event log shows it; the
// replay only shows where each day stands.

const KEY = "vigil-replay-results-days";

/** The services the recording's vigil.yaml lets VIGIL scale out (the demo shop: payment). */
function services(): string[] {
  const site = player.health()?.site;
  if (!site) return ["payment"];
  const out: string[] = [];
  for (const s of site.services) {
    const allowed = site.allowed[s];
    if (!allowed || allowed.includes("scale_out")) out.push(s);
  }
  return out;
}

interface Stored { id: string; name: string; at: number; prepare_s: number; lasts_s: number }

function load(): Stored[] {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return [];
    const list = JSON.parse(raw);
    if (!Array.isArray(list)) return [];
    return list;
  } catch {
    return [];
  }
}

let days: Stored[] = load();

function save(): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(days));
  } catch {
    // private window or storage blocked: the list still works until the tab closes
  }
}

function view(d: Stored, now: number) {
  const from = d.at - d.prepare_s;
  const until = d.at + d.lasts_s;
  let status = "upcoming";
  if (now >= from && now <= until) {
    status = "getting ready";
  } else if (now > until) {
    status = "over";
  }
  return {
    id: d.id, source: "console", name: d.name, at: d.at,
    prepare_from: from, until, services: services(), ready: [] as string[], status,
  };
}

export function listResultsDays() {
  const now = Date.now() / 1000;
  const sorted = [...days].sort((a, b) => a.at - b.at);
  const events = [];
  for (const d of sorted) {
    events.push(view(d, now));
  }
  return { events };
}

export function addResultsDay(body: { name?: string; at?: number; prepare_min?: number; lasts_min?: number }) {
  const name = (body.name ?? "").trim();
  const at = Number(body.at);
  const prepare = Number(body.prepare_min ?? 10);
  const lasts = Number(body.lasts_min ?? 60);
  if (!name || name.length > 60) {
    throw new ApiError(400, "Give it a name (1 to 60 characters)");
  }
  if (!Number.isFinite(at)) {
    throw new ApiError(400, "Pick a date and time");
  }
  if (!(prepare >= 0 && prepare <= 1440) || !(lasts >= 0 && lasts <= 1440)) {
    throw new ApiError(400, "Minutes must be between 0 and 1440");
  }
  if (at + lasts * 60 < Date.now() / 1000) {
    throw new ApiError(400, "That time is already over");
  }
  const id = `console-${Date.now().toString(36)}`;
  const day: Stored = { id, name, at, prepare_s: prepare * 60, lasts_s: lasts * 60 };
  days.push(day);
  save();
  return { event: view(day, Date.now() / 1000) };
}

export function removeResultsDay(id: string) {
  const before = days.length;
  days = days.filter((d) => d.id !== id);
  if (days.length === before) {
    throw new ApiError(404, "No such results day");
  }
  save();
  return { removed: id };
}
