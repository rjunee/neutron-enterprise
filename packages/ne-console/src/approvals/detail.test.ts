// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §8 step 2 + the Tier-3 withhold rule + §8 step 3, and the receipt's six lines.
//
// The load-bearing case is the UNREVEALED one: the queue listing is payload-free
// and stays that way, so until the operator runs the governed per-request read
// the screen knows nothing about what approving would do — and a Tier-3 request
// offers no Approve. The revealed case fixes that ONE predicate flips every
// consequence, and that a served value reaches the row exactly as sent.

import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import type { ApprovalActionResult, ApprovalPayloadView, PendingApprovalView } from "../api/types";
import {
  IDLE,
  approveWithheld,
  describes,
  disclosedFields,
  disclosureFromError,
  disclosureFromPayload,
  parameterText,
  refLabel,
  refProposalHref,
  sourceDetailWithheld,
  unconfirmedFacts,
} from "./detail";
import type { Disclosure } from "./detail";
import { buildReceipt, verdictWord } from "./receipt";

function approval(over: Partial<PendingApprovalView> = {}): PendingApprovalView {
  return {
    request_id: "req-t3-wire",
    action: "payments.wire_transfer",
    tier: 3,
    principal_id: "agent://finance-ops",
    requested_at: "2026-07-27T11:40:00Z",
    sla_deadline: "2026-07-27T14:00:00Z",
    expires_at: "2026-07-28T12:00:00Z",
    escalated: false,
    ...over,
  };
}

function payload(over: Partial<ApprovalPayloadView> = {}): ApprovalPayloadView {
  return {
    request_id: "req-t3-wire",
    action: "payments.wire_transfer",
    tier: 3,
    principal_id: "agent://finance-ops",
    target_resource: "ledger://acct/4021",
    description: "Wire transfer to a named counterparty.",
    material_parameters: { amount: "48200.00", counterparty: "Sable Ltd" },
    reversibility: "Reversible for 30 minutes by the same approver.",
    ...over,
  };
}

const revealed = (over: Partial<ApprovalPayloadView> = {}): Disclosure => ({
  kind: "revealed",
  payload: payload(over),
});

describe("§8 step 2 — the disclosed rows", () => {
  it("is Target, then one row per material parameter, then Reversibility", () => {
    expect(disclosedFields(payload())).toEqual([
      { label: "Target", value: "ledger://acct/4021", mono: true },
      { label: "Description", value: "Wire transfer to a named counterparty.", mono: false },
      { label: "amount", value: "48200.00", mono: true },
      { label: "counterparty", value: "Sable Ltd", mono: true },
      {
        label: "Reversibility",
        value: "Reversible for 30 minutes by the same approver.",
        mono: false,
      },
    ]);
  });

  it("renders a parameter exactly as sent — no currency, no unit, no rounding", () => {
    expect(parameterText("48200.00")).toBe("48200.00");
    expect(parameterText(48200)).toBe("48200");
    expect(parameterText({ nested: true })).toBe('{"nested":true}');
    expect(
      disclosedFields(payload({ material_parameters: { amount: 48200, ok: false } })),
    ).toContainEqual({ label: "amount", value: "48200", mono: true });
  });

  it("makes NO row for a field the response did not carry — no placeholder, no zero", () => {
    const fields = disclosedFields(
      payload({ description: "", material_parameters: null, reversibility: "  " }),
    );
    expect(fields).toEqual([{ label: "Target", value: "ledger://acct/4021", mono: true }]);
  });
});

describe("§8 — the Tier-3 withhold rule", () => {
  const unrevealed: Disclosure[] = [
    IDLE,
    { kind: "loading" },
    { kind: "refused", detail: "not a named approver for this request" },
    { kind: "error" },
  ];

  it("withholds Approve on a Tier-3 request until THIS request's payload is revealed", () => {
    for (const disclosure of unrevealed) {
      expect(approveWithheld(approval({ tier: 3 }), disclosure)).toBe(true);
    }
  });

  it("stops withholding the moment the payload is revealed — one predicate, every consequence", () => {
    expect(approveWithheld(approval({ tier: 3 }), revealed())).toBe(false);
    expect(describes(revealed())).toBe(true);
  });

  it("keeps withholding on a 200 that describes nothing — a payload with no rows informs nobody", () => {
    const empty = revealed({
      target_resource: "",
      description: "",
      material_parameters: null,
      reversibility: "",
    });
    expect(describes(empty)).toBe(false);
    expect(approveWithheld(approval({ tier: 3 }), empty)).toBe(true);
  });

  it("CONTROL: a Tier-2 request is never withheld, revealed or not", () => {
    for (const disclosure of [...unrevealed, revealed()]) {
      expect(approveWithheld(approval({ tier: 2 }), disclosure)).toBe(false);
    }
  });
});

describe("§8 — what a failed disclosure read becomes", () => {
  it("carries a 403's detail VERBATIM — the gate's refusal, never a console-authored one", () => {
    const detail = "payload withheld: not a named approver for this request";
    expect(disclosureFromError(new ApiError("boom", 403, detail))).toEqual({
      kind: "refused",
      detail,
    });
  });

  it("refuses with NO sentence when the gate sent none — it never borrows the client's", () => {
    // The client-composed string carries the request path and §8 permits no
    // endpoint path on this screen. It is not reachable here in any case: the
    // seam keeps it on `developerMessage`, and this refusal reads `detail`.
    expect(disclosureFromError(new ApiError("request to /v1/x failed with HTTP 403", 403))).toEqual(
      { kind: "refused", detail: null },
    );
  });

  it("treats every other failure as a failed read, not as a verdict", () => {
    expect(disclosureFromError(new ApiError("nope", 404, "approval request not found"))).toEqual({
      kind: "error",
    });
    expect(disclosureFromError(new ApiError("nope", 500))).toEqual({ kind: "error" });
    expect(disclosureFromError(new TypeError("network down"))).toEqual({ kind: "error" });
  });
});

