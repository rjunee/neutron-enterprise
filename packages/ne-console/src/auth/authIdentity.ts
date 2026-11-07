// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// WHOSE SESSION IS THIS? — the one place that answers it.
//
// The console's ownership boundary is keyed on ApiClient identity: a changed
// client invalidates every governed read (see auth/PrincipalContext.tsx and
// landing/useGovernedRead.ts), so no principal's rows can survive into another
// principal's session. That boundary is correct and must stay.
//
// But `automaticSilentRenew` rotates the access token on a timer (60s before
// each expiry — every ~4 minutes at the original 300s realm lifespan, every
// ~14 at the current 900s), and react-oidc-context publishes a NEW auth
// object for each rotation (its reducer spreads a fresh state on USER_LOADED).
// Keying the client on that object identity therefore invalidated every
// governed read on a timer, blanking loaded lists back to skeletons while
// nothing about WHO was signed in had changed.
//
// This module separates the two. `authIdentity` returns a string that is
// STABLE across a token rotation and DIFFERENT for a different principal, a
// different SSO session (sign-out/in), or a different IdP/client — and returns
// null when it cannot establish which of those it is looking at.
//
// FAIL SAFE: null means "not identified", NOT "same as before". Callers must
// treat null as a fresh identity every time. Retaining rows across an
// unidentified transition is the one failure this code exists to prevent; a
// redundant refetch is merely a flash.

import type { AuthContextProps } from "react-oidc-context";

/** Field separator (NUL) that cannot occur in a URL, a UUID, or a Keycloak sid,
 * so no combination of components can spell another combination. */
const SEP = "\u0000";

/**
 * The identity that owns an API client, or null when it cannot be established.
 *
 * Composed of the IdP coordinates (authority + client_id — a genuine client
 * swap), the subject (a different principal) and the SSO session discriminator
 * (the SAME principal signing out and back in). Deliberately excludes
 * everything a silent renew rotates: the bearer, the ID and refresh tokens,
 * the expiry, and the jti/iat/exp claims. (The guardian in
 * ./sessionBoundClient.guardian.test.ts is why the bearer is not named here.)
 */
export function authIdentity(auth: AuthContextProps): string | null {
  const settings = auth.settings as { authority?: unknown; client_id?: unknown } | undefined;
  const authority = settings?.authority;
  const clientId = settings?.client_id;
  // Without the IdP coordinates we cannot say two clients talk to the same
  // deployment at all. Not identified.
  if (typeof authority !== "string" || authority === "") return null;
  if (typeof clientId !== "string" || clientId === "") return null;
  const idp = `${authority}${SEP}${clientId}`;

  const user = auth.user;
  // No user is a single, well-defined identity: signed out. Every signed-out
  // client for one IdP may safely be the same client — there is no principal
  // whose rows could leak, because there are no rows.
  if (!user) return `anonymous${SEP}${idp}`;

  const sub = user.profile.sub;
  // The SSO session. `sid` is the ID-token claim; `session_state` is the token
  // endpoint's copy of the same value. Either identifies the session; a new
  // sign-in produces a new one, a refresh reuses it.
  const sid = user.profile.sid;
  const session = typeof sid === "string" && sid !== "" ? sid : user.session_state;

  // FAIL SAFE. A subject alone cannot distinguish a refresh from a fresh
  // sign-in, and a session alone cannot distinguish two principals. Missing
  // either one, we refuse to claim this is the same identity as before.
  if (typeof sub !== "string" || sub === "") return null;
  if (typeof session !== "string" || session === "") return null;

  return `principal${SEP}${idp}${SEP}${sub}${SEP}${session}`;
}
