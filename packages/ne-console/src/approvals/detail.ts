// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §8 STEP 2 — "WHAT HAPPENS IF APPROVED", AND THE ONE SEAM IT ARRIVES THROUGH.
//
// THE QUEUE READ IS PAYLOAD-FREE, PERMANENTLY. It serves exactly: request_id,
// action, tier, principal_id, requested_at, sla_deadline, expires_at, escalated.
// Target resource, material parameters and reversibility are not on it and are
// not coming to it — an approver may see that an entry EXISTS without being
// able to read it. The payload arrives instead through a separate, per-request,
// named-approver-only governed read whose EVERY CALL IS AUDITED, which is why
// nothing on this screen may fetch it before an operator asks for it.
//
// THE SEAM. `Disclosure` is the ONE value the whole payload story is told in,
// and `describes()` is the ONE predicate every consequence reads: the pane's
// payload block, the Tier-3 withhold rule and the pane's disabled-reason all
// ask it, so a request whose payload has been revealed flips all three at once
// and nothing else on the screen changes.
//
// THE DISCLOSURE IS PER REQUEST AND NEVER TRAVELS. A `Disclosure` is meaningless
// without the request id it was read for; the binding is enforced in
// `useDisclosure`, which returns IDLE for any id other than the one its state
// was read against. Nothing here caches, merges or defaults a payload.
//
// NOTHING HERE DEFAULTS. Absent is absent: there is no code path in this file
// that can turn a missing field into a value, a preview, or a zero.

import { ApiError } from "../api/client";
import type {
  ApprovalPayloadDisclosureView,
  ApprovalPayloadView,
  PendingApprovalView,
} from "../api/types";
import { parseExtractedSourceRef } from "../primitives/assurance";

/**
 * The payload disclosure for ONE request, as far as this pane has got with it.
 *
 * `refused` is the server's 403 — a role-holder who is not a named approver for
 * THIS request. `detail` is the server's own sentence, carried verbatim so the
 * pane can render it; null when the server sent none (never a client-invented
 * one). The console holds no disclosure logic of its own: it never decides who
 * may read, it renders what the read returned or what it refused.
 */
export type Disclosure =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "revealed"; payload: ApprovalPayloadView }
  | { kind: "refused"; detail: string | null }
  | { kind: "withheld" }
  | { kind: "retryable_unknown" }
  | { kind: "error" };

/**
 * 015A0-ENFORCEMENT: the gate's own disclosure status, read as a state.
 *
 * `content_withheld` is the permanent decision that this request's source
 * authority is no longer current, and `retryable_unknown` is an unresolved or
 * drifted one. Both are SAFE SHELLS: they carry no target, no parameter and no
 * source detail, and neither may carry a decision control.
 *
 * They are NOT refusals. A refusal is the 403 answer about this approver's
 * standing; these two are answers about the SOURCE, and rendering one as the
 * other would tell an approver their access was revoked when it was not.
 *
 * The two are also not each other: `content_withheld` is settled and
 * `retryable_unknown` may become determinate later, so the pane says which
 * one the server sent and never collapses the pair.
 */
export function disclosureFromPayload(body: ApprovalPayloadDisclosureView): Disclosure {
  if (body.disclosure_status === "content_withheld") return { kind: "withheld" };
  if (body.disclosure_status === "retryable_unknown") return { kind: "retryable_unknown" };
  return { kind: "revealed", payload: disclosedPayload(body) };
}

/**
 * The gate's status, and ONLY the gate's status, decides which branch of the
 * union a body is. This narrows on the two withheld statuses rather than on
 * which fields arrived: field presence is what the console must never read a
 * disclosure decision out of, and a body the server called available is
 * available even when it carries little.
 */
function disclosedPayload(body: ApprovalPayloadDisclosureView): ApprovalPayloadView {
  return body as ApprovalPayloadView;
}

/** Nothing asked for, nothing fetched. The state every request starts in. */
export const IDLE: Disclosure = { kind: "idle" };

/**
 * What a failed disclosure read becomes.
 *
 * A 403 is the gate REFUSING this approver, which is a verdict and is rendered
 * as one. Anything else is a failed read, which gets §2.4's error state and a
 * retry. Neither `ApiError.message` nor `userMessage` is used as the refusal
 * text: the gate's OWN sentence or nothing. A house fallback here would read as
 * the gate's stated reason when the gate stated none, and §8 permits no
 * endpoint path on this screen either way.
 */
