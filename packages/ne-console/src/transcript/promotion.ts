// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE PROMOTION RULE — the one asymmetry the transcript is built on.
//
// The tool calls ARE the governance story (design spec §3), so they cannot
// simply be hidden. The rule is therefore asymmetric, and it is a rule about
// WHERE a step is drawn, not about how loudly:
//
//   ROUTINE ALLOWED WORK DEMOTES. `allowed`, `running` and `failed` steps stay
//   inside the work band, a disclosure closed by default. Nothing is hidden:
//   the band's summary line — count · names · verdict tally · elapsed — IS the
//   audit précis, so a reader who never opens it still knows how many steps
//   ran, which tools, and how each one came out. Compression, not concealment.
//
//   ANYTHING PENDING A HUMAN OR REFUSED PROMOTES. `pending` and `refused` steps
//   leave the band entirely — out of its count, out of its names, out of its
//   tally — and become gate cards rendered OUTSIDE the collapsible region. They
//   are visible with nothing expanded and there is no state in which they fold.
//
// WHY `failed` STAYS IN THE BAND, which looks like an exception and is not. A
// failed step is a fault, not a verdict: no human owes it a decision and no
// policy refused anything. It is counted in the tally, in red ("✓1 allowed ·
// ✕1 failed"), and the TURN's failure gets its own promoted readout beside the
// band (TranscriptFailure) — one object for the turn, not one per step. This is
// exactly what ask-states.html draws for the Error state: `read.metrics.query`
// sits inside the band with an ✕, and `tr-fail` sits outside it.
//
// Pure. No React, no DOM — so the rule is testable as a rule, and the render
// cannot disagree with it by construction: the components below consume only
// what this file returns.

import type { TranscriptGate, TranscriptStep, TranscriptStepVerdict } from "./types";

/** The verdicts that leave the band. Named once; every check reads this set. */
const PROMOTED: ReadonlySet<TranscriptStepVerdict> = new Set<TranscriptStepVerdict>([
  "pending",
  "refused",
]);

/** The state word a promoted step draws when the adapter names none. */
const DEFAULT_STATE: Record<"pending" | "refused", string> = {
  pending: "Awaiting approval",
  refused: "Refused",
};

/** True when this step must be drawn outside the collapsible region. */
export function isPromoted(step: TranscriptStep): boolean {
  return PROMOTED.has(step.verdict);
}

/** One turn's steps split by the rule above. `band` keeps arrival order; so do
 * `gates`. Neither list is re-sorted: the server's order is the audit's order. */
export function partitionTurnSteps(steps: readonly TranscriptStep[]): {
  band: readonly TranscriptStep[];
  gates: readonly TranscriptGate[];
} {
  const band: TranscriptStep[] = [];
  const gates: TranscriptGate[] = [];
  for (const step of steps) {
    if (isPromoted(step)) {
      gates.push(gateFor(step));
      continue;
    }
    band.push(step);
  }
  return { band, gates };
}

/** The gate card a promoted step becomes. Every field is the step's own; the
 * only thing added is the state word, and only when the adapter named none. */
function gateFor(step: TranscriptStep): TranscriptGate {
  const kind = step.verdict === "pending" ? "pending" : "refused";
  return {
    key: step.key,
    kind,
    state: step.stateLabel ?? DEFAULT_STATE[kind],
    name: step.name,
    tier: step.tier,
    sentence: step.sentence ?? null,
    reference: step.reference ?? null,
    action: step.action,
  };
}

/** HOW MANY RECORDED STEPS ONE ROW STANDS FOR. Exactly one, unless the adapter
 * folded a consecutive run of content-free governed events into it — see
 * TranscriptStep.foldedCount.
 *
 * THE PRÉCIS MUST NOT SHRINK WHEN THE LEDGER FOLDS. The summary line is the
 * audit précis; if folding twelve `event.unavailable` rows into one made the
 * headline read "3 steps" for a turn that recorded fourteen, the fold would
 * have changed a governance fact to tidy a screen. Every count below is
 * therefore weighted by this, not by the number of <li>s. */
