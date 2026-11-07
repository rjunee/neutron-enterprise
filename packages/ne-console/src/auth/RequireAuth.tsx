// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// OIDC route guard. An unauthenticated visit to a protected route triggers the
// Authorization-Code+PKCE redirect and renders NOTHING protected until a valid
// session exists — the server-side governed API is the real ACL boundary, but
// the console never even paints protected chrome for an anonymous principal.

import { useEffect } from "react";
import type { ReactNode } from "react";
import { useAuth } from "react-oidc-context";

export function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth();

  useEffect(() => {
    if (!auth.isLoading && !auth.isAuthenticated && !auth.activeNavigator) {
      // Capture the requested path so AuthCallback can restore it after the IdP
      // round-trip returns to /auth/callback — otherwise the user lands on the
      // callback screen instead of the route they asked for.
      void auth.signinRedirect({
        state: { returnTo: window.location.pathname + window.location.search },
      });
    }
  }, [auth.isLoading, auth.isAuthenticated, auth.activeNavigator, auth]);

  if (auth.isLoading || auth.activeNavigator) {
    return (
      <div className="ne-spinner" role="status">
        <span className="ne-skeleton ne-spinner__bar" aria-hidden="true" />
        Loading…
      </div>
    );
  }
  if (!auth.isAuthenticated) {
    // Redirect is firing in the effect; render nothing protected meanwhile.
    return null;
  }
  return <>{children}</>;
}
