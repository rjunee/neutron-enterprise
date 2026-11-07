// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE AGENT WORKSPACE — ROUND 4.
//
// Every assertion here is paired: the thing that must be present, and the thing
// that must be ABSENT for the first one to mean anything. "The receipt opened"
// passes just as happily over a receipt that opened by NAVIGATING to another
// page, which is the exact defect this redesign exists to fix; so the pair is
// "the receipt opened" AND "the thread is still on screen". Each pair below was
// mutation-checked: break the mechanism, watch the named test go RED, restore
// it, watch it go GREEN.

import { useEffect } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { AgentWorkspace, capabilityCount, capsNote, viewFromSearch } from "./AgentWorkspace";
import { AgentRunPage } from "./AgentRunPage";
import { RUN_RECEIPT_LABEL, runOffersRetry } from "./RunReceiptPane";
import type {
  AgentCapabilityPreflight,
  AgentDetailView,
  AgentPage,
  AgentsClient,
  AgentWorkflowProfile,
  ConversationSummary,
  RunDetail,
  RunPage,
} from "./types";

const ASK_AGENT = "agt_01k3meridiananalyst000000000";
const DET_AGENT = "agt_01k3hardshipreview0000000000";

const agentic: AgentDetailView = {
  agent_id: ASK_AGENT,
  version: 4,
  label: "Meridian Analyst",
  description: "Answers Meridian servicing questions.",
  owner_label: "Meridian Servicing Operations",
  lifecycle: "active",
  allowed_strategies: ["agentic"],
  default_strategy: "agentic",
  model_policy_id: "agent.meridian-analyst.v2",
  tool_grants: ["read.knowledge.search", "draft.email.create"],
  knowledge_sources: [{ source_id: "meridian-servicing", tiers: ["t1", "t2"] }],
  skills: [{ skill_id: "meridian-hardship-draft", version: 1, content_hash: "a".repeat(64) }],
  budgets: {
    max_steps: 12,
    max_tool_calls: 10,
    max_input_tokens: 24_000,
    max_output_tokens: 4_000,
  },
  enforced_caps: {
    max_tool_calls: 10,
    max_model_requests: 11,
    wall_clock_seconds: 120,
    max_total_tokens: 28_000,
    max_parked_writes: 1,
  },
  workflow: null,
  available: true,
  availability_reasons: [],
  config_hash: "d".repeat(64),
};

const deterministic: AgentDetailView = {
  ...agentic,
  agent_id: DET_AGENT,
  version: 5,
  label: "Meridian Hardship Review",
  description: "Runs the fixed Meridian B1 disaster-hardship population review.",
  allowed_strategies: ["deterministic"],
  default_strategy: "deterministic",
  model_policy_id: "agent.meridian-hardship-review.v3",
  tool_grants: ["read.kg.customers_in_counties"],
  skills: [],
  // The registry's REAL numbers, `max_output_tokens: 1` included. It renders as
  // 1 (see the "verbatim" test below); a console that tidied it would be hiding
  // a registry defect behind a number that reads better.
  budgets: { max_steps: 1, max_tool_calls: 100, max_input_tokens: 8_210, max_output_tokens: 1 },
  // No bounded agentic loop runs for a workflow Agent, so the server serves no
  // enforced ceilings for it — not a copy of the conversational Agent's.
  enforced_caps: null,
  workflow: {
    profile_id: "meridian-b1-disaster-hardship",
    version: 1,
    config_hash: "e".repeat(64),
    max_subjects: 5,
    frozen_inputs: {
      counties: ["Columbia", "Jefferson", "Richmond"],
      payment_window_start: "2026-08-18",
      payment_window_end: "2026-09-01",
    },
  },
};

function preflightFor(agent: AgentDetailView): AgentCapabilityPreflight {
  return {
    agent_id: agent.agent_id,
    version: agent.version,
    definition_hash: agent.config_hash,
    available: true,
    availability_reasons: [],
    capabilities: {
      agent_version: agent.version,
      agent_definition_hash: agent.config_hash,
      capability_hash: "c".repeat(64),
      tools: [
        {
          name: agent.tool_grants[0]!,
          label: "Knowledge search",
          read_write_class: "read",
          declared_tier: 1,
          effective_tier: 2,
          connection_identity_label: "Meridian read identity",
          health: "healthy",
          description: "Search the governed knowledge base",
          arguments: [
            {
              name: "query",
              type_label: "string",
              required: true,
              description: "Free-text question searched over the governed knowledge base",
              enum: null,
              pattern: null,
              max_length: null,
              list_max: null,
            },
          ],
          answer_columns: null,
        },
      ],
      connectors: [],
    },
    skills: [],
    model: {
      task: agent.model_policy_id,
      allowed_strategies: agent.allowed_strategies,
      default_strategy: agent.default_strategy,
      max_steps: agent.budgets.max_steps,
      max_tool_calls: agent.budgets.max_tool_calls,
      max_input_tokens: agent.budgets.max_input_tokens,
      max_output_tokens: agent.budgets.max_output_tokens,
      candidates: [
        {
          model: "private-primary",
          provider: "vllm",
          lane: "private-vllm",
          evidence: "serving-ready",
          observed_at: "2026-08-27T00:00:00Z",
        },
      ],
      default_model: "private-primary",
    },
  };
}

