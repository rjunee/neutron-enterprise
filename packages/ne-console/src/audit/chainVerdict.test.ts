// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE LATCH IS THE AUTHORITY ONCE CONFIRMED — the rule, unit by unit.
//
// The screen-level halves of this live in audit/AuditPage.test.tsx,
// rail/StatusChip.test.tsx and trace/TracePage.test.tsx. This file pins the
// three things those cannot state as cleanly:
//
//   I1. `latched === true` ⟺ `verdict === "break"`, after EVERY fold. The two
//       came apart and the consequence was the worst defect this module can
//       have — a confirmed break, still latched in the data, rendering as
//       RE-VERIFYING, because nothing anywhere renders `latched`.
//   I2. Only `clearLatch`, on a PASSING sweep, moves `latched` back to false.
//   I3. `chainReading` answers BREAK from the latch BEFORE it consults a read,
//       so an absent read — a 502'd poll, a screen with no verification of its
//       own — cannot subtract a break this console confirmed.
//
// Plus the structural half: there is ONE latch. A second `useState(INITIAL_LATCH)`
// anywhere is how /trace and the rail chip came to render two of them side by
// side, off two reducers, disagreeing about one chain.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { ChainVerificationView } from "../api/types";
import type { ChainLatch } from "./chainVerdict";
import {
  applyVerification,
  BREAK_CONFIRMATIONS,
  chainReading,
  clearLatch,
  INITIAL_LATCH,
  MAX_OBSERVED,
} from "./chainVerdict";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");

function verdict(
  valid: boolean,
  seq: number | null = null,
  over: Partial<ChainVerificationView> = {},
): ChainVerificationView {
  return {
    valid,
    // The DEFAULT is the strongest proof, because that is what the reads this
    // file was written against were: a full unbounded `verify_chain` on every
    // page. `over` is how a test says otherwise — an anchored extension proof,
    // or a contradiction — and every rule that depends on WHICH proof ran is
    // exercised through it.
    basis: "full-chain",
    contradiction: false,
    checked: 12480,
    first_break_seq: seq,
    first_break_event_id: seq === null ? null : `evt-${seq}`,
    reason: valid ? null : "head mismatch (truncated or forged tail)",
    detail: valid ? "chain intact" : "chain broken",
    ...over,
  };
}

/** Fold a run of verdicts, `true` for a passing read. */
function fold(reads: readonly boolean[], from: ChainLatch = INITIAL_LATCH): ChainLatch {
  return reads.reduce(
    (latch, valid, i) => applyVerification(latch, verdict(valid, valid ? null : 4020), `t${i}`),
    from,
  );
}

/** A confirmed, standing break. */
function latched(): ChainLatch {
  const after = fold(Array<boolean>(BREAK_CONFIRMATIONS).fill(false));
  expect(after.latched).toBe(true);
  return after;
}

/** I1, stated as the assertion it is: the flag and the word are one fact. */
function expectI1(latch: ChainLatch): void {
  expect(latch.latched).toBe(latch.verdict === "break");
}

describe("the chain latch — a confirmed break is never withdrawn", () => {
  it("survives a passing read followed by a disagreeing one", () => {
    // THE REPRODUCED PAIR, and the defect this file exists for. The failing
    // branch computed `verdict: confirmed ? "break" : "reverifying"` from the
    // CURRENT run alone: one passing read reset the run to 0, and the next
    // disagreeing read then rendered at run 1 — as RE-VERIFYING — while
    // `latched` stayed true and no screen renders `latched`. Under the server's
    // reproduced ~50/50 read-during-append race, this pair arrives within two
    // polls.
    const after = fold([true, false], latched());
    expect(after.verdict).toBe("break");
    expect(after.latched).toBe(true);
    expectI1(after);
  });

  it("stays withdrawn-proof for the rest of the session, however the reads fall", () => {
    // The pair above is not a one-off state: every later passing read carries
    // the verdict forward, so a withdrawal is PERMANENT until a sweep. This
    // walks a long alternating run — the race's own signature — and requires
    // the word never to leave.
    let latch = latched();
    for (let i = 0; i < 40; i += 1) {
      latch = applyVerification(latch, verdict(i % 2 === 0, 4020), `t${i}`);
      expect(latch.verdict).toBe("break");
      expect(latch.latched).toBe(true);
      expectI1(latch);
    }
  });

  it("I1: the flag and the word are one fact, over every reachable sequence", () => {
    // Exhaustive over all 2^8 read sequences, from a fresh latch and from a
    // latched one. `latched` is the data and `verdict` is what renders; a state
    // in which they disagree is a state in which the screen lies.
    for (let mask = 0; mask < 256; mask += 1) {
      const reads = Array.from({ length: 8 }, (_, bit) => (mask & (1 << bit)) !== 0);
      expectI1(fold(reads));
      expectI1(fold(reads, latched()));
      // …and from a latched start the word is never anything BUT break.
      expect(fold(reads, latched()).verdict).toBe("break");
    }
  });

  it("I2: only a PASSING sweep clears it — and a sweep is still the way back", () => {
    const standing = latched();
    // A failing sweep re-confirms rather than clearing.
    expect(clearLatch(standing, verdict(false, 4020)).latched).toBe(true);
    // An absent read clears nothing.
    expect(clearLatch(standing, null).latched).toBe(true);
    // A passing sweep does, and the word moves with the flag.
    const swept = clearLatch(standing, verdict(true));
    expect(swept.latched).toBe(false);
    expect(swept.verdict).toBe("valid");
    expectI1(swept);
  });

  it("CONTROL: an unconfirmed disagreement is RE-VERIFYING, never BREAK", () => {
    // The failure in the other direction, and the one this branch shipped to
    // fix: a break must still cost BREAK_CONFIRMATIONS agreeing reads.
    for (let n = 1; n < BREAK_CONFIRMATIONS; n += 1) {
      const after = fold(Array<boolean>(n).fill(false));
      expect(after.verdict).toBe("reverifying");
      expect(after.latched).toBe(false);
      expectI1(after);
    }
    expect(fold(Array<boolean>(BREAK_CONFIRMATIONS).fill(false)).verdict).toBe("break");
  });
});

