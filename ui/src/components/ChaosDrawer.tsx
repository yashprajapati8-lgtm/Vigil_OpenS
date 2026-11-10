import { X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, getToken } from "../api/client";
import type { Scenario } from "../api/types";
import { REPLAY } from "../replay/player";
import { useLearning } from "./Banners";
import { HoldButton } from "./HoldButton";

// The chaos controller lives on its own network; VIGIL can't see it or call it.
// This drawer talks to it directly, so judges can break things themselves.

type Status = Record<string, Record<string, number | boolean | string>>;

// The answer key: what a correct diagnosis looks like. Shown here, never sent to VIGIL.
const ANSWER: Record<string, string> = {
  "slow-inventory": "inventory is the root cause (stuck process); payment is only a symptom",
  "payment-leak": "payment has a memory leak; restart before it hits its limit",
  "payment-runaway": "payment is stuck in a busy loop; restart clears it",
  "flash-sale": "real demand, nothing broken. VIGIL must never restart; it adds a payment replica, then removes it when traffic drops",
  "bad-deploy": "regression after a deploy. A restart brings the bug back; only rolling payment back to v1 fixes it",
};

function isActive(name: string, s: Status | null): boolean {
  if (!s) return false;
  if (name === "slow-inventory") return Number(s.inventory?.latency_ms ?? 0) > 0;
  if (name === "payment-leak") return Number(s.payment?.leak_mb_per_s ?? 0) > 0;
  if (name === "payment-runaway") return s.payment?.cpu_burn === true;
  if (name === "flash-sale") return Number(s.loadgen?.rps ?? 0) > 30;
  if (name === "bad-deploy") return s.deploy?.v2 === true;
  return false;
}

