// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// PER-CALL IDENTITY, held as a pure rule.
//
// The lane used to collapse on (name, approval_id), so two calls to the same
// non-approval tool in one turn — two audit-chain events, two policy decisions —
// rendered as one row. A console whose whole argument is that every governed
// call is visible must not silently merge two of them.

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { SseError } from "../api/sse";
import { transportUserMessage } from "../api/userMessage";
import type { ApiClient } from "../api/client";
import type { AgentRunDetailWire, StreamEvent, ToolTransitEvent } from "../api/types";
import {
  CITATION_READ_DEADLINE_MS,
  STALL_DEADLINE_MS,
  TURN_DEADLINE_MS,
  applySettledRun,
  applyToolTransit,
  transitKey,
  useAgentStream,
} from "./useAgentStream";
import type { UiMessage } from "./useAgentStream";

const call = (over: Partial<ToolTransitEvent> = {}): ToolTransitEvent => ({
  type: "tool",
  name: "read.knowledge.search",
  tier: 1,
  decision: "allow",
  audit_seq: null,
  ...over,
});

describe("transitKey", () => {
  it("prefers the approval request, then the chain sequence, then arrival", () => {
    expect(transitKey(call({ approval_id: "req-1", audit_seq: 9 }), 3)).toBe("approval:req-1");
    expect(transitKey(call({ audit_seq: 9 }), 3)).toBe("seq:9");
    expect(transitKey(call(), 3)).toBe("arrival:3");
  });
});

describe("applyToolTransit", () => {
  it("keeps two distinct calls to the same tool as two rows", () => {
    let rows = applyToolTransit([], call({ audit_seq: 11 }), 1);
    rows = applyToolTransit(rows, call({ audit_seq: 12 }), 2);
    expect(rows.map((r) => r.key)).toEqual(["seq:11", "seq:12"]);
  });

  it("keeps two unidentified calls to the same tool as two rows", () => {
    let rows = applyToolTransit([], call(), 1);
    rows = applyToolTransit(rows, call(), 2);
    expect(rows).toHaveLength(2);
  });

  it("replaces an exact re-delivery IN PLACE, so the row order never shuffles", () => {
    let rows = applyToolTransit([], call({ approval_id: "req-1", decision: "pending" }), 1);
    rows = applyToolTransit(rows, call({ audit_seq: 12 }), 2);
    rows = applyToolTransit(rows, call({ approval_id: "req-1", decision: "pending" }), 3);
    expect(rows.map((r) => r.key)).toEqual(["approval:req-1", "seq:12"]);
  });

  it("replaces pending with the exact server-correlated terminal transition", () => {
    let rows = applyToolTransit(
      [],
      call({ approval_id: "req-1", decision: "pending", audit_seq: 11 }),
      1,
    );
    rows = applyToolTransit(
      rows,
      call({ approval_id: "req-2", decision: "pending", audit_seq: 12 }),
      2,
    );
    rows = applyToolTransit(
      rows,
      call({ approval_id: "req-1", decision: "allow", audit_seq: 13 }),
      3,
    );
    expect(rows.map((r) => r.key)).toEqual(["approval:req-1", "approval:req-2"]);
    expect(rows[0]?.event).toMatchObject({ decision: "allow", approval_id: "req-1" });
    expect(rows[1]?.event).toMatchObject({ decision: "pending", approval_id: "req-2" });
  });

  it("replaces exact pending checkpoints with expired and execution-failed truth", () => {
    let rows = applyToolTransit(
      [],
      call({ approval_id: "req-expired", decision: "pending", audit_seq: 20 }),
      1,
    );
    rows = applyToolTransit(
      rows,
      call({ approval_id: "req-failed", decision: "pending", audit_seq: 21 }),
      2,
    );
    rows = applyToolTransit(
      rows,
      call({ approval_id: "req-expired", decision: "expired", audit_seq: 22 }),
      3,
    );
    rows = applyToolTransit(
      rows,
      call({ approval_id: "req-failed", decision: "failed", audit_seq: 23 }),
      4,
    );
    expect(rows.map((r) => r.key)).toEqual(["approval:req-expired", "approval:req-failed"]);
    expect(rows.map((r) => r.event.decision)).toEqual(["expired", "failed"]);
  });
});

// ---------------------------------------------------------------------------
// THE STALL CLOCK FEEDS ON SERVER SIGNALS — frames AND keep-alive heartbeats.
//
// Load-bearing since NE-TURN-PROGRESS: per-hop tool frames now arrive EARLY in
// the turn, so the first frame arms the stall clock long before the model has
// finished thinking. If only data frames re-armed it, every legitimately long
// turn (25 s thinking; a parked approval) would be falsely aborted at the stall
// window — the fix for "no progress" would have BROKEN long turns. Keep-alives
// arrive every 15 s while the server is alive, and they are the signal that
// distinguishes "long turn, live stream" from "dead wire".
// ---------------------------------------------------------------------------

/** A hand-driven ApiClient seam: the test owns the stream's callbacks. */
function streamController({ resolveOnAbort = true }: { resolveOnAbort?: boolean } = {}) {
  let onEvent: ((event: StreamEvent) => void) | undefined;
  let onHeartbeat: (() => void) | undefined;
  let finish: (() => void) | undefined;
  let fail: ((err: unknown) => void) | undefined;
  const client = {
    sendMessage: vi.fn(
      (
        _sid: string,
        _prompt: string,
        onEv: (event: StreamEvent) => void,
        signal?: AbortSignal,
        onHb?: () => void,
      ) =>
        new Promise<void>((resolve, reject) => {
          onEvent = onEv;
          onHeartbeat = onHb;
          finish = resolve;
          fail = reject;
          // The real reader resolves (never throws) on an abort — sse.ts.
          if (resolveOnAbort) signal?.addEventListener("abort", () => resolve(), { once: true });
        }),
    ),
  } as unknown as ApiClient;
  return {
    client,
    emit: (event: StreamEvent) => onEvent?.(event),
    heartbeat: () => onHeartbeat?.(),
    finish: () => finish?.(),
    /** The wire breaks: the real reader's `read()` rejects and `sendMessage`
     * propagates it, which is the only way this hook's catch is reached. */
    fail: (err: unknown) => fail?.(err),
  };
}

const ACCEPTED: Extract<StreamEvent, { type: "accepted" }> = {
  type: "accepted",
  agent_id: "agt_01k3meridiananalyst000000000",
  conversation_id: "conversation-1",
  run_id: "run-1",
  attempt_id: "attempt-1",
  status: "submitted",
};

/** One canonical Run as the single-Run GET serves it. */
function runDetail(over: Partial<AgentRunDetailWire> = {}): AgentRunDetailWire {
  return {
    run_id: "run-1",
    conversation_id: "conversation-1",
    agent_id: "agt_01k3meridiananalyst000000000",
    agent_version: 1,
    sequence: 1,
    version: 1,
    predecessor_run_id: null,
    root_run_id: "run-1",
    retry_ordinal: 0,
    display_label: "which contracts renew?",
    strategy: "agentic",
    status: "completed",
    output_verdict: "unavailable",
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
    updated_at: "2026-08-27T12:00:04Z",
    terminal_at: "2026-08-27T12:00:04Z",
    approval_id: null,
    failure_reason: null,
    agentic_plan: null,
    execution_profile_receipt: null,
    skill_activation_receipt: null,
    claims: [],
    claim_mapping_state: "unavailable",
    claims_truncated: false,
    ...over,
  };
}

/** The turn's placeholder as it stands the instant acceptance lands. */
const PLACEHOLDER: UiMessage = {
  id: "m2",
  role: "assistant",
  text: "",
  citations: [],
  streaming: true,
  tools: [],
  runRef: { agentId: "agt_01k3meridiananalyst000000000", runId: "run-1" },
  runStatus: "submitted",
};

/** The turn's assistant message (index 1), asserted present for the type system. */
function assistant(result: {
  current: { messages: readonly import("./useAgentStream").UiMessage[] };
}) {
  const message = result.current.messages[1];
  if (message === undefined) throw new Error("no assistant message in the thread");
  return message;
}

