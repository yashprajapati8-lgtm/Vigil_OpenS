import { useCallback, useEffect, useState } from "react";
import { ApprovalCard } from "./components/ApprovalCard";
import { Banners } from "./components/Banners";
import { ChaosDrawer } from "./components/ChaosDrawer";
import { ControlCapsule } from "./components/ControlCapsule";
import { OperatorModal } from "./components/OperatorModal";
import { Rail } from "./components/Rail";
import { getToken } from "./api/client";
import { usePath } from "./lib/router";
import { Home } from "./pages/Home";
import { IncidentDetail } from "./pages/IncidentDetail";
import { Incidents } from "./pages/Incidents";
import { Mission } from "./pages/Mission";
import { PhoneCheckout } from "./pages/PhoneCheckout";
import { Phones } from "./pages/Phones";
import { PhoneToasts } from "./components/PhoneToasts";
import { ProvingGround } from "./pages/ProvingGround";
import { Store } from "./pages/Store";
import { useVigil, VigilProvider } from "./state/vigil";
import { REPLAY } from "./replay/player";

export function App() {
  const path = usePath();
  if (path.startsWith("/store")) return <Store />;
  if (path === "/pay" || path.startsWith("/pay/")) return <PhoneCheckout />;
  if (path === "/" || path === "") return <Home />;
  return (
    <VigilProvider>
      <Console path={path} />
    </VigilProvider>
  );
}

/** Operator actions need the admin token. Ask for it once, then run what was asked. */
export type RunAsOperator = (action: () => Promise<void>) => void;

function Console({ path }: { path: string }) {
  const { focus } = useVigil();
  const [chaosOpen, setChaosOpen] = useState(false);
  const [unlocked, setUnlocked] = useState(() => Boolean(getToken()));
  const [pending, setPending] = useState<null | (() => Promise<void>)>(null);
  const [asking, setAsking] = useState(false);

  const runAsOperator: RunAsOperator = useCallback(
    (action) => {
      if (unlocked || REPLAY) {
        void action();
        return;
      }
      setPending(() => action);
      setAsking(true);
    },
    [unlocked],
  );

  const incidentId = path.match(/^\/incidents\/([A-Za-z0-9-]+)/)?.[1];
  const page = incidentId ? (
    <IncidentDetail id={incidentId} />
  ) : path.startsWith("/proving-ground") ? (
    <ProvingGround />
  ) : path.startsWith("/phones") ? (
    <Phones />
  ) : path.startsWith("/incidents") ? (
    <Incidents />
  ) : (
    <Mission onOpenChaos={() => setChaosOpen(true)} />
  );
  useEffect(() => {
    const open = () => setChaosOpen(true);
    window.addEventListener("vigil:open-chaos", open);
    return () => window.removeEventListener("vigil:open-chaos", open);
  }, []);
  useEffect(() => {
    const name = incidentId ? incidentId : path.startsWith("/proving-ground") ? "Proving Ground"
      : path.startsWith("/incidents") ? "Incidents" : path.startsWith("/phones") ? "Phone checkout" : "Mission";
    document.title = `VIGIL | ${name}`;
  }, [path, incidentId]);

  return (
    <div className={focus?.status === "AWAITING_APPROVAL" ? "shell has-approval" : "shell"}>
      <Rail path={path} chaosOpen={chaosOpen} onChaos={() => setChaosOpen((v) => !v)} />
      <main className="main">
        <ControlCapsule
          unlocked={unlocked}
          runAsOperator={runAsOperator}
          onLock={() => setAsking(true)}
          onLocked={() => setUnlocked(false)}
        />
        <Banners />
        {page}
      </main>
      <ApprovalCard runAsOperator={runAsOperator} />
      {!REPLAY && <PhoneToasts />}
      {chaosOpen && <ChaosDrawer onClose={() => setChaosOpen(false)} />}
      {asking && (
        <OperatorModal
          onCancel={() => {
            setAsking(false);
            setPending(null);
          }}
          onUnlocked={() => {
            setUnlocked(true);
            setAsking(false);
            const next = pending;
            setPending(null);
            if (next) void next();
          }}
        />
      )}
    </div>
  );
}
