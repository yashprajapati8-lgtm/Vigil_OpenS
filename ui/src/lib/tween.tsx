import { useEffect, useRef, useState } from "react";

// Numbers that change every 5 s glide to their new value instead of jumping, so
// the eye can follow which way they moved. Off when the person asked for less motion.

const reduced = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

export function useTweened(value: number | null | undefined, durationMs = 520): number | null {
  const [shown, setShown] = useState<number | null>(value ?? null);
  const from = useRef<number | null>(value ?? null);
  const frame = useRef<number | null>(null);

  useEffect(() => {
    if (value == null || from.current == null || reduced() || !Number.isFinite(value)) {
      from.current = value ?? null;
      setShown(value ?? null);
      return;
    }
    const start = from.current;
    const target = value;
    if (start === target) return;
    const t0 = performance.now();
    const step = (now: number) => {
      const t = Math.min(1, (now - t0) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3);          // ease-out: fast start, gentle landing
      const v = start + (target - start) * eased;
      from.current = v;
      setShown(v);
      if (t < 1) frame.current = requestAnimationFrame(step);
    };
    if (frame.current != null) cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(step);
    return () => {
      if (frame.current != null) cancelAnimationFrame(frame.current);
    };
  }, [value, durationMs]);

  return shown;
}

/** A number that glides between updates. `format` gets the in-between values too. */
export function Tween({ value, format }: { value: number | null | undefined; format: (v: number | null) => string }) {
  const v = useTweened(value);
  return <>{format(v)}</>;
}
