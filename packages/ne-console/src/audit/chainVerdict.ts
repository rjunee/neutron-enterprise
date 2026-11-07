// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE CHAIN VERDICT LATCH (review round 2, blocker — hostile skeptic + approver).
//
// Reproduced deterministically by two reviewers: the global header chip flipped
// `● VALID` / `⚠ BREAK seq 7830` / `● VALID` / `⚠ BREAK seq 7838` across five
// consecutive navigations, with the server returning
// `{"valid": false, "first_break_seq": 7830, "reason": "head mismatch
// (truncated or forged tail)"}` on roughly half of
// `GET /v1/audit/events?limit=25`. It is a read-during-append race server-side:
// the page snapshot and the head pointer are sampled at different instants
// while the console's own polling appends to the chain.
//
// THE `first_break_seq` IN THAT BODY IS NOT DECORATION, AND AN EARLIER REVISION
// OF THIS FILE DROPPED IT FROM THE QUOTE. Several rules below were then written
// against a seq-less break — a wire `ne-audit` cannot emit — and described as
// reproduced fact. The reproduction above says otherwise in its own transcript:
// the chip rendered `seq 7830` and `seq 7838`, which it can only do off a seq
// the server sent, and the two DIFFER because the head-mismatch arm names the
// MOVING HEAD. Where a rule below still guards a seq-less break it is labelled
// DEFENSIVE, with `_break` (verify.py:180) cited as the reason it cannot fire.
//
// ⚠ THOSE LABELS ARE NOW HALF WRONG, AND THE HALF THAT CHANGED IS LOAD-BEARING.
// An ORDINARY break still always names an entry. A CONTRADICTION does not: it
// carries a PASSING `verify_chain` underneath it — the ledger is internally
// consistent, it simply is not the ledger the server verified — so
// `first_break_seq` is `null` on a `valid: false` read, on purpose, from a real
// server. Every rule below that was written for "a shape the server cannot
// emit" is therefore a rule that RUNS. It is still right; it is no longer
// hypothetical, and it must not be deleted as ceremony.
//
// TWO THINGS ARE WRONG WITH RENDERING THAT VERBATIM, AND THEY PULL OPPOSITE WAYS:
//   1. A verdict that clears itself on the next poll is not tamper-evidence. If
//      the console shows BREAK and then quietly shows VALID, a REAL break is
//      indistinguishable from the race — the indicator has no evidential value.
//   2. The word "forged" in the header of a live demo, produced by the console's
//      own polling, is a self-inflicted wound.
//
// The resolution is NOT to hide a break. It is to require the verdict to AGREE
// WITH ITSELF before it becomes the resting state, and then to LATCH it:
//   * `valid` from the server, with nothing latched and no run standing → VALID.
//   * `invalid` observed 1..N-1 times consecutively  → RE-VERIFYING (not VALID,
//     not BREAK — the honest third state: the server has disagreed with itself
//     and the console is still counting). A passing read TOO WEAK to answer
//     that disagreement (I6) does not end the run, so it does not end this
//     state either: the console is still counting, and says so.
//   * `invalid` observed N times consecutively       → BREAK, LATCHED.
//   * A latched BREAK is NEVER cleared by a passing poll. Only an explicit
//     re-verification sweep clears it, and only if that sweep comes back valid.
//   * EVERY observed break — including the transient ones — is appended to a
//     durable list, so "it flickered during the demo" is a thing an operator can
//     open and read rather than a thing they half-remember.
//
// THE LATCH IS THE AUTHORITY ONCE CONFIRMED — the invariant every rule below
// serves, and the one that outranks the rest of this file.
//
//   I1. `latched` and the WORD ON SCREEN are the same fact. `latched === true`
//       ⟺ `verdict === "break"`, after every fold, for every input. They came
//       apart once and the consequence was the worst defect this file can have:
//       a confirmed break, still latched in the data, rendering as RE-VERIFYING
//       because the failing branch computed its verdict from the current run
//       alone. Nothing renders `latched`; every consumer renders the WORD.
//   I2. Nothing but `clearLatch`, on a PASSING sweep, moves `latched` back to
//       false. Not a passing poll, not a failed read, not a screen change.
//   I3. EVERY SURFACE THAT STATES A WORD ABOUT THE CHAIN resolves it through
//       `chainReading` — the one mapping below — so a confirmed break cannot be
//       on one screen and absent from another, and so an ABSENT read (a 502'd
//       poll, a screen with no verification of its own) cannot subtract a break
//       the console confirmed. The surfaces, exhaustively: /audit's verdict
//       strip (AuditPage.tsx), /trace's verdict strip (TracePage.tsx), the rail
//       chip and its popover's Audit-chain row (StatusChip.tsx), and Home's
//       "Audit log" standing-fact tile (landing/homeTiles.ts `auditTile`). The
//       ledger spine (audit/spine.ts) is not a fifth reading: it is DRAWN FROM
//       /audit's, taking that reading's `breakSeq` and its word.
//
//       ⚠ THE HOME TILE WAS THE COUNTEREXAMPLE, AND THIS INVARIANT WAS A FALSE
//       CLAIM ABOUT THIS CONSOLE FOR AS LONG AS IT STOOD. `auditTile` branched
//       on ONE read's raw `verification.valid` and never saw the latch, the run
//       or the observations. Two consequences, both on the FIRST screen of the
//       product and both inside one viewport with the rail chip (App.tsx
//       renders `StatusChip` in `AuthedLayout`'s TopBar and routes `/` inside
//       that same layout): a break /trace's unbounded walk had CONFIRMED
//       rendered as "Valid · 12,480 records" an inch under a chip reading
//       "Chain break at entry 4812" — because the shared poll's
//       `anchored-extension` read is the one proof that STRUCTURALLY CANNOT see
//       a prefix tampered after the anchor was blessed (I4) — and a SINGLE
//       disagreeing read rendered "Break at entry 7830" there with zero
//       confirmations behind it, the exact flicker `BREAK_CONFIRMATIONS`
//       exists to suppress. An invariant nothing asserted, about a consumer
//       nobody had enumerated; `round2-guardian.test.ts` now scans the source
//       so the next unenumerated consumer fails a test instead of a demo.
//
//       THE ONE SURFACE THAT STATES NO WORD IS NOT AN EXCEPTION TO THIS.
//       WorkflowsPage.tsx prints a literal em dash labelled "Verification
//       unavailable while run is active" beside its live governed trace. That
//       is an explicit ABSENCE and never a verdict — it vouches for nothing, so
//       it can neither contradict the latch nor subtract from it. A surface
//       becomes bound by this rule at the moment it acquires a word.
//   I4. A VERDICT MAY NOT WITHDRAW A FINDING IT COULD NOT HAVE MADE. The server
//       stopped walking the whole chain on every page read: the polled verdict
//       is now a PREFIX a full `verify_chain` blessed up to fifteen minutes ago
//       plus the rows appended since, and it does not re-read that prefix at
//       all. So a prefix row tampered after the blessing is invisible to it BY
//       CONSTRUCTION, while /trace's unbounded walk sees it and latches — and a
//       passing sweep through the poll then cleared the break. `latchedBasis`
//       records the strength of the strongest proof standing behind a latch and
//       `clearLatch` refuses anything weaker. Detection that can be un-detected
//       by a blinder instrument is not detection.
//   I5. A CONTRADICTION LATCHES ON SIGHT, and `BREAK_CONFIRMATIONS` stays 3.
//       The three exists for one race — a request-scoped read-during-append —
//       and `contradiction` marks the verdicts that race provably cannot
//       produce and that the server emits EXACTLY ONCE before re-anchoring.
//       Requiring three consecutive sightings of a one-shot verdict is
//       requiring something that cannot occur, so the strongest signal the
//       server has could not reach a screen at any poll interval. Flap
//       suppression applies to what flaps.
//   I6. A PASSING VERDICT MAY NOT ERASE PROGRESS TOWARD A FINDING IT COULD NOT
//       HAVE MADE. I4 protects a latch that already stands; this protects the
//       run that reaches one, and without it I4 guarded a door nothing could
//       walk through. `/trace`'s unbounded walk disagrees, the shared 5s poll
//       answers `valid` off its stale anchor — the one proof that cannot see
//       the tampered prefix — and the run went back to 0 on every one of them.
//       Alternating forever, the run never reached BREAK_CONFIRMATIONS, so the
//       break never latched and `latchedBasis` never had anything to defend.
//       `runBasis` records the strongest proof standing behind the run and a
//       weaker passing read now leaves it alone.
//
// Pure reducer, no React, no storage: the provider owns the effects and this
// owns the rule, so the rule is unit-testable on its own.

