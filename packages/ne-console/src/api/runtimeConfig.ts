// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-CONSOLE-ISSUER: the SPA reads its OIDC config (issuer/client_id/audience/
// api_base) at RUNTIME from the serving deployment's `GET /config.json` (served
// by ne-api from the SAME IdentitySettings the backend verifier trusts) instead
// of a Vite-build-time constant. This REPLACES the baked issuer — fail-closed,
// there is NO fallback issuer, ever: a failed/invalid fetch throws so the
// bootstrap shows an error rather than logging in against a wrong issuer.
//
// This is the ONLY new network call in the unit and it lives under src/api/ per
// the thin-client structural guardian (rule a).
//
// ITS FAILURE IS THE FIRST THING A VIEWER CAN SEE, so it obeys the same split
// as every other transport error here. This module used to throw a plain
// `Error` whose only string was the client's own — a status code and the word
// HTTP — and AppBootstrap renders that message full-viewport, above a Retry
// button, before anything else in the console exists. Being inside src/api/ was
// the only reason the transport-provenance guardian did not see it; the seam is
// where the sentence is WRITTEN, not a place raw strings may be thrown from.
//
// AND A RAW STRING NEEDS NO `throw` TO GET OUT. The first repair wrapped every
// string this module COMPOSED and left two `await`s bare — the `fetch` and the
// body parse — so the two failures nobody writes a string for walked straight
// past it to that same full-viewport slot: a rejected fetch rendered the
// engine's `Failed to fetch`, and a 200 carrying an HTML error page (an edge
// rewrite, an SPA fallback swallowing /config.json) rendered V8's `Unexpected
// token '<', "<!doctype "... is not valid JSON`. Neither is a sentence, and
// neither was a construction site a guardian could match, because there was no
// constructor. EVERY await here is guarded now, and what escapes this function
// is a RuntimeConfigError or nothing — which is also what makes the status-`<100`
// arm of the house vocabulary reachable at last: nothing used to construct a
// transport error for a fetch that never got a response.

import { transportUserMessage } from "./userMessage";

/**
 * Thrown on any failed or unusable read of the deployment's runtime config —
 * including the two that arrive as somebody else's exception: a rejected fetch
 * (no response at all, status 0) and a body that will not parse.
 *
 * Same split, same seam, same reason as ApiError and SseError.
 * `developerMessage` keeps the client-composed string with the status in it;
 * `message` — and its named twin `userMessage` — is the sentence the bootstrap
 * page shows.
 *
 * `sentence` overrides the status-class sentence for the failures that are not
 * about the status at all: a 200 carrying an unusable body is not "the server
 * is unavailable". Nothing here is ever the server's own words — `/config.json`
 * serves no `detail` field — and the page that renders it attributes it to
 * nobody: it is the console's own error screen, in the console's own voice.
 */
export class RuntimeConfigError extends Error {
  readonly status: number;
  /** The reader's sentence. ALWAYS populated. Render this. */
  readonly userMessage: string;
  /** The client-composed string. Console and telemetry only. */
  readonly developerMessage: string;
  constructor(developerMessage: string, status: number, sentence?: string) {
    super(transportUserMessage(status, sentence));
    this.name = "RuntimeConfigError";
    this.status = status;
    this.developerMessage = developerMessage;
    this.userMessage = this.message;
  }
}

/** The house sentence for a config the deployment served but the console
 * cannot use. Not a transport failure — the fetch succeeded — so it does not
 * borrow a status-class sentence that would name the wrong cause. */
const CONFIG_UNUSABLE = "The deployment’s configuration isn’t usable.";

/** What the ENGINE said, for the developer slot only. A rejected fetch and a
 * failed parse are the runtime's exceptions, not the console's, and their
 * strings ("Failed to fetch", "Unexpected token '<'…") are the ones this
 * module exists to keep off the bootstrap page. They are still worth keeping:
 * telemetry and the console want to know which one happened. */
function engineReason(error: unknown): string {
  return error instanceof Error ? `${error.name}: ${error.message}` : String(error);
}