const profile: AgentWorkflowProfile = {
  agent_id: DET_AGENT,
  agent_version: deterministic.version,
  agent_config_hash: deterministic.config_hash,
  strategy: "deterministic",
  workflow_id: "meridian-b1-disaster-hardship",
  workflow_version: 1,
  workflow_config_hash: "e".repeat(64),
  input_schema: {
    type: "object",
    required: ["max_subjects"],
    additionalProperties: false,
    properties: {
      max_subjects: { type: "integer", minimum: 1, maximum: 5, title: "Max Subjects" },
    },
  },
  fixed_inputs: {
    counties: ["Columbia", "Jefferson", "Richmond"],
    payment_window_start: "2026-08-18",
    payment_window_end: "2026-09-01",
  },
  fixed_plan: [
    {
      ordinal: 1,
      step_id: "customers",
      kind: "governed-tool",
      action: "List customers in the frozen counties",
      model_role: null,
    },
  ],
};

const conversation: ConversationSummary = {
  conversation_id: "conversation-new",
  agent_id: DET_AGENT,
  title: "Hardship review",
  created_at: "2026-08-27T00:00:00Z",
  updated_at: "2026-08-27T00:00:00Z",
  last_sequence: 0,
};

const emptyRuns: RunPage = { items: [], next_cursor: null };

const RUN_ID = "run_01k3wr8h2v6d";

function runDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: RUN_ID,
    conversation_id: "conversation-new",
    agent_id: DET_AGENT,
    agent_version: deterministic.version,
    sequence: 3,
    version: 2,
    predecessor_run_id: null,
    root_run_id: RUN_ID,
    retry_ordinal: 0,
    display_label: "Run 3 · Deterministic · 09:31",
    strategy: "deterministic",
    status: "completed",
    output_verdict: "grounded",
    model_policy_id: deterministic.model_policy_id,
    model_request_mode: "auto",
    context_snapshot_hash: null,
    requested_model: null,
    requested_model_lane: null,
    served_model: "private-primary",
    route_position: "unavailable",
    fallback_from: null,
    output_preview: "Actionable hardship population — 3 subjects selected of a maximum 5.",
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
    ...overrides,
  } as RunDetail;
}

const conversations = {
  items: [
    {
      conversation_id: "conv_01",
      agent_id: ASK_AGENT,
      title: "Conversation · 2026-08-27 09:12",
      created_at: "2026-08-27T09:12:00Z",
      updated_at: "2026-08-27T09:24:00Z",
      last_sequence: 3,
    },
    {
      conversation_id: "conv_02",
      agent_id: ASK_AGENT,
      title: "Conversation · 2026-08-25 14:02",
      created_at: "2026-08-25T14:02:00Z",
      updated_at: "2026-08-25T14:09:00Z",
      last_sequence: 1,
    },
  ],
  next_cursor: null,
};

function client(agent: AgentDetailView, overrides: Partial<AgentsClient> = {}): AgentsClient {
  return {
    listAgents: vi.fn(async (): Promise<AgentPage> => ({
      items: [agentic, deterministic],
      next_cursor: null,
    })),
    getAgent: vi.fn(async () => agent),
    listAgentConversations: vi.fn(async () => ({ items: [], next_cursor: null })),
    getConversation: vi.fn(async () => conversation),
    createAgentConversation: vi.fn(async () => conversation),
    listConversationTurns: vi.fn(async () => ({ items: [], next_cursor: null })),
    listConversationRuns: vi.fn(async () => emptyRuns),
    listAgentRuns: vi.fn(async () => emptyRuns),
    getAgentPreflight: vi.fn(async () => preflightFor(agent)),
    getAgentWorkflowProfile: vi.fn(async () =>
      agent.default_strategy === "deterministic" ? profile : Promise.reject({ status: 404 }),
    ),
    listAgentRunUpdates: vi.fn<AgentsClient["listAgentRunUpdates"]>(async () => ({
      view: "recent",
      window_start: "2026-08-27T00:00:00Z",
      window_end: "2026-08-28T00:00:00Z",
      items: [],
      next_cursor: null,
    })),
    startAgentRun: vi.fn<AgentsClient["startAgentRun"]>(async () => ({
      action: "start",
      run_id: "run-1",
      attempt_id: "attempt-1",
      version: 1,
      status: "submitted",
      enqueue_id: "enqueue-1",
      replayed: false,
    })),
    getAgentRun: vi.fn(async () => runDetail()),
    listAgentRunEvents: vi.fn(async () => ({ items: [], next_cursor: null })),
    cancelAgentRun: vi.fn(),
    retryAgentRun: vi.fn(),
    ...overrides,
  } as AgentsClient;
}

/** The address bar, so a test can prove a click did NOT navigate. Written in an
 * effect rather than during render: a render-phase write is a side effect, and
 * the rule that forbids it is the same one that keeps the product honest. */
let lastPath = "";
function PathProbe() {
  const location = useLocation();
  const path = `${location.pathname}${location.search}`;
  useEffect(() => {
    lastPath = path;
  }, [path]);
  return null;
}

