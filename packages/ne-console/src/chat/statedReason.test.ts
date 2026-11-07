// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE STATED REASON'S TRANSLATION — checked against the exact strings the live
// deployment published (2026-08-27, Meridian Analyst), not invented samples.
// Two invariants: the reader's line contains no validator vocabulary, and the
// VERBATIM original always survives in `technical` when anything was rewritten.

import { describe, expect, it } from "vitest";
import { UNEXPLAINED_FAILURE_SENTENCE, explainStatedReason } from "./statedReason";

const GROUNDING_RAW =
  "A claim could not be verified against its cited evidence: claim is not " +
  "grounded by exact evidence [numeral-ungrounded x4, phrase-recombination x1].";

const SCHEMA_RAW =
  "The model's final reply did not match this Run's required answer format, and " +
  "the bounded retry did not correct it. No answer was produced. Schema " +
  "rejected: <whole reply> [json_invalid; opens=prose fences=0 objects=1 " +
  "chars=274 sole=tool-call].";

describe("explainStatedReason", () => {
  it("MUTATION `no-validator-jargon`: the grounding finding list becomes stated English", () => {
    const explained = explainStatedReason(GROUNDING_RAW);
    expect(explained.sentence).toBe(
      "A claim could not be verified against its cited evidence: 4 numbers in " +
        "the answer are not present in the cited evidence; and 1 passage " +
        "stitches together wording that no single cited source contains.",
    );
    // The exact technical string survives, whole, for the disclosure.
    expect(explained.technical).toBe(GROUNDING_RAW);
  });

  it("keeps counts honest in the singular", () => {
    const explained = explainStatedReason(
      "A claim could not be verified against its cited evidence: claim is not " +
        "grounded by exact evidence [numeral-ungrounded x1].",
    );
    expect(explained.sentence).toContain(
      "1 number in the answer is not present in the cited evidence",
    );
  });

  it("states an unknown finding class AS a finding of that class — an honest generic, not a guess", () => {
    const explained = explainStatedReason(
      "A claim could not be verified against its cited evidence: claim is not " +
        "grounded by exact evidence [futuristic-check x2].",
    );
    expect(explained.sentence).toContain("2 findings of class “futuristic-check”");
    expect(explained.technical).not.toBeNull();
  });

  it("MUTATION `schema-tail-translated`: the reply-shape vocabulary becomes one plain diagnosis", () => {
    const explained = explainStatedReason(SCHEMA_RAW);
    expect(explained.sentence).toBe(
      "The model's final reply did not match this Run's required answer format, " +
        "and the bounded retry did not correct it. No answer was produced. The " +
        "model answered with a tool call where its final answer belonged.",
    );
    expect(explained.technical).toBe(SCHEMA_RAW);
    expect(explained.sentence).not.toContain("json_invalid");
    expect(explained.sentence).not.toContain("opens=");
  });

  it("diagnoses prose and fenced replies as themselves", () => {
    const prose = explainStatedReason(
      "No answer was produced. Schema rejected: <whole reply> [json_invalid; " +
        "opens=prose fences=0 objects=0 chars=1230].",
    );
    expect(prose.sentence).toContain("wrote prose instead of the required structured answer");
    const fenced = explainStatedReason(
      "No answer was produced. Schema rejected: <whole reply> [json_invalid; " +
        "opens=fence fences=2 objects=1 chars=300].",
    );
    expect(fenced.sentence).toContain("code fence");
  });

  it("passes a reason with no technical segment through verbatim, with nothing to disclose twice", () => {
    const plain = "The Run was cancelled before any model reply arrived.";
    expect(explainStatedReason(plain)).toEqual({ sentence: plain, technical: null });
  });

  it("translates the claim-binding attempt ledger and moves its shape bracket to the disclosure", () => {
    // Verbatim off the live box, 2026-08-27 (train-35's claim-form path).
    const raw =
      "The model's answer used this Run's sources but could not be bound to " +
      "them. Rejected: no span of the final answer is verbatim in any source " +
      "this run served. Attempts: no-span,no-span. [aw=154 sources=2 maxrun=1 floor=5]";
    const explained = explainStatedReason(raw);
    expect(explained.sentence).toBe(
      "The model's answer used this Run's sources but could not be bound to " +
        "them. Rejected: no span of the final answer is verbatim in any source " +
        "this run served. 2 bounded correction attempts were spent without correcting it.",
    );
    expect(explained.technical).toBe(raw);
  });

  // ne-runtime/run_finalizer.py `derived_failure_reason` — the floor every
  // failure whose raiser marked no prose reason publishes, and therefore the
  // single most likely string on a buyer's screen when a turn falls over.
  const FLOOR_RAW =
    "The run failed and the failing step published no explanation. Recorded " +
    "failure class: RunEffectSettlementUncertain.";

  it("MUTATION `no-exception-class-in-the-stated-line`: the runtime's unexplained-failure floor states the absence, not the class", () => {
    const explained = explainStatedReason(FLOOR_RAW);
    expect(explained.sentence).toBe(UNEXPLAINED_FAILURE_SENTENCE);
    // The whole defect in one assertion: a Python exception class is not the
    // platform's account of itself, and this line is the largest text in the
    // fault card.
    expect(explained.sentence).not.toContain("RunEffectSettlementUncertain");
    expect(explained.sentence).not.toContain("failure class");
    // Nothing is deleted — the class name survives whole, for the disclosure.
    expect(explained.technical).toBe(FLOOR_RAW);
  });

  it("says the same thing when the runtime could not name a class either", () => {
    // `safe_failure_class` substitutes this constant when the raiser supplied
    // no servable class; the reader's line does not change, because what the
    // reader learns did not change.
    const explained = explainStatedReason(
      "The run failed and the failing step published no explanation. Recorded " +
        "failure class: UnnamedRunFailure.",
    );
    expect(explained.sentence).toBe(UNEXPLAINED_FAILURE_SENTENCE);
    expect(explained.technical).not.toBeNull();
  });

  it("invents no cause: the stated line claims only what the record holds", () => {
    // Guards the one temptation this arm exists to resist — writing a
    // plausible reason (a timeout, a model error) over a record that names
    // none. Every word here is about the ABSENCE of an explanation.
    for (const forbidden of ["timed out", "timeout", "model", "network", "policy", "retry"]) {
      expect(UNEXPLAINED_FAILURE_SENTENCE.toLowerCase()).not.toContain(forbidden);
    }
  });

  it("strips a trailing key=value bracket it cannot translate, keeping the stated sentences", () => {
    const raw = "The Run stopped at its context ceiling. [ctx=131072 spent=131001]";
    expect(explainStatedReason(raw)).toEqual({
      sentence: "The Run stopped at its context ceiling.",
      technical: raw,
    });
  });
});