describe("useAgentStream — session surface", () => {
  it("clears synchronously on client generation change and ignores stale completion", async () => {
    const oldStream = streamController({ resolveOnAbort: false });
    const newClient = {
      sendMessage: vi.fn(
        async (_sid: string, _prompt: string, onEvent: (event: StreamEvent) => void) => {
          onEvent({ type: "done", session_id: "new-session", output: "NEW ANSWER" });
        },
      ),
    } as unknown as ApiClient;
    const { result, rerender } = renderHook(
      ({ client, principalId }: { client: ApiClient; principalId: string }) =>
        useAgentStream(client, principalId),
      { initialProps: { client: oldStream.client, principalId: "principal-1" } },
    );

    act(() => {
      void result.current.send("old-session", "OLD PRIVATE QUESTION");
    });
    expect(result.current.messages.some((message) => message.text.includes("OLD PRIVATE"))).toBe(
      true,
    );

    rerender({ client: newClient, principalId: "principal-1" });
    expect(result.current.messages).toEqual([]);
    expect(result.current.busy).toBe(false);

    act(() => {
      oldStream.emit({ type: "done", session_id: "old-session", output: "STALE ANSWER" });
      oldStream.finish();
    });
    expect(result.current.messages).toEqual([]);

    await act(async () => {
      await result.current.send("new-session", "new question");
    });
    expect(result.current.messages.at(-1)?.text).toBe("NEW ANSWER");
  });

  it("clears on same-client principal change, fails closed unresolved, and drops late frames", async () => {
    const stream = streamController({ resolveOnAbort: false });
    const { result, rerender } = renderHook(
      ({ principalId }: { principalId: string | null }) =>
        useAgentStream(stream.client, principalId),
      { initialProps: { principalId: "principal-ada" as string | null } },
    );

    act(() => {
      void result.current.send("ada-session", "ADA PRIVATE QUESTION");
    });
    expect(result.current.messages.some((message) => message.text.includes("ADA PRIVATE"))).toBe(
      true,
    );
    const retained = result.current;

    rerender({ principalId: null });
    expect(result.current).toMatchObject({ messages: [], busy: false, refused: false });
    await act(async () => {
      expect(await result.current.send("none", "MUST NOT SEND")).toBeUndefined();
    });
    expect(stream.client.sendMessage).toHaveBeenCalledTimes(1);

    rerender({ principalId: "principal-bob" });
    act(() => {
      stream.emit({ type: "done", session_id: "ada-session", output: "ADA LATE ANSWER" });
      stream.finish();
      retained.loadThread([
        {
          id: "ada-retained",
          role: "assistant",
          text: "ADA RETAINED THREAD",
          citations: [],
          streaming: false,
          tools: [],
        },
      ]);
      retained.stop();
    });
    expect(result.current.messages).toEqual([]);
  });

  it("makes retained send, Stop, and thread controls inert after a client replacement", async () => {
    const oldCreate = vi.fn(async () => ({
      session_id: "ada-session",
      agent_name: "console-chat",
    }));
    const oldSend = vi.fn(async () => undefined);
    const oldClient = { createSession: oldCreate, sendMessage: oldSend } as unknown as ApiClient;
    let bobSignal: AbortSignal | undefined;
    let bobDone: (() => void) | undefined;
    const bobSend = vi.fn(
      (
        _sid: string,
        _prompt: string,
        _onEvent: (event: StreamEvent) => void,
        signal?: AbortSignal,
      ) =>
        new Promise<void>((resolve) => {
          bobSignal = signal;
          bobDone = resolve;
        }),
    );
    const bobClient = { sendMessage: bobSend } as unknown as ApiClient;
    const { result, rerender } = renderHook(
      ({ client, principalId }: { client: ApiClient; principalId: string }) =>
        useAgentStream(client, principalId),
      { initialProps: { client: oldClient, principalId: "principal-1" } },
    );
    const retained = result.current;

    rerender({ client: bobClient, principalId: "principal-1" });
    let bobTurn: Promise<string | undefined> | undefined;
    act(() => {
      bobTurn = result.current.send("bob-session", "BOB CURRENT QUESTION");
    });
    expect(bobSignal?.aborted).toBe(false);
    expect(result.current.messages.some((message) => message.text.includes("BOB CURRENT"))).toBe(
      true,
    );

    act(() => {
      void retained.send(undefined, "ADA RETAINED QUESTION");
      retained.stop();
      retained.loadThread([
        {
          id: "ada-old",
          role: "assistant",
          text: "ADA RETAINED THREAD",
          citations: [],
          streaming: false,
          tools: [],
        },
      ]);
    });
    expect(oldCreate).not.toHaveBeenCalled();
    expect(oldSend).not.toHaveBeenCalled();
    expect(bobSignal?.aborted).toBe(false);
    expect(result.current.messages.some((message) => message.text.includes("ADA"))).toBe(false);

    await act(async () => {
      bobDone?.();
      await bobTurn;
    });
  });

  it.each([
    [undefined, "console-chat"],
    ["served-grounding-mode", "served-grounding-mode"],
  ] as const)(
    "creates the %s mode with agent %s and preserves prompt bytes",
    async (mode, agent) => {
      const prompt = "Which accounts am I cleared to see, and how many open orders does each have?";
      const createSession = vi.fn(async (agentName: string) => ({
        session_id: "session-1",
        agent_name: agentName,
      }));
      const sendMessage = vi.fn(
        async (_sessionId: string, _prompt: string, onEvent: (event: StreamEvent) => void) => {
          onEvent({ type: "done", session_id: "session-1", output: "Answer" });
        },
      );
      const client = { createSession, sendMessage } as unknown as ApiClient;
      const { result } = renderHook(() => useAgentStream(client, "principal-1", mode));

      await act(async () => {
        await result.current.send(undefined, prompt);
      });

      expect(createSession).toHaveBeenCalledTimes(1);
      expect(createSession).toHaveBeenCalledWith(agent);
      expect(sendMessage.mock.calls[0]?.[1]).toBe(prompt);
    },
  );

  it("accepts the server's canonical Agent identity for the default Ask adapter", async () => {
    const createSession = vi.fn(async () => ({
      session_id: "conversation-1",
      agent_name: "agt_01k3meridiananalyst000000000",
    }));
    const sendMessage = vi.fn(
      async (_sessionId: string, _prompt: string, onEvent: (event: StreamEvent) => void) => {
        onEvent({
          type: "accepted",
          agent_id: "agt_01k3meridiananalyst000000000",
          conversation_id: "conversation-1",
          run_id: "run-1",
          attempt_id: "attempt-1",
          status: "submitted",
        });
      },
    );
    const client = { createSession, sendMessage } as unknown as ApiClient;
    const { result } = renderHook(() => useAgentStream(client, "principal-1"));

    await act(async () => {
      await result.current.send(undefined, "which contracts renew?");
    });

    expect(createSession).toHaveBeenCalledWith("console-chat");
    expect(sendMessage).toHaveBeenCalledWith(
      "conversation-1",
      "which contracts renew?",
      expect.any(Function),
      expect.any(AbortSignal),
      expect.any(Function),
      undefined,
      // THE TURN'S IDEMPOTENCY KEY. Without one the server mints a fresh run
      // per resend, which is how one question became six runs.
      expect.any(String),
    );
    const sentKey = (sendMessage.mock.calls[0] as unknown as unknown[])[6] as string;
    expect(sentKey).toBeTruthy();
    // And the turn carries the key it was sent with, so a retry can resend it.
    expect(result.current.messages[1]?.idempotencyKey).toBe(sentKey);
    expect(result.current.acceptedRun?.conversation_id).toBe("conversation-1");
    // THE PLACEHOLDER SURVIVES. It used to be filtered out of the thread here,
    // which is why every canonical question rendered the reader's own words and
    // an "Open Run" link, permanently.
    expect(result.current.messages.some((message) => message.role === "assistant")).toBe(true);
  });
});

const TOOL_EVENT: StreamEvent = {
  type: "tool",
  name: "read.knowledge.search",
  tier: 1,
  decision: "allow",
  audit_seq: 7,
} as StreamEvent;