describe("§8 step 3 — unconfirmed facts", () => {
  it("renders nothing when the field is absent from the wire", () => {
    expect(unconfirmedFacts(approval())).toBeNull();
  });

  it("renders nothing at zero — the block's job is to warn, and there is nothing to warn about", () => {
    expect(unconfirmedFacts(approval({ provisional_source_count: 0 }))).toBeNull();
  });

  it("carries the count and its refs when the wire reports some", () => {
    expect(
      unconfirmedFacts(
        approval({
          provisional_source_count: 2,
          provisional_source_refs: ["llm:0123456789abcdef:doc-alpha#chunk-1"],
        }),
      ),
    ).toEqual({ count: 2, refs: ["llm:0123456789abcdef:doc-alpha#chunk-1"] });
  });

  it("shows the human-readable portion of a ref and links it to its proposal", () => {
    const ref = "llm:0123456789abcdef:doc-alpha#chunk-1";
    expect(refLabel(ref)).toBe("doc-alpha#chunk-1");
    expect(refProposalHref(ref)).toBe("/improvement?tab=queue&proposal=0123456789abcdef");
  });

  it("leaves an unparseable ref whole, and offers no link it cannot resolve", () => {
    expect(refLabel("not-a-parseable-ref")).toBe("not-a-parseable-ref");
    expect(refProposalHref("not-a-parseable-ref")).toBeNull();
  });
});

describe("§8 — the copied receipt", () => {
  const result: ApprovalActionResult = {
    status: "approved",
    approver: "bob@example.com",
    decided_at: "2026-07-27T12:04:22Z",
  };

  it("is the six named lines, in order", () => {
    const lines = buildReceipt(approval({ tier: 3 }), result).split("\n");
    expect(lines).toHaveLength(6);
    expect(lines[0]).toBe("Request id: req-t3-wire");
    expect(lines[1]).toBe("Action: payments.wire_transfer");
    expect(lines[2]).toBe("Tier: 3");
    expect(lines[3]).toBe("Verdict: APPROVED");
    expect(lines[4]).toBe("Approver: bob@example.com");
    expect(lines[5]).toMatch(/^Decided at: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });

  it("marks a field the server did not return as absent — never as a guess", () => {
    const lines = buildReceipt(approval(), { status: "rejected" }).split("\n");
    expect(lines[3]).toBe("Verdict: DENIED");
    expect(lines[4]).toBe("Approver: —");
    expect(lines[5]).toBe("Decided at: —");
  });

  it("speaks the same verdict vocabulary the pill does", () => {
    expect(verdictWord("approved")).toBe("APPROVED");
    expect(verdictWord("rejected")).toBe("DENIED");
    expect(verdictWord("pending_second")).toBe("AWAITING SECOND");
    expect(verdictWord("expired")).toBe("EXPIRED");
  });
});

// ===========================================================================
// 015A0-ENFORCEMENT — the gate's disclosure status as a pane state
// ===========================================================================

describe("the source disclosure status", () => {
  const PROTECTED = {
    action: "payments.wire_transfer",
    tier: 3,
    principal_id: "agent://finance-ops",
    target_resource: "account:ops-payables",
    description: "Wire transfer to a named counterparty.",
    material_parameters: { amount: "250,000.00" },
    reversibility: "irreversible",
  };

  it("reads a body with no stated status as the payload it served", () => {
    const state = disclosureFromPayload({ request_id: "req-1", ...PROTECTED });
    expect(state).toEqual({
      kind: "revealed",
      payload: { request_id: "req-1", ...PROTECTED },
    });
  });

  it("reads an available status as the payload it served", () => {
    const state = disclosureFromPayload({
      request_id: "req-1",
      disclosure_status: "available",
      ...PROTECTED,
    });
    expect(state.kind).toBe("revealed");
  });

  it("reads content_withheld as the withheld state and keeps no payload on it", () => {
    expect(
      disclosureFromPayload({ request_id: "req-1", disclosure_status: "content_withheld" }),
    ).toEqual({ kind: "withheld" });
  });

  it("keeps retryable_unknown distinct from withheld rather than collapsing the pair", () => {
    expect(
      disclosureFromPayload({ request_id: "req-1", disclosure_status: "retryable_unknown" }),
    ).toEqual({ kind: "retryable_unknown" });
  });

  it("describes nothing in either withheld state, so no consequence reads a payload", () => {
    expect(describes({ kind: "withheld" })).toBe(false);
    expect(describes({ kind: "retryable_unknown" })).toBe(false);
  });

  it("withholds every decision control in both states, at every tier", () => {
    for (const tier of [1, 2, 3]) {
      const request = approval({ tier });
      expect(sourceDetailWithheld({ kind: "withheld" })).toBe(true);
      expect(sourceDetailWithheld({ kind: "retryable_unknown" })).toBe(true);
      // The Tier-3 rule is a different rule and still holds on its own.
      expect(approveWithheld(request, { kind: "withheld" })).toBe(tier >= 3);
    }
  });

  it("withholds no control for the states that are not a source decision", () => {
    expect(sourceDetailWithheld(IDLE)).toBe(false);
    expect(sourceDetailWithheld({ kind: "loading" })).toBe(false);
    expect(sourceDetailWithheld({ kind: "error" })).toBe(false);
    // A 403 is a verdict about THIS approver, not about the source. It keeps
    // the existing refusal rendering and Deny stays available.
    expect(sourceDetailWithheld({ kind: "refused", detail: null })).toBe(false);
  });
});