import type { ChainVerificationView, VerificationBasis } from "../api/types";
import type { StateWord } from "../primitives/StatePill";
import { chainBreakSeq } from "./spine";

/** How many consecutive disagreeing reads promote RE-VERIFYING to BREAK. Three
 * is the smallest number that cannot be produced by a single straddled append
 * (the race is per-request), and at the 5s poll it resolves inside 15 seconds.
 *
 * THE COUNT ASSUMES THE READS ARE INDEPENDENT, and only the CALLER can make
 * them so: nothing bounds how wide an append window is, so three reads taken
 * close enough together can all straddle one of them and confirm a break that
 * is not there. A caller that re-reads on its own schedule owes this rule
 * spacing — see TracePage's REVERIFY_BACKOFF — and an operator-reachable way
 * to clear a latch it got wrong.
 *
 * THE RUN IS PER-CHAIN, NOT PER-SCREEN. Every fold — the shell's 5s poll and
 * /trace's own read of the same unbounded `verify_chain` — lands in ONE run,
 * because they are reads of ONE chain. Interleaving can only make promotion
 * harder (a passing read from either source resets the run), never easier, and
 * a chain that is genuinely broken answers invalid to both. */
export const BREAK_CONFIRMATIONS = 3;

/**
 * HOW STRONG A VERDICT IS, as an order — the second half of the rule above.
 *
 * ⚠ THIS ORDER IS NOT A COVERAGE ORDER. TWO PAIRS EXPOSE THAT, AND THEY ARE
 * CLOSED DIFFERENTLY — the first by the server, the second HERE.
 *
 * (a) `windowed` BREAK vs `anchored-extension` PASS. `windowed` ranks BELOW
 *     `anchored-extension`, so by rank the anchored pass may reset the run —
 *     yet an anchored proof cannot see a below-the-anchor tamper, which is
 *     exactly what a narrow window CAN see, so by the rule above it is not
 *     entitled to answer that disagreement. It never gets to: a windowed break
 *     withdraws the shared anchor (`ne.audit.verify`'s chain-walk observer), so
 *     the next poll has no anchor and walks the whole chain, which sees the
 *     tamper and keeps the run alive. That gap is UNREACHABLE, not absent. If
 *     the server-side withdrawal is ever removed, this ordering becomes wrong
 *     the same day.
 *
 * (b) `windowed` BREAK vs `windowed` PASS, over a DISJOINT WINDOW. ⭐ THIS ONE
 *     THE SERVER DOES NOT CLOSE, AND NO ORDERING CAN. Both sides are the same
 *     basis, so they are rank-EQUAL, and an equal-strength pass resets — so a
 *     valid read over rows `[9000, 9100]` erased a run built from a break found
 *     in `[10, 200]`, without having looked at a single row the break accused.
 *     Nothing in (a)'s argument reaches it: the anchor withdrawal makes the
 *     NEXT UNFILTERED poll walk the whole chain, but a second FILTERED read is
 *     still answered windowed, and the console is not told which rows either
 *     window covered. The rank is a strength, not a coverage claim, and here
 *     the two come apart with the same value on both sides.
 *
 *     SO THE RANK IS NOT THE ONLY GATE: `resetsARun` below is, and it says a
 *     `windowed` pass resets NOTHING — see its own comment for the cost.
 *
 * `BREAK_CONFIRMATIONS` decides WHEN this console believes a disagreement. This
 * decides WHICH verdicts may withdraw one, and it exists because the server
 * stopped answering every page read with a full walk. Three claims reach this
 * file and they are not interchangeable:
 *
 *   * `full-chain` — a real unbounded `verify_chain` re-hashed every record on
 *     that request. It is the only proof that looks at the PREFIX.
 *   * `anchored-extension` — the rows appended since a head a full verify
 *     blessed up to 15 minutes ago were re-hashed, and nothing else was. It
 *     STRUCTURALLY CANNOT SEE a prefix row tampered after that blessing.
 *   * `windowed` — a `from`/`to`-bounded walk. Blind to every row outside the
 *     window by construction, so it withdraws nothing and vouches for nothing.
 *
 * THE DEFECT THIS CLOSES, in the order it happens: a prefix row is tampered
 * without propagating its hash, AFTER the anchor was blessed. /trace's own
 * unbounded walk sees it, disagrees three times and LATCHES a break. The shared
 * poll meanwhile keeps answering `valid` off the anchor — it never looks at the
 * prefix — and the operator's Re-verify sweep resolves through that poll. A
 * proof that cannot see the tamper then withdrew the finding of a proof that
 * did. Not merely "failed to detect": detected, then UN-detected.
 */
