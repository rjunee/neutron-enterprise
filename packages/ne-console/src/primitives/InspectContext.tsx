// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// REDESIGN §1.4 — the INSPECT switch. This is the mechanism that makes ONE
// console serve both buyers rather than forcing two products (.impeccable.md:
// "Progressive disclosure is the answer, not two products"). Layers:
//   READOUT   plain English + at most one figure. No hash, no snake_case, no endpoint.
//   EVIDENCE  named rows, dates, counts — behind <EvidenceDrawer>.
//   RAW       hashes, UUIDs, policy ids, file paths, /v1/… — inside drawers only.
// INSPECT ON force-opens every EvidenceDrawer on the page and persists for the
// session. The ops leader never touches it; the architect flips it once.

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

const KEY = "ne.inspect";

interface InspectState {
  inspect: boolean;
  toggle: () => void;
}

const InspectContext = createContext<InspectState>({ inspect: false, toggle: () => {} });

export function InspectProvider({ children }: { children: ReactNode }) {
  const [inspect, setInspect] = useState<boolean>(() => {
    try {
      return sessionStorage.getItem(KEY) === "on";
    } catch {
      return false;
    }
  });

  const toggle = useCallback(() => {
    setInspect((prev) => {
      const next = !prev;
      try {
        sessionStorage.setItem(KEY, next ? "on" : "off");
      } catch {
        // storage unavailable — the switch still works for this page view.
      }
      return next;
    });
  }, []);

  // Keyboard shortcut: "I" toggles inspection from anywhere that is not a field.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "i" && e.key !== "I") return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const el = e.target as HTMLElement | null;
      const tag = el?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el?.isContentEditable)
        return;
      toggle();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle]);

  return <InspectContext.Provider value={{ inspect, toggle }}>{children}</InspectContext.Provider>;
}

export function useInspect(): InspectState {
  return useContext(InspectContext);
}
