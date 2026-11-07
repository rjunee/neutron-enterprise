// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Permission visibility is served by GET /connect/status; this hook never infers
// roles. POST remains authoritative and the worker owns execution. The client
// only queues, polls bounded metadata, and refreshes the server-authored status.

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import type { ConnectSyncCountsView, ConnectSyncRequestView } from "../api/types";
import { useApiClient } from "../auth/useApiClient";

export type SourceSyncPhase = "queued" | "running" | "succeeded" | "failed";

export interface SourceSyncUiState {
  phase: SourceSyncPhase;
  counts: ConnectSyncCountsView | null;
  refused: boolean;
}

const POLL_INTERVAL_MS = 1000;
const TERMINAL_GRACE_MS = 60_000;

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export function useSourceSync(onTerminal: () => void) {
  const client = useApiClient();
  const mounted = useRef(true);
  const inFlight = useRef(new Set<string>());
  const onTerminalRef = useRef(onTerminal);
  const [bySource, setBySource] = useState<Record<string, SourceSyncUiState>>({});

  onTerminalRef.current = onTerminal;

  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );

  const setResult = useCallback((source: string, result: ConnectSyncRequestView) => {
    if (!mounted.current) return;
    setBySource((current) => ({
      ...current,
      [source]: {
        phase: result.status,
        counts: result.counts,
        refused: false,
      },
    }));
  }, []);

  const start = useCallback(
    async (source: string) => {
      if (inFlight.current.has(source)) return;
      inFlight.current.add(source);
      try {
        let result = await client.requestConnectSync(source);
        setResult(source, result);
        const deadline = Date.parse(result.expires_at) + TERMINAL_GRACE_MS;
        while (
          mounted.current &&
          (result.status === "queued" || result.status === "running") &&
          Date.now() <= deadline
        ) {
          await wait(POLL_INTERVAL_MS);
          if (!mounted.current) return;
          result = await client.readConnectSync(result.request_id);
          setResult(source, result);
        }
        if (result.status === "succeeded" || result.status === "failed") {
          onTerminalRef.current();
        } else if (mounted.current) {
          setBySource((current) => ({
            ...current,
            [source]: { phase: "failed", counts: null, refused: false },
          }));
        }
      } catch (error) {
        if (!mounted.current) return;
        setBySource((current) => ({
          ...current,
          [source]: {
            phase: "failed",
            counts: null,
            refused: error instanceof ApiError && error.status === 403,
          },
        }));
      } finally {
        inFlight.current.delete(source);
      }
    },
    [client, setResult],
  );

  return { bySource, start };
}
