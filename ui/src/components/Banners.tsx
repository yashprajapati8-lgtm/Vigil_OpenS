import { CircleAlert, FlaskConical, GraduationCap, Pause, Play, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { Point } from "../api/types";
import { player, REPLAY, REPO_URL } from "../replay/player";
import { duration } from "../lib/format";
import { LIVE_VIDEO_URL } from "../lib/links";
import { useVigil } from "../state/vigil";
import { LearningChart } from "./LearningChart";

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
  const { history } = useVigil();
  const [picked, setPicked] = useState<string | null>(null);
  if (!l) return null;
  const pct = Math.round(l.progress * 100);
  // Show the service still learning slowest, unless the person picked one.
  const learning = l.services.filter((v) => v.state === "learning");
  let slowest = learning[0];
  for (const v of learning) {
    if ((v.learning_progress ?? 0) < (slowest.learning_progress ?? 0)) slowest = v;
  }
  const shown = l.services.find((v) => v.service === picked) ?? slowest;
  const shownProgress = shown.state === "learning" ? shown.learning_progress ?? 0 : 1;
  return (
    <section className="learning panel" role="status" aria-live="polite" aria-label={`VIGIL is learning normal traffic, ${pct}%`}>
      <div className="learning-icon" aria-hidden="true"><GraduationCap size={22} /></div>
      <div className="learning-body">
        <div className="learning-top">
          <h2>VIGIL is learning what normal looks like</h2>
          <span className="learning-num"><b>{pct}%</b>{l.eta != null && <span>{l.eta < 1 ? "finishing" : `about ${duration(l.eta)} left`}</span>}</span>
        </div>
        <div className="learning-services" role="tablist" aria-label="Service to show">
          {l.services.map((v) => (
            <button key={v.service} type="button" role="tab" aria-selected={v.service === shown.service}
              data-done={v.state !== "learning"} onClick={() => setPicked(v.service)}>
              {v.service} {v.state === "learning" ? `${Math.round((v.learning_progress ?? 0) * 100)}%` : "ready"}
            </button>
          ))}
        </div>
        <LearningChart service={shown.service} history={history} progress={shownProgress} />
        <p>
          Every dot is {shown.service}'s p95 latency, 5 s apart. VIGIL takes the median, so readings near normal
          shape the band and a far spike barely moves it. Detection starts at 100%; don't inject faults until then.
          After that it keeps learning, from healthy readings only: a spike it flags is never learned.
        </p>
      </div>
    </section>
  );
}

// Replay only: the live app relearns normal after a Fresh start, which a recording
// can't do. This plays the recording's own p95 readings through the same chart and
// the same maths, sped up (one 5 s reading every 0.4 s), so the learning is shown
// with real data and nothing invented.
const LEARN_WINDOW = 36;
const LEARN_STEP_MS = 400;

function ReplayLearnPanel() {
  const { history } = useVigil();
  const [readings, setReadings] = useState<Point[] | null>(null);
  const [n, setN] = useState(0);
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    const start = () => {
      setReadings(history.slice(-LEARN_WINDOW));
      setN(4);
    };
    window.addEventListener("vigil:replay-learn", start);
    return () => window.removeEventListener("vigil:replay-learn", start);
  }, [history]);

  const total = readings ? readings.length : 0;
  useEffect(() => {
    if (!readings || n >= total) return;
    const t = window.setTimeout(() => setN((x) => x + 1), LEARN_STEP_MS);
    return () => window.clearTimeout(t);
  }, [readings, n, total]);

  if (!readings || total < 4) return null;
  const services: string[] = [];
  for (const p of readings) {
    for (const s of Object.keys(p.services)) {
      if (!services.includes(s)) services.push(s);
    }
  }
  const shown = picked && services.includes(picked) ? picked : services[0];
  const progress = Math.min(1, n / LEARN_WINDOW);
  const done = n >= total;
  const pct = done ? 100 : Math.round(progress * 100);
  return (
    <section className="learning panel" role="status" aria-live="polite" aria-label={`VIGIL is learning normal traffic, ${pct}%`}>
      <div className="learning-icon" aria-hidden="true"><GraduationCap size={22} /></div>
      <div className="learning-body">
        <div className="learning-top">
          <h2>{done ? "VIGIL has learned what normal looks like" : "VIGIL is learning what normal looks like"}</h2>
          <span className="learning-num"><b>{pct}%</b><span>{done ? "detection on" : "recorded readings, sped up"}</span></span>
          <button type="button" className="icon-btn" aria-label="Close the learning chart" onClick={() => setReadings(null)}><X size={16} /></button>
        </div>
        <div className="learning-services" role="tablist" aria-label="Service to show">
          {services.map((s) => (
            <button key={s} type="button" role="tab" aria-selected={s === shown} data-done={done}
              onClick={() => setPicked(s)}>{s} {done ? "ready" : `${pct}%`}</button>
          ))}
        </div>
        <LearningChart service={shown} history={readings.slice(0, n)} progress={done ? 1 : progress} />
        <p>
          Every dot is {shown}'s p95 latency from this recording, 5 s apart (played here at 0.4 s each). VIGIL takes
          the median, so readings near normal shape the band and a far spike barely moves it. In the live app this
          takes about 3 minutes after a Fresh start; after that it keeps learning, from healthy readings only.
        </p>
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
      {REPLAY && <ReplayLearnPanel />}
      <LearningPanel />
      {health?.control.paused && (
        <div className="banner banner-warn" role="status">
          <Pause size={18} /> Actions paused. VIGIL is still watching and diagnosing, but it won't touch anything.
        </div>
      )}
    </>
  );
}
