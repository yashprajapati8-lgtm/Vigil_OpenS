import { CircleAlert, FlaskConical, GraduationCap, Pause, Play } from "lucide-react";
import { useEffect, useState } from "react";
import { player, REPLAY, REPO_URL } from "../replay/player";
import { duration } from "../lib/format";
import { LIVE_VIDEO_URL } from "../lib/links";
import { useVigil } from "../state/vigil";

/** Overall learning state: slowest service decides; the countdown ticks every second
 *  between the 5 s updates so the bar never looks frozen. */
export function useLearning() {
  const { verdicts, latest } = useVigil();
  const [, tick] = useState(0);
  const learning = Object.values(verdicts).filter((v) => v.state === "learning");
  const active = learning.length > 0;
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [active]);
  if (!active) return null;
  const since = latest ? Math.max(0, Date.now() / 1000 - latest.ts) : 0;
  const etas = learning.map((v) => v.learning_eta_s).filter((e): e is number => e != null);
  const eta = etas.length ? Math.max(0, Math.max(...etas) - since) : null;
  const progress = Math.min(...learning.map((v) => v.learning_progress ?? 0));
  return { progress, eta, services: Object.values(verdicts) };
}

function LearningPanel() {
  const l = useLearning();
  if (!l) return null;
  const pct = Math.round(l.progress * 100);
  return (
    <section className="learning panel" role="status" aria-live="polite" aria-label={`VIGIL is learning normal traffic, ${pct}%`}>
      <div className="learning-icon" aria-hidden="true"><GraduationCap size={22} /></div>
      <div className="learning-body">
        <div className="learning-top">
          <h2>VIGIL is learning what normal looks like</h2>
          <span className="learning-num"><b>{pct}%</b>{l.eta != null && <span>{l.eta < 1 ? "finishing" : `about ${duration(l.eta)} left`}</span>}</span>
        </div>
        <div className="learning-bar" aria-hidden="true"><span style={{ width: `${Math.max(2, pct)}%` }} /></div>
        <p>
          It watches normal traffic first, so it never judges a service against a guess. Detection starts at 100%;
          don't inject faults until then.
        </p>
        <div className="learning-services">
          {l.services.map((v) => (
            <span key={v.service} data-done={v.state !== "learning"}>
              {v.service} {v.state === "learning" ? `${Math.round((v.learning_progress ?? 0) * 100)}%` : "ready"}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

function useReplayState() {
  const [, tick] = useState(0);
  useEffect(() => player.onChange(() => tick((n) => n + 1)), []);
  return player;
}

function ReplayBanner() {
  const p = useReplayState();
  const when = p.index ? new Date(p.index.recorded_at * 1000) : null;
  const date = when ? `${when.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}, ${when.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : "";
  const now = p.current?.label ?? "";
  const short = now.replace(/\s*\(.*\)$/, "");
  const status = p.hold === "approval" ? "Waiting for you: approve or reject the fix below."
    : p.hold === "blocked" ? "Holding: actions are paused. Arm them to let VIGIL act."
    : p.phase === "playing" ? `Playing: ${short}`
    : p.phase === "done" ? (p.rejected ? "You rejected the fix and took over. Pick a fault to play again." : `Finished: ${short}`)
    : "Pick a fault to play it.";
  const yours = p.changedFromRecording ? ` Recorded at L3; now playing at ${p.control?.paused ? "your settings (actions paused)" : `your setting, ${p.control?.autonomy}`}.` : "";
  return (
    <div className="replay-banner" role="status">
      <span className="replay-dot" data-playing={p.phase === "playing"} aria-hidden="true"><Play size={12} /></span>
      <span className="replay-text" title={`A real run recorded on the team's laptop: every number on screen comes from that run.${yours}`}>
        <b>Recorded run</b>, {date}. <span className="replay-now">{status}</span>
      </span>
      <span className="replay-actions">
        <button type="button" className="btn btn-small btn-chaos" onClick={() => window.dispatchEvent(new Event("vigil:open-chaos"))}>
          <FlaskConical size={15} /> Pick a fault
        </button>
        <a className="btn btn-small btn-primary" href={LIVE_VIDEO_URL} target="_blank" rel="noreferrer"><Play size={14} /> Watch it live (3 min)</a>
        {REPO_URL && <a className="btn btn-small" href={REPO_URL} target="_blank" rel="noreferrer">Run it live</a>}
      </span>
    </div>
  );
}

export function Banners() {
  const { health, connected, loaded } = useVigil();
  if (loaded && !connected) {
    return (
      <div className="banner banner-bad" role="status">
        <CircleAlert size={18} /> The live stream dropped. Reconnecting; the screen refreshes itself when it's back.
      </div>
    );
  }
  return (
    <>
      {REPLAY && <ReplayBanner />}
      <LearningPanel />
      {health?.control.paused && (
        <div className="banner banner-warn" role="status">
          <Pause size={18} /> Actions paused. VIGIL is still watching and diagnosing, but it won't touch anything.
        </div>
      )}
    </>
  );
}
