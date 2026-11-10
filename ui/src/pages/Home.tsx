import { BookOpen, FolderGit2, MonitorPlay, Play } from "lucide-react";
import { LIVE_VIDEO_URL } from "../lib/links";
import { useEffect, useState } from "react";
import { GlassField, TiltCard } from "../components/GlassField";
import { Logo } from "../components/Logo";
import { duration, num } from "../lib/format";
import { Link } from "../lib/router";
import { REPLAY, REPO_URL } from "../replay/player";

// The front door. A judge should understand VIGIL in 30 seconds here, then open
// the console (live, or the recorded run on the hosted link) or watch the video.

const NOBODY = { secs: 22 * 60 + 31, lost: 20336 };   // the same fault, nobody acting: still failing when the test ended (measured)

interface Row { scenario: string; label: string; pass?: boolean; mttd_s?: number | null; mttr_s?: number | null; checkouts_lost?: number | null; actions?: string[]; root?: string | null }
interface Card { summary: { passed: number; total: number; median_detect_s: number | null; median_recover_s: number | null; false_alarms: number; started_at: number }; rows: Row[] }

function useScorecard(): Card | null {
  const [card, setCard] = useState<Card | null>(null);
  useEffect(() => {
    fetch("/proving-ground/scorecard.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setCard(d && d.summary ? d : null))
      .catch(() => setCard(null));
  }, []);
  return card;
}

// ---- hero: the flagship fault, told in five frames
type Tone = "ok" | "slow" | "symptom" | "root" | "acting";
const FRAMES: Array<{ cap: string; sf: Tone; pay: Tone; inv: Tone; wire: boolean; chip: string }> = [
  { cap: "Three services serve every checkout. All healthy.", sf: "ok", pay: "ok", inv: "ok", wire: false, chip: "Watching" },
  { cap: "Inventory slows down. Its health check stays green, but checkouts start failing.", sf: "symptom", pay: "symptom", inv: "slow", wire: true, chip: "Detected" },
  { cap: "Payment looks guilty. VIGIL follows the slow calls: inventory is the cause, payment only a symptom.", sf: "symptom", pay: "symptom", inv: "root", wire: true, chip: "Diagnosed" },
  { cap: "It restarts inventory, the cause, through a locked actuator. Restarting payment would fix nothing.", sf: "symptom", pay: "symptom", inv: "acting", wire: true, chip: "Acting" },
  { cap: "Checkouts stay healthy 4 checks in a row. Only then does VIGIL call it fixed.", sf: "ok", pay: "ok", inv: "ok", wire: false, chip: "Resolved" },
];
const STATE: Record<Tone, string> = { ok: "healthy", slow: "slow, health check green", symptom: "failing", root: "root cause", acting: "VIGIL restarting" };

function HeroLoop({ resolvedIn }: { resolvedIn: string | null }) {
  const [i, setI] = useState(0);
  const [auto, setAuto] = useState(true);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) { setI(2); setAuto(false); }
  }, []);
  useEffect(() => {
    if (!auto) return;
    const t = window.setTimeout(() => setI((n) => (n + 1) % FRAMES.length), i === FRAMES.length - 1 ? 4200 : 3000);
    return () => window.clearTimeout(t);
  }, [i, auto]);
  const f = FRAMES[i];
  const node = (name: string, role: string, tone: Tone) => (
    <div className="hl-node" data-tone={tone}>
      {tone === "root" && <span className="hl-badge">Root cause</span>}
      {tone === "acting" && <span className="hl-badge hl-badge-act">VIGIL acting</span>}
      <b>{name}</b>
      <span className="hl-role">{role}</span>
      <span className="hl-state"><i aria-hidden="true" />{name === "storefront" && tone === "symptom" ? "customers failing" : STATE[tone]}</span>
    </div>
  );
  return (
    <figure className="hero-loop" aria-label="How VIGIL handles a slow inventory service, in five steps">
      <div className="hl-top">
        <span className="hl-title">Slow inventory, replayed</span>
        <span className="hl-chip" data-step={i}>{f.chip === "Resolved" && resolvedIn ? `Resolved in ${resolvedIn}` : f.chip}</span>
      </div>
      <div className="hl-flow">
        {node("storefront", "checkout", f.sf)}
        <span className="hl-wire" data-bad={f.wire && f.sf !== "ok" ? true : undefined} aria-hidden="true" />
        {node("payment", "charges the card", f.pay)}
        <span className="hl-wire" data-bad={f.wire ? true : undefined} aria-hidden="true" />
        {node("inventory", "reserves stock", f.inv)}
      </div>
      <figcaption className="hl-cap" aria-live="polite">{f.cap}</figcaption>
      <div className="hl-dots" role="group" aria-label="Steps">
        {FRAMES.map((fr, k) => (
          <button key={k} type="button" aria-label={`Step ${k + 1}: ${fr.chip}`} aria-pressed={k === i}
            onClick={() => { setI(k); setAuto(false); }} />
        ))}
      </div>
    </figure>
  );
}