describe("useAgentStream — the stall clock and the stream's end", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  async function tick(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("keep-alive heartbeats carry a long-thinking turn past the stall window", async () => {
    vi.useFakeTimers();
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    // The hop arrives early and arms the stall clock…
    act(() => stream.emit(TOOL_EVENT));
    // …then the model thinks for twice the stall window, with only the server's
    // keep-alives on the wire. The turn must survive.
    for (let i = 0; i < 4; i += 1) {
      await tick(STALL_DEADLINE_MS / 2 - 1_000);
      act(() => stream.heartbeat());
    }
    act(() => stream.emit({ type: "done", session_id: "s1", output: "the answer" }));
    act(() => stream.finish());
    await act(async () => {
      await sent;
    });

    const answer = assistant(result);
    expect(answer.failure).toBeUndefined();
    expect(answer.streaming).toBe(false);
    expect(answer.text).toBe("the answer");
  });

  it("a stream that goes fully silent after its first signal stalls out (control)", async () => {
    vi.useFakeTimers();
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => stream.emit(TOOL_EVENT));
    await tick(STALL_DEADLINE_MS + 1_000);
    await act(async () => {
      await sent;
    });

    const answer = assistant(result);
    expect(answer.streaming).toBe(false);
    expect(answer.failure?.reason).toBe("timeout");
  });

  it("a heartbeat arms the stall clock even before any data frame", async () => {
    // Before: a stream that opened, ticked keep-alives, then died was invisible
    // until the 120 s turn deadline. The first heartbeat now arms the stall
    // clock, so a dead wire is named within the stall window.
    vi.useFakeTimers();
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => stream.heartbeat());
    await tick(STALL_DEADLINE_MS + 1_000);
    await act(async () => {
      await sent;
    });

    expect(assistant(result).failure?.reason).toBe("timeout");
  });

  it("a stream that closes with no terminal frame is a stated stream failure", async () => {
    // The worker died / a proxy closed the wire: the read loop resolves cleanly
    // having never seen done or error. That must NOT leave a forever-streaming
    // message — the cursor blinking over a dead wire reads as a working system.
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => stream.emit(TOOL_EVENT));
    act(() => stream.finish());
    await act(async () => {
      await sent;
    });

    const answer = assistant(result);
    expect(answer.streaming).toBe(false);
    expect(answer.failure?.reason).toBe("stream");
  });

  it("a completed turn is not marked as a stream failure (control)", async () => {
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => stream.emit({ type: "done", session_id: "s1", output: "fine" }));
    act(() => stream.finish());
    await act(async () => {
      await sent;
    });

    expect(assistant(result).failure).toBeUndefined();
    expect(assistant(result).text).toBe("fine");
  });

  it("keeps the accepted turn in the thread and does not close it as a dead stream", async () => {
    // The `accepted` frame carries no output bytes, and the server then CLOSES
    // the stream. The old code read that pair as a terminal and deleted the
    // unanswered placeholder; the clean-close branch would otherwise now stamp
    // "The answer stream failed." over a turn that is proceeding normally.
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("conversation-1", "which contracts renew?");
    });
    act(() => stream.emit(ACCEPTED));
    act(() => stream.finish());
    await act(async () => {
      await sent;
    });

    expect(result.current.acceptedRun).toEqual(ACCEPTED);
    expect(result.current.messages).toHaveLength(2);
    expect(assistant(result)).toMatchObject({
      role: "assistant",
      streaming: true,
      runRef: { agentId: "agt_01k3meridiananalyst000000000", runId: "run-1" },
    });
    expect(assistant(result).failure).toBeUndefined();
    // The turn is still IN FLIGHT: the composer stays held until the answer
    // lands, not until the connection closes.
    expect(result.current.busy).toBe(true);
    expect(result.current.startedAt).not.toBeNull();
  });

  it("stores the done-frame provenance on its turn and preserves it through thread history", async () => {
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() =>
      stream.emit({
        type: "done",
        session_id: "s1",
        output: "the verified answer",
        verified: {
          template_id: "contracts-renewing",
          template_version: 2,
          config_hash: "config-abc",
          row_count: 4,
          truncated: false,
        },
        trace_id: "trace-123",
        gate_event_id: "gate-9",
      }),
    );
    act(() => stream.finish());
    await act(async () => {
      await sent;
    });

    expect(assistant(result).provenance).toEqual({
      kind: "verified",
      trace: { traceId: "trace-123", eventId: "gate-9" },
    });
    const held = [...result.current.messages];
    act(() => result.current.loadThread([]));
    expect(result.current.messages).toEqual([]);
    act(() => result.current.loadThread(held));
    expect(assistant(result).provenance).toEqual({
      kind: "verified",
      trace: { traceId: "trace-123", eventId: "gate-9" },
    });
  });

  it("updates one approval checkpoint in place without disturbing answer evidence", async () => {
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?", "private-qwen");
    });
    act(() =>
      stream.emit({
        type: "tool",
        name: "read.knowledge.search",
        tier: 2,
        decision: "pending",
        approval_id: "approval-1",
        audit_seq: 40,
      }),
    );
    act(() => stream.emit({ type: "token", delta: "Four contracts renew." }));
    act(() =>
      stream.emit({
        type: "citations",
        citations: [
          {
            chunk_id: "chunk-1",
            document_id: "contract-1",
            source_system: "sharepoint",
            source_ref: "ref/contract-1",
          },
        ],
      }),
    );
    act(() =>
      stream.emit({
        type: "tool",
        name: "read.knowledge.search",
        tier: 2,
        decision: "allow",
        approval_id: "approval-1",
        audit_seq: 41,
      }),
    );
    act(() =>
      stream.emit({
        type: "done",
        session_id: "s1",
        output: "Four contracts renew.",
        trace_id: "trace-plain",
      }),
    );
    act(() => stream.finish());
    await act(async () => sent);

    const message = assistant(result);
    expect(message.streamItems?.map((item) => item.kind)).toEqual([
      "tool",
      "answer",
      "citations",
      "terminal",
    ]);
    const tools = message.streamItems?.filter((item) => item.kind === "tool");
    expect(tools).toHaveLength(1);
    expect(tools?.[0]).toMatchObject({
      entry: { event: { decision: "allow", approval_id: "approval-1", audit_seq: 41 } },
    });
    expect(message.traceId).toBe("trace-plain");
    expect(message.requestedModel).toBe("private-qwen");
    expect(message).not.toHaveProperty("servedModel");
  });

  it("announces a server-correlated approval resume without inventing an approver", async () => {
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "send it");
    });
    act(() =>
      stream.emit({
        type: "tool",
        name: "write.external.demo.send",
        tier: 2,
        decision: "pending",
        approval_id: "req-1",
        audit_seq: 40,
      }),
    );
    act(() =>
      stream.emit({
        type: "tool",
        name: "write.external.demo.send",
        tier: 2,
        decision: "allow",
        approval_id: "req-1",
        audit_seq: 41,
      }),
    );

    expect(assistant(result).announcement).toBe(
      "Approval granted for write.external.demo.send; the approved action ran.",
    );
    expect(assistant(result).announcement).not.toMatch(/approved by|approver/i);
    act(() => stream.emit({ type: "done", session_id: "s1", output: "sent" }));
    act(() => stream.finish());
    await act(async () => sent);
  });

  it("announces exact approval terminal and execution-failed states without rejection inference", async () => {
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "send it");
    });
    act(() =>
      stream.emit({
        type: "tool",
        name: "write.external.demo.send",
        tier: 2,
        decision: "expired",
        approval_id: "req-expired",
        audit_seq: 41,
      }),
    );
    expect(assistant(result).announcement).toBe("Approval expired for write.external.demo.send.");
    expect(assistant(result).announcement).not.toMatch(/denied|rejected/i);

    act(() =>
      stream.emit({
        type: "tool",
        name: "write.external.demo.send",
        tier: 2,
        decision: "integrity-failed",
        approval_id: "req-integrity",
        audit_seq: 42,
      }),
    );
    expect(assistant(result).announcement).toBe(
      "Approval integrity check failed for write.external.demo.send.",
    );

    act(() =>
      stream.emit({
        type: "tool",
        name: "write.external.demo.send",
        tier: 2,
        decision: "failed",
        approval_id: "req-failed",
        audit_seq: 43,
      }),
    );
    expect(assistant(result).announcement).toBe(
      "Approved action write.external.demo.send failed during execution.",
    );
    expect(assistant(result).announcement).not.toMatch(/resumed|denied/i);
    act(() => stream.emit({ type: "error", detail: "Tool execution failed." }));
    act(() => stream.finish());
    await act(async () => sent);
  });

  // -------------------------------------------------------------------------
  // THE TERMINAL FRAME SETTLES THE TURN — AND ONLY THE TURN.
  //
  // Once `done` or `error` is on the screen, that is what the turn WAS, and no
  // later event may restate it. Four things can try, and each replaces the
  // server's stated cause with the console's own generic one — a policy refusal
  // read back as "the turn timed out", the server's words blanked, `retryable`
  // flipped: the stall abort, the 120 s turn-deadline abort, a reader Stop, and
  // a transport throw (the SSE reader keeps reading past a terminal frame until
  // the body ends). All four are decisions about the CONNECTION, and the tests
  // below hold them to that: the verdict is fixed, while the composer is still
  // released — a delivered answer must not leave `busy` true forever on a wire
  // nobody closes, which is the hang TURN_DEADLINE_MS exists to forbid.
  //
  // THE WINDOWS ARE PART OF THE ASSERTION: these run to and past
  // TURN_DEADLINE_MS. A fixture that stopped at the stall window could not
  // observe the timer that fires at 120 s (vacuous-guards.md V8).
  // -------------------------------------------------------------------------

  it("a server error outlives the stall abort — a fault is never restated as a timeout", async () => {
    vi.useFakeTimers();
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => stream.emit({ type: "error", detail: "PolicyDenied: egress blocked" }));
    // The verdict is on the screen the instant the frame lands.
    expect(assistant(result).failure?.reason).toBe("refused");
    // The wire stays OPEN past the stall window — a proxy holding the
    // connection, a worker that emitted its terminal before closing the stream.
    await tick(STALL_DEADLINE_MS + 1_000);
    // Asserted BEFORE awaiting the turn: a clock disarmed at the terminal never
    // settles this stream at all, and that must read as a stated failure here
    // rather than as the suite's own timeout.
    expect(result.current.busy).toBe(false);
    await act(async () => {
      await sent;
    });

    const answer = assistant(result);
    expect(answer.streaming).toBe(false);
    expect(answer.failure?.reason).toBe("refused");
    expect(answer.failure?.kind).toBe("refused");
    // The server's verbatim words survive — an abort would have blanked them.
    expect(answer.failure?.detail).toBe("PolicyDenied: egress blocked");
    expect(answer.failure?.retryable).toBe(false);
  });

  it("a keep-alive after the terminal holds the connection but never the verdict", async () => {
    // `signal()` runs BEFORE the switch, so every server signal — including one
    // that arrives after the terminal — re-arms the stall clock. That is
    // harmless for the verdict and correct for the connection: a wire still
    // ticking is not a dead wire. What must not happen is the verdict moving,
    // and what must not happen EITHER is the composer being held for as long as
    // a proxy cares to keep ticking — so this runs past TURN_DEADLINE_MS, where
    // the per-send deadline (which no keep-alive can restart) ends it.
    vi.useFakeTimers();
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => stream.emit({ type: "error", detail: "PolicyDenied: egress blocked" }));
    // Keep-alives every half-window, well past the turn deadline. The count is
    // DERIVED from the two constants rather than fixed, so raising the deadline
    // cannot silently turn this into a test that stops short of the thing it
    // exists to prove.
    const beats = Math.ceil(TURN_DEADLINE_MS / (STALL_DEADLINE_MS / 2)) + 1;
    for (let i = 0; i < beats; i += 1) {
      await tick(STALL_DEADLINE_MS / 2);
      act(() => stream.heartbeat());
      expect(assistant(result).failure?.reason).toBe("refused");
    }
    // The per-send deadline, which no keep-alive restarts, has ended it.
    expect(result.current.busy).toBe(false);
    await act(async () => {
      await sent;
    });

    const answer = assistant(result);
    expect(answer.failure?.reason).toBe("refused");
    expect(answer.failure?.detail).toBe("PolicyDenied: egress blocked");
  });

  it("a done frame likewise outlives the abort, and frees the composer (the symmetric control)", async () => {
    vi.useFakeTimers();
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => stream.emit({ type: "done", session_id: "s1", output: "the answer" }));
    await tick(TURN_DEADLINE_MS + 1_000);
    expect(result.current.busy).toBe(false);
    await act(async () => {
      await sent;
    });

    const answer = assistant(result);
    expect(answer.failure).toBeUndefined();
    expect(answer.text).toBe("the answer");
    expect(answer.streaming).toBe(false);
  });

  it("Stop after the answer landed does not restate the finished turn as stopped", async () => {
    // The reader's Stop stays live until the stream closes, and the client's SSE
    // loop keeps reading past a terminal frame (api/sse.ts reads until the body
    // ends). Pressing Stop in that gap must not stamp "you stopped this" over an
    // answer the server already delivered: what happens to the CONNECTION is not
    // what happened to the TURN.
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => stream.emit({ type: "done", session_id: "s1", output: "the answer" }));
    act(() => result.current.stop());
    await act(async () => {
      await sent;
    });

    const answer = assistant(result);
    expect(answer.failure).toBeUndefined();
    expect(answer.text).toBe("the answer");
  });

  it("a transport error after the terminal does not restate the finished turn", async () => {
    // Same rule from the other side: the wire breaks AFTER the server's verdict
    // rendered, so `sendMessage` rejects. Classifying that throw would replace a
    // stated refusal with a retryable transport failure — the same lie about the
    // cause, arriving by a different path.
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => stream.emit({ type: "error", detail: "PolicyDenied: egress blocked" }));
    act(() => stream.fail(new Error("network error")));
    await act(async () => {
      await sent;
    });

    const answer = assistant(result);
    expect(answer.failure?.reason).toBe("refused");
    expect(answer.failure?.detail).toBe("PolicyDenied: egress blocked");
  });

  // -------------------------------------------------------------------------
  // THE PATH THAT ACTUALLY BROKE. Every existing test that asserts the stream
  // line drives the CLEAN-CLOSE route — the reader returns with no terminal
  // frame and `streamEndedFailure()` is applied directly. The route where the
  // stream call THROWS was covered only for a settled turn and for an abort, so
  // when the console started classifying that throw by matching prose that no
  // longer contained the words it matched, a dead stream began rendering "No
  // answer was produced." with a Retry and CI stayed green. These drive the
  // throw.
  // -------------------------------------------------------------------------
  describe("a stream that dies before its first frame says the stream failed", () => {
    for (const status of [409, 429, 500, 502, 503, 504]) {
      it(`says so on a ${status} with no served reason`, async () => {
        const stream = streamController();
        const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

        let sent: Promise<string | undefined> | undefined;
        act(() => {
          sent = result.current.send("s1", "which contracts renew?");
        });
        // Exactly what api/sse.ts throws when the response never became a
        // stream: no `detail`, because a gateway sends no JSON body.
        act(() => stream.fail(new SseError(`stream failed with HTTP ${status}`, status)));
        await act(async () => {
          await sent;
        });

        const failure = assistant(result).failure;
        expect(failure?.kind).toBe("transport");
        expect(failure?.reason).toBe("stream");
        expect(failure?.retryable).toBe(true);
        // NOTHING IS ATTRIBUTED TO A SERVER THAT SAID NOTHING. `detail` renders
        // behind "what the server said"; the house sentence is the console's.
        expect(failure?.detail).toBe("");
      });
    }

    it("keeps the server's own words when it sent them, verbatim and alone", async () => {
      const stream = streamController();
      const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

      let sent: Promise<string | undefined> | undefined;
      act(() => {
        sent = result.current.send("s1", "which contracts renew?");
      });
      const served = "upstream closed the connection after 0 bytes";
      act(() => stream.fail(new SseError("stream failed with HTTP 502", 502, served)));
      await act(async () => {
        await sent;
      });

      const failure = assistant(result).failure;
      expect(failure?.reason).toBe("stream");
      expect(failure?.detail).toBe(served);
    });

    it("classifies the same however the house sentence is worded", async () => {
      // THE REGRESSION, CAUGHT BY CONSTRUCTION. The old code read the error's
      // rendered string; this drives an error whose rendered string is the
      // §2.4 sentence for its status and asserts the verdict is unmoved. A
      // reworded sentence cannot reach the classification, because the
      // classification never reads one.
      for (const status of [500, 504]) {
        const stream = streamController();
        const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));
        let sent: Promise<string | undefined> | undefined;
        act(() => {
          sent = result.current.send("s1", "which contracts renew?");
        });
        const err = new SseError(`stream failed with HTTP ${status}`, status);
        expect(err.message).toBe(transportUserMessage(status));
        act(() => stream.fail(err));
        await act(async () => {
          await sent;
        });
        expect(assistant(result).failure?.reason, String(status)).toBe("stream");
      }
    });
  });

  it("a frame arriving after the terminal cannot edit the delivered answer", async () => {
    // The fourth way to restate a settled turn is the simplest: another frame.
    // Both server paths end at their terminal, so a token that arrives after
    // one is a re-delivery or a proxy's own invention — folding it in would
    // rewrite an answer the reader has already read.
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    // The control, first and in the same test: BEFORE the terminal a token
    // frame does exactly what it always did.
    act(() => stream.emit({ type: "token", delta: "partial " }));
    expect(assistant(result).text).toBe("partial ");
    act(() => stream.emit({ type: "done", session_id: "s1", output: "the answer" }));
    act(() => stream.emit({ type: "token", delta: " and more" }));
    act(() => stream.emit(TOOL_EVENT));
    act(() => stream.finish());
    await act(async () => {
      await sent;
    });

    const answer = assistant(result);
    expect(answer.text).toBe("the answer");
    expect(answer.tools).toEqual([]);
  });

  it("a transport error BEFORE any terminal is still classified (the control)", async () => {
    // The control for the two tests above: the settled-turn rule must be the
    // TERMINAL's doing, not a blanket bypass of the late-failure paths. With no
    // terminal frame, the same broken wire still renders as a stated failure.
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => stream.emit(TOOL_EVENT));
    act(() => stream.fail(new Error("network error")));
    await act(async () => {
      await sent;
    });

    // STATED, and stated as what it is: the wire broke. The runtime's own
    // "network error" is NOT carried into `detail` — that slot is rendered
    // behind "what the server said" and the server said nothing here.
    expect(assistant(result).failure?.reason).toBe("stream");
    expect(assistant(result).failure?.detail).toBe("");
    expect(assistant(result).streaming).toBe(false);
  });

  it("Stop renders the stopped line's reason — not 'no answer was produced'", async () => {
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("s1", "which contracts renew?");
    });
    act(() => result.current.stop());
    await act(async () => {
      await sent;
    });

    expect(assistant(result).failure?.reason).toBe("stopped");
  });

  it("measures the last server signal for the progress line", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    act(() => {
      void result.current.send("s1", "which contracts renew?");
    });
    expect(result.current.lastSignalAt).toBeNull(); // nothing has arrived yet
    act(() => stream.heartbeat());
    expect(result.current.lastSignalAt).toBe(Date.now());
    await tick(5_000);
    act(() => stream.emit(TOOL_EVENT));
    expect(result.current.lastSignalAt).toBe(Date.now());
  });

  it("passes each explicit model alias through without changing the session", async () => {
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let first: Promise<string | undefined> | undefined;
    act(() => {
      first = result.current.send("same-session", "first", "frontier-sonnet");
    });
    act(() => stream.emit({ type: "done", session_id: "same-session", output: "first answer" }));
    act(() => stream.finish());
    await act(async () => first);

    let second: Promise<string | undefined> | undefined;
    act(() => {
      second = result.current.send("same-session", "second", "private-qwen");
    });
    act(() => stream.emit({ type: "done", session_id: "same-session", output: "second answer" }));
    act(() => stream.finish());
    await act(async () => second);

    const calls = vi.mocked(stream.client.sendMessage).mock.calls;
    expect(calls.map((callArgs) => [callArgs[0], callArgs[1], callArgs[5]])).toEqual([
      ["same-session", "first", "frontier-sonnet"],
      ["same-session", "second", "private-qwen"],
    ]);
    const answers = result.current.messages.filter((message) => message.role === "assistant");
    expect(answers.map((message) => message.requestedModel)).toEqual([
      "frontier-sonnet",
      "private-qwen",
    ]);
    const held = [...result.current.messages];
    act(() => result.current.loadThread([]));
    act(() => result.current.loadThread(held));
    expect(
      result.current.messages
        .filter((message) => message.role === "assistant")
        .map((message) => message.requestedModel),
    ).toEqual(["frontier-sonnet", "private-qwen"]);
  });

  it.each([
    [403, "frontier model selection is unavailable under AIR_GAP"],
    [409, "requested model is unavailable"],
    [503, "model evidence is unavailable"],
  ] as const)(
    "invalidates a selected model after one status %s request and keeps its detail non-retryable",
    async (status, detail) => {
      const stream = streamController();
      const invalidated = vi.fn();
      const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

      let sent: Promise<string | undefined> | undefined;
      act(() => {
        sent = result.current.send("same-session", "one attempt", "frontier-sonnet", invalidated);
      });
      act(() => stream.fail(new ApiError("selection failed", status, detail)));
      await act(async () => sent);

      expect(stream.client.sendMessage).toHaveBeenCalledTimes(1);
      expect(invalidated).toHaveBeenCalledTimes(1);
      expect(assistant(result).failure).toMatchObject({
        detail,
        retryable: false,
      });
    },
  );

  it("invalidates once when Stop wins the turn after a definitive model status", async () => {
    // A slow FastAPI error body can still be draining after its non-200 status
    // arrived. Stop aborts that drain; the typed status remains authoritative
    // for the catalog even though Stop remains the visible turn outcome.
    const stream = streamController({ resolveOnAbort: false });
    const invalidated = vi.fn();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    let sent: Promise<string | undefined> | undefined;
    act(() => {
      sent = result.current.send("same-session", "one attempt", "frontier-sonnet", invalidated);
    });
    act(() => result.current.stop());
    act(() => stream.fail(new ApiError("selection failed", 409, "requested model is unavailable")));
    await act(async () => sent);

    expect(stream.client.sendMessage).toHaveBeenCalledTimes(1);
    expect(invalidated).toHaveBeenCalledTimes(1);
    expect(assistant(result).failure).toMatchObject({ reason: "stopped", retryable: false });
  });
});

