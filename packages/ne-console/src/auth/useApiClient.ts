// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { createApiClient } from "../api/client";
import type { ApiClient } from "../api/client";
import { useSessionBoundClient } from "./useSessionBoundClient";

/** Build a governed-API client wired to the live OIDC session: the bearer token
 * comes from the current user, and a 401 re-triggers the sign-in redirect.
 *
 * The client is RETAINED across silent token renewals and REPLACED on a genuine
 * identity change. That distinction is a governance boundary, not a performance
 * tweak — see ./useSessionBoundClient for why. */
export function useApiClient(): ApiClient {
  return useSessionBoundClient(createApiClient);
}
