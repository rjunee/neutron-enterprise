// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Render the terminal frame's STRUCTURAL provenance. This module does not read
// answer/refusal prose, count citations, or re-run a gate: the server already
// made the decision and these are its typed outcome bits.

import type { GroundedProvenanceWire, StreamEvent } from "../api/types";

export type AnswerProvenanceKind = "verified" | "grounded" | "refused-ungrounded";

export interface TraceDecisionRef {
  traceId: string;
  eventId?: string;
}

export interface AnswerProvenance {
  kind: AnswerProvenanceKind;
  trace?: TraceDecisionRef;
}

type DoneEvent = Extract<StreamEvent, { type: "done" }>;

/**
 * The server contract's exact deterministic-fallback outcome. A failed first
 * draft followed by an adopted, re-checked assist is GROUNDED; only a failed
 * draft with no adopted assist is the withheld/refused state.
 */
export function isUngroundedRefusal(grounded: GroundedProvenanceWire): boolean {
  return grounded.fallback && !grounded.assist_used;
}

/** Fold one authoritative terminal frame into the UI fact it carries. */
export function provenanceOf(event: DoneEvent): AnswerProvenance | undefined {
  const kind: AnswerProvenanceKind | undefined = event.grounded
    ? isUngroundedRefusal(event.grounded)
      ? "refused-ungrounded"
      : "grounded"
    : event.verified
      ? "verified"
      : undefined;
  if (kind === undefined) return undefined;

  const traceId = event.trace_id?.trim();
  const eventId = event.gate_event_id?.trim();
  return {
    kind,
    ...(traceId ? { trace: { traceId, ...(eventId ? { eventId } : {}) } } : {}),
  };
}
