// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { AgentRunEventWire } from "../api/types";
import type { AgentsClient } from "./types";
import { AgentRunPage } from "./AgentRunPage";
import runPageSource from "./AgentRunPage.tsx?raw";

const run = {
  run_id: "run-2",
  conversation_id: "conversation-1",
  agent_id: "meridian",
  agent_version: 3,
  sequence: 2,
  version: 4,
  predecessor_run_id: "run-1",
  root_run_id: "run-1",
  retry_ordinal: 1,
  display_label: "Run 2 · Deterministic · 09:00",
  strategy: "deterministic" as const,
  status: "failed" as const,
  output_verdict: "unavailable" as const,
  model_policy_id: "policy-1",
  model_request_mode: "auto" as const,
  context_snapshot_hash: "9".repeat(64),
  requested_model: null,
  requested_model_lane: null,
  served_model: "private-model",
  route_position: "unavailable" as const,
  fallback_from: null,
  output_preview: null,
  config_hash: "a".repeat(64),
  capability_snapshot_hash: "b".repeat(64),
  created_at: "2026-08-23T00:00:00Z",
  updated_at: "2026-08-23T00:00:04Z",
  terminal_at: "2026-08-23T00:00:04Z",
  approval_id: null,
  agentic_plan: null,
  execution_profile_receipt: {
    profile_receipt_id: "profile-receipt-frozen",
    run_id: "run-2",
    attempt_id: "attempt-frozen",
    workflow_id: "frozen-safe-review",
    profile_version: 7,
    profile_hash: "c".repeat(64),
    input_schema: {
      type: "object",
      properties: { max_subjects: { type: "integer", minimum: 1, maximum: 5 } },
      required: ["max_subjects"],
    },
    input_schema_hash: "d".repeat(64),
    typed_input_state: "available" as const,
    typed_input: { max_subjects: 3 },
    ordered_plan: [
      {
        ordinal: 1,
        step_id: "frozen-lookup",
        kind: "governed-tool" as const,
        action: "Frozen safe lookup",
        model_role: null,
      },
    ],
    ordered_plan_hash: "e".repeat(64),
    receipt_hash: "f".repeat(64),
    created_at: "2026-08-23T00:00:01Z",
  },
  skill_activation_receipt: null,
};
const evidence = {
  schema_version: 1 as const,
  agent_id: "meridian",
  run_id: "run-2",
  run_status: "completed" as const,
  mapping_state: "complete" as const,
  mapping_message: "Claim mapping complete",
  output: {
    verdict: "grounded" as const,
    state: "available" as const,
    message: "Answer available",
    display: "Safe answer",
  },
  claims: [
    {
      claim_id: "claim-a",
      claim_verdict: "grounded" as const,
      state: "available" as const,
      message: "Claim available",
      display: "Same claim",
      evidence_id: "ev-a",
      association_hash: "a".repeat(64),
      verifier_event_id: null,
      verifier_receipt_hash: null,
    },
    {
      claim_id: "claim-b",
      claim_verdict: "grounded" as const,
      state: "available" as const,
      message: "Claim available",
      display: "Same claim",
      evidence_id: "ev-b",
      association_hash: "b".repeat(64),
      verifier_event_id: null,
      verifier_receipt_hash: null,
    },
  ],
  evidence: [
    {
      evidence_id: "ev-a",
      state: "available" as const,
      message: "Evidence available",
      assurance: "direct" as const,
      locator: { entity_id: "entity-secret" },
    },
    {
      evidence_id: "ev-b",
      state: "withheld" as const,
      message: "Evidence withheld — access changed",
      assurance: "unavailable" as const,
      locator: null,
      protected_output: "PRIVATE-WITHHELD-BYTES",
    },
  ],
  governance: {
    status: "partial" as const,
    delegated_user_label: "Owner",
    agent_service_identity: null,
    connector_credential_owner_label: null,
    effective_scopes: [],
    policy_environment: null,
    tool_tier: null,
    tool_decision: null,
    unavailable_reasons: ["policy_join_unavailable"],
  },
};

