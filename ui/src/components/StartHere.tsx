import { Activity, ArrowUpRight, FlaskConical, ShoppingBag, SlidersHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";

// The first thing a cold visitor sees when nothing is happening: three steps, in
// order, that make VIGIL do its job in about a minute. Hidden once dismissed; the
// help button in the top bar brings it back.

const KEY = "vigil.guide.hidden";

function readHidden(): boolean {
  try {
    return localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

// Set by the help button so the guide shows even mid-incident, and even when the
// button was pressed on another page (the guide mounts after the event fired).
let requested = false;

export function showGuide() {
  requested = true;
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* private mode: the event below still shows it for this visit */
  }
  window.dispatchEvent(new Event("vigil:guide"));
}

type Busy = "incident" | "learning" | null;

export function StartHere({ onOpenChaos, replay = false, busy = null }: { onOpenChaos: () => void; replay?: boolean; busy?: Busy }) {
  // The replay starts playing by itself, so its guide waits for the help button.
  // Live, it shows by itself only while nothing is happening; the help button opens it any time.
  const [hidden, setHidden] = useState(() => (replay ? !requested : readHidden() && !requested));
  const [asked, setAsked] = useState(() => requested);
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const scroll = () => window.setTimeout(() => ref.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    const show = () => {
      setHidden(false);
      setAsked(true);
      scroll();
    };
    if (requested) scroll();
    window.addEventListener("vigil:guide", show);
    return () => window.removeEventListener("vigil:guide", show);
  }, []);
  if (hidden || (busy && !asked && !replay)) return null;

  const hide = () => {
    requested = false;
    setAsked(false);
    setHidden(true);
    if (replay) return;
    try {
      localStorage.setItem(KEY, "1");
    } catch {
      /* fine: hidden for this visit */
    }
  };

  if (replay) {
    return (
      <section className="panel guide" aria-labelledby="guide-title" ref={ref}>
        <div className="panel-head">
          <h2 id="guide-title">Try VIGIL yourself</h2>
          <button type="button" className="btn btn-ghost btn-small" onClick={hide}>Hide guide</button>
        </div>
        <p className="panel-caption">This page replays real runs recorded on our laptop. Nothing here can break anything, so change any setting and watch what VIGIL does.</p>
        <ol className="guide-steps">
          <li className="guide-step">
            <span className="guide-num" aria-hidden="true">1</span>
            <span className="guide-icon" aria-hidden="true"><SlidersHorizontal size={18} /></span>
            <b>Choose how much VIGIL may do</b>
            <span>Pick an autonomy level at the top. <i>L3</i> fixes alone, as recorded. <i>L2</i> asks before a rollback. <i>L1</i> and <i>L0</i> ask you to approve every fix. Turn <i>Actions</i> off and VIGIL holds.</span>
          </li>
          <li className="guide-step">
            <span className="guide-num" aria-hidden="true">2</span>
            <span className="guide-icon guide-icon-chaos" aria-hidden="true"><FlaskConical size={18} /></span>
            <b>Break something</b>
            <span>In Chaos Lab, hold a fault to play its recorded run. <i>Slow inventory</i> is the tricky one: payment fails, but inventory is the cause.</span>
            <button type="button" className="btn btn-small btn-chaos" onClick={onOpenChaos}>Pick a fault</button>
          </li>
          <li className="guide-step">
            <span className="guide-num" aria-hidden="true">3</span>
            <span className="guide-icon guide-icon-vigil" aria-hidden="true"><Activity size={18} /></span>
            <b>Watch it heal, or approve it</b>
            <span>The map marks the real cause and the loop fills in. Below L3 a card asks you to approve or reject the fix; then VIGIL checks real checkouts recover.</span>
            <span className="guide-note">Time you take to approve is added to the recovery time</span>
          </li>
        </ol>
      </section>
    );
  }

  return (
    <section className="panel guide" aria-labelledby="guide-title" ref={ref}>
      <div className="panel-head">
        <h2 id="guide-title">See VIGIL work in about a minute</h2>
        <button type="button" className="btn btn-ghost btn-small" onClick={hide}>Hide guide</button>
      </div>
      <p className="panel-caption">
        {busy === "incident"
          ? "An incident is open right now, so VIGIL is busy fixing it. Let it finish (the map turns green again), then break one thing at a time."
          : busy === "learning"
            ? "VIGIL is still learning what normal looks like, which takes about 3 minutes. Wait for that to finish, then break something."
            : "Everything is running and healthy. Break something on purpose and watch VIGIL find the real cause and fix it, on its own."}
      </p>
      <ol className="guide-steps">
        <li className="guide-step">
          <span className="guide-num" aria-hidden="true">1</span>
          <span className="guide-icon" aria-hidden="true"><ShoppingBag size={18} /></span>
          <b>Shop like a customer</b>
          <span>Buy something in the demo store. Every purchase is a real checkout through payment and inventory.</span>
          <a className="btn btn-small" href="/store" target="_blank" rel="noreferrer">Open the store <ArrowUpRight size={15} /></a>
        </li>
        <li className="guide-step">
          <span className="guide-num" aria-hidden="true">2</span>
          <span className="guide-icon guide-icon-chaos" aria-hidden="true"><FlaskConical size={18} /></span>
          <b>Break something</b>
          <span>In Chaos Lab, hold <i>Slow down inventory</i>. Customers start failing, but every health check stays green.</span>
          <button type="button" className="btn btn-small btn-chaos" onClick={onOpenChaos}>Open Chaos Lab</button>
        </li>
        <li className="guide-step">
          <span className="guide-num" aria-hidden="true">3</span>
          <span className="guide-icon guide-icon-vigil" aria-hidden="true"><Activity size={18} /></span>
          <b>Watch it heal</b>
          <span>The map marks the real cause, the loop fills in step by step, and VIGIL restarts the right service. Then it checks customers recovered.</span>
          <span className="guide-note">No clicks needed at autonomy L2</span>
        </li>
      </ol>
    </section>
  );
}
