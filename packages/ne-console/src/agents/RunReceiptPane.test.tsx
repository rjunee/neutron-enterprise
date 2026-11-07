// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE RECEIPT'S "REASON" IS SAID TO A PERSON — and this is the pane a presenter
// opens to PROVE the system governs itself, so it was the worst screen in the
// console on which to print the validators' closed vocabulary as the answer.
// Both surfaces that state a Run's failure reason are covered here: the docked
// receipt's field and the transcript turn's receipt fact. Every raw string
// below is one the live deployment actually publishes.

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { UNEXPLAINED_FAILURE_SENTENCE } from "../chat/statedReason";
import { RunReceiptFields } from "./RunReceiptPane";
import { buildRunTurn } from "./runTurn";
import type { RunDetail } from "./types";

/** ne-runtime/run_finalizer.py `derived_failure_reason` — the floor a failure
 * nobody explained publishes. Only ~10 of the ~40 classes in errors.py carry
 * prose of their own, so this is what most unanticipated failures say. */
const FLOOR_RAW =
  "The run failed and the failing step published no explanation. Recorded " +
  "failure class: RunEffectSettlementUncertain.";

/** Verbatim off the live box, 2026-08-27 (the claim-form binding path). */
const ATTEMPTS_RAW =
  "The model's answer used this Run's sources but could not be bound to them. " +
  "Rejected: no span of the final answer is verbatim in any source this run " +
  "served. Attempts: no-span,no-span. [aw=154 sources=2 maxrun=1 floor=5]";

function failedRun(failureReason: string | null): RunDetail {
  return {
    run_id: "run-9",
    conversation_id: "conversation-1",
    agent_id: "meridian",
    agent_version: 3,
    sequence: 9,
    version: 2,
    predecessor_run_id: null,
    root_run_id: "run-9",
    retry_ordinal: 0,
    display_label: "Run 9 · Agentic · 09:00",
    strategy: "agentic",
    status: "failed",
    output_verdict: "unavailable",
    model_policy_id: "policy-1",
    model_request_mode: "auto",
    context_snapshot_hash: "9".repeat(64),
    requested_model: null,
    requested_model_lane: null,
    served_model: null,
    route_position: "unavailable",
    fallback_from: null,
    output_preview: null,
    config_hash: "a".repeat(64),
    capability_snapshot_hash: "b".repeat(64),
    created_at: "2026-08-27T09:00:00Z",
    updated_at: "2026-08-27T09:00:12Z",
    terminal_at: "2026-08-27T09:00:12Z",
    output_state: "unavailable",
    approval_id: null,
    failure_reason: failureReason,
    agentic_plan: null,
    execution_profile_receipt: null,
    skill_activation_receipt: null,
    claims: [],
    claim_mapping_state: "unavailable",
    claims_truncated: false,
  };
}

function reasonField(): HTMLElement {
  const label = screen.getByText("Reason");
  const field = label.closest(".ne-detail__field");
  expect(field).not.toBeNull();
  return field as HTMLElement;
}

/** The field's own line, with the disclosure's contents removed — i.e. exactly
 * what the reader sees at rest, before opening anything. */
function headline(field: HTMLElement): string {
  const copy = field.cloneNode(true) as HTMLElement;
  copy.querySelectorAll("details").forEach((node) => node.remove());
  return (copy.querySelector(".ne-detail__value")?.textContent ?? "").trim();
}

function renderFields(run: RunDetail) {
  render(
    <MemoryRouter>
      <RunReceiptFields run={run} agent={null} conversation={null} />
    </MemoryRouter>,
  );
}

describe("RunReceiptFields — the Reason a failed Run states", () => {
  it("MUTATION `receipt-reason-translated`: the runtime's floor does not put a Python class on the receipt", () => {
    renderFields(failedRun(FLOOR_RAW));
    const field = reasonField();
    expect(headline(field)).toBe(UNEXPLAINED_FAILURE_SENTENCE);
    expect(headline(field)).not.toContain("RunEffectSettlementUncertain");
  });

  it("MUTATION `receipt-reason-disclosed`: the verbatim server string is one click away, not gone", () => {
    renderFields(failedRun(FLOOR_RAW));
    const field = reasonField();
    const drawer = field.querySelector("details");
    expect(drawer).not.toBeNull();
    // The SAME drawer wording the Ask card uses (chat/TurnFailureReadout
    // `ServerWords`), because it is the same component and not a second copy.
    expect(within(field).getByText(/what the server said/)).toBeTruthy();
    expect(drawer?.textContent).toContain(FLOOR_RAW);
  });

  it("keeps the validators' closed vocabulary out of the headline and whole in the drawer", () => {
    renderFields(failedRun(ATTEMPTS_RAW));
    const field = reasonField();
    const line = headline(field);
    expect(line).toContain("2 bounded correction attempts were spent without correcting it.");
    expect(line).not.toContain("aw=154");
    expect(line).not.toContain("no-span");
    expect(field.querySelector("details")?.textContent).toContain(ATTEMPTS_RAW);
  });

  it("draws NO drawer over a reason that was already plain — nothing is disclosed twice", () => {
    const plain = "The Run was cancelled before any model reply arrived.";
    renderFields(failedRun(plain));
    const field = reasonField();
    expect(headline(field)).toBe(plain);
    expect(field.querySelector("details")).toBeNull();
  });

  it("still states the absence where the Run published no marked reason", () => {
    renderFields(failedRun(null));
    expect(headline(reasonField())).toContain("the Run published no marked reason");
  });
});

describe("buildRunTurn — the Reason fact on the turn's receipt", () => {
  function reasonFact(run: RunDetail) {
    const turn = buildRunTurn({ run, siblings: [], approval: null });
    return turn.receipt?.facts?.find((fact) => fact.key === "reason");
  }

  it("MUTATION `turn-reason-translated`: the receipt strip states the floor, never the class name", () => {
    expect(reasonFact(failedRun(FLOOR_RAW))?.value).toBe(UNEXPLAINED_FAILURE_SENTENCE);
  });

  it("says the attempt ledger as a count — the footer strip has no room for a disclosure", () => {
    const value = reasonFact(failedRun(ATTEMPTS_RAW))?.value ?? "";
    expect(value).toContain("2 bounded correction attempts were spent without correcting it.");
    expect(value).not.toContain("[aw=154");
  });

  it("pushes no Reason fact at all where the server marked none", () => {
    expect(reasonFact(failedRun(null))).toBeUndefined();
  });
});
