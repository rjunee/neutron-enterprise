// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §4 — the tile derivations. The rules under test are the honesty rules: a
// refused read is OMITTED, a failed read is the em dash, and neither is ever a
// number.

import { describe, expect, it } from "vitest";
import type {
  ChainVerificationView,
  ConnectStatusView,
  PendingApprovalView,
  PostureView,
  VerificationBasis,
} from "../api/types";
import type { ChainLatch } from "../audit/chainVerdict";
import {
  applyVerification,
  BREAK_CONFIRMATIONS,
  chainReading,
  INITIAL_LATCH,
} from "../audit/chainVerdict";
import { approvalsTile, auditTile, controlsTile, plural, sourcesTile } from "./homeTiles";
import type { GovernedRead } from "./useGovernedRead";

const NOW = Date.parse("2026-07-07T12:00:00Z");
const lane = <T>(status: GovernedRead<T>["status"], data: T | null = null): GovernedRead<T> => ({
  status,
  data,
  reload: () => {},
});

const approval = (requested_at: string): PendingApprovalView => ({
  request_id: `req-${requested_at}`,
  action: "crm.create_note",
  tier: 2,
  principal_id: "agent://sales-drafts",
  requested_at,
  sla_deadline: "2026-07-07T13:00:00Z",
  expires_at: "2026-07-08T12:00:00Z",
  escalated: false,
});

describe("plural", () => {
  it("agrees with itself about the noun", () => {
    expect(plural(1, "record")).toBe("1 record");
    expect(plural(3, "record")).toBe("3 records");
    expect(plural(0, "host")).toBe("0 hosts");
  });

  it("groups thousands at the §2.7 threshold", () => {
    expect(plural(4812, "record")).toBe("4812 records");
    expect(plural(48120, "record")).toBe("48,120 records");
  });
});

describe("approvalsTile", () => {
  it("counts the pending rows and dates the oldest one", () => {
    expect(
      approvalsTile(
        lane("ok", {
          approvals: [approval("2026-07-07T11:50:00Z"), approval("2026-07-07T10:55:00Z")],
        }),
        NOW,
      ),
    ).toEqual({ kind: "ok", value: "2 pending", sub: "oldest 1 h ago" });
  });

  it("omits the sub-line at zero — there is no oldest of nothing", () => {
    expect(approvalsTile(lane("ok", { approvals: [] }), NOW)).toEqual({
      kind: "ok",
      value: "0 pending",
    });
  });

  it("is HIDDEN when refused and DASHED when it failed — never a zero", () => {
    expect(approvalsTile(lane("refused"), NOW)).toEqual({ kind: "hidden" });
    expect(approvalsTile(lane("error"), NOW)).toEqual({ kind: "error" });
    expect(approvalsTile(lane("loading"), NOW)).toEqual({ kind: "loading" });
  });
});

// ---------------------------------------------------------------------------
// §4 AUDIT TILE — I3: the Home tile resolves its word through `chainReading`.
//
// It did not, and the invariant in audit/chainVerdict.ts asserted that it did.
// `auditTile` branched on ONE read's raw `verification.valid`, so it saw
// neither the latch nor the run — and App.tsx renders the rail chip and this
// page in ONE viewport, so both defects below put two opposite words about one
// chain an inch apart on the first screen of the product.
// ---------------------------------------------------------------------------

const read = (
  valid: boolean,
  over: Partial<ChainVerificationView> = {},
): ChainVerificationView => ({
  valid,
  basis: "full-chain",
  contradiction: false,
  checked: 12480,
  first_break_seq: null,
  first_break_event_id: null,
  reason: valid ? null : "head mismatch (truncated or forged tail)",
  detail: valid ? "chain intact" : "",
  ...over,
});

/** The poll's proof: the rows appended since an anchor a full walk blessed up
 * to fifteen minutes ago. It STRUCTURALLY CANNOT see a prefix row tampered
 * after that blessing (chainVerdict.ts I4) — which is the whole reason it must
 * not be allowed to answer this tile on its own. */
const anchoredPass = read(true, { basis: "anchored-extension", detail: "extends anchor 12,455" });

/** /trace's unbounded walk, disagreeing at a tampered prefix row. */
const fullWalkBreak = (seq: number, basis: VerificationBasis = "full-chain") =>
  read(false, { basis, checked: 12479, first_break_seq: seq, first_break_event_id: "evt" });

