// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// AssuranceChip (NE-T2X-5, design §6.1/§6.2): the inline "LLM-extracted —
// unconfirmed" flag on a provisional graph fact. Renders NOTHING for a
// vouched-for assurance (`"source"`/`"confirmed"`) — per §6.2 the confirmed
// flag GOES AWAY entirely for every future reader; there is no third state,
// no expiry-derived "probably fine" badge, and no age/read-count auto-
// promotion anywhere in this component. Confirm/decline ARE the NE-066
// steward queue's existing approve/reject — this chip only deep-links there;
// it adds no new mutation surface.

import { Link } from "react-router";
import { isProvisionalAssurance, parseExtractedSourceRef } from "./assurance";
import "./assuranceChip.css";

export function AssuranceChip({
  assurance,
  sourceRef,
}: {
  assurance: string;
  /** The evidencing `source_ref` (the `llm:<sha16>:<ref>` grammar when this
   * assurance came from the extraction writer); shown verbatim, and parsed
   * to derive the confirm deep-link when it matches the grammar. */
  sourceRef: string;
}) {
  if (!isProvisionalAssurance(assurance)) {
    return null;
  }
  const parsed = parseExtractedSourceRef(sourceRef);
  return (
    // No `role="status"` here (Argus P3 round-1): this is a static inline label,
    // not a live-region announcement. A results page can carry many of these
    // chips at once; a per-chip live region would flood screen-reader users on
    // every render. The single result-level banner (SearchPage.tsx) keeps the
    // live region — one announcement per page, not N+1.
    <span className="ne-assurance-chip">
      <span className="ne-assurance-chip__label">LLM-extracted — unconfirmed</span>
      <span className="ne-assurance-chip__ref">{sourceRef}</span>
      {parsed && (
        <Link
          className="ne-assurance-chip__confirm"
          to={`/flywheel?tab=queue&proposal=${parsed.sha16}`}
        >
          Confirm →
        </Link>
      )}
    </span>
  );
}
