// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — THE LEDGER SPINE, as rules. STATIC: nothing here animates, and nothing
// here is timed. It is the tamper-evidence the screen already states in words,
// drawn once in the seq gutter so it survives a screenshot, a reduced-motion
// setting and the fortieth use of the day.
//
// WHAT THE HAIRLINE CLAIMS, EXACTLY. A segment between two rows says: the chain
// runs from one of these entries to the other and the SERVER verified it whole.
// It does not say the two rows are neighbours — a class chip or a search may be
// hiding entries between them, and the toolbar count states that narrowing. It
// is CONTINUITY that is drawn, not adjacency, and continuity is a served fact.
//
// WHICH IS WHY THE SPINE IS SEQ-ORDERED ONLY. Under a sort by Actor or Outcome
// the rendered order is not the chain's order, so a line joining consecutive
// rows would depict a chain that runs through them in that order — a shape the
// data never had (law 4 cl. 2). Sorted by anything but Seq, the spine is not
// drawn at all; the BREAK pill, the band and the dimmed tail carry the whole
// fact without it.
//
// THIN CLIENT (C7). `chainBreakSeq` reads the SERVED verdict and nothing else.
// No hash is compared, no seq delta is inspected, no continuity is inferred
// from the rows: a page whose hashes visibly do not chain still draws an
// unbroken spine if the server said `valid`, because a console that decided
// otherwise would be publishing a verdict it did not compute.

import type { ChainVerificationView } from "../api/types";
import type { SortState } from "../primitives/tableSort";

/**
 * Which tier of the chain a row sits in.
 *
 *   chain  — the server verified this entry. Full-strength hairline and node.
 *   break  — the entry the server named as the first failure.
 *   after  — past the break: still on the ledger, no longer vouched for. Dim,
 *            matching the dimmed tail the screen already renders.
 */
export type SpineTier = "chain" | "break" | "after";

/** Which edge of the break row carries the severance — the edge facing the
 * entry BEFORE it in the chain, which is the last one the server did verify.
 * Under Seq desc that neighbour is rendered below the break row; under Seq asc,
 * above it. The side is derived, never assumed. */
export type SpineCut = "up" | "down";

export interface SpineMark {
  tier: SpineTier;
  /** `null` on every row but the break entry. */
  cut: SpineCut | null;
}

/**
 * The seq the SERVER named as the first break, or `null` when there is nothing
 * to draw a gap for.
 *
 * A verdict of `valid` yields `null` even when `first_break_seq` is populated:
 * the field is only meaningful under a failing verdict, and a console that drew
 * a gap from a stale number beside `valid: true` would be inventing a break.
 */
export function chainBreakSeq(verification: ChainVerificationView | null): number | null {
  if (verification === null || verification.valid) return null;
  return verification.first_break_seq;
}

/**
 * The spine mark for one row, or `null` when no spine is drawn.
 *
 * `breakSeq` must come from `chainReading` — the entry the CONFIRMED break sits
 * at. `broken` is that same reading's WORD: true while a break stands over this
 * ledger, whether or not anything named its entry. Everything else is the row's
 * own seq and the order it is rendered in.
 *
 * THE TWO ARE NOT ONE FACT, and collapsing them draws the worst picture this
 * module can draw: `breakSeq === null` meaning "nothing to gap" puts a
 * full-strength hairline and node down every row — a whole, verified chain,
 * drawn underneath a strip reading BREAK.
 *
 * `broken` IS THE PARAMETER THAT MAKES THE SEQ-LESS BREAK DRAWABLE, and it is
 * no longer hypothetical. An earlier revision justified it with "the race's own
 * answer names no entry", which was wrong — `_break` (verify.py:180) takes
 * `seq: int`, so an ORDINARY invalid verdict always names an entry. The real
 * case arrived from elsewhere: `AnchoredChainVerifier`'s CONTRADICTION refuses
 * while the `verify_chain` beneath it PASSES, so it carries no
 * `first_break_seq` at all, and it latches on sight. `broken === true` with
 * `breakSeq === null` is therefore a state this console reaches, and this one
 * `if` is the difference between silence and a false picture.
 */
export function ledgerSpine(
  seq: number,
  breakSeq: number | null,
  sort: SortState | null,
  broken: boolean,
): SpineMark | null {
  // Not in chain order → the line would depict an order the chain never had.
  if (sort === null || sort.key !== "seq") return null;
  if (breakSeq === null) {
    // A break STANDS and nothing named its entry (see above: the contradiction
    // verdict) — the server said the chain is not the one it verified and did
    // not say where — so there is
    // no row this console may vouch for and none it may point at. Nothing is
    // drawn: the same silence a non-seq sort gets, and for the same reason.
    // What cannot be drawn truthfully is not drawn, and the strip's words carry
    // the fact alone.
    if (broken) return null;
    return { tier: "chain", cut: null };
  }
  if (seq === breakSeq) return { tier: "break", cut: sort.dir === "desc" ? "down" : "up" };
  // Past the break: the server verified nothing here.
  if (seq > breakSeq) return { tier: "after", cut: null };
  return { tier: "chain", cut: null };
}
