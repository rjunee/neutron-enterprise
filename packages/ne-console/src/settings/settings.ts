// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §16 SETTINGS — the deployment half, as shapes and pure rules.
//
// §16 keeps two kinds of setting visibly apart: what this PERSON prefers
// (local, instant, already held by rail/theme.tsx and primitives/density.tsx)
// and what this DEPLOYMENT is configured to do (governed, approved). Only the
// second needs anything here.
//
// TODAY the deployment half is read-only, from the configuration the deployment
// already serves. §18 B7 adds a settings read and a governed patch, and the
// shapes below are typed from the two fields it names, so the editable path is
// built rather than planned. Nothing in this file invents a value: an absent
// configured field stays absent all the way to the em dash.

/** A governed change to the deployment settings parked at the gate (§2.5). */
export interface PendingSettingsChange {
  request_id: string;
  requested_by: string;
}

/** §18 B7's read. Extensible by the same rule §16 states: no value is surfaced
 * here until it has a governed write behind it. */
export interface DeploymentSettingsView {
  deployment_name: string | null;
  search_deadline_ms: number | null;
  pending_change: PendingSettingsChange | null;
}

/** The patch §18 B7 accepts. Only the fields the person actually changed are
 * sent — a whole-object write would revert anything altered between this read
 * and this submit. */
export interface SettingsPatch {
  deployment_name?: string;
  search_deadline_ms?: number;
}

/**
 * What the server answers a governed write with (§2.5 step 2): applied, or
 * parked at the gate with the request id the pending marker links to. Denied by
 * policy arrives as a 403, never as a result object, so a denial can never be
 * mistaken for a quiet success.
 */
export interface GovernedWriteResult {
  status: "applied" | "pending";
  request_id: string | null;
}

// ===========================================================================
// The one unit conversion
// ===========================================================================

/**
 * §16 states the limit in SECONDS; the configuration carries it in
 * milliseconds. The conversion lives in exactly one place, in both directions,
 * because the field is read in one and written in the other and a screen that
 * did the arithmetic twice would eventually disagree with itself by a factor of
 * a thousand.
 *
 * An absent or unusable value stays absent — it never becomes a zero.
 */
export function msToSeconds(ms: number | null | undefined): number | null {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms <= 0) return null;
  return ms / 1000;
}

/** The typed seconds, back to the milliseconds the patch carries. Returns null
 * for anything that is not a positive number, so a half-typed field cannot be
 * submitted as a limit of nothing. */
export function secondsToMs(seconds: string): number | null {
  const value = Number(seconds.trim());
  if (seconds.trim() === "" || !Number.isFinite(value) || value <= 0) return null;
  return Math.round(value * 1000);
}

/** The seconds field's starting text: the configured value, or empty. Never a
 * placeholder number standing in for an unset limit. */
export function secondsField(ms: number | null | undefined): string {
  const seconds = msToSeconds(ms);
  return seconds === null ? "" : String(seconds);
}

/** Which of the two fields actually changed. An empty patch is not submitted. */
export function settingsPatch(
  before: { deployment_name: string | null; search_deadline_ms: number | null },
  after: { deployment_name: string; search_deadline_ms: number | null },
): SettingsPatch {
  const patch: SettingsPatch = {};
  const name = after.deployment_name.trim();
  if (name !== "" && name !== (before.deployment_name ?? "")) patch.deployment_name = name;
  if (after.search_deadline_ms !== null && after.search_deadline_ms !== before.search_deadline_ms) {
    patch.search_deadline_ms = after.search_deadline_ms;
  }
  return patch;
}

export function isEmptyPatch(patch: SettingsPatch): boolean {
  return Object.keys(patch).length === 0;
}