describe("chainReading — one mapping, and an absent read subtracts nothing", () => {
  const observed = latched().observed;

  it("I3: a confirmed break is decided BEFORE the read is consulted", () => {
    // The 502 case, verbatim: the poll nulls its verification on the error arm,
    // and every consumer that resolved its word from the read first fell
    // through to an absence — or, on /audit, to its own passing window read,
    // which published `● VALID · 12,480 records verified` over a chain this
    // console had confirmed broken.
    expect(chainReading("break", null, observed)).toEqual({
      word: "BREAK",
      breakSeq: 4020,
      breakReason: "head mismatch (truncated or forged tail)",
    });
    // A PASSING read is what a latched break comes to rest on once the race
    // clears, and it names no entry — so the entry comes off the confirmed
    // observation and the `/audit?seq=` deep link survives. The SENTENCE comes
    // off it too: the passing read's own `detail` is "chain intact", and that
    // is what /trace printed in a red alert under the BREAK pill.
    expect(chainReading("break", verdict(true), observed)).toEqual({
      word: "BREAK",
      breakSeq: 4020,
      breakReason: "head mismatch (truncated or forged tail)",
    });
    // DEFENSIVE ARM — `verdict(false, null)` is a wire `ne-audit` cannot emit
    // (`_break` takes `seq: int`; see `locates`). Pinned anyway because
    // `first_break_seq` is `int | None` on the dataclass and this type: if it
    // ever arrives, the break says so rather than inventing a seq.
    expect(chainReading("break", verdict(false, null), [])).toEqual({
      word: "BREAK",
      breakSeq: null,
      breakReason: "head mismatch (truncated or forged tail)",
    });
  });

  // -------------------------------------------------------------------------
  // A CONFIRMED, LOCATED BREAK IS NEVER SILENTLY DE-LOCATED.
  //
  // `breakSeq` is not decoration: /audit gates the red band (its only
  // `role="alert"`), the entry's BREAK pill, the dimmed tail and the severed
  // spine on it, and the chip's `/audit?seq=` deep link is it. A null seq under
  // a standing BREAK draws a WHOLE, VERIFIED CHAIN under the word — so every
  // route by which the located entry can go missing is a route by which the
  // console withdraws everything except the word.
  // -------------------------------------------------------------------------

  it("an UNCONFIRMED read at the moving head does not MOVE the confirmed break", () => {
    // DE-LOCATION'S OTHER HALF, AND THE ONLY ONE THAT NEEDS NOTHING BUT READS
    // THIS SERVER EMITS: not a break that loses its entry, but one that is
    // MOVED to an entry no confirmed read ever accused.
    //
    // A latched break is never withdrawn by a passing read — but the passing
    // read DOES reset the run, so the single disagreeing read after it lands at
    // run 1 and is recorded `confirmed: false` while the word stays BREAK off
    // the latch. The head-mismatch arm names the MOVING HEAD (`seq=head_seq`,
    // verify.py:509), so that read names a DIFFERENT entry — and reading the
    // location off the read in hand pointed /audit's red band, the entry's
    // BREAK pill, the dimmed tail, the severed spine and the chip's
    // `/audit?seq=` deep link at it, then back at 4020 on the next passing
    // poll, alternating for as long as the race lasted.
    let latch = latched();
    latch = applyVerification(latch, verdict(true), "t-pass");
    const moved = { ...verdict(false, 7838), reason: "head mismatch (records beyond head)" };
    latch = applyVerification(latch, moved, "t-transient");
    // The premise: BREAK still stands, and the read that named 7838 confirmed
    // nothing — the run it landed in is 1.
    expect(latch.verdict).toBe("break");
    expect(latch.observed[0]!.first_break_seq).toBe(7838);
    expect(latch.observed[0]!.confirmed).toBe(false);

    const reading = chainReading(latch.verdict, moved, latch.observed);
    expect(reading.breakSeq).toBe(4020);
    // …and the SENTENCE is that same observation's, so the entry and the reason
    // cannot be about two different records.
    expect(reading.breakReason).toBe("head mismatch (truncated or forged tail)");

    // It does not oscillate either: the next passing poll, and the transient
    // after that, rest on the same confirmed entry.
    latch = applyVerification(latch, verdict(true), "t-pass-2");
    expect(chainReading(latch.verdict, verdict(true), latch.observed).breakSeq).toBe(4020);
    const moved2 = { ...verdict(false, 7845), reason: "head mismatch (records beyond head)" };
    latch = applyVerification(latch, moved2, "t-transient-2");
    expect(chainReading(latch.verdict, moved2, latch.observed).breakSeq).toBe(4020);
  });

  it("a later CONFIRMED read that names no entry does not de-locate the break", () => {
    // THE FOURTH READ — a DEFENSIVE case, not a reproduced one. Three reads
    // locate the break at 4020 and confirm it; the fourth is invalid and names
    // no entry, which `_break` (verify.py:180) cannot produce and which an
    // earlier revision of this file wrongly called "the race's own documented
    // shape". Kept as a guard on the preference in `standingBreak`: were such a
    // read to arrive, `applyVerification` would mark it `confirmed` too (the
    // run is past BREAK_CONFIRMATIONS), so "the newest confirmed observation"
    // would be one whose seq is null.
    let latch = latched();
    latch = applyVerification(latch, verdict(false, null), "t-noseq");
    expect(latch.verdict).toBe("break");
    expect(latch.observed[0]!.confirmed).toBe(true);
    expect(latch.observed[0]!.first_break_seq).toBeNull();

    // The fallback is the newest confirmed observation THAT NAMES AN ENTRY.
    expect(chainReading("break", verdict(false, null), latch.observed).breakSeq).toBe(4020);
    // …and it survives the read going absent, and going passing, after that.
    expect(chainReading("break", null, latch.observed).breakSeq).toBe(4020);
    expect(chainReading("break", verdict(true), latch.observed).breakSeq).toBe(4020);
  });

  it("the observation that LOCATED the break is never aged out of the list", () => {
    // The same de-location, arriving later and by a different route — and this
    // route needs NOTHING BUT READS THE SERVER EMITS. Every read below names an
    // entry, because every invalid read does (`_break`, verify.py:180). The
    // version of this test that shipped fed `verdict(false, null)` in a loop
    // and so proved the guard only against a wire `ne-audit` cannot produce.
    //
    // A passing read RESETS THE RUN, so each failing read that follows one
    // lands at run 1 and is recorded `confirmed: false`. Unconfirmed
    // observations cannot locate, but they do take slots — and the
    // head-mismatch arm names the MOVING head (`seq=head_seq`, verify.py:509),
    // so no two of them are even the same entry. Twenty polls of that and the
    // observation that located 4020 is off the end of a newest-first cap: the
    // band, the pill, the dimmed tail, the severed spine and the `/audit?seq=`
    // deep link go with it, and the word BREAK stands alone.
    let latch = latched();
    for (let i = 0; i < MAX_OBSERVED + 5; i += 1) {
      latch = applyVerification(latch, verdict(true), `p${i}`);
      latch = applyVerification(latch, verdict(false, 7830 + i), `f${i}`);
      expect(latch.verdict).toBe("break");
    }
    expect(latch.observed.length).toBe(MAX_OBSERVED);
    // Every observation after the promotion is unconfirmed, so exactly one in
    // the list can still locate — and it is the retained one.
    expect(latch.observed.filter((o) => o.confirmed && !o.cleared)).toHaveLength(1);
    expect(latch.observed.filter((o) => o.first_break_seq === 4020)).toHaveLength(1);
    // The entry survives on the passing read the latch comes to rest on…
    expect(chainReading("break", verdict(true), latch.observed).breakSeq).toBe(4020);
    // …and on an absent one.
    expect(chainReading("break", null, latch.observed).breakSeq).toBe(4020);
  });

  it("a standing BREAK always names an entry, over every sequence of real reads", () => {
    // THE INVARIANT THE DEFENSIVE ARMS ARE DEFENSIVE AGAINST — stated once,
    // here, so the claim in `locates` is checked rather than asserted in prose.
    // `_break(*, seq: int, ...)` (verify.py:180) makes every invalid verdict
    // name an entry, so no sequence of reads this server can emit may leave a
    // confirmed break un-located. Anything that breaks this promotes the arms
    // in spine.ts, AuditPage, StatusChip and TracePage from unreachable to
    // reachable, and their sentences start rendering.
    for (let mask = 0; mask < 256; mask += 1) {
      let latch = latched();
      // The 8-read pattern is walked FOUR times, so the list passes MAX_OBSERVED
      // and the cap's retention rule is inside the invariant rather than beside
      // it: de-locating by eviction fails here too, not only in the test above.
      for (let i = 0; i < 8 * 4; i += 1) {
        const valid = (mask & (1 << (i % 8))) !== 0;
        // An invalid read names the moving head; a valid one names nothing.
        latch = applyVerification(latch, verdict(valid, valid ? null : 7830 + i), `t${i}`);
      }
      for (const read of [null, verdict(true), verdict(false, 9001)]) {
        const reading = chainReading(latch.verdict, read, latch.observed);
        expect(reading.word).toBe("BREAK");
        expect(reading.breakSeq).not.toBeNull();
      }
    }
  });

  it("a break WITHDRAWN by a sweep may not locate the next one", () => {
    // Mis-location, which is worse than none — and DEFENSIVE for the same
    // reason as the case above: it takes seq-less reads to confirm the second
    // break, which is a wire the server cannot emit. On today's wire the newest
    // locating observation is already the standing break's own, so `cleared`
    // never changes the answer. The durable list outlives a passing sweep on
    // purpose — "it flickered during the demo" stays readable
    // — but the entry a CLEARED break named has nothing to do with a later one
    // confirmed entirely by seq-less reads, and pointing the band, the pill and
    // the deep link at it states a location the server never gave for the break
    // now standing.
    const swept = clearLatch(latched(), verdict(true));
    expect(swept.observed.every((o) => o.cleared)).toBe(true);
    let latch = swept;
    for (let i = 0; i < BREAK_CONFIRMATIONS; i += 1) {
      latch = applyVerification(latch, verdict(false, null), `t${i}`);
    }
    expect(latch.verdict).toBe("break");
    expect(chainReading("break", verdict(false, null), latch.observed).breakSeq).toBeNull();
    // The CONTROL: a sweep does not erase history, it marks it.
    expect(latch.observed.some((o) => o.cleared && o.first_break_seq === 4020)).toBe(true);
  });

  it("never promotes a single disagreeing read to BREAK", () => {
    expect(chainReading("unknown", verdict(false, 4020), []).word).toBe("RE-VERIFYING");
    expect(chainReading("valid", verdict(false, 4020), []).word).toBe("RE-VERIFYING");
    expect(chainReading("reverifying", verdict(true), []).word).toBe("RE-VERIFYING");
  });

  it("vouches for nothing it has not been shown", () => {
    // No latch and no read is an ABSENCE the consumer states in its own words —
    // never VALID.
    expect(chainReading("unknown", null, []).word).toBeNull();
    expect(chainReading("valid", null, []).word).toBeNull();
    expect(chainReading("unknown", verdict(true), []).word).toBe("VALID");
  });

  it("names no break entry unless the word is BREAK", () => {
    // `first_break_seq` beside a passing verdict is a stale field, and a screen
    // that annotated its table from it would be drawing a break nothing claims.
    expect(chainReading("valid", verdict(true, 4020), observed).breakSeq).toBeNull();
    expect(chainReading("reverifying", verdict(false, 4020), observed).breakSeq).toBeNull();
  });
});