const STEPS: Array<[string, string]> = [
  ["Detect", "Learns each service's normal for 3 minutes, then flags numbers far outside it. Opens an incident only when customers are hurt, so a traffic spike or a busy laptop pages no one."],
  ["Diagnose", "A blame walk follows failing calls to the service whose own dependencies are healthy. An LLM answers the same question without seeing the walk's answer. Agreement means High confidence."],
  ["Decide", "Prices every possible fix in checkouts saved over the next 5 minutes, minus checkouts it drops. Restarting a symptom scores below zero."],
  ["Act", "Applies the fix through a locked actuator that can only restart, start or stop a labelled container. No shell, no other access."],
  ["Verify", "Done means services and real checkouts stay normal 4 checks in a row. If not, it tries once more, then hands over to a human."],
];

const FAULTS: Array<{ scenario: string; name: string; what: string; fix: string }> = [
  { scenario: "slow-inventory", name: "Slow inventory", what: "A gray failure: slow but 'healthy'. Payment fails because of it.", fix: "Restart inventory, not payment" },
  { scenario: "bad-deploy", name: "Bad deploy", what: "Payment v2 ships with a bug: 40% of payments fail.", fix: "Roll back to v1. A restart would bring the bug back" },
  { scenario: "flash-sale", name: "Flash sale", what: "6× real traffic. Nothing is broken, payment is just full.", fix: "Add a replica, never restart. Remove it when traffic drops" },
  { scenario: "payment-leak", name: "Memory leak", what: "Payment leaks memory and would crash in about a minute.", fix: "Restart before it runs out of memory" },
  { scenario: "payment-runaway", name: "Runaway loop", what: "A busy loop burns a CPU core in payment.", fix: "Restart payment" },
];

const SECTIONS: Array<[string, string]> = [["how", "How it works"], ["proof", "Proof"], ["safety", "Safety"]];

const LEVELS: Array<[string, string]> = [
  ["L0", "Watch only. Detects and diagnoses, never acts."],
  ["L1", "Proposes every fix. A human approves each one."],
  ["L2", "Acts alone only when confidence is High and the fix is low risk. Otherwise asks."],
  ["L3", "Acts alone on every fix the guardrails allow."],
];

