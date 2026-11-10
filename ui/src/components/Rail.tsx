import { FlaskConical, ShoppingBag, Smartphone, Trophy, TriangleAlert } from "lucide-react";
import { TERMINAL } from "../api/types";
import { Link } from "../lib/router";
import { useVigil } from "../state/vigil";
import { Logo } from "./Logo";

export function Rail({ path, chaosOpen, onChaos }: { path: string; chaosOpen: boolean; onChaos: () => void }) {
  const { incidents, connected } = useVigil();
  const open = Object.values(incidents).filter((i) => !TERMINAL.includes(i.status)).length;
  const isMission = path.startsWith("/console");
  const isIncidents = path.startsWith("/incidents");
  return (
    <nav className="rail" aria-label="Main">
      <Link href="/console" className="rail-btn" aria-current={isMission ? "page" : undefined} aria-label="Mission view" title="Mission view">
        <Logo size={28} />
      </Link>
      <Link href="/incidents" className="rail-btn" aria-current={isIncidents ? "page" : undefined}
        aria-label={`Incidents${open ? `, ${open} open` : ""}`} title="Incidents">
        <TriangleAlert size={22} strokeWidth={1.75} />
        {open > 0 && <span className="badge">{open}</span>}
      </Link>
      <Link href="/proving-ground" className="rail-btn" aria-current={path.startsWith("/proving-ground") ? "page" : undefined}
        aria-label="Proving Ground scorecard" title="Proving Ground: VIGIL's scorecard">
        <Trophy size={22} strokeWidth={1.75} />
      </Link>
      <button type="button" className="rail-btn rail-chaos" aria-expanded={chaosOpen} onClick={onChaos}
        aria-label="Chaos Lab" title="Chaos Lab: break things on purpose">
        <FlaskConical size={22} strokeWidth={1.75} />
      </button>
      <Link href="/phones" className="rail-btn" aria-current={path.startsWith("/phones") ? "page" : undefined}
        aria-label="Phone checkout: pay from your phone" title="Phone checkout: pay from your phone">
        <Smartphone size={22} strokeWidth={1.75} />
      </Link>
      <a href="/store" target="_blank" rel="noreferrer" className="rail-btn" aria-label="Open the demo store in a new tab" title="Demo store">
        <ShoppingBag size={22} strokeWidth={1.75} />
      </a>
      <div className="rail-spacer" />
      <span className="rail-conn">
        <Logo size={20} tone={connected ? "ok" : "bad"} pulse={!connected}
          label={connected ? "Live stream connected" : "Live stream disconnected, reconnecting"} />
      </span>
    </nav>
  );
}