describe("there is ONE latch", () => {
  it("no screen keeps a chain latch of its own", () => {
    // /trace held `useState(INITIAL_LATCH)` while the rail chip held the
    // shell's, and `/trace/:traceId` renders INSIDE the layout that draws the
    // chip — so the two stood side by side, off two reducers, and could state
    // opposite things about one chain at one instant. Only the reducer's own
    // module and the provider that owns the shared latch may touch these.
    const ALLOWED = new Set([
      join(SRC_DIR, "audit", "chainVerdict.ts"),
      join(SRC_DIR, "hooks", "useAuditPoll.tsx"),
    ]);
    const FOLDS = /\b(applyVerification|clearLatch|INITIAL_LATCH)\b/;
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
          walk(full);
          continue;
        }
        if (!entry.endsWith(".ts") && !entry.endsWith(".tsx")) continue;
        if (entry.endsWith(".test.ts") || entry.endsWith(".test.tsx")) continue;
        if (ALLOWED.has(full)) continue;
        if (FOLDS.test(readFileSync(full, "utf-8"))) offenders.push(full.slice(SRC_DIR.length + 1));
      }
    };
    walk(SRC_DIR);
    expect(offenders).toEqual([]);
    // Self-test: the pattern bites the exact line that shipped two latches.
    expect(FOLDS.test("  const [latch, setLatch] = useState(INITIAL_LATCH);")).toBe(true);
  });
});