function client(overrides: Partial<AgentsClient> = {}): AgentsClient {
  return {
    getAgentRun: vi.fn(async () => run),
    getAgent: vi.fn<AgentsClient["getAgent"]>(async () => ({
      agent_id: "meridian",
      version: 3,
      label: "Meridian",
      description: "",
      owner_label: "Ops",
      visibility: "department",
      lifecycle: "active",
      allowed_strategies: ["deterministic"],
      default_strategy: "deterministic",
      model_policy_id: "policy-1",
      tool_grants: [],
      knowledge_sources: [],
      skills: [],
      tool_count: 0,
      knowledge_source_count: 0,
      skill_count: 0,
      budgets: { max_steps: 1, max_tool_calls: 1, max_input_tokens: 1, max_output_tokens: 1 },
      enforced_caps: null,
      workflow: null,
      available: true,
      availability_reasons: [],
      config_hash: "a".repeat(64),
    })),
    getConversation: vi.fn(async () => ({
      conversation_id: "conversation-1",
      agent_id: "meridian",
      title: "Restricted operations review",
      created_at: run.created_at,
      updated_at: run.updated_at,
      last_sequence: 2,
    })),
    getAgentWorkflowProfile: vi.fn<AgentsClient["getAgentWorkflowProfile"]>(async () => ({
      agent_id: "meridian",
      agent_version: 3,
      agent_config_hash: "a".repeat(64),
      strategy: "deterministic",
      workflow_id: "CURRENT-DRIFT-MUST-NOT-RENDER",
      workflow_version: 99,
      workflow_config_hash: "9".repeat(64),
      input_schema: {},
      fixed_inputs: {},
      fixed_plan: [
        {
          ordinal: 1,
          step_id: "lookup",
          kind: "governed-tool",
          action: "CURRENT-PLAN-MUST-NOT-RENDER",
          model_role: null,
        },
      ],
    })),
    listAgentRunEvents: vi.fn<AgentsClient["listAgentRunEvents"]>(async () => ({
      items: [
        {
          event_id: "late",
          run_id: "run-2",
          attempt_id: "attempt-2",
          sequence: 2,
          event_kind: "run.terminal",
          event_status: "failed",
          parent_event_id: null,
          step_id: null,
          metadata: [],
          detail_hash: "d".repeat(64),
          created_at: run.updated_at,
        },
        {
          event_id: "early",
          run_id: "run-2",
          attempt_id: "attempt-2",
          sequence: 1,
          event_kind: "event.unavailable",
          event_status: "running",
          parent_event_id: null,
          step_id: null,
          metadata: [],
          detail_hash: "e".repeat(64),
          created_at: run.created_at,
        },
      ],
      next_cursor: null,
    })),
    retryAgentRun: vi.fn<AgentsClient["retryAgentRun"]>(async () => ({
      action: "retry",
      run_id: "run-3",
      attempt_id: "attempt-3",
      version: 1,
      status: "submitted",
      enqueue_id: "enqueue-3",
      replayed: false,
    })),
    cancelAgentRun: vi.fn(),
    listAgents: vi.fn(),
    listAgentConversations: vi.fn(),
    createAgentConversation: vi.fn(),
    listConversationTurns: vi.fn(),
    getAgentPreflight: vi.fn(),
    listAgentRunUpdates: vi.fn(),
    listConversationRuns: vi.fn<AgentsClient["listConversationRuns"]>(async () => ({
      items: [run],
      next_cursor: null,
    })),
    listAgentRuns: vi.fn(),
    startAgentRun: vi.fn(),
    getAgentRunEvidence: vi.fn(async () => evidence),
    exportAgentRunEvidence: vi.fn(async () => evidence),
    ...overrides,
  };
}

/** THE MAST HAS SETTLED, AND THE CONVERSATION IS NOW A META LINE, NOT AN `<h2>`.
 *
 * Round 4 deleted the run-list rail this page carried, and the `<h2>` that
 * headed it went with it: the Conversation this Run belongs to is now said once
 * in the title row's quiet meta as "<conversation> · Run N", beside an `<h1>`
 * that is the Run's own served `display_label`. The FACT — which Conversation
 * the reader is looking at, read from the server's projection — is unchanged,
 * which is why every test below still gates on it. */
function mastSettled(conversation = "Restricted operations review", sequence = 2) {
  return screen.findByText(`${conversation} · Run ${sequence}`);
}