const BASIS_STRENGTH: Record<VerificationBasis, number> = {
  windowed: 0,
  "anchored-extension": 1,
  "full-chain": 2,
};

/** A basis's strength, TOTAL — an unrecognised one is the WEAKEST, never a
 * missing number.
 *
 * TypeScript types `BASIS_STRENGTH[basis]` as `number` and the runtime does
 * not: a wire field this console has no entry for indexes to `undefined`, and
 * every comparison against `undefined` is FALSE. The two rules here read in
 * opposite directions (`>=` to reset a run, `<` to refuse a sweep), so a silent
 * `undefined` does not fail one way — it fails a different way in each place.
 * Weakest is the fail-safe answer for both: a proof this console cannot rank
 * neither erases a run nor withdraws a latch. */
function strengthOf(basis: VerificationBasis | null): number {
  return basis === null ? 0 : (BASIS_STRENGTH[basis] ?? 0);
}

/**
 * Whether a PASSING verdict of this basis may withdraw a RUN AT ALL — the
 * COVERAGE gate that `strengthOf` is not.
 *
 * `strengthOf` is a RANK. It answers "could this proof have seen what that one
 * saw?" for two proofs of DIFFERENT scope, and it is right about that. It says
 * nothing about two proofs of the SAME scope that looked at DIFFERENT ROWS, and
 * `windowed` is the only basis where that happens: two windowed reads are
 * rank-equal by construction and may share not one row. Equal rank resets —
 * that is what suppresses the read-during-append race — so a windowed pass over
 * a disjoint window erased a windowed break's progress.
 *
 * A `windowed` read "withdraws nothing and vouches for nothing" (see
 * BASIS_STRENGTH). That is taken literally here: it never resets a run. The
 * alternative — carrying `from`/`to` on the verdict and demanding the pass
 * COVER the break's window — needs a window on the wire that the server does
 * not send, a durable record of the accused window on every run, and a
 * containment rule that is still wrong for a tamper the accusing window only
 * partly spanned. It buys one thing: a windowed pass answering a windowed run
 * over the same rows. That case is already answered by the very next unfiltered
 * poll, which is unanchored (the break withdrew the anchor) and therefore
 * `full-chain` — so the extra surface buys nothing this does not already get.
 *
 * THE COST, stated plainly: a run built from windowed disagreements is now
 * withdrawable ONLY by an `anchored-extension` or `full-chain` pass. A screen
 * that reads nothing but filtered pages holds a windowed run until the shared
 * 5s poll — which is unfiltered — answers it. That poll runs on every screen,
 * so the wait is one poll interval, and a run that survives one poll is a run
 * the stronger instrument agreed with.
 *
 * An UNRANKED basis is `false` too, for `strengthOf`'s own stated reason: a
 * proof this console cannot rank neither erases a run nor withdraws a latch.
 * `null` is `false` and costs nothing — a run with no basis has count 0.
 */
function resetsARun(basis: VerificationBasis | null): boolean {
  return basis === "anchored-extension" || basis === "full-chain";
}

/**
 * WHICH PASS MAY ANSWER A RUN OF THIS BASIS — coverage, not rank.
 *
 * A run built from a WINDOWED break is about rows an ANCHORED proof cannot read
 * by construction: the anchor blesses a prefix and the extension reads only the
 * tail, while the accusing window may lie wholly inside that unread prefix. Rank
 * says `anchored-extension` is the stronger instrument in general; it does not
 * say it looked HERE. So a windowed run is answerable only by a whole-chain walk.
 *
 * ⭐ THIS ALSO CLOSES A MULTI-PROCESS HOLE, which is why it is a rule and not a
 * nicety. `deploy/helm/.../values.yaml` defaults ne-api to TWO replicas, and the
 * chain-walk observer registry is PER PROCESS (`anchor.py`: "ONE instance per
 * process per audit engine"). A filtered read can therefore prove a tamper on
 * pod A and withdraw only pod A's anchor, while the next unfiltered poll lands
 * on pod B, whose anchor still stands and which answers `anchored-extension`
 * valid — blind to the accused rows and on the wrong side of a network. Ranking
 * that pass above the window would let it erase the finding. Requiring a
 * whole-chain walk does not: pod B's own next background verify, or the
 * operator's Re-verify (`?full_verify=true`, unfiltered on either pod), produces
 * `full-chain` and answers it honestly. The break stays clearable; it just
 * cannot be clearable by a proof that never read the accusation.
 */
