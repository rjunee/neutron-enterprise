// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §4 — THE STATUS TILES, as pure derivations from the four served reads.
//
// THE THREE RULES THAT MAKE A TILE HONEST:
//   * REFUSED IS OMITTED. A tile whose read the server refused for this role is
//     not drawn at all — no lock, no "requires an ops key", no explanation of a
//     job that is not this person's. The home screen used to open with three red
//     refusal blocks; a role boundary is not news.
//   * ERRORED IS THE EM DASH. A read that failed renders "—" and "Couldn't load".
//     Never a zero, and never a remembered value: an unreachable posture read
//     must not render as twelve verified controls.
//   * LOADING IS A SKELETON in the tile's own geometry — never a number that
//     arrives and then changes.
//
// Every value below is a field named in §4's source table. There is nothing here
// this console computes on its own behalf: it counts rows the server sent and
// renders the verdict the server reached — and, for the chain, the verdict the
// server reached is the LATCHED one (`chainReading`), which is still entirely
// the server's verdicts, plus this console's count of how many times they have
// disagreed with each other. See `auditTile`.

import type {
  ChainVerificationView,
  ConnectStatusView,
  PendingApprovalView,
  PostureView,
} from "../api/types";
import type { ChainVerdict, ObservedBreak } from "../audit/chainVerdict";
import { chainReading } from "../audit/chainVerdict";
import { formatCount, relativeTime } from "../primitives/format";
import type { GovernedRead } from "./useGovernedRead";

/** What a tile is allowed to be. `hidden` is the refused arm. */
export type TileState =
  | { kind: "hidden" }
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "ok"; value: string; sub?: string };

/** "1 record" / "4,812 records" — grouped by §2.7's rule, agreeing with itself
 * about the noun. A template hole is not an excuse for "1 records". */
export function plural(n: number, noun: string): string {
  return `${formatCount(n)} ${noun}${n === 1 ? "" : "s"}`;
}

/** Approvals — `{n} pending`, sub `oldest {age}` (omitted at zero). */
export function approvalsTile(
  lane: GovernedRead<{ approvals: PendingApprovalView[] }>,
  now: number,
): TileState {
  if (lane.status === "refused") return { kind: "hidden" };
  if (lane.status === "loading") return { kind: "loading" };
  if (lane.status === "error" || lane.data === null) return { kind: "error" };
  const rows = lane.data.approvals;
  const oldest = rows.reduce<string | null>(
    (min, a) => (min === null || a.requested_at < min ? a.requested_at : min),
    null,
  );
  return {
    kind: "ok",
    value: `${formatCount(rows.length)} pending`,
    sub: oldest === null ? undefined : `oldest ${relativeTime(oldest, now)}`,
  };
}

/**
 * Audit log — `Valid` / `Re-checking` / `Break`, sub `{checked} records` /
 * `not confirmed yet` / `at entry {seq}`.
 *
 * THE WORD COMES FROM `chainReading`, THE SAME MAPPING /audit's strip, /trace's
 * strip and the rail chip render (chainVerdict.ts I3) — and it did not, which
 * made I3 a false claim about this file for as long as it stood. This tile
 * branched on ONE read's raw `verification.valid`, so it saw neither the latch
 * nor the run, and the two failures that followed are both on the landing
 * screen, inches under the rail chip that `AuthedLayout` renders in the same
 * viewport:
 *
 *   1. A prefix row tampered after the anchor was blessed. /trace's unbounded
 *      walk disagrees three times and LATCHES; the chip reads "Chain break at
 *      entry 4812". The shared poll meanwhile keeps answering `valid` off its
 *      `anchored-extension` proof — the one read that STRUCTURALLY CANNOT see
 *      that tamper (I4) — and this tile printed "Valid · 12,480 records" from
 *      it. One console, one chain, two opposite words, one glance apart.
 *   2. A SINGLE disagreeing read printed "Break · at entry 7830" here with zero
 *      confirmations behind it, then flipped back on the next poll — exactly
 *      the raw flicker `BREAK_CONFIRMATIONS` exists to suppress, rendered
 *      unsuppressed on the first screen of the product.
 *
 * THE ORDER IS THE RAIL CHIP'S, DELIBERATELY, because agreeing with it is the
 * whole point: a CONFIRMED BREAK IS DECIDED FIRST and consults no read (I3's
 * second clause — an absent read may not subtract a break the console
 * confirmed), then the server's refusal, then absence, then the latch's third
 * state, then the read in hand. Testing `degraded` first would have hidden the
 * tile out from under a standing break for a principal whose poll is refused
 * but whose /trace read latched one — `observe` folds that read into the same
 * latch, and the chip names the break for that principal — which is the same
 * defect this function is being fixed for, arriving from the other direction.
 *
 * RE-VERIFYING SAYS "Re-checking / not confirmed yet" AND CARRIES NO COUNT. The
 * console has a disagreement it has not answered; it is not entitled to the
 * word Valid, it has not earned the word Break, and `checked` under a
 * disagreeing read is the reach of a verdict that FAILED — printing it under
 * any word at all would read as a vouch for records nothing currently vouches
 * for. The sentence is the chip's own ("Checking again — this reading has not
 * been confirmed yet"), compressed to the 288px margin column.
 *
 * The verdict is still the SERVER's, rendered verbatim (C7). The console holds
 * no hash and compares nothing; what it adds here is only the console's own
 * count of how many times the server has disagreed with itself.
 */