// ---------------------------------------------------------------------------
// WHAT A SETTLED RUN SAYS IN THE THREAD.
//
// Four different facts that used to render as one blank rectangle: an answer,
// a decline, a withheld answer, and a genuinely empty turn. Each test below is
// PAIRED with the one next to it, because every one of these is only correct
// relative to the others -- an implementation that renders everything as an
// answer passes half of them, and one that renders everything as empty passes
// the other half.
// ---------------------------------------------------------------------------

describe("applySettledRun", () => {
  it("renders a completed Run's answer inline", () => {
    const settled = applySettledRun(
      PLACEHOLDER,
      runDetail({ output_preview: "Four contracts renew in Q4.", output_state: "available" }),
    );
    expect(settled.text).toBe("Four contracts renew in Q4.");
    expect(settled.streaming).toBe(false);
    expect(settled.failure).toBeUndefined();
    expect(settled.outputState).toBe("available");
  });

  it("leaves a genuinely empty completed Run empty, so it can say so honestly", () => {
    // THE PAIRED HALF of the test above. No text, no decline, no withholding:
    // the only honest reading is that nothing was produced, and MessageList's
    // empty-answer rule states it. Inventing a sentence here would be the
    // mirror image of the bug this whole unit fixes.
    const settled = applySettledRun(PLACEHOLDER, runDetail());
    expect(settled.text).toBe("");
    expect(settled.streaming).toBe(false);
    expect(settled.failure).toBeUndefined();
  });

  it("renders a DECLINED Run as a decision carrying the agent's own words", () => {
    const settled = applySettledRun(
      PLACEHOLDER,
      runDetail({
        output_verdict: "refused",
        output_state: "available",
        output_preview: "I can't answer that from the records I can ground against.",
      }),
    );
    // The refusal tone — never the fault tone, never a success.
    expect(settled.failure).toEqual({
      kind: "refused",
      reason: "declined",
      detail: "I can't answer that from the records I can ground against.",
      retryable: false,
    });
    expect(settled.runVerdict).toBe("refused");
    // The decline's words are the DECISION'S reason, not answer typography.
    expect(settled.text).toBe("");
  });

  it("does not render a declined Run as a success, nor an attested Run as declined", () => {
    // THE PAIRED HALF. Two directions of the same confusion, both pinned.
    const declined = applySettledRun(
      PLACEHOLDER,
      runDetail({ output_verdict: "refused", output_preview: "no." }),
    );
    expect(declined.failure?.kind).toBe("refused");
    expect(declined.provenance).toBeUndefined();

    const attested = applySettledRun(
      PLACEHOLDER,
      runDetail({
        output_verdict: "grounded",
        output_state: "available",
        output_preview: "Four contracts renew in Q4.",
      }),
    );
    expect(attested.failure).toBeUndefined();
    expect(attested.provenance).toEqual({ kind: "grounded" });
  });

  it("renders a WITHHELD answer as withheld, never as an empty turn", () => {
    const settled = applySettledRun(PLACEHOLDER, runDetail({ output_state: "withheld" }));
    expect(settled.failure).toEqual({
      kind: "refused",
      reason: "withheld",
      detail: "",
      retryable: false,
    });
    expect(settled.outputState).toBe("withheld");
  });

  it("carries the Run's served model, which the chat stream never names", () => {
    const settled = applySettledRun(PLACEHOLDER, runDetail({ served_model: "qwen3.6-35b-a3b" }));
    expect(settled.servedModel).toBe("qwen3.6-35b-a3b");
    // Absent stays absent: no alias is promoted into a served-model claim.
    expect(applySettledRun(PLACEHOLDER, runDetail()).servedModel).toBeUndefined();
  });

  it("carries WHY that model served, and drops the wire's non-answer", () => {
    const fellBack = applySettledRun(
      PLACEHOLDER,
      runDetail({
        served_model: "private-default",
        route_position: "fallback",
        fallback_from: "frontier-sonnet",
      }),
    );
    expect(fellBack.routePosition).toBe("fallback");
    expect(fellBack.fallbackFrom).toBe("frontier-sonnet");

    const primary = applySettledRun(
      PLACEHOLDER,
      runDetail({ served_model: "private-default", route_position: "primary" }),
    );
    expect(primary.routePosition).toBe("primary");
    expect(primary.fallbackFrom).toBeUndefined();

    // `"unavailable"` is the wire saying the Run recorded nothing, so it
    // becomes an absent field rather than a third state the receipt has to
    // reason about — and it must never arrive as "primary", which is a claim.
    const silent = applySettledRun(PLACEHOLDER, runDetail({ served_model: "private-default" }));
    expect(silent.routePosition).toBeUndefined();
    expect(silent.fallbackFrom).toBeUndefined();
  });

  it("states a non-completed terminal Run with the server's own status word", () => {
    const settled = applySettledRun(PLACEHOLDER, runDetail({ status: "failed" }));
    expect(settled.failure).toEqual({
      kind: "stage",
      reason: "run-incomplete",
      detail: "failed",
      retryable: true,
    });
  });
});

