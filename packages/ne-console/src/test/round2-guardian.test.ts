// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// ROUND-2 GUARDIANS — the four defects that a reviewer could see but a unit test
// could not, locked so they cannot come back.
//
// Each one is written to BITE: every assertion is paired with a control that
// proves the assertion is not vacuously true, and — where the defect was a
// specific shipped value or string — with a case reproducing that value and
// asserting it FAILS. A guard that passes on the code that shipped the bug is
// not a guard.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { classifyAnswer, hasUnclassifiedFrames } from "../chat/answerFrames";
import {
  applyVerification,
  BREAK_CONFIRMATIONS,
  clearLatch,
  INITIAL_LATCH,
} from "../audit/chainVerdict";
import { classifyEvent, countRefusals, withoutAccessRefusals } from "../audit/eventClass";
import { egressReason, egressState } from "../posture/egress";
import { entitlementsOf } from "../auth/entitlements";
import { shortPrincipal } from "../primitives/PrincipalLabel";
import type { ChainVerificationView } from "../api/types";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");

// ===========================================================================
// 1 — THE ANSWER SURFACE MAY CONTAIN ANSWER TEXT AND NOTHING ELSE
// ===========================================================================

/** Verbatim from the reviewer's screenshot of the flagship answer pane. */
const SHIPPED_BODY = `<think>
Okay, the user is asking which supplier contracts renew this quarter. Let me check the available tools. I have read.kg.accounts_directory and read.knowledge.search.
</think>
{"tool_call": {"name": "read.knowledge.search", "arguments": {"query": "contracts renew this quarter"}}}`;

