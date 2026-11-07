// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The ONE shared audit poll (design §6.7 — never per-component polling storms).
// A single AuditPollProvider, mounted once in the authed shell, polls
// /v1/audit/events through the injected ApiClient (thin-client seam — NO network
// primitive lives here). It feeds: (1) the rail's cherenkov transit pulses, one
// per governed event that transits a nav stage, and (2) the chain-verdict lamp
// (server-computed verification, rendered verbatim — the client never recomputes
// hashes). Role degradation (§6.4): a 403 (non-ops principal) yields a "degraded"
// status the rail renders as a hollow "REQUIRES OPS KEY" — no crash, no fake data.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { createApprovalDecisionClient } from "../api/approvalDecisionClient";
import { ApiError } from "../api/client";
import type { AuditRecordView, ChainVerificationView } from "../api/types";
import { entitlementsOf } from "../auth/entitlements";
import { usePrincipal } from "../auth/PrincipalContext";
import { useSessionBoundClient } from "../auth/useSessionBoundClient";
import type { ApprovalOwner } from "../approvals/approvalOwner";
import {
  approvalOwnerReady,
  sameApprovalOwner,
  useApprovalOwner,
  useApprovalRequestGate,
} from "../approvals/approvalOwner";
import { applyVerification, clearLatch, INITIAL_LATCH } from "../audit/chainVerdict";
import type { ChainLatch, ChainVerdict, ObservedBreak } from "../audit/chainVerdict";

/** Map an audit event to the rail nav route whose stage it transits — the
 * CURRENT §1.2 destinations. This table used to name the pre-rename paths
 * (/connect, /knowledge, /chat, /flywheel, /evals, /kpi), which App.tsx keeps
 * only as redirects: a transit pulse keyed to a route no nav item carries can
 * never render, which is exactly how the rail shipped with zero visible
 * motion. rail/railMotion.test.tsx pins every route here to a real rail
 * destination. First match wins, in the order below; unmatched events land on
 * /audit. Pure + exported so the mapping is unit-testable without a provider. */
const ROUTE_RULES: ReadonlyArray<readonly [RegExp, string]> = [
  [/connector|ingest|connect/, "/sources"],
  [/graph/, "/knowledge/graph"],
  [/ontology/, "/knowledge/ontology"],
  [/verified/, "/verified-queries"],
  [/retrieve|search/, "/search"],
  [/brain|knowledge|document/, "/knowledge/documents"],
  // Ask left the rail on 2026-08-27 (Agents is the top-level destination and
  // Ask is its Chat tab), so this stage transits to the destination that
  // exists. The rule already named `agent`; only its target moved.
  [/chat|agent|session/, "/agents"],
  [/approval/, "/approvals"],
  [/steward|proposal|flywheel|signal/, "/improvement"],
  [/eval/, "/evaluations"],
  [/kpi/, "/insights"],
  [/posture|config/, "/posture"],
];

export function routeForAuditEvent(evt: { event_type: string; action: string | null }): string {
  const hay = `${evt.event_type} ${evt.action ?? ""}`.toLowerCase();
  for (const [re, route] of ROUTE_RULES) {
    if (re.test(hay)) return route;
  }
  return "/audit";
}

/** Every route a transit can name: the mapped stages plus the /audit fallback,
 * DERIVED from ROUTE_RULES rather than restated. The mapping's exact codomain,
 * uniqueness and membership in the live nav are pinned against this list, so
 * an edit that points at a route the rail does not carry fails a test instead
 * of silently never pulsing again. */
export const TRANSIT_ROUTES: readonly string[] = [
  ...new Set([...ROUTE_RULES.map(([, route]) => route), "/audit"]),
];

export type AuditPollStatus = "loading" | "ok" | "degraded" | "error";

/** One FRESH audit event from the most-recent poll batch. `seq` is the audit
 * chain seq; `route` is `routeForAuditEvent`'s mapping. Feeds count-accurate
 * consumers (the flywheel detent dedup) that need per-event granularity rather
 * than the route-deduped `transits` view. */
export interface TransitEvent {
  seq: number;
  route: string;
}

/** One recent run the poll has SEEN, built from retained `trace_id`s on the
 * events it already fetches. There is no recent-traces endpoint; this is the
 * honest way to make /trace reachable without hand-typing a UUID (redesign
 * §3.6). A 403 poll yields NO list at all — never a fabricated one. */
