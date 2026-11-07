// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { ApiError } from "./client";
import { API_BASE } from "./config";
import type {
  ConversationLedgerView,
  ConversationMessageView,
  ConversationTurnView,
  ProtectedBindingView,
  ProtectedContentStatus,
} from "../conversation-ledger/types";

export interface ConversationLedgerClientOptions {
  getAccessToken: () => string | undefined;
  onUnauthorized: () => void;
}

export interface ConversationLedgerClient {
  read(signal?: AbortSignal): Promise<ConversationLedgerView>;
}

export class ConversationLedgerCutoverUnavailableError extends TypeError {}

const CONTENT_STATES = ["available", "content_withheld", "legacy_unavailable"] as const;
const CONTENT_STATUSES = ["source_free", "source_bound_container", "legacy_unavailable"] as const;
const SHA256 = /^[0-9a-f]{64}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function malformed(): never {
  throw new TypeError("malformed protected conversation ledger response");
}

function parseBinding(value: unknown): ProtectedBindingView {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["status", "protected_ref", "protected_hash"]) ||
    !CONTENT_STATUSES.includes(value.status as (typeof CONTENT_STATUSES)[number]) ||
    !isNonEmptyString(value.protected_ref) ||
    typeof value.protected_hash !== "string" ||
    !SHA256.test(value.protected_hash)
  ) {
    return malformed();
  }
  return {
    status: value.status as ProtectedContentStatus,
    protected_ref: value.protected_ref,
    protected_hash: value.protected_hash,
  };
}

function parseMessage(value: unknown): ConversationMessageView {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["role", "content"]) ||
    (value.role !== "user" && value.role !== "assistant") ||
    typeof value.content !== "string"
  ) {
    return malformed();
  }
  return { role: value.role, content: value.content };
}

function parseTurn(value: unknown): ConversationTurnView {
  if (!isRecord(value)) return malformed();
  const contentState = value.content_state;
  if (!CONTENT_STATES.includes(contentState as (typeof CONTENT_STATES)[number])) {
    return malformed();
  }
  const keys = [
    "session_id",
    "agent_name",
    "department",
    "seq",
    "principal_id",
    "trace_id",
    "attribution",
    "transport_generation",
    "input",
    "terminal",
    "created_at",
    "content_state",
    ...(contentState === "available" ? ["messages"] : []),
  ];
  if (
    !hasExactKeys(value, keys) ||
    !isNonEmptyString(value.session_id) ||
    !isNonEmptyString(value.agent_name) ||
    !isNonEmptyString(value.department) ||
    !Number.isInteger(value.seq) ||
    Number(value.seq) < 1 ||
    !isNonEmptyString(value.principal_id) ||
    !isNonEmptyString(value.trace_id) ||
    value.attribution !== "exact" ||
    value.transport_generation !== "agent-transport-v1" ||
    !isNonEmptyString(value.created_at)
  ) {
    return malformed();
  }
  const input = parseBinding(value.input);
  const terminal = parseBinding(value.terminal);
  const statuses = [input.status, terminal.status];
  const common = {
    session_id: value.session_id,
    agent_name: value.agent_name,
    department: value.department,
    seq: value.seq as number,
    principal_id: value.principal_id,
    trace_id: value.trace_id,
    attribution: "exact" as const,
    transport_generation: "agent-transport-v1" as const,
    input,
    terminal,
    created_at: value.created_at,
  };

  if (contentState === "available") {
    if (
      statuses.some((status) => status !== "source_free") ||
      !Array.isArray(value.messages) ||
      value.messages.length !== 2
    ) {
      return malformed();
    }
    const messages = value.messages.map(parseMessage);
    if (messages[0]?.role !== "user" || messages[1]?.role !== "assistant") return malformed();
    return { ...common, content_state: "available", messages };
  }
  if (
    contentState === "content_withheld" &&
    statuses.includes("source_bound_container") &&
    !statuses.includes("legacy_unavailable")
  ) {
    return { ...common, content_state: "content_withheld" };
  }
  if (
    contentState === "legacy_unavailable" &&
    statuses.every((status) => status === "legacy_unavailable")
  ) {
    return { ...common, content_state: "legacy_unavailable" };
  }
  return malformed();
}

export function parseConversationLedger(value: unknown): ConversationLedgerView {
  if (
    isRecord(value) &&
    (Object.hasOwn(value, "total_turns") || Object.hasOwn(value, "response_byte_limit"))
  ) {
    // The retired plaintext-ledger envelope is never interpreted as protected
    // data. Keep the reason neutral; no legacy body is inspected or surfaced.
    throw new ConversationLedgerCutoverUnavailableError(
      "protected transport cutover not completed or unavailable",
    );
  }
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["turns", "returned_turns", "truncated", "retention"]) ||
    !Array.isArray(value.turns) ||
    !Number.isInteger(value.returned_turns) ||
    Number(value.returned_turns) < 0 ||
    value.returned_turns !== value.turns.length ||
    typeof value.truncated !== "boolean" ||
    value.retention !== "until_governed_reset"
  ) {
    return malformed();
  }
  return {
    turns: value.turns.map(parseTurn),
    returned_turns: value.returned_turns as number,
    truncated: value.truncated,
    retention: value.retention,
  };
}

export function createConversationLedgerClient(
  options: ConversationLedgerClientOptions,
): ConversationLedgerClient {
  return {
    async read(signal) {
      const token = options.getAccessToken();
      const response = await fetch(`${API_BASE}/v1/chat/conversations`, {
        method: "GET",
        signal,
        headers: {
          Accept: "application/json",
          ...(token === undefined ? {} : { Authorization: `Bearer ${token}` }),
        },
      });
      if (response.status === 401) {
        options.onUnauthorized();
        throw new ApiError("unauthorized", 401);
      }
      if (!response.ok) {
        let detail: string | undefined;
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") detail = body.detail;
        } catch {
          // Status remains authoritative when a proxy served no JSON body.
        }
        throw new ApiError(
          `Conversation ledger request failed with HTTP ${response.status}`,
          response.status,
          detail,
        );
      }
      return parseConversationLedger(await response.json());
    },
  };
}
