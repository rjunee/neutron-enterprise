// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE POLLER'S TWO OBLIGATIONS, which pull in opposite directions.
//
// It must be prompt enough that an answer appears when it is ready, and it must
// not hammer a governed API to be so. Every test here holds one of those against
// the other: a watch that never stops is the failure the backoff exists to
// prevent, and a watch that stops early is the blank screen the whole unit
// exists to fix.

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { SseError } from "../api/sse";
import type { AgentRunDetailWire, AgentRunStatusWire } from "../api/types";
import {
  RUN_POLL_MAX_ERRORS,
  RUN_POLL_MAX_MS,
  RUN_POLL_PAUSED_MAX_MS,
  runIsTerminal,
  useRunCompletion,
} from "./useRunCompletion";
import type { RunPollOutcome } from "./useRunCompletion";

function detail(status: AgentRunStatusWire): AgentRunDetailWire {
  return {
    run_id: "run-1",
    conversation_id: "conversation-1",
    agent_id: "agent-1",
    agent_version: 1,
    sequence: 1,
    version: 1,
    predecessor_run_id: null,
    root_run_id: "run-1",
    retry_ordinal: 0,
    display_label: "q",
    strategy: "agentic",
    status,
    output_verdict: null,
    model_policy_id: null,
    model_request_mode: "auto",
    context_snapshot_hash: null,
    requested_model: null,
    requested_model_lane: null,
    served_model: null,
    route_position: "unavailable",
    fallback_from: null,
    output_preview: null,
    output_state: "unavailable",
    config_hash: "a".repeat(64),
    capability_snapshot_hash: "b".repeat(64),
    created_at: "2026-08-27T12:00:00Z",
    updated_at: "2026-08-27T12:00:00Z",
    terminal_at: null,
    approval_id: null,
    failure_reason: null,
    agentic_plan: null,
    execution_profile_receipt: null,
    skill_activation_receipt: null,
    claims: [],
    claim_mapping_state: "mapping_pending",
    claims_truncated: false,
  };
}

function harness(statuses: AgentRunStatusWire[]) {
  const outcomes: RunPollOutcome[] = [];
  const read = vi.fn(async () => detail(statuses.shift() ?? "running"));
  const hook = renderHook(() =>
    useRunCompletion(read, (_key, outcome) => {
      outcomes.push(outcome);
    }),
  );
  return { hook, read, outcomes };
}

describe("runIsTerminal", () => {
  it("counts a paused or cancelling Run as still going somewhere", () => {
    // The distinction from `runIsSettled`, which answers a RENDERING question
    // and calls both of these settled. Treating them as terminal here would
    // freeze the thread one state short of the answer.
    expect(runIsTerminal("waiting_approval")).toBe(false);
    expect(runIsTerminal("cancelling")).toBe(false);
    expect(runIsTerminal("completed")).toBe(true);
    expect(runIsTerminal("failed")).toBe(true);
  });
});