export interface RecentTrace {
  trace_id: string;
  occurred_at: string;
  events: number;
}

export interface AuditPollState {
  status: AuditPollStatus;
  /** Server-computed chain verdict — rendered verbatim, never recomputed. */
  verification: ChainVerificationView | null;
  /**
   * THE VERDICT EVERY SCREEN RENDERS — the ONE latched reading of the chain,
   * not the raw per-poll one and not one screen's private copy. `verification`
   * is still here verbatim for every evidence drawer; this is what a resting
   * indicator is allowed to say. See audit/chainVerdict.ts for the rule and the
   * race it exists for.
   *
   * IT IS THE LATCH'S, NOT THE POLL'S. This used to be forced to `"unknown"`
   * whenever `status !== "ok"`, which handed a failed poll the power to withdraw
   * a CONFIRMED break: one 502 and /audit fell back to its own passing read and
   * published `● VALID · 12,480 records verified` for the whole error backoff,
   * while the chip beside it said "Audit unavailable" — a contradiction inside
   * one screen, over the one indicator that is supposed to be tamper-evidence.
   * A poll that did not answer subtracts nothing; it only stops adding.
   */
  verdict: ChainVerdict;
  /** Every break this console observed, newest first — including transients. */
  observedBreaks: ObservedBreak[];
  /** Clear a LATCHED break with an explicit re-verification sweep. A passing
   * poll can never do this; only this call, only if the sweep passes, and only
   * if the sweep's PROOF is at least as strong as the one that latched the
   * break (`clearLatch`, audit/chainVerdict.ts I4). That is why the sweep's own
   * read asks the server for the real unbounded walk instead of the anchored
   * extension proof an interval poll gets. */
  reverify: () => void;
  /**
   * FOLD A READ OF THE SAME CHAIN INTO THE ONE LATCH.
   *
   * `assemble_trace` calls the same unbounded `verify_chain` the audit list
   * calls, so /trace's read is an observation of THIS chain — not of a second
   * one. It used to fold into a `useState` local to that screen, and /trace and
   * the rail chip then rendered two independent latches side by side inside one
   * layout: the chip could name a break at an entry while the strip two inches
   * below it read `● VALID`, and either screen's Re-verify cleared only its own.
   * There is one chain, so there is one latch, and this is how a screen with
   * its own read contributes to it.
   *
   * `sweep` marks the read an explicit re-verification — the only thing that can
   * clear a standing latch, and only if it comes back passing.
   *
   * IT RETURNS WHETHER THE READ WAS ACTUALLY FOLDED, because a caller that
   * re-reads on a budget has to spend that budget on reads the latch counted.
   * This call DISCARDS a read taken before the session it must be bound to
   * resolved (see the guard below), and /trace was incrementing its re-read
   * counter on every attempt: on a cold deep link where /v1/me was slow, the
   * whole budget went on discarded reads and the screen was left stating
   * "checking again" over a chain it had never got one countable reading of.
   * `false` here means nothing was folded and nothing can be counted.
   */
  observe: (verification: ChainVerificationView | null, options?: { sweep?: boolean }) => boolean;
  /** Highest seq observed so far, or null before the first successful poll. */
  latestSeq: number | null;
  /** The most recent page of audit records, verbatim. Empty when unreadable.
   * Consumers use it to resolve a just-written decision's chain sequence and to
   * render the recent-traces list; nothing here is ever synthesised. */
  events: AuditRecordView[];
  /** Distinct trace ids across `events`, newest first. */
  recentTraces: RecentTrace[];
  /** Nav routes to pulse from the MOST RECENT poll (unique). Empty on first load
   * (no fabricated pulses) and empty on any poll that surfaced no new events. */
  transits: string[];
  /** Per-event view of the SAME fresh batch `transits` collapses to unique routes:
   * one entry per new event (seq + route). `[]` on first load, `[]` when no new
   * events, `[]` on the error/degraded arms. */
  transitEvents: TransitEvent[];
}

const INITIAL: AuditPollState = {
  status: "loading",
  verification: null,
  verdict: "unknown",
  observedBreaks: [],
  reverify: () => {},
  // Nothing is mounted to fold into, so nothing was folded — the same answer a
  // real provider gives for a read it discards.
  observe: () => false,
  latestSeq: null,
  events: [],
  recentTraces: [],
  transits: [],
  transitEvents: [],
};

/** Distinct trace ids over a page of audit records, newest-occurrence first.
 * Pure + exported so the fold is unit-testable without a provider. */
