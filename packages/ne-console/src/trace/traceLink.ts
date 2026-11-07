// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Shared trace deep-link encoding. Both the Ask link and the trace row use the
// same event anchor, so no caller hand-builds a subtly different destination.

export interface TraceDecisionLink {
  traceId: string;
  eventId?: string;
}

export function traceEventAnchor(eventId: string): string {
  return `trace-event-${eventId}`;
}

export function traceDecisionHref(ref: TraceDecisionLink): string {
  const run = `/trace/${encodeURIComponent(ref.traceId)}`;
  return ref.eventId ? `${run}#${encodeURIComponent(traceEventAnchor(ref.eventId))}` : run;
}
