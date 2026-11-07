// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §6 — the rules behind the Search results table, as pure functions.
//
// They live apart from the component because the rules are the part worth
// testing: the join from a chunk to its citation, the union of two identities'
// result sets, the source-system facet, and the count line. None of them needs a
// DOM, a provider or a fixture to be proven.
//
// THIN CLIENT (C7). Everything here reads fields the server already sent and
// reshapes them for a table. Nothing ranks, scores, re-scores, or decides what
// an identity may see: the score is the server's, the order is the server's, and
// a row absent from an identity's result set is absent because the server did
// not send it.
//
// NO FABRICATION. A chunk with no matching citation has NO source system — the
// column renders the em dash, and this module returns `null` rather than
// guessing one from the document id.

import type {
  AuthorizedRetrievalEnvelopeWire,
  Citation,
  RetrievalGraphPathWire,
  ScoredChunk,
} from "../api/types";
import { isProvisionalAssurance } from "../primitives/assurance";
import { formatCount, formatDuration } from "../primitives/format";
import { excerpt as excerptOf, parseResultRecord, resultIdentity } from "./resultRecord";
import type { ResultIdentity } from "./resultRecord";

/** One row of the results table. Every field traces to a served field. */
export interface ResultRow {
  /** `chunks[].chunk.chunk_id` — the row's identity and the CSV's chunk_id. */
  chunkId: string;
  /** `chunks[].chunk.document_id`. */
  documentId: string;
  /** 1..n, the position the server ranked this chunk at. Never re-derived from
   * a sort: sorting by Source reorders the table, and the rank a row carries is
   * still the rank the server gave it. */
  rank: number;
  /** `chunks[].score` for THIS identity — `null` when this identity's result set
   * did not contain the row (the compare view's "Not returned"). */
  score: number | null;
  /** The comparison identity's score; `undefined` when not comparing, `null`
   * when that identity did not receive the row. */
  otherScore?: number | null;
  /** `source_system` off the matching `citations[]` entry; `null` unmatched. */
  sourceSystem: string | null;
  /** The matching citation's `source_ref`; `null` when unmatched. */
  sourceRef: string | null;
  /** The matching citation's `source_url` — server-side enrichment, absent when
   * the API cannot resolve a deep link. NEVER constructed here. */
  sourceUrl: string | null;
  /** The human-readable headline for this row's document, resolved from served
   * fields only (resultRecord.resultIdentity) — never fabricated, hash last. */
  identity: ResultIdentity;
  /** `chunks[].chunk.text`, unwrapped when the chunk carried a JSON envelope. */
  text: string;
  /** The two-line excerpt the table clamps. */
  excerpt: string;
  /** `chunks[].chunk.metadata` — an opaque map, rendered as it arrived. */
  metadata: Record<string, unknown>;
}

/**
 * The served result set as table rows, in the server's own order.
 *
 * The citation join is by `chunk_id` and is allowed to miss: the response's
 * `citations[]` need not cover every chunk, and a miss is an absent source
 * system, not an occasion to invent one.
 */
export function buildRows(
  chunks: readonly ScoredChunk[],
  citations: readonly Citation[],
): ResultRow[] {
  const byChunk = new Map(citations.map((c) => [c.chunk_id, c]));
  return chunks.map((scored, index) => {
    const citation = byChunk.get(scored.chunk.chunk_id);
    const record = parseResultRecord(scored.chunk.text);
    return {
      chunkId: scored.chunk.chunk_id,
      documentId: scored.chunk.document_id,
      rank: index + 1,
      score: scored.score,
      sourceSystem: citation?.source_system ?? null,
      sourceRef: citation?.source_ref ?? null,
      sourceUrl: citation?.source_url ?? null,
      identity: resultIdentity(record, scored.chunk.metadata, scored.chunk.document_id),
      text: record.body,
      excerpt: excerptOf(record),
      metadata: scored.chunk.metadata,
    };
  });
}

/**
 * §6.5 — the compare view is ONE table over the UNION of both result sets.
 *
 * Your rows first, in the order your server response ranked them, then the rows
 * only the other identity received, in theirs. `score` is what YOU were sent and
 * `otherScore` is what THEY were sent; either may be `null`, and `null` means
 * "that identity's result set did not contain this row" — it is never a zero.
 *
 * The content columns (source, document, excerpt, text) come from whichever
 * identity actually received the row; for a row both received they are the same
 * served record. Rank stays the position from the response that supplied the
 * row. Duplicate ranks are honest; a fresh 1..n sequence would be client-made.
 */
export function unionRows(mine: readonly ResultRow[], theirs: readonly ResultRow[]): ResultRow[] {
  const theirsByChunk = new Map(theirs.map((row) => [row.chunkId, row]));
  const mineKeys = new Set(mine.map((row) => row.chunkId));
  const onlyTheirs = theirs.filter((row) => !mineKeys.has(row.chunkId));
  return [
    ...mine.map((row) => ({ ...row, otherScore: theirsByChunk.get(row.chunkId)?.score ?? null })),
    ...onlyTheirs.map((row) => ({ ...row, score: null, otherScore: row.score })),
  ];
}