// ---------------------------------------------------------------------------
// THE WHOLE PATH: a question, an acceptance, a poll, an answer on screen.
// ---------------------------------------------------------------------------

describe("useAgentStream — the accepted turn is answered from its Run", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  async function acceptAndPoll(details: AgentRunDetailWire[]) {
    vi.useFakeTimers();
    const stream = streamController();
    const getAgentRun = vi.fn(async () => details.shift() ?? runDetail());
    const client = { ...stream.client, getAgentRun } as unknown as ApiClient;
    const { result } = renderHook(() => useAgentStream(client, "principal-1"));
    act(() => {
      void result.current.send("conversation-1", "which contracts renew?");
    });
    act(() => stream.emit(ACCEPTED));
    act(() => stream.finish());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    return { result, getAgentRun };
  }

  it("polls the accepted Run and patches the answer into the thread", async () => {
    const { result, getAgentRun } = await acceptAndPoll([
      runDetail({
        output_preview: "Four contracts renew in Q4.",
        output_state: "available",
        output_verdict: "grounded",
        served_model: "qwen3.6-35b-a3b",
      }),
    ]);

    expect(getAgentRun).toHaveBeenCalledWith("agt_01k3meridiananalyst000000000", "run-1");
    expect(assistant(result).text).toBe("Four contracts renew in Q4.");
    expect(assistant(result).streaming).toBe(false);
    // The turn is over, so the composer is released and the clock stops.
    expect(result.current.busy).toBe(false);
    expect(result.current.startedAt).toBeNull();
  });

  it("stops polling once the Run is terminal", async () => {
    const { getAgentRun } = await acceptAndPoll([
      runDetail({ output_preview: "done", output_state: "available" }),
    ]);
    const afterSettle = getAgentRun.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(getAgentRun.mock.calls.length).toBe(afterSettle);
  });

  it("shows waiting_approval as a paused turn and keeps polling to the answer", async () => {
    const { result, getAgentRun } = await acceptAndPoll([
      runDetail({ status: "waiting_approval", approval_id: "req-9" }),
      runDetail({ status: "waiting_approval", approval_id: "req-9" }),
      runDetail({ output_preview: "Approved — the draft is ready.", output_state: "available" }),
    ]);

    // PAUSED: not arriving, not failed, not empty — and the approval it is
    // parked on is named, so the thread can route to it.
    expect(assistant(result).runStatus).toBe("waiting_approval");
    expect(assistant(result).streaming).toBe(false);
    expect(assistant(result).approvalId).toBe("req-9");
    expect(assistant(result).failure).toBeUndefined();
    expect(result.current.busy).toBe(true);

    // AND IT RESUMES. The paused ceiling is 20s, so one advance past it is
    // enough for the next read to land.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(getAgentRun.mock.calls.length).toBeGreaterThan(1);
    expect(assistant(result).text).toBe("Approved — the draft is ready.");
    expect(assistant(result).runStatus).toBe("completed");
    expect(assistant(result).failure).toBeUndefined();
    expect(result.current.busy).toBe(false);
  });

  it("says so when the Run cannot be read back, rather than spinning forever", async () => {
    vi.useFakeTimers();
    const stream = streamController();
    const getAgentRun = vi.fn(async () => {
      throw new Error("Failed to fetch");
    });
    const client = { ...stream.client, getAgentRun } as unknown as ApiClient;
    const { result } = renderHook(() => useAgentStream(client, "principal-1"));
    act(() => {
      void result.current.send("conversation-1", "which contracts renew?");
    });
    act(() => stream.emit(ACCEPTED));
    act(() => stream.finish());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });

    expect(assistant(result).streaming).toBe(false);
    expect(assistant(result).failure).toMatchObject({ reason: "run-unreadable" });
    expect(result.current.busy).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// THE SCREEN IS NOT BLANK ANY MORE.
//
// The owner's case, exactly: a genuinely multi-hop question whose Run makes many
// model calls over minutes. What made it intolerable was not the wait — it was
// that the wait looked identical to a break. These tests hold both halves: the
// work appears while it is happening, and a Run that dies says so and says how
// far it got.
// ---------------------------------------------------------------------------

describe("useAgentStream — the working turn shows its work", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function activityEvent(sequence: number, over: Record<string, unknown> = {}) {
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
      ...over,
    };
  }

  /** Accept a turn, then drive the poll with scripted Run rows and event pages. */
  async function acceptWithActivity(
    details: AgentRunDetailWire[],
    pages: Array<{ items: unknown[]; next_cursor: string | null }>,
  ) {
    vi.useFakeTimers();
    const stream = streamController();
    const getAgentRun = vi.fn(async () => details.shift() ?? runDetail({ status: "running" }));
    const listAgentRunEvents = vi.fn(async () => pages.shift() ?? { items: [], next_cursor: null });
    const client = {
      ...stream.client,
      getAgentRun,
      listAgentRunEvents,
    } as unknown as ApiClient;
    const { result } = renderHook(() => useAgentStream(client, "principal-1"));
    act(() => {
      void result.current.send("conversation-1", "which contracts renew and why?");
    });
    act(() => stream.emit(ACCEPTED));
    act(() => stream.finish());
    return { result, getAgentRun, listAgentRunEvents };
  }

  it("MUTATION `steps-appear-while-running`: the band fills before the answer exists", async () => {
    const { result } = await acceptWithActivity(
      [
        runDetail({ status: "running" }),
        runDetail({ status: "running" }),
        runDetail({ output_preview: "Four contracts renew in Q4.", output_state: "available" }),
      ],
      [
        { items: [activityEvent(1)], next_cursor: null },
        { items: [activityEvent(1), activityEvent(2)], next_cursor: null },
        { items: [activityEvent(1), activityEvent(2), activityEvent(3)], next_cursor: null },
      ],
    );

    // ONE TICK IN: the Run has no answer yet, and the screen is ALREADY not
    // blank. This assertion is the entire point of the change.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    expect(assistant(result).text).toBe("");
    expect(assistant(result).runSteps).toHaveLength(1);
    expect(assistant(result).runSteps?.[0]?.intent).toBe("model:0");

    // AND IT GROWS while the turn works.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(assistant(result).runSteps).toHaveLength(2);

    // MUTATION `completed-still-answers`: the paired half. Showing the work must
    // not cost the turn its answer — the settled Run still lands its text, and
    // the steps it took stay beside it.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(assistant(result).text).toBe("Four contracts renew in Q4.");
    expect(assistant(result).streaming).toBe(false);
    expect(assistant(result).runSteps).toHaveLength(3);
    expect(result.current.busy).toBe(false);
  });

  it("MUTATION `nothing-invented`: a Run that records no steps shows no steps", async () => {
    // THE PAIRED HALF of the first assertion above. Same path, same poll, an
    // empty event log — and the turn renders no rows at all rather than a
    // placeholder, a spinner row, or a step 0 of N.
    const { result } = await acceptWithActivity(
      [
        runDetail({ status: "running" }),
        runDetail({ output_preview: "done", output_state: "available" }),
      ],
      [],
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    expect(assistant(result).runSteps).toEqual([]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(assistant(result).text).toBe("done");
    expect(assistant(result).runSteps).toEqual([]);
  });

  it("MUTATION `cancelled-says-so`: a killed Run states it AND keeps the steps it reached", async () => {
    // The failure the owner actually hit: the run was cancelled and the screen
    // could not tell him apart from a slow one. Now it says so in the server's
    // own status word, and the work it got through survives the settlement so
    // "how far did it get" has an answer on screen.
    const { result } = await acceptWithActivity(
      [runDetail({ status: "running" }), runDetail({ status: "cancelled" })],
      [
        { items: [activityEvent(1), activityEvent(2)], next_cursor: null },
        { items: [activityEvent(1), activityEvent(2)], next_cursor: null },
      ],
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(assistant(result).runStatus).toBe("cancelled");
    expect(assistant(result).failure).toEqual({
      kind: "stage",
      reason: "run-incomplete",
      detail: "cancelled",
      retryable: true,
    });
    // NEVER A SPINNER THAT RUNS FOREVER.
    expect(assistant(result).streaming).toBe(false);
    expect(result.current.busy).toBe(false);
    // The steps it reached are still on the screen.
    expect(assistant(result).runSteps).toHaveLength(2);
    expect(assistant(result).runSteps?.at(-1)?.intent).toBe("model:1");
  });

  it("shows the tool calls the Run checkpointed, not only its model calls", async () => {
    // `agentic_plan` is the only place a tool NAME appears, and it is written
    // per call while the Run is RUNNING — so a retrieval-heavy turn shows its
    // retrievals as it does them.
    const { result } = await acceptWithActivity(
      [
        runDetail({
          status: "running",
          strategy: "agentic",
          agentic_plan: [
            {
              ordinal: 0,
              tool: "search.contracts",
              step_intent: "renewal terms",
              tool_call_id: null,
            },
          ],
        }),
      ],
      [{ items: [activityEvent(1)], next_cursor: null }],
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700);
    });
    const steps = assistant(result).runSteps ?? [];
    expect(steps.map((s) => s.name)).toEqual(["search.contracts", "Model call"]);
    expect(steps[0]?.intent).toBe("renewal terms");
  });
});

