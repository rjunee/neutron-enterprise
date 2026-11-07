// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ANSWER'S CITATIONS, FOR THE CANONICAL ASK TURN.
//
// WHY THIS FILE EXISTS. Ask's citation UI — the chips, the source list, the
// source pane — has always worked; it just never had anything to draw. Its only
// writer was the SSE `citations` frame, and canonical Ask's stream carries one
// `accepted` frame and closes (ne-api routers/chat.py). So a governed answer
// that rested on four retrieved passages rendered with no sources at all, which
// on a governance product reads as "this answer cites nothing" — the opposite
// of what actually happened.
//
// The Run knows. Its evidence envelope
// (`GET /v1/agents/{agent}/runs/{run}/evidence`) states which evidence records
// the answer rests on, which claim each one is bound to, and where each record
// came from. This module turns that into the same `Citation` shape the chips
// already take, and nothing else.
//
// ---------------------------------------------------------------------------
// THE TWO RULES THIS MODULE ENFORCES, both of which are governance rules and
// not display preferences:
//
// 1. A CITATION ONLY WHERE THE SERVER ASSOCIATED THE CLAIM WITH THE EVIDENCE.
//    The envelope's claims carry `evidence_id`, and the server sets it only
//    from a persisted, validated association row (evidence_projection
//    `_valid_association`: run, owner and claim ids all match and the
//    association's locator hash equals the evidence's). An evidence record no
//    claim is bound to is NOT cited here, however available it looks —
//    proximity, ordering and "it was retrieved during this run" are all
//    inferences, and none of them is an association.
//
// 2. NO CHIP WHOSE TARGET WILL NOT RESOLVE. A `Citation` is a link, and a link
//    that 404s is worse than an absent one on a screen whose whole claim is
//    that you can check the work. The evidence wire carries `locator.chunk_id`
//    but NOT `document_id` or `source_ref`, which the pane and the source list
//    both need — so this module never invents them. It asks the server:
//    `retrieve(chunkIds)` returns the server's OWN `Citation` objects, fully
//    formed, for exactly the chunks the reader is still authorized to read. A
//    chunk the retrieval declines to return gets no chip, and `citationsNote`
//    says how many were dropped rather than quietly shrinking the list.
//
// WHAT IS DELIBERATELY NOT DONE HERE. No inline anchoring: the wire carries no
// span offsets for a claim inside the output (they exist server-side on
// `PersistedClaim` and are not projected), so chips stay end-of-prose exactly
// as CitationChip already documents. No count of "sources consulted": the
// envelope reports evidence PRODUCED, not everything read.

import type { AgentEvidenceEnvelopeWire, Citation } from "../api/types";
import { verifiedRowAnchors, type VerifiedRowAnchor } from "../agents/rowAnchors";

/** The two governed reads this module's rules are applied to — the seam both
 * the live thread and the read-back thread call, so "which sources may this
 * answer show" has exactly one implementation. */
export interface RunCitationReader {
  getAgentRunEvidence?(agentId: string, runId: string): Promise<AgentEvidenceEnvelopeWire>;
  retrieve(chunkIds: string[]): Promise<{ citations: Citation[] }>;
}

/** What one Run's answer rests on, as the thread renders it. */
export interface RunCitationsResult {
  citations: Citation[];
  note: string | null;
  /**
   * THE SERVED ROW ANCHORS — the canonical hash of each verified row the
   * answer's claims are associated with (agents/rowAnchors.ts owns the rule).
   * A verified row has no retrievable passage, so it can never become a chip;
   * its anchor IS its evidence, and the thread draws it beside the sources
   * note instead of leaving "server-verified record" as an unpointable claim.
   * Empty means the envelope carried none — never an absence invented here.
   */
  rowAnchors: VerifiedRowAnchor[];
}

/**
 * The answer's citations and its sources note, read off the Run.
 *
 * `null` when this deployment's client has no evidence read — then nothing is
 * stated, because nothing was asked. Both rules at the top of this file are
 * enforced by construction: the envelope decides WHICH chunks may be cited,
 * the authorized retrieval decides WHETHER this reader may still open them.
 */
