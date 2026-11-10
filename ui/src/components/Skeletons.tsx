import { CircleAlert, RotateCw } from "lucide-react";

// Placeholders shaped exactly like the real panels, so nothing jumps when data lands.

function Bar({ w = "100%", h = 12, r = 8 }: { w?: string | number; h?: number; r?: number }) {
  return <span className="sk" style={{ width: w, height: h, borderRadius: r }} />;
}

function Head({ title }: { title: string }) {
  return (
    <div className="panel-head">
      <h2>{title}</h2>
      <Bar w={110} h={10} />
    </div>
  );
}

export function MissionSkeleton() {
  return (
    <div className="mission" aria-busy="true" aria-label="Loading VIGIL">
      <div className="mission-main">
        <section className="panel">
          <Head title="Service map" />
          <div className="sk-map">
            {[0, 1, 2].map((i) => <span key={i} className="sk sk-node" />)}
          </div>
        </section>
        <div className="mission-top">
          <div className="col">
            <section className="panel">
              <Head title="Signals" />
              <div className="signals">
                {[0, 1, 2, 3].map((i) => <div key={i} className="chart sk-chart"><Bar w="45%" /><span className="sk sk-plot" /></div>)}
              </div>
            </section>
          </div>
          <div className="col col-loop">
            <section className="panel">
              <Head title="The loop" />
              <div className="steps">
                {[0, 1, 2, 3, 4].map((i) => <span key={i} className="sk sk-step" />)}
              </div>
            </section>
          </div>
        </div>
      </div>
      <div className="col col-feed">
        <section className="panel">
          <Head title="What VIGIL is doing" />
          <div className="sk-feed">
            {[0, 1, 2, 3, 4, 5].map((i) => <span key={i} className="sk-feed-row"><Bar w={56} h={10} /><Bar w={`${88 - i * 7}%`} /></span>)}
          </div>
        </section>
      </div>
    </div>
  );
}

export function RowsSkeleton({ rows = 5, title }: { rows?: number; title: string }) {
  return (
    <section className="panel" aria-busy="true" aria-label={`Loading ${title}`}>
      <div className="panel-head"><h1 className="page-title">{title}</h1></div>
      <div className="sk-rows">
        {Array.from({ length: rows }, (_, i) => <span key={i} className="sk sk-row" />)}
      </div>
    </section>
  );
}

export function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <section className="panel load-error" role="alert">
      <span className="load-error-icon" aria-hidden="true"><CircleAlert size={22} /></span>
      <div>
        <h2>VIGIL's engine isn't answering</h2>
        <p>{message}. If the stack is stopped, start it with <code className="fault-cmd">docker compose up -d</code>, then retry.</p>
        <button type="button" className="btn" onClick={onRetry}><RotateCw size={16} /> Try again</button>
      </div>
    </section>
  );
}