function answersAWindowedRun(basis: VerificationBasis | null): boolean {
  return basis === "full-chain";
}

/** The stronger of two bases (`null` = nothing claimed yet). */
function strongest(
  a: VerificationBasis | null,
  b: VerificationBasis | null,
): VerificationBasis | null {
  if (a === null) return b;
  if (b === null) return a;
  return strengthOf(a) >= strengthOf(b) ? a : b;
}

export type ChainVerdict = "unknown" | "valid" | "reverifying" | "break";

/** One break the console actually observed, kept whether or not it was confirmed. */
export interface ObservedBreak {
  /** ISO instant this console saw it. */
  at: string;
  first_break_seq: number | null;
  /** The SERVER'S OWN sentence about this observation — its `reason`, or its
   * `detail` when it named none. The fallback is not cosmetic: a CONTRADICTION
   * carries a passing `verify_chain` underneath it (the ledger is internally
   * consistent; it just is not the ledger this process verified), so its
   * `reason` is `null` and its whole account of itself is in `detail`. Recording
   * only `reason` left the one verdict that can latch on sight with nothing to
   * say for itself. */
  reason: string | null;
  /** How many consecutive disagreeing polls had been seen at this point. */
  run: number;
  /** True once this observation stands as a confirmed break — the run reached
   * BREAK_CONFIRMATIONS, or the server marked the verdict a CONTRADICTION and
   * there is no run to wait for. */
  confirmed: boolean;
  /** Which proof produced the verdict this observation was folded from. */
  basis: VerificationBasis;
  /** True once a PASSING re-verification sweep withdrew the latch this
   * observation belongs to. The observation is never deleted — "it flickered
   * during the demo" stays a thing an operator can open and read — but a break
   * an operator explicitly cleared may not go on LOCATING the next one, or the
   * console would pill, cut and deep-link an entry the server never named for
   * the break now standing.
   *
   * DEFENSIVE. Today's server cannot reach that mis-location: every invalid
   * read names an entry (see `locates`), the list is newest-first, and so the
   * newest locating observation is already the standing break's own. The flag
   * bites only against a verdict that names none — the shape
   * `first_break_seq: int | None` still permits and `_break` does not emit. */
  cleared: boolean;
}

export interface ChainLatch {
  verdict: ChainVerdict;
  /** Consecutive invalid verdicts observed so far (reset by any valid verdict). */
  run: number;
  /** True while a confirmed break stands. Cleared only by `clearLatch`. */
  latched: boolean;
  /**
   * THE STRENGTH A SWEEP MUST MATCH TO WITHDRAW THE STANDING BREAK — the
   * strongest proof that has disagreed while this latch stood, or `null` when
   * nothing is latched.
   *
   * A latch is a finding, and a finding is only as withdrawable as the proof
   * that made it. An `anchored-extension` verdict does not re-read the prefix
   * at all, so it can pass over exactly the tamper a `full-chain` walk latched
   * — and before this field existed, it did: the operator's Re-verify resolved
   * through the shared (anchored) poll and cleared a break /trace's full walk
   * had confirmed. `clearLatch` now refuses anything weaker than this.
   *
   * It RISES with every disagreeing read — INCLUDING the ones taken before
   * promotion, which is what `runBasis` carries across for it. Reading it off
   * the promoting verdict alone latched a break built from one full-chain and
   * two anchored disagreements as `anchored-extension`, so an anchored sweep
   * could withdraw a finding only the full walk could have made. It never falls
   * while the latch stands, so the bar is always the strongest evidence
   * standing behind the break.
   */
  latchedBasis: VerificationBasis | null;
  /**
   * THE STRENGTH A PASSING READ MUST MATCH TO RESET THE RUN — the strongest
   * proof among the disagreeing reads the current run is made of, or `null`
   * when the run is 0.
   *
   * `latchedBasis` is the same rule one layer later, and having only that one
   * left the rule guarding a door nothing could reach. A prefix row tampered
   * after the anchor was blessed is invisible to every `anchored-extension`
   * verdict BY CONSTRUCTION, so the shared poll keeps answering `valid` off it
   * while /trace's unbounded walk disagrees on every read. Both fold into ONE
   * run, and a passing read used to reset that run unconditionally: full-chain
   * invalid (run 1), anchored valid (run 0), full-chain invalid (run 1),
   * forever. The run never reached BREAK_CONFIRMATIONS, so nothing ever
   * latched, so `latchedBasis` never existed to refuse the weak sweep. The
   * detection was not merely withdrawn — it could not be MADE.
   *
   * It RISES with every disagreeing read (`strongest`) and is cleared only
   * where the run itself is cleared: a passing read strong enough to reset the
   * run, or `clearLatch`.
   */
  runBasis: VerificationBasis | null;
  /** Newest first, capped. */
  observed: ObservedBreak[];
}

export const INITIAL_LATCH: ChainLatch = {
  verdict: "unknown",
  run: 0,
  latched: false,
  latchedBasis: null,
  runBasis: null,
  observed: [],
};

/** How many observations the durable list keeps. Exported so the cap and the
 * one exception to it (`capObserved`) are testable as the rule they are. */
export const MAX_OBSERVED = 20;