// ---------------------------------------------------------------------------
// THE CONNECTION IS NOT THE TURN.
//
// The owner's run finished at 02:32:08. He cancelled at 02:36:20 — four minutes
// after the server had already answered — because the console had walked away
// from a run that was still live, and then minted five more by resending
// without a key. Both halves of that are held here.
// ---------------------------------------------------------------------------

describe("useAgentStream — a run outlives the connection that started it", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("MUTATION `run-outlives-connection`: the answer still lands long past the window", async () => {
    vi.useFakeTimers();
    const stream = streamController();
    // The Run stays working for longer than the console's own deadline, then
    // answers — exactly the shape of the turn that was abandoned.
    let reads = 0;
    const getAgentRun = vi.fn(async () => {
      reads += 1;
      return reads < 40
        ? runDetail({ status: "running" })
        : runDetail({ output_preview: "Four contracts renew in Q4.", output_state: "available" });
    });
    const client = { ...stream.client, getAgentRun } as unknown as ApiClient;
    const { result } = renderHook(() => useAgentStream(client, "principal-1"));
    act(() => {
      void result.current.send("conversation-1", "which contracts renew?");
    });
    act(() => stream.emit(ACCEPTED));
    // THE CONNECTION ENDS HERE, seconds in. The canonical stream always does.
    act(() => stream.finish());

    // Well past the console's own deadline — the point at which this turn used
    // to be declared dead while the server was still working on it.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TURN_DEADLINE_MS + 60_000);
    });

    // THE TURN IS NOT A CASUALTY OF ITS OWN SOCKET.
    expect(assistant(result).text).toBe("Four contracts renew in Q4.");
    expect(assistant(result).failure).toBeUndefined();
    expect(result.current.busy).toBe(false);
  });

  it("sends one Idempotency-Key per question, and the SAME one on a resend", async () => {
    vi.useFakeTimers();
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));

    act(() => {
      void result.current.send("conversation-1", "which contracts renew?");
    });
    act(() => stream.emit(ACCEPTED));
    act(() => stream.finish());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    const first = ((stream.client.sendMessage as unknown as { mock: { calls: unknown[][] } }).mock
      .calls[0] ?? [])[6] as string;
    expect(first).toBeTruthy();

    // A RESEND OF THE SAME TURN carries the key that turn was born with, so the
    // server resolves it to the run already running. Minting a new key here is
    // precisely what turned one question into six runs.
    act(() => {
      void result.current.send(
        "conversation-1",
        "which contracts renew?",
        undefined,
        undefined,
        undefined,
        first,
      );
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    const second = ((stream.client.sendMessage as unknown as { mock: { calls: unknown[][] } }).mock
      .calls[1] ?? [])[6] as string;
    expect(second).toBe(first);
  });

  it("MUTATION `distinct-questions-distinct-keys`: the paired half", async () => {
    // The key must not be a constant either. Two genuinely different questions
    // are two turns and must mint two keys, or the second would be swallowed as
    // a replay of the first and the reader would never get an answer to it.
    vi.useFakeTimers();
    const stream = streamController();
    const { result } = renderHook(() => useAgentStream(stream.client, "principal-1"));
    for (const prompt of ["which contracts renew?", "what changed in clause 7?"]) {
      act(() => {
        void result.current.send("conversation-1", prompt);
      });
      act(() => stream.emit(ACCEPTED));
      act(() => stream.finish());
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10);
      });
    }
    const calls = (stream.client.sendMessage as unknown as { mock: { calls: unknown[][] } }).mock
      .calls;
    expect(calls[0]?.[6]).not.toBe(calls[1]?.[6]);
  });
});

