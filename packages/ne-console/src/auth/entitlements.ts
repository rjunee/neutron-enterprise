// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// WHAT THIS KEY MAY OPEN — as answered by the server, never decided here.
//
// Round-2 findings, four reviewers, one root cause: the console asked every
// governed surface on every route for every principal, and the server correctly
// refused about half of them. Three consequences, all of them real:
//   * the reader's home screen was three red 403 receipts and a wall of "the
//     server refused this one";
//   * /audit was ~100 rows of `api.audit.unauthorized / DENY` — the console's own
//     page loads — with the two real governance events buried in them;
//   * refusals are audited by design, so the KPI window (which counts audited
//     events) climbed 0.68 → 0.92 hours-saved while a reviewer merely browsed.
//
// GET /v1/me now carries `entitlements` — the deployment's own list of coarse
// surfaces this principal's groups satisfy, computed from the SAME fail-closed
// role tuples the guarded routes check (ne-api routers/me.py). This module is the
// single place the console consults it.
//
// THE RULE THIS DOES NOT BREAK. Authorization stays on the server: every read
// still goes through a route that runs its own check, and a surface the console
// thinks is open can still come back 403 — which renders exactly as it does
// today. What changes is that the console stops ASKING questions it has been
// told the answer to, and stops walking someone into a page of refusal prose
// when a lock on the nav item would have said it in one glyph.
//
// UNKNOWN IS NOT DENIED — AND PENDING IS NOT PERMITTED. Two different unknowns,
// and collapsing them was a shipped defect (the rail rendered every destination
// while /v1/me was in flight, then snatched the refused ones away when the
// answer landed — the "Approvals flash" the owner saw in his first 30 seconds):
//   * PENDING (`pending: true`) — /v1/me has not RESOLVED yet. An answer is
//     coming; rendering a gated affordance now asserts a permission the server
//     has not granted, which is the same class of error as inventing a denial.
//     Gated affordances render as loading, not as permitted.
//   * UNADVISED (`known: false, pending: false`) — /v1/me resolved without an
//     `entitlements` list (older build), or failed. No verdict is coming that
//     could take anything away, so callers treat `unknown` as "ask the server":
//     everything renders, and the server refuses each request on its own terms.
// In both states `refused()` is false for every surface — the client never
// computes a denial of its own.

import type { PrincipalState } from "./PrincipalContext";

/** The coarse surfaces a key can be refused on, as named by the server. */
export type Surface = "approvals" | "steward" | "ops";

export type Access = "granted" | "refused" | "unknown";

export interface Entitlements {
  /** What the server says about one surface. */
  access(surface: Surface): Access;
  /** True only when the server positively said this key does NOT hold it. */
  refused(surface: Surface): boolean;
  /** True when the server's answer has arrived at all. */
  known: boolean;
  /** True only while the answer is still IN FLIGHT (/v1/me unresolved). Distinct
   * from `known: false, pending: false`, which means the answer ARRIVED carrying
   * no advisory — see the header: pending renders as loading, unadvised renders
   * as open. */
  pending: boolean;
}

/** /v1/me is in flight. An answer is coming; nothing is granted yet. */
const PENDING: Entitlements = {
  access: () => "unknown",
  refused: () => false,
  known: false,
  pending: true,
};

/** /v1/me concluded without an advisory (no list, or the read failed). Nothing
 * will arrive later to trim against, so unknown-is-not-denied applies in full. */
const UNADVISED: Entitlements = {
  access: () => "unknown",
  refused: () => false,
  known: false,
  pending: false,
};

/** Derive the entitlement view from the shared /v1/me state. Pure + exported so
 * the rule is testable without a provider. */
export function entitlementsOf(state: PrincipalState): Entitlements {
  if (state.status === "loading") return PENDING;
  const list = state.principal?.entitlements;
  if (state.status !== "ok" || list === undefined) return UNADVISED;
  const held = new Set(list);
  return {
    access: (surface) => (held.has(surface) ? "granted" : "refused"),
    refused: (surface) => !held.has(surface),
    known: true,
    pending: false,
  };
}

/** Which surface a nav route needs, for the routes that need one. A route absent
 * from this map is open to every authenticated key. */
export const ROUTE_SURFACE: Readonly<Record<string, Surface>> = {
  "/approvals": "approvals",
  "/audit": "ops",
  "/conversation-ledger": "ops",
  "/trace": "ops",
  "/improvement": "steward",
  // The pre-rename paths are still routed (they redirect), and a bookmark or a
  // shared link still arrives on one. They keep their surface so a stale URL is
  // filtered and refused on exactly the same terms as the current one.
  "/flywheel": "steward",
  // Operations audience = platform owner + steward. The destination route ships
  // here (NE-CONSOLE-IA); its own governed API is a separate unit blocked on
  // NE-COST-PRINCIPAL, so the server still owns the real authorization once
  // that lands — this entry only lets the rail hide the link consistently with
  // the rest of Operations in the meantime.
  "/models": "ops",
  // NE-WORKFLOWS: routers/workflows.py guards BOTH the list and the run with
  // `_require_ops`. Without this entry the rail would show an approver or a
  // steward a link whose every read 403s — `routeVisibility` hides only on the
  // server's POSITIVE refusal, and a route absent from this map is treated as
  // open to every authenticated key.
  "/workflows": "ops",
};

/** The one-line reason a locked nav item carries, in the words the surface uses. */
export const SURFACE_REASON: Readonly<Record<Surface, string>> = {
  approvals: "needs an approver key — the server refuses this one",
  steward: "needs a steward key — the server refuses this one",
  ops: "needs an ops key — the server refuses this one",
};
