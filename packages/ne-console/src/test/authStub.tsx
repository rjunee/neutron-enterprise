// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// A test AuthProvider stub: it supplies an already-authenticated principal +
// access token via react-oidc-context's AuthContext, so component tests can
// exercise the governed-API client without a real OIDC round-trip. The
// RequireAuth test deliberately does NOT use this — it drives the real
// react-oidc-context unauthenticated path with a spied signinRedirect.

import type { ReactNode } from "react";
import { AuthContext } from "react-oidc-context";
import type { AuthContextProps } from "react-oidc-context";

export const TEST_ACCESS_TOKEN = "test-access-token";

export function makeAuthValue(overrides: Partial<AuthContextProps> = {}): AuthContextProps {
  const base = {
    isAuthenticated: true,
    isLoading: false,
    activeNavigator: undefined,
    error: undefined,
    user: {
      access_token: TEST_ACCESS_TOKEN,
      profile: { sub: "alice@example.com" },
    },
    signinRedirect: async () => {},
    signinSilent: async () => null,
    removeUser: async () => {},
    signoutRedirect: async () => {},
  } as unknown as AuthContextProps;
  return { ...base, ...overrides };
}

export function AuthStub({
  children,
  value,
}: {
  children: ReactNode;
  value?: Partial<AuthContextProps>;
}) {
  return <AuthContext.Provider value={makeAuthValue(value)}>{children}</AuthContext.Provider>;
}
