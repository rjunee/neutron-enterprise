// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-CONSOLE-ISSUER: the bootstrap gate. Before rendering the authed app it
// fetches `/config.json` from the serving ne-api deployment and initializes the
// OIDC client from the RUNTIME config, not a Vite-baked constant. It fails CLOSED:
// if the fetch fails (or the payload is invalid) it renders a visible error with a
// Retry button and NEVER mounts the AuthProvider/App — there is no fallback
// issuer, so a config failure can never become a silent wrong-issuer login.

import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { fetchRuntimeConfig, type ConsoleRuntimeConfig } from "./api/runtimeConfig";
import { AuthProvider } from "./auth/AuthProvider";

/** The resolved runtime config, available to any descendant once bootstrapped. */
export const RuntimeConfigContext = createContext<ConsoleRuntimeConfig | null>(null);

/** Read the bootstrapped runtime config. Throws if used outside a successful
 * AppBootstrap (there is no default config — fail-closed). */
export function useRuntimeConfig(): ConsoleRuntimeConfig {
  const cfg = useContext(RuntimeConfigContext);
  if (cfg === null) {
    throw new Error("useRuntimeConfig used outside a bootstrapped AppBootstrap");
  }
  return cfg;
}

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; config: ConsoleRuntimeConfig };

export function AppBootstrap({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>({ status: "loading" });
  // A monotonically increasing token forces the effect to re-run on Retry.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // StrictMode double-invokes this effect in dev; the GET is idempotent, and
    // the cancelled flag prevents setting state after unmount (no act() warning).
    // The loading state is set by the initial state + the Retry handler, never
    // synchronously here (that would cascade renders).
    let cancelled = false;
    fetchRuntimeConfig()
      .then((config) => {
        if (!cancelled) setState({ status: "ready", config });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  function retry(): void {
    setState({ status: "loading" });
    setAttempt((n) => n + 1);
  }

  if (state.status === "loading") {
    return (
      <div className="ne-spinner" role="status">
        {/* The breathe is the SYSTEM'S one loading motion, borrowed rather than
            re-invented (styles/shell.css). aria-hidden because the word beside
            it is what a reader hears; a bar has nothing to announce. */}
        <span className="ne-skeleton ne-spinner__bar" aria-hidden="true" />
        Loading configuration…
      </div>
    );
  }
  if (state.status === "error") {
    return (
      <div role="alert" className="ne-config-error">
        <p>Console configuration could not be loaded — sign-in is unavailable.</p>
        <p>{state.message}</p>
        <button type="button" className="ne-btn ne-btn--primary" onClick={retry}>
          Retry
        </button>
      </div>
    );
  }
  return (
    <AuthProvider config={state.config}>
      <RuntimeConfigContext.Provider value={state.config}>{children}</RuntimeConfigContext.Provider>
    </AuthProvider>
  );
}
