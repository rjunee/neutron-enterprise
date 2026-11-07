// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE PROMOTION RULE, tested as a rule.
//
// The rule is asymmetric, so every assertion here is paired with the control
// that would pass if the asymmetry collapsed in either direction: a test that
// only proves "pending promotes" is satisfied by a rule that promotes
// EVERYTHING, which would empty the band and undo the unit.

import { describe, expect, it } from "vitest";
import {
  bandNames,
  bandStepCount,
  bandTally,
  formatSeconds,
  hasLiveStep,
  isPromoted,
  partitionTurnSteps,
} from "./promotion";
import type { TranscriptStep, TranscriptStepVerdict } from "./types";

function step(verdict: TranscriptStepVerdict, over: Partial<TranscriptStep> = {}): TranscriptStep {
  return {
    key: `${verdict}:${over.name ?? "read.knowledge.search"}`,
    name: over.name ?? "read.knowledge.search",
    verdict,
    verdictLabel: over.verdictLabel ?? "Allowed",
    tier: over.tier ?? null,
    durationSeconds: over.durationSeconds ?? 1.4,
    ...over,
  };
}

describe("which verdicts leave the work band", () => {
  it("promotes the two verdicts that owe a human something, and only those", () => {
    // Both directions in one assertion: the promoted set and its complement.
    const promoted: TranscriptStepVerdict[] = ["pending", "refused"];
    const demoted: TranscriptStepVerdict[] = ["allowed", "running", "failed"];
    expect(promoted.filter((v) => isPromoted(step(v)))).toEqual(promoted);
    expect(demoted.filter((v) => isPromoted(step(v)))).toEqual([]);
  });

  it("keeps a FAILED step in the band — a fault owes no decision", () => {
    // ask-states.html, Error: read.metrics.query sits inside the band with an ✕
    // while `tr-fail` — one object for the TURN — takes the promoted slot.
    const { band, gates } = partitionTurnSteps([
      step("allowed", { name: "read.knowledge.search" }),
      step("failed", { name: "read.metrics.query", verdictLabel: "Error" }),
    ]);
    expect(band.map((s) => s.name)).toEqual(["read.knowledge.search", "read.metrics.query"]);
    expect(gates).toEqual([]);
  });

  it("removes a promoted step from the band's count, names and tally", () => {
    // ask-states.html, Refused: "1 step · read.knowledge.search · ✓1 allowed".
    // The refused call is nowhere in that line — it is its own card.
    const { band, gates } = partitionTurnSteps([
      step("allowed", { name: "read.knowledge.search" }),
      step("refused", { name: "write.comms.email.send" }),
    ]);
    expect(band.map((s) => s.name)).toEqual(["read.knowledge.search"]);
    expect(bandTally(band)).toEqual([{ verdict: "allowed", count: 1, glyph: "✓", tone: "ok" }]);
    expect(gates.map((g) => g.name)).toEqual(["write.comms.email.send"]);
  });

  it("preserves the server's arrival order in both lists", () => {
    const { band, gates } = partitionTurnSteps([
      step("allowed", { name: "a" }),
      step("pending", { name: "p1" }),
      step("allowed", { name: "b" }),
      step("refused", { name: "r1" }),
    ]);
    expect(band.map((s) => s.name)).toEqual(["a", "b"]);
    expect(gates.map((g) => g.name)).toEqual(["p1", "r1"]);
  });
});

describe("the gate a promoted step becomes", () => {
  it("carries the step's sentence and reference through unchanged", () => {
    const sentence = "Tier 2 write, parked. It executes only after a decision is recorded.";
    const [gate] = partitionTurnSteps([
      step("pending", {
        name: "write.internal.crm.note",
        tier: 2,
        sentence,
        reference: "approval-7c19",
      }),
    ]).gates;
    expect(gate).toMatchObject({
      kind: "pending",
      name: "write.internal.crm.note",
      tier: 2,
      sentence,
      reference: "approval-7c19",
    });
  });

  it("nulls an unserved sentence and reference rather than inventing either", () => {
    const [gate] = partitionTurnSteps([step("refused", { name: "write.comms.email.send" })]).gates;
    expect(gate?.sentence).toBeNull();
    expect(gate?.reference).toBeNull();
  });

  it("names the state from the verdict only when the adapter named none", () => {
    const defaults = partitionTurnSteps([step("pending"), step("refused", { name: "w" })]).gates;
    expect(defaults.map((g) => g.state)).toEqual(["Awaiting approval", "Refused"]);
    const named = partitionTurnSteps([step("pending", { stateLabel: "Held for review" })]).gates;
    expect(named[0]?.state).toBe("Held for review");
  });
});