/** The observation a STANDING break may be LOCATED from: confirmed, not
 * withdrawn by a sweep, and NAMING AN ENTRY.
 *
 * THE THIRD CLAUSE IS A DEFENSIVE GUARD, NOT A CASE THE SERVER PRODUCES, and an
 * earlier revision of this file had it the other way round — it held that the
 * read-during-append race answers invalid with no seq, and presented the
 * machinery resting on that as a defect reproduced and fixed. It was neither.
 * `ne-audit` builds a verdict in exactly two places and only one can say
 * invalid:
 *
 *     def _break(*, seq: int, event_id: str | None, reason: str, checked: int)
 *             -> ChainVerification:                          # verify.py:180
 *
 * `seq` is REQUIRED and an `int`. All 23 call sites pass one — including the
 * head-mismatch arm this race actually takes, which passes `seq=head_seq`
 * (verify.py:509) — and `wire.py:1131` and `trace_assembly.py:780` copy the
 * field to the wire verbatim. So `valid: false` ALWAYS names an entry, and
 * `first_break_seq: null` reaches this console only on a PASSING verdict
 * (`_ok`, verify.py:169).
 *
 * ⚠ AND THE SERVER NOW PRODUCES ONE. The clause was written against a type
 * that permitted what no constructor emitted; `AnchoredChainVerifier`'s
 * CONTRADICTION verdict is the "second break-constructor that names no entry"
 * this comment said was one function away. It refuses while the walk beneath it
 * PASSES, so it has no `first_break_seq` to give — and it is exactly the
 * observation a standing break must not be LOCATED from, because there is
 * nothing to locate. The fallback in `standingBreak` carries it instead, with
 * the server's own sentence. */
function locates(observation: ObservedBreak): boolean {
  return observation.confirmed && !observation.cleared && observation.first_break_seq !== null;
}

/** The observation a standing break is drawn from — the newest that names an
 * entry, and only if none does, the newest confirmed one (which still carries
 * the server's sentence even though it named no entry). */
function standingBreak(observed: readonly ObservedBreak[]): ObservedBreak | undefined {
  return observed.find(locates) ?? observed.find((o) => o.confirmed && !o.cleared);
}

/**
 * The durable list, capped — MINUS the one observation the screens draw the
 * standing break FROM.
 *
 * THIS EXCEPTION IS REACHABLE AND LOAD-BEARING; THE REASON THIS COMMENT USED TO
 * GIVE FOR IT WAS NOT. It argued from a seq-less break — a wire the server
 * cannot emit (see `locates`) — and so read as ceremony for an impossible
 * verdict. The eviction it prevents needs nothing but reads this server does
 * emit:
 *
 *   A break is confirmed at 4020 and LATCHES. The race then alternates —
 *   passing read, failing read, passing read — and a passing read RESETS THE
 *   RUN, so every failing read after it lands at run 1 and is recorded
 *   `confirmed: false`. Those observations cannot locate (they are not
 *   confirmed) but they do take slots, and the head-mismatch arm names the
 *   MOVING head, so no two of them are even the same entry. Twenty polls later
 *   the observation that located 4020 is off the end of a newest-first cap, and
 *   the red band, the entry's pill, the dimmed tail, the severed spine and the
 *   `/audit?seq=` deep link have all left the screen while the word BREAK
 *   stayed. That is the same de-location as reading the entry off the newest
 *   confirmed observation, arriving later and by a different route.
 *
 * So the locating observation is retained in place of the oldest of the newest
 * twenty. Everything else still ages out, and the list stays capped.
 */
function capObserved(observed: readonly ObservedBreak[]): ObservedBreak[] {
  if (observed.length <= MAX_OBSERVED) return [...observed];
  const locating = observed.findIndex(locates);
  if (locating < MAX_OBSERVED) return observed.slice(0, MAX_OBSERVED);
  return [...observed.slice(0, MAX_OBSERVED - 1), observed[locating]!];
}

