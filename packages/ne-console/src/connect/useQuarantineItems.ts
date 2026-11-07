// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The quarantine drill-down load (NE-CONNECT-CSV). ONE governed, ops-gated read
// — GET /v1/connect/quarantine — mirroring useConnectStatus exactly: issued only
// after /v1/me settles, skipped when that server answer refuses ops, and
// refetched when the shared audit poll advances (a new sync's parks surface on
// the next tick), latest-request guarded.
//
// REFUSED IS NOT ERROR, and here refused is also NOT A FAILURE OF THE SCREEN:
// the categories ledger is served to everyone; only the row-level drill-down is
// ops-gated. A 403 therefore renders as "no drill-down", never as a broken
// quarantine section and never as a locally-fabricated empty list.

import { useCallback, useEffect, useState } from "react";
import type { ConnectQuarantineView } from "../api/types";
import { ApiError } from "../api/client";
import { useApiClient } from "../auth/useApiClient";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { useLatestRequest } from "../hooks/useLatestRequest";

export type QuarantineItemsPhase = "loading" | "ok" | "refused" | "error";

export interface QuarantineItemsState {
  status: QuarantineItemsPhase;
  data: ConnectQuarantineView | null;
}

export function useQuarantineItems(knownRefused = false, deferred = false): QuarantineItemsState {
  const client = useApiClient();
  const runLatest = useLatestRequest();
  const { latestSeq } = useAuditPoll();

  const [state, setState] = useState<QuarantineItemsState>({ status: "loading", data: null });

  const load = useCallback(async () => {
    if (knownRefused || deferred) return;
    const outcome = await runLatest(() => client.connectQuarantine());
    if (outcome.kind === "superseded") return;
    if (outcome.kind === "success") {
      setState({ status: "ok", data: outcome.value });
      return;
    }
    const refused = outcome.error instanceof ApiError && outcome.error.status === 403;
    setState({ status: refused ? "refused" : "error", data: null });
  }, [client, runLatest, knownRefused, deferred]);

  useEffect(() => {
    void (async () => {
      await load();
    })();
  }, [load, latestSeq]);

  if (deferred) return { status: "loading", data: null };
  if (knownRefused) return { status: "refused", data: null };
  return state;
}
