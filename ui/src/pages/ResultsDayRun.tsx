import { useRef } from "react";
import { MonitorPlay } from "lucide-react";
import { Link } from "../lib/router";

// Demo videos: screen recordings of VIGIL running, with short explainer animations.
// The rest of this replay plays recorded runs of the demo shop; these show the same
// system end to end, including the college results portal and a real phone paying.

interface Chapter { at: number; label: string }

function DemoVideo({ id, title, lede, src, poster, chapters, facts }: {
  id: string; title: string; lede: string; src: string; poster: string;
  chapters: Chapter[]; facts: Array<[string, string]>;
}) {
  const video = useRef<HTMLVideoElement>(null);

  const jump = (at: number) => {
    const v = video.current;
    if (!v) return;
    v.currentTime = at;
    void v.play().catch(() => undefined);
  };

  return (
    <section className="panel rdr-section" aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="rdr-title">{title}</h2>
      <p className="rd-lede">{lede}</p>
      <video ref={video} className="rdr-video" src={src} poster={poster} controls preload="metadata" playsInline>
        Your browser cannot play this video.
      </video>
      <div className="rdr-chapters" role="group" aria-label={`Jump to a part of ${title}`}>
        {chapters.map((c) => (
          <button key={c.at} type="button" className="btn btn-ghost btn-small" onClick={() => jump(c.at)}>
            {`${Math.floor(c.at / 60)}:${String(c.at % 60).padStart(2, "0")}`} {c.label}
          </button>
        ))}
      </div>
      <ul className="rdr-facts">
        {facts.map(([head, text]) => (
          <li key={head}><b>{head}</b> {text}</li>
        ))}
      </ul>
    </section>
  );
}

export function ResultsDayRun() {
  return (
    <div className="rd">
      <h1 className="page-title"><MonitorPlay size={22} aria-hidden="true" /> Demo</h1>

      <DemoVideo
        id="demo-results"
        title="College results day"
        lede="The Nexora college results portal on results day, first on its own, then with VIGIL beside it. One copy of the results service serves about 40 students a second; when results are declared, 60 a second arrive."
        src="/results-day/results-day.mp4"
        poster="/results-day/poster.jpg"
        chapters={[
          { at: 0, label: "Without VIGIL" },
          { at: 28, label: "With VIGIL: it fixes the rush alone" },
          { at: 85, label: "Results days: ready before the rush" },
        ]}
        facts={[
          ["Without VIGIL:", "about 4 in 5 students get an error page within 10 seconds, while /health still says OK."],
          ["With VIGIL:", "it sees students failing, finds real demand on results-api (not a crash), starts the spare copy on its own, and closes the incident only after 4 healthy checks in a row."],
          ["Results days:", "the admin adds the date in the console, and VIGIL starts the spare copy before the rush."],
        ]}
      />

      <DemoVideo
        id="demo-store"
        title="Online store: a real phone pays, VIGIL fixes two faults"
        lede="The Lumen Goods demo shop (storefront, payment, inventory) running on a laptop. A real phone pays through it while VIGIL watches every service."
        src="/demo/store-demo.mp4"
        poster="/demo/store-poster.jpg"
        chapters={[
          { at: 0, label: "What downtime costs" },
          { at: 93, label: "The console" },
          { at: 128, label: "A real phone pays" },
          { at: 148, label: "Inventory gets stuck" },
          { at: 172, label: "Flash sale" },
        ]}
        facts={[
          ["Gray failure:", "inventory stalls but its health check stays green; payment shows the errors. VIGIL blames inventory, restarts it, and the phone pays again."],
          ["Flash sale:", "6 times the traffic is real demand, not a fault. A restart would drop every customer mid-sale, so VIGIL adds a second payment server instead."],
          ["Email:", "VIGIL emails the admin what broke, what it found and every step it took."],
        ]}
      />

      <p className="rdr-try">
        Try it yourself: play a recorded fault with <b>Pick a fault</b> above, or add a date in the{" "}
        <Link href="/results-days">Results days</Link> tab.
      </p>
    </div>
  );
}
