// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The ONE shared sovereignty-posture load (design §6.7 — no per-component polling
// storms). A single PostureProvider, mounted once in the authed shell, reads
// GET /v1/posture on mount through the injected ApiClient (thin-client seam —
// NO network primitive lives here). It is not polled, but its one refresh seam
// re-reads after a successfully applied governed runtime change. It feeds three
// consumers off one shared payload: the containment rail's
// C-lamps / AIR_GAP / config_hash plate, the sovereignty landing hero's instrument
// row, and the /posture annunciator board. Loading/error render hollow, never lit
// (§0 law 5): a failed fetch surfaces an honest fail-closed state, never fake data.

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { PostureView } from "../api/types";
import { ApiError } from "../api/client";
import { useApiClient } from "../auth/useApiClient";
import { useLatestRequest } from "../hooks/useLatestRequest";

export type PostureStatus = "loading" | "ok" | "refused" | "error";

export interface PostureState {
  status: PostureStatus;
  /** The governed posture payload — null until the first successful fetch. */
  posture: PostureView | null;
  /** Re-read the shared server posture after a successfully applied change. */
  refresh: () => Promise<void>;
}

interface PostureSnapshot {
  status: PostureStatus;
  posture: PostureView | null;
}

const NO_REFRESH = async () => {};
const INITIAL_SNAPSHOT: PostureSnapshot = { status: "loading", posture: null };
const INITIAL: PostureState = { ...INITIAL_SNAPSHOT, refresh: NO_REFRESH };

// Defaults to INITIAL (see useAuditPoll's note): a component rendered outside
// the shell reads "loading / no posture", never a fabricated posture.
const PostureContext = createContext<PostureState>(INITIAL);

/** The single posture provider. Mount ONCE inside the authed shell. */
export function PostureProvider({ children }: { children: ReactNode }) {
  const client = useApiClient();
  const runLatest = useLatestRequest();
  const [snapshot, setSnapshot] = useState<PostureSnapshot>(INITIAL_SNAPSHOT);

  const refresh = useCallback(async () => {
    const outcome = await runLatest(() => client.posture());
    if (outcome.kind === "success") {
      setSnapshot({ status: "ok", posture: outcome.value });
    } else if (outcome.kind === "error") {
      // A 403 and an unreachable server are NOT the same fact, and the status
      // popover states one of them out loud. Collapsing both into "error" is
      // what let the chip render "Not visible to your role" while posture was
      // merely loading or the read had failed — a false claim about
      // authorization, on the one surface designated as the home of global
      // state. useAuditPoll already draws this distinction; posture now does too.
      const refused = outcome.error instanceof ApiError && outcome.error.status === 403;
      setSnapshot({ status: refused ? "refused" : "error", posture: null });
    }
    // "superseded" — a newer request or an unmount supersedes this one; ignore.
  }, [client, runLatest]);

  useEffect(() => {
    void (async () => {
      await refresh();
    })();
  }, [refresh]);

  const value = useMemo(() => ({ ...snapshot, refresh }), [snapshot, refresh]);

  return <PostureContext.Provider value={value}>{children}</PostureContext.Provider>;
}

/** Read the shared posture state. Outside a provider this is the honest
 * loading state — never a fabricated posture. */
export function usePosture(): PostureState {
  return useContext(PostureContext);
}