export function foldRecentTraces(events: AuditRecordView[]): RecentTrace[] {
  const byId = new Map<string, RecentTrace>();
  for (const e of events) {
    if (!e.trace_id) continue;
    const prev = byId.get(e.trace_id);
    if (prev) {
      prev.events += 1;
      if (e.occurred_at > prev.occurred_at) prev.occurred_at = e.occurred_at;
    } else {
      byId.set(e.trace_id, { trace_id: e.trace_id, occurred_at: e.occurred_at, events: 1 });
    }
  }
  return [...byId.values()].sort((a, b) => (a.occurred_at < b.occurred_at ? 1 : -1));
}

// The context defaults to INITIAL rather than null. The authed shell always
// mounts the provider; a component rendered in isolation (a unit test, a
// storybook-style harness) therefore reads the HONEST initial state — loading,
// no verdict, no events — rather than throwing or, worse, inventing one.
const AuditPollContext = createContext<AuditPollState>(INITIAL);

interface BoundAuditPoll {
  owner: ApprovalOwner;
  state: AuditPollState;
  latch: ChainLatch;
}

/** The single audit-poll provider. Mount ONCE inside the authed shell. */
export function AuditPollProvider({
  children,
  intervalMs = 5000,
}: {
  children: ReactNode;
  intervalMs?: number;
}) {
  const owner = useApprovalOwner();
  const { begin, owns } = useApprovalRequestGate(owner, "audit-poll");
  const principal = usePrincipal();
  const [bound, setBound] = useState<BoundAuditPoll>({
    owner,
    state: INITIAL,
    latch: INITIAL_LATCH,
  });
  const [nonce, setNonce] = useState(0);
  const currentOwnerRef = useRef(owner);
  useLayoutEffect(() => {
    currentOwnerRef.current = owner;
  }, [owner]);
  const reverifyingRef = useRef<ApprovalOwner | null>(null);
  const visible = sameApprovalOwner(bound.owner, owner)
    ? bound
    : { owner, state: INITIAL, latch: INITIAL_LATCH };

  /** The server has already told us it will refuse the chain to this key. Asking
   * anyway writes an audited denial every five seconds, on every screen, for a
   * person who can never read the answer — the measured cause of the ~100
   * `api.audit.unauthorized` rows per ledger page and of a business metric that
   * moved while a reviewer browsed. */
  const opsRefused = approvalOwnerReady(owner) && entitlementsOf(principal).refused("ops");

  const reverify = useCallback(() => {
    // A retained button from the prior principal is inert before it can cause a
    // read under the new session.
    if (!approvalOwnerReady(owner) || !sameApprovalOwner(currentOwnerRef.current, owner)) {
      return;
    }
    reverifyingRef.current = owner;
    setNonce((n) => n + 1);
  }, [owner]);

  /**
   * A read taken by ANOTHER consumer of this same chain (/trace's own
   * `assemble_trace`), folded into the ONE latch.
   *
   * OWNER-BOUND, AND DELIBERATELY NOT A STABLE IDENTITY. The latch is
   * session-bound — `visible` drops it whole when the owner moves — so an
   * observation folded before the principal resolved is not merely unbound, it
   * is DISCARDED by the next render. A caller folds this from the effect that
   * took the read, so this identity moving with `owner` is what re-runs that
   * effect and takes the read again under the session that can actually hold
   * it. Made stable, /trace's first read on a cold deep link was silently
   * dropped and the screen was left one confirmation short of a break it had
   * already seen twice.
   */
  const observe = useCallback(
    (verification: ChainVerificationView | null, options?: { sweep?: boolean }) => {
      // The two discards, reported as such. A caller bounding its own re-reads
      // counts what came back true; see `observe` on AuditPollState.
      if (verification === null) return false;
      if (!approvalOwnerReady(owner) || !sameApprovalOwner(currentOwnerRef.current, owner)) {
        return false;
      }
      const at = new Date().toISOString();
      const sweep = options?.sweep === true;
      setBound((previous) => {
        const prior = sameApprovalOwner(previous.owner, owner)
          ? previous
          : { owner, state: INITIAL, latch: INITIAL_LATCH };
        // A sweep clears a standing latch ONLY on a passing verdict; `clearLatch`
        // returns `prev` untouched on a failing one, so a real break re-confirms
        // on the spot and no button can wish it away.
        const base = sweep ? clearLatch(prior.latch, verification) : prior.latch;
        return { ...prior, owner, latch: applyVerification(base, verification, at) };
      });
      return true;
    },
    [owner],
  );

  useEffect(() => {
    // Not "we decided you may not". The SERVER said so, on /v1/me, using the same
    // rule the route itself runs — so the request is simply not made. The state
    // this exposes for that case is derived below, not written from here.
    if (!approvalOwnerReady(owner) || opsRefused) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let latestSeq = visible.state.latestSeq;
    // Exponential backoff multiplier for non-403 errors: 2,4,… capped at 12.
    let errorMultiplier = 1;
    const CAP = 12;

    const schedule = (delay: number) => {
      if (cancelled) return;
      timer = setTimeout(poll, delay);
    };

    async function poll() {
      const ticket = begin(owner, "audit-poll");
      if (ticket === null) return;
      // DECIDED BEFORE THE REQUEST, because it changes the request. A sweep
      // must ask the server for the REAL unbounded walk: `clearLatch` refuses a
      // verdict weaker than the one that latched (chainVerdict.ts, I4), every
      // break this console can latch is confirmed off a full-chain verdict, and
      // the anchored proof this poll normally gets does not re-read the prefix
      // at all — so a sweep on the default read could never clear anything it
      // had confirmed, and before I4 it cleared breaks it could not see.
      const sweeping =
        reverifyingRef.current !== null && sameApprovalOwner(reverifyingRef.current, owner);
      if (sweeping) reverifyingRef.current = null;
      try {
        const page = await owner.client.listAuditEvents({
          limit: 25,
          full_verify: sweeping,
        });
        if (cancelled || !owns(ticket)) return;
        errorMultiplier = 1;
        const prev = latestSeq;
        const maxSeq = page.events.reduce((m, e) => (e.seq > m ? e.seq : m), prev ?? 0);
        // First load never pulses; thereafter only genuinely-new (seq > prev) events do.
        const freshEvents =
          prev === null
            ? []
            : page.events
                .filter((e) => e.seq > prev)
                .map((e) => ({ seq: e.seq, route: routeForAuditEvent(e) }));
        const transits = [...new Set(freshEvents.map((t) => t.route))];
        latestSeq = page.events.length > 0 ? maxSeq : prev;
        // THE LATCH, not the raw verdict. A read-during-append race made the
        // server disagree with itself between polls; rendering that verbatim in
        // the header made the product's single most load-bearing indicator flip
        // between VALID and "forged tail" on consecutive page loads.
        setBound((previous) => {
          const prior = sameApprovalOwner(previous.owner, owner)
            ? previous
            : { owner, state: INITIAL, latch: INITIAL_LATCH };
          const latchBase = sweeping ? clearLatch(prior.latch, page.verification) : prior.latch;
          return {
            owner,
            latch: applyVerification(latchBase, page.verification, new Date().toISOString()),
            state: {
              ...prior.state,
              status: "ok",
              verification: page.verification,
              latestSeq,
              events: page.events,
              recentTraces: foldRecentTraces(page.events),
              transits,
              transitEvents: freshEvents,
            },
          };
        });
        schedule(intervalMs);
        return;
      } catch (error) {
        if (cancelled || !owns(ticket)) return;
        // A sweep that never reached a verdict has not been spent. Put the
        // request back so the retry is the sweep, instead of silently
        // downgrading it to an ordinary anchored read that could not clear the
        // latch the operator asked about.
        if (sweeping) reverifyingRef.current = owner;
        const degraded = error instanceof ApiError && error.status === 403;
        errorMultiplier = degraded ? CAP : Math.min(errorMultiplier * 2, CAP);
        setBound((previous) => {
          const prior = sameApprovalOwner(previous.owner, owner)
            ? previous
            : { owner, state: INITIAL, latch: INITIAL_LATCH };
          return {
            ...prior,
            owner,
            state: {
              ...prior.state,
              status: degraded ? "degraded" : "error",
              verification: null,
              events: [],
              recentTraces: [],
              transits: [],
              transitEvents: [],
            },
          };
        });
        schedule(intervalMs * errorMultiplier);
      }
    }

    // Kick off immediately.
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // `visible.state.latestSeq` seeds this owner cycle once. Depending on the
    // value written by each tick would tear down and duplicate the poll loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [begin, intervalMs, nonce, opsRefused, owner, owns]);

  // The verdict a resting indicator may show, plus the durable break list and the
  // one control that can clear a latch. `verification` still rides verbatim for
  // every evidence drawer — nothing the server said is hidden, only the RESTING
  // STATE is required to agree with itself first.
  const value: AuditPollState = !approvalOwnerReady(owner)
    ? { ...INITIAL, reverify, observe }
    : opsRefused
      ? {
          ...INITIAL,
          // The SAME shape a live 403 produces, so every consumer renders exactly
          // what it renders for a real refusal — the difference is only that no
          // audited denial was written to get here.
          status: "degraded",
          // …but the LATCH still rides. A key the server refuses the audit LIST
          // to may still be able to open a run, and a break those reads confirmed
          // is this console's own observation. Zeroing it here would mean a
          // confirmed break is unreachable on /trace for exactly the principals
          // whose only window onto the chain is /trace.
          verdict: visible.latch.verdict,
          observedBreaks: visible.latch.observed,
          reverify,
          observe,
        }
      : {
          ...visible.state,
          // THE LATCH, unconditionally — see `verdict` on AuditPollState. The
          // poll's status is a fact about the POLL and is carried by `status`;
          // it is not allowed to edit what this console has confirmed about the
          // chain. `verification` is still nulled on a failed poll, which is
          // what stops a stale count from being printed beside the word.
          verdict: visible.latch.verdict,
          observedBreaks: visible.latch.observed,
          reverify,
          observe,
        };

  return <AuditPollContext.Provider value={value}>{children}</AuditPollContext.Provider>;
}

/** Read the shared audit-poll state. Outside a provider this is the honest
 * initial state (loading, no verdict, no events) — never fabricated data. */
export function useAuditPoll(): AuditPollState {
  return useContext(AuditPollContext);
}

/** Server-owned resolution of one approval receipt's terminal audit row.
 *
 * This deliberately does not inspect the shared newest-25 page. The server's
 * bounded exact-request lookup distinguishes a found terminal decision, an
 * authoritative miss, and an unreadable lookup; none means "a write is in
 * progress". State is request-bound so a late answer for one pane cannot link
 * the next request to the prior request's chain row.
 */
export type ApprovalDecisionAuditState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "found"; seq: number }
  | { status: "not_found" }
  | { status: "failure" };

interface BoundApprovalDecisionState {
  owner: ApprovalOwner;
  requestId: string | null;
  value: ApprovalDecisionAuditState;
}

const IDLE_DECISION: ApprovalDecisionAuditState = { status: "idle" };
const CHECKING_DECISION: ApprovalDecisionAuditState = { status: "checking" };

export function useApprovalDecisionAudit(
  requestId: string | null,
  enabled: boolean,
): ApprovalDecisionAuditState {
  const owner = useApprovalOwner();
  const { begin, owns, invalidate } = useApprovalRequestGate(owner, requestId ?? "");
  // Retained across silent token renewals, replaced on a genuine identity
  // change — see auth/useSessionBoundClient.ts. Keying this client on the auth
  // object rebuilt it every ~4 minutes and re-ran the decision audit with it.
  const client = useSessionBoundClient(createApprovalDecisionClient);
  const [bound, setBound] = useState<BoundApprovalDecisionState>({
    owner,
    requestId: null,
    value: IDLE_DECISION,
  });

  useEffect(() => {
    if (!approvalOwnerReady(owner) || !enabled || requestId === null || requestId === "") return;

    const controller = new AbortController();
    const boundRequestId = requestId;
    const ticket = begin(owner, boundRequestId);
    if (ticket === null) return;
    void client.read(boundRequestId, controller.signal).then(
      (record) => {
        if (!controller.signal.aborted && owns(ticket)) {
          setBound({
            owner,
            requestId: boundRequestId,
            value: { status: "found", seq: record.seq },
          });
        }
      },
      (error: unknown) => {
        if (controller.signal.aborted || !owns(ticket)) return;
        setBound({
          owner,
          requestId: boundRequestId,
          value:
            error instanceof ApiError && error.status === 404
              ? { status: "not_found" }
              : { status: "failure" },
        });
      },
    );
    return () => {
      controller.abort();
      invalidate(ticket);
    };
  }, [begin, client, enabled, invalidate, owner, owns, requestId]);

  if (!approvalOwnerReady(owner) || !enabled || requestId === null || requestId === "") {
    return IDLE_DECISION;
  }
  return sameApprovalOwner(bound.owner, owner) && bound.requestId === requestId
    ? bound.value
    : CHECKING_DECISION;
}
