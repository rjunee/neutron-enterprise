// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "../test/mocks/server";
import type {
  ConversationLedgerView,
  ConversationTurnView,
  ProtectedBindingView,
  ProtectedContentStatus,
} from "../conversation-ledger/types";
import {
  createConversationLedgerClient,
  parseConversationLedger,
} from "./conversationLedgerClient";

const TOKEN = "conversation-ledger-token";

function binding(status: ProtectedContentStatus, marker: string): ProtectedBindingView {
  return {
    status,
    protected_ref: "protected-" + marker,
    protected_hash: marker.repeat(64),
  };
}

function turn(
  contentState: ConversationTurnView["content_state"],
  inputStatus: ProtectedContentStatus,
  terminalStatus: ProtectedContentStatus,
): ConversationTurnView {
  const common = {
    session_id: "conversation-1",
    agent_name: "console-chat",
    department: "finance",
    seq: 7,
    principal_id: "user:subject",
    trace_id: "trace-exact",
    attribution: "exact" as const,
    transport_generation: "agent-transport-v1" as const,
    input: binding(inputStatus, "a"),
    terminal: binding(terminalStatus, "b"),
    created_at: "2026-08-18T17:00:00Z",
  };
  return contentState === "available"
    ? {
        ...common,
        content_state: "available",
        messages: [
          { role: "user", content: "QUESTION" },
          { role: "assistant", content: "ANSWER" },
        ],
      }
    : { ...common, content_state: contentState };
}

const EMPTY: ConversationLedgerView = {
  turns: [],
  returned_turns: 0,
  truncated: false,
  retention: "until_governed_reset",
};

const PROTECTED: ConversationLedgerView = {
  turns: [
    turn("available", "source_free", "source_free"),
    turn("content_withheld", "source_bound_container", "source_free"),
    turn("legacy_unavailable", "legacy_unavailable", "legacy_unavailable"),
  ],
  returned_turns: 3,
  truncated: false,
  retention: "until_governed_reset",
};

function client(onUnauthorized = () => {}) {
  return createConversationLedgerClient({
    getAccessToken: () => TOKEN,
    onUnauthorized,
  });
}

describe("createConversationLedgerClient", () => {
  it("reads and validates only the governed protected conversation route", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    server.use(
      http.get("*/api/v1/chat/conversations", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json(PROTECTED);
      }),
    );

    await expect(client().read()).resolves.toEqual(PROTECTED);
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/chat/conversations");
  });

  it("rejects body fields on an opaque shell instead of admitting protected bytes", () => {
    const raw = structuredClone(PROTECTED) as unknown as {
      turns: Array<Record<string, unknown>>;
    };
    const withheld = raw.turns[1];
    if (withheld === undefined) throw new Error("withheld fixture expected");
    raw.turns[1] = {
      ...withheld,
      messages: [{ role: "assistant", content: "SOURCE-BOUND-PAYLOAD-SENTINEL" }],
      payload: "RETIRED-PAYLOAD-SENTINEL",
    };

    expect(() => parseConversationLedger(raw)).toThrow(/malformed protected conversation ledger/);
  });

  it.each(["messages", "payload"])(
    "rejects an otherwise-valid withheld shell carrying only %s",
    (field) => {
      const raw = structuredClone(PROTECTED) as unknown as {
        turns: Array<Record<string, unknown>>;
      };
      const withheld = raw.turns[1];
      if (withheld === undefined) throw new Error("withheld fixture expected");
      withheld[field] =
        field === "messages"
          ? [{ role: "assistant", content: "WITHHELD-MESSAGE" }]
          : "WITHHELD-PAYLOAD";
      expect(() => parseConversationLedger(raw)).toThrow(/malformed protected conversation ledger/);
    },
  );

  it.each([
    [0, "input", "source_bound_container"],
    [1, "input", "source_free"],
    [2, "terminal", "source_free"],
  ] as const)(
    "rejects sole-delta state/status mismatch on row %s %s",
    (rowIndex, bindingName, status) => {
      const raw = structuredClone(PROTECTED) as unknown as {
        turns: Array<Record<string, unknown>>;
      };
      const row = raw.turns[rowIndex];
      if (row === undefined) throw new Error("turn fixture expected");
      (row[bindingName] as Record<string, unknown>).status = status;
      expect(() => parseConversationLedger(raw)).toThrow(/malformed protected conversation ledger/);
    },
  );

  it("classifies the legacy envelope as neutral cutover unavailability without parsing bodies", async () => {
    server.use(
      http.get("*/api/v1/chat/conversations", () =>
        HttpResponse.json({
          turns: [{ messages: [{ content: "LEGACY-PRIVATE-SENTINEL" }] }],
          total_turns: 1,
          returned_turns: 1,
          truncated: false,
          retention: "until_governed_reset",
          response_byte_limit: 1_000_000,
        }),
      ),
    );
    await expect(client().read()).rejects.toThrow(
      "protected transport cutover not completed or unavailable",
    );
  });

  it("does not infer source-free availability when the disclosure state is absent", () => {
    const raw = structuredClone(PROTECTED) as unknown as {
      turns: Array<Record<string, unknown>>;
    };
    const available = raw.turns[0];
    if (available === undefined) throw new Error("available fixture expected");
    delete available.content_state;

    expect(() => parseConversationLedger(raw)).toThrow(/malformed protected conversation ledger/);
  });

  it("does not infer the owner when the server principal is absent", () => {
    const raw = structuredClone(PROTECTED) as unknown as {
      turns: Array<Record<string, unknown>>;
    };
    const available = raw.turns[0];
    if (available === undefined) throw new Error("available fixture expected");
    delete available.principal_id;

    expect(() => parseConversationLedger(raw)).toThrow(/malformed protected conversation ledger/);
  });

  it.each([
    [
      "state/status mismatch",
      (row: Record<string, unknown>) => {
        row.content_state = "available";
      },
    ],
    [
      "empty protected ref",
      (row: Record<string, unknown>) => {
        (row.input as Record<string, unknown>).protected_ref = "";
      },
    ],
    [
      "invalid protected hash",
      (row: Record<string, unknown>) => {
        (row.terminal as Record<string, unknown>).protected_hash = "not-a-sha256";
      },
    ],
    [
      "unknown transport generation",
      (row: Record<string, unknown>) => {
        row.transport_generation = "legacy_transport_retired";
      },
    ],
  ])("rejects a %s rather than accepting mismatched safe metadata", (_label, mutate) => {
    const raw = structuredClone(PROTECTED) as unknown as {
      turns: Array<Record<string, unknown>>;
    };
    const withheld = raw.turns[1];
    if (withheld === undefined) throw new Error("withheld fixture expected");
    mutate(withheld);
    expect(() => parseConversationLedger(raw)).toThrow(/malformed protected conversation ledger/);
  });

  it("preserves a governed refusal without converting it to an empty ledger", async () => {
    server.use(
      http.get("*/api/v1/chat/conversations", () =>
        HttpResponse.json({ detail: "not authorized" }, { status: 403 }),
      ),
    );
    await expect(client().read()).rejects.toMatchObject({
      status: 403,
      detail: "not authorized",
    });
  });

  it("invokes the primary re-auth path exactly once on 401", async () => {
    server.use(
      http.get("*/api/v1/chat/conversations", () => new HttpResponse(null, { status: 401 })),
    );
    const onUnauthorized = vi.fn();
    await expect(client(onUnauthorized).read()).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("accepts the empty protected ledger envelope", () => {
    expect(parseConversationLedger(EMPTY)).toEqual(EMPTY);
  });
});
