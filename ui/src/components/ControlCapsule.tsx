import { CircleHelp, Lock, LockOpen, Pause, Play } from "lucide-react";
import { useState } from "react";
import { api, ApiError, setToken } from "../api/client";
import type { Control } from "../api/types";
import type { RunAsOperator } from "../App";
import { isDemand, trafficMultiple } from "../lib/demand";
import { duration, pct } from "../lib/format";
import { useVigil } from "../state/vigil";
import { useLearning } from "./Banners";
import { Logo, type LogoTone } from "./Logo";
import { showGuide } from "./StartHere";
import { Link, navigate } from "../lib/router";
import { REPLAY } from "../replay/player";

const LEVELS: Array<{ id: Control["autonomy"]; help: string }> = [
  { id: "L0", help: "Observe only: detect and diagnose" },
  { id: "L1", help: "Advise: propose a fix, a human approves every one" },
  { id: "L2", help: "Supervised: fix low-risk problems alone when confidence is High" },
  { id: "L3", help: "Autonomous: fix every allowed problem alone" },
];

function useStatus() {
  const { connected, loaded, health, focus, verdicts, latest } = useVigil();
  const learn = useLearning();
  if (!loaded && !connected) return { tone: "idle", text: "Connecting to VIGIL" };
  if (!connected) return { tone: "bad", text: "Can't reach the VIGIL engine. Retrying" };
  if (health && health.telemetry_ok === false) return { tone: "warn", text: `Telemetry degraded: ${health.problem ?? "no data"}. No decisions until it's back` };
  if (focus && !["RESOLVED", "SELF_RESOLVED", "ESCALATED"].includes(focus.status)) {
    return { tone: "bad", text: `${focus.id} ${focus.severity}: ${focus.title}`, strong: true };
  }
  const failing = latest?.user.fail_ratio ?? 0;
  const vs = Object.values(verdicts);
  const off = failing >= 0.02 || vs.some((v) => (v.flagged || v.in_incident || v.state === "down") && !isDemand(v));
  const busy = failing < 0.02 && vs.some((v) => v.flagged && isDemand(v));
  if (focus?.status === "ESCALATED" && off) {
    return { tone: "bad", text: `${focus.id} escalated: a human has it. ${pct(failing)} of checkouts still failing`, strong: true };
  }
  if (busy && !off) {
    const x = trafficMultiple(verdicts.payment, latest?.services.payment?.rps);
    const fixed = focus?.status === "RESOLVED" ? `${focus.id} fixed by VIGIL in ${duration(focus.data.metrics?.mttr_s ?? 0)}. ` : "";
    return { tone: "ok", text: `${fixed}Busy: checkout traffic ${x ?? "far above"} normal, and every checkout is served` };
  }
  if (off) return { tone: "warn", text: `Something looks off (${pct(failing)} of checkouts failing). Confirming before opening an incident` };
  if (learn) {
    const eta = learn.eta ?? 0;
    return { tone: "warn", text: `Learning normal traffic: ${Math.round(learn.progress * 100)}%${eta >= 1 ? `, about ${duration(eta)} left` : ""}. Detection starts at 100%` };
  }
  if (focus?.status === "RESOLVED") return { tone: "ok", text: `${focus.id} fixed by VIGIL in ${duration(focus.data.metrics?.mttr_s ?? 0)}. All services normal` };
  return { tone: "ok", text: "All services normal" };
}

interface Props {
  unlocked: boolean;
  runAsOperator: RunAsOperator;
  onLock: () => void;
  onLocked: () => void;
}

export function ControlCapsule({ unlocked, runAsOperator, onLock, onLocked }: Props) {
  const { health, setControl } = useVigil();
  const status = useStatus();
  const [error, setError] = useState<string | null>(null);
  const control = health?.control;

  const send = (body: Partial<Control>) =>
    runAsOperator(async () => {
      try {
        setError(null);
        setControl(await api.post<Control>("/api/control", body, true));
      } catch (e) {
        if (e instanceof ApiError && e.status === 403) {
          setToken(null);
          onLocked();
          setError("Operator token rejected. Unlock again.");
        } else {
          setError(e instanceof Error ? e.message : "Change failed");
        }
      }
    });

  const level = LEVELS.find((l) => l.id === control?.autonomy);
  return (
    <header className="capsule" aria-label="VIGIL controls">
      <Link href="/" className="brand" title="VIGIL home: what it is and how it works">
        <Logo size={30} />
        VIGIL
      </Link>
      <div className="status-line" aria-live="polite">
        <Logo size={18} tone={status.tone as LogoTone} pulse={status.tone === "bad"} />
        {status.strong ? <strong>{status.text}</strong> : <span>{status.text}</span>}
        {error && <span style={{ color: "#f9a39b" }}>{error}</span>}
      </div>
      <div className="control-group">
        <span className="control-label" id="autonomy-label">Autonomy</span>
        <div className="segmented" role="group" aria-labelledby="autonomy-label">
          {LEVELS.map((l) => (
            <button key={l.id} type="button" aria-pressed={control?.autonomy === l.id} disabled={!control}
              title={REPLAY && l.id === "L3" ? `${l.help}. The recorded runs used L3` : l.help}
              onClick={() => control?.autonomy !== l.id && send({ autonomy: l.id })}>
              {l.id}
            </button>
          ))}
        </div>
        <span className="sr-only">{level?.help}</span>
      </div>
      <button type="button" className="switch" role="switch" aria-checked={!control?.paused} disabled={!control}
        title={control?.paused ? "Actions paused: VIGIL watches but won't act. Click to arm" : "Actions armed: VIGIL may fix things within its guardrails. Click to pause (kill switch)"}
        onClick={() => send({ paused: !control?.paused })}>
        <span className="switch-knob">{control?.paused ? <Pause size={18} /> : <Play size={18} />}</span>
        Actions {control?.paused ? "paused" : "armed"}
      </button>
      <button type="button" className="icon-btn" aria-label="Show the guide: how to see VIGIL work" title="How to see VIGIL work"
        onClick={() => { navigate("/console"); showGuide(); }}>
        <CircleHelp size={18} />
      </button>
      <button type="button" className="icon-btn" data-unlocked={unlocked || REPLAY}
        aria-label={REPLAY ? "Operator controls: no token needed in this replay" : unlocked ? "Operator controls unlocked. Lock them" : "Unlock operator controls"}
        title={REPLAY ? "Operator controls: open in this replay" : unlocked ? "Operator mode: lock" : "Operator mode: unlock"}
        onClick={() => {
          if (REPLAY) onLock();
          else if (unlocked) {
            setToken(null);
            onLocked();
          } else onLock();
        }}>
        {unlocked || REPLAY ? <LockOpen size={18} /> : <Lock size={18} />}
      </button>
    </header>
  );
}
