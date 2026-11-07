// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5's copy table has five lines for a turn that produced no answer, and this
// module decides which. The one that must never be wrong is REFUSED: a policy
// refusal drawn as a fault makes the permission trap — two logins, one question,
// two results — unobservable, because both outcomes read as the same word.
//
// The server's string survives byte-for-byte in `detail` in every branch. That
// is the only account of what happened that is not this console guessing.

import { describe, expect, it } from "vitest";
import {
  abortFailure,
  classifyTurnFailure,
  groundingUnavailableFailure,
  isModelSelectionVerdict,
  modelSelectionFailure,
  noDepartmentFailure,
  thrownTransportFailure,
} from "./turnFailure";
import { transportUserMessage } from "../api/userMessage";

// ---------------------------------------------------------------------------
// THE THROWN TRANSPORT ERROR — classified from the throw, not from its prose.
//
// WHY THIS BLOCK EXISTS. The console used to hand `classifyTurnFailure` the
// string a thrown transport error carried, and for a detail-less stream death
// that string was the CLIENT'S own transport line. It contained the two words
// the stream pattern looks for, so the classification came out right for a
// reason that had nothing to do with the failure. The moment that property
// became a sentence written for a reader, every dead stream started classifying
// as "the turn settled and produced no answer" — ASK_NO_ANSWER and a Retry over
// a wire that was never open — and no test noticed, because the only ones
// asserting the stream line exercised the clean-close path, which never throws.
//
// So these tests assert the property the old shape could not have: the verdict
// is a function of the STATUS and of nothing else. Every one of them sweeps the
// detail through unrelated wordings, INCLUDING the house sentences, and expects
// the same answer each time.
// ---------------------------------------------------------------------------

/** Wordings a thrown error's string has taken or could take: the house
 * sentences the seam writes today, prose that would trip each of
 * classifyTurnFailure's patterns, and nothing at all. */
const WORDINGS = [
  "",
  transportUserMessage(500),
  transportUserMessage(409),
  transportUserMessage(429),
  "PolicyDenied: tool read.finance.ledger",
  "upstream timed out",
  "ECONNREFUSED",
  "RedactionError",
  "the gateway is having a quiet lie-down",
];

describe("thrownTransportFailure", () => {
  it("says the stream did not survive, for every status a dead stream arrives with", () => {
    // The exact six an adversarial review measured regressing to stage/none.
    for (const status of [409, 429, 500, 502, 503, 504]) {
      for (const detail of WORDINGS) {
        expect(thrownTransportFailure(status, detail), `${status}: ${detail}`).toEqual({
          kind: "transport",
          reason: "stream",
          detail,
          retryable: true,
        });
      }
    }
  });

  it("says the same when there was no HTTP response at all", () => {
    for (const detail of WORDINGS) {
      expect(thrownTransportFailure(null, detail).reason).toBe("stream");
    }
  });

  it("is INDEPENDENT of the prose: one status, one verdict, whatever the words", () => {
    // The property, stated directly. If any wording could move a verdict, the
    // classification would again be a function of copy — and copy changes.
    for (const status of [null, 400, 401, 403, 404, 408, 409, 418, 422, 429, 500, 502, 503, 504]) {
      const verdicts = new Set(
        WORDINGS.map((detail) => {
          const failure = thrownTransportFailure(status, detail);
          return `${failure.kind}/${failure.reason}/${String(failure.retryable)}`;
        }),
      );
      expect([status, verdicts.size]).toEqual([status, 1]);
    }
  });

  it("keeps the gate's 403 a refusal and the 422 the department line", () => {
    // Two statuses that are NOT a dead stream. A 403 is a decision — refusal
    // tone, no retry — and a 422 is the session create's no-department answer.
    expect(thrownTransportFailure(403, "not in the analyst role")).toEqual({
      kind: "refused",
      reason: "refused",
      detail: "not in the analyst role",
      retryable: false,
    });
    expect(thrownTransportFailure(422, "").reason).toBe("no-department");
  });

  it("carries the detail through untouched, and never reads it", () => {
    const served = "upstream closed the connection after 0 bytes";
    expect(thrownTransportFailure(502, served).detail).toBe(served);
  });
});

