// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ANSWER ARRIVES HERE.
//
// Canonical Ask's SSE carries ONE frame — `accepted` — and closes
// (ne-api routers/chat.py: the generator yields the acceptance and ends). There
// are no token frames, no `done`, no output bytes on that wire. So the turn's
// answer cannot come from the stream; it comes from the Run the acceptance
// named, read back until the Run settles.
//
// WHY POLLING AND NOT A HELD STREAM. The deciding case is a Tier-gated turn.
// Meridian Analyst holds `draft.email.create`, so a turn can sit in
// `waiting_approval` for minutes to hours while the deployment's turn timeout
// bounds any open connection. A held SSE either dies mid-approval or needs a
// new paused-frame-and-reconnect wire contract on both ends. A poll degrades:
// it survives a refresh, it survives a laptop lid, and the same Run URL shows
// the same state to anyone who opens it.
//
// THE COST OF GETTING THE INTERVAL WRONG is a console that hammers a governed
// API, so the interval is not a constant. It starts fast because a short
// question really does finish in a second or two, grows geometrically, and
// separates the two waits that look identical from here: a Run that is WORKING
// is checked on a machine's timescale, and a Run PARKED ON A HUMAN DECISION is
// checked on a human's. And every watch stops — at the Run's terminal state, at
// unmount, or after this many consecutive unreadable responses.
//
// NOTHING HERE INTERPRETS A RUN. This module decides only WHEN to read and WHEN
// to stop; what a settled Run means for the thread is the caller's (see
// useAgentStream's `accepted` case). It writes no verdict, no text and no
// failure of its own.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { reconcileRunEvents } from "../agent-harness/adapters/runWorkspace";
import { ApiError } from "../api/client";
import { SseError } from "../api/sse";
import type { AgentRunDetailWire, AgentRunEventPageWire, AgentRunEventWire } from "../api/types";
import type { AgentRunStatusWire } from "../api/types";

/** The first read, soon after acceptance: a short question is often already
 * done, and waiting a full second to say so is a second of blank screen. */
export const RUN_POLL_FIRST_MS = 700;

/** Geometric, so the first seconds are dense and the cadence then settles:
 * 0.7s, 1.1s, 1.8s, then the ceiling. */
export const RUN_POLL_GROWTH = 1.6;

/** The ceiling while the Run is WORKING.
 *
 * TWO SECONDS BECAUSE THE BAND IS LIVE NOW. This used to be six, which was a
 * defensible ceiling when the poll's only job was to notice that a Run had
 * finished. It is the wrong ceiling for a feed: a governed turn records ~40
 * events over 60–90 s, and at six seconds the rows arrived in visible clumps
 * with five-second holes between them — a screen that looked stalled while the
 * Run was working, which is the exact failure this whole change exists to end.
 * At two seconds a 90-second turn costs ~45 reads and the band grows steadily.
 * The Run's OWN timestamps still date every row, so a slow tick delays when a
 * row appears and never what it says. */
export const RUN_POLL_MAX_MS = 2_000;

/** The ceiling while the Run is PARKED ON A HUMAN. An approval is answered in
 * minutes or hours, not seconds; polling it at the working cadence would spend
 * hundreds of governed reads to learn nothing. Twenty seconds is still prompt
 * on the scale of the thing being waited for. */
export const RUN_POLL_PAUSED_MAX_MS = 20_000;

/** Consecutive unreadable responses before the watch gives up and SAYS SO. A
 * single 502 or a dropped connection is not a verdict, so transient failures
 * are retried on the same backoff; a wall of them is a fact the reader needs,
 * because the alternative is a turn that spins forever over a dead read. */
export const RUN_POLL_MAX_ERRORS = 6;

/** Event pages drained in ONE tick. The cursor only advances when the server
 * hands back a truncated page, so a Run that has out-run the page size is
 * caught up over a few ticks rather than in one unbounded `while`. A blocked
 * drain cannot stall the status read: the events call happens beside it. */
export const RUN_EVENT_PAGES_PER_TICK = 4;

/** Events asked for per page — the server's maximum (`list_run_events` caps
 * `limit` at 200). One page holds every event of a normal turn several times
 * over, so the drain almost always ends on its first request. */
export const RUN_EVENT_PAGE_SIZE = 200;

/**
 * A Run that will never change again.
 *
 * DELIBERATELY NOT `runIsSettled` (agent-harness/adapters/runWorkspace.ts).
 * That predicate answers a RENDERING question — "has this Run a settled
 * narrative to close a receipt with" — and counts `waiting_approval` and
 * `cancelling` as settled, because both draw a receipt. This one answers a
 * POLLING question: both of those states are still going somewhere, and
 * stopping the watch on either would freeze the thread one state short of the
 * answer. Two different questions, two named predicates.
 */
