// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §13b — `/trace/{trace_id}`: what one run actually did.
//
// A DRILL-DOWN, NOT A DESTINATION. It is reached from an audit row, an answer's
// tool row and an approval receipt, and it has no rail entry. What left this
// screen with that ruling: the run-a-delegation trigger (a palette action now),
// the "recent runs" list folded out of the audit poll, and the paste-an-id form
// — three ways to arrive at a screen nobody arrives at by browsing.
//
// What stays, restyled to §2: the rollup as three stat tiles (Events · Duration
// · Cost, and an unpriced lane reads "Unpriced", never $0.00), the per-model
// aggregates as a sortable table, the span tree, and the server's verification
// line.
//
// NO TOOLBAR, NO SORT ON THE TREE, AND BOTH ARE DELIBERATE. A run is one object,
// not a list: the aggregates table is the only list here and it sorts on every
// column, while the span tree's order IS the run's order — sorting it would
// destroy the one thing it is for. There is nothing to filter within a single
// run, so it carries no search-within.
//
// THIN CLIENT (C6/C7): every cost, duration, count and verdict here is a server
// field. This screen sums nothing — the run total is the server's rollup, which
// may legitimately differ from the aggregates beside it, and when it does, both
// are still rendered as sent.

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router";
import { ApiError } from "../api/client";
import type {
  TraceAssemblyView,
  TraceModelAggregateView,
  TraceNodeView,
  TraceVerificationView,
} from "../api/types";
import { BREAK_CONFIRMATIONS, chainReading } from "../audit/chainVerdict";
import { useApiClient } from "../auth/useApiClient";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { useLatestRequest } from "../hooks/useLatestRequest";
import { DataTable } from "../primitives/DataTable";
import type { Column } from "../primitives/DataTable";
import { HashChip } from "../primitives/HashChip";
import { ListScreen } from "../primitives/PageHeader";
import { StatusPill } from "../primitives/StatePill";
import type { StateWord } from "../primitives/StatePill";
import { ToolbarButton } from "../primitives/Toolbar";
import { Empty, Loading, LoadError, Refused } from "../primitives/states";
import { formatCount, formatDuration, formatMoney } from "../primitives/format";
import { formatTokens } from "./format";
import { SpanPane } from "./SpanPane";
import { SpanTree } from "./SpanTree";
import "./trace.css";

type Status = "loading" | "ok" | "refused" | "notfound" | "error";
type SpanSelection = { node: TraceNodeView; opener: HTMLElement };