export function ChaosDrawer({ onClose }: { onClose: () => void }) {
  const [scenarios, setScenarios] = useState<Record<string, Scenario> | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const learning = useLearning();

  const load = useCallback(async () => {
    try {
      const [sc, st] = await Promise.all([
        scenarios ? Promise.resolve(scenarios) : api.get<Record<string, Scenario>>("/chaos/scenarios"),
        api.get<Status>("/chaos/status"),
      ]);
      setScenarios(sc);
      setStatus(st);
      setError(null);
    } catch {
      setError("Can't reach the chaos controller. Is the chaos-controller container running?");
    }
  }, [scenarios]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 3000);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearInterval(t);
      window.removeEventListener("keydown", onKey);
    };
  }, [load, onClose]);

  const inject = async (name: string, label: string) => {
    try {
      await api.post(`/chaos/scenarios/${name}/inject`);
      setMessage(REPLAY ? `${label}: playing the recorded run. Watch the service map.` : `${label}: injected. Watch the service map.`);
      load();
    } catch (e) {
      setMessage(e instanceof Error ? `${label} failed: ${e.message}` : `${label} failed`);
    }
  };
  const reset = async () => {
    try {
      const r = await api.post<{ errors: Record<string, string> }>("/chaos/reset");
      const errs = Object.keys(r.errors ?? {});
      setMessage(errs.length ? `Reset, but ${errs.join(", ")} didn't answer (probably restarting).` : "All faults cleared and traffic back to normal.");
      load();
    } catch (e) {
      setMessage(e instanceof Error ? `Reset failed: ${e.message}` : "Reset failed");
    }
  };

  // Clean slate for a demo: clear every fault, let the numbers settle (rates look back
  // 20 s), then close any open incident and relearn normal. Operator only.
  const freshStart = async () => {
    if (!getToken()) {
      setMessage("Fresh start needs the admin token: unlock with the lock button (top right), then hold again.");
      return;
    }
    try {
      await api.post("/chaos/reset");
      for (let left = 25; left > 0; left -= 1) {
        setMessage(`Faults cleared. Letting the numbers settle: relearning in ${left} s.`);
        await new Promise((r) => window.setTimeout(r, 1000));
      }
      const r = await api.post<{ closed: string[] }>("/api/fresh-start");
      setMessage(`Fresh start${r.closed?.length ? `: closed ${r.closed.join(", ")}` : ""}. VIGIL is relearning normal (about 3 minutes); faults unlock when it's ready.`);
      load();
    } catch (e) {
      setMessage(e instanceof Error ? `Fresh start failed: ${e.message}` : "Fresh start failed");
    }
  };

  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="false" aria-labelledby="chaos-title">
        <div className="hatch" aria-hidden="true" />
        <div className="drawer-head">
          <div>
            <h2 id="chaos-title">Chaos Lab</h2>
            <p>{REPLAY
              ? "Each fault plays back the real recorded run, start to finish: how VIGIL noticed, what it blamed, what it did and how long recovery took."
              : "Faults run in their own container. VIGIL has no code path to it and no chaos token: it only sees what Prometheus sees."}</p>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close Chaos Lab"><X size={18} /></button>
        </div>
        <div className="drawer-body">
          {error && <p className="error-text" role="alert">{error}</p>}
          {learning && (
            <p className="drawer-wait" role="status">
              VIGIL is still learning normal traffic ({Math.round(learning.progress * 100)}%
              {learning.eta != null && learning.eta >= 1 ? `, about ${Math.ceil(learning.eta)} s left` : ""}). Faults unlock when it's ready,
              so it's judged fairly.
            </p>
          )}
          {message && <p className="panel-sub" role="status">{message}</p>}
          {scenarios && Object.entries(scenarios).map(([name, s]) => {
            const active = isActive(name, status);
            return (
              <article key={name} className="fault" data-active={active}>
                <div className="fault-top">
                  <h3>{s.label}</h3>
                  <span className="target">{active ? "running" : s.target}</span>
                </div>
                <p>{s.description}</p>
                {ANSWER[name] && <span className="fault-key">Answer key: {ANSWER[name]}.</span>}
                <HoldButton label={learning ? "Wait: VIGIL is learning" : active ? "Running. Hold to start again" : REPLAY ? "Hold to play this fault" : "Hold to inject"}
                  doneLabel={REPLAY ? "Playing: watch VIGIL" : "Injected: watch VIGIL"} disabled={!!learning} onFire={() => inject(name, s.label)} />
              </article>
            );
          })}
        {!REPLAY && <article className="fault fault-deploy">
          <div className="fault-top">
            <h3>Bad deploy (payment v2)</h3>
            <span className="target">terminal</span>
          </div>
          <p>A developer ships payment v2 with a bug baked in: 40% of payments fail. Run this in the VS Code terminal:</p>
          <code className="fault-cmd">.\scripts\deploy.ps1 v2</code>
          <span className="fault-key">Answer key: regression after a deploy. A restart brings the bug back; only rolling payment back to v1 fixes it. At L2 VIGIL asks you first.</span>
        </article>}
        </div>
        <div className="drawer-foot">
          <HoldButton className="hold-reset" label={REPLAY ? "Hold to stop the replay" : "Hold to reset all faults"}
            doneLabel={REPLAY ? "Back to the calm start" : "All faults cleared"} holdMs={1200} onFire={reset} />
          <span className="panel-sub">{REPLAY
            ? "Stopping returns to the calm moment before the fault. In the live app, VIGIL never uses reset; it fixes faults with real container actions."
            : "Reset is for demo re-runs. VIGIL never uses it; it fixes faults with real container actions."}</span>
          {!REPLAY && <>
            <HoldButton className="hold-reset" label="Hold for a fresh start (relearn normal)" doneLabel="Fresh start running"
              holdMs={1500} onFire={freshStart} />
            <span className="panel-sub">Clears every fault, closes any open incident and makes VIGIL learn normal again from scratch (about 3 minutes). Needs the admin token.</span>
          </>}
        </div>
      </aside>
    </>
  );
}