describe("classifyTurnFailure", () => {
  it("classifies a policy refusal as refused, and never as a fault", () => {
    for (const detail of [
      "PolicyDenied: tool read.finance.ledger",
      "the pdp denied this request",
      "403 Forbidden",
      "not authorized for this action",
    ]) {
      const failure = classifyTurnFailure(detail);
      expect(failure.kind, detail).toBe("refused");
      expect(failure.reason, detail).toBe("refused");
      expect(failure.retryable, detail).toBe(false);
      expect(failure.detail, detail).toBe(detail);
    }
  });

  it("classifies a guardrail block as refused", () => {
    expect(classifyTurnFailure("guardrail: prompt injection blocked").reason).toBe("refused");
  });

  it("keeps an AIR_GAP model refusal non-retryable", () => {
    const detail = "frontier model selection is unavailable under AIR_GAP";
    expect(classifyTurnFailure(detail)).toEqual({
      kind: "refused",
      reason: "refused",
      detail,
      retryable: false,
    });
  });

  it("classifies a timeout as the timeout line", () => {
    expect(classifyTurnFailure("upstream timed out").reason).toBe("timeout");
    expect(classifyTurnFailure("deadline exceeded").reason).toBe("timeout");
  });

  it("classifies a dropped connection as the stream line", () => {
    expect(classifyTurnFailure("failed to fetch").reason).toBe("stream");
    expect(classifyTurnFailure("ECONNREFUSED").reason).toBe("stream");
  });

  it("classifies anything else as a turn that produced no answer, and keeps the server's words", () => {
    const failure = classifyTurnFailure("RedactionError");
    expect(failure.reason).toBe("none");
    expect(failure.kind).toBe("stage");
    expect(failure.retryable).toBe(true);
    // The exception class name is CARRIED, never promoted into answer typography.
    expect(failure.detail).toBe("RedactionError");
  });
});

describe("the two facts that are not classifications", () => {
  it("names the missing department claim as its own state, with no retry", () => {
    const failure = noDepartmentFailure("principal carries no department claim");
    expect(failure.reason).toBe("no-department");
    expect(failure.retryable).toBe(false);
  });

  it("keeps a stale grounded-agent 422 distinct from the department claim", () => {
    expect(groundingUnavailableFailure("unknown agent_name")).toEqual({
      kind: "stage",
      reason: "grounding-unavailable",
      detail: "unknown agent_name",
      retryable: false,
    });
  });

  it("draws the console's own give-up as a timeout, and the reader's stop as its own fact", () => {
    expect(abortFailure("deadline", "").reason).toBe("timeout");
    expect(abortFailure("stall", "").reason).toBe("timeout");
    // Pressing Stop is not a timeout, and it is not "no answer was produced"
    // either — the console stopped LISTENING; a durably-run turn may still
    // finish server-side. Its own reason renders §5's honest stopped line.
    expect(abortFailure("reader", "").reason).toBe("stopped");
    expect(abortFailure("reader", "").retryable).toBe(true);
  });
});

describe("explicit model-selection failures", () => {
  it("keeps a 403 policy refusal authoritative and non-retryable", () => {
    const detail = "frontier model selection is unavailable under AIR_GAP";
    expect(modelSelectionFailure(403, detail)).toEqual({
      kind: "refused",
      reason: "refused",
      detail,
      retryable: false,
    });
  });

  it("keeps 409 availability distinct from a policy refusal", () => {
    expect(modelSelectionFailure(409, "requested model is unavailable")).toEqual({
      kind: "stage",
      reason: "model-unavailable",
      detail: "requested model is unavailable",
      retryable: false,
    });
  });

  it("keeps a 503 unverified result fail-closed and non-retryable", () => {
    expect(modelSelectionFailure(503, "model evidence is unavailable")).toEqual({
      kind: "stage",
      reason: "model-unverified",
      detail: "model evidence is unavailable",
      retryable: false,
    });
  });
});

describe("isModelSelectionVerdict", () => {
  // REGRESSION — the second-turn 409. A model-carrying turn that died on the
  // Conversation-admission conflict was reported as "The selected model is
  // unavailable.", which was simply false: the model was fine, the turn was
  // refused admission. Status alone is not the verdict; the server's words are.
  it("does not read a canonical-Ask admission conflict as a model verdict", () => {
    expect(isModelSelectionVerdict(409, "canonical default Ask conflict")).toBe(false);
    expect(isModelSelectionVerdict(503, "canonical default Ask is bootstrapping")).toBe(false);
    expect(isModelSelectionVerdict(503, "canonical Agent Run enqueue failed")).toBe(false);
    expect(
      isModelSelectionVerdict(409, "idempotency key conflicts with the original request"),
    ).toBe(false);
  });

  it("keeps every closed model-selection verdict a model verdict", () => {
    expect(isModelSelectionVerdict(409, "requested model is unavailable")).toBe(true);
    expect(isModelSelectionVerdict(409, "requested model is unavailable: frontier-default")).toBe(
      true,
    );
    expect(isModelSelectionVerdict(409, "no model is currently available")).toBe(true);
    expect(isModelSelectionVerdict(503, "model selection is unavailable")).toBe(true);
    expect(isModelSelectionVerdict(503, "model selection evidence is unavailable")).toBe(true);
    // A 403 is a policy refusal either way and renders the server's words.
    expect(isModelSelectionVerdict(403, "selection is unavailable under AIR_GAP")).toBe(true);
  });

  it("classifies the admission conflict as a plain stage failure with the server's words", () => {
    expect(classifyTurnFailure("canonical default Ask conflict")).toEqual({
      kind: "stage",
      reason: "none",
      detail: "canonical default Ask conflict",
      retryable: true,
    });
  });
});
