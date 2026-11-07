// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE CITATION RULES, held from both sides.
//
// A governance product's citations are the part a reader is invited to CHECK,
// so the two failures are not symmetric and both are tested here on every rule:
// a source the answer really rested on must appear, and a source it did not
// must never appear — not by proximity, not by ordering, not because the run
// happened to read it.

import { describe, expect, it } from "vitest";
import type { AgentEvidenceEnvelopeWire, Citation } from "../api/types";
import {
  CITATIONS_NONE_ASSOCIATED,
  CITATIONS_NO_PASSAGE,
  CITATIONS_WITHHELD,
  citationsNote,
  citationsVerifiedRowsNote,
  citedChunkIds,
  readRunCitations,
  runCitations,
} from "./runCitations";

type Claim = AgentEvidenceEnvelopeWire["claims"][number];
type Evidence = AgentEvidenceEnvelopeWire["evidence"][number];

function claim(over: Partial<Claim> = {}): Claim {
  return {
    claim_id: "claim-1",
    claim_verdict: "grounded",
    state: "available",
    message: "Claim available",
    display: "Provisional credit is due within 10 business days.",
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
    message: "Evidence available",
    assurance: "direct",
    locator: { chunk_id: "chunk-1", source_system: "sharepoint" },
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
    output: { verdict: "grounded", state: "available", message: "", display: "answer" },
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

function citation(over: Partial<Citation> = {}): Citation {
  return {
    chunk_id: "chunk-1",
    document_id: "doc-1",
    source_system: "sharepoint",
    source_ref: "reg-e/§1005.11",
    ...over,
  };
}

describe("only an ASSOCIATED evidence record may be cited", () => {
  it("cites the record a claim is bound to", () => {
    expect(citedChunkIds(envelope())).toEqual(["chunk-1"]);
  });

  it("MUTATION `cite-by-presence`: an unassociated record is NOT cited", () => {
    // The record is available, it has a chunk, it was produced during this very
    // run — and no claim names it. That is not a citation, and inferring one is
    // the exact failure this rule exists to prevent.
    const wire = envelope({
      claims: [claim({ evidence_id: null, association_hash: null, claim_verdict: "unavailable" })],
    });
    expect(citedChunkIds(wire)).toEqual([]);
  });

  it("MUTATION `cite-by-position`: a claim cites ITS record, never its neighbour", () => {
    const wire = envelope({
      claims: [claim({ claim_id: "c1", evidence_id: "ev-2" })],
      evidence: [
        evidence({ evidence_id: "ev-1", locator: { chunk_id: "chunk-1" } }),
        evidence({ evidence_id: "ev-2", locator: { chunk_id: "chunk-2" } }),
      ],
    });
    expect(citedChunkIds(wire)).toEqual(["chunk-2"]);
  });

  it("withheld and locator-unavailable records are not cited", () => {
    for (const state of ["withheld", "locator_unavailable"] as const) {
      const wire = envelope({ evidence: [evidence({ state })] });
      expect(citedChunkIds(wire)).toEqual([]);
    }
  });

  it("a record with no retrievable passage is not cited", () => {
    // A `verified_t3` row anchor and a graph-only locator are real evidence
    // with nothing a reader can open. A chip pointing at them would go nowhere.
    const wire = envelope({
      evidence: [evidence({ locator: { kind: "verified_t3", row_anchor: "b".repeat(64) } })],
    });
    expect(citedChunkIds(wire)).toEqual([]);
  });

  it("deduplicates two claims resting on the same record", () => {
    const wire = envelope({
      claims: [claim({ claim_id: "c1" }), claim({ claim_id: "c2" })],
    });
    expect(citedChunkIds(wire)).toEqual(["chunk-1"]);
  });
});

describe("no chip whose target will not resolve", () => {
  it("draws the server's own Citation for a cited chunk", () => {
    expect(runCitations(envelope(), [citation()])).toEqual([citation()]);
  });

  it("DROPS a cited chunk the authorized retrieval did not return", () => {
    // The reader's access changed between the run and the read. The list gets
    // shorter and the note SAYS so; nothing is fabricated to fill the gap.
    const wire = envelope({
      claims: [
        claim({ claim_id: "c1", evidence_id: "ev-1" }),
        claim({ claim_id: "c2", evidence_id: "ev-2" }),
      ],
      evidence: [
        evidence({ evidence_id: "ev-1", locator: { chunk_id: "chunk-1" } }),
        evidence({ evidence_id: "ev-2", locator: { chunk_id: "chunk-2" } }),
      ],
    });
    const shown = runCitations(wire, [citation({ chunk_id: "chunk-1" })]);
    expect(shown.map((c) => c.chunk_id)).toEqual(["chunk-1"]);
    expect(citationsNote(wire, shown)).toBe(
      "1 of 2 cited sources are no longer readable with your current access.",
    );
  });

  it("IGNORES a retrieved citation the envelope never cited", () => {
    // `retrieve` is asked only about cited chunks, but a server that returned
    // more must not turn into chips the answer never rested on.
    const shown = runCitations(envelope(), [citation(), citation({ chunk_id: "chunk-9" })]);
    expect(shown.map((c) => c.chunk_id)).toEqual(["chunk-1"]);
  });
});

describe("the absence is STATED, and a refusal is not an absence", () => {
  it("says so when no claim was associated with any evidence", () => {
    const wire = envelope({
      claims: [claim({ evidence_id: null, association_hash: null, claim_verdict: "unavailable" })],
    });
    expect(citationsNote(wire, [])).toBe(CITATIONS_NONE_ASSOCIATED);
  });

  it("MUTATION `verified-rows-are-counted`: all-verified evidence states the count and the kind", () => {
    // Measured on the live box (run de8e9c91): a completed VERIFIED answer
    // whose associated evidence is a `verified_t3` receipt-anchored row.
    // "There is nothing to open" alone read as "this answer rests on nothing"
    // beside a Verified pill.
    const wire = envelope({
      evidence: [
        evidence({ locator: { kind: "verified_t3_empty", receipt_hash: "c".repeat(64) } }),
      ],
    });
    expect(citationsNote(wire, [])).toBe(citationsVerifiedRowsNote(1));
    expect(citationsVerifiedRowsNote(1)).toContain("1 server-verified evidence record");
    expect(citationsVerifiedRowsNote(3)).toContain("3 server-verified evidence records");
  });

  it("keeps the generic sentence when a passage-less locator is not one it can vouch for", () => {
    const wire = envelope({
      evidence: [evidence({ locator: { entity_id: "ent-1" } })],
    });
    expect(citationsNote(wire, [])).toBe(CITATIONS_NO_PASSAGE);
  });

  it('says WITHHELD, not "no passage", when the server declined to re-authorize', () => {
    // Measured on the live box: a completed verified Run whose evidence all
    // came back `state: "withheld"`. Collapsing that into "there was no
    // passage" would tell the reader their answer rested on nothing, and would
    // hide a real access regression behind a shrug.
    const wire = envelope({
      evidence: [evidence({ state: "withheld", message: "Evidence withheld — access changed" })],
    });
    expect(citedChunkIds(wire)).toEqual([]);
    expect(citationsNote(wire, [])).toBe(CITATIONS_WITHHELD);
  });

  it("A REFUSAL CITES NOTHING AND SAYS NOTHING ABOUT IT", () => {
    // The refusal card already states the outcome. A second sentence about
    // missing sources would read as a defect in an outcome that is CORRECT.
    const wire = envelope({
      output: { verdict: "refused", state: "available", message: "", display: "I will not" },
      claims: [],
      evidence: [],
    });
    expect(citedChunkIds(wire)).toEqual([]);
    expect(citationsNote(wire, [])).toBeNull();
  });

  it("stays silent when every cited source is listed", () => {
    expect(citationsNote(envelope(), [citation()])).toBeNull();
  });
});

describe("the knowledge_search locator arm cites every served passage", () => {
  // Measured live (run aaea4860): both verified claims anchored to ONE search
  // read whose locator carries `chunk_ids` (plural) — no `chunk_id` at all —
  // and the answer rendered "SOURCES · 0 … cites no source" while the server
  // held a valid association the whole time. These pin the plural arm.
  const searchEvidence = evidence({
    locator: { kind: "knowledge_search", chunk_ids: ["chunk-1", "chunk-2"] },
  });

  it("cites every chunk of an associated search read, in served order", () => {
    const wire = envelope({ evidence: [searchEvidence] });
    expect(citedChunkIds(wire)).toEqual(["chunk-1", "chunk-2"]);
    const drawn = runCitations(wire, [
      citation(),
      citation({ chunk_id: "chunk-2", source_ref: "reg-e/§1005.11(c)" }),
    ]);
    expect(drawn.map((item) => item.chunk_id)).toEqual(["chunk-1", "chunk-2"]);
    expect(citationsNote(wire, drawn)).toBeNull();
  });

  it("MUTATION `cite-by-presence`: an unassociated search read is NOT cited", () => {
    const wire = envelope({
      claims: [claim({ evidence_id: null, association_hash: null })],
      evidence: [searchEvidence],
    });
    expect(citedChunkIds(wire)).toEqual([]);
    expect(citationsNote(wire, [])).toBe(CITATIONS_NONE_ASSOCIATED);
  });

  it("deduplicates a chunk named by both the singular and plural fields", () => {
    const wire = envelope({
      evidence: [
        evidence({
          locator: { kind: "knowledge_search", chunk_id: "chunk-1", chunk_ids: ["chunk-1"] },
        }),
      ],
    });
    expect(citedChunkIds(wire)).toEqual(["chunk-1"]);
  });

  it('NEVER says "cites no source" while an association exists', () => {
    // The false negative that is worse than showing nothing: the claim IS
    // associated, only its evidence failed to disclose. The note must report
    // the evidence condition, never deny the association.
    const wire = envelope({
      evidence: [
        evidence({
          state: "locator_unavailable",
          message: "Evidence locator unavailable",
          locator: null,
        }),
      ],
    });
    expect(citedChunkIds(wire)).toEqual([]);
    expect(citationsNote(wire, [])).toBe(CITATIONS_NO_PASSAGE);
    expect(citationsNote(wire, [])).not.toBe(CITATIONS_NONE_ASSOCIATED);
  });
});

describe("readRunCitations carries the served row anchors through", () => {
  it("returns the verified rows' anchors beside the citations, verbatim", async () => {
    const anchor = "b".repeat(64);
    const wire = envelope({
      claims: [claim({ evidence_id: "ev-1" })],
      evidence: [evidence({ locator: { kind: "verified_t3", row_anchor: anchor } })],
    });
    const result = await readRunCitations(
      {
        getAgentRunEvidence: async () => wire,
        retrieve: async () => ({ citations: [] }),
      },
      "agent-1",
      "run-1",
    );
    expect(result).not.toBeNull();
    expect(result!.rowAnchors).toEqual([{ evidenceId: "ev-1", anchor }]);
    // The verified-rows sentence and the anchors travel together: the note
    // says what the answer rests on, the anchors are the rows' identities.
    expect(result!.note).toBe(citationsVerifiedRowsNote(1));
  });

  it("returns no anchors for an ordinary chunk-backed answer", async () => {
    const result = await readRunCitations(
      {
        getAgentRunEvidence: async () => envelope(),
        retrieve: async () => ({ citations: [citation()] }),
      },
      "agent-1",
      "run-1",
    );
    expect(result!.rowAnchors).toEqual([]);
    expect(result!.citations.map((c) => c.chunk_id)).toEqual(["chunk-1"]);
  });
});
