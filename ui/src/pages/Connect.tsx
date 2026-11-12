import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { useSite } from "../lib/site";
import { useVigil } from "../state/vigil";

// Connect your app: how to put VIGIL beside your own site, step by step, with the
// exact code and files. It only shows and copies; nothing here changes anything.
// Every snippet is the real thing (instrument/, scripts/vigil_init.py, deploy/),
// rewritten for the example picked, so the reader sees VIGIL speak their site's words.

interface Svc { name: string; about: string; calls: string[]; allow: string[] }
interface Example {
  id: string;
  label: string;
  site: string;
  entry: string;
  entryAbout: string;
  unit: string;
  units: string;
  metric: string;
  duration: string;
  route: string;          // the customer route, FastAPI template
  ok: string[];
  failed: string[];
  calls: string[];        // what the entry calls
  services: Svc[];
  autonomy: string;
  saved: number;          // for the "how VIGIL will talk" preview
  root: string;
}

const EXAMPLES: Example[] = [
  {
    id: "results", label: "Results portal", site: "University results portal",
    entry: "web", entryAbout: "Where students check results",
    unit: "result lookup", units: "result lookups",
    metric: "result_requests_total", duration: "result_request_duration_seconds", route: "/result/{roll}",
    ok: ["ok", "not_found"], failed: ["failed", "timeout"], calls: ["results-api"],
    services: [
      { name: "results-api", about: "Looks up a student's marks", calls: ["auth", "pdf-render"], allow: ["restart", "rollback", "scale_out"] },
      { name: "auth", about: "Checks roll number and date of birth", calls: [], allow: [] },
      { name: "pdf-render", about: "Renders the marksheet PDF", calls: [], allow: ["restart", "scale_out"] },
    ],
    autonomy: "L1", saved: 4120, root: "pdf-render",
  },
  {
    id: "shop", label: "Online shop", site: "Campus store",
    entry: "storefront", entryAbout: "Where customers check out",
    unit: "checkout", units: "checkouts",
    metric: "checkout_requests_total", duration: "checkout_duration_seconds", route: "/checkout",
    ok: ["ok"], failed: ["failed", "timeout"], calls: ["payment"],
    services: [
      { name: "payment", about: "Charges the card", calls: ["inventory"], allow: ["restart", "rollback", "scale_out"] },
      { name: "inventory", about: "Reserves the stock", calls: [], allow: ["restart", "rollback"] },
    ],
    autonomy: "L2", saved: 840, root: "inventory",
  },
];

const list = (xs: string[]) => `[${xs.join(", ")}]`;

function vigilYaml(e: Example): string {
  return [
    "version: 1",
    "site:",
    `  name: ${e.site}`,
    "customers:",
    `  entry: ${e.entry}                 # where customers arrive: measured, never touched`,
    `  about: ${e.entryAbout}`,
    `  calls: ${list(e.calls)}`,
    `  unit: ${e.unit}`,
    `  units: ${e.units}`,
    `  metric: ${e.metric}`,
    `  ok: ${list(e.ok)}`,
    `  failed: ${list(e.failed)}`,
    `  duration_metric: ${e.duration}`,
    "services:",
    ...e.services.flatMap((s) => [
      `  ${s.name}:`,
      `    about: ${s.about.includes("'") ? JSON.stringify(s.about) : s.about}`,
      `    calls: ${list(s.calls)}`,
      `    allow: ${list(s.allow)}${s.allow.length ? "" : "              # watch and diagnose, a human fixes"}`,
    ]),
    `autonomy: ${e.autonomy}`,
  ].join("\n");
}

function serviceCode(e: Example): string {
  const s = e.services[0];
  const targets = s.calls.map((c) => `"${c}:8000": "${c}"`).join(", ");
  return [
    "# in every service VIGIL should watch, e.g. " + s.name,
    "from vigil_instrument import AsyncVigilTransport, Vigil, instrument_fastapi",
    "",
    `vigil = instrument_fastapi(app, Vigil(targets={${targets}}))`,
    "http = httpx.AsyncClient(transport=AsyncVigilTransport(vigil))   # calls to your other services",
  ].join("\n");
}