describe("applySettledRun — a failed run reports its OWN cause", () => {
  it("MUTATION `stated-cause-survives`: the server's reason becomes the stated line", () => {
    const settled = applySettledRun(
      PLACEHOLDER,
      runDetail({
        status: "failed",
        failure_reason: "No current serving evidence for the requested lane.",
      }),
    );
    expect(settled.failure?.reason).toBe("run-incomplete");
    // The reason the server MARKED publishable, verbatim — not the console's
    // generic timeout line, which is what a reader used to be shown for this.
    expect(settled.failure?.statedReason).toBe(
      "No current serving evidence for the requested lane.",
    );
    // The status word survives too: WHAT happened and WHY are both kept.
    expect(settled.failure?.detail).toBe("failed");
  });

  it("MUTATION `no-reason-invented`: a run that published none gets no sentence", () => {
    // THE PAIRED HALF. Where the server marked no reason there is none, and the
    // console's own stated line stands rather than a plausible fabrication.
    const settled = applySettledRun(PLACEHOLDER, runDetail({ status: "failed" }));
    expect(settled.failure?.reason).toBe("run-incomplete");
    expect(settled.failure?.statedReason).toBeUndefined();
    expect(settled.failure?.detail).toBe("failed");
  });

  it("refuses a blank reason as if it were none", () => {
    const blank = applySettledRun(
      PLACEHOLDER,
      runDetail({ status: "failed", failure_reason: "   " }),
    );
    expect(blank.failure?.statedReason).toBeUndefined();
  });
});

/* =============================================== the answer's sources ===== */
//
// Canonical Ask's stream carries no `citations` frame, so a governed answer
// that rested on four retrieved passages rendered as though it cited nothing —
// which on this product reads as an admission, not as a missing feature. The
// sources are read from the Run once it settles, under exactly the same two
// rules chat/runCitations holds: only an ASSOCIATED record, and only a chip
// whose target resolves.

describe("the accepted turn's citations are read from its Run", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  const ENVELOPE = {
    schema_version: 1,
    agent_id: "agt_01k3meridiananalyst000000000",
    run_id: "run-1",
    run_status: "completed",
    mapping_state: "complete",
    mapping_message: "Claim mapping complete",
    output: { verdict: "grounded", state: "available", message: "", display: "answer" },
    claims: [
      {
        claim_id: "claim-1",
        claim_verdict: "grounded",
        state: "available",
        message: "Claim available",
        display: "Four contracts renew in Q4.",
        evidence_id: "ev-1",
        association_hash: "a".repeat(64),
        verifier_event_id: null,
        verifier_receipt_hash: null,
      },
    ],
    evidence: [
      {
        evidence_id: "ev-1",
        state: "available",
        message: "Evidence available",
        assurance: "direct",
        locator: { chunk_id: "chunk-1" },
      },
      // Produced by the same Run, available, retrievable — and bound to no
      // claim. It must not become a chip.
      {
        evidence_id: "ev-2",
        state: "available",
        message: "Evidence available",
        assurance: "direct",
        locator: { chunk_id: "chunk-2" },
      },
    ],
    governance: {
      status: "complete",
      delegated_user_label: null,
      agent_service_identity: null,
      connector_credential_owner_label: null,
      effective_scopes: [],
      policy_environment: null,
      tool_tier: null,
      tool_decision: null,
      unavailable_reasons: [],
    },
  };

  const CITATION = {
    chunk_id: "chunk-1",
    document_id: "doc-1",
    source_system: "sharepoint",
    source_ref: "reg-e/§1005.11",
  };

  async function settleWithEvidence(overrides: Record<string, unknown> = {}) {
    vi.useFakeTimers();
    const stream = streamController();
    const getAgentRun = vi.fn(async () =>
      runDetail({ status: "completed", output_preview: "Four contracts renew in Q4." }),
    );
    const getAgentRunEvidence = vi.fn(async () => ENVELOPE);
    const retrieve = vi.fn(async (chunkIds: string[]) => ({
      chunks: [],
      citations: chunkIds.includes("chunk-1") ? [CITATION] : [],
      graph_paths: [],
      provisional_source_count: 0,
    }));
    const client = {
      ...stream.client,
      getAgentRun,
      getAgentRunEvidence,
      retrieve,
      ...overrides,
    } as unknown as ApiClient;
    const { result } = renderHook(() => useAgentStream(client, "principal-1"));
    act(() => {
      void result.current.send("conversation-1", "which contracts renew?");
    });
    act(() => stream.emit(ACCEPTED));
    act(() => stream.finish());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    return { result, getAgentRunEvidence, retrieve };
  }

  it("patches in ONLY the associated record's citation, resolved by the server", async () => {
    const { result, getAgentRunEvidence, retrieve } = await settleWithEvidence();
    expect(getAgentRunEvidence).toHaveBeenCalledWith("agt_01k3meridiananalyst000000000", "run-1");
    // It asked about the cited chunk and NOT about the unassociated one.
    expect(retrieve).toHaveBeenCalledWith(["chunk-1"]);
    expect(assistant(result).citations).toEqual([CITATION]);
    // The answer is untouched by the source read.
    expect(assistant(result).text).toBe("Four contracts renew in Q4.");
    // And the read's window is closed: nothing is pending once it landed.
    expect(assistant(result).citationsPending).toBe(false);
  });

  it("MUTATION `pending-while-reading`: the window between answer and citations is stated", async () => {
    // The owner measured tens of seconds of client-side dead air here — answer
    // on screen, source block silent. The turn now carries `citationsPending`
    // for exactly the read's lifetime: set in the patch that settles the turn,
    // cleared in the patch that lands (or buries) the read.
    let releaseEnvelope!: () => void;
    const gate = new Promise<void>((res) => {
      releaseEnvelope = res;
    });
    const { result } = await settleWithEvidence({
      getAgentRunEvidence: vi.fn(async () => {
        await gate;
        return ENVELOPE;
      }),
    });
    // Settled, answer on screen, read still in flight: pending — no citations
    // claimed, and no absence claimed either.
    expect(assistant(result).streaming).toBe(false);
    expect(assistant(result).text).toBe("Four contracts renew in Q4.");
    expect(assistant(result).citationsPending).toBe(true);
    expect(assistant(result).citations).toEqual([]);
    expect(assistant(result).citationsNote).toBeUndefined();
    // The read lands: pending clears in the same patch that writes the chips.
    releaseEnvelope();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(assistant(result).citationsPending).toBe(false);
    expect(assistant(result).citations).toEqual([CITATION]);
  });

  it("MUTATION `pending-has-a-deadline`: a read that never returns cannot spin forever", async () => {
    // Fail-closed, with a terminal condition: past the deadline the pending
    // line clears and NOTHING else changes — no chips, no invented absence
    // sentence. The block goes back to claiming nothing.
    const { result } = await settleWithEvidence({
      getAgentRunEvidence: vi.fn(() => new Promise(() => {})),
    });
    expect(assistant(result).citationsPending).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CITATION_READ_DEADLINE_MS);
    });
    expect(assistant(result).citationsPending).toBe(false);
    expect(assistant(result).citations).toEqual([]);
    expect(assistant(result).citationsNote).toBeUndefined();
  });

  it("MUTATION `sources-fail-the-turn`: an unreadable evidence endpoint leaves the answer", async () => {
    // A turn whose answer is fine must never be failed over its source list.
    const { result } = await settleWithEvidence({
      getAgentRunEvidence: vi.fn(async () => {
        throw new Error("evidence unavailable");
      }),
    });
    expect(assistant(result).text).toBe("Four contracts renew in Q4.");
    expect(assistant(result).failure).toBeUndefined();
    expect(assistant(result).citations).toEqual([]);
    // And it invents no absence sentence for a question it could not ask.
    expect(assistant(result).citationsNote).toBeUndefined();
    // The dead read buried its pending line with it: fail-closed, not forever.
    expect(assistant(result).citationsPending).toBe(false);
  });

  it("MUTATION `chip-without-a-target`: an unresolvable chunk yields no chip", async () => {
    const { result } = await settleWithEvidence({
      retrieve: vi.fn(async () => ({
        chunks: [],
        citations: [],
        graph_paths: [],
        provisional_source_count: 0,
      })),
    });
    expect(assistant(result).citations).toEqual([]);
    expect(assistant(result).citationsNote).toBe(
      "1 of 1 cited source is no longer readable with your current access.",
    );
  });
});