/**
 * THE SPACING BETWEEN RE-READS, and why it is not one constant.
 *
 * `BREAK_CONFIRMATIONS` counts consecutive disagreeing reads, and the count is
 * only worth what the reads' INDEPENDENCE is worth. Nothing bounds how wide the
 * server's append window is, so three reads taken 400ms apart can all straddle
 * one append and confirm a break that is not there — and on a drill-down that
 * verdict then rests, for this visit, on a chain nothing is wrong with.
 *
 * So the first re-read stays quick and each later one widens. The common case
 * is a single straddled append that the very next read withdraws, and making an
 * operator watch RE-VERIFYING for seconds over nothing is its own harm; but the
 * read that can PROMOTE to BREAK is taken 1.6s after the one before it rather
 * than 400ms, which is an order of magnitude past the per-REQUEST window that
 * produces this race. A genuine break stands about two seconds after the run is
 * opened, and the screen states RE-VERIFYING for that whole time.
 *
 * WIDENING IS NOT A PROOF, and no constant here could be. A chain being
 * appended to continuously makes every read disagree at any spacing, and the
 * console cannot tell that apart from a real break — which is why this screen
 * also carries Re-verify. The delay makes a false latch unlikely; the control
 * makes one recoverable. Going wider buys diminishing independence against a
 * per-request race while charging every genuine break for it.
 *
 * AND THE RE-READS ARE BOUNDED HERE, not by the shared run counter. The run
 * this screen's reads land in is the CHAIN's, and the shell's 5s poll folds
 * into it too — a passing poll resets it. Bounding on that could leave a
 * drill-down re-reading the audit service indefinitely while the race flips the
 * run back and forth, so this screen counts its OWN reads and stops at
 * BREAK_CONFIRMATIONS of them per visit. What it does not resolve, the shell's
 * poll resolves within one interval — on the same latch, in the same words.
 *
 * THE BUDGET IS THIS SCREEN'S; THE SPACING IS THE RUN'S, and they are different
 * numbers for the same reason. How many reads a drill-down may PAY FOR is a
 * fact about this screen. How independent the read that can PROMOTE has to be
 * is a fact about the run — which every reader of the chain folds into — so
 * widening on this screen's fold count scheduled the promoting read 400ms out
 * whenever the shell's poll had already contributed a disagreeing read.
 *
 * THE BUDGET COUNTS READS THAT WERE FOLDED, NOT READS THAT WERE TAKEN, and the
 * difference is a whole visit. `observe` DISCARDS a read taken before the
 * session the latch is bound to has resolved, and this screen used to increment
 * on every attempt: a cold deep link where /v1/me took over two seconds spent
 * the reads at ~0s, 0.4s and 2.0s on observations the latch threw away, then
 * disabled itself. For a principal whose audit poll is refused (no `ops`) there
 * was no other reader of the chain, so the strip stated "Checking again" for
 * the rest of the visit over a chain the server had called broken on every
 * read, and only three manual Re-verify clicks could get it back. A discarded
 * read is also not worth RE-TAKING on a timer: `observe`'s identity moves with
 * the owner, so the session resolving re-runs the read effect on its own and
 * takes the read again under the session that can hold it.
 */
const REVERIFY_DELAY_MS = 400;
const REVERIFY_BACKOFF = 4;

/** The paragraph the verdict line reveals — and, through `aria-describedby`,
 * the line's accessible DESCRIPTION whether it is revealed or not. One per
 * screen: this strip renders once. */
const VERDICT_REACH_ID = "trace-verdict-reach";

/**
 * The sentence beside the pill, in words an operator has — and never a claim
 * the server did not make. The seq-less arm is DEFENSIVE for an ordinary break
 * (`_break`, verify.py:180, takes `seq: int`) but it RENDERS for a
 * CONTRADICTION: that verdict carries a passing `verify_chain` underneath it,
 * so it names no entry and says so rather than printing "Break at entry —",
 * which reads as a sentence that lost its number rather than as a fact about
 * the chain.
 *
 * THE VALID ARM IS THE ONE THIS FILE GOT WRONG, AND IT IS THE WORST PLACE TO
 * GET IT WRONG. It printed `Entire audit chain · N records verified` off
 * WHICHEVER read was in hand — and this screen prefers the shared audit poll's
 * read to its own. That poll's verdict stopped being a full walk: it is a
 * prefix a `verify_chain` blessed up to fifteen minutes ago plus the rows
 * appended since, re-hashed now. So the strongest sentence in the console —
 * "entire", "verified", no qualifier — was being written over the weaker of the
 * two verdicts available, on the screen that had the stronger one. It was also
 * a stronger claim than /audit makes about the very same read.
 *
 * The verb now follows the server's own `basis`:
 *
 *   * `full-chain` — a real unbounded walk re-hashed every record on that
 *     request. "verified" is exactly what happened, and nothing here is
 *     softened for a proof that was actually taken.
 *   * `anchored-extension` — the verdict COVERS the whole chain and the work on
 *     that call did not. "covered", the same verb /audit settled on, and the
 *     server's own account of the split rides `chainScope`'s tooltip.
 *   * `windowed` — not a whole-chain reading at all, so "Entire audit chain"
 *     may not lead the sentence. It does not reach this screen today (the poll
 *     sends no window and `assemble_trace` walks unbounded), and if it ever
 *     does it will state its own scope instead of borrowing one.
 */