describe("round 2 — the answer surface never renders model internals", () => {
  it("strips the reasoning span and keeps it addressable for the Inspect layer", () => {
    const { answer, reasoning } = classifyAnswer(SHIPPED_BODY);
    expect(answer).not.toContain("<think>");
    expect(answer).not.toContain("Okay, the user is asking");
    expect(reasoning).toContain("Okay, the user is asking");
  });

  it("lifts a tool-call frame out of the answer and names it unclassified", () => {
    const { answer, frames } = classifyAnswer(SHIPPED_BODY);
    expect(answer).not.toContain("tool_call");
    expect(frames).toHaveLength(1);
    expect(frames[0]!.name).toBe("read.knowledge.search");
    // The frame survives VERBATIM for the RAW disclosure — never re-serialised.
    expect(frames[0]!.raw).toContain('"read.knowledge.search"');
  });

  it("marks a turn that emitted an undispatched tool call as failed", () => {
    // The console read GOVERNED TOOL CALLS 0 and TURNS THAT FAILED 0 while a tool
    // call was visibly on screen. A call that never reached the executor is not a
    // governed call — and the turn that emitted it did not complete.
    expect(hasUnclassifiedFrames(SHIPPED_BODY)).toBe(true);
  });

  it("CONTROL: an ordinary cited answer is untouched and is NOT marked failed", () => {
    const plain = "Three supplier contracts renew in Q3 [1], and one lapses in Q4 [2].";
    const { answer, reasoning, frames } = classifyAnswer(plain);
    expect(answer).toBe(plain);
    expect(reasoning).toBe("");
    expect(frames).toEqual([]);
    expect(hasUnclassifiedFrames(plain)).toBe(false);
  });

  it("CONTROL: prose that merely mentions a tool call is not eaten as a frame", () => {
    const prose = 'The agent may emit a {"tool" : "x"} shaped object, which is not a tool_call.';
    const { answer, frames } = classifyAnswer(prose);
    expect(frames).toEqual([]);
    expect(answer).toBe(prose);
  });

  it("holds back a still-streaming reasoning span rather than flashing it as answer", () => {
    const midStream = "<think>Let me check the available tools";
    const { answer, reasoning } = classifyAnswer(midStream);
    expect(answer).toBe("");
    expect(reasoning).toContain("Let me check");
  });

  it("no source file renders message.text directly into the answer element", () => {
    // The structural half: a future edit that puts the raw stream body back into
    // answer typography defeats every assertion above.
    const list = readFileSync(join(SRC_DIR, "chat", "MessageList.tsx"), "utf-8");
    expect(list).toContain("classifyAnswer");
    // The assistant branch renders `answer`, never `message.text`.
    expect(list).not.toMatch(/ne-message__text">\s*\{message\.text\}\s*\n\s*\{message\.streaming/);
  });

  it("the clipboard export derives from the same classification as the render", () => {
    const exp = readFileSync(join(SRC_DIR, "chat", "AnswerExport.tsx"), "utf-8");
    expect(exp).toContain("classifyAnswer");
    expect(exp).not.toContain("message.text.trim()");
  });
});

// ===========================================================================
// 2 — A VERDICT THAT CLEARS ITSELF IS NOT TAMPER-EVIDENCE
// ===========================================================================

function verdict(valid: boolean, seq: number | null = null): ChainVerificationView {
  return {
    valid,
    // THE SERVER SENDS A BASIS ON EVERY VERDICT and this fixture used to omit
    // it behind the cast below. That is not a harmless gap: `applyVerification`
    // now compares the strength of a passing read against the run it is about
    // to erase, so a basis-less fixture asked the reducer a question the wire
    // cannot ask — and the alternating CONTROL below went red for a reason that
    // had nothing to do with the rule it guards. These reads are the
    // read-during-append race, which the page read answers with a full walk.
    basis: "full-chain",
    contradiction: false,
    checked: 8162,
    first_break_seq: seq,
    first_break_event_id: null,
    reason: valid ? null : "head mismatch (truncated or forged tail)",
    detail: "server-computed",
  } as ChainVerificationView;
}

describe("round 2 — the chain verdict latches and never flickers", () => {
  it("a single disagreeing poll is RE-VERIFYING, not BREAK", () => {
    // The measured defect: one straddled read flipped the global header to
    // "⚠ BREAK seq 7830", and the next poll flipped it back to "● VALID".
    const after = applyVerification(INITIAL_LATCH, verdict(false, 7830), "t0");
    expect(after.verdict).toBe("reverifying");
    expect(after.latched).toBe(false);
  });

  it(`promotes to BREAK only after ${BREAK_CONFIRMATIONS} consecutive agreeing polls`, () => {
    let latch = INITIAL_LATCH;
    for (let i = 0; i < BREAK_CONFIRMATIONS - 1; i += 1) {
      latch = applyVerification(latch, verdict(false, 7830), `t${i}`);
      expect(latch.verdict).toBe("reverifying");
    }
    latch = applyVerification(latch, verdict(false, 7830), "tN");
    expect(latch.verdict).toBe("break");
    expect(latch.latched).toBe(true);
  });

  it("a passing poll resets the run but CANNOT clear a confirmed break", () => {
    let latch = INITIAL_LATCH;
    for (let i = 0; i < BREAK_CONFIRMATIONS; i += 1) {
      latch = applyVerification(latch, verdict(false, 7830), `t${i}`);
    }
    expect(latch.verdict).toBe("break");
    latch = applyVerification(latch, verdict(true), "later");
    expect(latch.verdict).toBe("break");
    expect(latch.latched).toBe(true);
  });

  it("only an explicit re-verification sweep clears it, and only if the sweep passes", () => {
    let latch = INITIAL_LATCH;
    for (let i = 0; i < BREAK_CONFIRMATIONS; i += 1) {
      latch = applyVerification(latch, verdict(false, 7830), `t${i}`);
    }
    // A FAILING sweep re-confirms rather than clearing.
    expect(clearLatch(latch, verdict(false, 7830)).latched).toBe(true);
    const swept = clearLatch(latch, verdict(true));
    expect(swept.latched).toBe(false);
    expect(swept.verdict).toBe("valid");
  });

  it("records every observed break, including the transient ones", () => {
    const latch = applyVerification(INITIAL_LATCH, verdict(false, 7830), "t0");
    expect(latch.observed).toHaveLength(1);
    expect(latch.observed[0]!.confirmed).toBe(false);
    expect(latch.observed[0]!.first_break_seq).toBe(7830);
  });

  it("CONTROL: an all-valid sequence never reaches BREAK or RE-VERIFYING", () => {
    let latch = INITIAL_LATCH;
    for (let i = 0; i < 10; i += 1) latch = applyVerification(latch, verdict(true), `t${i}`);
    expect(latch.verdict).toBe("valid");
    expect(latch.latched).toBe(false);
    expect(latch.observed).toEqual([]);
  });

  it("CONTROL: alternating verdicts never promote — the run has to be consecutive", () => {
    let latch = INITIAL_LATCH;
    for (let i = 0; i < 8; i += 1) {
      latch = applyVerification(latch, verdict(i % 2 === 0 ? false : true), `t${i}`);
    }
    expect(latch.verdict).not.toBe("break");
    expect(latch.latched).toBe(false);
  });

  it("I3: no surface reads a chain verdict's raw fields for a word of its own", () => {
    // THE STRUCTURAL HALF OF I3, and it is here because the invariant was a
    // FALSE CLAIM while nothing checked it. `landing/homeTiles.ts` branched on
    // `verification.valid` and printed `first_break_seq` directly, so Home
    // rendered "Valid · 12,480 records" under a rail chip — the same viewport,
    // App.tsx — reading "Chain break at entry 4812", and rendered a break off a
    // SINGLE disagreeing read with zero confirmations behind it. Four consumers
    // went through `chainReading` and the fifth was never enumerated.
    //
    // So: outside the two files that OWN the raw verdict, no source file may
    // touch `.valid` or `first_break_seq` at all. Everything else takes the
    // latched reading.
    const RAW = /\.valid\b|\bfirst_break_seq\b/;
    // Comments quote the wire constantly (the reducer's own reproduction
    // transcript is in a comment), and a comment renders nothing.
    const stripComments = (source: string) =>
      source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
    const OWNERS = [
      join("audit", "chainVerdict.ts"), // the reducer and the one mapping
      join("audit", "spine.ts"), // `chainBreakSeq`, which the mapping calls
      join("api", "types.ts"), // the wire's own declaration
    ];
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
          if (entry === "test") continue;
          walk(full);
          continue;
        }
        if (!entry.endsWith(".tsx") && !entry.endsWith(".ts")) continue;
        if (entry.endsWith(".test.tsx") || entry.endsWith(".test.ts")) continue;
        if (OWNERS.some((owner) => full.endsWith(owner))) continue;
        if (RAW.test(stripComments(readFileSync(full, "utf-8")))) offenders.push(entry);
      }
    };
    walk(SRC_DIR);
    expect(offenders).toEqual([]);
    // Self-test: the pattern bites BOTH expressions that shipped in the tile,
    // and does not bite the comments that legitimately quote them.
    expect(RAW.test(stripComments("  if (verification.valid) {"))).toBe(true);
    expect(
      RAW.test(stripComments("    sub: `at entry ${formatCount(verification.first_break_seq)}`,")),
    ).toBe(true);
    expect(RAW.test(stripComments("// it used to render this poll's raw verification.valid"))).toBe(
      false,
    );
    expect(RAW.test(stripComments("/* first_break_seq is not decoration */"))).toBe(false);
    // And the URL guard holds: `//` inside a string is not a comment opener.
    expect(RAW.test(stripComments('const u = "https://x/first_break_seq";'))).toBe(true);
  });
});

