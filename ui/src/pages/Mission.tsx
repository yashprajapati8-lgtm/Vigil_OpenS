import { useLearning } from "../components/Banners";
import { DecisionPanel } from "../components/DecisionPanel";
import { LoopStepper } from "../components/LoopStepper";
import { Narration } from "../components/Narration";
import { ServiceMap } from "../components/ServiceMap";
import { Signals } from "../components/Signals";
import { LoadError, MissionSkeleton } from "../components/Skeletons";
import { StartHere } from "../components/StartHere";
import { REPLAY } from "../replay/player";
import { useVigil } from "../state/vigil";

// Top: the service map, full width, so every card says what is happening to it.
// Under it the signals beside how far VIGIL has got (the loop); the reasoning
// behind the fix runs full width underneath; the plain-English feed stays pinned on the right.
export function Mission({ onOpenChaos }: { onOpenChaos: () => void }) {
  const { focus, events, verdicts, health, loaded, error, refresh } = useVigil();
  const learning = useLearning();
  if (!loaded) return error ? <LoadError message={error} onRetry={refresh} /> : <MissionSkeleton />;
  return (
    <>
      {REPLAY ? (
        <StartHere replay onOpenChaos={onOpenChaos} />
      ) : (
        <StartHere onOpenChaos={onOpenChaos} busy={focus ? "incident" : learning ? "learning" : null} />
      )}
      <div className="mission">
        <div className="mission-main">
          <ServiceMap />
          <div className="mission-top">
            <div className="col">
              <Signals />
            </div>
            <div className="col col-loop">
              <LoopStepper incident={focus} events={events} verdicts={verdicts} />
            </div>
          </div>
          <DecisionPanel incident={focus} control={health?.control} />
        </div>
        <div className="col col-feed">
          <Narration events={events} />
        </div>
      </div>
    </>
  );
}
