// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Proves the OIDC-gated route: an unauthenticated visit triggers
// signinRedirect (spied at the oidc-client-ts UserManager boundary — the real
// react-oidc-context state machine runs) and renders NO protected content; an
// authenticated visit renders the children. Also asserts the PKCE public-client
// invariant on the config object (response_type "code", no client_secret).

import { render, screen, waitFor } from "@testing-library/react";
import { AuthProvider as OidcAuthProvider } from "react-oidc-context";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { buildOidcConfig } from "./oidcConfig";
import { RequireAuth } from "./RequireAuth";

type UserLike = { access_token: string; profile: { sub: string } } | null;

function fakeUserManager(user: UserLike, signinRedirect: () => Promise<void>) {
  const noop = vi.fn();
  return {
    settings: { authority: "http://idp", client_id: "ne-console" },
    events: {
      addUserLoaded: noop,
      removeUserLoaded: noop,
      addUserUnloaded: noop,
      removeUserUnloaded: noop,
      addSilentRenewError: noop,
      removeSilentRenewError: noop,
      addUserSignedOut: noop,
      removeUserSignedOut: noop,
    },
    getUser: vi.fn().mockResolvedValue(user),
    signinCallback: vi.fn().mockResolvedValue(null),
    signinRedirect: vi.fn(signinRedirect),
    removeUser: vi.fn().mockResolvedValue(undefined),
  };
}

function Protected() {
  return <div>SECRET DASHBOARD</div>;
}

afterEach(() => vi.restoreAllMocks());

describe("RequireAuth", () => {
  it("redirects to sign-in and renders no protected content when unauthenticated", async () => {
    const redirect = vi.fn().mockResolvedValue(undefined);
    const manager = fakeUserManager(null, redirect);
    render(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      <OidcAuthProvider userManager={manager as any}>
        <RequireAuth>
          <Protected />
        </RequireAuth>
      </OidcAuthProvider>,
    );

    await waitFor(() => expect(manager.signinRedirect).toHaveBeenCalled());
    expect(screen.queryByText("SECRET DASHBOARD")).not.toBeInTheDocument();
  });

  it("renders children when authenticated", async () => {
    const manager = fakeUserManager(
      { access_token: "tok", profile: { sub: "alice" } },
      vi.fn().mockResolvedValue(undefined),
    );
    render(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      <OidcAuthProvider userManager={manager as any}>
        <RequireAuth>
          <Protected />
        </RequireAuth>
      </OidcAuthProvider>,
    );

    expect(await screen.findByText("SECRET DASHBOARD")).toBeInTheDocument();
    expect(manager.signinRedirect).not.toHaveBeenCalled();
  });

  it("maps runtime config to Authorization Code + PKCE with no client secret", () => {
    // NE-CONSOLE-ISSUER: the config is BUILT from the runtime-fetched values —
    // issuer→authority, client_id→client_id — never a baked constant.
    const cfg = buildOidcConfig(RUNTIME_CONFIG) as Record<string, unknown>;
    expect(cfg.response_type).toBe("code");
    expect("client_secret" in cfg).toBe(false);
    expect(cfg.client_id).toBe("ne-console");
    expect(cfg.authority).toBe(RUNTIME_CONFIG.issuer);
    // Sign-out must land back on the console root (→ RequireAuth → the IdP
    // sign-in form), never strand the user on Keycloak's logged-out page.
    // oidc-client-ts omits the post_logout_redirect_uri param entirely when
    // this key is absent, so its presence is load-bearing.
    expect(cfg.post_logout_redirect_uri).toBe(`${window.location.origin}/`);
  });
});
