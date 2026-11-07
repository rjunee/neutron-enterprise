// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ROW-ANCHOR EXTRACTION RULE, held from both sides: an anchor the answer
// really rests on must surface, and an anchor it does not rest on — an
// unassociated record's, a withheld record's, an empty arm's — must never
// surface. The value itself is only ever the SERVED bytes; there is no branch
// in the unit under test that could compute one, and the assertions here pin
// the passthrough by exact string equality.

import { describe, expect, it } from "vitest";
import type { AgentEvidenceEnvelopeWire } from "../api/types";
import { verifiedRowAnchors } from "./rowAnchors";

type Claim = AgentEvidenceEnvelopeWire["claims"][number];
type Evidence = AgentEvidenceEnvelopeWire["evidence"][number];

const ANCHOR_1 = "1a".repeat(32);
const ANCHOR_2 = "2b".repeat(32);

function claim(over: Partial<Claim> = {}): Claim {
  return {
    claim_id: "claim-1",
    claim_verdict: "verified",
    state: "available",
    message: "Claim available",
    display: "One overdue dispute row.",
    evidence_id: "ev-1",
    association_hash: "a".repeat(64),
    verifier_event_id: null,
    verifier_receipt_hash: null,
    ...over,
  };
}

function evidence(over: Partial<Evidence> = {}): Evidence {
  return {
    evidence_id: "ev-1",
    state: "available",
    message: "Verified row",
    assurance: "direct",
    locator: { kind: "verified_t3", row_anchor: ANCHOR_1, receipt_hash: "c".repeat(64) },
    ...over,
  };
}

function envelope(over: Partial<AgentEvidenceEnvelopeWire> = {}): AgentEvidenceEnvelopeWire {
  return {
    schema_version: 1,
    agent_id: "agent-1",
    run_id: "run-1",
    run_status: "completed",
    mapping_state: "complete",
    mapping_message: "Claim mapping complete",
    output: { verdict: "verified", state: "available", message: "", display: "answer" },
    claims: [claim()],
    evidence: [evidence()],
    governance: {
      status: "complete",
      delegated_user_label: null,
      agent_service_identity: null,
      connector_credential_owner_label: null,
      effective_scopes: [],
      policy_environment: null,
      tool_tier: null,
      tool_decision: null,
      unavailable_reasons: [],
    },
    ...over,
  };
}

describe("verifiedRowAnchors — the served identity, and only the served identity", () => {
  it("surfaces the anchor of a claim-associated available verified row, verbatim", () => {
    expect(verifiedRowAnchors(envelope())).toEqual([{ evidenceId: "ev-1", anchor: ANCHOR_1 }]);
  });

  it("keeps the envelope's own order across several rows", () => {
    const wire = envelope({
      claims: [
        claim({ claim_id: "c1", evidence_id: "ev-1" }),
        claim({ claim_id: "c2", evidence_id: "ev-2" }),
      ],
      evidence: [
        evidence({ evidence_id: "ev-1" }),
        evidence({ evidence_id: "ev-2", locator: { kind: "verified_t3", row_anchor: ANCHOR_2 } }),
      ],
    });
    expect(verifiedRowAnchors(wire).map((row) => row.anchor)).toEqual([ANCHOR_1, ANCHOR_2]);
  });

  it("MUTATION `anchor-by-presence`: an unassociated record's anchor never surfaces", () => {
    const wire = envelope({
      claims: [claim({ evidence_id: null, association_hash: null })],
    });
    expect(verifiedRowAnchors(wire)).toEqual([]);
  });

  it("a withheld or locator-unavailable record's anchor never surfaces", () => {
    for (const state of ["withheld", "locator_unavailable"] as const) {
      expect(verifiedRowAnchors(envelope({ evidence: [evidence({ state })] }))).toEqual([]);
    }
  });

  it("the verified_t3_empty arm has no row and yields no anchor", () => {
    const wire = envelope({
      evidence: [
        evidence({ locator: { kind: "verified_t3_empty", receipt_hash: "c".repeat(64) } }),
      ],
    });
    expect(verifiedRowAnchors(wire)).toEqual([]);
  });

  it("an absent or empty row_anchor yields no entry — nothing is derived to fill it", () => {
    for (const locator of [
      null,
      { kind: "verified_t3" as const },
      { kind: "verified_t3" as const, row_anchor: "" },
    ]) {
      expect(verifiedRowAnchors(envelope({ evidence: [evidence({ locator })] }))).toEqual([]);
    }
  });

  it("a chunk-bearing locator that also names an anchor still surfaces the served value", () => {
    // The rule keys on the SERVED field, not on the arm's name: this module
    // never narrows what the server chose to serve.
    const wire = envelope({
      evidence: [evidence({ locator: { chunk_id: "chunk-1", row_anchor: ANCHOR_2 } })],
    });
    expect(verifiedRowAnchors(wire)).toEqual([{ evidenceId: "ev-1", anchor: ANCHOR_2 }]);
  });
});