/** Undo the union: your own result set, unchanged, with the other identity's
 * column dropped. Your rows lead the union in your served order, so their ranks
 * are already 1..n and nothing is renumbered. */
export function withoutComparison(rows: readonly ResultRow[]): ResultRow[] {
  return rows
    .filter((row) => row.score !== null)
    .map(({ otherScore: _dropped, ...row }) => ({ ...row }));
}

/** §6.2 — the source-system facet, derived from the result set's citations,
 * A–Z. A result set with no citations has no chips, not an empty chip. */
export function sourceSystems(rows: readonly ResultRow[]): string[] {
  const seen = new Set<string>();
  for (const row of rows) if (row.sourceSystem !== null) seen.add(row.sourceSystem);
  return [...seen].sort((a, b) => a.localeCompare(b, "en"));
}

/** No selection filters nothing. A selection keeps only rows whose citation
 * names one of the chosen systems — a row with no citation has no system and is
 * not silently counted as one. */
export function applySourceFilter(
  rows: readonly ResultRow[],
  selected: readonly string[],
): readonly ResultRow[] {
  if (selected.length === 0) return rows;
  return rows.filter((row) => row.sourceSystem !== null && selected.includes(row.sourceSystem));
}

/**
 * §6.2's count: "{n} results · {t} s", extended by "· {n} unconfirmed" when the
 * response reports `provisional_source_count > 0`.
 *
 * `n` is the rendered window, so the number always describes the rows beside it.
 * `t` is the measured wall-clock of the run through §2.7's duration format.
 */
export function searchCountText(
  shown: number,
  elapsedMs: number,
  provisional: number,
  comparison?: { identity: string; provisional: number },
): string {
  const head = `${formatCount(shown)} results · ${formatDuration(elapsedMs)}`;
  if (comparison !== undefined) {
    const facts: string[] = [];
    if (provisional > 0) facts.push(`You: ${formatCount(provisional)} unconfirmed`);
    if (comparison.provisional > 0) {
      facts.push(`${comparison.identity}: ${formatCount(comparison.provisional)} unconfirmed`);
    }
    return facts.length === 0 ? head : `${head} · ${facts.join(" · ")}`;
  }
  return provisional > 0 ? `${head} · ${formatCount(provisional)} unconfirmed` : head;
}

/** A document id keeps its head and its tail: the tail is where a version, a
 * page or a chunk ordinal lives, and a right-truncated id loses exactly the part
 * that distinguishes two rows. The full value goes in `title`. */
export function truncateMiddle(value: string, max = 28): string {
  if (value.length <= max) return value;
  const head = Math.ceil((max - 1) / 2);
  const tail = max - 1 - head;
  return `${value.slice(0, head)}…${value.slice(value.length - tail)}`;
}

// ===========================================================================
// CR-B7 — term marks. A fact about the text, never a claim about the ranking:
// the score column stays the server's only relevance claim (§6.3 doctrine
// above). This marks WHAT MATCHED, not HOW WELL.
// ===========================================================================

/** One span of rendered text. `marked` spans are verbatim occurrences of the
 * query's own words; unmarked spans are everything between them. */
export interface TextSegment {
  text: string;
  marked: boolean;
}

function isWordChar(ch: string | undefined): boolean {
  return ch !== undefined && /[\p{L}\p{N}]/u.test(ch);
}

/**
 * Every contiguous run of the query's own words, in the query's own order,
 * longest first. "termination notice period" yields "termination notice
 * period", "notice period", "termination notice", then the three single
 * words — so two query words that sit adjacent in the text mark as ONE span
 * ("notice period"), while a lone occurrence elsewhere ("written notice.")
 * still marks on its own. Never a synonym, a stem, or a fuzzy match.
 */
function queryPhrases(query: string): string[] {
  const tokens = query
    .trim()
    .split(/\s+/)
    .filter((t) => t.length > 0);
  const phrases = new Set<string>();
  for (let start = 0; start < tokens.length; start++) {
    for (let end = tokens.length; end > start; end--) {
      phrases.add(tokens.slice(start, end).join(" "));
    }
  }
  return [...phrases].sort((a, b) => {
    const byWordCount = b.split(" ").length - a.split(" ").length;
    return byWordCount !== 0 ? byWordCount : b.length - a.length;
  });
}

/**
 * Splits `text` into segments, marking every verbatim, WHOLE-WORD occurrence
 * of one of the query's own terms — case-insensitive (so "Termination" and
 * "termination" both mark), but never a match embedded in a larger word (the
 * "notice" inside "noticeable" is not a verbatim occurrence of "notice").
 *
 * An empty query marks nothing — the one unmarked segment is the text
 * unchanged, never an empty result that would render as a blank row.
 */