function entryCode(e: Example): string {
  return [
    `# only in ${e.entry}: count what customers come to do`,
    `${e.id === "results" ? "lookups" : "checkouts"} = vigil.customer("${e.metric}", duration="${e.duration}")`,
    `vigil.count_route("${e.id === "results" ? "GET" : "POST"}", "${e.route}", ${e.id === "results" ? "lookups" : "checkouts"})`,
  ].join("\n");
}

function labelsYaml(e: Example): string {
  const s = e.services[0];
  return [
    `# your docker-compose.yml`,
    `  ${s.name}:`,
    "    labels:",
    '      vigil.managed: "true"',
    `      vigil.service: ${s.name}`,
    "      vigil.version: v1          # same as the app's APP_VERSION",
  ].join("\n");
}

function CopyBlock({ code, label }: { code: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked (http, old browser): the code is on screen to select */
    }
  };
  return (
    <div className="cx-code">
      <button type="button" className="cx-copy" onClick={copy} aria-label={`Copy ${label}`}>
        {copied ? <><Check size={14} aria-hidden="true" /> Copied</> : <><Copy size={14} aria-hidden="true" /> Copy</>}
      </button>
      <pre tabIndex={0} aria-label={label}><code>{code}</code></pre>
    </div>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <li className="cx-step">
      <span className="cx-num" aria-hidden="true">{n}</span>
      <div className="cx-step-body">
        <h3>{title}</h3>
        {children}
      </div>
    </li>
  );
}

const FIX_WORD: Record<string, string> = { restart: "restart", rollback: "roll back", scale_out: "add capacity" };

