// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE GUIDED TOUR — the provider. Owns the overlay's open/index/checks
// state and drives the check-off ("the demo audits itself"). It mounts INSIDE the
// AuditPollProvider and consumes the ONE shared audit poll (design §6.7 — no new
// fetch, no per-component polling): a station "checks off" when a NEW governed
// audit seq is observed on the shared poll while that station is armed.
//
// PLACEMENT: the stations were once the BODY of the home route — the product
// opened on its own walkthrough. They are not the product. The tour now lives
// where every product puts a walkthrough:
//   - FIRST RUN: the panel opens once, on the first authed load, and says so.
//   - ONE ACTION KILLS IT: `dismiss()` writes the localStorage dismissal, so the
//     panel can never become the default view again — including after a reload,
//     a new tab, or a different route. There is no second confirmation step.
//   - IT STAYS REACHABLE: the account menu's "Guided tour" item calls `summon()`.
// `mode` distinguishes the two presentations so the panel can introduce itself on
// a first run and simply be a tour when someone asks for it.
//
// CHECK-OFF: the recorded seq is ALWAYS a real seq the server
// produced — poll.latestSeq observed to advance while the station is armed. The
// module holds NO other seq source, so an invented check-off is impossible by
// construction. Attribution is deliberately weak: it is "the newest audit entry
// while this station is armed" (a background event could satisfy it), and it is
// never presented as causal proof. When the audit read is refused for this role
// the panel says so once, and no station checks off.

import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { clampIndex, STATIONS } from "./stations";
import { loadDemoState, saveDemoState } from "./demoStorage";
import { usePrincipal } from "../auth/PrincipalContext";
import { usePendingApprovals } from "../hooks/usePendingApprovals";
import { dismissTour, isTourDismissed } from "./tourStorage";

/** Health of the shared audit poll, mapped to the demo's check-off vocabulary. */
export type CheckoffStatus = "live" | "requires-ops-key" | "unreachable" | "verifying";

/** How the panel is being shown: unprompted on a first run, or because someone
 * asked for it. Only the presentation differs — the stations are identical. */
export type DemoMode = "first-run" | "summoned";

export interface DemoContextValue {
  /** Overlay visibility. True on mount ONLY on a never-dismissed first run. */
  open: boolean;
  /** Which presentation the panel is wearing. */
  mode: DemoMode;
  /** Current station index into STATIONS. */
  index: number;
  /** Per-station check-off: station id → the real audit seq that checked it off. */
  checks: Record<string, number>;
  /** The shared audit-poll health, for the check-off placard. */
  checkoffStatus: CheckoffStatus;
  /** Open the panel at the persisted station, as a tour someone asked for. */
  summon(): void;
  /** Close the panel AND record the dismissal permanently (position is retained
   * so a later summon resumes where the tour left off). One action, no
   * confirmation: the panel never auto-opens again. */
  dismiss(): void;
  /** Advance one station (clamped, no wraparound). */
  next(): void;
  /** Retreat one station (clamped, no wraparound). */
  prev(): void;
  /** Jump to a station index (clamped). */
  goTo(i: number): void;
}

const DemoContext = createContext<DemoContextValue | null>(null);

function checkoffStatusFor(pollStatus: string): CheckoffStatus {
  switch (pollStatus) {
    case "ok":
      return "live";
    case "degraded":
      return "requires-ops-key";
    case "error":
      return "unreachable";
    default:
      return "verifying";
  }
}

