// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §15 ACCESS — the served shapes, and the pure rules over them.
//
// The screen answers three questions the console could not answer anywhere:
// who can sign in, what does each role open, and who can approve a Tier-3
// action. All three are reads this deployment's API does not expose yet (§18
// B6), so the shapes are declared HERE, from the fields §15 names, and the
// screen is built against them. Nothing in this file can invent one: every
// value below arrives from a read, and no function here derives a surface
// grant, a member count or an approver.
//
// THIN CLIENT (C7). `member_count` is the SERVER's count and is rendered as
// served — never `members.length`. The member list a key may read can be
// trimmed for that key while the count is not, and a console that counted the
// list it was shown would quietly disagree with the deployment about how many
// people hold a role.

import { compareValues } from "../primitives/tableSort";

// ===========================================================================
// The B6 shapes, typed from the fields §15 names
// ===========================================================================

/** A governed change to this object parked at the approval gate (§2.5). */
export interface PendingAccessChange {
  request_id: string;
  requested_by: string;
}

/** Why a principal opens a surface: the group that carries the grant (§15,
 * "per-surface grant provenance (via group {g})"). */
export interface AccessGrant {
  surface: string;
  via_group: string;
}

/** One principal, as the server computes it from the same role tuples the
 * routes enforce. `surfaces` is the server's answer, not a mapping this console
 * holds — the console has no copy of the role→surface table and cannot. */
export interface AccessPrincipalEntry {
  principal_id: string;
  groups: string[];
  surfaces: string[];
  grants: AccessGrant[];
  pending_change: PendingAccessChange | null;
}

export interface AccessPrincipalsView {
  principals: AccessPrincipalEntry[];
}

/** One role (group): what it opens, how many hold it, and — for the row pane —
 * the member list this key is allowed to see. */
export interface AccessRoleEntry {
  group: string;
  surfaces: string[];
  member_count: number;
  members: string[];
}

export interface AccessRolesView {
  roles: AccessRoleEntry[];
}

/** One name on the list the Tier-3 gate enforces. */
export interface Tier3ApproverEntry {
  principal_id: string;
  since: string | null;
}

export interface Tier3ApproversView {
  approvers: Tier3ApproverEntry[];
  pending_change: PendingAccessChange | null;
}

/**
 * The two governed writes §15 names, as one submission shape.
 *
 * Both go to B6's single governed assignment write; the console never edits the
 * identity provider and holds no second write path. Membership is expressed as
 * the sets to add and remove — never as "the new list", which would silently
 * revert a change someone else made between this read and this submit.
 */
export type AccessChange =
  | {
      kind: "roles";
      principal_id: string;
      add_groups: string[];
      remove_groups: string[];
    }
  | {
      kind: "tier3-approvers";
      add_principals: string[];
      remove_principals: string[];
    };

/**
 * What the server answers a governed write with (§2.5 step 2): applied, or
 * parked at the gate with the request id the pending marker links to. The third
 * answer — denied by policy — arrives as a 403 and is an ApiError, never a
 * result object, so a denial can never be mistaken for a quiet success.
 */
export interface GovernedWriteResult {
  status: "applied" | "pending";
  request_id: string | null;
}

// ===========================================================================
// Counts — the toolbar's live count, agreeing with itself
// ===========================================================================

export function peopleCount(n: number): string {
  return `${n} ${n === 1 ? "person" : "people"}`;
}

export function roleCount(n: number): string {
  return `${n} ${n === 1 ? "role" : "roles"}`;
}

export function approverCount(n: number): string {
  return `${n} ${n === 1 ? "approver" : "approvers"}`;
}

// ===========================================================================
// The surface filter (§15: "surface filter (Approvals / Improvement / Ops)")
// ===========================================================================

export const SURFACE_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "all", label: "All surfaces" },
  { value: "Approvals", label: "Approvals" },
  { value: "Improvement", label: "Improvement" },
  { value: "Ops", label: "Ops" },
];

/** Narrows to the principals the SERVER says open this surface. The comparison
 * is against the served strings; nothing here decides that a group implies a
 * surface. */
export function applySurfaceFilter(
  rows: readonly AccessPrincipalEntry[],
  surface: string,
): readonly AccessPrincipalEntry[] {
  if (surface === "all") return rows;
  return rows.filter((row) => row.surfaces.includes(surface));
}

// ===========================================================================
// Cells
// ===========================================================================

/** §15's "chip list, +n overflow". Returns the chips to draw and the names the
 * `+n` stands for, so the overflow carries the full list in its title rather
 * than hiding it. */
export function chipOverflow(
  values: readonly string[],
  limit: number,
): { shown: readonly string[]; rest: readonly string[] } {
  if (values.length <= limit) return { shown: values, rest: [] };
  return { shown: values.slice(0, limit), rest: values.slice(limit) };
}

/** The sort key for a chip-list column: the served list, joined. An EMPTY list
 * sorts as absent (null → last in both directions) because "no groups" is not
 * the alphabetically-first group. */
export function listSortValue(values: readonly string[]): string | null {
  return values.length === 0 ? null : values.join(" ");
}

/** The provenance lines §15's row pane shows: one per surface, in the order the
 * server sent, naming the group that carries the grant. */
export function grantLines(entry: AccessPrincipalEntry): ReadonlyArray<{
  surface: string;
  via: string;
}> {
  return entry.grants.map((grant) => ({
    surface: grant.surface,
    via: `via group ${grant.via_group}`,
  }));
}

/** Every role name the server sent, A–Z — the checklist the "Change roles…"
 * editor offers. It is derived from the ROLES READ, so the editor can only ever
 * name a group this deployment actually has. */
export function roleNames(roles: readonly AccessRoleEntry[]): readonly string[] {
  return [...roles.map((role) => role.group)].sort((a, b) => compareValues(a, b));
}

/** The add/remove sets a checklist submits: what changed, never the whole list. */
export function membershipDelta(
  before: readonly string[],
  after: readonly string[],
): { add: string[]; remove: string[] } {
  const had = new Set(before);
  const has = new Set(after);
  return {
    add: after.filter((value) => !had.has(value)),
    remove: before.filter((value) => !has.has(value)),
  };
}
