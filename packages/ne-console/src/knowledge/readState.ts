// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// WHAT A FAILED READ ACTUALLY MEANS — one classifier, so no section on this
// screen guesses.
//
// The rule this exists to hold: A ROLE BOUNDARY AND AN OUTAGE ARE DIFFERENT
// FACTS AND MUST NEVER SHARE A MESSAGE. "Your role doesn't include this" is a
// statement about authorization. Rendering it for a read that timed out, 500'd,
// or hit a deployment where the tier is switched off is a false statement about
// authorization — the exact class of fabrication this console exists to not
// commit. So refusal comes from a 403 and from nothing else.
//
// The graph routes answer 404 "graph tier not enabled" when the knowledge graph
// is off in a deployment (and a 503 on the ontology-disabled path); that is a
// deployment fact, not a fault and not a permission, and it gets its own state.

import { ApiError } from "../api/client";

/** What a rejected governed read was. */
export type ReadFailure = "refused" | "off" | "error";

/** True only for a server 403 — the one answer that means "not for this role". */
export function isRefusal(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

/**
 * Classify a failed graph read. 403 is the role boundary; 404/503 is the tier
 * switched off in this deployment; everything else — including a network drop,
 * where there is no status at all — is an error with a Retry.
 */
export function classifyGraphFailure(error: unknown): ReadFailure {
  if (isRefusal(error)) return "refused";
  if (error instanceof ApiError && (error.status === 404 || error.status === 503)) return "off";
  return "error";
}

/** Classify a failed read on a route with no "switched off" answer of its own. */
export function classifyFailure(error: unknown): Exclude<ReadFailure, "off"> {
  return isRefusal(error) ? "refused" : "error";
}

/** The four states a section can be in, carrying its value only when it has one. */
export type ReadState<T> =
  | { phase: "loading" }
  | { phase: "ok"; value: T }
  | { phase: "refused" }
  | { phase: "off" }
  | { phase: "error" };

/** The table status a section's read state maps to. `refused` and `error` are
 * the table's own states; `off` is never a table — the section renders the
 * deployment fact instead. */
export function tableStatus<T>(state: ReadState<T>): "loading" | "ok" | "refused" | "error" {
  switch (state.phase) {
    case "loading":
      return "loading";
    case "refused":
      return "refused";
    case "error":
      return "error";
    default:
      return "ok";
  }
}