describe("PRE-013 Run workspace", () => {
  it("reopens labels, renders fixed plan and canonical timeline order without raw detail", async () => {
    const user = userEvent.setup();
    const api = client();
    render(
      <MemoryRouter>
        <AgentRunPage
          client={api}
          agentId="meridian"
          runId="run-2"
          identityKey="user:meridian-owner"
        />
      </MemoryRouter>,
    );
    expect(await mastSettled()).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    expect(screen.getByText(/Frozen safe lookup — fixed workflow step/)).toBeInTheDocument();
    expect(screen.getByText(/"max_subjects": 3/)).toBeInTheDocument();
    expect(screen.queryByText(/CURRENT-DRIFT|CURRENT-PLAN/)).not.toBeInTheDocument();
    expect(api.getAgentWorkflowProfile).not.toHaveBeenCalled();
    expect(screen.queryByText(/reasoning|secret/i)).toBeNull();
    await user.click(screen.getByRole("tab", { name: "Timeline" }));
    const timeline = within(screen.getByRole("tabpanel", { name: "Timeline" })).getByRole("list");
    // Canonical order, read by the word the clamped kind now carries.
    expect(within(timeline).getAllByRole("listitem")[0]).toHaveTextContent("Kind not disclosed");
    expect(screen.getByRole("button", { name: "Follow live" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("does not infer an unavailable frozen profile from the current workflow endpoint", async () => {
    const user = userEvent.setup();
    const api = client({
      getAgentRun: vi.fn(async () => ({ ...run, execution_profile_receipt: null })),
    });
    render(
      <MemoryRouter>
        <AgentRunPage client={api} agentId="meridian" runId="run-2" identityKey="owner" />
      </MemoryRouter>,
    );

    await mastSettled();
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    expect(screen.getByText(/Deterministic execution receipt unavailable/)).toBeInTheDocument();
    expect(screen.queryByText(/CURRENT-DRIFT|CURRENT-PLAN/)).not.toBeInTheDocument();
    expect(api.getAgentWorkflowProfile).not.toHaveBeenCalled();
  });

  it("renders only the frozen Skill activation receipt for an agentic Run", async () => {
    const user = userEvent.setup();
    const api = client({
      getAgentRun: vi.fn(async () => ({
        ...run,
        strategy: "agentic" as const,
        execution_profile_receipt: null,
        skill_activation_receipt: {
          activation_receipt_id: "activation-frozen",
          run_id: "run-2",
          attempt_id: "attempt-skill-frozen",
          agent_id: "meridian",
          agent_version: 3,
          surface_key: "run:run-2:attempt:attempt-skill-frozen:skills",
          pins: [{ skill_id: "frozen-hardship-skill", version: 4, content_hash: "7".repeat(64) }],
          index_hash: "8".repeat(64),
          surface_hash: "9".repeat(64),
          receipt_hash: "a".repeat(64),
          created_at: "2026-08-23T00:00:01Z",
        },
      })),
    });
    render(
      <MemoryRouter>
        <AgentRunPage client={api} agentId="meridian" runId="run-2" identityKey="owner" />
      </MemoryRouter>,
    );

    await mastSettled();
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    expect(
      screen.getByRole("region", { name: "Frozen Skill activation receipt" }),
    ).toHaveTextContent("frozen-hardship-skill v4");
    expect(screen.queryByText(/CURRENT-DRIFT|CURRENT-PLAN/)).not.toBeInTheDocument();
    expect(api.getAgentWorkflowProfile).not.toHaveBeenCalled();
  });

  it("renders model-change continuity truth and requested versus served per step", async () => {
    const user = userEvent.setup();
    const listAgentRunEvents = vi.fn<AgentsClient["listAgentRunEvents"]>(async () => ({
      items: [
        {
          event_id: "served-2",
          run_id: "run-2",
          attempt_id: "attempt-2",
          sequence: 2,
          event_kind: "model.served",
          event_status: "served",
          parent_event_id: null,
          step_id: "synthesis",
          metadata: [
            { key: "requested_model", value: "private-b" },
            { key: "served_model", value: "private-b-runtime" },
            { key: "served_lane", value: "private-vllm" },
          ],
          detail_hash: "2".repeat(64),
          created_at: run.updated_at,
        },
        {
          event_id: "change-1",
          run_id: "run-2",
          attempt_id: "attempt-2",
          sequence: 1,
          event_kind: "model.changed",
          event_status: "changed",
          parent_event_id: null,
          step_id: null,
          metadata: [
            { key: "safe_boundary", value: "between-runs" },
            { key: "before_requested_model", value: "frontier-a" },
            { key: "before_served_model", value: "frontier-a-runtime" },
            { key: "after_requested_model", value: "private-b" },
            { key: "after_authorized_model", value: "private-b" },
            { key: "context_snapshot_hash", value: "7".repeat(64) },
            { key: "context_fit", value: "fits" },
            { key: "compaction", value: "not_required" },
            { key: "cache_impact", value: "reset_expected" },
          ],
          detail_hash: "1".repeat(64),
          created_at: run.created_at,
        },
      ],
      next_cursor: null,
    }));
    render(
      <MemoryRouter>
        <AgentRunPage
          client={client({ listAgentRunEvents })}
          agentId="meridian"
          runId="run-2"
          identityKey="owner-a"
        />
      </MemoryRouter>,
    );
    await mastSettled();
    await user.click(screen.getByRole("tab", { name: "Timeline" }));
    const timelineItems = within(
      within(screen.getByRole("tabpanel", { name: "Timeline" })).getByRole("list"),
    ).getAllByRole("listitem");
    expect(timelineItems[0]).toHaveTextContent("Model changed");
    expect(timelineItems[1]).toHaveTextContent("Model served");
    expect(
      screen.getByRole("region", { name: "Model continuity receipt change-1" }),
    ).toHaveTextContent("Prompt-cache impactreset_expected");
    const step = screen.getByLabelText("Model step synthesis");
    expect(step).toHaveTextContent("Requested modelprivate-b");
    expect(step).toHaveTextContent("Served modelprivate-b-runtime");
  });

  it("renders only the server-projected safe agentic plan", async () => {
    const user = userEvent.setup();
    const api = client({
      getAgentRun: vi.fn(async () => ({
        ...run,
        strategy: "agentic" as const,
        agentic_plan: [
          { ordinal: 0, tool: "knowledge.search", step_intent: "Find current source facts" },
          { ordinal: 1, tool: "graph.path", step_intent: "Resolve the governed relationship" },
        ],
      })),
      listAgentRunEvents: vi.fn<AgentsClient["listAgentRunEvents"]>(async () => ({
        items: [
          {
            event_id: "unsafe-plan-looking-event",
            run_id: "run-2",
            attempt_id: "attempt-2",
            sequence: 1,
            event_kind: "event.unavailable",
            event_status: "running",
            parent_event_id: null,
            step_id: "PRIVATE-EVENT-TEXT",
            metadata: [],
            detail_hash: "f".repeat(64),
            created_at: run.created_at,
          },
        ],
        next_cursor: null,
      })),
    });
    render(
      <MemoryRouter>
        <AgentRunPage client={api} agentId="meridian" runId="run-2" identityKey="owner" />
      </MemoryRouter>,
    );

    await mastSettled();
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    expect(screen.getByText(/knowledge\.search — Find current source facts/)).toBeInTheDocument();
    expect(screen.getByText(/graph\.path — Resolve the governed relationship/)).toBeInTheDocument();
    expect(screen.queryByText(/PRIVATE-EVENT-TEXT/)).not.toBeInTheDocument();
  });

  it("never synthesizes an agentic plan from Run events", async () => {
    const user = userEvent.setup();
    const api = client({
      getAgentRun: vi.fn(async () => ({ ...run, strategy: "agentic" as const })),
      listAgentRunEvents: vi.fn<AgentsClient["listAgentRunEvents"]>(async () => ({
        items: [
          {
            event_id: "event-plan-lookalike",
            run_id: "run-2",
            attempt_id: "attempt-2",
            sequence: 1,
            event_kind: "event.unavailable",
            event_status: "running",
            parent_event_id: null,
            step_id: "PRIVATE-EVENT-PLAN",
            metadata: [],
            detail_hash: "e".repeat(64),
            created_at: run.created_at,
          },
        ],
        next_cursor: null,
      })),
    });
    render(
      <MemoryRouter>
        <AgentRunPage client={api} agentId="meridian" runId="run-2" identityKey="owner" />
      </MemoryRouter>,
    );

    await mastSettled();
    await user.click(screen.getByRole("tab", { name: "Timeline" }));
    expect(await screen.findByText("PRIVATE-EVENT-PLAN")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    expect(screen.getByText(/Model-selected plan unavailable/)).toBeInTheDocument();
    expect(screen.queryByText("PRIVATE-EVENT-PLAN")).not.toBeInTheDocument();
  });

  it("clears continuity receipts immediately when the principal/client changes", async () => {
    const user = userEvent.setup();
    const oldClient = client({
      listAgentRunEvents: vi.fn(async () => ({
        items: [
          {
            event_id: "old-owner-change",
            run_id: "run-2",
            attempt_id: "attempt-2",
            sequence: 1,
            event_kind: "model.changed" as const,
            event_status: "changed" as const,
            parent_event_id: null,
            step_id: null,
            metadata: [{ key: "actor" as const, value: "OLD-OWNER-SENTINEL" }],
            detail_hash: "1".repeat(64),
            created_at: run.created_at,
          },
        ],
        next_cursor: null,
      })),
    });
    let resolveNew!: (value: Awaited<ReturnType<AgentsClient["listAgentRunEvents"]>>) => void;
    const newClient = client({
      getConversation: vi.fn(async () => ({
        conversation_id: "conversation-1",
        agent_id: "meridian",
        title: "New owner conversation",
        created_at: run.created_at,
        updated_at: run.updated_at,
        last_sequence: 2,
      })),
      listAgentRunEvents: vi.fn<AgentsClient["listAgentRunEvents"]>(
        () =>
          new Promise<Awaited<ReturnType<AgentsClient["listAgentRunEvents"]>>>(
            (resolve) => (resolveNew = resolve),
          ),
      ),
    });
    const view = render(
      <MemoryRouter>
        <AgentRunPage client={oldClient} agentId="meridian" runId="run-2" identityKey="owner-a" />
      </MemoryRouter>,
    );
    await mastSettled();
    await user.click(screen.getByRole("tab", { name: "Timeline" }));
    expect(await screen.findAllByText("OLD-OWNER-SENTINEL")).toHaveLength(2);

    view.rerender(
      <MemoryRouter>
        <AgentRunPage client={newClient} agentId="meridian" runId="run-2" identityKey="owner-b" />
      </MemoryRouter>,
    );
    expect(await mastSettled("New owner conversation")).toBeInTheDocument();
    expect(screen.queryByText("OLD-OWNER-SENTINEL")).toBeNull();
    resolveNew({ items: [], next_cursor: null });
  });

  it("keeps retry lineage and opens the governed evidence drawer", async () => {
    const user = userEvent.setup();
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:evidence");
    const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const api = client();
    render(
      <MemoryRouter>
        <AgentRunPage client={api} agentId="meridian" runId="run-2" identityKey="owner" />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("link", { name: "Predecessor Run" })).toHaveAttribute(
      "href",
      "/agents/meridian/runs/run-1",
    );
    // AND NO "Open run record" HERE. This page IS the run record — the drawer
    // opened two lines below is the thing that exit exists to reach — so the
    // link would point at the screen the reader is already on. It is offered by
    // the DOCKED frame only.
    expect(screen.queryByRole("link", { name: "Open run record" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Evidence" }));
    expect(await screen.findByRole("dialog", { name: "evidence drawer" })).toHaveTextContent(
      "Claim mapping complete",
    );
    const claims = screen.getAllByRole("button", { name: "Same claim" });
    await user.click(claims[1]!);
    expect(claims[0]).toHaveAttribute("aria-pressed", "false");
    expect(claims[1]).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Evidence withheld — access changed").closest("li")).toHaveAttribute(
      "data-focused",
      "true",
    );
    expect(screen.queryByText("entity-secret")).toBeNull();
    expect(screen.queryByText(/PRIVATE-WITHHELD-BYTES/)).toBeNull();
    expect(screen.getByRole("link", { name: "Open in Graph" })).toHaveAttribute(
      "href",
      "/knowledge/graph?mode=graph&agent=meridian&run=run-2&evidence=ev-a",
    );
    await user.click(screen.getByRole("button", { name: "Export governed JSON" }));
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:evidence");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "evidence drawer" })).toBeNull();
    expect(screen.getByRole("button", { name: "Evidence" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Retry as new Run" }));
    expect(api.retryAgentRun).toHaveBeenCalledWith(
      "meridian",
      "run-2",
      expect.objectContaining({ expected_version: 4 }),
      expect.any(String),
    );
  });

  it("CR-B4: a paused Run draws the exact Output sentence and an em-dash Verdict, never a computed substitute", async () => {
    const pausedRun = {
      ...run,
      run_id: "run-4",
      sequence: 4,
      predecessor_run_id: null,
      retry_ordinal: 0,
      display_label: "post renewal note",
      strategy: "agentic" as const,
      status: "waiting_approval" as const,
      output_verdict: null,
      output_preview: null,
      approval_id: "approval-9d44",
      agentic_plan: [
        { ordinal: 0, tool: "read.crm.account_lookup", step_intent: "account context" },
      ],
      claims: [],
      claim_mapping_state: "mapping_pending" as const,
      execution_profile_receipt: null,
      skill_activation_receipt: null,
    };
    const api = client({
      getAgentRun: vi.fn(async () => pausedRun),
      listConversationRuns: vi.fn(async () => ({ items: [pausedRun], next_cursor: null })),
      listPendingApprovals: vi.fn(async () => ({
        approvals: [
          {
            request_id: "approval-9d44",
            action: "write.internal.crm.note",
            tier: 2,
            principal_id: "user:owner",
            requested_at: "2026-08-23T12:06:00Z",
            sla_deadline: "2026-08-23T16:00:00Z",
            expires_at: "2026-08-23T20:00:00Z",
            escalated: false,
          },
        ],
      })),
    });
    render(
      <MemoryRouter>
        <AgentRunPage client={api} agentId="meridian" runId="run-4" identityKey="owner" />
      </MemoryRouter>,
    );
    await mastSettled("Restricted operations review", 4);
    const summary = screen.getByRole("tabpanel", { name: "Summary" });

    // The Output slot: the EXACT sentence the design's hard rule fixes --
    // never "unavailable", never a 0, never a plausible sample.
    expect(summary).toHaveTextContent(
      "No safe output preview is available while the Run is paused.",
    );
    // The receipt's Verdict fact: an em dash plus "pending" -- never "No".
    expect(summary).toHaveTextContent("Verdict · — pending");
    // The gate card: the approval's own reference and decision-due line, as
    // served -- request id, requested-at and sla_deadline, none invented. The
    // gate's own display fetch is a second, independent effect, so this one
    // assertion waits for it rather than reading a stale first paint.
    expect(
      await within(summary).findByRole("link", { name: "Review in Approvals" }),
    ).toHaveAttribute("href", "/approvals?request=approval-9d44");
    expect(summary).toHaveTextContent(
      "approval-9d44 · requested 2026-08-23 12:06:00 · decision due 2026-08-23 16:00:00",
    );
  });

  // THE RUN-LIST RAIL IS DELETED (the workspace sidebar is the roster now), so
  // the lineage no longer has a rail row to sit in. The FACT it carried is
  // unchanged and still rendered: `runLineageSuffix` composes it from the
  // sibling list the Conversation already served, and it now closes the turn's
  // own question meta ("Run 3 · <time> · retry of Run 2"). Both ends of the
  // served graph are still read, so both directions are pinned below.
  const lineage = (() => {
    const run1 = {
      ...run,
      run_id: "run-1",
      sequence: 1,
      predecessor_run_id: null,
      status: "completed" as const,
    };
    const run2 = {
      ...run,
      run_id: "run-2",
      sequence: 2,
      predecessor_run_id: null,
      status: "failed" as const,
    };
    const run3 = {
      ...run,
      run_id: "run-3",
      sequence: 3,
      predecessor_run_id: "run-2",
      status: "completed" as const,
    };
    return { run1, run2, run3, siblings: [run1, run2, run3] };
  })();

  it("CR-B4: a retry's turn names its predecessor exactly as the sibling list serves it", async () => {
    const listConversationRuns = vi.fn(async () => ({
      items: lineage.siblings,
      next_cursor: null,
    }));
    render(
      <MemoryRouter>
        <AgentRunPage
          client={client({ getAgentRun: vi.fn(async () => lineage.run3), listConversationRuns })}
          agentId="meridian"
          runId="run-3"
          identityKey="owner"
        />
      </MemoryRouter>,
    );
    await mastSettled("Restricted operations review", 3);
    const summary = screen.getByRole("tabpanel", { name: "Summary" });
    // The sibling fetch is a second, independent effect, so this waits for it
    // rather than reading the pre-lineage first paint.
    expect(await within(summary).findByText(/· retry of Run 2$/)).toBeInTheDocument();
    // ONE SERVED GRAPH, ONE FETCH. The lineage is read out of the list the
    // Conversation already served; nothing goes back for a second answer.
    expect(listConversationRuns).toHaveBeenCalledTimes(1);
    expect(listConversationRuns).toHaveBeenCalledWith("meridian", "conversation-1");
  });

  it("CR-B4: the predecessor's own turn names the retry back, from the same served list", async () => {
    render(
      <MemoryRouter>
        <AgentRunPage
          client={client({
            getAgentRun: vi.fn(async () => lineage.run2),
            listConversationRuns: vi.fn(async () => ({
              items: lineage.siblings,
              next_cursor: null,
            })),
          })}
          agentId="meridian"
          runId="run-2"
          identityKey="owner"
        />
      </MemoryRouter>,
    );
    await mastSettled();
    const summary = screen.getByRole("tabpanel", { name: "Summary" });
    expect(await within(summary).findByText(/· retried as Run 3$/)).toBeInTheDocument();
  });
});

/** THE RECEIPT, READ RATHER THAN DUMPED.
 *
 * Every assertion here was proved red by reverting exactly the change it covers
 * (see the branch's report): the chips against the 64-hex text that used to be
 * painted at the reader, the words against the raw wire enums, the stamp
 * against the raw ISO instant. */
describe("PRE-013 Run receipt legibility", () => {
  function timelineOf(events: AgentRunEventWire[]) {
    return client({
      listAgentRunEvents: vi.fn<AgentsClient["listAgentRunEvents"]>(async () => ({
        items: events,
        next_cursor: null,
      })),
    });
  }

  async function openTimeline(api: AgentsClient) {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <AgentRunPage client={api} agentId="meridian" runId="run-2" identityKey="owner" />
      </MemoryRouter>,
    );
    await mastSettled();
    await user.click(screen.getByRole("tab", { name: "Timeline" }));
    const rows = within(
      within(screen.getByRole("tabpanel", { name: "Timeline" })).getByRole("list"),
    ).getAllByRole("listitem");
    // `noUncheckedIndexedAccess` is on for this package, and a row that is not
    // there must fail as a MISSING ROW rather than as an `undefined` sliding
    // into a query that then reports something else.
    return (index: number): HTMLElement => {
      const row = rows[index];
      if (row === undefined) throw new Error(`No timeline row at index ${index}`);
      return row;
    };
  }

  const claimed: AgentRunEventWire = {
    event_id: "claimed-1",
    run_id: "run-2",
    attempt_id: "attempt-2",
    sequence: 1,
    event_kind: "run.execution.claimed",
    event_status: "running",
    parent_event_id: null,
    step_id: "run-execution",
    metadata: [],
    detail_hash: "4".repeat(64),
    created_at: "2026-08-23T00:00:00Z",
  };

  it("reads the frozen deterministic receipt's digests as chips, never 64 characters of hex", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <AgentRunPage client={client()} agentId="meridian" runId="run-2" identityKey="owner" />
      </MemoryRouter>,
    );
    await mastSettled();
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    const plan = within(screen.getByRole("tabpanel", { name: "Plan" }));
    // Eight characters is what a person reads; sixty-four is what a person
    // pastes — and the full value is never lost (§2.7, HashChip.tsx).
    expect(plan.getByText("cccccccc")).toHaveAttribute("title", "c".repeat(64));
    expect(plan.queryByText("c".repeat(64))).toBeNull();
    expect(plan.queryByText("f".repeat(64))).toBeNull();
    expect(plan.getByRole("button", { name: "Copy execution profile hash" })).toBeInTheDocument();
    expect(plan.getByRole("button", { name: "Copy execution receipt hash" })).toBeInTheDocument();
    expect(plan.getByRole("button", { name: "Copy attempt id" })).toBeInTheDocument();
  });

  it("reads the frozen Skill activation receipt's digests as chips, its pins included", async () => {
    const user = userEvent.setup();
    const api = client({
      getAgentRun: vi.fn(async () => ({
        ...run,
        strategy: "agentic" as const,
        execution_profile_receipt: null,
        skill_activation_receipt: {
          activation_receipt_id: "activation-frozen",
          run_id: "run-2",
          attempt_id: "attempt-skill-frozen",
          agent_id: "meridian",
          agent_version: 3,
          surface_key: "run:run-2:attempt:attempt-skill-frozen:skills",
          pins: [{ skill_id: "frozen-hardship-skill", version: 4, content_hash: "7".repeat(64) }],
          index_hash: "8".repeat(64),
          surface_hash: "9".repeat(64),
          receipt_hash: "6".repeat(64),
          created_at: "2026-08-23T00:00:01Z",
        },
      })),
    });
    render(
      <MemoryRouter>
        <AgentRunPage client={api} agentId="meridian" runId="run-2" identityKey="owner" />
      </MemoryRouter>,
    );
    await mastSettled();
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    const receipt = within(screen.getByRole("region", { name: "Frozen Skill activation receipt" }));
    expect(receipt.getByText("77777777")).toHaveAttribute("title", "7".repeat(64));
    expect(receipt.queryByText("7".repeat(64))).toBeNull();
    expect(receipt.queryByText("8".repeat(64))).toBeNull();
    expect(receipt.queryByText("6".repeat(64))).toBeNull();
    expect(
      receipt.getByRole("button", { name: "Copy Skill activation receipt hash" }),
    ).toBeInTheDocument();
    expect(receipt.getByRole("button", { name: "Copy Skill index hash" })).toBeInTheDocument();
    // The pin's chip names WHICH Skill it belongs to: a row of identical "copy
    // hash" buttons is not an affordance a reader can aim.
    expect(
      receipt.getByRole("button", { name: "Copy content hash for Skill frozen-hardship-skill" }),
    ).toBeInTheDocument();
  });

  it("leads each timeline row with words, keeps the wire code in title, and dates it as an instant", async () => {
    const rows = await openTimeline(
      timelineOf([
        claimed,
        {
          ...claimed,
          event_id: "intent-2",
          sequence: 2,
          event_kind: "run.audit.intent",
          event_status: "receipt_pending",
          detail_hash: "5".repeat(64),
          created_at: "2026-08-23T00:00:02Z",
        },
      ]),
    );
    expect(rows(0)).toHaveTextContent("Execution claimed");
    expect(rows(0)).toHaveTextContent("Running");
    expect(rows(1)).toHaveTextContent("Audit receipt intent recorded");
    expect(rows(1)).toHaveTextContent("Receipt pending");
    // THE CODE IS NOT DROPPED. It is the identifier the record is filtered and
    // exported by; the row reads as words and still hands it over on hover.
    expect(within(rows(0)).getByTitle("run.execution.claimed")).toBeInTheDocument();
    expect(within(rows(0)).getByTitle("running")).toBeInTheDocument();
    expect(within(rows(1)).getByTitle("run.audit.intent")).toBeInTheDocument();
    expect(within(rows(1)).getByTitle("receipt_pending")).toBeInTheDocument();
    // The absolute local instant, not the wire's ISO string. Asserted by shape
    // so the test does not depend on the runner's timezone.
    expect(within(rows(0)).getByText(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)).toBeInTheDocument();
    expect(within(rows(0)).queryByText("2026-08-23T00:00:00Z")).toBeNull();
    expect(
      within(rows(0))
        .getByText(/^\d{4}-\d{2}-\d{2}/)
        .closest("time"),
    ).toHaveAttribute("datetime", "2026-08-23T00:00:00Z");
  });

  it("degrades an untaught kind and status to their own words — never to nothing", async () => {
    const rows = await openTimeline(
      timelineOf([
        {
          ...claimed,
          // Neither value is in the wire's declared unions. The server's
          // vocabulary is not this console's to close, and a governance row
          // that renders as a blank leading word is the failure this guards.
          event_kind: "some.new.kind" as AgentRunEventWire["event_kind"],
          // `constructor` is the shape that turns a bare map index into
          // `Object`'s constructor FUNCTION and empties the row.
          event_status: "constructor" as AgentRunEventWire["event_status"],
        },
      ]),
    );
    expect(rows(0)).toHaveTextContent("Some new kind");
    expect(rows(0)).toHaveTextContent("Constructor");
    expect(within(rows(0)).getByTitle("some.new.kind")).toBeInTheDocument();
    expect(within(rows(0)).getByTitle("constructor")).toBeInTheDocument();
  });

  it("chips a metadata digest, says a boolean in a word, and leaves every other value byte-exact", async () => {
    const rows = await openTimeline(
      timelineOf([
        {
          ...claimed,
          event_kind: "evidence.produced",
          event_status: "preterminal",
          metadata: [
            { key: "authorization_receipt_hash", value: "3".repeat(64) },
            { key: "safe_boundary", value: true },
            { key: "contributor_count", value: 3 },
            { key: "served_model", value: "private-b-runtime" },
          ],
        },
      ]),
    );
    const detail = within(rows(0));
    expect(rows(0)).toHaveTextContent("Recorded mid-Run");
    expect(detail.getByText("33333333")).toHaveAttribute("title", "3".repeat(64));
    expect(detail.queryByText("3".repeat(64))).toBeNull();
    expect(
      detail.getByRole("button", { name: "Copy authorization receipt hash" }),
    ).toBeInTheDocument();
    // The key side asks the question; `true` is not an answer a reader reads.
    expect(rows(0)).toHaveTextContent("safe boundaryYes");
    // AND THE REST IS THE RECORD. A served model name is compared against
    // another system's logs, so its bytes are not this page's to re-case.
    expect(detail.getByText("private-b-runtime")).toBeInTheDocument();
    expect(rows(0)).toHaveTextContent("contributor count3");
    // Every event's own detail hash is a chip too.
    expect(detail.getByRole("button", { name: "Copy event detail hash" })).toBeInTheDocument();
    expect(detail.queryByText("4".repeat(64))).toBeNull();
  });

  // 🔴 THE PLAN ROWS PRINTED TWO NUMBERS. Each <li> renders the SERVER's own
  // `ordinal` in its text, and a default <ol> adds a browser marker on top, so a row
  // read "1. 1. verified.invoke". Nothing reset it: the only bare-`ol` CSS rule in
  // the package is `.ne-documents__graph ol`, a different subtree.
  //
  // The SERVER's number is the one that must survive — the agentic list is
  // `visibleAgenticPlan`, i.e. FILTERED, so a positional marker would disagree with
  // the plan the run actually executed.
  //
  // ⚠️ jsdom does not render list markers, so this CANNOT assert the visual result.
  // It pins the two halves that produce it. The invariant is deliberately NARROW:
  // only a list whose rows PRINT an ordinal needs the marker suppressed. The Skill
  // pins list below is a bare <ol> on purpose — its rows carry skill names, not
  // numbers, so the browser's marker is the only one and is correct.
  it("suppresses the browser's marker on exactly the lists that print an ordinal", () => {
    // The source is imported as TEXT by the bundler (`?raw`), so this touches no
    // filesystem. Two earlier attempts were worse: a cwd-relative `readFile` passed
    // here and would break wherever the runner starts elsewhere, and `import.meta.url`
    // is not a file: URL under Vite's transform.
    //
    // ⚠️ This asserts ONLY that the class is applied. That the RULE exists is covered
    // by `test/unstyled-class-guardian.test.ts`, which already fails with
    // `"ne-run-plan (agents/AgentRunPage.tsx)"` when the rule is deleted -- verified by
    // deleting it. A second assertion here would be a worse duplicate of a general
    // guardian the package already owns.
    // Strip JSX comments so the prose above a list cannot satisfy or trip this.
    const code = runPageSource.replace(/\{\/\*[\s\S]*?\*\/\}/g, "");
    const lists = [...code.matchAll(/<ol([^>]*)>([\s\S]*?)<\/ol>/g)];
    expect(lists.length).toBeGreaterThan(0);
    for (const match of lists) {
      // Capture groups are `string | undefined` under strict tsc; both groups here
      // are non-optional in the pattern, so an empty string is the honest default
      // rather than a non-null assertion that would hide a real regex change.
      const attrs = match[1] ?? "";
      const body = match[2] ?? "";
      if (/\.ordinal\b/.test(body)) {
        expect(attrs).toMatch(/className="[^"]*\bne-run-plan\b/);
      }
    }
  });
});