function mount(
  agent: AgentDetailView,
  {
    overrides = {},
    entry = `/agents/${agent.agent_id}`,
    renderChat,
  }: {
    overrides?: Partial<AgentsClient>;
    entry?: string;
    renderChat?: React.ComponentProps<typeof AgentWorkspace>["renderChat"];
  } = {},
) {
  const c = client(agent, overrides);
  render(
    <MemoryRouter initialEntries={[entry]}>
      <PathProbe />
      <Routes>
        <Route
          path="/agents/:id"
          element={
            <AgentWorkspace
              client={c}
              agentId={agent.agent_id}
              identityKey="principal-a"
              platformConfigHash={"f".repeat(64)}
              configurationState="ready"
              {...(renderChat ? { renderChat } : {})}
            />
          }
        />
      </Routes>
    </MemoryRouter>,
  );
  return c;
}

// ===========================================================================
// PAIR 1 — the receipt opens IN PLACE, and the deep link renders the SAME body.
//
// The owner: "its so confusing opening a run and it takes me to some other
// page." Half of this pair is that the receipt appeared; the other half is that
// the thread it came from is still there and the address bar never left. A test
// that only asserts the first passes over the exact defect being fixed.
// ===========================================================================

describe("the run receipt is docked, not navigated", () => {
  it("opening a run receipt keeps you on the thread", async () => {
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
      },
    });
    // The run form composer is the deterministic thread's own surface.
    await screen.findByRole("region", { name: "Start a deterministic run" });
    const before = lastPath;

    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));

    const pane = await screen.findByRole("dialog", { name: /Run 3/ });
    // The receipt is here…
    expect(within(pane).getByText("Capability set")).toBeInTheDocument();
    expect(within(pane).getByText("Evidence assurance")).toBeInTheDocument();
    // …AND the thread it came from never left.
    expect(screen.getByRole("region", { name: "Start a deterministic run" })).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: "Agent workspace" })).toBeInTheDocument();
    // The pane is a query parameter on the SAME route, not another page.
    expect(lastPath.split("?")[0]).toBe(before.split("?")[0]);
    expect(lastPath).toContain(`receipt=${RUN_ID}`);
  });

  it("the docked receipt draws the SERVED row anchors as the Run's row identity", async () => {
    const anchor = "7a".repeat(32);
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
        getAgentRunEvidence: vi.fn(async () => ({
          schema_version: 1 as const,
          agent_id: DET_AGENT,
          run_id: RUN_ID,
          run_status: "completed" as const,
          mapping_state: "complete" as const,
          mapping_message: "Claim mapping complete",
          output: {
            verdict: "verified" as const,
            state: "available" as const,
            message: "",
            display: "answer",
          },
          claims: [
            {
              claim_id: "c1",
              claim_verdict: "verified" as const,
              state: "available" as const,
              message: "Claim available",
              display: "One row",
              evidence_id: "ev-1",
              association_hash: "a".repeat(64),
              verifier_event_id: null,
              verifier_receipt_hash: null,
            },
          ],
          evidence: [
            {
              evidence_id: "ev-1",
              state: "available" as const,
              message: "Verified row",
              assurance: "direct" as const,
              locator: {
                kind: "verified_t3" as const,
                row_anchor: anchor,
                receipt_hash: "b".repeat(64),
              },
            },
          ],
          governance: {
            status: "complete" as const,
            delegated_user_label: null,
            agent_service_identity: null,
            connector_credential_owner_label: null,
            effective_scopes: [],
            policy_environment: null,
            tool_tier: null,
            tool_decision: null,
            unavailable_reasons: [],
          },
        })),
      },
    });
    await screen.findByRole("region", { name: "Start a deterministic run" });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });

    // The field is present, the short form is the served value's own first 8
    // chars, and the FULL served value rides in the title — never recomputed.
    expect(await within(pane).findByText("Row identity")).toBeInTheDocument();
    expect(within(pane).getByText(anchor.slice(0, 8))).toHaveAttribute("title", anchor);
    expect(within(pane).getByRole("button", { name: "Copy row anchor" })).toBeInTheDocument();
    // And the exit into the side-by-side carries THIS Run's served address.
    expect(within(pane).getByRole("link", { name: "Compare with another Run" })).toHaveAttribute(
      "href",
      expect.stringContaining(`a_run=${RUN_ID}`),
    );
  });

  it("a client without the evidence read draws NO row-identity field — absence, not an error", async () => {
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
      },
    });
    await screen.findByRole("region", { name: "Start a deterministic run" });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });
    expect(within(pane).getByText("Capability set")).toBeInTheDocument();
    expect(within(pane).queryByText("Row identity")).not.toBeInTheDocument();
  });

  it("says what the record is for, and states a model divergence rather than listing two names", async () => {
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        getAgentRun: vi.fn(async () =>
          runDetail({ requested_model: "frontier-best", served_model: "private-default" }),
        ),
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
      },
    });
    await screen.findByRole("region", { name: "Start a deterministic run" });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });

    // WHAT IT IS FOR, at the point of use — not a tooltip glossing "receipt".
    expect(
      within(pane).getByText(/makes an answer defensible, not merely plausible/),
    ).toBeVisible();
    // Both identifiers stay exactly as served, and the relationship is stated.
    expect(within(pane).getByText("frontier-best")).toBeInTheDocument();
    expect(within(pane).getByText("private-default")).toBeInTheDocument();
    expect(within(pane).getByText("Model routing").closest("div")).toHaveTextContent(
      "The request named a different model than the one that served.",
    );
  });

  it("states an INVOLUNTARY FALLBACK on an auto-routed run, which the divergence check cannot see", async () => {
    // The gap this closes: `modelDiverged` needs a `requested_model`, and
    // `run_store` enforces that an "auto" request has none. So for every
    // task-routed run the divergence field could never render — fallback or
    // not — and this pane showed NOTHING while the chat footer and the trace
    // view both said the lane had moved. Since NE-DEMO-NO-CPU-FALLBACK a
    // private-primary task falls back to `frontier-default`, so that silence
    // was the difference between "served on the sovereign lane" and "used the
    // frontier API" on the screen an audit entry links to.
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        getAgentRun: vi.fn(async () =>
          runDetail({
            model_request_mode: "auto",
            requested_model: null,
            requested_model_lane: null,
            served_model: "frontier-default",
            route_position: "fallback",
            fallback_from: "private-default",
          }),
        ),
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
      },
    });
    await screen.findByRole("region", { name: "Start a deterministic run" });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });

    // It names WHAT was passed over, so the reader can tell WHICH lane was lost.
    expect(within(pane).getByText("Model routing").closest("div")).toHaveTextContent(
      "private-default did not serve, and a fallback answered",
    );
  });

  it("states no routing on an auto-routed run that its PRIMARY served", async () => {
    // THE PAIRED CONTROL: without this, the assertion above would pass against
    // a pane that announced a fallback on every auto-routed run.
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        getAgentRun: vi.fn(async () =>
          runDetail({
            model_request_mode: "auto",
            requested_model: null,
            requested_model_lane: null,
            served_model: "private-default",
            route_position: "primary",
            fallback_from: null,
          }),
        ),
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
      },
    });
    await screen.findByRole("region", { name: "Start a deterministic run" });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });

    expect(within(pane).queryByText("Model routing")).not.toBeInTheDocument();
  });

  it("states no routing where the served model IS the requested one", async () => {
    // THE PAIRED HALF: the field is a claim about this Run, so it may not
    // appear on a Run that has nothing to reconcile.
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        getAgentRun: vi.fn(async () =>
          runDetail({ requested_model: "private-primary", served_model: "private-primary" }),
        ),
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
      },
    });
    await screen.findByRole("region", { name: "Start a deterministic run" });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });
    expect(within(pane).queryByText("Model routing")).toBeNull();
  });

  it("a predecessor Run re-fills the dock instead of throwing the reader onto a page", async () => {
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        getAgentRun: vi.fn(async () => runDetail({ predecessor_run_id: "run-earlier" })),
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
      },
    });
    await screen.findByRole("region", { name: "Start a deterministic run" });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    await screen.findByRole("dialog", { name: /Run 3/ });
    const before = lastPath.split("?")[0];

    // A LINK here was the same defect "Open Run" was, one pane deeper.
    await user.click(screen.getByRole("button", { name: "Predecessor Run" }));
    expect(lastPath.split("?")[0]).toBe(before);
    expect(lastPath).toContain("receipt=run-earlier");
  });

  it("the docked receipt offers the ONE exit to the run record, because the drawer is only there", async () => {
    // THE DEFECT. "How this turn ran" docks the receipt beside the thread, and
    // its exits went to the trace, the audit log, a comparison and the
    // predecessor Run. The evidence drawer — the only place "Served document
    // versions" is rendered — is mounted on `/agents/:id/runs/:runId` and
    // nowhere else, so a reader who opened the receipt to find WHICH VERSION of
    // a document was served had no click that got them there.
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        getAgentRun: vi.fn(async () => runDetail()),
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
      },
    });
    await screen.findByRole("region", { name: "Start a deterministic run" });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });

    // Built from the served ids on this receipt — the same URL the predecessor
    // exit already builds, pointed at THIS Run.
    expect(within(pane).getByRole("link", { name: "Open run record" })).toHaveAttribute(
      "href",
      `/agents/${DET_AGENT}/runs/${RUN_ID}`,
    );
  });

  it("the deep link still renders the same receipt content standalone", async () => {
    // `/agents/:id/runs/:runId` survives for the links other systems hand out —
    // an audit entry, a notification. It must render the SAME body of facts the
    // docked pane does, from the same components, or the two frames could drift
    // into two different accounts of one Run.
    const c = client(deterministic, { getAgentRun: vi.fn(async () => runDetail()) });
    render(
      <MemoryRouter initialEntries={[`/agents/${DET_AGENT}/runs/${RUN_ID}`]}>
        <AgentRunPage client={c} agentId={DET_AGENT} runId={RUN_ID} identityKey="principal-a" />
      </MemoryRouter>,
    );
    // The standalone frame titles the pane by WHAT IT IS: the page's own h1
    // already names the Run, and a pane heading repeating it said nothing new.
    const pane = await screen.findByRole("dialog", { name: RUN_RECEIPT_LABEL });
    expect(within(pane).getByText("Capability set")).toBeInTheDocument();
    expect(within(pane).getByText("Evidence assurance")).toBeInTheDocument();
    expect(within(pane).getByText("Deterministic — fixed plan")).toBeInTheDocument();
    // …and the standalone frame is a page, not a workspace: no sidebar.
    expect(screen.queryByRole("complementary", { name: /Agents and/ })).toBeNull();
  });

  it("the deep link states WHY a failed run failed, in the server's words", async () => {
    // train-4's `failure_reason` wire. Deleting RunReceiptCard was fine; losing
    // the reason with it would silently restore the defect that wire ended — a
    // failed Run reported as a generic line while the server had said exactly
    // what happened.
    const failed = runDetail({
      status: "failed",
      output_verdict: null,
      output_preview: null,
      terminal_at: null,
      failure_reason: "No current serving evidence for the requested lane.",
    });
    const c = client(deterministic, { getAgentRun: vi.fn(async () => failed) });
    render(
      <MemoryRouter initialEntries={[`/agents/${DET_AGENT}/runs/${RUN_ID}`]}>
        <AgentRunPage client={c} agentId={DET_AGENT} runId={RUN_ID} identityKey="principal-a" />
      </MemoryRouter>,
    );
    const pane = await screen.findByRole("dialog", { name: RUN_RECEIPT_LABEL });
    expect(
      within(pane).getByText("No current serving evidence for the requested lane."),
    ).toBeInTheDocument();
  });

  it("nothing in the workspace links to the run route", async () => {
    mount(deterministic, {
      overrides: {
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
      },
    });
    await screen.findByRole("region", { name: "Start a deterministic run" });
    const links = screen.getAllByRole("link");
    expect(links.some((link) => /\/runs\//.test(link.getAttribute("href") ?? ""))).toBe(false);
  });
});

