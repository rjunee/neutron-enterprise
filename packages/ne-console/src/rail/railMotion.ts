// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §4.4 (docs/design/ne-console-motion-design.md) — THE APPROVALS BADGE TICK.
//
// "A number that changes silently in the periphery is invisible; one tick marks
// the moment." The badge scales 1→1.06→1 over --motion-snap exactly when the
// pending count RISES between two polled readings, and at no other time: not on
// first load (law 4 cl. 1 — a queue that merely already existed is not an
// event), not on a decrease or an unchanged reading (cl. 2 — the tick depicts
// new work arriving, and a queue that blinks all day trains the approver to
// ignore it), and never continuously (design §6.10).
//
// The counterpart effect — §4.3's rail transit pulse — needs no hook at all:
// NavRail renders it directly from the shared audit poll's single-batch
// `transits`, so its cause-or-absent guarantee is inherited from useAuditPoll
// rather than re-derived here.

import { useState } from "react";

/**
 * Monotone tick epoch over a polled count: increments exactly when `count`
 * rises from one KNOWN reading to a higher KNOWN reading.
 *
 * `null` means unknown (loading / refused / error — usePendingApprovals's
 * contract for every non-ok status). A null on EITHER side of a change never
 * ticks: the first reading after mount, and the first reading after an outage,
 * re-baseline silently instead of fabricating an "increase" the client never
 * observed (law 4 cl. 1–2). A genuine 0 is a known reading, so an empty queue
 * that receives work (0 → n) ticks — that is precisely new work arriving.
 *
 * Derived during render, like approvals/verdictMotion.ts's useVerdictArrival:
 * setState in render makes React discard the pass and re-render immediately,
 * so the caller is never a render stale. Nothing gates on the animation this
 * feeds (cl. 7) — the count renders in the same frame whether or not it ticks;
 * the epoch only decides how the already-final numeral is dressed.
 */
export function useCountTick(count: number | null): number {
  const [seen, setSeen] = useState({ count, epoch: 0 });
  if (seen.count !== count) {
    const rose = seen.count !== null && count !== null && count > seen.count;
    setSeen({ count, epoch: rose ? seen.epoch + 1 : seen.epoch });
  }
  return seen.epoch;
}