// ===========================================================================
// 3 — ONE DERIVATION PER STATE WORD
// ===========================================================================

describe("round 2 — two readouts of the same field cannot disagree", () => {
  it("an empty allowlist is SEALED, which is the mapping the landing tile inverted", () => {
    expect(egressState({ air_gap: false, egress_allowlist: [] })).toBe("SEALED");
    expect(egressState({ air_gap: true, egress_allowlist: [] })).toBe("SEALED");
    expect(egressState({ air_gap: true, egress_allowlist: ["api.anthropic.com"] })).toBe("SEALED");
  });

  it("CONTROL: a sanctioned host with the air gap off is OPEN", () => {
    expect(egressState({ air_gap: false, egress_allowlist: ["api.anthropic.com"] })).toBe("OPEN");
  });

  it("the explanatory sentence is derived from the same facts as the word", () => {
    expect(egressReason({ air_gap: false, egress_allowlist: [] })).toMatch(/No external host/);
    expect(egressReason({ air_gap: true, egress_allowlist: [] })).toMatch(/Air gap/);
    expect(egressReason({ air_gap: false, egress_allowlist: ["a", "b"] })).toMatch(/2 sanctioned/);
  });

  it("no page derives the egress state word for itself", () => {
    // The structural half of the guard. The landing tile read `air_gap` alone and
    // rendered OPEN on a deployment /posture rendered SEALED, from the same
    // field, on the same load.
    const INVERTED = /air_gap\s*\?\s*"SEALED"\s*:\s*"OPEN"/;
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
          if (entry === "test") continue;
          walk(full);
          continue;
        }
        if (!entry.endsWith(".tsx") && !entry.endsWith(".ts")) continue;
        if (entry.endsWith(".test.tsx") || entry.endsWith(".test.ts")) continue;
        if (full.endsWith(join("posture", "egress.ts"))) continue;
        if (INVERTED.test(readFileSync(full, "utf-8"))) offenders.push(entry);
      }
    };
    walk(SRC_DIR);
    expect(offenders).toEqual([]);
    // Self-test: the pattern bites the exact expression that shipped.
    expect(INVERTED.test('state={posture.air_gap ? "SEALED" : "OPEN"}')).toBe(true);
  });
});

// ===========================================================================
// 4 — AN ACCESS REFUSAL IS NOT A DECISION ABOUT AN ACTION
// ===========================================================================

