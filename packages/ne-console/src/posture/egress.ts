// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// ONE DERIVATION FOR THE EGRESS STATE WORD (round-2 blocker).
//
// Two screens rendered the same field, from the same source, with opposite state
// words on the same deployment:
//   /        EXTERNAL HOSTS SANCTIONED · 0 · ◑ OPEN  · GET /v1/posture · egress_allowlist
//   /posture EGRESS                    · 0 · ● SEALED · No external host is sanctioned.
// Under the four-part readout rule the STATE word is the part a reader acts on,
// so one of those two screens was telling a security reviewer the boundary was
// open while C2 SEALED EGRESS sat attested-green in the rail beside it.
//
// The landing tile had the mapping inverted: it read `air_gap` alone, so a
// deployment with the air gap off and an EMPTY allowlist — nothing sanctioned,
// the most sealed state there is — rendered OPEN.
//
// THE RULE, IN ONE PLACE. Nothing may reach out if nothing is sanctioned, and
// nothing may reach out if the air gap is engaged. Everything else is OPEN, with
// the sanctioned hosts enumerable. Every readout of this field imports this.

import type { StateWord } from "../primitives/StatePill";

export interface EgressFacts {
  air_gap: boolean;
  egress_allowlist: string[];
}

/** The state word for this deployment's egress boundary. */
export function egressState(posture: EgressFacts | null): StateWord | undefined {
  if (posture === null) return undefined;
  return posture.air_gap || posture.egress_allowlist.length === 0 ? "SEALED" : "OPEN";
}

/** The sentence that explains the word, so the two never drift apart either. */
export function egressReason(posture: EgressFacts | null): string | null {
  if (posture === null) return null;
  if (posture.air_gap) return "Air gap engaged — frontier egress is disabled.";
  if (posture.egress_allowlist.length === 0) return "No external host is sanctioned.";
  return `${posture.egress_allowlist.length} sanctioned host${
    posture.egress_allowlist.length === 1 ? "" : "s"
  }; every other destination is refused at the boundary.`;
}
