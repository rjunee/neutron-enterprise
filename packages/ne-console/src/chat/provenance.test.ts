// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { GroundedProvenanceWire, StreamEvent, VerifiedProvenanceWire } from "../api/types";
import { provenanceOf } from "./provenance";

const VERIFIED: VerifiedProvenanceWire = {
  template_id: "accounts-directory",
  template_version: 1,
  config_hash: "config-abc",
  row_count: 3,
  truncated: false,
};

function grounded(overrides: Partial<GroundedProvenanceWire> = {}): GroundedProvenanceWire {
  return {
    fetches: [VERIFIED],
    citation_count: 2,
    gate: "ne-grounding-gate:1",
    fallback: false,
    assist_used: false,
    prompt_echo_count: 0,
    strictness: "strict",
    ...overrides,
  };
}

function done(overrides: Partial<Extract<StreamEvent, { type: "done" }>> = {}) {
  return {
    type: "done" as const,
    session_id: "session-1",
    output: "Answer",
    ...overrides,
  };
}

describe("terminal-frame answer provenance", () => {
  it("maps only structural verified and grounded outcomes to positive states", () => {
    expect(provenanceOf(done({ verified: VERIFIED }))).toEqual({ kind: "verified" });
    expect(provenanceOf(done({ grounded: grounded() }))).toEqual({ kind: "grounded" });
    // A failed first draft followed by an adopted, re-checked assist is still a
    // grounded FINAL answer, not a refusal.
    expect(
      provenanceOf(done({ grounded: grounded({ fallback: true, assist_used: true }) })),
    ).toEqual({ kind: "grounded" });
  });

  it("maps the gate's deterministic fallback outcome to refused-ungrounded", () => {
    expect(
      provenanceOf(done({ grounded: grounded({ fallback: true, assist_used: false }) })),
    ).toEqual({ kind: "refused-ungrounded" });
  });

  it("gives plain generation no positive provenance chip", () => {
    expect(provenanceOf(done())).toBeUndefined();
  });

  it("prefers the terminal grounding gate when both additive carriers coexist", () => {
    expect(
      provenanceOf(
        done({
          verified: VERIFIED,
          grounded: grounded({ fallback: true, assist_used: false }),
        }),
      ),
    ).toEqual({ kind: "refused-ungrounded" });
  });

  it("carries an exact trace/gate pointer only when the server supplies a trace id", () => {
    expect(
      provenanceOf(
        done({ verified: VERIFIED, trace_id: "trace-123", gate_event_id: "gate-event-9" }),
      ),
    ).toEqual({
      kind: "verified",
      trace: { traceId: "trace-123", eventId: "gate-event-9" },
    });
    expect(
      provenanceOf(done({ verified: VERIFIED, trace_id: "", gate_event_id: "orphan" })),
    ).toEqual({ kind: "verified" });
  });
});
