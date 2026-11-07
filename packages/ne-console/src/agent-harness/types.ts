// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

/** The governed surface that supplied a frame. Never inferred from its payload. */
export type AgentFrameSource =
  "ask" | "workflow" | "trace" | "answer-ledger" | "conversation-ledger" | "unknown";

/** The authority for a frame's position. Browser time is deliberately absent. */
export type AgentFrameOrderBasis = "wire-arrival" | "audit-seq" | "server-time" | "source-order";

/** Exact identifiers a source supplied. An adapter leaves unavailable joins absent. */
export interface AgentFrameCorrelation {
  traceId?: string;
  sessionId?: string;
  workflowId?: string;
  runId?: string;
  turnId?: string;
  eventId?: string;
  approvalId?: string;
  questionId?: string;
}

export type AgentFrameStatus =
  "neutral" | "active" | "pending" | "allowed" | "denied" | "complete" | "failed" | "refused";

export interface AgentFrameLink {
  label: string;
  href: string;
}

export interface AgentFrameFact {
  label: string;
  value: string | number | boolean | null;
  mono?: boolean;
}

interface AgentFrameBase {
  /** Namespaced, source-stable identity. Two sources never share a React key by accident. */
  key: string;
  source: AgentFrameSource;
  orderBasis: AgentFrameOrderBasis;
  correlation: AgentFrameCorrelation;
  /** Present only when the source supplied an ordered position. */
  ordinal?: number;
  /** Present only when the source supplied an authoritative sequence. */
  sequence?: number;
  /** Present only when the source supplied a timestamp. */
  occurredAt?: string;
  title: string;
  body?: string;
  status: AgentFrameStatus;
  facts?: readonly AgentFrameFact[];
  links?: readonly AgentFrameLink[];
}

export interface PromptFrame extends AgentFrameBase {
  kind: "prompt";
}

export interface AnswerFrame extends AgentFrameBase {
  kind: "answer";
}

export interface ToolDecisionFrame extends AgentFrameBase {
  kind: "tool-decision";
  tool: string;
  decision: "allow" | "deny" | "pending" | "unknown";
  /** Concise, server-grounded transition text for the single polite live region. */
  announcement: string;
}

/** A durable handoff or refusal. It is evidence, never a local confirmation control. */
export interface GovernanceCheckpointFrame extends AgentFrameBase {
  kind: "governance-checkpoint";
  checkpoint:
    | "approval"
    | "pdp-denial"
    | "acl-refusal"
    | "model-route-approval"
    | "human-checkpoint"
    | "unknown";
  announcement: string;
}

export interface CitationsFrame extends AgentFrameBase {
  kind: "citations";
  citations: readonly {
    key: string;
    label: string;
    href?: string;
  }[];
}

export interface ModelCallFrame extends AgentFrameBase {
  kind: "model-call";
}

export interface ContextChangeFrame extends AgentFrameBase {
  kind: "context-change";
  announcement: string;
}

export interface RetrievalFrame extends AgentFrameBase {
  kind: "retrieval";
}

export interface GuardrailFrame extends AgentFrameBase {
  kind: "guardrail";
  announcement: string;
}

export interface WorkflowFrame extends AgentFrameBase {
  kind: "workflow";
}

export interface TerminalFrame extends AgentFrameBase {
  kind: "terminal";
  announcement: string;
}

/** Forward-compatible inert fallback. Raw content is escaped inside a disclosure. */
export interface UnknownFrame extends AgentFrameBase {
  kind: "unknown";
  raw: unknown;
}

/**
 * There is intentionally no reasoning/chain-of-thought frame. Only observable,
 * source-authored facts belong in this transcript.
 */
export type AgentFrame =
  | PromptFrame
  | AnswerFrame
  | ToolDecisionFrame
  | GovernanceCheckpointFrame
  | CitationsFrame
  | ModelCallFrame
  | ContextChangeFrame
  | RetrievalFrame
  | GuardrailFrame
  | WorkflowFrame
  | TerminalFrame
  | UnknownFrame;

export interface AgentTranscriptModel {
  label: string;
  frames: readonly AgentFrame[];
  busy: boolean;
}

/** Material transitions alone feed the transcript's polite announcer. */
export function materialAnnouncement(frame: AgentFrame): string | null {
  switch (frame.kind) {
    case "tool-decision":
    case "governance-checkpoint":
    case "context-change":
    case "guardrail":
    case "terminal":
      return frame.announcement;
    default:
      return null;
  }
}
