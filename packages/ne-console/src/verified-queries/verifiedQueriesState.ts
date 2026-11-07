// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { ApiError } from "../api/client";
import type { VerifiedTemplateWire } from "../api/types";

const CLIENT_IDS = new WeakMap<object, number>();
let nextClientId = 1;

export function verifiedQueriesOwnerId(client: object, principalId: string): string {
  let clientId = CLIENT_IDS.get(client);
  if (clientId === undefined) {
    clientId = nextClientId;
    nextClientId += 1;
    CLIENT_IDS.set(client, clientId);
  }
  return `${clientId}:${principalId}`;
}

export interface OwnedRequestId {
  readonly owner: string;
  readonly sequence: number;
}

export function ownsRequest(active: OwnedRequestId | null, candidate: OwnedRequestId): boolean {
  return active?.owner === candidate.owner && active.sequence === candidate.sequence;
}

/** The catalog's short human label, derived from the template id — the served
 * `title` is the template's full DESCRIPTION (the registry has no separate title
 * field, NE-135) and grew to paragraph length, which made the catalog column
 * unreadable. The id is the stable, server-stated name of the query; rendering
 * it as words ("disaster-declarations" → "Disaster declarations") states nothing
 * the server did not. A token like "t3" renders uppercased ("T3 record"). */
export function templateShortLabel(templateId: string): string {
  const words = templateId
    .split("-")
    .filter((word) => word !== "")
    .map((word) => (/^[a-z][0-9]+$/.test(word) ? word.toUpperCase() : word));
  const [first, ...rest] = words;
  if (first === undefined) return templateId;
  const lead =
    first === first.toUpperCase() ? first : first.charAt(0).toUpperCase() + first.slice(1);
  return [lead, ...rest].join(" ");
}

export function catalogMatchesHumanDescription(
  template: VerifiedTemplateWire,
  query: string,
): boolean {
  const needle = query.trim().toLocaleLowerCase("en");
  return (
    needle === "" ||
    template.title.toLocaleLowerCase("en").includes(needle) ||
    templateShortLabel(template.template_id).toLocaleLowerCase("en").includes(needle)
  );
}

export interface VerifiedInvokeRequestSnapshot {
  readonly template_id: string;
  readonly version: number;
  readonly args: Readonly<Record<string, unknown>>;
}

export function snapshotInvokeRequest(
  template: Pick<VerifiedTemplateWire, "template_id" | "version">,
  args: Record<string, unknown>,
): VerifiedInvokeRequestSnapshot {
  return Object.freeze({
    template_id: template.template_id,
    version: template.version,
    args: Object.freeze({ ...args }),
  });
}

export type VerifiedFailure =
  | { kind: "refused"; detail: string | null }
  | { kind: "invalid"; detail: string | null }
  | { kind: "error"; detail: string | null };

export function classifyVerifiedFailure(error: unknown): VerifiedFailure {
  const detail = error instanceof ApiError ? (error.detail ?? null) : null;
  if (error instanceof ApiError && error.status === 403) return { kind: "refused", detail };
  if (
    error instanceof ApiError &&
    error.status === 422 &&
    (error.detail === "invalid-args" || error.detail === "guardrail-violation")
  ) {
    return { kind: "invalid", detail };
  }
  return { kind: "error", detail };
}