describe("the band summary's tally", () => {
  it("counts only the rows the ledger draws, in allowed/running/failed order", () => {
    const tally = bandTally([
      step("failed", { name: "c" }),
      step("allowed", { name: "a" }),
      step("running", { name: "b" }),
      step("allowed", { name: "d" }),
    ]);
    expect(tally).toEqual([
      { verdict: "allowed", count: 2, glyph: "✓", tone: "ok" },
      { verdict: "running", count: 1, glyph: null, tone: "live" },
      { verdict: "failed", count: 1, glyph: "✕", tone: "fault" },
    ]);
  });

  it("omits an empty group instead of printing a zero", () => {
    // "0 failed" is a claim about the world; the absent group is the honest
    // shape, and this is the assertion that stops a future default from
    // reintroducing a zero row.
    expect(bandTally([step("allowed")]).map((g) => g.verdict)).toEqual(["allowed"]);
    expect(bandTally([]).length).toBe(0);
  });
});

describe("when the band opens itself", () => {
  it("opens on a running step and stays closed otherwise", () => {
    expect(hasLiveStep([step("allowed"), step("running", { name: "b" })])).toBe(true);
    expect(hasLiveStep([step("allowed"), step("failed", { name: "b" })])).toBe(false);
    expect(hasLiveStep([])).toBe(false);
  });
});

describe("how a served duration prints", () => {
  it("prints one decimal and the unit", () => {
    expect(formatSeconds(1.4)).toBe("1.4s");
    expect(formatSeconds(30)).toBe("30.0s");
    expect(formatSeconds(0)).toBe("0.0s");
  });
});

/* --------------------------------------------------------------------------
 * THE PRÉCIS'S MIDDLE — what the turn did, characterised.
 *
 * The band summary used to be `map(name).join(" · ")`, so a turn that called
 * one tool thirty times drew
 *
 *     read.knowledge.search · read.knowledge.search · read.k…
 *
 * — the one fact worth stating (that it did the same thing thirty times) spent
 * as three repetitions and an ellipsis. Paired throughout: characterising the
 * work may not DROP any of it.
 * ------------------------------------------------------------------------ */

describe("bandNames", () => {
  const at = (name: string, key: string, over: Partial<TranscriptStep> = {}): TranscriptStep => ({
    key,
    name,
    verdict: "allowed",
    verdictLabel: "Allowed",
    tier: null,
    durationSeconds: null,
    ...over,
  });

  it("MUTATION `repetition-is-stated`: thirty calls to one tool say so", () => {
    const band = Array.from({ length: 30 }, (_, i) => at("read.knowledge.search", `s${i}`));
    expect(bandNames(band)).toBe("30 × read.knowledge.search");
  });

  it("MUTATION `nothing-is-summarised-away`: every distinct name survives", () => {
    // THE PAIRED HALF. Compressing repetition may not cost a name: a reader
    // scanning the closed band must still be able to tell WHICH tools ran.
    const band = [
      at("read.knowledge.search", "a"),
      at("read.knowledge.search", "b"),
      at("draft.email", "c"),
      at("read.document", "d"),
      at("read.document", "e"),
      at("read.document", "f"),
    ];
    expect(bandNames(band)).toBe("2 × read.knowledge.search · draft.email · 3 × read.document");
  });

  it("keeps first-seen order and leaves a single call as itself", () => {
    // A name that ran once reads as its own name — "1 × x" would be noise, and
    // the server's order is the audit's order.
    expect(bandNames([at("b", "1"), at("a", "2")])).toBe("b · a");
  });

  it("MUTATION `count-is-never-doubled`: a folded row states its size once", () => {
    // The two numbers in the précis measure different things and only one of
    // them is weighted. The HEADLINE count must report every recorded step, so
    // folding cannot shrink it. This line counts ROWS, because a folded row's
    // own name already says how many it stands for — weighting it here printed
    // "6 × 6 governed steps".
    const band = [at("6 governed steps", "f", { foldedCount: 6 })];
    expect(bandStepCount(band)).toBe(6);
    expect(bandNames(band)).toBe("6 governed steps");
  });

  it("an empty band names nothing", () => {
    expect(bandNames([])).toBe("");
  });
});