/** Fold `n` reads of the same verdict into one latch, the way the provider does. */
function fold(n: number, verification: ChainVerificationView, from: ChainLatch = INITIAL_LATCH) {
  let latch = from;
  for (let i = 0; i < n; i += 1) latch = applyVerification(latch, verification, `t${i}`);
  return latch;
}

describe("auditTile", () => {
  it("renders the server's passing verdict verbatim, with that read's own count", () => {
    const latch = applyVerification(INITIAL_LATCH, read(true, { checked: 4812 }), "t0");
    expect(auditTile("ok", latch.verdict, read(true, { checked: 4812 }), latch.observed)).toEqual({
      kind: "ok",
      value: "Valid",
      sub: "4812 records",
    });
  });

  it("names the CONFIRMED break's entry once the latch stands", () => {
    const latch = fold(BREAK_CONFIRMATIONS, fullWalkBreak(4812));
    expect(latch.verdict).toBe("break");
    expect(auditTile("ok", latch.verdict, fullWalkBreak(4812), latch.observed)).toEqual({
      kind: "ok",
      value: "Break",
      sub: "at entry 4812",
    });
  });

  // -------------------------------------------------------------------------
  // I3, FAILURE 1 — a break CONFIRMED on /trace, absent from Home.
  // -------------------------------------------------------------------------
  it("states a break the poll's own proof CANNOT SEE, instead of that proof's Valid", () => {
    // A prefix row is tampered AFTER the anchor was blessed. /trace's unbounded
    // walk disagrees three times and latches; the rail chip reads "Chain break
    // at entry 4812". The shared poll meanwhile answers `valid` off the anchored
    // extension — the one proof structurally blind to that tamper — and it is
    // that read Home holds.
    const latch = fold(BREAK_CONFIRMATIONS, fullWalkBreak(4812));
    expect(latch.latched).toBe(true);
    const tile = auditTile("ok", latch.verdict, anchoredPass, latch.observed);

    expect(tile).toEqual({ kind: "ok", value: "Break", sub: "at entry 4812" });
    // The shipped sentence, named so the guard bites the exact defect.
    expect(tile).not.toEqual({ kind: "ok", value: "Valid", sub: "12,480 records" });
    // And it agrees with the chip and both strips BY CONSTRUCTION, not by luck.
    expect(chainReading(latch.verdict, anchoredPass, latch.observed).word).toBe("BREAK");
  });

  // -------------------------------------------------------------------------
  // I3, FAILURE 2 — one disagreeing read rendered as a break, unsuppressed.
  // -------------------------------------------------------------------------
  it("a SINGLE disagreeing read is Re-checking, never a located Break", () => {
    const latch = fold(1, fullWalkBreak(7830));
    expect(latch.verdict).toBe("reverifying");
    expect(latch.run).toBe(1);
    const tile = auditTile("ok", latch.verdict, fullWalkBreak(7830), latch.observed);

    expect(tile).toEqual({ kind: "ok", value: "Re-checking", sub: "not confirmed yet" });
    // The shipped sentence: a break the console had zero confirmations for.
    expect(tile).not.toEqual({ kind: "ok", value: "Break", sub: "at entry 7830" });
  });

  it("carries NO record count while re-checking — `checked` there is a FAILED verdict's reach", () => {
    const latch = fold(BREAK_CONFIRMATIONS - 1, fullWalkBreak(7830));
    const tile = auditTile("ok", latch.verdict, fullWalkBreak(7830), latch.observed);
    expect(tile).toMatchObject({ kind: "ok" });
    expect(JSON.stringify(tile)).not.toContain("12,479");
    expect(JSON.stringify(tile)).not.toContain("record");
  });

  it("a break the server did not LOCATE says so, rather than dropping its sub-line", () => {
    // The CONTRADICTION verdict refuses while the `verify_chain` beneath it
    // passes, so it carries no `first_break_seq` — and it latches on SIGHT.
    const contradiction = read(false, {
      contradiction: true,
      reason: null,
      detail: "not the ledger this process verified",
    });
    const latch = applyVerification(INITIAL_LATCH, contradiction, "t0");
    expect(latch.verdict).toBe("break");
    expect(auditTile("ok", latch.verdict, contradiction, latch.observed)).toEqual({
      kind: "ok",
      value: "Break",
      sub: "entry not named",
    });
  });

  // -------------------------------------------------------------------------
  // I3's second clause — an ABSENT read may not subtract a confirmed break.
  // -------------------------------------------------------------------------
  it("keeps a confirmed break through a 502'd poll instead of dashing it away", () => {
    const latch = fold(BREAK_CONFIRMATIONS, fullWalkBreak(4812));
    expect(auditTile("error", latch.verdict, null, latch.observed)).toEqual({
      kind: "ok",
      value: "Break",
      sub: "at entry 4812",
    });
  });

  it("keeps a confirmed break for a principal whose audit poll is REFUSED", () => {
    // `observe` folds /trace's own read into the SAME latch, so a key without
    // the ops-gated poll can still confirm a break — and the rail chip names it
    // for that key. Hiding the tile under `degraded` first would have taken the
    // fact off this screen while the chip an inch above still stated it.
    const latch = fold(BREAK_CONFIRMATIONS, fullWalkBreak(4812));
    expect(auditTile("degraded", latch.verdict, null, latch.observed)).toEqual({
      kind: "ok",
      value: "Break",
      sub: "at entry 4812",
    });
  });

  it("CONTROL: with nothing latched, refused still hides, loading still skeletons, failed still dashes", () => {
    expect(auditTile("degraded", "unknown", null, [])).toEqual({ kind: "hidden" });
    expect(auditTile("loading", "unknown", null, [])).toEqual({ kind: "loading" });
    expect(auditTile("error", "unknown", null, [])).toEqual({ kind: "error" });
    // An `ok` poll that produced no verification is still an absence, not a Valid.
    expect(auditTile("ok", "unknown", null, [])).toEqual({ kind: "error" });
  });

  it("CONTROL: a sweep that withdraws the latch returns the tile to Valid", () => {
    const latch = fold(BREAK_CONFIRMATIONS, fullWalkBreak(4812));
    const swept = applyVerification(
      { ...latch, verdict: "valid", run: 0, latched: false, latchedBasis: null, runBasis: null },
      read(true, { checked: 12481 }),
      "after",
    );
    expect(auditTile("ok", swept.verdict, read(true, { checked: 12481 }), swept.observed)).toEqual({
      kind: "ok",
      value: "Valid",
      sub: "12,481 records",
    });
  });

  it("the tile's word IS `chainReading`'s word, for every state the latch reaches", () => {
    // The structural half of I3: one mapping, one word. If these ever come
    // apart, Home and the chip can state opposite things about one chain again.
    const cases: Array<[ChainLatch, ChainVerificationView | null]> = [
      [INITIAL_LATCH, read(true)],
      [fold(1, fullWalkBreak(7830)), fullWalkBreak(7830)],
      [fold(BREAK_CONFIRMATIONS, fullWalkBreak(4812)), anchoredPass],
      [fold(BREAK_CONFIRMATIONS, fullWalkBreak(4812)), null],
      [fold(2, fullWalkBreak(7830)), anchoredPass],
    ];
    const WORD: Record<string, string> = {
      VALID: "Valid",
      "RE-VERIFYING": "Re-checking",
      BREAK: "Break",
    };
    for (const [latch, verification] of cases) {
      const { word } = chainReading(latch.verdict, verification, latch.observed);
      const tile = auditTile(
        verification === null ? "error" : "ok",
        latch.verdict,
        verification,
        latch.observed,
      );
      if (word === null) {
        expect(tile.kind).not.toBe("ok");
        continue;
      }
      expect(tile).toMatchObject({ kind: "ok", value: WORD[word] });
    }
  });
});

