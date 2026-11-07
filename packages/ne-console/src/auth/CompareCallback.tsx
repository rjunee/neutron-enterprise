// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 — the /auth/compare-callback route (design §5.3). This renders inside
// the OIDC POPUP window opened by the COMPARE control. It relays the IdP response
// URL to window.opener via completeComparePopup (a throwaway UserManager); the
// PARENT window performs the code→token exchange, so NO token ever exists in this
// popup window. It reads the runtime config NULLABLY (never useRuntimeConfig,
// which throws) and renders honest text on the null-config and failure paths.

import { useContext, useEffect, useState } from "react";
import { completeComparePopup } from "../api/compareAuth";
import { RuntimeConfigContext } from "../AppBootstrap";

export function CompareCallback() {
  const config = useContext(RuntimeConfigContext);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (config === null) return;
    completeComparePopup(config).catch((err: unknown) => {
      setError(err instanceof Error ? err.message : "compare sign-in could not complete");
    });
  }, [config]);

  if (config === null) {
    return (
      <div className="ne-callback" role="alert">
        Runtime configuration unavailable — compare sign-in cannot complete.
      </div>
    );
  }
  if (error) {
    return (
      <div className="ne-callback" role="alert">
        Compare sign-in failed: {error}
      </div>
    );
  }
  return (
    <div className="ne-callback">Completing compare sign-in — return to the console window.</div>
  );
}