// ===========================================================================
// PAIR 2 — ONE history list per agent, and empty is EMPTY.
//
// The sidebar shows exactly one list, from the canonical Conversations store —
// the same rows `POST /v1/chat/sessions` writes. The pair's other half is the
// agent with nothing in it: a list that pads an absence with a zero, a
// placeholder row or a stranded skeleton is not an empty list, it is a lie
// with the shape of data.
// ===========================================================================

describe("one history list per agent", () => {
  it("an agentic agent lists conversations, and only conversations", async () => {
    const c = mount(agentic, {
      overrides: { listAgentConversations: vi.fn(async () => conversations) },
      renderChat: () => <div>thread</div>,
    });
    const side = await screen.findByRole("complementary", { name: /Agents and conversations/ });
    expect(await within(side).findByText("Conversation · 2026-08-27 09:12")).toBeInTheDocument();
    expect(within(side).getByText("Conversation · 2026-08-25 14:02")).toBeInTheDocument();
    // ONE index, not two peers: the run index is never read for this agent.
    expect(c.listAgentConversations).toHaveBeenCalled();
    expect(c.listAgentRuns).not.toHaveBeenCalled();
    // And the tab strip carries no second history noun.
    const tabs = screen.getAllByRole("tab").map((tab) => tab.textContent);
    expect(tabs).toHaveLength(2);
    expect(tabs[0]).toContain("Conversation");
    expect(tabs.join(" ")).not.toContain("Runs");
  });

  it("a deterministic agent lists runs, and only runs", async () => {
    const c = mount(deterministic, {
      overrides: {
        listAgentRuns: vi.fn(async () => ({ items: [runDetail()], next_cursor: null })),
      },
    });
    const side = await screen.findByRole("complementary", { name: /Agents and runs/ });
    expect(await within(side).findByText("Run 3 · Deterministic · 09:31")).toBeInTheDocument();
    expect(c.listAgentRuns).toHaveBeenCalled();
    expect(c.listAgentConversations).not.toHaveBeenCalled();
    const tabs = screen.getAllByRole("tab").map((tab) => tab.textContent);
    expect(tabs).toHaveLength(2);
    expect(tabs[0]).toContain("Runs");
  });

  it("an agent with no history renders empty, not zero-padded", async () => {
    mount(agentic, {
      overrides: { listAgentConversations: vi.fn(async () => ({ items: [], next_cursor: null })) },
      renderChat: () => <div>thread</div>,
    });
    const side = await screen.findByRole("complementary", { name: /Agents and conversations/ });
    expect(await within(side).findByText(/No conversations with this agent yet/)).toBeVisible();
    // No rows, no count, no zero anywhere in the history section.
    const history = within(side).queryByRole("list", { name: "Conversations" });
    expect(history).toBeNull();
    expect(within(side).queryByText(/\b0\b/)).toBeNull();
    // …and the empty state is not a stranded skeleton either.
    expect(side.querySelectorAll(".ne-skeleton")).toHaveLength(0);
  });

  it("an empty capability grant is not rendered as a zero", () => {
    // The tab's count clause is omitted entirely rather than saying "0 skills".
    expect(capabilityCount({ ...agentic, skills: [] })).toBe("2 tools");
    expect(capabilityCount({ ...agentic, skills: [], tool_grants: [] })).toBeNull();
    expect(capabilityCount(agentic)).toBe("2 tools · 1 skill");
  });

  it("the composer footer states the ENFORCED caps and never the band's word", () => {
    // THE DEFECT. This footer read "Caps · 12 steps · 10 tool calls" while the
    // transcript band beside it read "30 steps ... 30 allowed", because the two
    // count different things under one word: the band counts recorded ACTIVITY
    // rows (tool calls, model calls, evidence and delivery decisions) and the
    // footer was quoting a manifest budget. Worse, `max_steps` is enforced as no
    // count at all — it narrows the tool-call ceiling and buys wall clock.
    //
    // So: the served, enforced numbers, and no "steps".
    expect(capsNote(agentic)).toBe("Caps · 10 tool calls · 11 model calls · 120s");
    expect(capsNote(agentic)).not.toContain("steps");
    // THE MUTATION GUARD. The footer reads `enforced_caps` and never `budgets`:
    // move the declared budgets to nonsense and the footer must not move with
    // them. Point capsNote back at `budgets` and this line goes red.
    expect(
      capsNote({
        ...agentic,
        budgets: {
          max_steps: 64,
          max_tool_calls: 99,
          max_input_tokens: 1,
          max_output_tokens: 1,
        },
      }),
    ).toBe("Caps · 10 tool calls · 11 model calls · 120s");

    // An Agent whose Runs enforce no agentic ceilings states none, rather than
    // borrowing another Agent's or printing zeroes.
    expect(capsNote(deterministic)).toBeNull();
  });
});

