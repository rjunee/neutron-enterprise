// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Landing route for the OIDC redirect_uri (/auth/callback). react-oidc-context
// processes the ?code=&state= response automatically; AuthProvider.onSigninCallback
// then strips those query params from history. Once a session exists we restore
// the originally requested protected path — captured as the signin `state` by
// RequireAuth — so the user is never stranded on the "Completing sign-in…"
// screen. Falls back to /chat when no return path is known.

import { useEffect } from "react";
import { useAuth } from "react-oidc-context";
import { useNavigate } from "react-router";

export function AuthCallback() {
  const auth = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (!auth.isAuthenticated) return;
    const state = auth.user?.state as { returnTo?: string } | undefined;
    const returnTo = state?.returnTo;
    // Never bounce back to the callback route itself (would loop the screen).
    const target = returnTo && !returnTo.startsWith("/auth/callback") ? returnTo : "/chat";
    navigate(target, { replace: true });
  }, [auth.isAuthenticated, auth.user, navigate]);

  return <div className="ne-callback">Completing sign-in…</div>;
}
