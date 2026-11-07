// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// ONE GOVERNED READ, WITH THE FOUR STATES HOME ACTUALLY DISTINGUISHES.
//
// `refused` is a separate state from `error` because §4 renders them
// differently and must: a tile whose read was REFUSED for this role is omitted
// entirely (a lock on the home screen explains a job that is not this person's),
// while a tile whose read ERRORED shows the em dash and "Couldn't load". Collapse
// the two and one of them is a lie — either an outage reads as a permission
// boundary, or a permission boundary reads as an outage.
//
// A refusal the server has ALREADY declared on /v1/me is not asked again. That
// traffic — one audited denial per surface per home-screen load, for a key that
// can never read the answer — is what auth/entitlements.ts exists to stop.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { useLatestRequest } from "../hooks/useLatestRequest";

export type ReadStatus = "loading" | "ok" | "refused" | "error";

export interface GovernedRead<T> {
  status: ReadStatus;
  /** The server's payload. Null on every status but "ok" — never a stand-in. */
  data: T | null;
  /** Re-run the same read (the Retry affordance, §2.4). */
  reload: () => void;
}

export function useGovernedRead<T>(
  run: () => Promise<T>,
  /** The server said, on /v1/me, that it will refuse this surface to this key. */
  knownRefused = false,
  /** Identity/client generation that owns every rendered byte and completion. */
  ownershipKey: object = run,
  /** The server's advisory answer is still in flight. Wait; unknown is not a
   * grant, and asking now can create the exact denial this hook exists to skip. */
  deferred = false,
): GovernedRead<T> {
  const runLatest = useLatestRequest();
  const ownerRef = useRef(ownershipKey);
  useLayoutEffect(() => {
    ownerRef.current = ownershipKey;
  }, [ownershipKey]);
  const [state, setState] = useState<{
    owner: object;
    status: ReadStatus;
    data: T | null;
  }>({
    owner: ownershipKey,
    status: "loading",
    data: null,
  });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    // The refused arm is DERIVED below rather than written from here: there is
    // nothing to fetch and nothing to set.
    if (knownRefused || deferred) return;
    void (async () => {
      const outcome = await runLatest(run);
      if (ownerRef.current !== ownershipKey) return;
      if (outcome.kind === "success") {
        setState({ owner: ownershipKey, status: "ok", data: outcome.value });
        return;
      }
      if (outcome.kind === "error") {
        const refused = outcome.error instanceof ApiError && outcome.error.status === 403;
        setState({
          owner: ownershipKey,
          status: refused ? "refused" : "error",
          data: null,
        });
      }
      // "superseded" — a newer call or an unmount won; leave the state alone.
    })();
  }, [run, runLatest, knownRefused, deferred, nonce, ownershipKey]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  // Pending is not refused: the server has not answered yet. Keep an honest
  // loading state without retaining data or issuing the governed request.
  if (deferred) return { status: "loading", data: null, reload };
  if (knownRefused) return { status: "refused", data: null, reload };
  if (state.owner !== ownershipKey) return { status: "loading", data: null, reload };
  return { status: state.status, data: state.data, reload };
}
