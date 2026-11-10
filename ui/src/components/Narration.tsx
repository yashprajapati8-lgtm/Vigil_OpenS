import { useEffect, useRef } from "react";
import type { VigilEvent } from "../api/types";
import { clock } from "../lib/format";

// VIGIL explains itself in plain English. Teal = VIGIL acted; amber = a human;
// red = something failed; green = verified recovery.

const WHO: Record<string, string> = {
  detector: "Detector", engine: "VIGIL", walk: "Blame walk", llm: "LLM", policy: "Policy",
  executor: "Actuator", verifier: "Verifier", human: "Operator",
};

function tone(e: VigilEvent): string {
  if (/failed|escalated|degraded|rejected/.test(e.kind)) return "tone-bad";
  if (e.kind === "resolved" || e.kind === "self_resolved" || e.kind === "service_recovered" || e.kind === "telemetry_ok") return "tone-ok";
  if (e.actor === "human" || e.kind === "awaiting_approval" || e.kind === "action_blocked") return "tone-warn";
  if (["policy", "executor", "verifier"].includes(e.actor)) return "tone-vigil";
  return "tone-plain";
}

export function Narration({ events, title = "What VIGIL is doing", limit = 80 }: { events: VigilEvent[]; title?: string; limit?: number }) {
  const seen = useRef<Set<number> | null>(null);
  const list = [...events].reverse().slice(0, limit);
  const firstRender = seen.current === null;
  if (seen.current === null) seen.current = new Set(list.map((e) => e.id));
  const fresh = new Set(list.filter((e) => !seen.current!.has(e.id)).map((e) => e.id));
  useEffect(() => {
    for (const e of list) seen.current!.add(e.id);
  });

  return (
    <section className="panel feed area-feed" aria-labelledby="feed-title">
      <div className="panel-head">
        <h2 id="feed-title">{title}</h2>
        <span className="panel-sub">newest first</span>
      </div>
      <p className="panel-caption">Every step VIGIL takes, in plain words, the moment it happens.</p>
      {list.length === 0 ? (
        <p className="empty">Quiet. VIGIL writes every step here the moment it happens.</p>
      ) : (
        <ol className="feed-list" aria-live="polite" aria-relevant="additions">
          {list.map((e) => (
            <li key={e.id} className={`feed-item ${tone(e)} ${!firstRender && fresh.has(e.id) ? "fresh" : ""}`}>
              <span className="feed-time">{clock(e.ts)}</span>
              <span className="feed-text"><span className="feed-who">{WHO[e.actor] ?? e.actor}</span>{e.summary}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