describe("controlsTile", () => {
  const posture = (over: Partial<PostureView>): PostureView => ({
    constraints: [
      { id: "C1", short: "one", enforcement: "", attested: true, mode: "static", evidence: [] },
      { id: "C2", short: "two", enforcement: "", attested: false, mode: "static", evidence: [] },
    ],
    air_gap: true,
    egress_allowlist: [],
    model_lanes: [],
    config_hash: "a".repeat(64),
    config_hash_history: [],
    attestation_source: "",
    air_gap_pending: null,
    ...over,
  });

  it("counts the attested constraints against the served total", () => {
    expect(controlsTile("ok", posture({}))).toEqual({
      kind: "ok",
      value: "1/2 verified",
      sub: "Air gap on",
    });
  });

  it("names the sanctioned hosts when the air gap is off", () => {
    expect(
      controlsTile("ok", posture({ air_gap: false, egress_allowlist: ["api.example.com"] })),
    ).toMatchObject({ sub: "1 host allowed" });
    expect(
      controlsTile("ok", posture({ air_gap: false, egress_allowlist: ["a", "b"] })),
    ).toMatchObject({ sub: "2 hosts allowed" });
  });

  it("dashes rather than remembering a posture it could not read", () => {
    expect(controlsTile("error", null)).toEqual({ kind: "error" });
    expect(controlsTile("ok", null)).toEqual({ kind: "error" });
  });
});

