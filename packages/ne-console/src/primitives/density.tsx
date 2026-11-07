// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.1 — row density is a USER SETTING, applied product-wide, persisted locally.
//
// It lives on <html> as `data-density`, exactly like the theme, so a table never
// decides its own row height. That is deliberate: the two places a user can
// change it (the account menu and Settings) read and write the SAME value, so
// they cannot disagree, and a screen that forgot to consult a context still
// renders at the chosen height because the height arrives through the cascade.
//
// FAIL-SOFT, like the theme: an unreadable localStorage costs the preference,
// never the render.

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

export type Density = "compact" | "comfortable";

const KEY = "ne-density:v1";

/** The default is compact — §2.1's stated default row height (36px). */
export function readDensity(): Density {
  try {
    return localStorage.getItem(KEY) === "comfortable" ? "comfortable" : "compact";
  } catch {
    return "compact";
  }
}

/** Applied to <html>. `compact` REMOVES the attribute (it is the token default),
 * so the markup carries an attribute only when the user has departed from it. */
export function applyDensity(density: Density): void {
  const root = document.documentElement;
  if (density === "comfortable") root.setAttribute("data-density", "comfortable");
  else root.removeAttribute("data-density");
}

interface DensityState {
  density: Density;
  setDensity: (next: Density) => void;
}

const DensityContext = createContext<DensityState | null>(null);

export function DensityProvider({ children }: { children: ReactNode }) {
  const [density, setState] = useState<Density>(readDensity);

  useEffect(() => {
    applyDensity(density);
  }, [density]);

  const setDensity = useCallback((next: Density) => {
    setState(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Not persistable here; the in-session choice still applies.
    }
  }, []);

  const value = useMemo<DensityState>(() => ({ density, setDensity }), [density, setDensity]);
  return <DensityContext.Provider value={value}>{children}</DensityContext.Provider>;
}

/** Read/write the density preference. Outside a provider it reports the stored
 * choice and its setter is inert — a component rendered in isolation still
 * renders, it just cannot change a preference nothing is listening to. */
export function useDensity(): DensityState {
  const ctx = useContext(DensityContext);
  return ctx ?? { density: readDensity(), setDensity: () => {} };
}
