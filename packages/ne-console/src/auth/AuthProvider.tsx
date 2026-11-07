// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react";
import { AuthProvider as OidcAuthProvider } from "react-oidc-context";
import type { ConsoleRuntimeConfig } from "../api/runtimeConfig";
import { buildOidcConfig } from "./oidcConfig";

/** After the IdP redirect completes, strip the ?code=&state= query params so
 * the auth code never lingers in the address bar or browser history. */
function onSigninCallback(): void {
  window.history.replaceState({}, document.title, window.location.pathname);
}

// NE-CONSOLE-ISSUER: the OIDC settings are built from the RUNTIME-fetched config
// (passed in by AppBootstrap once /config.json resolves), never a baked constant.
export function AuthProvider({
  config,
  children,
}: {
  config: ConsoleRuntimeConfig;
  children: ReactNode;
}) {
  return (
    <OidcAuthProvider {...buildOidcConfig(config)} onSigninCallback={onSigninCallback}>
      {children}
    </OidcAuthProvider>
  );
}
