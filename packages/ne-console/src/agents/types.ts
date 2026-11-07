// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import type {
  AcceptedAgentRunSubmissionWire,
  AgentCapabilityPreflightWire,
  AgentCatalogItemWire,
  AgentCatalogPageWire,
  AgentConversationPageWire,
  AgentConversationTurnPageWire,
  AgentConversationWire,
  AgentRunUpdatePageWire,
  AgentRunUpdateViewWire,
  AgentRunDetailWire,
  AgentRunEventPageWire,
  AgentEvidenceEnvelopeWire,
  ControlAgentRunRequestWire,
  AgentWorkflowProfileWire,
  AgentRunPageWire,
  AgentRunStatusWire,
  AgentRunWire,
  ApprovalActionResult,
  ApprovalPayloadDisclosureView,
  PendingApprovalView,
  StartAgentRunRequestWire,
} from "../api/types";

export type AgentSummary = AgentCatalogItemWire;
export type AgentPage = AgentCatalogPageWire;
export type AgentDetailView = AgentCatalogItemWire;
export type ConversationSummary = AgentConversationWire;
export type ConversationPage = AgentConversationPageWire;
export type RunStatus = AgentRunStatusWire;
export type RunReceipt = AgentRunWire;
export type RunDetail = AgentRunDetailWire;
export type RunEventPage = AgentRunEventPageWire;
export type ControlRunRequest = ControlAgentRunRequestWire;
export type RunPage = AgentRunPageWire;
export type ConversationTurnPage = AgentConversationTurnPageWire;
export type AgentWorkflowProfile = AgentWorkflowProfileWire;
export type AgentCapabilityPreflight = AgentCapabilityPreflightWire;
export type StartAgentRunRequest = StartAgentRunRequestWire;
export type AcceptedAgentRunSubmission = AcceptedAgentRunSubmissionWire;

/** THE COMPOSER'S SUBMISSION ENVELOPE FOR A NON-DEFAULT AGENTIC AGENT.
 *
 * The default-Ask chat wire reaches exactly ONE agent — the canonical default
 * (`createSession("console-chat")` admits nothing else, ne-api routers/chat.py)
 * — so a turn typed at any other agentic Agent must go where every other
 * canonical Run goes: `POST /v1/agents/{id}/runs`, carrying the same frozen
 * identities the deterministic launcher presents. Every field is a SERVED
 * value re-presented, never derived here: the workspace's own agent read, its
 * capability preflight, and the platform's frozen config hash. The server
 * revalidates all of them at admission and a stale one refuses the Run —
 * which is the fail-closed behavior this envelope exists to preserve. */
export interface AgentRunTarget {
  readonly agentId: string;
  readonly agentVersion: number;
  /** Frozen platform configuration hash (GET /v1/posture). */
  readonly configHash: string;
  /** The served capability snapshot hash admission validates against. */
  readonly capabilityHash: string;
}

export interface AgentEligibility {
  readonly agent: AgentDetailView;
  readonly preflight: AgentCapabilityPreflight;
  readonly profile: AgentWorkflowProfile | null;
}

/** Read-only injection seam for 012A. It intentionally has no create/submit/cancel method. */
export interface AgentsInteriorClient {
  listAgents(cursor?: string): Promise<AgentPage>;
  getAgent(agentId: string): Promise<AgentDetailView>;
  listAgentConversations(agentId: string, cursor?: string): Promise<ConversationPage>;
  getConversation(agentId: string, conversationId: string): Promise<ConversationSummary>;
  listConversationTurns(
    agentId: string,
    conversationId: string,
    cursor?: string,
  ): Promise<ConversationTurnPage>;
  listConversationRuns(agentId: string, conversationId: string, cursor?: string): Promise<RunPage>;
  listAgentRuns(agentId: string, cursor?: string): Promise<RunPage>;
}

/** The bounded executable seam shipped by PRE-012.  It starts a canonical Run
 * only inside an already-owned Conversation; there is no create-Conversation
 * method because the backend does not expose one. */
export interface AgentsExecutionClient {
  createAgentConversation(agentId: string): Promise<ConversationSummary>;
  getAgentPreflight(agentId: string, capabilityHash?: string): Promise<AgentCapabilityPreflight>;
  getAgentWorkflowProfile(agentId: string): Promise<AgentWorkflowProfile>;
  listAgentRunUpdates(
    view: AgentRunUpdateViewWire,
    windowStart: string,
    windowEnd: string,
    cursor?: string,
  ): Promise<AgentRunUpdatePageWire>;
  startAgentRun(
    agentId: string,
    body: StartAgentRunRequest,
    idempotencyKey: string,
  ): Promise<AcceptedAgentRunSubmission>;
  getAgentRun(agentId: string, runId: string): Promise<RunReceipt>;
  listAgentRunEvents(
    agentId: string,
    runId: string,
    cursor?: string,
    limit?: number,
  ): Promise<RunEventPage>;
  listPendingApprovals?(): Promise<{ approvals: PendingApprovalView[] }>;
  readApprovalPayload?(requestId: string): Promise<ApprovalPayloadDisclosureView>;
  approve?(requestId: string): Promise<ApprovalActionResult>;
  reject?(requestId: string, reason?: string): Promise<ApprovalActionResult>;
  getAgentRunEvidence?(agentId: string, runId: string): Promise<AgentEvidenceEnvelopeWire>;
  exportAgentRunEvidence?(agentId: string, runId: string): Promise<AgentEvidenceEnvelopeWire>;
  cancelAgentRun(
    agentId: string,
    runId: string,
    body: ControlRunRequest,
    idempotencyKey: string,
  ): Promise<AcceptedAgentRunSubmission>;
  retryAgentRun(
    agentId: string,
    runId: string,
    body: ControlRunRequest,
    idempotencyKey: string,
  ): Promise<AcceptedAgentRunSubmission>;
}

export type AgentsClient = AgentsInteriorClient & AgentsExecutionClient;

export interface ExecutionIdentity {
  readonly principal_label: string;
  readonly department_label?: string;
  readonly role_labels: readonly string[];
}