function stepWeight(step: TranscriptStep): number {
  const folded = step.foldedCount;
  return folded === undefined || folded < 1 ? 1 : folded;
}

/** The number of recorded steps the band stands for — the summary's headline
 * count, and the number a folded row cannot make smaller. */
export function bandStepCount(band: readonly TranscriptStep[]): number {
  return band.reduce((total, step) => total + stepWeight(step), 0);
}

/**
 * THE PRÉCIS'S MIDDLE: what the turn DID, characterised rather than enumerated.
 *
 * It used to be `steps.map(name).join(" · ")`. On a turn that called one tool
 * thirty times that produced
 *
 *     read.knowledge.search · read.knowledge.search · read.k…
 *
 * which tells a reader neither what the agent was doing nor that it did the
 * same thing thirty times over — the one fact that line was in a position to
 * state. The repetition WAS the story and the render was spending its whole
 * width hiding it.
 *
 * So: each distinct name once, in first-seen order, carrying how many rows bear
 * it. A name that appears once reads as itself; a name that repeats reads
 * "30 × read.knowledge.search". Distinct tools stay distinguishable at a
 * glance, because each of them still appears.
 *
 * NOTHING IS SUMMARISED AWAY. Every distinct name the band holds is in this
 * string. Where the line is still too long for its track the STYLESHEET
 * ellipsizes it, which is reversible; this function drops nothing.
 *
 * IT COUNTS ROWS, NOT RECORDED STEPS — the one place in this file that does,
 * and deliberately. A folded row's NAME already states its own size ("6
 * governed steps"), so weighting it by `foldedCount` here printed "6 × 6
 * governed steps". The headline count (`bandStepCount`) is the number that must
 * be weighted, because it is the only one claiming how much work the Run did.
 */
export function bandNames(band: readonly TranscriptStep[]): string {
  const order: string[] = [];
  const counts = new Map<string, number>();
  for (const step of band) {
    const seen = counts.get(step.name);
    if (seen === undefined) order.push(step.name);
    counts.set(step.name, (seen ?? 0) + 1);
  }
  return order
    .map((name) => {
      const count = counts.get(name)!;
      return count > 1 ? `${count} × ${name}` : name;
    })
    .join(" · ");
}

/** One counted group in the band's verdict tally. `tone` picks the hue: the
 * tally is the précis, so a failure inside it must be visible as one. */
export interface TranscriptTally {
  readonly verdict: "allowed" | "running" | "failed";
  readonly count: number;
  readonly glyph: string | null;
  readonly tone: "ok" | "live" | "fault";
}

/** The tally, derived by counting the rows the band actually draws. It can
 * therefore never claim a step the ledger does not list. Groups with a zero
 * count are omitted rather than printed as "0 refused" — a zero here would be
 * a claim about the world, and the absent group is the honest shape. */
export function bandTally(band: readonly TranscriptStep[]): readonly TranscriptTally[] {
  const shape = [
    { verdict: "allowed", glyph: "✓", tone: "ok" },
    { verdict: "running", glyph: null, tone: "live" },
    { verdict: "failed", glyph: "✕", tone: "fault" },
  ] as const;
  const out: TranscriptTally[] = [];
  for (const group of shape) {
    const count = bandStepCount(band.filter((s) => s.verdict === group.verdict));
    if (count === 0) continue;
    out.push({ verdict: group.verdict, count, glyph: group.glyph, tone: group.tone });
  }
  return out;
}

/** True while the server says a step of this turn is still running. The band
 * opens itself for exactly this case (ask-states.html, Streaming) and for no
 * other — it is closed by default everywhere else. */
export function hasLiveStep(band: readonly TranscriptStep[]): boolean {
  return band.some((s) => s.verdict === "running");
}

/** Seconds as the transcript prints them: one decimal, then the unit. Given a
 * number the server measured — `null` has no rendering here on purpose, because
 * every absent duration goes through the em dash instead. */
export function formatSeconds(seconds: number): string {
  return `${seconds.toFixed(1)}s`;
}
