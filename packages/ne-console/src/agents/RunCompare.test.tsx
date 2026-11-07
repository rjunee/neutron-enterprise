// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE RUN COMPARE VIEW. The load-bearing claims:
//
//   * each column renders ITS OWN Run's served record — answer, claims, row
//     anchors — addressed purely by the URL, with no privileged agent or run;
//   * the row anchor drawn is the SERVED value, short form with the full hash
//     in the title, and the console makes NO match/mismatch statement about
//     the two sides — there is no equality claim anywhere in the DOM;
//   * an absent evidence read is STATED, never padded with invented anchors;
//   * the picker offers the served catalog and the served run list, and a pick
//     writes the URL — the URL is the whole state.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { AgentEvidenceEnvelopeWire } from "../api/types";
import { RunComparePage, RUN_COMPARE_EVIDENCE_UNAVAILABLE } from "./RunCompare";
import { runCompareHref, runCompareRefFromSearch } from "./runCompareLink";
import type { AgentDetailView, AgentsClient, RunDetail } from "./types";

const AGENT_A = "agt_01k3analyst00000000000000000";
const AGENT_B = "agt_01k3verified0000000000000000";
const RUN_A = "run_01k3agentic0";
const RUN_B = "run_01k3verified";
const ANCHOR_A = "3c".repeat(32);
const ANCHOR_B = "5e".repeat(32);

function agent(over: Partial<AgentDetailView>): AgentDetailView {
  return {
    agent_id: AGENT_A,
    version: 1,
    label: "Analyst",
    description: "",
    owner_label: "Ops",
    lifecycle: "active",
    allowed_strategies: ["agentic"],
    default_strategy: "agentic",
    model_policy_id: "policy",
    tool_grants: [],
    knowledge_sources: [],
    skills: [],
    budgets: { max_steps: 1, max_tool_calls: 1, max_input_tokens: 1, max_output_tokens: 1 },
    enforced_caps: null,
    workflow: null,
    available: true,
    availability_reasons: [],
    config_hash: "d".repeat(64),
    ...over,
  };
}

const analyst = agent({ agent_id: AGENT_A, label: "Dispute Analyst" });
const verified = agent({
  agent_id: AGENT_B,
  label: "Dispute Ledger",
  allowed_strategies: ["deterministic"],
  default_strategy: "deterministic",
});

function runDetail(over: Partial<RunDetail>): RunDetail {
  return {
    run_id: RUN_A,
    conversation_id: "conv-1",
    agent_id: AGENT_A,
    agent_version: 1,
    sequence: 2,
    version: 1,
    predecessor_run_id: null,
    root_run_id: RUN_A,
    retry_ordinal: 0,
    display_label: "Run 2",
    strategy: "agentic",
    status: "completed",
    output_verdict: "verified",
    model_policy_id: "policy",
    model_request_mode: "auto",
    context_snapshot_hash: null,
    requested_model: null,
    requested_model_lane: null,
    served_model: "private-primary",
    route_position: "unavailable",
    fallback_from: null,
    output_preview: "Three disputes are overdue.",
    output_state: "available",
    failure_reason: null,
    config_hash: "f".repeat(64),
    capability_snapshot_hash: "c".repeat(64),
    created_at: "2026-08-27T09:31:12Z",
    updated_at: "2026-08-27T09:31:21Z",
    terminal_at: "2026-08-27T09:31:21Z",
    approval_id: null,
    agentic_plan: null,
    execution_profile_receipt: null,
    skill_activation_receipt: null,
    claims: [],
    claim_mapping_state: "complete",
    claims_truncated: false,
    ...over,
  } as RunDetail;
}

const runA = runDetail({});
const runB = runDetail({
  run_id: RUN_B,
  agent_id: AGENT_B,
  strategy: "deterministic",
  display_label: "Run 7",
  output_preview: "One dispute is overdue.",
});

