import { useEffect, useRef, type ReactNode, type MouseEvent as ReactMouseEvent } from "react";

// The home page's ground: big rounded slabs of light and smoked glass, stacked at
// different depths. The slab under the cursor sinks in and tips toward the point
// you're pressing, the rest stay put; move away and it rises back. Content sits
// above, so the slabs are found by position, not by pointer events.

// x, y: top-left in vw / vh. s: size in vmax. tone: light or dark glass. z: stacking depth.
const SLABS: Array<{ x: number; y: number; w: number; h: number; tone: "light" | "dark"; z: number }> = [
  { x: -6, y: -14, w: 30, h: 26, tone: "light", z: 1 },
  { x: 21, y: -20, w: 24, h: 22, tone: "dark", z: 2 },
  { x: 58, y: -16, w: 34, h: 30, tone: "light", z: 3 },
  { x: 86, y: 16, w: 20, h: 26, tone: "dark", z: 1 },
  { x: -8, y: 30, w: 17, h: 24, tone: "dark", z: 2 },
  { x: 30, y: 64, w: 30, h: 32, tone: "light", z: 1 },
  { x: 66, y: 50, w: 26, h: 24, tone: "dark", z: 3 },
  { x: 2, y: 66, w: 26, h: 28, tone: "light", z: 3 },
  { x: 42, y: 80, w: 22, h: 20, tone: "dark", z: 2 },
  { x: 84, y: 78, w: 20, h: 22, tone: "light", z: 2 },
];

export function GlassField() {
  const refs = useRef<Array<HTMLDivElement | null>>([]);
  useEffect(() => {
    if (window.matchMedia("(hover: none), (prefers-reduced-motion: reduce)").matches) return;
    let frame = 0;
    let pressed: HTMLDivElement | null = null;
    const order = SLABS.map((s, i) => ({ i, z: s.z })).sort((a, b) => b.z - a.z);
    const release = () => {
      if (pressed) { pressed.style.removeProperty("--rx"); pressed.style.removeProperty("--ry"); delete pressed.dataset.pressed; }
      pressed = null;
    };
    const onMove = (e: MouseEvent) => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        let hit: HTMLDivElement | null = null;
        let rect: DOMRect | null = null;
        for (const { i } of order) {
          const el = refs.current[i];
          if (!el) continue;
          const r = el.getBoundingClientRect();
          if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) { hit = el; rect = r; break; }
        }
        if (hit !== pressed) release();
        if (hit && rect) {
          // Tip toward the point under the cursor, as if pressing a pad there.
          const px = (e.clientX - rect.left) / rect.width - 0.5;
          const py = (e.clientY - rect.top) / rect.height - 0.5;
          hit.style.setProperty("--ry", `${(px * 9).toFixed(2)}deg`);
          hit.style.setProperty("--rx", `${(-py * 9).toFixed(2)}deg`);
          hit.dataset.pressed = "true";
          pressed = hit;
        }
      });
    };
    const onLeave = () => release();
    window.addEventListener("mousemove", onMove, { passive: true });
    document.addEventListener("mouseleave", onLeave);
    return () => { cancelAnimationFrame(frame); window.removeEventListener("mousemove", onMove); document.removeEventListener("mouseleave", onLeave); };
  }, []);
  return (
    <div className="glass-field" aria-hidden="true">
      {SLABS.map((s, i) => (
        <div key={i} ref={(el) => { refs.current[i] = el; }} className={`slab slab-${s.tone}`} data-z={s.z}
          style={{ left: `${s.x}vw`, top: `${s.y}vh`, width: `${s.w}vmax`, height: `${s.h}vmax`, zIndex: s.z }} />
      ))}
    </div>
  );
}

/** A card that tips toward the cursor: the corner you point at sinks, a soft glare follows. */
export function TiltCard({ children, className = "" }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const still = typeof window !== "undefined" && window.matchMedia("(hover: none), (prefers-reduced-motion: reduce)").matches;
  const move = (e: ReactMouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el || still) return;
    const r = el.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width;
    const py = (e.clientY - r.top) / r.height;
    el.style.setProperty("--ry", `${((px - 0.5) * 14).toFixed(2)}deg`);
    el.style.setProperty("--rx", `${(-(py - 0.5) * 14).toFixed(2)}deg`);
    el.style.setProperty("--gx", `${(px * 100).toFixed(1)}%`);
    el.style.setProperty("--gy", `${(py * 100).toFixed(1)}%`);
    el.dataset.tilt = "true";
  };
  const leave = () => {
    const el = ref.current;
    if (!el) return;
    el.style.setProperty("--rx", "0deg");
    el.style.setProperty("--ry", "0deg");
    delete el.dataset.tilt;
  };
  return (
    <div className={`tilt ${className}`} onMouseMove={move} onMouseLeave={leave}>
      <div className="tilt-body" ref={ref}>
        {children}
        <span className="tilt-glare" aria-hidden="true" />
      </div>
    </div>
  );
}
