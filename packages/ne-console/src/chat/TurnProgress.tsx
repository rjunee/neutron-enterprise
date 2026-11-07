// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — THE IN-FLIGHT TURN, MEASURED.
//
// A governed turn with thinking enabled runs 25–30 s (a question) to ~2 min
// (multi-hop), and this screen used to show a blinking cursor and nothing else
// for that entire window. The owner's words: "why can't it show progress and
// that it's not hung?"
//
// What this supplies is measured, never simulated — this product's claim is that
// what you see is what happened, and a progress bar that lies about a 30-second
// wait is a small version of the thing it sells against:
//
//   * the elapsed seconds are this machine's clock since the question was sent
//     (Search's `Searching… {n}s` counter, same discipline);
//   * the server-activity age is the time since the last thing — frame or
//     keep-alive — actually arrived on the wire, which is the difference
//     between "long turn" and "hung";
//   * the turn's hops are the work band ABOVE this line, whose summary is the
//     server's own step ledger: exactly as many steps as the server reported,
//     never a step list running ahead of it.
//
// THE CLOCK LIVES HERE AND ONLY HERE. The transcript module owns no timer and
// cannot produce a substitute for either number: it takes the two measurements
// this hook makes and draws them, and renders the em dash when there are none.
//
// No spinner, no percentage, no animation (motion law 1: a screen at rest runs
// zero animations — and nothing here depicts activity that did not happen; the
// numbers are true at every tick). Stop lives in the composer the whole time.

import { useEffect, useState } from "react";
import type { TranscriptProgress } from "../transcript";
import { ASK_NO_SERVER_WORD, askServerWordAge } from "./askCopy";

/** What the screen measured about the turn in flight. */
export interface TurnProgressFacts {
  /** When the in-flight turn was sent (stamped in the submit handler). */
  startedAt: number;
  /** When the last frame or keep-alive arrived; null before the first. */
  lastSignalAt: number | null;
}

/** A 1 Hz clock, running only while a turn is in flight. The clock is read in a
 * timer callback and held in state — never during a render (the Search page's
 * counter pattern). An idle thread starts no interval at all.
 *
 * The zero-delay first read is what keeps the FIRST second of a turn honest.
 * This hook outlives any one turn — the thread mounts once and answers many
 * questions — so without it a second question would open against whatever
 * reading the previous turn's last tick left behind. It is deferred rather than
 * taken synchronously in the effect so it cannot cascade a render. */
function useNow(live: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    const first = setTimeout(() => setNow(Date.now()), 0);
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, [live]);
  return now;
}

/**
 * The measured in-flight line, as the transcript's view model.
 *
 * Called unconditionally — the hook order never depends on whether a turn is
 * running. `null` in gives `null` out and no timer.
 */
export function useTurnProgress(facts: TurnProgressFacts | null): TranscriptProgress | null {
  const now = useNow(facts !== null);
  if (facts === null) return null;
  return {
    elapsedSeconds: Math.max(0, Math.floor((now - facts.startedAt) / 1000)),
    liveness:
      facts.lastSignalAt === null
        ? ASK_NO_SERVER_WORD
        : askServerWordAge(Math.max(0, Math.floor((now - facts.lastSignalAt) / 1000))),
  };
}
