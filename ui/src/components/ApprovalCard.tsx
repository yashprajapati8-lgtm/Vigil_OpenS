import { Hand } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import type { RunAsOperator } from "../App";
import { CATEGORY_LABEL, num } from "../lib/format";
import { useVigil } from "../state/vigil";

// Appears by itself when VIGIL needs a human, docked at the bottom so the map
// and the loop stay visible above it. Never auto-approves.
export function ApprovalCard({ runAsOperator }: { runAsOperator: RunAsOperator }) {
  const { focus } = useVigil();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!focus || focus.status !== "AWAITING_APPROVAL" || !focus.data.decision?.action) return null;
  const d = focus.data.decision;
  const diag = focus.data.diagnosis;
  const chosen = d.options.find((o) => o.action === d.action && o.target === d.target);
  const verb = chosen?.label ?? `${d.action} ${d.target}`;
  const Verb = verb.charAt(0).toUpperCase() + verb.slice(1);

  const act = (what: "approve" | "reject") =>
    runAsOperator(async () => {
      setBusy(true);
      setError(null);
      try {
        await api.post(`/api/incidents/${focus.id}/${what}`, undefined, true);
      } catch (e) {
        setError(e instanceof Error ? e.message : "That didn't go through. Try again.");
      } finally {
        setBusy(false);
      }
    });

  return (
    <aside className="approval" role="alertdialog" aria-labelledby="approve-title" aria-describedby="approve-why">
      <div className="approval-icon" aria-hidden="true"><Hand size={20} /></div>
      <div className="approval-text">
        <h2 id="approve-title">{Verb}?</h2>
        <p id="approve-why">
          {diag
            ? <>Root cause: <strong>{diag.root}</strong> ({(CATEGORY_LABEL[diag.category] ?? diag.category).toLowerCase()}), confidence <strong>{diag.confidence}</strong>. VIGIL asks first: {d.why ?? "a human must approve"}. </>
            : <>{focus.title}. VIGIL asks first. </>}
          This should save about <strong>{num(chosen?.saves ?? 0)}</strong> checkouts in the next {d.horizon_min ?? 5} minutes.
        </p>
        {error && <p className="error-text" role="alert">{error}</p>}
      </div>
      <div className="approval-actions">
        <button type="button" className="btn btn-primary" disabled={busy} onClick={() => act("approve")}>Approve {verb}</button>
        <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => act("reject")}>Reject and take over</button>
      </div>
    </aside>
  );
}