export function Home() {
  const card = useScorecard();
  const rows = Object.fromEntries((card?.rows ?? []).map((r) => [r.scenario, r]));
  const flagship = rows["slow-inventory"];
  const resolvedIn = flagship?.mttr_s != null ? duration(flagship.mttr_s) : null;
  const s = card?.summary;
  useEffect(() => { document.title = "VIGIL | Autonomous resilience engine"; }, []);
  const [scrolled, setScrolled] = useState(false);
  const [active, setActive] = useState<string | null>(null);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) setActive(e.target.id || null);
    }, { rootMargin: "-45% 0px -50% 0px" });
    for (const id of ["top", ...SECTIONS.map(([x]) => x)]) { const el = document.getElementById(id); if (el) io.observe(el); }
    return () => { window.removeEventListener("scroll", onScroll); io.disconnect(); };
  }, []);

  return (
    <div className="home">
      <GlassField />
      <header className="home-bar" data-scrolled={scrolled || undefined}>
        <div className="nav-capsule">
          <a href="#top" className="home-brand" aria-label="VIGIL, back to top"><Logo size={28} /><span>VIGIL</span></a>
          <nav className="home-nav" aria-label="Sections">
            {SECTIONS.map(([id, label]) => (
              <a key={id} href={`#${id}`} aria-current={active === id ? "true" : undefined}>{label}</a>
            ))}
          </nav>
          <span className="nav-status" title={REPLAY ? "This link plays back a real recorded run" : "Connected to the running stack"}>
            <i aria-hidden="true" data-live={!REPLAY || undefined} />{REPLAY ? "Recorded run" : "Live stack"}
          </span>
          {REPO_URL && <a className="nav-icon" href={REPO_URL} target="_blank" rel="noreferrer" aria-label="Code on GitHub" title="Code on GitHub"><FolderGit2 size={18} /></a>}
          <Link href="/console" className="btn btn-primary nav-cta"><MonitorPlay size={16} /> Open the console</Link>
        </div>
      </header>

      <main>
        <section className="home-hero" id="top">
          <div className="hero-copy">
            <h1>Watches every service. Fixes the real cause.</h1>
            <p className="hero-lede">
              VIGIL is an autonomous resilience engine for microservices. When customers start failing, it finds the
              service that is actually broken, not just the loudest one, applies the right fix inside strict guardrails,
              and only calls it done when real checkouts recover.
            </p>
            <div className="hero-ctas">
              <Link href="/console" className="btn btn-primary btn-lg"><MonitorPlay size={18} /> Open the console</Link>
              <a className="btn btn-lg" href={LIVE_VIDEO_URL} target="_blank" rel="noreferrer"><Play size={18} /> Watch it live (3 min)</a>
              {REPO_URL && <a className="btn btn-lg" href={REPO_URL} target="_blank" rel="noreferrer"><FolderGit2 size={18} /> Code on GitHub</a>}
            </div>
            <p className="hero-note">
              {REPLAY
                ? "The console plays back a real run recorded on our laptop. Open Chaos Lab and hold a fault to watch exactly how VIGIL handled it."
                : "Live: open Chaos Lab in the console and hold a fault. Open the demo store beside it to feel each failure as a customer."}
            </p>
          </div>
          <TiltCard className="hero-tilt"><HeroLoop resolvedIn={resolvedIn} /></TiltCard>
        </section>

        <section className="home-section problem" aria-labelledby="problem-title">
          <div className="sec-copy">
            <h2 id="problem-title">The outage that hurts most is the one that looks healthy</h2>
            <p>
              Inventory slows down, but its health check stays green. Payment, which calls it, starts failing, so payment
              looks guilty. Restart payment and nothing changes. Kubernetes won't catch it either: the health check
              passes, so nothing gets restarted. At 3 a.m. someone has to work that out by hand while customers leave.
            </p>
          </div>
          <div className="race" role="img"
            aria-label={`Same fault. Nobody acting: still failing after ${duration(NOBODY.secs)}, ${num(NOBODY.lost)} checkouts lost.${flagship?.mttr_s != null ? ` With VIGIL: ${duration(flagship.mttr_s)}, ${num(flagship.checkouts_lost ?? 0)} lost.` : ""}`}>
            <div className="race-row">
              <span className="race-who">Nobody acting</span>
              <span className="race-bar race-bad" style={{ width: "100%" }} />
              <span className="race-val"><b>{duration(NOBODY.secs)}</b> and still failing, {num(NOBODY.lost)} checkouts lost</span>
            </div>
            {flagship?.mttr_s != null && (
              <div className="race-row">
                <span className="race-who">With VIGIL</span>
                <span className="race-bar race-good" style={{ width: `${Math.max(2, (flagship.mttr_s / NOBODY.secs) * 100)}%` }} />
                <span className="race-val"><b>{duration(flagship.mttr_s)}</b> {num(flagship.checkouts_lost ?? 0)} checkouts lost</span>
              </div>
            )}
            <p className="race-note">The same fault, injected for real. With nobody acting it was still failing when we ended the test; with VIGIL it is timed from the moment the fault was injected to verified recovery.</p>
          </div>
        </section>

        <section id="how" className="home-section" aria-labelledby="how-title">
          <h2 id="how-title">One loop, every 5 seconds</h2>
          <ol className="loop-steps">
            {STEPS.map(([name, text], k) => (
              <li key={name}><span className="ls-n" aria-hidden="true">{k + 1}</span><h3>{name}</h3><p>{text}</p></li>
            ))}
          </ol>
        </section>

        <section id="proof" className="home-section" aria-labelledby="proof-title">
          <div className="sec-head">
            <h2 id="proof-title">The right fix for each failure, proven</h2>
            <p>
              The Proving Ground injects every fault for real and scores VIGIL against an answer key it never sees.
              It also runs a quiet period with nothing broken to count false alarms.
            </p>
          </div>
          {s && (
            <dl className="proof-stats">
              <div><dt>Scenarios passed</dt><dd>{s.passed}/{s.total}</dd></div>
              <div><dt>Median time to detect</dt><dd>{s.median_detect_s != null ? `${Math.round(s.median_detect_s)} s` : "–"}</dd></div>
              <div><dt>Median time to verified recovery</dt><dd>{s.median_recover_s != null ? duration(s.median_recover_s) : "–"}</dd></div>
              <div><dt>False alarms</dt><dd>{s.false_alarms}</dd></div>
            </dl>
          )}
          <div className="fault-grid">
            {FAULTS.map((f) => {
              const r = rows[f.scenario];
              return (
                <article key={f.scenario} className="fault-card">
                  <div className="fc-top">
                    <h3>{f.name}</h3>
                    {r && <span className={`chip ${r.pass ? "chip-ok" : "chip-bad"}`}>{r.pass ? "Passed" : "Failed"}</span>}
                  </div>
                  <p>{f.what}</p>
                  <p className="fc-fix">{f.fix}</p>
                  {r && (r.mttd_s != null || r.mttr_s != null) && (
                    <p className="fc-nums">
                      {r.mttd_s != null && <span>Detected in <b>{Math.round(r.mttd_s)} s</b></span>}
                      {r.mttr_s != null && <span>Recovered in <b>{duration(r.mttr_s)}</b></span>}
                    </p>
                  )}
                </article>
              );
            })}
          </div>
          <p className="proof-link"><Link href="/proving-ground">See the full scorecard</Link></p>
        </section>

        <section id="safety" className="home-section safety" aria-labelledby="safety-title">
          <div className="sec-copy">
            <h2 id="safety-title">Safe by design, humans in control</h2>
            <ul className="safe-list">
              <li>The LLM is only a second opinion. It never runs commands or picks fixes.</li>
              <li>Faults run in their own container. VIGIL can't see them and only knows what Prometheus measures.</li>
              <li>Cooldowns, a limit of 2 attempts per incident and an action budget. Never a restart loop.</li>
              <li><b>Actions armed</b> is a kill switch: one click and VIGIL keeps watching but touches nothing.</li>
            </ul>
          </div>
          <div className="levels">
            <h3>Autonomy, chosen by the operator</h3>
            <dl>
              {LEVELS.map(([lvl, text]) => <div key={lvl}><dt>{lvl}</dt><dd>{text}</dd></div>)}
            </dl>
          </div>
        </section>

        <section className="home-section built" aria-labelledby="built-title">
          <h2 id="built-title">Built with</h2>
          <ul className="tech-stack">
            <li><b>Engine</b> Python, FastAPI, scikit-learn IsolationForest, 104 tests</li>
            <li><b>Telemetry</b> Prometheus golden signals</li>
            <li><b>Second opinion</b> Groq, gpt-oss-120b</li>
            <li><b>Console</b> React, TypeScript, Vite, live server-sent events</li>
            <li><b>Runs on</b> Docker Compose: 3 services, load generator, chaos controller</li>
          </ul>
          <p className="team">Built by Yash Prajapati (team Yash_P) for the Baloon National Hackathon 2026.</p>
        </section>
      </main>

      <footer className="home-foot">
        <Link href="/console" className="btn btn-primary"><MonitorPlay size={16} /> Open the console</Link>
        {REPO_URL && <a className="btn" href={REPO_URL} target="_blank" rel="noreferrer"><BookOpen size={16} /> Read the code</a>}
      </footer>
    </div>
  );
}