/* ===========================================================================
 * I4 — A WEAKER PROOF MAY NOT WITHDRAW A STRONGER PROOF'S FINDING.
 *
 * The server stopped walking the whole chain on every page read. The polled
 * verdict is now an `anchored-extension` proof: the rows appended since a head
 * a full `verify_chain` blessed up to fifteen minutes ago, and NOTHING ELSE. It
 * does not re-read the prefix, so a prefix row tampered after that blessing is
 * invisible to it by construction — while /trace's unbounded walk sees it,
 * disagrees three times and latches. The operator's Re-verify then resolved
 * through the poll, the poll answered `valid` off the anchor, and the console
 * WITHDREW a break it had confirmed. Detected, then un-detected.
 * ======================================================================== */

describe("I4 — clearing requires a proof at least as strong as the one that latched", () => {
  it("an ANCHORED pass does not clear a break a FULL walk confirmed", () => {
    const standing = latched();
    expect(standing.latchedBasis).toBe("full-chain");

    const swept = clearLatch(standing, verdict(true, null, { basis: "anchored-extension" }));

    // Untouched — the word, the flag, the bar and the durable list.
    expect(swept).toBe(standing);
    expect(swept.latched).toBe(true);
    expect(swept.verdict).toBe("break");
    expect(swept.observed.some((o) => o.confirmed && !o.cleared)).toBe(true);
    expectI1(swept);
  });

  it("a WINDOWED pass does not clear anything either", () => {
    // Blind to every row outside the window by construction, so it vouches for
    // nothing about the chain.
    const swept = clearLatch(latched(), verdict(true, null, { basis: "windowed" }));
    expect(swept.latched).toBe(true);
    expect(swept.verdict).toBe("break");
  });

  it("CONTROL: a FULL-chain pass still clears it — this is a latch, not a stain", () => {
    const swept = clearLatch(latched(), verdict(true, null, { basis: "full-chain" }));
    expect(swept.latched).toBe(false);
    expect(swept.verdict).toBe("valid");
    expect(swept.latchedBasis).toBeNull();
    // The list stays DURABLE; every break in it is WITHDRAWN.
    expect(swept.observed.every((o) => o.cleared)).toBe(true);
    expectI1(swept);
  });

  it("CONTROL: an anchored pass clears a break only anchored reads confirmed", () => {
    // The bar is the STRENGTH OF THE EVIDENCE, not a blanket demand for a full
    // walk. A latch nothing stronger than an anchored proof ever stood behind is
    // withdrawable by an anchored proof.
    const anchoredBreak = Array<boolean>(BREAK_CONFIRMATIONS)
      .fill(false)
      .reduce(
        (latch, _v, i) =>
          applyVerification(latch, verdict(false, 4020, { basis: "anchored-extension" }), `t${i}`),
        INITIAL_LATCH,
      );
    expect(anchoredBreak.latched).toBe(true);
    expect(anchoredBreak.latchedBasis).toBe("anchored-extension");

    const swept = clearLatch(anchoredBreak, verdict(true, null, { basis: "anchored-extension" }));
    expect(swept.latched).toBe(false);
    expect(swept.verdict).toBe("valid");
  });

  it("the bar RISES when a stronger proof joins a standing break", () => {
    // An anchored latch that a full walk then also disagrees with is no longer
    // withdrawable by an anchored pass: the strongest evidence standing behind
    // the break sets the bar, and it never falls while the latch stands.
    let latch = INITIAL_LATCH;
    for (let i = 0; i < BREAK_CONFIRMATIONS; i += 1) {
      latch = applyVerification(
        latch,
        verdict(false, 4020, { basis: "anchored-extension" }),
        `a${i}`,
      );
    }
    expect(latch.latchedBasis).toBe("anchored-extension");

    latch = applyVerification(latch, verdict(false, 4020, { basis: "full-chain" }), "full");
    expect(latch.latchedBasis).toBe("full-chain");

    expect(clearLatch(latch, verdict(true, null, { basis: "anchored-extension" })).latched).toBe(
      true,
    );
    expect(clearLatch(latch, verdict(true, null, { basis: "full-chain" })).latched).toBe(false);
  });

  it("a FAILING sweep still re-confirms on the spot, whatever its basis", () => {
    // Unchanged, and asserted so the new gate cannot be mistaken for the whole
    // rule: `clearLatch` refuses an invalid verdict before it ever looks at a
    // basis.
    expect(clearLatch(latched(), verdict(false, 4020)).latched).toBe(true);
    expect(clearLatch(latched(), null).latched).toBe(true);
  });
});

