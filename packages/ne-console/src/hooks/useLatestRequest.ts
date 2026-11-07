// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE console's ONE shared latest-request-wins guard. It closes three tracked
// refetch races that all share the same root cause (no ordering guarantee
// across concurrent async refetches):
//   - AuditPage / EvalsPage: a FAILED refetch must not leave the PRIOR
//     successful response's rows (and, for Audit, its chain verdict) rendered
//     as if current — the guard reports a distinct "error" outcome so the
//     caller can clear/gate its state atomically with the failure.
//   - flywheel/ProposalQueue / ApprovalsPage: overlapping refetches (initial load
//     + post-decision refetches) can resolve OUT OF ORDER; a slow stale response
//     must never overwrite a fresher one, or a just-decided proposal (flywheel
//     queue) or approval (Approvals) could momentarily reappear as re-actionable.
//     The failed-refetch clear applies here too — the error outcome lets the
//     caller drop the stale rows atomically with the error banner.
//
// This hook contains NO network primitive of its own — the caller passes an
// already-governed api-client task (e.g. `() => client.listAuditEvents(...)`),
// so the src/api/ thin-client seam (C6/C7) is untouched. One hook instance
// guards exactly ONE logical data stream: a page with a second independent
// stream (e.g. the flywheel DigestPanel's latency stream) needs its own
// `useLatestRequest()` instance (or, for a genuine one-shot effect, its own
// cancelled-flag pattern) — do not share one instance across two streams.

import { useCallback, useEffect, useRef } from "react";

/** The outcome of a single call through the guard's returned run function.
 * `superseded` means a NEWER call was started (or the component unmounted)
 * before or while this call's task was in flight — its result (success OR
 * error) must be ignored by the caller. */
export type LatestOutcome<T> =
  { kind: "success"; value: T } | { kind: "error"; error: unknown } | { kind: "superseded" };

/** Returns a stable (useCallback, empty deps) run function that guards ONE
 * logical data stream: each call bumps an internal sequence token, awaits the
 * given task, and reports "superseded" instead of "success"/"error" if a
 * later call (or an unmount) has since bumped the token again. Because the
 * returned function never changes identity, it can sit in an effect's
 * dependency array without retriggering that effect. */
export function useLatestRequest(): <T>(task: () => Promise<T>) => Promise<LatestOutcome<T>> {
  const seqRef = useRef(0);

  // Unmount supersedes everything still in flight. This also makes the guard
  // safe under React StrictMode's dev double-mount: the first mount's effect
  // starts a request, its cleanup runs this and bumps the token, the second
  // mount's request becomes the one that can still win.
  useEffect(() => {
    return () => {
      seqRef.current += 1;
    };
  }, []);

  return useCallback(async <T>(task: () => Promise<T>): Promise<LatestOutcome<T>> => {
    const token = ++seqRef.current;
    try {
      const value = await task();
      return token === seqRef.current ? { kind: "success", value } : { kind: "superseded" };
    } catch (error) {
      return token === seqRef.current ? { kind: "error", error } : { kind: "superseded" };
    }
  }, []);
}
