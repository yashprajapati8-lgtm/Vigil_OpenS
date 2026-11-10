import { createContext, useContext } from "react";

/** Operator actions need the admin token. Ask for it once, then run what was asked. */
export type RunAsOperator = (action: () => Promise<void>) => void;

/** For operator buttons deep inside a page (e.g. "Accept new normal" on a service card). */
export const OperatorContext = createContext<RunAsOperator>((action) => void action());
export const useOperator = () => useContext(OperatorContext);