/* ===========================================================================
 * I6 — A PASSING VERDICT MAY NOT ERASE PROGRESS TOWARD A FINDING IT COULD NOT
 * HAVE MADE.
 *
 * I4 protects a latch that already stands. It was guarding a door nothing could
 * walk through, because the RUN that reaches a latch was erased by any passing
 * verdict at all, whatever its basis. The scenario, and it is the one this
 * whole file exists for:
 *
 *   A prefix row is tampered AFTER the anchor was blessed. /trace's unbounded
 *   walk sees it and answers invalid on EVERY read. The shell's 5s poll answers
 *   `valid` off the stale anchor on every read, because an `anchored-extension`
 *   proof does not re-read the prefix AT ALL. Both fold into ONE run (the run
 *   is per-chain, by design). The reads alternate, the run goes 1, 0, 1, 0 …
 *   and BREAK_CONFIRMATIONS is never reached. The break NEVER LATCHES, so
 *   `latchedBasis` never exists to refuse the weak sweep, and the strongest
 *   instrument in the product sees the tamper every single time while the
 *   weakest one erases the evidence of it.
 *
 * The rule: a passing read resets the run only when its own proof is AT LEAST
 * AS STRONG as the strongest proof the run is made of.
 * ======================================================================== */

describe("I6 — a weaker passing read cannot erase a stronger proof's run", () => {
  const anchoredPass = verdict(true, null, { basis: "anchored-extension" });
  const fullBreak = verdict(false, 4020, { basis: "full-chain" });

  it("an ANCHORED pass leaves a FULL walk's confirmation count exactly where it was", () => {
    const one = applyVerification(INITIAL_LATCH, fullBreak, "t0");
    expect(one.run).toBe(1);
    expect(one.runBasis).toBe("full-chain");

    const after = applyVerification(one, anchoredPass, "t1");
    expect(after.run).toBe(1);
    expect(after.runBasis).toBe("full-chain");
    // And it does not publish VALID over a chain a full walk has accused and
    // nothing has answered: RE-VERIFYING is the honest third state, and this
    // read is not a proof that could withdraw the accusation.
    expect(after.verdict).toBe("reverifying");
    expectI1(after);
  });

  it("THE SCENARIO: alternating full-chain breaks and anchored passes still LATCH", () => {
    // The exact interleaving above, read for read. Before this rule it ran
    // forever without ever latching.
    let latch = INITIAL_LATCH;
    for (let i = 0; i < BREAK_CONFIRMATIONS; i += 1) {
      latch = applyVerification(latch, fullBreak, `break${i}`);
      if (latch.latched) break;
      latch = applyVerification(latch, anchoredPass, `pass${i}`);
    }
    expect(latch.latched).toBe(true);
    expect(latch.verdict).toBe("break");
    // And the bar defending it is the bar the EVIDENCE sets, so the anchored
    // poll that kept passing cannot withdraw it either.
    expect(latch.latchedBasis).toBe("full-chain");
    expect(clearLatch(latch, anchoredPass).latched).toBe(true);
    expectI1(latch);
  });

  it("CONTROL: an EQUAL-strength pass still resets — the race stays suppressed", () => {
    // This is what the reproduced read-during-append race actually looks like:
    // the same instrument on both reads. A full-chain invalid clears the
    // process's anchor, so the next request-path read walks the chain inline
    // too. Two of these in a row must NOT accumulate toward a break.
    let latch = INITIAL_LATCH;
    for (let i = 0; i < 8; i += 1) {
      latch = applyVerification(latch, fullBreak, `b${i}`);
      latch = applyVerification(latch, verdict(true, null, { basis: "full-chain" }), `p${i}`);
      expect(latch.run).toBe(0);
      expect(latch.runBasis).toBeNull();
      expect(latch.verdict).toBe("valid");
      expect(latch.latched).toBe(false);
    }
  });

  it("CONTROL: a STRONGER pass resets an anchored run — the rule is strength, not rank", () => {
    // A run built from anchored disagreements is fully answerable by a full
    // walk: that proof CAN see everything the anchored one could. Nothing here
    // demands the same basis, only one at least as strong.
    let latch = applyVerification(
      INITIAL_LATCH,
      verdict(false, 4020, { basis: "anchored-extension" }),
      "a0",
    );
    expect(latch.run).toBe(1);
    latch = applyVerification(latch, verdict(true, null, { basis: "full-chain" }), "p0");
    expect(latch.run).toBe(0);
    expect(latch.runBasis).toBeNull();
    expect(latch.verdict).toBe("valid");
  });

  it("a WINDOWED pass erases nothing at all — it vouches for nothing", () => {
    const one = applyVerification(INITIAL_LATCH, fullBreak, "t0");
    const after = applyVerification(one, verdict(true, null, { basis: "windowed" }), "t1");
    expect(after.run).toBe(1);
    expect(after.verdict).toBe("reverifying");
  });

  it("a WINDOWED pass does not reset a run built from a DISJOINT WINDOWED break", () => {
    // ⭐ THE COVERAGE GAP THE RANK CANNOT SEE. A filtered read over window W1
    // proves a tamper; the run is `windowed`. A LATER filtered read over a
    // DISJOINT window W2 passes, and it is `windowed` too — rank-EQUAL, and
    // equal strength used to reset. So a valid read of rows nobody accused
    // erased the progress toward a break found in rows it never looked at.
    //
    // The server's anchor-withdrawal argument does not reach this pair: that
    // makes the next UNFILTERED poll walk the whole chain, but a second
    // FILTERED read is still answered `windowed`, and nothing on the wire tells
    // this console which rows either window covered.
    const w1 = applyVerification(INITIAL_LATCH, verdict(false, 42, { basis: "windowed" }), "w1");
    expect(w1.run).toBe(1);
    expect(w1.runBasis).toBe("windowed");

    const w2 = applyVerification(w1, verdict(true, null, { basis: "windowed" }), "w2");
    expect(w2.run).toBe(1);
    expect(w2.runBasis).toBe("windowed");
    expect(w2.verdict).toBe("reverifying");

    // …and the run still reaches BREAK_CONFIRMATIONS through the passes, which
    // is the point: the evidence was not erased, it was CARRIED.
    let latch = w2;
    for (let i = 1; i < BREAK_CONFIRMATIONS; i += 1) {
      latch = applyVerification(latch, verdict(false, 42, { basis: "windowed" }), `b${i}`);
      latch = applyVerification(latch, verdict(true, null, { basis: "windowed" }), `p${i}`);
    }
    expect(latch.latched).toBe(true);
    expect(latch.verdict).toBe("break");
    expectI1(latch);
  });

  it("CONTROL: the windowed run is not permanent — a whole-chain pass still answers it", () => {
    // The cost of the rule, pinned so it stays a COST and not a stain. A
    // windowed run is withdrawable by either proof that reads rows it did not
    // name — and the shared 5s poll is unfiltered, so one of those arrives on
    // the next tick without an operator doing anything.
    const w1 = applyVerification(INITIAL_LATCH, verdict(false, 42, { basis: "windowed" }), "w1");
    expect(w1.run).toBe(1);

    // ONLY a whole-chain walk answers it. This asserted `anchored-extension`
    // too, which was the defect: that proof reads the tail beyond an anchor and
    // is blind to a window inside the blessed prefix — and with ne-api at the
    // Helm default of TWO replicas, the pass can come from a different pod whose
    // anchor never saw the break at all.
    for (const basis of ["full-chain"] as const) {
      const after = applyVerification(w1, verdict(true, null, { basis }), "p");
      expect(after.run).toBe(0);
      expect(after.runBasis).toBeNull();
      expect(after.verdict).toBe("valid");
    }
    // ...and an ANCHORED pass does NOT: it never read the accused rows.
    const anchored = applyVerification(
      w1,
      verdict(true, null, { basis: "anchored-extension" }),
      "a",
    );
    expect(anchored.run).toBe(1);
    expect(anchored.runBasis).toBe("windowed");
    expect(anchored.verdict).toBe("reverifying");
  });

  it("CONTROL: equal-strength ANCHORED alternation still resets — the race stays suppressed", () => {
    // The rule the fix must not break. `resetsARun` gates BASIS, not equality:
    // two `anchored-extension` reads are the same instrument over the same
    // scope, so a passing one still answers a failing one and the
    // read-during-append race does not accumulate toward a break.
    let latch = INITIAL_LATCH;
    for (let i = 0; i < 8; i += 1) {
      latch = applyVerification(
        latch,
        verdict(false, 4020, { basis: "anchored-extension" }),
        `b${i}`,
      );
      latch = applyVerification(
        latch,
        verdict(true, null, { basis: "anchored-extension" }),
        `p${i}`,
      );
      expect(latch.run).toBe(0);
      expect(latch.runBasis).toBeNull();
      expect(latch.latched).toBe(false);
    }
  });

  it("a WINDOWED sweep cannot clear a WINDOWED latch, or erase its run", () => {
    // The same coverage gap, one layer up, through the operator's own control:
    // `clearLatch`'s bar is `strengthOf`, so a latch whose bar is `windowed`
    // (rank 0) was cleared by a windowed sweep (rank 0) — `0 < 0` is false —
    // over a window sharing not one accused row. And a refused sweep matters
    // doubly here: the fold that runs immediately after would have reset the
    // run anyway.
    let standing = INITIAL_LATCH;
    for (let i = 0; i < BREAK_CONFIRMATIONS; i += 1) {
      standing = applyVerification(standing, verdict(false, 42, { basis: "windowed" }), `w${i}`);
    }
    expect(standing.latched).toBe(true);
    expect(standing.latchedBasis).toBe("windowed");

    const swept = clearLatch(standing, verdict(true, null, { basis: "windowed" }));
    expect(swept).toBe(standing);
    expect(swept.latched).toBe(true);
    expect(swept.verdict).toBe("break");

    // An UNRANKED basis is refused by the same gate, and used to slip through
    // the same arithmetic.
    const unknown = { ...verdict(true), basis: "quantum-walk" } as unknown as ChainVerificationView;
    expect(clearLatch(standing, unknown)).toBe(standing);

    // CONTROL: a proof entitled to withdraw a run withdraws this too.
    const cleared = clearLatch(standing, verdict(true, null, { basis: "anchored-extension" }));
    expect(cleared.latched).toBe(false);
    expect(cleared.verdict).toBe("valid");
    expect(cleared.run).toBe(0);
    expectI1(cleared);
  });

  it("CONTROL: with NOTHING standing, a windowed sweep still normalises the word", () => {
    // The gate is skipped when the bar is null, so a sweep against a clean
    // latch behaves exactly as it always did — the fix refuses withdrawals, it
    // does not refuse sweeps.
    const clean = clearLatch(INITIAL_LATCH, verdict(true, null, { basis: "windowed" }));
    expect(clean.verdict).toBe("valid");
    expect(clean.latched).toBe(false);
  });

  it("a REFUSED sweep cannot erase the run either, the way it cannot erase a latch", () => {
    // A sweep resolves through `clearLatch` and THEN through
    // `applyVerification`, so a sweep weak enough to be refused would have gone
    // on to reset the run in the fold immediately after — the identical defect
    // one instant earlier, delivered by the operator's own control.
    const two = applyVerification(
      applyVerification(INITIAL_LATCH, fullBreak, "t0"),
      fullBreak,
      "t1",
    );
    expect(two.run).toBe(2);

    const swept = clearLatch(two, anchoredPass);
    expect(swept).toBe(two);
    expect(applyVerification(swept, anchoredPass, "t2").run).toBe(2);

    // CONTROL: a sweep strong enough to answer the run does reset it.
    const strong = clearLatch(two, verdict(true, null, { basis: "full-chain" }));
    expect(strong.run).toBe(0);
    expect(strong.runBasis).toBeNull();
  });

  it("a basis this console cannot rank is the WEAKEST, not a missing number", () => {
    // `BASIS_STRENGTH[basis]` is typed `number` and is `undefined` at runtime
    // for a wire value this console has no entry for — and the two rules read
    // in OPPOSITE directions (`>=` to reset a run, `<` to refuse a sweep), so a
    // silent `undefined` does not fail one way, it fails a DIFFERENT way in
    // each place. This is not hypothetical arithmetic: a fixture in
    // test/round2-guardian.test.ts omitted `basis` behind a cast and turned the
    // alternating CONTROL there red for a reason that had nothing to do with
    // the rule it guards.
    const unknown = { ...verdict(true), basis: "quantum-walk" } as unknown as ChainVerificationView;
    const running = applyVerification(
      INITIAL_LATCH,
      verdict(false, 4020, { basis: "full-chain" }),
      "t0",
    );

    // It erases nothing…
    expect(applyVerification(running, unknown, "t1").run).toBe(1);
    // …and it withdraws nothing.
    expect(clearLatch(latched(), unknown).latched).toBe(true);
  });

  it("a latched break keeps its word when a weaker pass leaves the run standing", () => {
    // I1 on the branch this rule touches: `latched` and the WORD are one fact,
    // whichever way the reset goes.
    const standing = latched();
    const after = applyVerification(standing, anchoredPass, "t-weak");
    expect(after.latched).toBe(true);
    expect(after.verdict).toBe("break");
    expectI1(after);
    expectI1(applyVerification(standing, verdict(true, null, { basis: "full-chain" }), "t-strong"));
  });

  it("the latch's bar is the RUN's, so pre-promotion disagreements count toward it", () => {
    // `latchedBasis` was read off the PROMOTING verdict alone, and it is null
    // for every read before promotion — so the disagreements that BUILT the
    // break contributed nothing to the bar defending it. A run of one full-chain
    // read and two anchored ones latched as `anchored-extension`, and an
    // anchored sweep could then withdraw a finding only the full walk could have
    // made. Not reachable on today's wire (every invalid verdict the server
    // sends is full-chain) and a trap all the same.
    let latch = applyVerification(INITIAL_LATCH, fullBreak, "full");
    latch = applyVerification(latch, verdict(false, 4020, { basis: "anchored-extension" }), "a1");
    latch = applyVerification(latch, verdict(false, 4020, { basis: "anchored-extension" }), "a2");
    expect(latch.latched).toBe(true);
    expect(latch.latchedBasis).toBe("full-chain");
    expect(clearLatch(latch, anchoredPass).latched).toBe(true);
    expect(clearLatch(latch, verdict(true, null, { basis: "full-chain" })).latched).toBe(false);
  });
});