const TERMINAL: ReadonlySet<AgentRunStatusWire> = new Set<AgentRunStatusWire>([
  "completed",
  "failed",
  "cancelled",
  "expired",
  "context_exhausted",
  "budget_exhausted",
  "unavailable",
]);

export function runIsTerminal(status: AgentRunStatusWire): boolean {
  return TERMINAL.has(status);
}

/** What one poll learned. `progress` may arrive many times; exactly one
 * `settled` or `unreachable` ends a watch.
 *
 * `events` IS THE ACTIVITY, and it rides on the same outcome as the status for
 * one reason: it must not become a second poll loop. The Run's event log is the
 * only account of what the turn is DOING while it does it, so one tick reads
 * both and reports both, on one cadence, under one error budget.
 *
 * It is the WHOLE accumulated list every time, not a delta, so a consumer that
 * drops an outcome cannot end up with a hole in the middle of the band. An
 * empty array means the server has recorded nothing yet — never that this
 * module failed to ask. */
export type RunPollOutcome =
  | { kind: "progress"; run: AgentRunDetailWire; events: readonly AgentRunEventWire[] }
  | { kind: "settled"; run: AgentRunDetailWire; events: readonly AgentRunEventWire[] }
  /** The Run could not be read. `detail` is THE SERVER'S OWN WORDS or the empty
   * string — never this module's, never the engine's: it is disclosed to the
   * reader under "what the server said" (see detailOf below). */
  | { kind: "unreachable"; detail: string };

/** The exact Run one watch reads. Both ids come from the server's `accepted`
 * frame; this module resolves neither. */
export interface RunWatchTarget {
  agentId: string;
  runId: string;
}

export interface RunCompletionWatcher {
  /** Begin watching one Run under a caller-owned key (Ask uses the assistant
   * message id, so a second question cannot patch the first turn). Watching a
   * key that is already watched is a no-op — the SSE may re-deliver its
   * acceptance, and that must not open a second poll loop. */
  watch: (key: string, target: RunWatchTarget) => void;
  /** Stop every watch — a different session was selected, or the served
   * identity changed and no earlier turn's bytes may enter the new window. */
  reset: () => void;
  /** How many Runs are still being read. Ask holds the composer on this, so a
   * turn is "in flight" until its ANSWER lands, not until its stream closes. */
  pending: number;
}

function nextDelay(previous: number, status: AgentRunStatusWire | null): number {
  const ceiling = status === "waiting_approval" ? RUN_POLL_PAUSED_MAX_MS : RUN_POLL_MAX_MS;
  return Math.min(ceiling, Math.round(previous * RUN_POLL_GROWTH));
}

/**
 * THE SERVER'S WORDS OR NOTHING.
 *
 * This string becomes `TurnFailure.detail` (useAgentStream's `unreachable`
 * case -> runUnreadableFailure), which is documented as "the server's VERBATIM
 * words" and is disclosed to the reader under "what the server said". So the
 * only string admissible here is one the server actually sent: the `detail` it
 * published on the error body, and nothing else.
 *
 * It used to return `error.message`, which is neither. Since the transport
 * classes split their strings, `message` is the CONSOLE'S house sentence — an
 * ApiError(503) put "The server is unavailable." behind a disclosure claiming
 * the server said it, signing the console's prose with the server's name. And
 * for a throw that is not a transport error at all — a `TypeError` from a
 * dropped fetch — `message` is the ENGINE's "Failed to fetch", a raw runtime
 * string in a reader's drawer.
 *
 * Empty is the honest answer to "what did the server say" when it said
 * nothing, and the readout draws no drawer for it (TurnFailureReadout: an
 * empty detail is not disclosed). The reader still gets the console's own
 * stated line for an unreadable Run — in the console's own voice, where it
 * belongs.
 */
function detailOf(error: unknown): string {
  const transport = error instanceof ApiError || error instanceof SseError ? error : null;
  return transport?.detail ?? "";
}

/**
 * Watch accepted Runs to their terminal state.
 *
 * The loop is IMPERATIVE, started from the caller's event handler, not from an
 * effect keyed on a run id. An effect would restart on every identity change of
 * its deps and re-open a poll loop mid-turn; and a Run to watch is something
 * that HAPPENED (a frame arrived), not something the render state implies.
 */