export async function readRunCitations(
  client: RunCitationReader,
  agentId: string,
  runId: string,
): Promise<RunCitationsResult | null> {
  const readEvidence = client.getAgentRunEvidence;
  if (readEvidence === undefined) return null;
  const envelope = await readEvidence.call(client, agentId, runId);
  const chunks = citedChunkIds(envelope);
  const retrieved = chunks.length === 0 ? [] : (await client.retrieve(chunks)).citations;
  const citations = runCitations(envelope, retrieved);
  return {
    citations,
    note: citationsNote(envelope, citations),
    rowAnchors: verifiedRowAnchors(envelope),
  };
}

/**
 * The chunks this Run's answer actually rests on, in the envelope's own order,
 * deduplicated.
 *
 * An evidence record qualifies only when ALL of these hold:
 *   * some claim names it in `evidence_id` — the validated association (rule 1)
 *   * `state === "available"` — the server is disclosing this record now; the
 *     other two states are its way of saying access changed or the locator did
 *     not resolve, and neither is a source a reader can open
 *   * its locator names a `chunk_id` (the generic locator arm) or `chunk_ids`
 *     (the `knowledge_search` arm — a search read rests on SEVERAL passages,
 *     all server-persisted in the locator, so all of them are the citation;
 *     picking one would be a selection the server never made). A `verified_t3`
 *     row anchor and a graph-only locator are real evidence with no retrievable
 *     passage behind them, and they are reported by `citationsNote`, never
 *     turned into a chip that goes nowhere.
 */
export function citedChunkIds(envelope: AgentEvidenceEnvelopeWire): string[] {
  const associated = new Set(
    envelope.claims
      .map((claim) => claim.evidence_id)
      .filter((id): id is string => id !== null && id !== ""),
  );
  const ids: string[] = [];
  const push = (chunk: unknown) => {
    if (typeof chunk !== "string" || chunk === "") return;
    if (!ids.includes(chunk)) ids.push(chunk);
  };
  for (const record of envelope.evidence) {
    if (!associated.has(record.evidence_id)) continue;
    if (record.state !== "available") continue;
    push(record.locator?.chunk_id);
    for (const chunk of record.locator?.chunk_ids ?? []) push(chunk);
  }
  return ids;
}

/**
 * The citations to draw: the server's own `Citation` objects for exactly the
 * cited chunks, in the envelope's evidence order.
 *
 * `retrieved` is whatever `client.retrieve(citedChunkIds(envelope))` returned.
 * The intersection is taken deliberately in this direction — the envelope
 * decides WHICH chunks may be cited, and the retrieval decides WHETHER this
 * reader may still open them. A chunk in one and not the other is dropped.
 */
export function runCitations(
  envelope: AgentEvidenceEnvelopeWire,
  retrieved: readonly Citation[],
): Citation[] {
  const byChunk = new Map(retrieved.map((citation) => [citation.chunk_id, citation]));
  const out: Citation[] = [];
  for (const chunk of citedChunkIds(envelope)) {
    const citation = byChunk.get(chunk);
    if (citation !== undefined) out.push(citation);
  }
  return out;
}

/** The wording of the absent/partial note. Kept beside the rule that produces
 * it so the sentence and the condition cannot drift. */
export const CITATIONS_NONE_ASSOCIATED =
  "No claim in this answer was associated with an evidence record, so it cites no source.";
export const CITATIONS_NO_PASSAGE =
  "This answer's evidence has no retrievable passage behind it, so there is nothing to open.";

/**
 * EVERY associated record is a server-verified row (`verified_t3` /
 * `verified_t3_empty` locators — receipt-anchored governed rows). "There is
 * nothing to open" alone read as "this answer rests on nothing" on the one
 * platform whose whole claim is the opposite, so the count and the kind are
 * stated. Only the fully-verified case earns this sentence; a mix with
 * locators this module cannot vouch for keeps the generic one.
 */