/** Fold one server verdict into the latch. Pure — safe under StrictMode. */
export function applyVerification(
  prev: ChainLatch,
  verification: ChainVerificationView | null,
  at: string,
): ChainLatch {
  if (verification === null) return prev;
  if (verification.valid) {
    // A passing poll resets the RUN but NEVER clears a latched break: that is
    // the whole difference between tamper-evidence and a mood ring. The word is
    // WRITTEN here rather than inherited from `prev`, so I1 holds by
    // construction on this branch instead of by luck.
    //
    // AND IT ONLY RESETS A RUN ITS OWN PROOF COULD HAVE BUILT (I6). Resetting
    // unconditionally is `clearLatch`'s defect one layer earlier and it is the
    // worse half of it: an `anchored-extension` verdict does not read the
    // prefix at all, so against a prefix tampered after the anchor was blessed
    // it answers `valid` on every poll while /trace's unbounded walk answers
    // invalid on every read. Both land in ONE run (the run is per-chain), the
    // reads alternate, and the run went 1, 0, 1, 0 — it never reached
    // BREAK_CONFIRMATIONS, so the break never latched and the `latchedBasis`
    // bar never existed to be raised. The stronger instrument saw the tamper
    // every single time and the weaker one erased the evidence of it.
    //
    // SO: a passing read resets the run when its basis is AT LEAST AS STRONG as
    // the strongest proof the run is made of, and otherwise leaves the run
    // exactly where it was. Strictly weaker is the only case that changes; an
    // equal-strength passing read still resets, which is what keeps the
    // read-during-append race — anchored polls disagreeing with anchored polls,
    // full walks disagreeing with full walks — suppressed exactly as before.
    //
    // AND STRENGTH IS NOT ENOUGH ON ITS OWN (`resetsARun`). Rank compares SCOPE;
    // it cannot compare COVERAGE, and two `windowed` reads are rank-equal while
    // sharing no rows — so equal-strength reset, correct for the two whole-chain
    // bases, let a valid read of one window erase a break found in another. A
    // windowed pass therefore resets nothing at all.
    const resets =
      prev.runBasis === "windowed"
        ? answersAWindowedRun(verification.basis)
        : resetsARun(verification.basis) &&
          strengthOf(verification.basis) >= strengthOf(prev.runBasis);
    const run = resets ? 0 : prev.run;
    const runBasis = resets ? null : prev.runBasis;
    if (prev.latched) return { ...prev, verdict: "break", run, runBasis };
    // NOT `valid` WHILE A STRONGER PROOF'S RUN STANDS. The run surviving means
    // a proof this read cannot answer has disagreed and has not been answered,
    // which is RE-VERIFYING by `chainReading`'s own rule 2 — "a verdict that
    // has disagreed with itself is neither vouched for nor accused". Writing
    // VALID there would let the blind instrument publish the reassurance as
    // well as erase the evidence.
    return { ...prev, verdict: run > 0 ? "reverifying" : "valid", run, runBasis };
  }
  const run = prev.run + 1;
  /**
   * A CONTRADICTION IS BELIEVED ON SIGHT, AND THE RUN COUNT IS NOT WEAKENED TO
   * DO IT.
   *
   * `BREAK_CONFIRMATIONS` is 3 because of ONE race and one only: a page
   * snapshot and the head pointer sampled at different instants while the
   * console's own polling appends, which makes `verify_chain`'s head-mismatch
   * arm name the moving head. That race is a property of a REQUEST, so three
   * spaced reads cannot all straddle it, and the count is exactly right for
   * every ordinary break — which is why it stays 3 here and why this is not a
   * constant to turn down.
   *
   * `contradiction` marks a verdict that race PROVABLY cannot produce.
   * `verified_extension` bounds every check to a head snapshot read FIRST and
   * closes the tail with the same race-free beyond-head epilogue `verify_chain`
   * uses, so a legitimate concurrent append can never make the chain stop
   * extending a head this server verified. When it does stop, something at or
   * below that head was rewritten — the one tamper class an anchored proof is
   * otherwise blind to, and the strongest signal the server has.
   *
   * IT IS ALSO STRUCTURALLY ONE-SHOT: the server installs the freshly verified
   * head before returning, so the very next read PASSES. Requiring three
   * consecutive disagreements of a verdict that appears exactly once is
   * requiring something that cannot happen — the signal could not reach a
   * screen at all, at any poll interval, ever. Flap suppression that discards
   * a signal which by construction never flaps is not suppression, it is a
   * blind spot.
   *
   * Nothing else changes: `run` still counts, an ordinary break still needs its
   * three, and a contradiction that is somehow wrong is withdrawable by the
   * same Re-verify sweep as any other latch.
   */
  const confirmed = verification.contradiction || run >= BREAK_CONFIRMATIONS;
  // I1: a STANDING latch is part of this verdict, not just of the data behind
  // it. Deriving the word from `confirmed` alone withdrew a confirmed break in
  // two polls — one passing read reset the run to 0, and the next disagreeing
  // read then rendered RE-VERIFYING at run 1 while `latched` stayed true and
  // nothing on any screen renders `latched`. The BREAK word, the red band, the
  // entry's pill, the severed spine and the chip's `/audit?seq=` link all left
  // the screen together, which is a worse failure than the flicker this whole
  // file exists to stop.
  const latched = prev.latched || confirmed;
  // THE RUN'S OWN BAR RISES WITH EVERY DISAGREEING READ, latched or not — it is
  // the run that has to survive long enough to latch at all.
  const runBasis = strongest(prev.runBasis, verification.basis);
  const observation: ObservedBreak = {
    at,
    first_break_seq: verification.first_break_seq,
    // `detail` when the server named no `reason` — the CONTRADICTION verdict
    // carries a passing walk underneath it and states itself only in `detail`.
    // Same pair `chainReading` already falls back through for a failing read.
    reason: verification.reason ?? verification.detail,
    run,
    confirmed,
    basis: verification.basis,
    cleared: false,
  };
  return {
    verdict: latched ? "break" : "reverifying",
    run,
    latched,
    // THE BAR RISES WITH THE EVIDENCE. Every disagreeing read taken while the
    // latch stands contributes its proof's strength, so a full walk joining an
    // anchored latch means only a full walk can withdraw it from then on. It
    // never falls: `strongest` is monotone and nothing but `clearLatch` resets
    // it to null.
    // OFF THE RUN, NOT OFF THIS READ ALONE — and that is a correction, not a
    // restatement. This said `strongest(prev.latchedBasis, verification.basis)`
    // while `latchedBasis` is null for every read BEFORE promotion, so the
    // disagreements that BUILT the break contributed nothing to the bar
    // defending it: a run of one full-chain read and two anchored ones latched
    // as `anchored-extension`, and an anchored sweep — a proof that cannot see
    // the prefix the full walk accused — could then withdraw it. The docstring
    // always said the field rises with every disagreeing read; now it does.
    // `runBasis` already IS that accumulation, which is what makes the two
    // rules one rule: the bar a sweep must clear is the bar a passing read must
    // clear, computed once.
    latchedBasis: latched ? strongest(prev.latchedBasis, runBasis) : null,
    runBasis,
    observed: capObserved([observation, ...prev.observed]),
  };
}