export function useRunCompletion(
  read: (agentId: string, runId: string) => Promise<AgentRunDetailWire>,
  onOutcome: (key: string, outcome: RunPollOutcome) => void,
  readEvents?: (
    agentId: string,
    runId: string,
    cursor?: string,
    limit?: number,
  ) => Promise<AgentRunEventPageWire>,
): RunCompletionWatcher {
  // The sink is read at the moment a poll resolves, which is never the render
  // that supplied it. A ref keeps the loop pinned to the LATEST callback
  // without making the callback's identity a reason to restart the loop.
  const sink = useRef(onOutcome);
  useLayoutEffect(() => {
    sink.current = onOutcome;
  }, [onOutcome]);
  const reader = useRef(read);
  useLayoutEffect(() => {
    reader.current = read;
  }, [read]);
  const eventReader = useRef(readEvents);
  useLayoutEffect(() => {
    eventReader.current = readEvents;
  }, [readEvents]);

  /** Live watches by key. The value is that watch's cancel handle. */
  const live = useRef(new Map<string, { cancelled: boolean; timer?: number }>());
  const [pending, setPending] = useState(0);

  const stop = useCallback((key: string) => {
    const handle = live.current.get(key);
    if (handle === undefined) return;
    handle.cancelled = true;
    if (handle.timer !== undefined) clearTimeout(handle.timer);
    live.current.delete(key);
    setPending(live.current.size);
  }, []);

  const reset = useCallback(() => {
    live.current.forEach((handle) => {
      handle.cancelled = true;
      if (handle.timer !== undefined) clearTimeout(handle.timer);
    });
    live.current.clear();
    setPending(0);
  }, []);

  // Unmount cancels every watch: no poll may outlive the screen that wanted it.
  useEffect(() => reset, [reset]);

  const watch = useCallback(
    (key: string, target: RunWatchTarget) => {
      if (live.current.has(key)) return;
      const handle: { cancelled: boolean; timer?: number } = { cancelled: false };
      live.current.set(key, handle);
      setPending(live.current.size);

      let delay = RUN_POLL_FIRST_MS;
      let consecutiveErrors = 0;
      /** Every event seen for this Run, by canonical sequence. */
      let events: AgentRunEventWire[] = [];
      /** The last sequence the SERVER confirmed it had served, as its own opaque
       * cursor. It is a WATERMARK (ne-api `list_events`): the server mints one
       * whenever it served anything, so after the first tick every later tick
       * asks only for what is NEW. That is what keeps a two-second cadence on a
       * ninety-second Run cheap — each tick reads the handful of events since
       * the last one instead of re-serving a log that keeps growing. */
      let cursor: string | undefined;

      /**
       * Read the activity beside the status read.
       *
       * NEVER THROWS, and never touches the error budget. The events are what
       * the turn is DOING; the Run row is what the turn IS. If the activity
       * read fails the turn must still answer, so a failure here degrades to
       * the events already in hand — the band simply stops growing — rather
       * than failing a turn whose answer is fine.
       */
      const readActivity = async () => {
        const reader = eventReader.current;
        if (reader === undefined) return;
        try {
          for (let page = 0; page < RUN_EVENT_PAGES_PER_TICK; page += 1) {
            const next = await reader(target.agentId, target.runId, cursor, RUN_EVENT_PAGE_SIZE);
            if (handle.cancelled) return;
            events = reconcileRunEvents(events, next.items);
            // Advance the watermark on ANY page that served something, so the
            // next tick resumes after it. `reconcileRunEvents` still folds a
            // repeat away if the server ever re-serves one, but under the
            // watermark it no longer has to: a tick that learns nothing new
            // reads an empty page rather than the whole log again.
            if (next.next_cursor !== null) cursor = next.next_cursor;
            // A page the server did not fill is the end of the log AS IT
            // STANDS. More may be recorded a second from now; that is the next
            // tick's business, not another page of this one.
            if (next.items.length < RUN_EVENT_PAGE_SIZE) return;
          }
        } catch {
          // Deliberately swallowed: see above. No verdict, no retry of its own.
        }
      };

      const finish = (outcome: RunPollOutcome) => {
        if (handle.cancelled) return;
        stop(key);
        sink.current(key, outcome);
      };

      const tick = async () => {
        if (handle.cancelled) return;
        let run: AgentRunDetailWire;
        try {
          // Both reads are issued together, so the activity costs the tick no
          // extra latency, and the status is never delayed behind the log.
          [run] = await Promise.all([reader.current(target.agentId, target.runId), readActivity()]);
        } catch (error: unknown) {
          if (handle.cancelled) return;
          consecutiveErrors += 1;
          if (consecutiveErrors >= RUN_POLL_MAX_ERRORS) {
            finish({ kind: "unreachable", detail: detailOf(error) });
            return;
          }
          // A transient failure is not a verdict on the Run. Back off exactly
          // as a slow Run would and read again — the answer may be there.
          delay = nextDelay(delay, null);
          schedule();
          return;
        }
        if (handle.cancelled) return;
        consecutiveErrors = 0;
        if (runIsTerminal(run.status)) {
          finish({ kind: "settled", run, events });
          return;
        }
        sink.current(key, { kind: "progress", run, events });
        if (handle.cancelled) return;
        delay = nextDelay(delay, run.status);
        schedule();
      };

      const schedule = () => {
        if (handle.cancelled) return;
        handle.timer = window.setTimeout(() => void tick(), delay);
      };

      handle.timer = window.setTimeout(() => void tick(), RUN_POLL_FIRST_MS);
    },
    [stop],
  );

  return { watch, reset, pending };
}
