// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The /sources data load. ONE governed read — GET /v1/connect/status — through
// the injected ApiClient (thin-client seam; no network primitive here), issued
// on mount and REFETCHED whenever the shared audit poll's latestSeq advances, so
// a connector or quarantine event surfaces on the next tick without a second
// polling loop. The latest-request guard drops a stale refetch, so an
// out-of-order response can never overwrite a fresher one.
//
// REFUSED IS NOT ERROR. A 403 is the server saying this key does not hold the
// surface; anything else is the read not arriving. They render as different
// screens (§2.4) and are therefore different statuses here — a screen that
// collapses them tells someone their role blocked a read that actually timed
// out, which is a false statement about authorization.
//
// `loadedAt` is the moment the response arrived. `cursor_age_s` is an age as of
// that moment, so the screen needs it to render the age as a time at all.

import { useCallback, useEffect, useState } from "react";
import type { ConnectStatusView } from "../api/types";
import { ApiError } from "../api/client";
import { useApiClient } from "../auth/useApiClient";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { useLatestRequest } from "../hooks/useLatestRequest";

export type ConnectStatusPhase = "loading" | "ok" | "refused" | "error";

export interface ConnectStatusState {
  status: ConnectStatusPhase;
  /** The served payload — null until the first successful read, and cleared on
   * a failed one so no stale window renders as if it were current. */
  data: ConnectStatusView | null;
  /** Epoch ms at which `data` arrived; 0 before the first success. */
  loadedAt: number;
  /** §2.4's Retry, wired to the same read. */
  refetch: () => void;
}

export function useConnectStatus(): ConnectStatusState {
  const client = useApiClient();
  const runLatest = useLatestRequest();
  const { latestSeq } = useAuditPoll();

  const [state, setState] = useState<{
    status: ConnectStatusPhase;
    data: ConnectStatusView | null;
    loadedAt: number;
  }>({ status: "loading", data: null, loadedAt: 0 });

  const load = useCallback(async () => {
    const outcome = await runLatest(() => client.connectStatus());
    if (outcome.kind === "superseded") return;
    if (outcome.kind === "success") {
      setState({ status: "ok", data: outcome.value, loadedAt: Date.now() });
      return;
    }
    const refused = outcome.error instanceof ApiError && outcome.error.status === 403;
    setState({ status: refused ? "refused" : "error", data: null, loadedAt: 0 });
  }, [client, runLatest]);

  useEffect(() => {
    // The read is awaited inside the effect rather than called from its body:
    // every state write here happens after the response, never synchronously
    // during the effect.
    void (async () => {
      await load();
    })();
    // Refetch on mount and whenever the shared audit poll advances.
  }, [load, latestSeq]);

  const refetch = useCallback(() => {
    setState((s) => ({ ...s, status: "loading" }));
    void load();
  }, [load]);

  return { ...state, refetch };
}