function chainLine(
  word: StateWord,
  verification: TraceVerificationView,
  breakSeq: number | null,
): string {
  if (word === "BREAK") {
    // The CONFIRMED break's entry, not this read's field: a latched break comes
    // to rest on a passing read once the race clears, and a PASSING read is the
    // one read that names no entry (`_ok`, verify.py:169). Same pair /audit and
    // the rail chip use.
    return breakSeq === null
      ? "Entire audit chain · Break confirmed — the server did not name the entry"
      : `Entire audit chain · Break at entry ${formatCount(breakSeq)}`;
  }
  if (word === "RE-VERIFYING") {
    return "Entire audit chain · Checking again — this reading has not been confirmed yet";
  }
  if (verification.basis === "windowed") {
    return `Selected window · ${formatCount(verification.checked)} records covered`;
  }
  const verb = verification.basis === "full-chain" ? "verified" : "covered";
  return `Entire audit chain · ${formatCount(verification.checked)} records ${verb}`;
}

/**
 * The SERVER'S OWN sentence about how far this verdict's proof reaches —
 * `ChainVerificationView.detail`, verbatim, never summarised — for the reads
 * whose reach is not the whole sentence already.
 *
 * Same rule, same words and same DISCLOSURE as /audit's strip, because it is
 * the same verdict: this screen renders the shared poll's read whenever it has
 * one, so the two screens are describing one read and must describe it
 * identically. On a `full-chain` verdict there is nothing left to disclose —
 * "N records verified" IS the whole claim and `detail` only restates it — so
 * the marker stays off and the line stands alone.
 *
 * IT WAS A `title` ATTRIBUTE ON A NON-FOCUSABLE SPAN — mouse-only. No Tab
 * reaches a `title`, no tap reveals one, and screen readers announce them
 * inconsistently at best. /audit had the identical defect and was fixed; this
 * screen kept it, and here it is WORSE: this strip leads with the words
 * "Entire audit chain", so the qualification is the ONLY thing standing
 * between an `anchored-extension` verdict and an outright overclaim. A reader
 * without a pointer was shown `Entire audit chain · 12,480 records covered`
 * with the sentence explaining what was NOT re-checked withheld entirely.
 * It is now the line's accessible description AND the text of the paragraph
 * the line reveals, so focus announces it and Enter/Space/tap puts it on the
 * screen.
 *
 * ONLY UNDER VALID. Under BREAK the read in hand is routinely a PASSING one (a
 * latched break comes to rest on passing reads), so its `detail` is "chain
 * intact: …", and hanging that off an accusation is the contradiction
 * `breakReason` was narrowed to keep out. Under RE-VERIFYING there is no
 * verdict yet to state the reach of.
 */
function chainScope(word: StateWord, verification: TraceVerificationView): string | null {
  if (word !== "VALID" || verification.basis === "full-chain") return null;
  const detail = verification.detail.trim();
  return detail.length > 0 ? detail : null;
}

/** Per-model aggregates (§13b: "aggregates as a §2.1 table"). Rendered in the
 * server's order until a header is clicked; every column sorts. */