describe("useRunCompletion", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reads until the Run is terminal, then stops", async () => {
    vi.useFakeTimers();
    const { hook, read, outcomes } = harness(["queued", "running", "completed"]);
    act(() => hook.result.current.watch("m2", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(outcomes.at(-1)?.kind).toBe("settled");
    const settledAt = read.mock.calls.length;
    // THE PAIRED HALF: nothing after the terminal. A watch that keeps reading a
    // finished Run is the hammering this backoff exists to prevent.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600_000);
    });
    expect(read.mock.calls.length).toBe(settledAt);
    expect(hook.result.current.pending).toBe(0);
  });

  it("backs off geometrically instead of polling at a fixed rate", async () => {
    vi.useFakeTimers();
    const { hook, read } = harness([]);
    act(() => hook.result.current.watch("m2", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    // A fixed 700ms cadence would be ~85 reads in a minute; the ceiling bounds
    // it to roughly one per RUN_POLL_MAX_MS after the ramp.
    expect(read.mock.calls.length).toBeLessThan(60_000 / RUN_POLL_MAX_MS + 8);
    expect(read.mock.calls.length).toBeGreaterThan(4);
  });

  it("slows to the human timescale while the Run waits on an approval", async () => {
    vi.useFakeTimers();
    const paused = harness(Array.from({ length: 200 }, () => "waiting_approval" as const));
    act(() => paused.hook.result.current.watch("m2", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });
    // Read before the second harness runs: the paused watch is still live and
    // would keep polling through the working harness's own advance.
    const pausedReads = paused.read.mock.calls.length;
    // AND IT IS STILL WATCHING. Slowing down is not stopping: the approval will
    // be decided, and the answer must arrive when it is.
    expect(paused.hook.result.current.pending).toBe(1);
    act(() => paused.hook.result.current.reset());

    const working = harness(Array.from({ length: 200 }, () => "running" as const));
    act(() => working.hook.result.current.watch("m2", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });

    expect(pausedReads).toBeLessThan(300_000 / RUN_POLL_PAUSED_MAX_MS + 8);
    expect(pausedReads).toBeLessThan(working.read.mock.calls.length);
  });

  it("retries a transient read failure and gives up only after a wall of them", async () => {
    vi.useFakeTimers();
    const outcomes: RunPollOutcome[] = [];
    let failures = 0;
    const read = vi.fn(async () => {
      failures += 1;
      if (failures <= 2) throw new Error("Failed to fetch");
      return detail("completed");
    });
    const hook = renderHook(() =>
      useRunCompletion(read, (_key, outcome) => {
        outcomes.push(outcome);
      }),
    );
    act(() => hook.result.current.watch("m2", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    // Two failures did not decide the turn: the answer still arrived.
    expect(outcomes.at(-1)?.kind).toBe("settled");
    expect(failures).toBeGreaterThan(2);
  });

  it("gives up and SAYS SO after RUN_POLL_MAX_ERRORS consecutive failures", async () => {
    vi.useFakeTimers();
    const outcomes: RunPollOutcome[] = [];
    const read = vi.fn(async () => {
      throw new Error("Failed to fetch");
    });
    const hook = renderHook(() =>
      useRunCompletion(read, (_key, outcome) => {
        outcomes.push(outcome);
      }),
    );
    act(() => hook.result.current.watch("m2", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });
    expect(outcomes).toHaveLength(1);
    expect(outcomes[0]).toMatchObject({ kind: "unreachable" });
    expect(read.mock.calls.length).toBe(RUN_POLL_MAX_ERRORS);
    expect(hook.result.current.pending).toBe(0);
  });

  // WHAT THE READER IS TOLD THE SERVER SAID. `unreachable.detail` becomes
  // `TurnFailure.detail` and is disclosed under "what the server said", so the
  // only admissible string is one the server actually sent. Three throws, three
  // answers — and two of them used to be forgeries.
  const gaveUpWith = async (thrown: unknown): Promise<RunPollOutcome> => {
    vi.useFakeTimers();
    const outcomes: RunPollOutcome[] = [];
    const read = vi.fn(async () => {
      throw thrown;
    });
    const hook = renderHook(() =>
      useRunCompletion(read, (_key, outcome) => {
        outcomes.push(outcome);
      }),
    );
    act(() => hook.result.current.watch("m9", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });
    expect(outcomes).toHaveLength(1);
    return outcomes[0] as RunPollOutcome;
  };

  it("discloses NOTHING as the server's words when the server sent none", async () => {
    // An ApiError's `message` is the CONSOLE'S house sentence now. Disclosing
    // it under "what the server said" signs the console's prose with the
    // server's name — the defect round 2 closed on six screens.
    const api = await gaveUpWith(new ApiError("request to /v1/agents/x failed with HTTP 503", 503));
    expect(api).toEqual({ kind: "unreachable", detail: "" });
    // A throw that is not a transport error at all: the engine's own string,
    // in a reader's drawer, attributed to the server.
    const engine = await gaveUpWith(new TypeError("Failed to fetch"));
    expect(engine).toEqual({ kind: "unreachable", detail: "" });
    // Not an Error at all.
    const thing = await gaveUpWith({ nope: true });
    expect(thing).toEqual({ kind: "unreachable", detail: "" });
  });

  it("discloses the server's own words, verbatim, when it sent them", async () => {
    const served = "run 8f3a is not readable by this principal";
    const api = await gaveUpWith(
      new ApiError("request to /v1/agents/x failed with HTTP 403", 403, served),
    );
    expect(api).toEqual({ kind: "unreachable", detail: served });
    const sse = await gaveUpWith(new SseError("stream failed with HTTP 403", 403, served));
    expect(sse).toEqual({ kind: "unreachable", detail: served });
  });

  it("opens exactly one loop per key, so a re-delivered acceptance cannot double it", async () => {
    vi.useFakeTimers();
    const { hook, read } = harness([]);
    act(() => {
      hook.result.current.watch("m2", { agentId: "agent-1", runId: "run-1" });
      hook.result.current.watch("m2", { agentId: "agent-1", runId: "run-1" });
    });
    expect(hook.result.current.pending).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    const once = read.mock.calls.length;
    expect(once).toBeGreaterThan(0);
    expect(once).toBeLessThan(12);
  });

  it("reset stops every watch, so no poll outlives the identity that started it", async () => {
    vi.useFakeTimers();
    const { hook, read, outcomes } = harness([]);
    act(() => hook.result.current.watch("m2", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    act(() => hook.result.current.reset());
    const stoppedAt = read.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });
    expect(read.mock.calls.length).toBe(stoppedAt);
    expect(hook.result.current.pending).toBe(0);
    expect(outcomes.every((outcome) => outcome.kind === "progress")).toBe(true);
  });

  it("unmount cancels the loop", async () => {
    vi.useFakeTimers();
    const { hook, read } = harness([]);
    act(() => hook.result.current.watch("m2", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    hook.unmount();
    const stoppedAt = read.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });
    expect(read.mock.calls.length).toBe(stoppedAt);
  });
});

// ---------------------------------------------------------------------------
// THE ACTIVITY RIDES ON THE SAME TICK.
//
// The Run's event log is the only account of what a canonical turn is DOING
// while it does it. Reading it must not become a second poll loop — that would
// double the load on a system being made faster — and it must never be able to
// fail a turn whose answer is perfectly fine.
// ---------------------------------------------------------------------------

import type { AgentRunEventPageWire, AgentRunEventWire } from "../api/types";
import { RUN_EVENT_PAGES_PER_TICK, RUN_EVENT_PAGE_SIZE } from "./useRunCompletion";

function runEvent(sequence: number): AgentRunEventWire {
  return {
    event_id: `evt-${sequence}`,
    run_id: "run-1",
    attempt_id: "attempt-1",
    sequence,
    event_kind: "model.served",
    event_status: "served",
    parent_event_id: null,
    step_id: `model:${sequence - 1}`,
    metadata: [],
    detail_hash: "c".repeat(64),
    created_at: "2026-08-27T12:00:00Z",
  };
}

/** A watch whose status reads and event reads are both scripted. */
function activityHarness(
  statuses: AgentRunStatusWire[],
  pages: AgentRunEventPageWire[],
  options: { failEvents?: boolean } = {},
) {
  const outcomes: RunPollOutcome[] = [];
  const read = vi.fn(async () => detail(statuses.shift() ?? "running"));
  const readEvents = vi.fn(
    async (
      _agentId: string,
      _runId: string,
      _cursor?: string,
      _limit?: number,
    ): Promise<AgentRunEventPageWire> => {
      if (options.failEvents === true) throw new Error("events unavailable");
      return pages.shift() ?? { items: [], next_cursor: null };
    },
  );
  const hook = renderHook(() =>
    useRunCompletion(
      read,
      (_key, outcome) => {
        outcomes.push(outcome);
      },
      readEvents,
    ),
  );
  return { hook, read, readEvents, outcomes };
}

/** The events carried by the last outcome that carried any. */
function lastEvents(outcomes: RunPollOutcome[]): readonly AgentRunEventWire[] {
  for (let i = outcomes.length - 1; i >= 0; i -= 1) {
    const outcome = outcomes[i];
    if (outcome !== undefined && outcome.kind !== "unreachable") return outcome.events;
  }
  return [];
}

describe("useRunCompletion — the Run's recorded activity", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("MUTATION `steps-appear-incrementally`: each tick carries the work so far", async () => {
    vi.useFakeTimers();
    const { hook, outcomes } = activityHarness(
      ["running", "running", "completed"],
      [
        { items: [runEvent(1)], next_cursor: null },
        { items: [runEvent(2)], next_cursor: null },
        { items: [runEvent(3)], next_cursor: null },
      ],
    );
    act(() => hook.result.current.watch("k", { agentId: "agent-1", runId: "run-1" }));

    // FIRST TICK: one step, on screen, while the Run is still running. This is
    // the whole fix — the reader sees work before the answer exists.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    expect(outcomes[0]?.kind).toBe("progress");
    expect(lastEvents(outcomes).map((e) => e.sequence)).toEqual([1]);

    // IT GROWS. A second tick reports two, not a replacement of one.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(lastEvents(outcomes).map((e) => e.sequence)).toEqual([1, 2]);

    // AND THE SETTLEMENT CARRIES THE WHOLE LIST, so the finished turn keeps the
    // account of how it got there.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(outcomes.at(-1)?.kind).toBe("settled");
    expect(lastEvents(outcomes).map((e) => e.sequence)).toEqual([1, 2, 3]);
  });

  it("MUTATION `nothing-invented`: a Run that records nothing reports nothing", async () => {
    // THE PAIRED HALF. The server has written no events; every outcome carries
    // an empty list, and no row is conjured to fill the silence.
    vi.useFakeTimers();
    const { hook, outcomes } = activityHarness(["running", "completed"], []);
    act(() => hook.result.current.watch("k", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    for (const outcome of outcomes) {
      if (outcome.kind !== "unreachable") expect(outcome.events).toEqual([]);
    }
  });

  it("re-delivers without duplicating: the same event twice is still one row", async () => {
    vi.useFakeTimers();
    const { hook, outcomes } = activityHarness(
      ["running", "completed"],
      [
        { items: [runEvent(1), runEvent(2)], next_cursor: null },
        { items: [runEvent(1), runEvent(2), runEvent(3)], next_cursor: null },
      ],
    );
    act(() => hook.result.current.watch("k", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    // A server that re-ships what it already sent — which the watermark makes
    // rare rather than impossible — must not print the same model call twice.
    // Reconciling by canonical sequence is what keeps the band whole.
    expect(lastEvents(outcomes).map((e) => e.sequence)).toEqual([1, 2, 3]);
  });

  it("drains a FULL page forward, bounded, and remembers the watermark", async () => {
    vi.useFakeTimers();
    // A page the server FILLED is the only signal that more is behind it. Under
    // the watermark contract (ne-api `list_events`) the server mints a cursor
    // for every page it serves anything on, so the cursor no longer means "more
    // to come" — fullness does, and a short page ends the drain.
    const full = (from: number) =>
      Array.from({ length: RUN_EVENT_PAGE_SIZE }, (_, i) => runEvent(from + i));
    const { hook, readEvents, outcomes } = activityHarness(
      ["running", "completed"],
      [
        { items: full(1), next_cursor: "cur-1" },
        { items: [runEvent(RUN_EVENT_PAGE_SIZE + 1)], next_cursor: "cur-2" },
        { items: [], next_cursor: null },
      ],
    );
    act(() => hook.result.current.watch("k", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    expect(lastEvents(outcomes).map((e) => e.sequence)).toEqual(
      Array.from({ length: RUN_EVENT_PAGE_SIZE + 1 }, (_, i) => i + 1),
    );
    // The drain is BOUNDED: a server that never stops handing back a full page
    // cannot spin this tick forever.
    expect(readEvents.mock.calls.length).toBeLessThanOrEqual(RUN_EVENT_PAGES_PER_TICK);
    // The drain RESUMED rather than re-reading: the first page's watermark is
    // what the second read carried.
    expect(readEvents.mock.calls[0]?.[2]).toBeUndefined();
    expect(readEvents.mock.calls[1]?.[2]).toBe("cur-1");
    // And it asked for the server's maximum page, not the default.
    expect(readEvents.mock.calls.at(-1)?.[3]).toBe(RUN_EVENT_PAGE_SIZE);
  });

  it("STOPS re-reading the whole log: after one tick every read is incremental", async () => {
    vi.useFakeTimers();
    const { hook, readEvents } = activityHarness(
      ["running", "running", "completed"],
      [
        { items: [runEvent(1), runEvent(2)], next_cursor: "cur-2" },
        { items: [runEvent(3)], next_cursor: "cur-3" },
        { items: [], next_cursor: null },
      ],
    );
    act(() => hook.result.current.watch("k", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    // THE WHOLE POINT OF THE WATERMARK. The first read has no cursor; every
    // read after it resumes from the last sequence the server confirmed, so a
    // two-second cadence on a ninety-second Run does not re-serve a growing log
    // forty-five times.
    const cursors = readEvents.mock.calls.map((call) => call[2]);
    expect(cursors[0]).toBeUndefined();
    expect(cursors.slice(1).every((cursor) => cursor !== undefined)).toBe(true);
  });

  it("MUTATION `activity-failure-never-fails-the-turn`: the answer still lands", async () => {
    // The events are what the turn is DOING; the Run row is what the turn IS.
    // A failing activity read must cost the band its rows and NOTHING else —
    // it must not consume the error budget and it must not turn a completed
    // turn into an unreachable one.
    vi.useFakeTimers();
    const { hook, outcomes, read } = activityHarness(["running", "running", "completed"], [], {
      failEvents: true,
    });
    act(() => hook.result.current.watch("k", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(outcomes.at(-1)?.kind).toBe("settled");
    expect(outcomes.some((o) => o.kind === "unreachable")).toBe(false);
    expect(lastEvents(outcomes)).toEqual([]);
    // The status read was never abandoned either.
    expect(read.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("costs one activity read per tick — not a second loop", async () => {
    vi.useFakeTimers();
    const { hook, read, readEvents } = activityHarness(
      ["running", "running", "running", "completed"],
      [],
    );
    act(() => hook.result.current.watch("k", { agentId: "agent-1", runId: "run-1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    // One activity read per status read. A separate interval would drift away
    // from this ratio immediately.
    expect(readEvents.mock.calls.length).toBe(read.mock.calls.length);
  });
});