export function DemoProvider({ children }: { children: ReactNode }) {
  const poll = useAuditPoll();
  // The dismissal is scoped to the signed-in principal, so one person dismissing
  // the tour on a shared machine no longer hides it from the next (tourStorage.ts
  // is explicit about what this does and does not fix).
  const principal = usePrincipal();
  const principalId = principal.principal?.id ?? null;

  // Position + check-offs resume from sessionStorage.
  const [restored] = useState(() => loadDemoState() ?? { v: 1 as const, station: 0, checks: {} });
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<DemoMode>("summoned");

  // THE FIRST-RUN DECISION WAITS FOR THE PRINCIPAL.
  //
  // It used to be seeded synchronously from localStorage at mount. Once the
  // dismissal became principal-scoped that was a live defect, not just a test
  // failure: at mount GET /v1/me has not resolved, so the id is null, so the
  // scoped dismissal is invisible and the panel re-opens on every load for
  // someone who had already dismissed it — the exact behaviour the store exists
  // to prevent.
  //
  // This is React's documented "adjust state during render" pattern (not an
  // effect): the decision is taken once, the first time the principal is known,
  // and re-taken only if a different principal signs in.
  //
  // AND IT WAITS FOR THE QUEUE. A first-run panel is for someone who has nothing
  // else to do here. The approver opens this console BECAUSE something is waiting
  // for a decision — an unprompted walkthrough is then an interruption of the
  // highest-frequency job in the product, on the screen where that job starts. A
  // principal whose role holds a decision queue never gets the tour unprompted;
  // "Guided tour" in the account menu summons it exactly as before.
  const pending = usePendingApprovals();
  const queueSettled = pending.status !== "loading";
  const holdsAQueue = pending.status === "ok";
  const [decidedFor, setDecidedFor] = useState<string | null | undefined>(undefined);
  if (principal.status !== "loading" && queueSettled && decidedFor !== principalId) {
    setDecidedFor(principalId);
    const dismissed = isTourDismissed(principalId);
    const autoOpen = !dismissed && !holdsAQueue;
    setOpen(autoOpen);
    setMode(autoOpen ? "first-run" : "summoned");
  }
  const [index, setIndex] = useState(() => clampIndex(restored.station, STATIONS.length));
  const [checks, setChecks] = useState<Record<string, number>>(() => restored.checks);

  // Persist position + checks on every change (open is intentionally excluded).
  useEffect(() => {
    saveDemoState({ v: 1, station: index, checks });
  }, [index, checks]);

  // Arm/check-off against the ONE shared audit poll. armedSeqRef is the seq
  // baseline for the current station; armedIndexRef tracks which station it is
  // armed for so a station change re-baselines (and never inherits a stale, lower
  // baseline that would instantly check off).
  const armedSeqRef = useRef<number | null>(null);
  const armedIndexRef = useRef<number>(-1);

  useEffect(() => {
    if (!open) {
      // Closing disarms; a re-summon re-baselines against the live seq.
      armedIndexRef.current = -1;
      armedSeqRef.current = null;
      return;
    }
    const station = STATIONS[index];
    if (!station) return;
    const seq = poll.latestSeq;

    // (Re)arm the baseline whenever we land on a (new) station.
    if (armedIndexRef.current !== index) {
      armedIndexRef.current = index;
      armedSeqRef.current = seq; // may be null — arming completes when a page arrives
      return;
    }
    // No real audit page has arrived yet: the station stays uncheckable (no
    // fabricated seq is ever synthesized).
    if (seq === null) return;
    if (armedSeqRef.current === null) {
      // First real page since arming establishes the baseline — never a check-off.
      armedSeqRef.current = seq;
      return;
    }
    // A genuinely NEW seq advanced past the baseline while armed → check off
    // with that real seq.
    if (seq > armedSeqRef.current && checks[station.id] === undefined) {
      armedSeqRef.current = seq;
      setChecks((prev) => (prev[station.id] === undefined ? { ...prev, [station.id]: seq } : prev));
    }
  }, [open, index, poll.latestSeq, checks]);

  const value = useMemo<DemoContextValue>(
    () => ({
      open,
      mode,
      index,
      checks,
      checkoffStatus: checkoffStatusFor(poll.status),
      summon: () => {
        setMode("summoned");
        setOpen(true);
      },
      dismiss: () => {
        // The dismissal is written on the way out, unconditionally. Closing the
        // panel and "stop showing me this" are ONE action by design.
        dismissTour(principalId);
        setOpen(false);
      },
      next: () => setIndex((i) => clampIndex(i + 1, STATIONS.length)),
      prev: () => setIndex((i) => clampIndex(i - 1, STATIONS.length)),
      goTo: (i: number) => setIndex(clampIndex(i, STATIONS.length)),
    }),
    [open, mode, index, checks, poll.status, principalId],
  );

  return <DemoContext.Provider value={value}>{children}</DemoContext.Provider>;
}

// An inert default returned when useDemo() is read outside a provider. The
// summon control lives in the account menu, which is exercised in isolation; the
// production shell always mounts DemoProvider around it, so this fallback only
// ever backs those isolated renders — summon() is then a safe no-op.
const INERT: DemoContextValue = {
  open: false,
  mode: "summoned",
  index: 0,
  checks: {},
  checkoffStatus: "verifying",
  summon: () => {},
  dismiss: () => {},
  next: () => {},
  prev: () => {},
  goTo: () => {},
};

/** Read the guided-demo controls. Outside a DemoProvider returns an inert default
 * (the rail summon must not require the provider to render in isolation). */
export function useDemo(): DemoContextValue {
  return useContext(DemoContext) ?? INERT;
}
