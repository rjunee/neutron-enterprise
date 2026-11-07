// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Fixtures here are the REAL emitter vocabulary (`ne-runtime/approvals.py`,
// `ne-api/routers/approvals.py`), verified against the live chain: an approve
// writes `approval.granted` with outcome `success`; a reject writes
// `approval.rejected` with outcome `denied`; the API layer echoes both as
// `api.approval.submitted` with outcome `success`. An earlier revision of this
// file pinned invented strings (`approval.approved`, outcome `approved`) that
// no emitter in this repo produces — which is exactly how the Decided section
// shipped unable to state a real verdict (vacuous-guards V8: the claim
// quantifies over the wire; the fixture sampled strings the wire never says).

import { describe, expect, it } from "vitest";
import type { AuditRecordView } from "../api/types";
import { chainDecisions, decisionWord, namesRequest, seqForRequest } from "./chain";

function record(over: Partial<AuditRecordView> = {}): AuditRecordView {
  return {
    id: 1,
    seq: 1,
    event_id: "evt-1",
    event_type: "audit",
    config_hash: "a".repeat(64),
    actor: "06a099a0-f835-49d8-b118-b05d9ede8bfa",
    occurred_at: "2026-07-27T12:04:22Z",
    kpi_tag: null,
    trace_id: null,
    action: "approval.granted",
    resource: "tool:write.internal.crm.note",
    outcome: "success",
    payload: {
      detail: { tier: 2, action: "write.internal.crm.note", status: "approved" },
      provenance: { approval_id: "req-1", approver: "06a099a0" },
    },
    prev_hash: "b".repeat(64),
    record_hash: "c".repeat(64),
    ...over,
  };
}

describe("resolving a decision's chain sequence", () => {
  it("requires the exact terminal action and payload provenance id conjunctively", () => {
    expect(seqForRequest([record({ seq: 77 })], "req-1")).toBe(77);
    expect(
      namesRequest(
        record({
          action: "approval.rejected",
          payload: { provenance: { approval_id: { value: "req-1" } } },
        }),
        "req-1",
      ),
    ).toBe(true);
  });

  it("does not substring-collide terminal ids", () => {
    expect(
      seqForRequest(
        [record({ seq: 4812, payload: { provenance: { approval_id: "prefix-req-1-suffix" } } })],
        "req-1",
      ),
    ).toBeNull();
  });

  it("does not retarget to newer payload-read/tool/API events carrying the same id", () => {
    const laterEvents = [
      record({ seq: 80, action: "tool.execute" }),
      record({ seq: 79, action: "approval.payload_read" }),
      record({ seq: 78, action: "api.approval.submitted", resource: "req-1" }),
      record({ seq: 77, action: "approval.granted" }),
    ];
    expect(seqForRequest(laterEvents, "req-1")).toBe(77);
  });

  it("INVENTS NOTHING: an unmatched request id resolves to null", () => {
    expect(seqForRequest([record({ resource: "req-other", payload: {} })], "req-9")).toBeNull();
    expect(seqForRequest([], "req-1")).toBeNull();
  });

  it("FAIL-CLOSED: an empty request id names nothing, never the first record", () => {
    expect(seqForRequest([record({ seq: 4812, resource: "req-1" })], "")).toBeNull();
  });
});

describe("the Decided section's chain rows", () => {
  it("keeps BOTH real verdict records — granted and rejected — and nothing else, BY IDENTITY", () => {
    const granted = record({ seq: 10 });
    const rejected = record({
      seq: 11,
      action: "approval.rejected",
      outcome: "denied",
      payload: {
        detail: { tier: 2, action: "write.internal.crm.note", status: "rejected" },
        provenance: { approval_id: "req-2", approver: "06a099a0" },
      },
    });
    const apiEcho = record({
      seq: 12,
      event_type: "api",
      action: "api.approval.submitted",
      resource: "req-1",
      outcome: "success",
      payload: {},
    });
    const refusal = record({
      seq: 13,
      event_type: "api",
      action: "api.approval.unauthorized",
      resource: null,
      outcome: "denied",
      payload: {},
    });
    const read = record({ seq: 14, action: "approval.payload_read", outcome: "success" });
    const expired = record({
      seq: 15,
      action: "approval.expired",
      outcome: "denied",
      actor: "system",
    });
    const rows = chainDecisions([granted, rejected, apiEcho, refusal, read, expired]);
    expect(rows.map((r) => r.seq)).toEqual([10, 11]);
  });

  it("CONTROL: an unrelated governed event is not pulled into the approvals section", () => {
    expect(chainDecisions([record({ event_type: "tool.invoke", action: "pdp.decide" })])).toEqual(
      [],
    );
  });

  it("reads the verdict from the served ACTION NAME — never sniffed out of the outcome", () => {
    expect(decisionWord("approval.granted")).toBe("APPROVED");
    expect(decisionWord("approval.rejected")).toBe("DENIED");
    // The API echo's outcome is `success` for BOTH verdicts; it states none.
    expect(decisionWord("api.approval.submitted")).toBeNull();
    expect(decisionWord("approval.expired")).toBeNull();
    expect(decisionWord(null)).toBeNull();
  });
});