// ===========================================================================
// PAIR 3 — REFUSED IS NOT FAILED.
//
// A refusal is a completed outcome the product is proud of: the run finished
// and the answer is "no, with proof". Clicking again cannot undo it, so it
// offers no Retry. A failure is the ABSENCE of an outcome, and asking again is
// an ordinary next move. One test without the other proves nothing: a screen
// that offered Retry on both, or on neither, would pass half of this pair.
// ===========================================================================

describe("refused is a completed outcome; failed is an absence of one", () => {
  it("a refused run shows as refused with no Retry", async () => {
    const user = userEvent.setup();
    const refused = runDetail({ status: "completed", output_verdict: "refused" });
    mount(deterministic, {
      overrides: {
        listAgentRuns: vi.fn(async () => ({ items: [refused], next_cursor: null })),
        getAgentRun: vi.fn(async () => refused),
      },
    });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });
    expect(within(pane).getByText("Refused")).toBeInTheDocument();
    expect(within(pane).queryByRole("button", { name: /Retry/ })).toBeNull();
  });

  it("a failed run offers Retry", async () => {
    const user = userEvent.setup();
    const failed = runDetail({ status: "failed", output_verdict: null, terminal_at: null });
    mount(deterministic, {
      overrides: {
        listAgentRuns: vi.fn(async () => ({ items: [failed], next_cursor: null })),
        getAgentRun: vi.fn(async () => failed),
      },
    });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });
    expect(within(pane).getByRole("button", { name: "Retry as new Run" })).toBeInTheDocument();
  });

  it("the docked receipt carries the stated cause, verbatim", async () => {
    const user = userEvent.setup();
    const failed = runDetail({
      status: "failed",
      output_verdict: null,
      output_preview: null,
      terminal_at: null,
      failure_reason: "Model lane unavailable after 2 attempts.",
    });
    mount(deterministic, {
      overrides: {
        listAgentRuns: vi.fn(async () => ({ items: [failed], next_cursor: null })),
        getAgentRun: vi.fn(async () => failed),
      },
    });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });
    // VERBATIM — not paraphrased, and not the opaque class, which the server
    // deliberately does not serve.
    expect(within(pane).getByText("Model lane unavailable after 2 attempts.")).toBeInTheDocument();
  });

  it("a run that published no reason says so, rather than acquiring one", async () => {
    const user = userEvent.setup();
    const failed = runDetail({
      status: "failed",
      output_verdict: null,
      output_preview: null,
      terminal_at: null,
      failure_reason: null,
    });
    mount(deterministic, {
      overrides: {
        listAgentRuns: vi.fn(async () => ({ items: [failed], next_cursor: null })),
        getAgentRun: vi.fn(async () => failed),
      },
    });
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });
    expect(within(pane).getByText(/published no marked reason/)).toBeInTheDocument();
  });

  it("the retry rule reads the server's status word and nothing else", () => {
    // `completed` is excluded EXPLICITLY, because a refusal's served status is
    // `completed` — excluding it by accident would be one refactor from a
    // Retry button on a refusal.
    expect(runOffersRetry(runDetail({ status: "completed", output_verdict: "refused" }))).toBe(
      false,
    );
    expect(runOffersRetry(runDetail({ status: "completed" }))).toBe(false);
    expect(runOffersRetry(runDetail({ status: "failed" }))).toBe(true);
    expect(runOffersRetry(runDetail({ status: "budget_exhausted" }))).toBe(true);
    expect(runOffersRetry(runDetail({ status: "running" }))).toBe(false);
  });
});