export function markTerms(text: string, query: string): TextSegment[] {
  const phrases = queryPhrases(query);
  if (phrases.length === 0) return [{ text, marked: false }];
  const lower = text.toLowerCase();
  const segments: TextSegment[] = [];
  let i = 0;
  while (i < text.length) {
    let matchLen = 0;
    for (const phrase of phrases) {
      const needle = phrase.toLowerCase();
      if (!lower.startsWith(needle, i)) continue;
      if (isWordChar(text[i - 1]) || isWordChar(text[i + phrase.length])) continue;
      matchLen = phrase.length;
      break;
    }
    if (matchLen > 0) {
      segments.push({ text: text.slice(i, i + matchLen), marked: true });
      i += matchLen;
      continue;
    }
    const last = segments[segments.length - 1];
    if (last !== undefined && !last.marked) last.text += text[i];
    else segments.push({ text: text[i]!, marked: false });
    i += 1;
  }
  return segments;
}

/** §6.4 — one chip per distinct entity across the result set's graph paths. */
export interface RelatedEntity {
  entityKey: string;
  display: string;
  /** True when the server's `assurance` label is not one a human vouched for. */
  provisional: boolean;
}

/**
 * The entities the result set's `graph_paths` traversed, deduplicated by
 * `entity_key`, first appearance first.
 *
 * The provisional flag is the SERVER's `assurance` label read through the shared
 * rule (an unrecognised label counts as provisional — fail toward flagging).
 * Nothing here decides whether a fact is true.
 */
export function relatedEntities(paths: readonly RetrievalGraphPathWire[]): RelatedEntity[] {
  const out: RelatedEntity[] = [];
  const seen = new Set<string>();
  for (const path of paths) {
    for (const hop of path.hops) {
      for (const ref of [hop.subject, hop.obj]) {
        if (seen.has(ref.entity_key)) continue;
        seen.add(ref.entity_key);
        out.push({
          entityKey: ref.entity_key,
          display: ref.display,
          provisional: isProvisionalAssurance(ref.assurance),
        });
      }
    }
  }
  return out;
}

// ===========================================================================
// 015A0-ENFORCEMENT — the server's retrieval status, rendered and never derived
// ===========================================================================

/**
 * SPEC's exact sentence for an authority-limited page. It is a fixed literal,
 * not a template: the counts that go beside it are bounded scan accounting and
 * must never be phrased as "n of N documents", which would state a protected
 * corpus total this response does not carry.
 */
export const AUTHORITY_LIMITED_LINE = "Authority-limited — results may be incomplete";

/** What the screen says when the server stated no completeness at all. It
 * claims nothing about authority — the server did not report an authority
 * decision, it reported nothing — only that this page is not proven whole. */
export const COMPLETENESS_UNSTATED_LINE =
  "Completeness unstated — this page is not proven complete";

/**
 * How complete the server said this page is.
 *
 * `complete` is the ONLY state that proves a page whole, and it is the only one
 * a server can put the console in. `limited` carries the two authority-limited
 * statuses with the server's own bounds. `unstated` is the fail-closed state
 * for a response that carried no envelope: absence is not evidence of
 * completeness, so the console holds no completeness claim rather than
 * inventing the reassuring one.
 */
export type RetrievalCompleteness =
  | { kind: "complete" }
  | {
      kind: "limited";
      status: "partial" | "exhausted";
      scanned: number;
      returned: number;
      scanLimit: number;
    }
  | { kind: "unstated" };

export function retrievalCompleteness(
  envelope: AuthorizedRetrievalEnvelopeWire | undefined,
): RetrievalCompleteness {
  if (envelope === undefined) return { kind: "unstated" };
  if (envelope.status === "complete") return { kind: "complete" };
  return {
    kind: "limited",
    status: envelope.status,
    scanned: envelope.scanned_count,
    returned: envelope.returned_count,
    scanLimit: envelope.scan_limit,
  };
}

/**
 * The bounds line beneath the sentence — the server's own scan accounting,
 * formatted and not interpreted. Nothing here compares the bounds to the
 * rendered row count, because the rendered window is this screen's filter and
 * the bounds are the server's scan.
 */
export function authorityLimitBounds(limit: {
  scanned: number;
  returned: number;
  scanLimit: number;
}): string {
  return `${formatCount(limit.scanned)} scanned · ${formatCount(limit.returned)} authorized · ${formatCount(limit.scanLimit)} scan bound`;
}

/**
 * May an empty result set be reported as "nothing matched"?
 *
 * ONLY when the server proved it. `partial` and `exhausted` are
 * authority-limited scan exhaustion — turning either into a silent empty answer
 * is the exact claim ENFORCEMENT forbids, because it asserts that no data
 * exists when what happened is that the scan ran out of authorized budget. An
 * absent envelope proves nothing either, so it does not license the claim
 * (015A0-ENFORCEMENT fail-closed; the pre-ENFORCEMENT reading of absence as
 * "complete" is gone).
 */
export function reportsOrdinaryNoResults(
  envelope: AuthorizedRetrievalEnvelopeWire | undefined,
): boolean {
  return retrievalCompleteness(envelope).kind === "complete";
}
