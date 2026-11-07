// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ONE SHARED PENDING-APPROVALS POLL.
//
// Review round 1: "Nothing tells you work has arrived." The Approvals nav item
// was plain text with no count on any screen, so an approver sitting on /posture
// or /kpi had no indication that a Tier-2 action had landed — the only way to
// find out was to navigate and look. On a Tuesday that is polling by hand all
// day, or missing the SLA clock the product itself draws.
//
// One provider, mounted once in the authed shell, exactly like the audit poll
// (design §6.7 — never per-component polling storms). It feeds the rail badge on
// every route, the Approvals queue, and home's "Waiting on you" lane.
//
// IT PUBLISHES THE ROWS, NOT JUST THE COUNT. This store used to expose only the
// derived `count`/`breached`, so a screen that needed the ROWS had no choice but
// to open its own stream of the same endpoint — which is exactly what /home and
// /approvals each did. Three readers on two unsynchronised timers cannot agree:
// the rail badge and the Approvals toolbar count became two answers to one
// question, drifting apart by up to a full interval, and /approvals — the
// highest-frequency screen in the product — paid double the request rate to
// disagree with itself. §8: "the toolbar count and rail badge update together —
// they read the same store." A projection is not a store; the rows are.
//
// So the count is no longer carried beside the rows. It IS the rows: derived at
// the context value from the one array every consumer renders. Badge and toolbar
// cannot drift, because there is no longer anything for them to drift between.
//
// HONESTY. A 403 is `refused`, NOT zero: an approver-less key must never see a
// confident "0 waiting" derived from a queue it was never shown. `count` is null
// on every non-ok status and the badge renders nothing rather than a figure.
// `breached` counts rows whose sla_deadline is already past, from the server's
// own timestamps — the only arithmetic here is a date comparison against the
// wall clock, which is what a deadline is.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import type { PendingApprovalView } from "../api/types";
import { entitlementsOf } from "../auth/entitlements";
import { usePrincipal } from "../auth/PrincipalContext";
import type { ApprovalOwner } from "../approvals/approvalOwner";
import {
  approvalOwnerReady,
  sameApprovalOwner,
  useApprovalOwner,
  useApprovalRequestGate,
} from "../approvals/approvalOwner";

export type PendingStatus = "loading" | "ok" | "refused" | "error";

export interface PendingApprovalsState {
  status: PendingStatus;
  /** The rows the server returned, verbatim and unfiltered — the ONE copy of
   * this queue the console holds. Empty on every non-ok status, never a
   * stand-in, and never mutated in place by a consumer. */
  approvals: PendingApprovalView[];
  /** Pending rows this key was actually shown. null on every non-ok status. */
  count: number | null;
  /** How many of those are already past their sla_deadline. null when unread. */
  breached: number | null;
  /** Re-read the queue NOW, off the timer, resolving when that read settles. It
   * REPLACES the pending tick rather than adding one, so a decision, a Retry and
   * every mounted screen share the single cadence. */
  refresh: () => Promise<void>;
}

/** What the provider holds. `count` is deliberately absent — it is `rows.length`
 * at the context value, so no code path can publish a count the rows disagree
 * with. `breached` is a wall-clock reading and so is taken ONCE, in the same
 * statement that stores the rows it counts (reading the clock during render is
 * impure, and would also be a different answer on every re-render). */
interface Store {
  status: PendingStatus;
  rows: PendingApprovalView[];
  breached: number | null;
}

interface BoundStore {
  owner: ApprovalOwner;
  value: Store;
}

/** The empty queue, shared so a non-ok status never allocates a new array (and
 * so `approvals` keeps a stable identity across error ticks). */
const NO_ROWS: PendingApprovalView[] = [];

const INITIAL_STORE: Store = { status: "loading", rows: NO_ROWS, breached: null };

const NO_REFRESH = async () => {};

/** Outside a provider: loading, no rows, no count. Honest, never a zero. */
const INITIAL_STATE: PendingApprovalsState = {
  status: "loading",
  approvals: NO_ROWS,
  count: null,
  breached: null,
  refresh: NO_REFRESH,
};

const PendingApprovalsContext = createContext<PendingApprovalsState>(INITIAL_STATE);

