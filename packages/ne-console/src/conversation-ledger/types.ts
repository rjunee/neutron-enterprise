// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

export type ProtectedContentStatus =
  "source_free" | "source_bound_container" | "legacy_unavailable";

export interface ProtectedBindingView {
  status: ProtectedContentStatus;
  protected_ref: string;
  protected_hash: string;
}

export interface ConversationMessageView {
  role: "user" | "assistant";
  content: string;
}

interface ProtectedConversationTurnBase {
  session_id: string;
  agent_name: string;
  department: string;
  seq: number;
  principal_id: string;
  trace_id: string;
  attribution: "exact";
  transport_generation: "agent-transport-v1";
  input: ProtectedBindingView;
  terminal: ProtectedBindingView;
  created_at: string;
}

export interface AvailableConversationTurnView extends ProtectedConversationTurnBase {
  content_state: "available";
  messages: ConversationMessageView[];
}

export interface WithheldConversationTurnView extends ProtectedConversationTurnBase {
  content_state: "content_withheld";
}

export interface LegacyUnavailableConversationTurnView extends ProtectedConversationTurnBase {
  content_state: "legacy_unavailable";
}

export type ConversationTurnView =
  | AvailableConversationTurnView
  | WithheldConversationTurnView
  | LegacyUnavailableConversationTurnView;

export interface ConversationLedgerView {
  turns: ConversationTurnView[];
  returned_turns: number;
  truncated: boolean;
  retention: "until_governed_reset";
}
