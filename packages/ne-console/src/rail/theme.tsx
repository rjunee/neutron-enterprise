// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The theme preference. §1.2 removes the toggle from the rail; §1.3 puts it in
// the account menu, and §16 puts the same control on Settings. Both read and
// write THIS state, so the two surfaces cannot disagree about what the theme is.
//
// THREE choices, not two. A plain light/dark switch cannot express "follow my
// machine", and on a laptop that flips at sunset that is the state most people
// actually want. `system` is the default and writes NO attribute, which is
// exactly the shape tokens.css's `:root:not([data-theme="dark"])` media block
// expects — so the default path is byte-identical to having no control at all.
//
// FAIL-SOFT: an unreadable localStorage costs the preference, never the render.

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

export type ThemeChoice = "system" | "light" | "dark";

const KEY = "ne-theme:v1";

function isChoice(value: string | null): value is ThemeChoice {
  return value === "system" || value === "light" || value === "dark";
}

export function readTheme(): ThemeChoice {
  try {
    const value = localStorage.getItem(KEY);
    return isChoice(value) ? value : "system";
  } catch {
    return "system";
  }
}

/** Applied to <html>: `system` REMOVES the attribute rather than writing one. */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

interface ThemeState {
  theme: ThemeChoice;
  setTheme: (next: ThemeChoice) => void;
}

const ThemeContext = createContext<ThemeState | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setState] = useState<ThemeChoice>(readTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const setTheme = useCallback((next: ThemeChoice) => {
    setState(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Not persistable here; the in-session choice still applies.
    }
  }, []);

  const value = useMemo<ThemeState>(() => ({ theme, setTheme }), [theme, setTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

/** Read/write the theme. Outside a provider it reports the stored choice and its
 * setter is inert, so a component rendered in isolation still renders. */
export function useTheme(): ThemeState {
  const ctx = useContext(ThemeContext);
  return ctx ?? { theme: readTheme(), setTheme: () => {} };
}
