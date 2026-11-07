// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// CLIENT IDENTITY IS SIGNED-IN IDENTITY — not react-oidc-context object identity.
//
// Those are not the same thing, and conflating them was a defect. `automaticSilentRenew`
// rotates the access token 60s before it expires — measured on the live pilot
// deployment (originally a 300s Keycloak default lifespan, so a NEW auth object
// every 240s; the realm lifespan is now 900s — see ne-pilot-realm.json) —
// and client identity is what the whole console uses to invalidate governed
// reads: PrincipalContext's ownership generation, useGovernedRead's ownershipKey,
// DocumentsWorkspace's fetch context, entityRailOwnerId, verifiedQueriesOwnerId.
// Keying a client on the auth object therefore blanked every loaded list back to
// skeletons every four minutes, on a screen nobody was touching.
//
// That invalidation is NOT the bug and is not weakened here. Read both halves
// together; the pair is the point:
//
//  - A TOKEN ROTATION must NOT produce a new client. It never needed to: the
//    bearer is read at call time, so a retained client always sends the CURRENT
//    token (see LiveSession below).
//  - A REAL IDENTITY CHANGE — a different principal, the same principal signing
//    out and back in, a different IdP/client — MUST produce a new client, because
//    that is precisely the signal every governed read keys on to drop rows that
//    are no longer this principal's.
//
// ./authIdentity draws that line and fails safe: when it cannot tell the two
// apart it returns null and we key on the auth object exactly as before — a
// redundant rebuild. A flash is an acceptable failure; a retained row is not.
//
// Every governed client in the console binds through here, so the boundary is
// stated once rather than re-derived per surface.

import { useLayoutEffect, useMemo, useState } from "react";
import { useAuth } from "react-oidc-context";
import type { AuthContextProps } from "react-oidc-context";
import type { User } from "oidc-client-ts";
import { authIdentity } from "./authIdentity";

/** The credential-facing options every governed client factory takes. */
export interface SessionBoundClientOptions {
  getAccessToken: () => string | undefined;
  onUnauthorized: () => void;
  /**
   * Renew the access token NOW unless it already has at least
   * `minRemainingSeconds` of verified lifetime left. Long-running submissions
   * (an agent Run freezes the bearer's expiry into its authorization snapshot
   * at admission, and the worker fail-closed re-checks that snapshot before
   * every protected disclosure) call this first so a legitimate run never
   * starts on a credential shorter than its own execution window.
   *
   * NEVER throws and NEVER weakens anything: on a failed or unavailable renew
   * the submission proceeds on the current token exactly as before, and the
   * server-side expiry checks still refuse an actually-expired credential.
   */
  ensureFreshAccessToken?: (minRemainingSeconds: number) => Promise<void>;
}

/**
 * The live OIDC session behind a retained client.
 *
 * One mutable cell, adopted in a layout effect, so a client that outlives a
 * token rotation still presents the rotation's token. Every read happens at
 * call time — from a network request or a 401 — never during render.
 */
class LiveSession {
  private auth: AuthContextProps;
  /**
   * A token minted by an on-demand `ensureFreshAccessToken` renew, held until
   * react-oidc-context re-renders and `adopt` catches the auth object up.
   * Without this cell the awaited renew would be invisible to `accessToken()`
   * until the NEXT render — exactly the gap a pre-submit renew must not have.
   * Same-principal only: a renew that comes back as a different subject is
   * discarded (the identity-keyed client rebuild owns that transition).
   */
  private renewed: User | null = null;
  private renewInFlight: Promise<void> | null = null;

  constructor(auth: AuthContextProps) {
    this.auth = auth;
  }

  adopt(auth: AuthContextProps): void {
    this.auth = auth;
  }

  accessToken(): string | undefined {
    const current = this.auth.user ?? undefined;
    const renewed = this.renewed;
    if (renewed !== null) {
      if (current === undefined || current.profile.sub !== renewed.profile.sub) {
        // Signed out or a different principal signed in since the renew: the
        // stashed token no longer speaks for the session. Drop it.
        this.renewed = null;
      } else if ((current.expires_at ?? 0) >= (renewed.expires_at ?? 0)) {
        // The auth state caught up (userLoaded re-render adopted the renewed
        // or an even fresher token) — the stash has served its purpose.
        this.renewed = null;
      } else {
        return renewed.access_token;
      }
    }
    return current?.access_token;
  }

  reauthenticate(): void {
    void this.auth.signinRedirect();
  }

  async ensureFreshAccessToken(minRemainingSeconds: number): Promise<void> {
    const user = this.auth.user;
    if (user == null || user.expires_at === undefined) return;
    const stashed = this.renewed;
    const effectiveExpiry =
      stashed !== null && stashed.profile.sub === user.profile.sub
        ? Math.max(user.expires_at, stashed.expires_at ?? 0)
        : user.expires_at;
    if (effectiveExpiry - Date.now() / 1000 >= minRemainingSeconds) return;
    // Single-flight: concurrent submissions share one silent renew.
    this.renewInFlight ??= (async () => {
      try {
        const renewed = await this.auth.signinSilent();
        if (renewed !== null && renewed.profile.sub === user.profile.sub) {
          this.renewed = renewed;
        }
      } catch {
        // Fail-open to the CURRENT token: the renew is an availability
        // improvement, never an authorization decision. The governed API and
        // the worker's fail-closed snapshot-expiry checks are unchanged.
      } finally {
        this.renewInFlight = null;
      }
    })();
    await this.renewInFlight;
  }
}

/**
 * Build a client bound to the live OIDC session: the bearer comes from the
 * current user and a 401 re-triggers sign-in. The client is RETAINED across
 * token rotations and REPLACED on a genuine identity change — see the module
 * comment above for why that distinction is load-bearing.
 *
 * `create` must be a stable module-level factory (it is the client's shape, not
 * its identity); the rebuild key is the signed-in identity alone.
 */
export function useSessionBoundClient<T>(create: (options: SessionBoundClientOptions) => T): T {
  const auth = useAuth();
  const [live] = useState(() => new LiveSession(auth));
  useLayoutEffect(() => {
    live.adopt(auth);
  }, [auth, live]);

  // A string when the signed-in identity is established; the auth object itself
  // when it is not — null must never collapse two identities onto one client.
  const identity: string | AuthContextProps = authIdentity(auth) ?? auth;

  return useMemo(
    () =>
      create({
        getAccessToken: () => live.accessToken(),
        onUnauthorized: () => live.reauthenticate(),
        ensureFreshAccessToken: (minRemainingSeconds) =>
          live.ensureFreshAccessToken(minRemainingSeconds),
      }),
    // `identity` is the deliberate rebuild key, not a value the factory reads;
    // the factory reads only the stable `live` cell. Listing `auth` here is the
    // defect this hook fixes; listing neither would retain one principal's
    // client across a change of principal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [create, identity, live],
  );
}