export interface ConsoleRuntimeConfig {
  issuer: string;
  client_id: string;
  audience: string;
  api_base: string;
  /** OPTIONAL deployment name for the rail's placard (§1.6). Not required by
   * validation — its absence renders the honest "UNNAMED DEPLOYMENT" placard. */
  deployment_name?: string;
  /**
   * OPTIONAL client-side give-up time for POST /v1/brain/search, in
   * milliseconds.
   *
   * ROUND-3 BLOCKER: the deadline was a compile-time constant of 75s, calibrated
   * below the backend's real latency on the deployment the console is demoed
   * against — a reviewer measured one success at 71s and then four consecutive
   * runs of the same query that blew the deadline. The one loop that works
   * became a coin flip because a NUMBER IN THE BUNDLE disagreed with the
   * DEPLOYMENT it was pointed at. A deployment-specific latency belongs in
   * deployment configuration, and this is the file the deployment already
   * serves. Absent, the console uses its own default.
   */
  search_deadline_ms?: number;
}

const REQUIRED_KEYS: ReadonlyArray<keyof ConsoleRuntimeConfig> = [
  "issuer",
  "client_id",
  "audience",
  "api_base",
];

/** Fetch + validate the deployment's runtime OIDC config. Throws (fail-closed)
 * on any HTTP error, non-object body, or a missing/empty non-string field. Extra
 * keys are tolerated (forward-compatible). */
export async function fetchRuntimeConfig(): Promise<ConsoleRuntimeConfig> {
  let res: Response;
  try {
    res = await fetch("/config.json", { headers: { Accept: "application/json" } });
  } catch (error: unknown) {
    // NO HTTP RESPONSE AT ALL — a dropped network, a TLS failure, a DNS miss.
    // The engine's own words are a developer fact and stay on the developer
    // slot; status 0 is what the house vocabulary keys "The server couldn't be
    // reached." on, and this throw is the only thing that ever reaches it.
    throw new RuntimeConfigError(`runtime config fetch rejected: ${engineReason(error)}`, 0);
  }
  if (!res.ok) {
    throw new RuntimeConfigError(`runtime config fetch failed: HTTP ${res.status}`, res.status);
  }
  let raw: unknown;
  try {
    raw = await res.json();
  } catch (error: unknown) {
    // THE DEPLOYMENT ANSWERED WITH SOMETHING THAT IS NOT ITS CONFIG — an edge
    // or proxy error page, an SPA fallback serving index.html for a missing
    // route. The server is not unavailable; its configuration is unusable, so
    // this borrows the unusable-body sentence and not the transport one.
    throw new RuntimeConfigError(
      `runtime config unreadable: HTTP ${res.status} body did not parse: ${engineReason(error)}`,
      res.status,
      CONFIG_UNUSABLE,
    );
  }
  if (typeof raw !== "object" || raw === null) {
    throw new RuntimeConfigError(
      "runtime config invalid: expected a JSON object",
      res.status,
      CONFIG_UNUSABLE,
    );
  }
  const obj = raw as Record<string, unknown>;
  for (const key of REQUIRED_KEYS) {
    const value = obj[key];
    if (typeof value !== "string" || value.length === 0) {
      throw new RuntimeConfigError(
        `runtime config invalid: '${key}' must be a non-empty string`,
        res.status,
        CONFIG_UNUSABLE,
      );
    }
  }
  const config: ConsoleRuntimeConfig = {
    issuer: obj.issuer as string,
    client_id: obj.client_id as string,
    audience: obj.audience as string,
    api_base: obj.api_base as string,
  };
  // Optional, forward-compatible: only carried through when a non-empty string.
  if (typeof obj.deployment_name === "string" && obj.deployment_name.length > 0) {
    config.deployment_name = obj.deployment_name;
  }
  // Optional, and only when it is a usable positive number — a malformed value
  // falls back to the console's default rather than arming a zero-length or NaN
  // deadline, which would abort every query instantly.
  if (typeof obj.search_deadline_ms === "number" && Number.isFinite(obj.search_deadline_ms)) {
    if (obj.search_deadline_ms > 0) config.search_deadline_ms = obj.search_deadline_ms;
  }
  return config;
}
