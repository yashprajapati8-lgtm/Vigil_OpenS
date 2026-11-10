import { Check } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

// Press and hold to fire: a stray click mid-demo can't break or reset anything.
// While held, the button fills like liquid from the middle out, so everyone in
// the room can see it charging; when it fires it locks full and says so.
type Phase = "idle" | "holding" | "done";

export function HoldButton({ label, doneLabel = "Done", holdMs = 1200, onFire, disabled, className = "" }: {
  label: string; doneLabel?: string; holdMs?: number; onFire: () => void; disabled?: boolean; className?: string;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const timer = useRef<number | null>(null);
  const doneTimer = useRef<number | null>(null);

  const cancel = () => {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = null;
    setPhase((p) => (p === "holding" ? "idle" : p));
  };
  const start = () => {
    if (disabled || timer.current || phase === "done") return;
    setPhase("holding");
    timer.current = window.setTimeout(() => {
      timer.current = null;
      setPhase("done");
      onFire();
      doneTimer.current = window.setTimeout(() => setPhase("idle"), 1800);
    }, holdMs);
  };
  useEffect(() => () => {
    if (timer.current) window.clearTimeout(timer.current);
    if (doneTimer.current) window.clearTimeout(doneTimer.current);
  }, []);

  const key = (e: KeyboardEvent, down: boolean) => {
    if (e.key !== " " && e.key !== "Enter") return;
    e.preventDefault();
    if (down && !e.repeat) start();
    if (!down) cancel();
  };

  return (
    <button type="button" className={`hold ${className}`} data-phase={phase} disabled={disabled}
      style={{ ["--hold-ms" as string]: `${holdMs}ms` }} aria-live="polite"
      onPointerDown={start} onPointerUp={cancel} onPointerLeave={cancel} onPointerCancel={cancel}
      onKeyDown={(e) => key(e, true)} onKeyUp={(e) => key(e, false)} onBlur={cancel}>
      <span className="hold-fill" aria-hidden="true"><span className="hold-wave" /></span>
      <span className="hold-label">
        {phase === "done" ? <><Check size={16} strokeWidth={2.6} /> {doneLabel}</>
          : phase === "holding" ? "Keep holding…" : label}
      </span>
    </button>
  );
}
