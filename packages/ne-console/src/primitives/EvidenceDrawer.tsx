// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// REDESIGN §1.4 — <EvidenceDrawer>. A native <details>: keyboard-reachable with
// no custom focus management, no JS trap, no aria plumbing to get wrong. The
// summary STATES ITS CONTENTS AND A COUNT ("Show evidence — 6 CI tests"), because
// a count at rest is what tells a reviewer the depth is there without making him
// open every drawer to find out. Force-opened by INSPECT.

import type { ReactNode } from "react";
import { useInspect } from "./InspectContext";

export function EvidenceDrawer({
  summary,
  count,
  children,
  tone = "normal",
}: {
  /** What is inside — "event hash, prev hash, payload", "10 prior configs". */
  summary: string;
  /** Optional explicit count rendered after the summary. */
  count?: number | string;
  children: ReactNode;
  tone?: "normal" | "quiet";
}) {
  const { inspect } = useInspect();
  return (
    <details className={`ne-drawer ne-drawer--${tone}`} open={inspect ? true : undefined}>
      <summary className="ne-drawer__summary">
        <span className="ne-drawer__caret" aria-hidden="true">
          ▸
        </span>
        <span>
          Show evidence — {summary}
          {count !== undefined ? ` (${count})` : ""}
        </span>
      </summary>
      <div className="ne-drawer__body">{children}</div>
    </details>
  );
}
