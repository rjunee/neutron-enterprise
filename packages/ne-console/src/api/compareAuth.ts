// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 Dual-Key Compare — the SECOND-PERSONA OIDC seam + compare fetch context
// (design §5.3, §6 governance). This is the ONLY place the compare (key-B) token
// ever lives. It is held in a module CLOSURE variable, never in React state and
// never in browser storage: the UserManager is constructed with
// InMemoryWebStorage-backed user AND state stores, so oidc-client-ts persists
// nothing to sessionStorage/localStorage/cookies (token discipline). Exit =
// withdraw() best-effort-revokes the token, removes the user, and nulls the
// closure so getAccessToken() returns undefined forever after.
//
// GOVERNANCE:
//   - Thin client / C6-at-the-seam: this module lives under src/api/, the ONLY
//     zone allowed to reach the network. The compare fetch context is
//     createApiClient reuse — no literal fetch()/EventSource/WebSocket here; the
//     bearer flows through the same governed ApiClient the primary session uses.
//   - PUBLIC PKCE client: NO client credential anywhere (a browser client must
//     never hold one). oidc-client-ts performs Authorization Code + PKCE S256
//     automatically for response_type "code".
//   - No baked issuer: the authority/client_id come from the RUNTIME-fetched
//     ConsoleRuntimeConfig (/config.json), never a Vite env.
//   - C7: identity is SERVER-derived from the key-B bearer. The compare is two
//     independently server-trimmed responses; this seam only carries the second
//     bearer — it never diffs privileged data.
//   - The primary session token and the compare token NEVER mix: this is a
//     SEPARATE ApiClient instance, and its 401 handler NEVER calls
//     signinRedirect (that would clobber the operator's primary login).

import { InMemoryWebStorage, UserManager, WebStorageStateStore } from "oidc-client-ts";
import type { User } from "oidc-client-ts";
import { createApiClient, type ApiClient } from "./client";
import type { ConsoleRuntimeConfig } from "./runtimeConfig";

/** A live second-persona key, scoped to the compare pane's lifetime. The token
 * lives only behind getAccessToken(); withdraw() discards it (and best-effort
 * revokes it server-side). */
export interface CompareKey {
  /** The key-B persona label for the pane's KEY B plate (preferred_username ??
   * sub) — a server-carried display string, rendered inert (C12). */
  readonly label: string;
  /** The current key-B bearer, or undefined once withdrawn / never acquired. */
  getAccessToken(): string | undefined;
  /** Best-effort revoke + removeUser + null the held token. Idempotent. */
  withdraw(): Promise<void>;
}

/** The compare-callback redirect path (a real route in App.tsx). */
export const COMPARE_CALLBACK_PATH = "/auth/compare-callback";

function compareRedirectUri(): string {
  return `${window.location.origin}${COMPARE_CALLBACK_PATH}`;
}

/** Build a UserManager configured EXACTLY like the primary login except: both
 * the user store AND the state store are InMemoryWebStorage (nothing persisted),
 * and silent renew is off (the compare token is short-lived by design). */
function buildCompareUserManager(cfg: ConsoleRuntimeConfig): UserManager {
  return new UserManager({
    authority: cfg.issuer,
    client_id: cfg.client_id,
    redirect_uri: compareRedirectUri(),
    popup_redirect_uri: compareRedirectUri(),
    // Authorization Code flow; PKCE S256 automatic for a public client. NO
    // client credential is present, by construction.
    response_type: "code",
    scope: "openid profile email",
    // Token + auth-request state live ONLY in memory — never sessionStorage,
    // localStorage, or cookies. This is the token-discipline crux.
    userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
    stateStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
    automaticSilentRenew: false,
  });
}

function personaLabel(user: User): string {
  const profile = user.profile;
  const preferred = profile.preferred_username;
  if (typeof preferred === "string" && preferred.length > 0) return preferred;
  return profile.sub ?? "second keyholder";
}

/**
 * Run the REAL second-persona login. Opens an OIDC popup (Authorization Code +
 * PKCE) against the runtime-config issuer with `prompt:"login"` — MANDATORY:
 * without it Keycloak's SSO cookie silently re-issues persona A and the compare
 * would be a lie (§0 law 5). Resolves a CompareKey whose token lives ONLY in the
 * returned closure. Rejects (popup blocked/closed, IdP error) so the caller can
 * render an honest error and never a fabricated pane.
 */
/**
 * PREFLIGHT: can this browser open a popup at all?
 *
 * The compare beat is the flagship two-identity demo and it depended on a popup
 * surviving a blocker, with no way to tell whether one had opened — the page sat
 * on "waiting on it, giving up at 120s" and only then said "if no window opened,
 * it was blocked". A minute and a half of dead air in front of a buyer, for a
 * condition that is detectable in a millisecond.
 *
 * Opening and immediately closing a 1x1 window inside the user's own click
 * gesture is the only reliable test: a blocked open returns null (or a window
 * whose `closed` is already true). Called synchronously from the click handler,
 * because a deferred window.open loses the gesture and would fail spuriously.
 */
export function popupsBlocked(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const probe = window.open("", "_blank", "width=1,height=1,left=-1000,top=-1000");
    if (probe === null || probe.closed) return true;
    probe.close();
    return false;
  } catch {
    return true;
  }
}

export async function acquireCompareKey(cfg: ConsoleRuntimeConfig): Promise<CompareKey> {
  const um = buildCompareUserManager(cfg);
  // prompt:"login" forces re-authentication — the second keyholder must prove
  // identity, never inherit persona A's SSO session.
  const user = await um.signinPopup({ prompt: "login" });

  // The ONLY reference to the key-B user. Never React state, never storage.
  let held: User | null = user;
  const label = personaLabel(user);

  return {
    label,
    getAccessToken() {
      return held?.access_token;
    },
    async withdraw() {
      held = null;
      // Best-effort server-side revocation, then wipe the in-memory user. Both
      // tolerate rejection: a withdrawn key must end up unusable regardless.
      await um.revokeTokens().catch(() => {});
      await um.removeUser().catch(() => {});
    },
  };
}

/**
 * The compare fetch context: a SEPARATE ApiClient bound to the key-B bearer. The
 * identical governed query bodies run under this client (server trims key B's
 * response independently — C7). `onKeyExpired` fires on a 401 under key B; it
 * must NEVER trigger the primary signinRedirect (that would clobber the
 * operator's own session) — the pane renders an honest expired state instead.
 */
export function createCompareClient(key: CompareKey, onKeyExpired: () => void): ApiClient {
  return createApiClient({
    getAccessToken: () => key.getAccessToken(),
    onUnauthorized: onKeyExpired,
  });
}

/**
 * POPUP-WINDOW half of the flow. The popup navigates to the IdP, returns to
 * /auth/compare-callback, and here relays the response URL to window.opener via
 * a throwaway, identically-configured UserManager. The PARENT window's
 * UserManager performs the code→token exchange — NO token ever materialises in
 * the popup window (the reason the in-memory state store works across the
 * popup boundary). Do NOT attempt to complete the exchange here.
 */
export async function completeComparePopup(cfg: ConsoleRuntimeConfig): Promise<void> {
  const um = buildCompareUserManager(cfg);
  await um.signinPopupCallback();
}
