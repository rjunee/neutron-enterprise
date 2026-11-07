// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 SERVING — the live observation rows, as pure functions over served fields.
//
// THE CLAIM THIS SURFACE MAKES CHECKABLE. The lanes table says a model routes
// to a privately-served lane; that is configuration. This panel renders the
// server's MEASUREMENT of that lane's endpoint: whether it answered just now,
// what model IT says it is serving, and when it last answered. Every value is
// a field of the governed read (C7) — the console computes no reachability,
// never contacts a model endpoint, and shows nothing the server did not
// observe.
//
// THE THREE THINGS THAT ARE NOT EACH OTHER, kept distinct here:
//   * a lane the deployment does not carry has NO row (the server sends none);
//   * a verdict is shown WITH its age (`checked_at` is the observation time —
//     a cached verdict arrives with an older stamp and renders as older);
//   * a lane that is down now but answered in this process shows when; null
//     history makes only a process-lifetime claim. Neither renders as up.

import type { ApiClient } from "../api/client";
import type { LaneServingView, ServingState, ServingStatusView } from "../api/types";
import type { StateWord } from "../primitives/StatePill";
import { relativeTime } from "../primitives/format";

/** The read §18's serving surface adds to the governed client. */
export interface ServingReadApi {
  modelsServing(): Promise<ServingStatusView>;
}

export type ServingReader = () => Promise<ServingStatusView>;

/** The serving read, or null on a deployment whose client cannot serve it —
 * the reads.ts probe pattern: no client method, no section, no dead branch. */
export function servingReader(client: ApiClient): ServingReader | null {
  const read = (client as Partial<ServingReadApi>).modelsServing;
  return typeof read === "function" ? () => read.call(client) : null;
}

/** The pill word for a server verdict and model-match verdict. Only an endpoint
 * that answered with the configured model is SERVING. An ok response that
 * reports another model, no model, or no match verdict is DEGRADED. */
export function pillWord(
  state: ServingState,
  servesConfiguredModel: boolean | null = null,
): StateWord {
  switch (state) {
    case "ok":
      return servesConfiguredModel === true ? "SERVING" : "DEGRADED";
    case "unreachable":
      return "UNREACHABLE";
    case "timeout":
      return "NO ANSWER";
    case "http_error":
    case "malformed":
      return "DEGRADED";
    case "not_probed":
      return "NOT CHECKED";
  }
}

/** `host[:port]` for display — the server already reduced the endpoint to
 * `scheme://host[:port]`; the scheme adds nothing for the operator. Empty
 * stays empty (the row renders the em dash, never an invented address). */
export function endpointHost(endpoint: string): string {
  return endpoint.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
}

/**
 * The one-line availability fact under a non-SERVING row, or null when the
 * pill already says everything (a lane that is up needs no epitaph).
 *
 * `last_ok_at` and its absence are DIFFERENT facts and get different
 * sentences: "down since when" versus "none observed in this process". A
 * cached success is never re-worded into the present — the current verdict is
 * the failure.
 */
export function availabilityLine(lane: LaneServingView, now: number = Date.now()): string | null {
  if (lane.state === "ok") return null;
  if (lane.state === "not_probed") return "The configured address can’t be checked.";
  if (lane.last_ok_at === null) {
    return "No successful answer observed since this service started.";
  }
  return `Last answered ${relativeTime(lane.last_ok_at, now)}.`;
}

/**
 * What the ENDPOINT says it is serving, or null when there is no observation
 * to report (every non-ok state — an unreachable endpoint has no model name,
 * and rendering the configured one would be the label this panel exists to
 * replace).
 */
export function reportedModel(lane: LaneServingView): { text: string; mismatch: boolean } | null {
  if (lane.state !== "ok") return null;
  const reported = lane.served_models.join(", ");
  if (lane.serves_configured_model === false) {
    return {
      text: `Reports ${reported || "no models"} — the policy names ${lane.configured_model}`,
      mismatch: true,
    };
  }
  if (reported === "") return null;
  return { text: reported, mismatch: false };
}
