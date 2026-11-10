import { ShieldCheck } from "lucide-react";
import type { Control, Incident } from "../api/types";
import { TERMINAL } from "../api/types";
import { num } from "../lib/format";

const LEVEL_TEXT: Record<Control["autonomy"], string> = {
  L0: "Observe only: VIGIL detects and diagnoses, and never acts.",
  L1: "Advise: VIGIL proposes every fix and waits for a human.",
  L2: "Supervised: VIGIL fixes low-risk problems alone when it is highly confident, and asks for the rest.",
  L3: "Autonomous: VIGIL applies every allowed fix alone, inside its guardrails.",
};

export function DecisionPanel({ incident, control }: { incident: Incident | null; control?: Control }) {
  const decision = incident?.data.decision;
  const diag = incident?.data.diagnosis;

  if (!decision || !decision.options?.length) {
    const g = control?.guardrails ?? {};
    return (
      <section className="panel area-decision" aria-labelledby="decision-title">
        <div className="panel-head">
          <h2 id="decision-title">What VIGIL may do</h2>
          <span className="panel-sub">autonomy {control?.autonomy ?? "–"}</span>
        </div>
        <p className="panel-caption">The rules VIGIL follows before it touches anything. You can tighten them with the autonomy level at the top.</p>
        <ul className="rules rules-wide">
          <li><ShieldCheck size={16} />{control ? LEVEL_TEXT[control.autonomy] : "Loading"}</li>
          <li><ShieldCheck size={16} />Acts alone only when its blame walk and the LLM agree on the cause.</li>
          <li><ShieldCheck size={16} />Same fix on the same service at most once every {Math.round((g.cooldown_s ?? 120) / 60)} minutes; {g.max_attempts ?? 2} tries per incident, then a human.</li>
          <li><ShieldCheck size={16} />At most {g.budget_actions ?? 3} actions in {Math.round((g.budget_window_s ?? 600) / 60)} minutes, and none on stale data.</li>
          <li><ShieldCheck size={16} />Its only hands: restart, start or stop a labelled container. No shell.</li>
        </ul>
      </section>
    );
  }

  const opts = decision.options;
  const max = Math.max(1, ...opts.map((o) => Math.abs(o.saves)), ...opts.map((o) => o.drops));
  const live = incident && !TERMINAL.includes(incident.status);
  return (
    <section className="panel area-decision" aria-labelledby="decision-title">
      <div className="panel-head">
        <h2 id="decision-title">Why this fix</h2>
        <span className="panel-sub">checkouts saved in {decision.horizon_min ?? 5} min</span>
      </div>
      <p className="panel-caption">Every option VIGIL considered, priced in checkouts it would save in the next {decision.horizon_min ?? 5} minutes. The best safe option wins.</p>
      <div className="decision-body">
      <ol className="ledger">
        {opts.map((o) => {
          const chosen = o.action === decision.action && o.target === decision.target;
          const width = `${(Math.max(o.saves, 0) / max) * 100}%`;
          return (
            <li key={o.label} className="ledger-row" data-chosen={chosen}>
              <span className="ledger-name">{o.label}{chosen ? (live ? " (doing this)" : " (did this)") : ""}</span>
              <span className="ledger-score" style={{ color: o.score < 0 ? "#f9a39b" : undefined }}>{o.score > 0 ? "+" : ""}{num(o.score)}</span>
              <span className="ledger-bar" aria-hidden="true">
                <span className="ledger-fill" style={{ width, background: chosen ? "var(--vigil)" : "#5b524c" }} />
                {o.drops > 0 && (
                  <span className="ledger-fill" style={{ left: "auto", right: 0, width: `${(o.drops / max) * 100}%`, background: "rgba(224,87,76,0.75)", minWidth: 3 }} />
                )}
              </span>
              <span className="ledger-note">
                {o.action === "wait" ? o.note : `saves ~${num(o.saves)}, costs ${num(o.drops)} during the restart; ${o.note}`}
              </span>
            </li>
          );
        })}
      </ol>
      {diag && (
        <dl className="facts">
          <dt>Confidence</dt><dd>{diag.confidence}: {diag.confidence_reasons.join("; ")}</dd>
          <dt>Decided by</dt><dd>{decision.acted_alone ? `VIGIL alone (${decision.why})` : decision.why ?? "a human"}</dd>
          {diag.llm.hypothesis && (<><dt>LLM said</dt><dd>“{diag.llm.hypothesis}”</dd></>)}
        </dl>
      )}
      </div>
    </section>
  );
}
