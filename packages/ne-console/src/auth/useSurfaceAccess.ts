// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// WHAT THIS KEY MAY OPEN — the server's ADVISORY answer, plus the server's
// OBSERVED answer.
//
// VERIFIED DEFECT, ROUND 3. `entitlementsOf()` reads `entitlements` off GET
// /v1/me. The deployment the console is actually demoed against does not send
// that field — its /v1/me returns `{id, groups}` and nothing else — so
// `refused()` is FALSE for every surface, "unknown is not denied" holds, and
// every affordance that gated on it silently reverted to the pre-round-2
// behaviour:
//   * the nav rail drew no locks at all, so GOVERN was three identical items and
//     three walls;
//   * /approvals rendered RUN THE GOVERNED DELEGATION as an enabled, full-weight
//     primary button that answers 403 AFTER the click — while /trace, which gates
//     on an OBSERVED refusal instead, correctly pre-disabled the same control.
// The console had two sources of truth for the same question and shipped the one
// that does not work on the deployment.
//
// This module is the single answer. It combines:
//   1. the ADVISORY entitlement list, when the deployment sends one — the only
//      source that can say "refused" BEFORE any request is made; and
//   2. the OBSERVED verdict of the two governed polls the shell already runs —
//      a 403 the server actually returned for this bearer on this surface.
// Either one saying "refused" is enough. Neither one saying so leaves the answer
// `unknown`, which every caller still treats as "ask the server".
//
// AUTHORIZATION IS STILL THE SERVER'S. Nothing here grants anything: an observed
// 403 is a fact this console was told, and a surface this module calls `open`
// can still come back 403, which renders exactly as it does today.

import { entitlementsOf, type Access, type Surface } from "./entitlements";
import { usePrincipal } from "./PrincipalContext";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { usePendingApprovals } from "../hooks/usePendingApprovals";

export interface SurfaceAccess {
  access(surface: Surface): Access;
  /** True only when this key has been told, or shown, that it does not hold it. */
  refused(surface: Surface): boolean;
}

/**
 * Merge the advisory answer with the observed one. Pure + exported so the rule is
 * testable without providers: `observed` is what the shell's polls saw.
 */
export function mergeAccess(
  advisory: (surface: Surface) => Access,
  observed: Partial<Record<Surface, Access>>,
): SurfaceAccess {
  const access = (surface: Surface): Access => {
    const fromAdvisory = advisory(surface);
    if (fromAdvisory !== "unknown") return fromAdvisory;
    return observed[surface] ?? "unknown";
  };
  return { access, refused: (surface) => access(surface) === "refused" };
}

/**
 * The shell-wide answer. Consumes only polls that are ALREADY mounted once in
 * the authed shell — this issues no request of its own.
 */
export function useSurfaceAccess(): SurfaceAccess {
  const entitled = entitlementsOf(usePrincipal());
  const audit = useAuditPoll();
  const approvals = usePendingApprovals();
  return mergeAccess(entitled.access, {
    // The audit poll IS the ops-gated read: `degraded` is its 403.
    ops: audit.status === "degraded" ? "refused" : audit.status === "ok" ? "granted" : "unknown",
    approvals:
      approvals.status === "refused"
        ? "refused"
        : approvals.status === "ok"
          ? "granted"
          : "unknown",
    // No shared steward poll exists, so nothing is observed for it. The advisory
    // answer is the only one, and its absence stays `unknown` rather than being
    // guessed at from `groups` — which would be client-side authorization.
  });
}
