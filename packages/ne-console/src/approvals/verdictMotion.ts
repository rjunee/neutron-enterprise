// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §4.5 / §4.6 — THE VERDICT GRAMMAR. A DENIAL NEVER EASES.
//
// Law 4 cl. 4 (styles/primitives.css) is this module's whole contract: refusal,
// break and fault states appear instantly at full strength; affirmative
// completions settle with --ease-damped. An allow and a deny must FEEL different
// before the word is read. The grammar therefore lives HERE, as one exported
// mapping over the closed verdict vocabulary, rather than as an inline ternary
// per surface — a grammar spread across four call sites is a grammar that drifts
// on the fifth.
//
// TWO GATES, AND THEY ARE DIFFERENT ON PURPOSE.
//
//   The verdict word decides WHICH grammar applies. It is total over the closed
//   set (receipt.ts's verdictWord), so a new verdict cannot be added without
//   choosing a side, and the guardian asserts every word individually — a
//   mapping that made everything settle, or everything arrest, fails.
//
//   Arrival decides WHETHER a settle happens at all (cl. 2: depicts only what
//   happened). A pane that opens onto a decision it already had is rendering an
//   already-known state; dressing that with an arrival is an animation of an
//   event that did not just occur. An ARREST is never gated: a refusal renders
//   instantly whether it landed a moment ago or is being read for the tenth
//   time, which is exactly what "instantly at full strength" means.
//
// The classes below are applied by the pane and defined in approvals.css. They
// carry NO information: every fact they dress — the verdict word, the glyph, the
// approver, the time, the chain entry — is text in the same DOM (cl. 6).

import { useState } from "react";

/** The closed set of server verdicts this screen renders (receipt.ts). */
export type Verdict = "APPROVED" | "DENIED" | "AWAITING SECOND" | "EXPIRED";

/** `settle` seats a state into place over --motion-settle; `arrest` is the
 * absence of any easing, declared rather than merely omitted. */
export type Grammar = "settle" | "arrest";

/**
 * Which side of law 4 cl. 4 each verdict falls on.
 *
 * APPROVED and AWAITING SECOND are affirmations — the request moved forward, and
 * in the Tier-3 case it moved forward by exactly one approver. DENIED and
 * EXPIRED are stops: one is a refusal, the other is a request that ran out of
 * time without one. Both arrest.
 */
const GRAMMAR: Record<Verdict, Grammar> = {
  APPROVED: "settle",
  "AWAITING SECOND": "settle",
  DENIED: "arrest",
  EXPIRED: "arrest",
};

export function verdictGrammar(verdict: Verdict): Grammar {
  return GRAMMAR[verdict];
}

/** translateY(3px) → 0 + fade, --motion-settle, --ease-damped. */
export const SETTLE_CLASS = "ne-approvals__settle";
/** §4.6's one --motion-chain step: the record lands after the decision. */
export const CHAINED_CLASS = "ne-approvals__settle--chained";
/** No animation, no transition — stated, not left to the absence of a rule. */
export const ARREST_CLASS = "ne-approvals__arrest";

/**
 * §4.6 — the receipt's two lines, as one decision rather than two.
 *
 * The whole receipt obeys the verdict's grammar. A denied receipt arrests in
 * BOTH lines: it would read as a contradiction for the record of a refusal to
 * come easing in 60 ms behind the refusal itself, and cl. 4 does not admit
 * "mostly instant". An approved receipt settles, and its "recorded in the audit
 * log" line settles one --motion-chain step later, so the order of appearance
 * mirrors the order of fact: decision first, record second.
 */
export interface ReceiptMotion {
  /** Class for the verdict line, or undefined for no motion at all. */
  verdict: string | undefined;
  /** Class for the audit-record line, or undefined for no motion at all. */
  record: string | undefined;
}

export function receiptMotion(verdict: Verdict, arrived: boolean): ReceiptMotion {
  if (verdictGrammar(verdict) === "arrest") {
    return { verdict: ARREST_CLASS, record: ARREST_CLASS };
  }
  if (!arrived) return { verdict: undefined, record: undefined };
  return { verdict: SETTLE_CLASS, record: `${SETTLE_CLASS} ${CHAINED_CLASS}` };
}

/**
 * §4.5's novelty gate: did this answer ARRIVE in front of the reader?
 *
 * True only for a component instance that watched `key` go from unanswered to
 * answered. A pane mounted straight onto an answer never returns true, and once
 * it flips it STAYS flipped — the class is applied once and never re-applied, so
 * the ~1/s clock re-render that ticks the Due countdown cannot restart the
 * animation. Switching to another request resets it in the same render pass.
 *
 * No data waits on this (cl. 7): the receipt renders from `state.result` in the
 * frame the client learns it, and this decides only whether it is dressed.
 */
export function useVerdictArrival(key: string, answered: boolean): boolean {
  const [seen, setSeen] = useState({ key, unanswered: !answered });
  if (seen.key !== key) {
    // Deriving state from a changed prop during render: React discards this
    // pass and re-renders immediately, so the answer below is never one render
    // stale — which for a decision key that opens a confirm would matter.
    setSeen({ key, unanswered: !answered });
    return false;
  }
  return seen.unanswered && answered;
}

/** Join the class parts a surface carries, dropping the ones it does not. */
export function motionClass(base: string, motion: string | undefined): string {
  return motion === undefined ? base : `${base} ${motion}`;
}
