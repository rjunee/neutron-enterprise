// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE SILENT-RENEW SEAM. `automaticSilentRenew` is oidc-client-ts machinery, not
// console code: the SilentRenewService renews the token and the UserManager
// emits ONE `userLoaded` carrying the rotation. react-oidc-context turns that
// into a USER_LOADED dispatch and a fresh auth object — with no `isLoading`
// flip and no `activeNavigator`, which is exactly what makes an idle renewal
// invisible to a route guard and visible to everything keyed on auth identity.
//
// Faking at the UserManager boundary (rather than stubbing AuthContext) keeps
// the REAL react-oidc-context state machine in the test, so a renewal test
// exercises the same path production takes. Shared by the renewal tests in
// ../auth so both halves of the pair drive one seam.

import { act } from "react";
import { vi } from "vitest";

export interface FakeUser {
  access_token: string;
  session_state: string | null;
  expired: boolean;
  /** NumericDate seconds, as oidc-client-ts carries it. Optional: a test that
   * does not exercise expiry-sensitive paths states nothing about it. */
  expires_at?: number;
  profile: { sub: string; sid?: string };
}

/** A signed-in user, defaulting to the rotation-invariant half of an identity
 * (same subject, same SSO session) so a test states only what it varies. */
export function fakeUser(overrides: Partial<FakeUser> & { sub: string }): FakeUser {
  const { sub, ...rest } = overrides;
  return {
    access_token: "token-1",
    session_state: "session-1",
    expired: false,
    profile: { sub, sid: "session-1" },
    ...rest,
  };
}

/** A UserManager stand-in whose userLoaded listeners the test fires by hand. */
export function fakeUserManager(initial: FakeUser | null) {
  const listeners: ((user: FakeUser) => void)[] = [];
  const noop = vi.fn();
  return {
    listeners,
    settings: { authority: "https://idp.ne.test/realms/ne-test", client_id: "ne-console" },
    events: {
      addUserLoaded: (handler: (user: FakeUser) => void) => listeners.push(handler),
      removeUserLoaded: noop,
      addUserUnloaded: noop,
      removeUserUnloaded: noop,
      addSilentRenewError: noop,
      removeSilentRenewError: noop,
      addUserSignedOut: noop,
      removeUserSignedOut: noop,
    },
    getUser: vi.fn().mockResolvedValue(initial),
    signinCallback: vi.fn().mockResolvedValue(null),
    signinRedirect: vi.fn().mockResolvedValue(undefined),
    /** On-demand renew (the pre-submit run-credential guard drives this).
     * Default: resolves null (renew unavailable); a test overrides per case. */
    signinSilent: vi.fn().mockResolvedValue(null),
    removeUser: vi.fn().mockResolvedValue(undefined),
  };
}

/** ONE silent renewal, as oidc-client-ts delivers it. */
export function emitUserLoaded(
  manager: ReturnType<typeof fakeUserManager>,
  next: FakeUser | null,
): void {
  act(() => {
    for (const listener of manager.listeners) listener(next as FakeUser);
  });
}