describe("sourcesTile", () => {
  const status = (connectors: ConnectStatusView["connectors"]): ConnectStatusView => ({
    connectors,
    quarantine_total: null,
    quarantine_reasons: [],
  });
  const connector = (id: string, acl_mirror: "ok" | "degraded" | null) => ({
    id,
    kind: id,
    posture: "live" as const,
    cursor_age_s: 10,
    last_checked_age_s: 10,
    acl_mirror,
    last_sync_seq: 1,
    quarantined_count: 0,
    sync_allowed: false,
  });

  it("counts what is configured and what is degraded", () => {
    expect(
      sourcesTile(lane("ok", status([connector("m365", "ok"), connector("jira", "degraded")]))),
    ).toEqual({ kind: "ok", value: "2 configured", sub: "1 degraded" });
  });

  it("omits the sub-line when nothing is degraded, and never counts a null as one", () => {
    expect(
      sourcesTile(lane("ok", status([connector("m365", "ok"), connector("new", null)]))),
    ).toEqual({ kind: "ok", value: "2 configured" });
  });

  it("does not call a configured-but-never-checked lane connected", () => {
    expect(
      sourcesTile(
        lane(
          "ok",
          status([
            {
              id: "dead-credential",
              kind: "m365",
              posture: "live",
              cursor_age_s: null,
              last_checked_age_s: null,
              acl_mirror: null,
              last_sync_seq: null,
              quarantined_count: null,
              sync_allowed: false,
            },
          ]),
        ),
      ),
    ).toEqual({ kind: "ok", value: "1 configured" });
  });

  it("is hidden when the server refused the read", () => {
    expect(sourcesTile(lane("refused"))).toEqual({ kind: "hidden" });
  });
});

describe("controlsTile — a role boundary and an outage are different facts", () => {
  // The status popover and this tile both state, out loud, why posture is
  // missing. Before this, PostureContext collapsed 403 and unreachable into one
  // "error" state, so the popover rendered "Not visible to your role" whenever
  // the read had merely failed — or was still in flight. That is a false claim
  // about authorization, made by the surface the spec designates as the one
  // home of global state.
  //
  // Asserting the three outcomes are DISTINCT, not merely that each is
  // non-empty: collapsing any two of them back together is the bug, and an
  // assertion that each returns "something" survives that collapse.
  it("distinguishes loading, refused and unreachable", () => {
    const loading = controlsTile("loading", null);
    const refused = controlsTile("refused", null);
    const unreachable = controlsTile("error", null);

    expect(loading.kind).toBe("loading");
    expect(refused.kind).toBe("hidden");
    expect(unreachable.kind).toBe("error");

    const kinds = [loading.kind, refused.kind, unreachable.kind];
    expect(new Set(kinds).size).toBe(3);
  });

  it("still renders the real reading when posture arrives", () => {
    // The CONTROL. Without it, every assertion above is satisfied by a function
    // that never returns "ok" — which would hide the tile permanently and look,
    // on screen, exactly like the feature working.
    const posture = {
      constraints: [
        { id: "C1", short: "s", enforcement: "e", evidence: [], attested: true, mode: "auto" },
        { id: "C2", short: "s", enforcement: "e", evidence: [], attested: false, mode: "auto" },
      ],
      air_gap: true,
      egress_allowlist: [],
      model_lanes: [],
      config_hash: "a".repeat(64),
      config_hash_history: [],
    } as unknown as PostureView;

    const tile = controlsTile("ok", posture);
    expect(tile.kind).toBe("ok");
    if (tile.kind === "ok") {
      expect(tile.value).toBe("1/2 verified");
      expect(tile.sub).toBe("Air gap on");
    }
  });
});