function envelopeFor(runId: string, anchor: string, display: string): AgentEvidenceEnvelopeWire {
  return {
    schema_version: 1,
    agent_id: runId === RUN_A ? AGENT_A : AGENT_B,
    run_id: runId,
    run_status: "completed",
    mapping_state: "complete",
    mapping_message: "Claim mapping complete",
    output: { verdict: "verified", state: "available", message: "", display },
    claims: [
      {
        claim_id: `claim-${runId}`,
        claim_verdict: "verified",
        state: "available",
        message: "Claim available",
        display,
        evidence_id: `ev-${runId}`,
        association_hash: "a".repeat(64),
        verifier_event_id: null,
        verifier_receipt_hash: null,
      },
    ],
    evidence: [
      {
        evidence_id: `ev-${runId}`,
        state: "available",
        message: "Verified row",
        assurance: "direct",
        locator: { kind: "verified_t3", row_anchor: anchor, receipt_hash: "b".repeat(64) },
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
}

function fakeClient(overrides: Partial<AgentsClient> = {}): AgentsClient {
  return {
    listAgents: vi.fn(async () => ({ items: [analyst, verified], next_cursor: null })),
    getAgent: vi.fn(async (agentId: string) => (agentId === AGENT_A ? analyst : verified)),
    listAgentConversations: vi.fn(async () => ({ items: [], next_cursor: null })),
    getConversation: vi.fn(async () => ({}) as never),
    createAgentConversation: vi.fn(),
    listConversationTurns: vi.fn(async () => ({ items: [], next_cursor: null })),
    listConversationRuns: vi.fn(async () => ({ items: [], next_cursor: null })),
    listAgentRuns: vi.fn(async (agentId: string) => ({
      items: agentId === AGENT_A ? [runA] : [runB],
      next_cursor: null,
    })),
    getAgentPreflight: vi.fn(),
    getAgentWorkflowProfile: vi.fn(),
    listAgentRunUpdates: vi.fn(),
    startAgentRun: vi.fn(),
    getAgentRun: vi.fn(async (_agentId: string, runId: string) => (runId === RUN_A ? runA : runB)),
    listAgentRunEvents: vi.fn(async () => ({ items: [], next_cursor: null })),
    getAgentRunEvidence: vi.fn(async (_agentId: string, runId: string) =>
      runId === RUN_A
        ? envelopeFor(RUN_A, ANCHOR_A, "mer-dsp-002")
        : envelopeFor(RUN_B, ANCHOR_B, "mer-dsp-001"),
    ),
    cancelAgentRun: vi.fn(),
    retryAgentRun: vi.fn(),
    ...overrides,
  } as AgentsClient;
}

function mount(entry: string, client: AgentsClient) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/agents/compare"
          element={<RunComparePage client={client} identityKey="principal:demo" />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("the URL addresses both sides", () => {
  it("round-trips through the href builder and reader", () => {
    const href = runCompareHref({ agentId: AGENT_A, runId: RUN_A });
    const search = new URLSearchParams(href.split("?")[1]);
    expect(runCompareRefFromSearch(search, "a")).toEqual({ agentId: AGENT_A, runId: RUN_A });
    expect(runCompareRefFromSearch(search, "b")).toBeNull();
  });

  it("renders BOTH Runs' served records side by side, each with its own served anchor", async () => {
    const client = fakeClient();
    mount(
      runCompareHref({ agentId: AGENT_A, runId: RUN_A }, { agentId: AGENT_B, runId: RUN_B }),
      client,
    );

    const left = await screen.findByRole("region", { name: "Left Run" });
    const right = await screen.findByRole("region", { name: "Right Run" });

    // Each side's own answer, claims and anchor — never its neighbour's.
    expect(within(left).getByText("Three disputes are overdue.")).toBeInTheDocument();
    expect(within(right).getByText("One dispute is overdue.")).toBeInTheDocument();
    expect(within(left).getByText("mer-dsp-002")).toBeInTheDocument();
    expect(within(right).getByText("mer-dsp-001")).toBeInTheDocument();

    // The anchor is the SERVED value: short form on screen, the full served
    // hash in the title. One per side, and both anchors appear in full.
    const leftShorts = within(left).getAllByText(ANCHOR_A.slice(0, 8));
    expect(leftShorts.some((el) => el.getAttribute("title") === ANCHOR_A)).toBe(true);
    const rightShorts = within(right).getAllByText(ANCHOR_B.slice(0, 8));
    expect(rightShorts.some((el) => el.getAttribute("title") === ANCHOR_B)).toBe(true);

    // NO equality verdict anywhere: the console displays, the reader compares.
    expect(screen.queryByText(/match/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/identical/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/differ/i)).not.toBeInTheDocument();
  });

  it("states an absent evidence read instead of inventing anchors", async () => {
    const client = fakeClient();
    delete (client as { getAgentRunEvidence?: unknown }).getAgentRunEvidence;
    mount(runCompareHref({ agentId: AGENT_A, runId: RUN_A }), client);
    const left = await screen.findByRole("region", { name: "Left Run" });
    await waitFor(() =>
      expect(within(left).getAllByText(RUN_COMPARE_EVIDENCE_UNAVAILABLE).length).toBeGreaterThan(0),
    );
    expect(within(left).queryByText(ANCHOR_A.slice(0, 8))).not.toBeInTheDocument();
  });
});

describe("the picker fills a side from the served lists, through the URL", () => {
  it("agent then run, and the chosen Run's record renders in place", async () => {
    const user = userEvent.setup();
    const client = fakeClient();
    mount("/agents/compare", client);

    const left = await screen.findByRole("region", { name: "Left Run" });
    await user.selectOptions(await within(left).findByRole("combobox"), AGENT_B);
    await user.click(await within(left).findByRole("button", { name: /Run 7/ }));

    expect(await within(left).findByText("One dispute is overdue.")).toBeInTheDocument();
    const shorts = within(left).getAllByText(ANCHOR_B.slice(0, 8));
    expect(shorts.some((el) => el.getAttribute("title") === ANCHOR_B)).toBe(true);
    // The right side is untouched and still offers its own picker.
    const right = screen.getByRole("region", { name: "Right Run" });
    expect(within(right).getByRole("combobox")).toBeInTheDocument();
  });
});
