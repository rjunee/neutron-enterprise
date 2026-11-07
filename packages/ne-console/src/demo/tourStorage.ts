// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// FIRST-RUN DISMISSAL for the guided tour. The tour was, wrongly, the body of
// the landing route; it is now the familiar first-run panel — shown once, killed
// by one action, reachable afterwards from the rail's "Guided tour" control.
//
// This is deliberately a SEPARATE store from demoStorage.ts:
//   - demoStorage holds POSITION (which station, which check-offs) in
//     sessionStorage, because a presenter's place in a walkthrough is a property
//     of the sitting, not of the person.
//   - this holds the DISMISSAL in localStorage, because "I have seen this, stop
//     showing it" must survive closing the tab. A dismissal that expires with
//     the session is not a dismissal.
//
// FAIL-CLOSED (the conservative direction): if localStorage cannot be READ
// (private mode, quota, a hostile embedding), `isTourDismissed()` returns TRUE.
// The failure mode of guessing "not dismissed" is the panel re-opening on every
// single load forever, which is precisely the defect this module exists to
// prevent. A tour that fails to appear is recoverable in one click from the rail;
// a tour that cannot be dismissed is not.

const KEY = "ne-tour:v1";
const DISMISSED = "dismissed";

/**
 * PER PRINCIPAL, not per browser (round-2 minor).
 *
 * The key was browser-global, so one person dismissing the tour hid it from the
 * next person to sign in on the same machine — on a shared demo laptop or a
 * kiosk that is the whole population. Scoping the key to the principal fixes
 * that direction.
 *
 * IT DOES NOT FIX THE OTHER DIRECTION, and the panel's copy no longer pretends
 * it does: the dismissal is still local, so a new machine or a cleared profile
 * shows the tour again. Persisting it against the principal needs a server-side
 * per-user preference store, and this API has none. The panel now says "will not
 * open again in this browser".
 */
function keyFor(principalId: string | null): string {
  return principalId === null ? KEY : `${KEY}:${principalId}`;
}

/** True when the guided tour must NOT auto-open. Any storage failure reads as
 * dismissed — see the fail-closed note above. */
export function isTourDismissed(principalId: string | null = null): boolean {
  try {
    // The unscoped key still counts, so an existing dismissal is not resurrected
    // by this change.
    return (
      localStorage.getItem(keyFor(principalId)) === DISMISSED ||
      localStorage.getItem(KEY) === DISMISSED
    );
  } catch {
    return true;
  }
}

/** Record the dismissal. Swallows any storage error: the panel still closes for
 * this session, it just cannot promise to stay closed. */
export function dismissTour(principalId: string | null = null): void {
  try {
    localStorage.setItem(keyFor(principalId), DISMISSED);
  } catch {
    // Storage unavailable — the in-memory close still happened.
  }
}

/** Clear the dismissal. Exported for tests and for a deliberate "show me the
 * tour on next load" affordance; nothing in the shipped UI calls it today. */
export function resetTourDismissal(principalId: string | null = null): void {
  try {
    localStorage.removeItem(keyFor(principalId));
    localStorage.removeItem(KEY);
  } catch {
    // Storage unavailable — nothing to clear.
  }
}