export function disclosureFromError(err: unknown): Disclosure {
  if (err instanceof ApiError && err.status === 403) {
    const detail = err.detail?.trim();
    return { kind: "refused", detail: detail ? detail : null };
  }
  return { kind: "error" };
}

/** One labelled row of §8 step 2, in the order step 2 lists them. */
export interface DisclosedField {
  label: string;
  value: string;
  mono: boolean;
}

/** A served string, or null when the server sent nothing to show. An empty
 * string is nothing to show: a label above a blank value states a fact the
 * response does not carry. */
function present(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" ? null : value;
}

/**
 * §8 step 2's rows: Target, then one row per material parameter key with the
 * value EXACTLY as sent, then Reversibility.
 *
 * `description` rides with Target because it is the gate's own sentence about
 * what the action does, served on the same read; withholding served text from
 * the person being asked to authorise it is the failure this disclosure exists
 * to end.
 *
 * A field the response does not carry produces no row — no placeholder, no
 * "not reported", no zero. `material_parameters: null` (a row that predates
 * payload recording) produces no parameter rows and no sentence about why.
 */
export function disclosedFields(payload: ApprovalPayloadView): DisclosedField[] {
  const fields: DisclosedField[] = [];
  const target = present(payload.target_resource);
  if (target !== null) fields.push({ label: "Target", value: target, mono: true });
  const description = present(payload.description);
  if (description !== null) fields.push({ label: "Description", value: description, mono: false });
  for (const [key, value] of Object.entries(payload.material_parameters ?? {})) {
    fields.push({ label: key, value: parameterText(value), mono: true });
  }
  const reversibility = present(payload.reversibility);
  if (reversibility !== null)
    fields.push({ label: "Reversibility", value: reversibility, mono: false });
  return fields;
}

/**
 * THE predicate: does the screen have something to show this approver about what
 * approving would do? True only when the governed read returned a payload that
 * renders at least one row — a 200 carrying nothing describes nothing.
 */
export function describes(disclosure: Disclosure): boolean {
  return disclosure.kind === "revealed" && disclosedFields(disclosure.payload).length > 0;
}

/**
 * §8's Tier-3 withhold rule, keyed on whether the payload has actually been
 * revealed FOR THIS REQUEST. While it has not, a Tier-3 request offers no
 * Approve — the console will not invite a decision it cannot inform. Deny stays
 * available: denying what you cannot see is a defensible verdict; approving it
 * is not.
 */
export function approveWithheld(approval: PendingApprovalView, disclosure: Disclosure): boolean {
  return approval.tier >= 3 && !describes(disclosure);
}

/**
 * 015A0-ENFORCEMENT: is this request's source-derived detail gone?
 *
 * True for exactly the two server-authored shells. It is the ONE predicate the
 * pane reads for every consequence of drift — no payload rows, no decision
 * control at ANY tier, no copy — so a request cannot be actionable on one
 * screen element and withheld on another.
 *
 * Tier does not enter into it. `approveWithheld` withholds Approve from an
 * uninformed Tier-3 decision; this withholds every control from a request
 * whose source authority the server says it can no longer resolve, including
 * Deny, which would otherwise decide a request whose target nobody can see.
 */
export function sourceDetailWithheld(disclosure: Disclosure): boolean {
  return disclosure.kind === "withheld" || disclosure.kind === "retryable_unknown";
}

/** A parameter value, exactly as sent. Strings pass through untouched; anything
 * else is serialised. No currency, no unit, no rounding, no interpretation. */
export function parameterText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

export interface UnconfirmedFacts {
  count: number;
  refs: string[];
}

/**
 * §8 step 3 — rendered ONLY when `provisional_source_count` is present and > 0.
 *
 * Absent from the wire returns null and the block renders NOTHING: no "not
 * reported" line, no zero. A count of 0 is a real answer and also renders
 * nothing, because the block's whole job is to warn.
 */
export function unconfirmedFacts(approval: PendingApprovalView): UnconfirmedFacts | null {
  const count = approval.provisional_source_count;
  if (count === undefined || count <= 0) return null;
  return { count, refs: approval.provisional_source_refs ?? [] };
}

/** The human-readable portion of an evidence ref. The full opaque ref rides in
 * `title`; an unparseable ref renders whole, rather than being guessed at. */
export function refLabel(ref: string): string {
  return parseExtractedSourceRef(ref)?.evidenceRef ?? ref;
}

/** The Improvement proposal this ref belongs to, when the ref carries one. */
export function refProposalHref(ref: string): string | null {
  const parsed = parseExtractedSourceRef(ref);
  return parsed === null ? null : `/improvement?tab=queue&proposal=${parsed.sha16}`;
}