// ===========================================================================
// THE CHROME — two tabs, a served strategy, and a fallback that strands nobody.
// ===========================================================================

describe("the workspace mast", () => {
  it("the strategy word is the server's, and the tab strip is two tabs", async () => {
    mount(agentic, { renderChat: () => <div>thread</div> });
    const title = await screen.findByRole("heading", { name: "Meridian Analyst", level: 1 });
    const mast = title.closest(".ag-mast")!;
    expect(within(mast as HTMLElement).getByText("Agentic")).toBeInTheDocument();
    expect(within(mast as HTMLElement).getByText("model-directed within caps")).toBeInTheDocument();
    // The four solid buttons are gone: exactly two tabs, and neither is "Chat".
    const tabs = screen.getAllByRole("tab").map((tab) => tab.textContent ?? "");
    expect(tabs).toHaveLength(2);
    expect(tabs.some((label) => label.startsWith("Chat"))).toBe(false);
  });

  it("an unknown ?view= lands on the thread rather than stranding the reader", () => {
    expect(viewFromSearch(null)).toBe("thread");
    expect(viewFromSearch("conversations")).toBe("thread");
    expect(viewFromSearch("capabilities")).toBe("capabilities");
  });

  it("the Capabilities tab shows what the agent can touch", async () => {
    const user = userEvent.setup();
    mount(agentic, { renderChat: () => <div>thread</div> });
    await user.click(await screen.findByRole("tab", { name: /Capabilities/ }));
    await waitFor(() =>
      expect(screen.queryByRole("tab", { name: /Capabilities/ })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    expect(screen.queryByText("thread")).toBeNull();
  });
});

// ===========================================================================
// FAIL CLOSED ON STALE ELIGIBILITY.
//
// A Run is admitted against an exact agent version, definition hash and
// capability snapshot; when one has moved the server answers the same sanitized
// 404 it gives a hidden Agent. That is not "something broke" — it is "the
// answer you were holding is out of date", and the pair is: it SAYS so, and it
// does not leave a Run button that can only fail again.
// ===========================================================================

describe("stale eligibility fails closed", () => {
  it("names the staleness and offers the one action that resolves it", async () => {
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        createAgentConversation: vi.fn(async () => Promise.reject({ status: 404 })),
      },
    });
    const form = await screen.findByRole("region", { name: "Start a deterministic run" });
    await user.click(within(form).getByRole("button", { name: "Run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/eligibility changed/i);
    expect(screen.getByRole("button", { name: "Refresh eligibility" })).toBeInTheDocument();
    // …and Run is not left standing as a button that can only fail again.
    await waitFor(() => expect(within(form).getByRole("button", { name: "Run" })).toBeDisabled());
  });

  it("an ordinary write failure is not dressed up as staleness", async () => {
    const user = userEvent.setup();
    mount(deterministic, {
      overrides: {
        createAgentConversation: vi.fn(async () =>
          Promise.reject({ status: 500, detail: "upstream unavailable" }),
        ),
      },
    });
    const form = await screen.findByRole("region", { name: "Start a deterministic run" });
    await user.click(within(form).getByRole("button", { name: "Run" }));
    expect(await screen.findByText(/No Conversation was created and nothing ran./)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Refresh eligibility" })).toBeNull();
  });
});

// ===========================================================================
// THE SETTLEMENT SURVIVES THE RESTYLE (train-4).
//
// A Run that reaches `completed` while serving nothing is a fault, not a
// success, and the adapter downgrades it: "Completed without output", fault
// tone, ✕ "No output" in a list. The Round 4 surfaces are new frames over that
// same rule, and each of them has to call it rather than fall back to the raw
// status word — which is exactly the regression a restyle makes easy.
// ===========================================================================

describe("a run that completed while serving nothing", () => {
  const noOutput = () =>
    runDetail({
      status: "completed",
      output_verdict: null,
      output_preview: null,
      output_state: "unavailable",
    });

  it("does not sit in the sidebar wearing a green Completed", async () => {
    mount(deterministic, {
      overrides: { listAgentRuns: vi.fn(async () => ({ items: [noOutput()], next_cursor: null })) },
    });
    const side = await screen.findByRole("complementary", { name: /Agents and runs/ });
    expect(await within(side).findByText(/No output/)).toBeInTheDocument();
    expect(within(side).queryByText(/Completed ·/)).toBeNull();
  });

  it("closes its receipt as a fault rather than as a success", async () => {
    const user = userEvent.setup();
    const run = noOutput();
    mount(deterministic, {
      overrides: {
        listAgentRuns: vi.fn(async () => ({ items: [run], next_cursor: null })),
        getAgentRun: vi.fn(async () => run),
      },
    });
    // The turn's own receipt footer takes the downgraded outcome word…
    expect(await screen.findByText("Completed without output")).toBeInTheDocument();
    // …and so does the docked receipt's Status field.
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: /Run 3/ });
    expect(within(pane).getByText(/No output/)).toBeInTheDocument();
  });
});

// ===========================================================================
// THE COMPOSER'S DOOR — a non-default agent's thread routes to ITS OWN agent.
//
// The default-Ask chat wire reaches exactly one agent, so the injection must
// name the door: the canonical default keeps "default-ask", and every other
// agentic agent gets "agent-run" plus the frozen envelope assembled from the
// workspace's own served reads. The pair that makes each assertion mean
// something: the right door is named AND the wrong envelope is absent.
// ===========================================================================

describe("the agentic thread's routing injection", () => {
  const customerSupport: AgentDetailView = {
    ...agentic,
    agent_id: "agt_01k3customersupport000000000",
    version: 2,
    label: "Meridian Customer Support",
  };

  function captureInjection(agent: AgentDetailView) {
    let captured:
      Parameters<NonNullable<React.ComponentProps<typeof AgentWorkspace>["renderChat"]>>[0] | null =
      null;
    mount(agent, {
      renderChat: (props) => {
        captured = props;
        return <div>thread</div>;
      },
    });
    return () => captured;
  }

  it("hands a NON-default agentic agent the agent-run door with its exact envelope", async () => {
    const read = captureInjection(customerSupport);
    await screen.findByText("thread");
    await waitFor(() => {
      const props = read();
      expect(props?.runTarget).not.toBeNull();
    });
    const props = read()!;
    expect(props.runRouting).toBe("agent-run");
    // Served values only: the agent read's version, the platform's frozen
    // config hash, the preflight's capability snapshot hash.
    expect(props.runTarget).toEqual({
      agentId: "agt_01k3customersupport000000000",
      agentVersion: 2,
      configHash: "f".repeat(64),
      capabilityHash: "c".repeat(64),
    });
  });

  it("keeps the canonical default agent on the default-ask door, with NO envelope", async () => {
    const read = captureInjection(agentic);
    await screen.findByText("thread");
    const props = read()!;
    expect(props.runRouting).toBe("default-ask");
    expect(props.runTarget).toBeNull();
  });
});

// ===========================================================================
// PAIR 4 — THE ANSWER-LEDGER DOOR STAYS OUT OF THE SIDEBAR, AND THE HOLE IT
// CAME THROUGH STAYS BRICKED UP.
//
// POST_DEMO #15: `MonitoredAnswerLink` was mounted as `sidebarFoot`, plumbed
// down through `AgentWorkspace` to `WorkspaceSidebar` as `foot`, and pinned a
// whole question under the conversation history where it crowded the list.
// 4691e9b5 removed the link AND that plumbing — but removed it STRUCTURALLY.
// Nothing went red if it came back. These two do.
//
// The pair is the two ways it can return, and NEITHER test covers the other's
// way in — which is exactly why both are here:
//
//   (a) mounted from inside the sidebar itself. Caught by reading the links
//       the sidebar actually renders. The control half is that the roster and
//       "All agents" ARE found by the same query, so "no answer-ledger link"
//       is a fact about the sidebar rather than a fact about an empty result.
//
//   (b) injected by the OWNING ROUTE — which is how it was mounted the first
//       time: `App.tsx` passed `sidebarFoot={<MonitoredAnswerLink />}`. This
//       file mounts `AgentWorkspace` directly and would never pass that prop,
//       so (a) is structurally blind to (b). What (b) asserts instead is that
//       the route CANNOT do it again: offer the component every slot name the
//       removed plumbing used and nothing of the caller's reaches the screen.
//       The cast is the point — the types already refuse these props, and a
//       type error is a compile-time opinion this suite would never watch
//       fail. Re-add the `sidebarFoot`/`foot` pass-through and the probe
//       renders.
// ===========================================================================

describe("the answer ledger is not a workspace-sidebar destination", () => {
  it("(a) the sidebar's links go to agents, and not one of them is the monitored answer", async () => {
    mount(agentic, {
      overrides: { listAgentConversations: vi.fn(async () => conversations) },
      renderChat: () => <div>thread</div>,
    });
    const side = await screen.findByRole("complementary", { name: /Agents and conversations/ });
    await within(side).findByText("Conversation · 2026-08-27 09:12");

    // THE CONTROL. Without this the next assertion passes just as happily over
    // a sidebar that rendered no links at all, or over a query pointed at the
    // wrong element.
    const links = within(side).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toContain("/agents/all");

    expect(links.some((link) => /answer-ledger/.test(link.getAttribute("href") ?? ""))).toBe(false);
    // …and the wording it wore, in case it comes back pointing elsewhere.
    expect(within(side).queryAllByText(/monitor/i)).toHaveLength(0);
  });

  it("(b) the workspace offers NO content slot, so the route cannot inject the door back in", async () => {
    // The link exactly as it rendered: `Monitor a question` when the ledger
    // read came back empty — which is what it would render here, since no
    // answer-ledger client is mounted in this suite at all.
    const door = <a href="/answer-ledger">Monitor a question</a>;
    const slots = {
      sidebarFoot: door,
      foot: door,
      children: door,
    } as unknown as Partial<React.ComponentProps<typeof AgentWorkspace>>;

    render(
      <MemoryRouter initialEntries={[`/agents/${agentic.agent_id}`]}>
        <Routes>
          <Route
            path="/agents/:id"
            element={
              <AgentWorkspace
                client={client(agentic, {
                  listAgentConversations: vi.fn(async () => conversations),
                })}
                agentId={agentic.agent_id}
                identityKey="principal-a"
                platformConfigHash={"f".repeat(64)}
                configurationState="ready"
                renderChat={() => <div>thread</div>}
                {...slots}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // THE CONTROL. The sidebar settled with its history on screen, so the
    // probe had every render the real link would have had to appear in.
    const side = await screen.findByRole("complementary", { name: /Agents and conversations/ });
    await within(side).findByText("Conversation · 2026-08-27 09:12");

    expect(screen.queryByRole("link", { name: "Monitor a question" })).toBeNull();
    expect(screen.queryAllByRole("link").map((link) => link.getAttribute("href"))).not.toContain(
      "/answer-ledger",
    );
  });
});