const REFUSAL = { event_type: "api.audit", action: "api.audit.unauthorized" };
const APPROVAL_REFUSAL = { event_type: "api.approval", action: "api.approval.unauthorized" };
const DISPATCH = { event_type: "runtime", action: "subagent.dispatch" };
const PDP = { event_type: "govern", action: "pdp.decide" };
const GATEWAY = { event_type: "gateway", action: "ne.gateway.complete" };

describe("round 2 — the ledger separates refusals from decisions", () => {
  it("classes an *.unauthorized row as an access refusal, never a decision", () => {
    expect(classifyEvent(REFUSAL)).toBe("access-refusal");
    expect(classifyEvent(APPROVAL_REFUSAL)).toBe("access-refusal");
  });

  it("CONTROL: the real governance events are decisions and are never hidden", () => {
    expect(classifyEvent(DISPATCH)).toBe("decision");
    expect(classifyEvent(PDP)).toBe("decision");
    expect(classifyEvent(GATEWAY)).toBe("decision");
    const page = [REFUSAL, REFUSAL, DISPATCH, PDP, GATEWAY];
    expect(withoutAccessRefusals(page)).toEqual([DISPATCH, PDP, GATEWAY]);
    expect(countRefusals(page)).toBe(2);
  });

  it("CONTROL: an approval VERDICT is a decision even though it says 'approval'", () => {
    // The old filter was a substring match on "approval", which swept
    // api.approval.unauthorized under a heading reading "DECIDED — ON THE CHAIN"
    // with OUTCOME `denied`. The narrowing must not overshoot in the other
    // direction and hide the real verdicts.
    expect(classifyEvent({ event_type: "gate", action: "approval.approved" })).toBe("decision");
    expect(classifyEvent({ event_type: "gate", action: "approval.rejected" })).toBe("decision");
  });
});

// ===========================================================================
// 5 — THE SERVER ANSWERS "MAY I", NOT THE CLIENT
// ===========================================================================

describe("round 2 — entitlements are advisory and fail OPEN when unknown", () => {
  it("an unresolved principal refuses nothing — the console still asks the server", () => {
    const e = entitlementsOf({ status: "loading", principal: null });
    expect(e.known).toBe(false);
    expect(e.refused("ops")).toBe(false);
    expect(e.access("ops")).toBe("unknown");
    // …but it is PENDING: an answer is coming, so gated affordances render as
    // loading, never as permitted (the Approvals-flash fix). Only the in-flight
    // state may say so — see the two unadvised cases below.
    expect(e.pending).toBe(true);
  });

  it("a deployment that does not send entitlements behaves exactly as before", () => {
    const e = entitlementsOf({
      status: "ok",
      principal: { id: "per-0005", groups: ["all-employees"] },
    });
    expect(e.known).toBe(false);
    expect(e.refused("approvals")).toBe(false);
    // The answer ARRIVED and carried no advisory: nothing further is coming,
    // so this must not read as still-loading (a permanent skeleton is a lie).
    expect(e.pending).toBe(false);
  });

  it("a FAILED /v1/me read is unadvised, not forever-pending", () => {
    const e = entitlementsOf({ status: "error", principal: null });
    expect(e.known).toBe(false);
    expect(e.refused("ops")).toBe(false);
    expect(e.pending).toBe(false);
  });

  it("a served entitlement list is honoured in both directions", () => {
    const e = entitlementsOf({
      status: "ok",
      principal: { id: "approver", groups: ["approvers"], entitlements: ["approvals"] },
    });
    expect(e.refused("approvals")).toBe(false);
    expect(e.refused("ops")).toBe(true);
    expect(e.refused("steward")).toBe(true);
  });

  it("an EMPTY entitlement list is a real answer, not an unknown", () => {
    const e = entitlementsOf({
      status: "ok",
      principal: { id: "per-0006", groups: [], entitlements: [] },
    });
    expect(e.known).toBe(true);
    expect(e.refused("ops")).toBe(true);
    expect(e.pending).toBe(false);
  });
});

// ===========================================================================
// 6 — A RAW UUID IS NOT A NAME
// ===========================================================================

describe("round 2 — principal ids are shortened, never invented", () => {
  it("shortens the UUID the brief lists as a known defect", () => {
    expect(shortPrincipal("c21456b9-c8dc-4458-b393-bc3230db0081")).toBe("c21456b9");
  });

  it("CONTROL: an already-short id is left exactly as it is", () => {
    expect(shortPrincipal("per-0038")).toBe("per-0038");
    expect(shortPrincipal("alice@example.com")).toBe("alice@example.com");
  });
});
