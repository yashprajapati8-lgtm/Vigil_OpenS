import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, setToken } from "../api/client";
import { REPLAY } from "../replay/player";

export function OperatorModal({ onCancel, onUnlocked }: { onCancel: () => void; onUnlocked: () => void }) {
  return REPLAY ? <ReplayNote onClose={onCancel} /> : <TokenForm onCancel={onCancel} onUnlocked={onUnlocked} />;
}

/** The hosted replay has no engine to protect, so there is nothing to unlock: say so instead of asking for a token. */
function ReplayNote({ onClose }: { onClose: () => void }) {
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    button.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="op-title">
        <h2 id="op-title">Operator controls are open here</h2>
        <p>
          This page replays real runs, so autonomy, the kill switch and approvals only change what plays in your
          browser. No token is needed: try L1 and approve the fix yourself.
        </p>
        <p>
          On the live stack (<code>docker compose up</code>) the same controls change what VIGIL may do to real
          containers, so they ask for the admin token set as <code>VIGIL_ADMIN_TOKEN</code> in <code>.env</code>.
        </p>
        <div className="modal-actions">
          <button ref={button} type="button" className="btn btn-primary" onClick={onClose}>Got it</button>
        </div>
      </div>
    </div>
  );
}

function TokenForm({ onCancel, onUnlocked }: { onCancel: () => void; onUnlocked: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    input.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setToken(value.trim());
    try {
      await api.post("/api/operator/check", undefined, true);
      onUnlocked();
    } catch (err) {
      setToken(null);
      setError(err instanceof ApiError && err.status === 403
        ? "That token doesn't match VIGIL_ADMIN_TOKEN in your .env."
        : err instanceof Error ? err.message : "Couldn't check the token.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onCancel()}>
      <form className="modal" role="dialog" aria-modal="true" aria-labelledby="op-title" onSubmit={submit}>
        <h2 id="op-title">Unlock operator controls</h2>
        <p>Autonomy, the kill switch and approvals change what VIGIL may do. Enter the admin token to use them.</p>
        <label className="field">
          Admin token
          <input ref={input} type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} />
        </label>
        {error && <p className="error-text" role="alert">{error}</p>}
        <div className="modal-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={!value.trim() || busy}>{busy ? "Checking" : "Unlock"}</button>
        </div>
      </form>
    </div>
  );
}
