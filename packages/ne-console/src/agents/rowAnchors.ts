// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ROW ANCHOR — the canonical hash of a verified template row, AS SERVED.
//
// A `verified_t3` evidence locator carries `row_anchor`: the server's own
// canonical hash of the governed row the answer rests on. It is the row's
// IDENTITY — byte-identical served rows carry an identical anchor, so two Runs
// that answered from the same verified row can be laid side by side and the
// sameness is visible in the hash itself, with nothing recomputed and nothing
// compared by this client (C7: the console displays a hash; it never evaluates
// one). Until this module existed the wire carried the anchor and no surface
// drew it, which made the strongest fact this platform serves — "identical
// every run" — unshowable.
//
// THE EXTRACTION RULE, one implementation for every surface that draws anchors:
//
//   * only a record some claim names in `evidence_id` — the server's own
//     validated association, the same rule runCitations enforces for chunks.
//     An anchor on evidence nothing was bound to is real but is not part of
//     what THIS answer rests on;
//   * only `state === "available"` — a withheld record's identity is part of
//     what was withheld;
//   * only a locator that actually carries a non-empty `row_anchor`. The
//     `verified_t3_empty` arm has no row and therefore no anchor; nothing here
//     invents one, and no anchor is ever derived client-side from row content.
//
// Envelope order, one entry per evidence record. NO value-level dedupe: if the
// server served two records with one anchor, that is two served facts.

import type { AgentEvidenceEnvelopeWire } from "../api/types";

/** One served row anchor, keyed by the evidence record that carried it. */
export interface VerifiedRowAnchor {
  /** The evidence record's own id — the stable render key. */
  readonly evidenceId: string;
  /** The served canonical row hash, verbatim. */
  readonly anchor: string;
}

/**
 * Every served row anchor the answer rests on — claim-associated, available,
 * anchor-bearing evidence records, in the envelope's own order.
 */
export function verifiedRowAnchors(envelope: AgentEvidenceEnvelopeWire): VerifiedRowAnchor[] {
  const associated = new Set(
    envelope.claims
      .map((claim) => claim.evidence_id)
      .filter((id): id is string => id !== null && id !== ""),
  );
  const anchors: VerifiedRowAnchor[] = [];
  for (const record of envelope.evidence) {
    if (!associated.has(record.evidence_id)) continue;
    if (record.state !== "available") continue;
    const anchor = record.locator?.row_anchor;
    if (typeof anchor !== "string" || anchor === "") continue;
    anchors.push({ evidenceId: record.evidence_id, anchor });
  }
  return anchors;
}