export function citationsVerifiedRowsNote(rows: number): string {
  return rows === 1
    ? "This answer rests on 1 server-verified evidence record — a governed row bound to its claim by receipt. It has no document passage to open here."
    : `This answer rests on ${rows} server-verified evidence records — governed rows bound to its claims by receipt. They have no document passage to open here.`;
}
/**
 * THE WITHHOLD, SAID AS ITSELF.
 *
 * `state: "withheld"` on an evidence record is the server reporting that the
 * source-bound materializer declined to re-authorize it for THIS reader now
 * ("Evidence withheld — access changed"). That is a governance outcome and a
 * completely different fact from "there was no passage", which is what this
 * note used to collapse it into. Saying the wrong one here would tell a reader
 * their answer rested on nothing when it rested on something they may no longer
 * open — and would hide a real access regression behind a shrug.
 */
export const CITATIONS_WITHHELD =
  "This answer's sources exist but were withheld — your access to them has changed since the answer was produced.";

/**
 * WHY THERE ARE FEWER CHIPS THAN EVIDENCE RECORDS — or `null` when there is
 * nothing to explain.
 *
 * The band's absent-note discipline: an absence is STATED, never left to look
 * like nothing happened. Three cases, each a different fact:
 *
 *   * no claim was associated with any evidence at all — the answer genuinely
 *     rests on nothing the server bound to it
 *   * evidence exists and is associated, but the server WITHHELD it from this
 *     reader — an access decision, and the strongest of these facts
 *   * evidence exists and is associated, but none of it has a retrievable
 *     passage (graph-only or `verified_t3` locators)
 *   * some cited chunks did not come back from the authorized retrieval — the
 *     reader's access to them changed, and the count says so rather than the
 *     list silently shrinking
 *
 * A refusal is NOT one of these cases and gets `null`: a refusal cites nothing
 * because it answered nothing, which the refusal card already says. Adding a
 * second sentence about missing sources would read as a defect in an outcome
 * that is correct.
 */
export function citationsNote(
  envelope: AgentEvidenceEnvelopeWire,
  shown: readonly Citation[],
): string | null {
  if (envelope.output.verdict === "refused") return null;
  const cited = citedChunkIds(envelope);
  if (cited.length === 0) {
    const associated = new Set(
      envelope.claims
        .map((claim) => claim.evidence_id)
        .filter((id): id is string => id !== null && id !== ""),
    );
    if (associated.size === 0) return CITATIONS_NONE_ASSOCIATED;
    // THE WITHHOLD WINS OVER "no passage", because it is the stronger and more
    // actionable statement: a record the server declined to re-authorize is a
    // source that EXISTS, and a reader told "there is nothing to open" would
    // have no idea an access decision had been made about their answer.
    const withheld = envelope.evidence.some(
      (record) => associated.has(record.evidence_id) && record.state === "withheld",
    );
    if (withheld) return CITATIONS_WITHHELD;
    // WHEN EVERY ASSOCIATED RECORD IS A SERVER-VERIFIED ROW, say that. A
    // verified_t3 locator is the server's own receipt-anchored row — evidence
    // of the strongest kind this platform serves — and "there is nothing to
    // open" alone read as "this answer rests on nothing". The count and the
    // kind are both the envelope's; nothing here upgrades a locator it cannot
    // see into a "verified" claim, which is why the mixed case keeps the
    // generic sentence.
    const associatedAvailable = envelope.evidence.filter(
      (record) => associated.has(record.evidence_id) && record.state === "available",
    );
    const verifiedRows = associatedAvailable.filter(
      (record) =>
        record.locator?.kind === "verified_t3" || record.locator?.kind === "verified_t3_empty",
    ).length;
    if (verifiedRows > 0 && verifiedRows === associatedAvailable.length) {
      return citationsVerifiedRowsNote(verifiedRows);
    }
    return CITATIONS_NO_PASSAGE;
  }
  const missing = cited.length - shown.length;
  if (missing <= 0) return null;
  return `${missing} of ${cited.length} cited ${
    cited.length === 1 ? "source is" : "sources are"
  } no longer readable with your current access.`;
}
