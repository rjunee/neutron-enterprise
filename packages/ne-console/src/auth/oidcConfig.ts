// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// OIDC Authorization-Code + PKCE (S256) configuration for the PUBLIC client
// `ne-console`. There is NO client credential anywhere in the returned object (a
// public browser client must never hold one) — oidc-client-ts performs PKCE S256
// automatically for a public client using response_type "code".
//
// NE-CONSOLE-ISSUER: the issuer/client_id are NO LONGER baked at Vite build time.
// `buildOidcConfig` maps the RUNTIME-fetched `ConsoleRuntimeConfig` (from ne-api's
// /config.json — the same IdentitySettings the backend verifies with) into the
// oidc-client-ts settings, so one built console self-configures its login from the
// deployment it is served by. There is no fallback issuer, ever.

import { WebStorageStateStore } from "oidc-client-ts";
import type { AuthProviderProps } from "react-oidc-context";
import type { ConsoleRuntimeConfig } from "../api/runtimeConfig";

export function buildOidcConfig(cfg: ConsoleRuntimeConfig): AuthProviderProps {
  return {
    authority: cfg.issuer,
    client_id: cfg.client_id,
    redirect_uri: `${window.location.origin}/auth/callback`,
    // Sign-out returns to the console root, where RequireAuth immediately
    // bounces an unauthenticated visitor to the IdP sign-in form. Without this
    // key oidc-client-ts sends NO post_logout_redirect_uri and the user is
    // stranded on Keycloak's "You are logged out" page (no link back). The
    // realm must declare this target in `post.logout.redirect.uris` — the
    // claimed-vs-declared guard (tools/ne-cli/tests/test_oidc_redirect_claims.py)
    // derives it from this file and enforces that.
    post_logout_redirect_uri: `${window.location.origin}/`,
    // Authorization Code flow; PKCE S256 is applied automatically for a public
    // client. NO client credential key is present, by construction.
    response_type: "code",
    scope: "openid profile email",
    // Bearer tokens live only in sessionStorage — cleared when the tab closes.
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    automaticSilentRenew: true,
  };
}
