// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// One ops-gated load of the server-filtered CURRENT authority-attempt queue.
// It waits for /v1/me and skips a server-declared refusal, so merely opening
// Sources cannot generate its own denied receipt. The browser never reads raw
// connector state and never chooses a retry source.

import { useCallback, useEffect, useRef, useState } from "react";
import type { AuthorityAttemptsView } from "../api/types";
import { ApiError } from "../api/client";
import { useApiClient } from "../auth/useApiClient";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { useLatestRequest } from "../hooks/useLatestRequest";

export type AuthorityAttemptsPhase = "loading" | "ok" | "refused" | "error";

export interface AuthorityAttemptsState {
  status: AuthorityAttemptsPhase;
  data: AuthorityAttemptsView | null;
  retrying: string | null;
  retryFailed: string | null;
  refetch: () => void;
  retry: (attemptId: string) => void;
}

export function useAuthorityAttempts(
  knownRefused = false,
  deferred = false,
): AuthorityAttemptsState {
  const client = useApiClient();
  const runLatest = useLatestRequest();
  const { latestSeq } = useAuditPoll();
  const retryEpoch = useRef(0);
  const retryingRef = useRef(false);
  const [state, setState] = useState<{
    status: AuthorityAttemptsPhase;
    data: AuthorityAttemptsView | null;
    retrying: string | null;
    retryFailed: string | null;
  }>({ status: "loading", data: null, retrying: null, retryFailed: null });

  const load = useCallback(async () => {
    if (knownRefused || deferred) return;
    const epoch = retryEpoch.current;
    // Keep the effect callback free of a synchronous React state write; any
    // state transition below happens in the first microtask, before any
    // network result can win.
    await Promise.resolve();
    // Only the genuine FIRST load (no data on screen yet) shows the loading
    // state and clears the queue. A background refetch — the audit-poll tick
    // re-running this same `load` while a prior response is already showing —
    // must keep the stale rows up rather than blanking the table and refilling
    // it a moment later. (The `retry` path clears data itself, immediately and
    // synchronously, before this function ever runs — see `retry` below — so
    // this check still sees `data === null` and behaves the same for it.)
    setState((current) =>
      current.data === null ? { ...current, status: "loading", data: null } : current,
    );
    // Audit-triggered reads cannot repopulate an action while its retry is
    // pending. The post-retry load below is the sole read allowed to win.
    if (retryingRef.current) return;
    const outcome = await runLatest(() => client.authorityAttempts());
    if (epoch !== retryEpoch.current) return;
    if (outcome.kind === "superseded") return;
    if (outcome.kind === "success") {
      setState((current) => ({ ...current, status: "ok", data: outcome.value }));
      return;
    }
    const refused = outcome.error instanceof ApiError && outcome.error.status === 403;
    setState((current) => ({
      ...current,
      status: refused ? "refused" : "error",
      data: null,
    }));
  }, [client, runLatest, knownRefused, deferred]);

  useEffect(() => {
    void (async () => {
      await load();
    })();
  }, [load, latestSeq]);

  const refetch = useCallback(() => {
    void load();
  }, [load]);

  const retry = useCallback(
    (attemptId: string) => {
      // Removing data immediately removes every stale action from the DOM. The
      // post then carries only the opaque id; source binding stays server-owned.
      retryEpoch.current += 1;
      retryingRef.current = true;
      setState((current) => ({
        ...current,
        status: "loading",
        data: null,
        retrying: attemptId,
        retryFailed: null,
      }));
      void (async () => {
        let failed = false;
        try {
          await client.retryAuthorityAttempt(attemptId);
        } catch {
          failed = true;
        }
        retryingRef.current = false;
        await load();
        setState((current) => ({
          ...current,
          retrying: null,
          retryFailed: failed ? attemptId : null,
        }));
      })();
    },
    [client, load],
  );

  if (deferred) {
    return {
      status: "loading",
      data: null,
      retrying: null,
      retryFailed: null,
      refetch,
      retry,
    };
  }
  if (knownRefused) {
    return {
      status: "refused",
      data: null,
      retrying: null,
      retryFailed: null,
      refetch,
      retry,
    };
  }
  return { ...state, refetch, retry };
}