// ---------------------------------------------------------------------------
// THE AGENT-RUN DOOR — a non-default agent's turn runs on ITS OWN agent.
//
// The chat wire admits exactly one agent name and always answers with the
// canonical default (ne-api routers/chat.py create_session), so before this
// door existed a turn typed into ANY other agentic workspace silently ran on
// the Analyst. These tests pin the routing: with a runTarget, the turn goes
// through the agent's own Conversation + POST /v1/agents/{id}/runs envelope,
// touches the chat wire not at all, and then hands over to the SAME Run poll
// the canonical chat path uses.
// ---------------------------------------------------------------------------

describe("useAgentStream — the agent-run door", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  const CS_AGENT = "agt_01k3customersupport000000000";
  const TARGET = {
    agentId: CS_AGENT,
    agentVersion: 2,
    configHash: "1".repeat(64),
    capabilityHash: "2".repeat(64),
  };

  function runClient(over: Record<string, unknown> = {}) {
    const createSession = vi.fn();
    const sendMessage = vi.fn();
    const createAgentConversation = vi.fn(async () => ({
      conversation_id: "conv-cs-1",
      agent_id: CS_AGENT,
      title: "Conversation",
      created_at: "2026-08-28T09:00:00Z",
      updated_at: "2026-08-28T09:00:00Z",
      last_sequence: 0,
    }));
    const getConversation = vi.fn(async () => ({
      conversation_id: "conv-cs-9",
      agent_id: CS_AGENT,
      title: "Conversation",
      created_at: "2026-08-28T09:00:00Z",
      updated_at: "2026-08-28T09:05:00Z",
      last_sequence: 4,
    }));
    const startAgentRun = vi.fn(async () => ({
      action: "start",
      run_id: "run-cs-1",
      attempt_id: "attempt-cs-1",
      version: 1,
      status: "submitted",
      enqueue_id: "enq-cs-1",
      replayed: false,
    }));
    const getAgentRun = vi.fn(async () =>
      runDetail({
        run_id: "run-cs-1",
        conversation_id: "conv-cs-1",
        agent_id: CS_AGENT,
        agent_version: 2,
        output_preview: "The outreach draft is parked for approval.",
        output_state: "available",
      }),
    );
    const client = {
      createSession,
      sendMessage,
      createAgentConversation,
      getConversation,
      startAgentRun,
      getAgentRun,
      ...over,
    } as unknown as ApiClient;
    return {
      client,
      createSession,
      sendMessage,
      createAgentConversation,
      getConversation,
      startAgentRun,
      getAgentRun,
    };
  }

  it("submits through the agent's own Run envelope and NEVER the chat wire", async () => {
    vi.useFakeTimers();
    const seams = runClient();
    const { result } = renderHook(() =>
      useAgentStream(seams.client, "principal-1", "console-chat", TARGET),
    );
    await act(async () => {
      void result.current.send(undefined, "Send the disaster-hardship outreach.");
      await vi.advanceTimersByTimeAsync(1_000);
    });

    // The chat wire was not touched: no session, no SSE.
    expect(seams.createSession).not.toHaveBeenCalled();
    expect(seams.sendMessage).not.toHaveBeenCalled();

    // The envelope is the served identity, verbatim.
    expect(seams.createAgentConversation).toHaveBeenCalledWith(CS_AGENT);
    expect(seams.startAgentRun).toHaveBeenCalledTimes(1);
    const [agentId, body, idempotencyKey] = seams.startAgentRun.mock.calls[0]! as unknown as [
      string,
      Record<string, unknown>,
      string,
    ];
    expect(agentId).toBe(CS_AGENT);
    expect(body).toEqual({
      conversation_id: "conv-cs-1",
      expected_version: 0,
      agent_version: 2,
      strategy: "agentic",
      config_hash: "1".repeat(64),
      capability_snapshot_hash: "2".repeat(64),
      input: "Send the disaster-hardship outreach.",
      requested_model: null,
      requested_model_lane: null,
    });
    expect(typeof idempotencyKey).toBe("string");
    expect(idempotencyKey.length).toBeGreaterThan(0);

    // The acceptance hands over to the SAME Run poll the canonical path uses,
    // and the answer lands from the Run — on the right agent.
    expect(seams.getAgentRun).toHaveBeenCalledWith(CS_AGENT, "run-cs-1");
    expect(assistant(result).runRef).toEqual({ agentId: CS_AGENT, runId: "run-cs-1" });
    expect(assistant(result).text).toBe("The outreach draft is parked for approval.");
    expect(result.current.busy).toBe(false);
  });

  it("a continued Conversation re-reads its CURRENT version instead of guessing", async () => {
    vi.useFakeTimers();
    const seams = runClient();
    const { result } = renderHook(() =>
      useAgentStream(seams.client, "principal-1", "console-chat", TARGET),
    );
    await act(async () => {
      void result.current.send("conv-cs-9", "And record it in the CRM.");
      await vi.advanceTimersByTimeAsync(1_000);
    });

    expect(seams.createAgentConversation).not.toHaveBeenCalled();
    expect(seams.getConversation).toHaveBeenCalledWith(CS_AGENT, "conv-cs-9");
    const [, body] = seams.startAgentRun.mock.calls[0]! as unknown as [
      string,
      Record<string, unknown>,
    ];
    expect(body["conversation_id"]).toBe("conv-cs-9");
    expect(body["expected_version"]).toBe(4);
  });

  it("a refused submission states the server's own verdict, not a fallback turn", async () => {
    vi.useFakeTimers();
    const seams = runClient({
      startAgentRun: vi.fn(async () => {
        throw new ApiError(
          "request conflicts with current Run state",
          409,
          "request conflicts with current Run state",
        );
      }),
    });
    const { result } = renderHook(() =>
      useAgentStream(seams.client, "principal-1", "console-chat", TARGET),
    );
    await act(async () => {
      void result.current.send(undefined, "Send the outreach.");
      await vi.advanceTimersByTimeAsync(1_000);
    });
    // No chat-wire fallback: the refusal is the turn's stated end.
    expect(seams.createSession).not.toHaveBeenCalled();
    expect(seams.sendMessage).not.toHaveBeenCalled();
    expect(assistant(result).streaming).toBe(false);
    expect(assistant(result).failure).toBeDefined();
    expect(result.current.busy).toBe(false);
  });
});
