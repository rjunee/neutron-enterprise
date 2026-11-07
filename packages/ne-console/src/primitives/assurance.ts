// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Assurance helpers (NE-T2X-5, design §6.1/§6.2): the console-side reflection
// of `ne.api.wire._VOUCHED_ASSURANCE` — the floor values a human has vouched
// for (the structured backbone `"source"`, or an extracted element a steward
// has since approved, `"confirmed"`). Every OTHER label — including
// `"extracted"` and any unrecognized/misspelled string — is treated as
// PROVISIONAL: fail TOWARD flagging, never toward trust, exactly mirroring
// the server's own pinned stance (an unknown label is exactly the shape a
// tampered or stale payload would take).

/** Assurance labels a human has vouched for — never render a chip for these. */
const VOUCHED_ASSURANCE = new Set(["source", "confirmed"]);

/**
 * True iff `assurance` is NOT a vouched-for label — i.e. it should render the
 * "LLM-extracted — unconfirmed" chip (§6.1). Mirrors
 * `ne.api.wire._VOUCHED_ASSURANCE` bit-for-bit: an unknown/garbage label
 * (including the empty string) counts as provisional, never as trusted.
 */
export function isProvisionalAssurance(assurance: string): boolean {
  return !VOUCHED_ASSURANCE.has(assurance);
}

/** The minimal shape a graph hop needs for {@link provisionalEvidenceForHop}. */
export interface AssuranceHopLike {
  assurance: string;
  source_ref: string;
  subject: { assurance: string; source_ref: string };
  obj: { assurance: string; source_ref: string };
}

/** The ONE representative provisional label + its OWNING element's own
 * evidencing ref (§6.1: "the evidencing chunk one click away"). The `sourceRef`
 * always belongs to the SAME element that contributed `assurance` — never a
 * different element's provenance (NE-T2X-5-ENDPOINT-EVIDENCE-REF: the prior
 * defect paired an endpoint's provisional label with the EDGE's ref). */
export interface ProvisionalEvidence {
  assurance: string;
  sourceRef: string;
}

/**
 * The ONE representative provisional evidence for a hop (§6.1: "a chip on any
 * provisional hop/ref"), or `null` when the asserting relation and BOTH its
 * endpoints are vouched-for. A hop can carry up to three independent
 * assurance labels (the asserting edge itself, plus its subject/object) —
 * this collapses them to exactly ONE chip per row, preferring the edge's own
 * assurance (the asserting record) over its endpoints. The returned
 * `sourceRef` is ALWAYS the ref of the element that contributed `assurance`
 * — the edge's own ref when the edge is provisional, the subject's own ref
 * when only the subject is, and so on — never a mismatched element's ref.
 */
export function provisionalEvidenceForHop(hop: AssuranceHopLike): ProvisionalEvidence | null {
  if (isProvisionalAssurance(hop.assurance)) {
    return { assurance: hop.assurance, sourceRef: hop.source_ref };
  }
  if (isProvisionalAssurance(hop.subject.assurance)) {
    return { assurance: hop.subject.assurance, sourceRef: hop.subject.source_ref };
  }
  if (isProvisionalAssurance(hop.obj.assurance)) {
    return { assurance: hop.obj.assurance, sourceRef: hop.obj.source_ref };
  }
  return null;
}

/** A parsed LLM-extracted `source_ref`: the steward-proposal sha16 plus the
 * underlying evidence ref. */
export interface ExtractedSourceRef {
  sha16: string;
  evidenceRef: string;
}

const EXTRACTED_SOURCE_REF_RE = /^llm:([0-9a-f]{16}):(.+)$/;

/**
 * Parse the writer-stamped `llm:<sha16>:<ref>` `source_ref` grammar (design
 * §3.2/§6.1) into its proposal sha16 (a derivation of the NE-066 steward
 * proposal id, used to deep-link `/flywheel?tab=queue&proposal=<sha16>`) and
 * the underlying evidence ref. Returns `null` on any near-miss (wrong prefix,
 * a sha16 that is not exactly 16 lowercase hex characters, or an empty
 * trailing ref) — a chip with no parseable ref renders its evidencing text
 * WITHOUT a confirm link, rather than guessing at a malformed shape.
 */
export function parseExtractedSourceRef(ref: string): ExtractedSourceRef | null {
  const match = EXTRACTED_SOURCE_REF_RE.exec(ref);
  if (match === null) {
    return null;
  }
  const sha16 = match[1];
  const evidenceRef = match[2];
  if (!sha16 || !evidenceRef) {
    return null;
  }
  return { sha16, evidenceRef };
}