/**
 * An explicit re-verification sweep came back. Only this can clear a latch, and
 * only on a passing verdict — a failing sweep re-confirms it immediately.
 *
 * AND ONLY ON A VERDICT AT LEAST AS STRONG AS THE ONE THAT LATCHED IT. A
 * standing break is a finding some proof made; a proof that could not have made
 * that finding cannot unmake it. The concrete case is not hypothetical: a prefix
 * row tampered after the anchor was blessed is invisible to every
 * `anchored-extension` verdict by construction — the proof does not read the
 * prefix — while /trace's unbounded walk sees it and latches. The Re-verify
 * sweep resolves through the shared poll, the poll was anchored, and a passing
 * anchored verdict withdrew a break a full walk had confirmed. Detected, then
 * un-detected, which is worse than never detecting.
 *
 * A refused sweep is not a dead end: /trace re-reads the unbounded
 * `assemble_trace` verdict, and the audit poll's sweep asks the server for the
 * real walk (`full_verify`). So the control an operator has still produces a
 * verdict strong enough to clear anything this console can latch — it just has
 * to actually produce it.
 *
 * Returning `prev` untouched is the whole behaviour on refusal: the latch, the
 * word and the durable list are all exactly as they were, and the next sweep
 * (or the next reading strong enough) can still clear it.
 *
 * AND THE SAME BAR GUARDS THE RUN, not only the latch (I6). A sweep resolves
 * through this function and then through `applyVerification`, so a sweep weak
 * enough to be refused here would have gone on to reset the run in the fold
 * immediately after — erasing, by the operator's own control, the confirmations
 * a stronger proof had accumulated toward a break that has not latched YET.
 * That is the identical defect one instant earlier, so the bar is the strongest
 * proof standing behind EITHER the latch or the run.
 *
 * AND THE BAR IS A RANK, SO IT NEEDS `resetsARun`'s COVERAGE GATE IN FRONT OF
 * IT, for the same reason `applyVerification` does. A break found in one window
 * latches with `latchedBasis: "windowed"`, which ranks 0 — and a sweep resolving
 * through a FILTERED page over a DISJOINT window is also `windowed`, also 0, so
 * `0 < 0` is false and it cleared a latch it had not looked at a single accused
 * row of. An unranked wire basis cleared it too, by the same arithmetic. So
 * while anything stands, only a proof entitled to withdraw a run may withdraw
 * this. With NOTHING standing the bar is null and the gate is skipped — a sweep
 * against a clean latch still normalises the word, which is all it ever did.
 */
export function clearLatch(
  prev: ChainLatch,
  verification: ChainVerificationView | null,
): ChainLatch {
  if (verification === null || !verification.valid) return prev;
  const bar = strongest(prev.latched ? prev.latchedBasis : null, prev.runBasis);
  if (bar !== null && !resetsARun(verification.basis)) return prev;
  if (strengthOf(verification.basis) < strengthOf(bar)) return prev;
  return {
    ...prev,
    verdict: "valid",
    run: 0,
    latched: false,
    latchedBasis: null,
    // The run goes with the latch, and so does its bar: this sweep was strong
    // enough to withdraw the finding, so it is strong enough to withdraw the
    // progress toward the next one.
    runBasis: null,
    // The list stays DURABLE — every transient is still readable — but the
    // breaks in it are now WITHDRAWN, and a withdrawn break may not go on
    // locating a later one it has nothing to do with.
    observed: prev.observed.map((o) => (o.cleared ? o : { ...o, cleared: true })),
  };
}

/** What a verdict strip, a chip or a band may state about the chain. */
export interface ChainReading {
  /** The word to rest on, or `null` when there is nothing to say at all —
   * nothing confirmed AND no read in hand. A `null` word is an ABSENCE the
   * consumer states in its own words ("Loading…", "Unavailable"); it is never
   * VALID, and — I3 — it is never reachable while a break stands. */
  word: StateWord | null;
  /** The entry a CONFIRMED break sits at, or `null` when nothing has named one.
   * Non-null only under BREAK.
   *
   * IT IS THE STANDING OBSERVATION'S ENTRY, NOT THE READ IN HAND'S. It survives
   * a read that names no entry — a PASSING read is exactly what a latched break
   * comes to rest on once the race clears, and `chainBreakSeq` refuses to read
   * `first_break_seq` off one — and it survives a read that names a DIFFERENT
   * entry, which is what an unconfirmed disagreeing read at the moving head
   * does (`seq=head_seq`, verify.py:509) after a passing read reset the run.
   *
   * AN EARLIER REVISION CLAIMED A SECOND SHAPE — a FAILING read carrying no seq
   * — and called it "the race's own documented answer". The server cannot emit
   * it (`_break` takes `seq: int`; see `locates`), so no poll has ever nulled
   * an entry that way and none can today.
   *
   * Among the observations it prefers the newest standing one THAT NAMES AN
   * ENTRY over merely the newest confirmed one. That preference USED to be
   * labelled defensive; it is not. A seq-less break HAS shipped — the
   * CONTRADICTION verdict refuses while its own walk passes, so it names no
   * entry — and `applyVerification` marks every disagreeing read after
   * promotion `confirmed`. One of them arriving after a located break becomes
   * "the newest confirmed observation" and would null the entry — and with it
   * would go /audit's red band
   * (the only `role="alert"` on that screen), the entry's BREAK pill, the
   * dimmed tail, the severed spine (`ledgerSpine` draws an UNBROKEN chain for
   * every row on a null seq) and the chip's `/audit?seq=` deep link. The word
   * BREAK, alone, over a ledger drawn as whole. */
  breakSeq: number | null;
  /** The SERVER's own sentence about the standing break — its `reason`, or its
   * `detail` when it sent no reason — for the one consumer that shouts it.
   * `null` when nothing has stated one.
   *
   * It comes from the STANDING observation — the same one `breakSeq` comes
   * from, so the entry and the sentence are about one record — and from the
   * read at hand only when nothing standing named an entry, and only while that
   * read is ITSELF failing. /trace printed `verification.reason ??
   * verification.detail` off whatever read it happened to hold, so a latched
   * break resting on a PASSING read published that read's "chain intact" inside
   * the red `role="alert"` under the BREAK pill. */
  breakReason: string | null;
}