function WatchingNow() {
  const site = useSite();
  const { health, verdicts } = useVigil();
  const autonomy = health?.control?.autonomy;
  const config = health?.site?.config ?? null;
  return (
    <section className="panel cx-now" aria-labelledby="cx-now-title">
      <div className="panel-head">
        <h2 id="cx-now-title">Watching now</h2>
        <span className="panel-sub">{config ? "from vigil.yaml" : "built-in demo"}</span>
      </div>
      <p className="cx-now-site">{site.name}</p>
      <p className="cx-now-line">Customers arrive at <b>{site.entry}</b> to make <b>{site.units}</b>.</p>
      <ul className="cx-now-list">
        {site.services.map((name) => {
          const v = verdicts[name];
          const reporting = !!v && v.state !== "unknown";
          const allowed = health?.site?.allowed?.[name];
          return (
            <li key={name}>
              <span className="cx-dot" data-on={reporting || undefined} aria-hidden="true" />
              <span className="cx-now-name">{name}</span>
              <span className="cx-now-state" data-on={reporting || undefined}>{reporting ? "reporting" : "no data yet"}</span>
              {allowed && (
                <span className="cx-now-meta">
                  {allowed.length ? `may ${allowed.map((a) => FIX_WORD[a] ?? a).join(", ")}` : "watch only: a human fixes"}
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {autonomy && <p className="cx-now-line">Autonomy <b>{autonomy}</b>{config ? <> · <code className="cx-inline">{config}</code></> : null}</p>}
      {health?.alerts && (
        <p className="cx-now-line cx-now-alerts">
          Email alerts{" "}
          {health.alerts.email === "on"
            ? <><b>on</b>, to {health.alerts.to?.join(", ")}{health.alerts.last_error ? <span className="cx-now-err"> · last send failed</span> : null}</>
            : <><b>off</b>: set them in .env</>}
        </p>
      )}
    </section>
  );
}

const READY: Array<[string, boolean, string]> = [
  ["FastAPI services", true, "vigil-instrument, two lines per service"],
  ["Docker Compose", true, "restart, roll back, add capacity on labelled containers"],
  ["Email alerts", true, "when VIGIL acts, needs you, escalates or can't see"],
  ["Approve from email or phone", false, "next"],
  ["Any website without code changes (PHP and others)", false, "next"],
  ["Node (Express)", false, "next"],
  ["Kubernetes", false, "planned"],
];

export function Connect() {
  const [exId, setExId] = useState(EXAMPLES[0].id);
  const e = EXAMPLES.find((x) => x.id === exId) ?? EXAMPLES[0];
  const symptom = e.services[0].name === e.root ? e.entry : e.services[0].name;

  return (
    <div className="cx">
      <div className="cx-main">
        <section className="panel" aria-labelledby="cx-title">
          <h1 id="cx-title" className="page-title">Connect your app</h1>
          <p className="pg-lede cx-lede">
            Put VIGIL beside your own site. It learns your services, talks in your customers' words, and only ever
            uses the fixes you allow. About 20 minutes; nothing on this page changes anything.
          </p>

          <div className="cx-switch" role="radiogroup" aria-label="Example site">
            {EXAMPLES.map((x) => (
              <button key={x.id} type="button" role="radio" aria-checked={x.id === e.id} onClick={() => setExId(x.id)}>
                {x.label}
              </button>
            ))}
          </div>

          <figure className="cx-preview" key={e.id} aria-label="How VIGIL will report an incident on this site">
            <figcaption>How VIGIL will report on {e.site.toLowerCase()}</figcaption>
            <p><span className="cx-tag">Root cause</span> {e.root} (stuck process). {symptom} is only a symptom.</p>
            <p><span className="cx-tag">Decision</span> restart {e.root}: saves ~{e.saved.toLocaleString("en-IN")} {e.units} in the next 5 min.</p>
            <p><span className="cx-tag">Verified</span> {e.units} back to normal, 4 checks in a row.</p>
          </figure>
        </section>

        <section className="panel" aria-labelledby="cx-steps-title">
          <div className="panel-head"><h2 id="cx-steps-title">Five steps</h2><span className="panel-sub">{e.label}</span></div>
          <ol className="cx-steps">
            <Step n={1} title="Make each service report">
              <p>Install the package, then two lines per FastAPI service. It serves <code className="cx-inline">/metrics</code>; nothing else changes.</p>
              <CopyBlock label="install command" code="pip install ./instrument/python" />
              <CopyBlock label="service code" code={serviceCode(e)} />
              <CopyBlock label="entry service code" code={entryCode(e)} />
            </Step>
            <Step n={2} title="Describe your site">
              <p>Answer a few questions. It writes these files and checks them with the same rules VIGIL uses at start.</p>
              <CopyBlock label="setup command" code="python scripts/vigil_init.py" />
              <CopyBlock label="vigil.yaml" code={vigilYaml(e)} />
            </Step>
            <Step n={3} title="Label what VIGIL may touch">
              <p>VIGIL acts only on containers labelled like this, and only with the fixes <code className="cx-inline">allow</code> lists.</p>
              <CopyBlock label="container labels" code={labelsYaml(e)} />
            </Step>
            <Step n={4} title="Start VIGIL beside your app">
              <p>Only VIGIL: engine, actuator, console and a Prometheus that joins your app's network. None of the demo.
                To get emails, add your SMTP details to <code className="cx-inline">deploy/.env</code> first.</p>
              <CopyBlock label="email settings" code={"VIGIL_SMTP_HOST=smtp.gmail.com\nVIGIL_SMTP_USER=you@gmail.com\nVIGIL_SMTP_PASSWORD=<a Gmail app password>\nVIGIL_ALERT_TO=oncall@yourcollege.edu"} />
              <CopyBlock label="start command" code={"cd deploy\ndocker compose up --build -d"} />
            </Step>
            <Step n={5} title="Watch it learn">
              <p>
                Open <code className="cx-inline">http://localhost:3000</code>. Your map appears in your words; for 3 minutes
                each service says "learning", then VIGIL keeps learning from healthy traffic on its own.
                Start at <b>{e.autonomy}</b>: {e.autonomy === "L1" ? "VIGIL proposes, you click." : "VIGIL acts alone only on low-risk fixes it is sure of."}
              </p>
            </Step>
          </ol>
        </section>
      </div>

      <aside className="cx-side">
        <WatchingNow />
        <section className="panel" aria-labelledby="cx-ready-title">
          <div className="panel-head"><h2 id="cx-ready-title">Works with</h2></div>
          <ul className="cx-ready">
            {READY.map(([what, ready, how]) => (
              <li key={what} data-ready={ready || undefined}>
                <span className="cx-ready-mark" aria-hidden="true">{ready ? <Check size={14} /> : null}</span>
                <span><b>{what}</b><small>{ready ? how : `${how}: not built yet`}</small></span>
              </li>
            ))}
          </ul>
          <p className="cx-note">Full guide: <code className="cx-inline">docs/ADD_TO_YOUR_APP.md</code></p>
        </section>
      </aside>
    </div>
  );
}