/* ===========================================================================
 * I5 — A ONE-SHOT CONTRADICTION REACHES THE SCREEN.
 *
 * `BREAK_CONFIRMATIONS` is 3 because of ONE race: a page snapshot and the head
 * pointer sampled at different instants while the console's own polling
 * appends. That race is request-scoped, so three spaced reads cannot all
 * straddle it — and the count stays 3 for every verdict it applies to.
 *
 * It does not apply to a CONTRADICTION. That verdict means the ledger stopped
 * extending a head the server itself verified; the extension proof is race-free
 * by construction, so no legitimate append can manufacture it. And the server
 * re-anchors on the way out, so it appears EXACTLY ONCE. Requiring three
 * consecutive sightings of a one-shot verdict is requiring something that
 * cannot happen: the strongest signal the server has could not reach a screen
 * at any poll interval, ever.
 * ======================================================================== */

describe("I5 — a contradiction latches on sight without loosening the run count", () => {
  const contradiction = () =>
    verdict(false, null, {
      contradiction: true,
      reason: null,
      detail:
        "chain does not extend the head verified at seq 12478: the ledger is internally " +
        "consistent (12,480 record(s) verified, now at head seq 12480) but it is not an " +
        "append-only continuation of the state this process verified",
    });

  it("ONE contradicting read latches BREAK", () => {
    const latch = applyVerification(INITIAL_LATCH, contradiction(), "t0");
    expect(latch.verdict).toBe("break");
    expect(latch.latched).toBe(true);
    expect(latch.run).toBe(1);
    expect(latch.observed[0]!.confirmed).toBe(true);
    expectI1(latch);
  });

  it("CONTROL: the SAME verdict without the flag is RE-VERIFYING at run 1", () => {
    // The discriminator. Nothing about this read but the server's flag makes it
    // latchable, so `BREAK_CONFIRMATIONS` is provably still doing its job for
    // every verdict the race can produce.
    const ordinary = applyVerification(
      INITIAL_LATCH,
      verdict(false, null, { contradiction: false }),
      "t0",
    );
    expect(ordinary.verdict).toBe("reverifying");
    expect(ordinary.latched).toBe(false);
    expectI1(ordinary);
  });

  it("CONTROL: an ordinary break still needs its three consecutive reads", () => {
    for (let n = 1; n < BREAK_CONFIRMATIONS; n += 1) {
      expect(fold(Array<boolean>(n).fill(false)).verdict).toBe("reverifying");
    }
    expect(fold(Array<boolean>(BREAK_CONFIRMATIONS).fill(false)).verdict).toBe("break");
  });

  it("the latch SURVIVES the passing read that follows it — which is the only kind that follows", () => {
    // The server adopts the freshly verified head before it returns the
    // refusal, so the very next read PASSES. A latch that a passing read could
    // reset would make this verdict unobservable even after it was believed.
    const latch = applyVerification(
      applyVerification(INITIAL_LATCH, contradiction(), "t0"),
      verdict(true),
      "t1",
    );
    expect(latch.verdict).toBe("break");
    expect(latch.latched).toBe(true);
    expectI1(latch);
  });

  it("the screen gets the server's SENTENCE even though no entry was named", () => {
    // A contradiction carries a PASSING `verify_chain` underneath it, so it has
    // no `first_break_seq` and no `reason` — its whole account of itself is
    // `detail`. Recording only `reason` left the one verdict that can latch on
    // sight with nothing to say for itself, under a BREAK pill.
    const latch = applyVerification(INITIAL_LATCH, contradiction(), "t0");
    const reading = chainReading(latch.verdict, contradiction(), latch.observed);
    expect(reading.word).toBe("BREAK");
    expect(reading.breakSeq).toBeNull();
    expect(reading.breakReason).toContain("not an append-only continuation");
  });

  it("a contradiction latch demands a FULL-chain sweep to clear", () => {
    // It IS a full-chain verdict (the walk ran; it is the anchor that was
    // disproved), so I4 puts the bar at the top — and the console's Re-verify
    // asks the server for exactly that walk.
    const latch = applyVerification(INITIAL_LATCH, contradiction(), "t0");
    expect(latch.latchedBasis).toBe("full-chain");
    expect(clearLatch(latch, verdict(true, null, { basis: "anchored-extension" })).latched).toBe(
      true,
    );
    expect(clearLatch(latch, verdict(true, null, { basis: "full-chain" })).latched).toBe(false);
  });
});
