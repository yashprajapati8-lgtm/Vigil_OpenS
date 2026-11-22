import { useRef } from "react";
import { GraduationCap } from "lucide-react";
import { Link } from "../lib/router";

// The college results-day demo, from the Round 3 demo video (screen recordings plus explainer animations). The rest of
// this replay plays the demo shop's recorded runs; this run is shown as video.

const CHAPTERS: Array<{ at: number; label: string }> = [
  { at: 0, label: "Without VIGIL" },
  { at: 28, label: "With VIGIL: it fixes the rush alone" },
  { at: 85, label: "Results days: ready before the rush" },
];

export function ResultsDayRun() {
  const video = useRef<HTMLVideoElement>(null);

  const jump = (at: number) => {
    const v = video.current;
    if (!v) return;
    v.currentTime = at;
    void v.play().catch(() => undefined);
  };

  return (
    <div className="rd">
      <section className="panel" aria-labelledby="rdr-title">
        <h1 id="rdr-title" className="page-title"><GraduationCap size={22} aria-hidden="true" /> Results day: the college demo</h1>
        <p className="rd-lede">
          From the Round 3 demo video: screen recordings of the Nexora college results portal on results day, first on
          its own, then with VIGIL beside it, with short explainer animations between them. One copy of the results service serves about 40 students a second; when results are
          declared, 60 a second arrive.
        </p>

        <video ref={video} className="rdr-video" src="/results-day/results-day.mp4" poster="/results-day/poster.jpg"
          controls preload="metadata" playsInline>
          Your browser cannot play this video.
        </video>

        <div className="rdr-chapters" role="group" aria-label="Jump to a part">
          {CHAPTERS.map((c) => (
            <button key={c.at} type="button" className="btn btn-ghost btn-small" onClick={() => jump(c.at)}>
              {`${Math.floor(c.at / 60)}:${String(c.at % 60).padStart(2, "0")}`} {c.label}
            </button>
          ))}
        </div>

        <ul className="rdr-facts">
          <li><b>Without VIGIL:</b> about 4 in 5 students get an error page within 10 seconds, while /health still says OK.</li>
          <li><b>With VIGIL:</b> it sees students failing, finds real demand on results-api (not a crash), starts the spare
            copy on its own, and closes the incident only after 4 healthy checks in a row.</li>
          <li><b>Results days:</b> the admin adds the date in the console, and VIGIL starts the spare copy before the rush.
            Try it yourself in the <Link href="/results-days">Results days</Link> tab.</li>
        </ul>
      </section>
    </div>
  );
}