const MODEL_COLUMNS: readonly Column<TraceModelAggregateView>[] = [
  {
    key: "model",
    header: "Model",
    mono: true,
    cell: (row) => row.model,
    sortValue: (row) => row.model,
    searchText: (row) => row.model,
  },
  {
    key: "lane",
    header: "Lane",
    mono: true,
    cell: (row) => row.lane,
    sortValue: (row) => row.lane,
    searchText: (row) => row.lane ?? "",
  },
  {
    // WHICH TASKS THIS MODEL SERVED — the run's task→model routing, read
    // without opening a span. The server's own list, joined for display and
    // sorted/searched on the same text; a run whose records never named a
    // task renders absent rather than an empty cell.
    key: "tasks",
    header: "Tasks",
    cell: (row) => (row.tasks?.length ? row.tasks.join(", ") : null),
    sortValue: (row) => (row.tasks?.length ? row.tasks.join(", ") : null),
    searchText: (row) => row.tasks?.join(" ") ?? "",
  },
  {
    key: "calls",
    header: "Calls",
    numeric: true,
    firstSort: "desc",
    cell: (row) => formatCount(row.calls),
    sortValue: (row) => row.calls,
  },
  {
    key: "input",
    header: "Input tokens",
    numeric: true,
    firstSort: "desc",
    cell: (row) => formatTokens(row.input_tokens),
    sortValue: (row) => (row.input_tokens === 0 ? null : row.input_tokens),
  },
  {
    key: "output",
    header: "Output tokens",
    numeric: true,
    firstSort: "desc",
    cell: (row) => formatTokens(row.output_tokens),
    sortValue: (row) => (row.output_tokens === 0 ? null : row.output_tokens),
  },
  {
    key: "cost",
    header: "Cost",
    numeric: true,
    firstSort: "desc",
    // The money format is two decimals (§2.7), which reads two sub-cent lane
    // costs the same. The served figure is exact in `title`, so nothing the
    // rounding hides is lost.
    cell: (row) => <Money value={row.cost_usd} />,
    sortValue: (row) => row.cost_usd,
  },
];

export function TracePage() {
  const { traceId } = useParams<{ traceId?: string }>();
  // KEYED on the id so navigating between two runs fully remounts the view.
  if (!traceId) return <NoRun />;
  return <TraceView key={traceId} traceId={traceId} />;
}

/** The route with no id. A trace is opened ABOUT something, so this states where
 * one comes from rather than offering a box to paste an id nobody has memorised. */
function NoRun() {
  return (
    <ListScreen title="Trace">
      <Empty
        message="No run selected. Open a trace from an audit record, an answer, or an approval."
        action={<Link to="/audit">Open audit log</Link>}
      />
    </ListScreen>
  );
}