export function auditTile(
  status: "loading" | "ok" | "degraded" | "error",
  verdict: ChainVerdict,
  verification: ChainVerificationView | null,
  observed: readonly ObservedBreak[],
): TileState {
  const { word, breakSeq } = chainReading(verdict, verification, observed);
  if (word === "BREAK") {
    return {
      kind: "ok",
      value: "Break",
      // A break the server did not locate (the CONTRADICTION verdict refuses
      // while the walk beneath it passes, so it names no entry) says so rather
      // than dropping to a bare "Break" that reads as a sentence with its
      // number still loading.
      sub: breakSeq === null ? "entry not named" : `at entry ${formatCount(breakSeq)}`,
    };
  }
  if (status === "degraded") return { kind: "hidden" };
  if (status === "loading") return { kind: "loading" };
  if (status === "error" || verification === null) return { kind: "error" };
  if (word === "RE-VERIFYING") {
    return { kind: "ok", value: "Re-checking", sub: "not confirmed yet" };
  }
  // VALID is answered only FROM a read in hand (`chainReading` rule 3), so the
  // count is the word's own read's, never a remembered one.
  return { kind: "ok", value: "Valid", sub: plural(verification.checked, "record") };
}

/**
 * Controls — `{attested}/{total} verified`, sub `Air gap on` / `{n} hosts allowed`.
 *
 * The shared posture read reports "error" for an unreachable endpoint AND for a
 * refusal, so this tile cannot distinguish them; the route is open to every
 * authenticated key, so the refused arm is unreachable in practice and the tile
 * degrades to "Couldn't load" rather than guessing at a role boundary.
 */
export function controlsTile(
  status: "loading" | "ok" | "refused" | "error",
  posture: PostureView | null,
): TileState {
  if (status === "loading") return { kind: "loading" };
  // A role boundary is not an outage, and an outage is not a role boundary.
  // The tile hides on refusal rather than showing a broken read, matching how
  // the rail hides a destination this key cannot open.
  if (status === "refused") return { kind: "hidden" };
  if (status === "error" || posture === null) return { kind: "error" };
  const attested = posture.constraints.filter((c) => c.attested).length;
  return {
    kind: "ok",
    value: `${formatCount(attested)}/${formatCount(posture.constraints.length)} verified`,
    sub: posture.air_gap
      ? "Air gap on"
      : `${plural(posture.egress_allowlist.length, "host")} allowed`,
  };
}

/** Sources — `{n} configured`, sub `{d} degraded` (omitted at zero).
 *
 * The status wire contains every configured lane, including one that has never
 * completed a check and therefore has all-null health. It does not carry a
 * current-connectivity verdict, so this tile must not invent one from row count
 * or stale health fields. */
export function sourcesTile(lane: GovernedRead<ConnectStatusView>): TileState {
  if (lane.status === "refused") return { kind: "hidden" };
  if (lane.status === "loading") return { kind: "loading" };
  if (lane.status === "error" || lane.data === null) return { kind: "error" };
  const connectors = lane.data.connectors;
  const degraded = connectors.filter((c) => c.acl_mirror === "degraded").length;
  return {
    kind: "ok",
    value: `${formatCount(connectors.length)} configured`,
    sub: degraded === 0 ? undefined : `${formatCount(degraded)} degraded`,
  };
}