export function PendingApprovalsProvider({
  children,
  intervalMs = 15_000,
}: {
  children: ReactNode;
  intervalMs?: number;
}) {
  const owner = useApprovalOwner();
  const { begin, owns } = useApprovalRequestGate(owner, "pending-approvals");
  const principal = usePrincipal();
  const [boundStore, setBoundStore] = useState<BoundStore>({ owner, value: INITIAL_STORE });
  // Render/commit gate: old rows become unreadable in the same render that sees
  // a different client/principal, before the prior effect's cleanup can run.
  const store = sameApprovalOwner(boundStore.owner, owner) ? boundStore.value : INITIAL_STORE;
  // /v1/me already carries the server's own answer for this surface. Polling it
  // anyway writes an audited denial every fifteen seconds for a key that can
  // never read the queue (auth/entitlements.ts documents what that cost).
  const approvalsRefused =
    approvalOwnerReady(owner) && entitlementsOf(principal).refused("approvals");

  // The live cycle, owned by the effect below. `refresh` calls THROUGH this ref,
  // so an off-cycle read re-enters the one loop; no consumer can start a second.
  const cycleRef = useRef<{ owner: ApprovalOwner; run: () => Promise<void> } | null>(null);

  useEffect(() => {
    // The server's own answer is already in hand; the refused state it implies is
    // derived at the provider value below rather than written from an effect.
    if (!approvalOwnerReady(owner) || approvalsRefused) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      if (cancelled) return;
      // An off-cycle refresh TAKES OVER the pending tick instead of running
      // beside it. Without this, each Retry press would leave a live timer
      // behind and the queue would quietly speed up for the rest of the session.
      if (timer !== undefined) {
        clearTimeout(timer);
        timer = undefined;
      }
      const ticket = begin(owner, "pending-approvals");
      if (ticket === null) return;
      try {
        const page = await owner.client.listPendingApprovals();
        if (cancelled || !owns(ticket)) return;
        const now = Date.now();
        const rows = page.approvals;
        // One payload, one statement: the rows and the overdue tally the badge
        // tints on can never be from different reads.
        setBoundStore({
          owner,
          value: {
            status: "ok",
            rows,
            breached: rows.filter((a) => Date.parse(a.sla_deadline) - now < 0).length,
          },
        });
        timer = setTimeout(() => void poll(), intervalMs);
        return;
      } catch (error) {
        if (cancelled || !owns(ticket)) return;
        const refused = error instanceof ApiError && error.status === 403;
        setBoundStore({
          owner,
          value: { status: refused ? "refused" : "error", rows: NO_ROWS, breached: null },
        });
        // A key that will never hold this queue must not poll it every interval.
        timer = setTimeout(() => void poll(), intervalMs * (refused ? 12 : 4));
      }
    }

    cycleRef.current = { owner, run: poll };
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (cycleRef.current && sameApprovalOwner(cycleRef.current.owner, owner)) {
        cycleRef.current = null;
      }
    };
  }, [begin, intervalMs, owns, approvalsRefused, owner]);

  const refresh = useCallback(() => {
    const cycle = cycleRef.current;
    return cycle && sameApprovalOwner(cycle.owner, owner) ? cycle.run() : NO_REFRESH();
  }, [owner]);

  const value = useMemo<PendingApprovalsState>(() => {
    if (!approvalOwnerReady(owner)) {
      return { ...INITIAL_STATE, refresh };
    }
    if (approvalsRefused) {
      return { status: "refused", approvals: NO_ROWS, count: null, breached: null, refresh };
    }
    return {
      status: store.status,
      approvals: store.rows,
      // The count IS the rows. Every consumer that renders a figure and every
      // consumer that renders the list are reading one array, so there is no
      // second number left to fall out of step with the first.
      count: store.status === "ok" ? store.rows.length : null,
      breached: store.breached,
      refresh,
    };
  }, [approvalsRefused, owner, store, refresh]);

  return (
    <PendingApprovalsContext.Provider value={value}>{children}</PendingApprovalsContext.Provider>
  );
}

/** Read the shared pending-approvals state. Outside a provider this is the
 * honest initial state — loading, no count — never a fabricated zero. */
export function usePendingApprovals(): PendingApprovalsState {
  return useContext(PendingApprovalsContext);
}

/** The Approvals screen's view of the SAME store: the served rows, the read's
 * status, and an off-cycle refetch for the instant after a decision. No timer,
 * no request and no second copy of the queue live behind this. */
export interface PendingApprovalsQueue {
  rows: readonly PendingApprovalView[];
  status: PendingStatus;
  /** Re-read now; resolves when the read settles, so a decision handler can
   * `await` it exactly as it awaited a refetch of its own. */
  refetch: () => Promise<void>;
}

export function usePendingApprovalsQueue(): PendingApprovalsQueue {
  const { approvals, status, refresh } = usePendingApprovals();
  return useMemo(
    () => ({ rows: approvals, status, refetch: refresh }),
    [approvals, status, refresh],
  );
}

/** Home's view of the SAME store, shaped exactly like its other governed reads
 * (`{ status, data, reload }`), so the attention lane and the approvals tile can
 * read the shared poll without a second request — or a second shape. */
export interface PendingApprovalsRead {
  status: PendingStatus;
  /** The server's payload. Null on every status but "ok" — never a stand-in. */
  data: { approvals: PendingApprovalView[] } | null;
  /** Re-run the read (the Retry affordance, §2.4). */
  reload: () => void;
}

export function usePendingApprovalsRead(): PendingApprovalsRead {
  const { approvals, status, refresh } = usePendingApprovals();
  const reload = useCallback(() => void refresh(), [refresh]);
  return useMemo(
    () => ({ status, data: status === "ok" ? { approvals } : null, reload }),
    [status, approvals, reload],
  );
}