function TraceView({ traceId }: { traceId: string }) {
  const client = useApiClient();
  const runLatest = useLatestRequest();
  const [view, setView] = useState<TraceAssemblyView | null>(null);
  const [status, setStatus] = useState<Status>("loading");
  const [selected, setSelected] = useState<SpanSelection | null>(null);
  const [attempt, setAttempt] = useState(0);
  /** How many of THIS visit's reads were actually FOLDED into the shared latch.
   * Bounds the loop below and sets its widening delay — see REVERIFY_BACKOFF.
   * Reads `observe` discarded are not in it: they cost the verdict nothing, so
   * they may not cost it its budget either. */
  const [folds, setFolds] = useState(0);
  /** Set by Re-verify: the NEXT read is an explicit re-verification sweep — the
   * one thing that can clear a standing latch, and only if it comes back
   * passing. A ref, not state: it must not re-render, and it is consumed by the
   * read it arms. */
  const sweeping = useRef(false);
  /** THE VERDICT'S REACH, SHOWN ON DEMAND — see `chainScope`. Collapsed by
   * default so the strip stays one scannable line; expanded by the verdict line
   * itself, which is a real control and therefore reachable with a Tab and
   * operable with a tap. It was a `title` on a non-focusable span, which no
   * keyboard and no touch screen can reach. */
  const [reachShown, setReachShown] = useState(false);
  // THE SHARED CHAIN LATCH (audit/chainVerdict.ts) — the shell's, not a second
  // copy of it.
  //
  // This screen used to render `verification.valid` from ONE response. The
  // server has a REPRODUCED read-during-append race — roughly half of the reads
  // taken while the chain is being appended to come back
  // `{"valid": false, "reason": "head mismatch (truncated or forged tail)"}` —
  // so a run opened at the wrong instant flashed `⚠ BREAK` and the word
  // "forged" at whoever was watching, over a chain nothing was wrong with.
  //
  // It then kept its OWN latch, which was the same defect one level up. /trace
  // is a child of the layout that renders the rail's <StatusChip/>, and both
  // sit inside AuditPollProvider (App.tsx: every route is under AuthedLayout),
  // so the two latches rendered SIDE BY SIDE: the chip could read "Chain break
  // at entry 4812" in the fault colour while the strip two inches below it read
  // `● VALID`, or the reverse — and each screen's Re-verify cleared only its
  // own. One chain, one latch: this screen folds its read into the shell's
  // through `observe` and renders `poll.verdict`, so agreement is structural
  // rather than something two reducers have to keep arriving at separately.
  //
  // A GENUINE break returns invalid on every read, so it still promotes here —
  // in about a second — and then stands, on this screen and on the chip at once.
  const poll = useAuditPoll();
  const { observe } = poll;

  useEffect(() => {
    void (async () => {
      const settled = await runLatest(() => client.trace(traceId));
      if (settled.kind === "superseded") return;
      // Armed by Re-verify and spent HERE, on the read it armed: a sweep that
      // never lands must not be redeemed by some later ordinary read.
      const sweep = sweeping.current;
      sweeping.current = false;
      if (settled.kind === "success") {
        setView(settled.value);
        setStatus("ok");
        // One read = one observation, folded into the SHELL's latch — the same
        // one the chip renders. `runLatest` reports every superseded read
        // (StrictMode's discarded first mount included) above, so no single
        // response is ever counted twice and one straddled append can never
        // spend two confirmations.
        // …and the budget is spent on what the latch TOOK. `observe` returns
        // false for a read it discarded, which is every read taken before the
        // session it must be bound to resolved.
        if (observe(settled.value.verification, { sweep })) setFolds((n) => n + 1);
        return;
      }
      setView(null);
      const error = settled.error;
      // THREE DIFFERENT FACTS, THREE DIFFERENT STATES. A role boundary, a run
      // that does not exist and an unreachable read are not interchangeable, and
      // telling a reader their role was the problem when the server never
      // answered is a false statement about authorization.
      if (error instanceof ApiError && error.status === 403) setStatus("refused");
      else if (error instanceof ApiError && error.status === 404) setStatus("notfound");
      else setStatus("error");
    })();
  }, [attempt, client, observe, runLatest, traceId]);

  /** THE ONE READING — the shell's latch, over THE READ THE CHIP ALSO RESOLVES.
   *
   * The word, the sentence and the alert are all decided here so they cannot
   * drift into disagreeing about one verdict, and the verdict itself is the
   * shared one, so this strip and the chip above it cannot either. A confirmed
   * break is decided from the latch before any read is consulted — so a passing
   * read of a chain the console has confirmed broken states the break, at the
   * entry the confirming observation named, rather than withdrawing it.
   *
   * AND THE POLL'S READ IS PREFERRED TO THIS SCREEN'S, which is the half that
   * was missing. A confirmed break and a re-verifying run are decided by the
   * latch and agree everywhere by construction; the REMAINING arm — nothing
   * latched, nothing being re-checked — is decided by the read in hand, and
   * this screen passed its own while <StatusChip/>, rendered by the layout this
   * route is an outlet of, passed the poll's. So a poll resting `valid` beside
   * a trace read that disagreed left `● VALID` on the chip and `RE-VERIFYING`
   * on this strip, two inches apart, about one chain, with nothing left to
   * settle it: this screen stops re-reading once BREAK_CONFIRMATIONS of its own
   * reads have been folded, and the poll's passing reads keep resetting the run.
   * One chain, one latch, and now ONE READ under it.
   *
   * THIS SCREEN'S READ IS STILL A REAL OBSERVATION. It is folded into the one
   * latch above (`observe`) before anything is rendered, so it counts toward
   * promotion, it still confirms a break on its own for a principal whose poll
   * is REFUSED (the poll then has no read at all and this one decides), and a
   * disagreement it sees reaches the chip as RE-VERIFYING rather than being
   * kept to itself. What it no longer does is state a private word.
   *
   * AND THE PREFERENCE IS NOT A LICENCE TO OVERSTATE THE READ IT PREFERS. The
   * poll's verdict stopped being a full walk — it is an anchored extension
   * proof — while this strip went on printing "Entire audit chain · N records
   * verified" over it, unqualified: the stronger sentence, over the weaker of
   * the two verdicts in hand, on the screen that had the stronger one. The fix
   * is NOT to swap back to this screen's own read. Doing that re-opens exactly
   * the contradiction the preference closed (the chip two inches above renders
   * the poll's, and `chainReading`'s rule 3 is decided by the read, so the two
   * come to rest on different words about one chain) and it buys nothing the
   * latch does not already deliver — this screen's full walk is FOLDED, so
   * anything it disagrees about reaches every screen through the latch. What
   * was wrong was the sentence, not the choice of read: `chainLine` now takes
   * its verb from the server's own `basis` and `chainScope` discloses the
   * reach, so the preferred read is described as what it is.
   */
  const chain = view === null ? null : (poll.verification ?? view.verification);
  const reading = chain === null ? null : chainReading(poll.verdict, chain, poll.observedBreaks);
  const word = reading?.word ?? null;
  /** The server's own sentence about a STANDING break — never the passing read's
   * "chain intact", which is what `verification.reason ?? verification.detail`
   * printed inside the red alert once a latch came to rest on a passing read. */
  const breakReason = reading?.breakReason ?? null;
  /** The reach of the verdict the line states, in the server's own words —
   * present exactly when the read in hand is NOT a full walk, so the line's
   * "covered" is never left to stand as the whole account of the proof. */
  const verdictScope = chain === null || word === null ? null : chainScope(word, chain);
  /** THE SHARED RUN — how many consecutive disagreeing reads of THIS CHAIN the
   * one latch is holding, from every reader of it, not just this screen's.
   *
   * It is the newest observation's `run`, and under RE-VERIFYING that IS the
   * current run: the word is only RE-VERIFYING when the last fold was invalid,
   * a valid fold rests the word on VALID or BREAK instead, and every invalid
   * fold unshifts the observation carrying its own run. It sets the spacing
   * below, so the read that can PROMOTE is spaced whether the reads before it
   * were this screen's or the shell's poll's. */
  const sharedRun = poll.observedBreaks[0]?.run ?? 0;

  // A verdict that has not agreed with itself yet is not a resting state, so the
  // screen goes and asks again rather than leaving RE-VERIFYING on the strip.
  // Bounded by THIS SCREEN'S OWN folds (see REVERIFY_BACKOFF): the shared run
  // can be reset by a passing poll mid-race, and a drill-down must not re-read
  // the audit service for as long as that lasts. What this does not settle, the
  // shell's poll settles on the same latch.
  //
  // ONE FOLD SCHEDULES THE NEXT READ, which is what `folds` in the deps buys.
  // A read the latch DISCARDED leaves the count where it was, so it schedules
  // nothing — and needs to schedule nothing: the only reason a read is
  // discarded is that the session it must be bound to has not resolved, and
  // that resolving moves `observe`'s identity and re-runs the read effect
  // above by itself. The bound is unchanged and now bites on the right thing:
  // at most BREAK_CONFIRMATIONS folded reads per visit, which is exactly enough
  // for this screen to promote a break on its own when nothing else is reading
  // the chain, and no more.
  useEffect(() => {
    if (word !== "RE-VERIFYING") return;
    if (folds === 0 || folds >= BREAK_CONFIRMATIONS) return;
    // Widening per consecutive disagreement (see REVERIFY_BACKOFF): the read
    // that can promote is the one that most needs to be independent of the
    // append the read before it may have straddled. The exponent counts the
    // spaced re-reads, so the FIRST one is still the quick 400ms.
    //
    // AND IT COUNTS THE RUN, NOT THIS SCREEN'S FOLDS. Promotion is decided by
    // the SHARED run — the shell's 5s poll reads the same chain and lands in
    // the same run — while the exponent used this screen's own fold count, and
    // the two are not the same number the moment the poll has contributed. One
    // disagreeing poll read, then this screen's first fold, and the run stands
    // at 2: the next read is the PROMOTING one and it was scheduled 400ms out,
    // which is inside the per-request window this whole race lives in. Three
    // reads that all straddle one append then latch a global BREAK — the word
    // "forged" in a header, over an intact chain, until someone clicks
    // Re-verify. The exponent is the run's, so the read that can promote is
    // always the widely-spaced one, whoever took the reads before it.
    const delay = REVERIFY_DELAY_MS * REVERIFY_BACKOFF ** Math.max(sharedRun - 1, 0);
    const timer = setTimeout(() => setAttempt((n) => n + 1), delay);
    return () => clearTimeout(timer);
  }, [folds, sharedRun, word]);

  const retry = useCallback(() => {
    setStatus("loading");
    setFolds(0);
    setAttempt((n) => n + 1);
  }, []);

  /** THE ONE THING THAT CAN CLEAR A LATCH, and the affordance this screen was
   * missing: it could confirm a break but never withdraw one, so a latch this
   * screen got wrong stood for the whole visit with no way back. Same control,
   * same words and same rule as /audit's — read the run again, and clear the
   * standing break only if that read passes. It clears the SHARED latch now, so
   * the chip stops naming a break the same instant this strip does. */
  const reverify = useCallback(() => {
    sweeping.current = true;
    setFolds(0);
    setAttempt((n) => n + 1);
  }, []);

  return (
    <ListScreen title="Trace">
      {status === "loading" ? <Loading things="this run" /> : null}
      {status === "refused" ? <Refused thing="traces" page="Trace" /> : null}
      {status === "error" ? <LoadError things="this run" onRetry={retry} /> : null}
      {status === "notfound" ? (
        <Empty
          message="No records for this run."
          action={<Link to="/audit">Open audit log</Link>}
        />
      ) : null}

      {status === "ok" && view !== null && chain !== null && word !== null ? (
        <div className="ne-trace">
          <div className="ne-trace__overview">
            <div className="ne-trace__head">
              <p className="ne-trace__verdict">
                <StatusPill word={word} />
                {verdictScope === null ? (
                  // NO SENTENCE, NO CONTROL. A button that reveals nothing is a
                  // promise the server did not make — under BREAK, under
                  // RE-VERIFYING, on a full walk and whenever `detail` came back
                  // empty, the line is plain text exactly as before.
                  <span>{chainLine(word, chain, reading?.breakSeq ?? null)}</span>
                ) : (
                  // THE LINE IS THE DISCLOSURE'S BUTTON. Its accessible NAME is
                  // the claim ("Entire audit chain · 12,480 records covered");
                  // its accessible DESCRIPTION is the server's scope sentence,
                  // verbatim, read out of the paragraph below — so focus alone
                  // announces the caveat, and Enter/Space/tap puts it on the
                  // screen for everyone else.
                  <button
                    type="button"
                    className="ne-trace__verdictline--scoped"
                    aria-expanded={reachShown}
                    aria-controls={VERDICT_REACH_ID}
                    aria-describedby={VERDICT_REACH_ID}
                    onClick={() => setReachShown((shown) => !shown)}
                  >
                    {chainLine(word, chain, reading?.breakSeq ?? null)}
                  </button>
                )}
                <ToolbarButton label="Re-verify" onClick={reverify} />
              </p>
              <p className="ne-trace__run">
                <span className="ne-trace__runlabel">Run</span>
                <HashChip hash={view.trace_id} label="Copy run id" />
              </p>
            </div>

            {/* THE VERDICT'S REACH, IN THE SERVER'S OWN WORDS — never
                summarised, never reassembled here. It sits inside the overview
                card, directly under the head row it qualifies and above the
                tiles, which is where this screen already puts the sentences
                that belong to the verdict (the break band is the next one
                down).
                ALWAYS IN THE DOM WHEN THERE IS A SENTENCE TO STATE, `hidden`
                when collapsed. `aria-describedby` on a hidden element is
                included in the accessible description by construction
                (accname), which is the whole point: the caveat reaches a screen
                reader on focus, before anyone decides whether to press.
                IT CANNOT COLLIDE WITH THE BREAK BAND. `chainScope` answers only
                under VALID, so a screen showing this paragraph is a screen with
                no band, and the band's red is never competed with. */}
            {verdictScope === null ? null : (
              <p id={VERDICT_REACH_ID} className="ne-trace__reach" hidden={!reachShown}>
                {verdictScope}
              </p>
            )}

            {/* The server's own sentence — which contains the word "forged" —
                rides a CONFIRMED break only. Under RE-VERIFYING the strip
                already states, in as many words, that a reading disagreed and
                is being checked: nothing is hidden, and an accusation the next
                read may withdraw is not shouted in an alert first.

                It is the STANDING break's sentence, not the sentence of
                whatever read this screen is holding. Those came apart the
                moment a latch came to rest on a passing read: the alert then
                printed that read's `detail` — "chain intact" — in red, under
                the BREAK pill. A break with no served sentence behind it says
                nothing here rather than borrowing one. */}
            {word === "BREAK" && breakReason !== null ? (
              <p className="ne-trace__break" role="alert">
                {breakReason}
              </p>
            ) : null}

            <div className="ne-trace__tiles">
              <Tile label="Events" value={formatCount(view.rollup.total_events)} />
              <Tile label="Duration" value={formatDuration(view.rollup.duration_ms)} />
              <Tile label="Cost" value={<Money value={view.rollup.total_cost_usd} />} />
            </div>
          </div>

          <section className="ne-trace__section">
            <div className="ne-trace__sectionhead">
              <div>
                <h2 className="ne-trace__heading">Run timeline</h2>
                <p className="ne-trace__sectioncopy">
                  Server sequence and hierarchy. Select an event for exact fields.
                </p>
              </div>
            </div>
            <div className="ne-trace__body">
              <SpanTree
                nodes={view.nodes}
                capability="interactive"
                selectedId={selected?.node.event_id ?? null}
                onSelect={(node, opener) => setSelected({ node, opener })}
              />
              {selected ? (
                <SpanPane
                  node={selected.node}
                  traceId={view.trace_id}
                  returnFocusTo={selected.opener}
                  onClose={() => setSelected(null)}
                />
              ) : null}
            </div>
          </section>

          <section className="ne-trace__section">
            <h2 className="ne-trace__heading">Models</h2>
            <DataTable<TraceModelAggregateView>
              columns={MODEL_COLUMNS}
              rows={view.models}
              rowKey={(row) => row.model}
              label="Models"
              things="models"
              empty={<Empty message="No model call in this run." />}
            />
          </section>
        </div>
      ) : null}
    </ListScreen>
  );
}

/** A cost, in the console's money format, with the server's exact figure in
 * `title`. A lane the server does not price reads "Unpriced" — never $0.00,
 * and the hover must agree: a cell whose text says "Unpriced" carries no
 * `title` claiming a zero-dollar figure. */
function Money({ value }: { value: number | null }) {
  const text = formatMoney(value);
  return <span title={text === "Unpriced" ? undefined : `${value} USD`}>{text}</span>;
}

/** A rollup stat tile: label, value. No sub-line — the three facts are the run's
 * own, and there is nothing to qualify. */
function Tile({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="ne-trace__tile">
      <span className="ne-trace__tilelabel">{label}</span>
      <span className="ne-trace__tilevalue">{value}</span>
    </div>
  );
}