/**
 * THE ONE READING — what /audit's strip, /trace's strip and the rail chip each
 * render, from one latch, through one mapping.
 *
 * It lives beside the reducer rather than on any screen because the screens
 * read ONE chain: `assemble_trace` and the audit list call the same unbounded
 * `verify_chain`, so a run opened from an audit row is the same verdict the
 * ledger behind it just showed. Latching one screen and leaving the other on
 * the raw per-read `valid` produced exactly that: `⚠ BREAK` on /audit and
 * `● VALID` on /trace, about one chain, at one instant — a contradiction worse
 * than either state on its own, because the reader cannot tell which screen to
 * believe. Sharing the mapping makes agreement structural.
 *
 * THE ORDER OF THE RULES IS THE POINT.
 *
 *   1. A CONFIRMED BREAK OUTRANKS EVERY OTHER ARGUMENT, including the absence
 *      of a read. It is decided FIRST and it does not consult `verification` to
 *      decide the word — only to name the entry. A 502 on the poll nulls the
 *      current read; a screen that let `verification === null` fall through to
 *      an absence, or to its own passing read, published `● VALID · 12,480
 *      records verified` over a chain this console had confirmed broken.
 *   2. RE-VERIFYING is the honest third state and is never skipped: a verdict
 *      that has disagreed with itself is neither vouched for nor accused.
 *   3. With nothing latched and nothing being re-checked, the READ AT HAND
 *      decides — and a read that says invalid reads RE-VERIFYING, never BREAK.
 *      BREAK is reserved for a verdict that has agreed with itself.
 *   4. No read at all, and nothing latched: `null`. A strip must not vouch for
 *      a chain nothing has checked.
 *
 * `verification` is whichever read the WORDS BESIDE THE PILL come from, so the
 * caller passes the read whose `checked` count it is about to print: a record
 * count taken off a read that answered INVALID is a false sentence.
 *
 * AND EVERY CONSUMER MUST RESOLVE THAT READ THE SAME WAY — the shared poll's
 * when it has one, its own only when the poll has none. Rules 1 and 2 are
 * decided from the latch, so they agree across screens no matter what is
 * passed; RULE 3 IS DECIDED BY THE READ, and two screens that pass different
 * reads come to rest on different words about one chain. /trace passed its own
 * trace read while the rail chip two inches above it passed the poll's, so a
 * poll resting `valid` beside a disagreeing trace read left `● VALID` on the
 * chip and `RE-VERIFYING` on the strip, permanently — the very contradiction
 * this mapping exists to make impossible, surviving in the one arm the latch
 * does not cover. A screen's own read is still a real observation: it is folded
 * into the ONE latch (`observe`), where it counts toward promotion and can
 * confirm a break on its own for a principal whose poll is refused.
 */
export function chainReading(
  verdict: ChainVerdict,
  verification: ChainVerificationView | null,
  observed: readonly ObservedBreak[],
): ChainReading {
  if (verdict === "break") {
    // THE STANDING OBSERVATION IS THE AUTHORITY ON WHERE AND WHY — NOT THE READ
    // IN HAND, WHICH NEED NOT BE PART OF THE RUN THAT CONFIRMED ANYTHING.
    //
    // A latched break is never withdrawn by a passing read, but a passing read
    // DOES reset the run — so the next single disagreeing read lands at run 1
    // and is recorded `confirmed: false`, while the word stays BREAK off the
    // latch. Preferring that read's own `first_break_seq` moved the standing
    // break to the entry ONE UNCONFIRMED read named, and the head-mismatch arm
    // names the MOVING HEAD (`seq=head_seq`, verify.py:509), so the entry was a
    // different one each time and moved back on the next passing poll: /audit's
    // red band (its only `role="alert"`), the entry's BREAK pill, the dimmed
    // tail, the severed spine and the chip's `/audit?seq=` deep link all
    // pointed, alternately, at a record no confirmed read ever accused. Tamper
    // evidence that oscillates locates nothing, and the record it lands on in
    // between is one the server never named for the break that is standing.
    //
    // NOTHING IS LOST BY PREFERRING THE OBSERVATION. `applyVerification` folds
    // a read into `observed` BEFORE any consumer reads the latch, so the read
    // that PROMOTED a break is already the newest standing observation when
    // this runs — the preference above could only ever have changed the answer
    // for a read that had NOT confirmed anything.
    //
    // AND THE TWO COME FROM ONE OBSERVATION, so the entry and the sentence
    // cannot describe two different records. The read in hand is consulted only
    // when NOTHING standing named an entry, and only while it is itself FAILING
    // — a passing read's own "chain intact" printed inside the red alert under
    // a BREAK pill is the sentence this arm exists to keep out.
    const standing = standingBreak(observed);
    if (standing !== undefined) {
      return {
        word: "BREAK",
        breakSeq: standing.first_break_seq,
        breakReason: standing.reason,
      };
    }
    const failing = verification !== null && !verification.valid ? verification : null;
    return {
      word: "BREAK",
      breakSeq: chainBreakSeq(failing),
      breakReason: failing?.reason ?? failing?.detail ?? null,
    };
  }
  if (verdict === "reverifying") {
    return { word: "RE-VERIFYING", breakSeq: null, breakReason: null };
  }
  if (verification === null) return { word: null, breakSeq: null, breakReason: null };
  return {
    word: verification.valid ? "VALID" : "RE-VERIFYING",
    breakSeq: null,
    breakReason: null,
  };
}
