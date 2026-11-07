// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 — the COMPARE control (design §4.6, top-right of the chamber). Clicking
// it runs the REAL second-persona OIDC popup (acquireCompareKey, at the src/api
// seam) against the RUNTIME-config issuer; on success it hands the pane a live
// CompareKey. It is disabled — with an honest title — until the chamber is ready
// (a graph is anchored) and the runtime config is available. A popup failure
// (blocked/closed/IdP error) renders an inline honest error and opens NO pane —
// never a fabricated compare (§0 law 5).

import { useContext, useState } from "react";
import { acquireCompareKey, type CompareKey } from "../../api/compareAuth";
import { RuntimeConfigContext } from "../../AppBootstrap";

export interface CompareControlProps {
  /** True once the chamber has a live subgraph (status === "ready"). */
  hasGraph: boolean;
  /** Called with the acquired key so KnowledgePage can open the pane. */
  onKeyAcquired: (key: CompareKey) => void;
}

const NO_GRAPH_TITLE =
  "Anchor a citation or entity first — compare runs the identical governed query under a second key.";
const NO_CONFIG_TITLE = "runtime configuration unavailable";

export function CompareControl({ hasGraph, onKeyAcquired }: CompareControlProps) {
  // NULLABLE read (never useRuntimeConfig — it throws outside a bootstrap).
  const config = useContext(RuntimeConfigContext);
  const [acquiring, setAcquiring] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const disabled = !hasGraph || config === null || acquiring;
  const title = config === null ? NO_CONFIG_TITLE : !hasGraph ? NO_GRAPH_TITLE : undefined;

  async function onClick() {
    if (config === null) return;
    setError(null);
    setAcquiring(true);
    try {
      const key = await acquireCompareKey(config);
      onKeyAcquired(key);
    } catch (err) {
      // Popup blocked/closed or an IdP error — render it honestly, open nothing.
      setError(err instanceof Error ? err.message : "second-key sign-in did not complete");
    } finally {
      setAcquiring(false);
    }
  }

  return (
    <div className="ne-compare-control">
      <button
        type="button"
        className="ne-compare-control__button ne-label"
        aria-label="Dual-key compare"
        title={title}
        disabled={disabled}
        onClick={() => {
          void onClick();
        }}
      >
        {acquiring ? "OPENING KEY…" : "COMPARE"}
      </button>
      {/* THE REASON IS BESIDE THE CONTROL, NOT ONLY IN ITS TOOLTIP (round-3
          minor). A disabled COMPARE sat above the empty state with no indication
          of what it would compare or what would enable it — a tooltip is neither
          visible at rest nor reachable by keyboard, and this console states the
          reason inline everywhere else. This was the outlier. */}
      {disabled && !acquiring && title ? (
        <p className="ne-compare-control__reason">{title}</p>
      ) : null}
      {error && (
        <p className="ne-compare-control__error ne-mono" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
