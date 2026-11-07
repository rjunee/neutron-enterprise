// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// PRE-012A's guarantees, on the round-4 surface.
//
// The interior this suite was written against is gone: `AgentDetailPage` and
// `ConversationWorkspace` are deleted, the four-tab strip is two underline tabs,
// and `/agents/:id/conversations/:cid` is deleted outright. What did NOT change
// is what this file exists to hold: the wire is the server's, the reads are
// principal-effective, a sanitized 404 is neutral and unretryable, nothing
// invents a total, and the deterministic surface is a form and not a prompt box.
// Every test below is one of those facts, asked of the component that owns it
// now.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { AgentRunPage } from "./AgentRunPage";
import { AgentWorkspace } from "./AgentWorkspace";
import type { AgenticThreadInjection } from "./AgentWorkspace";
import { capabilityMismatch, exactHash } from "./capabilityGuard";
import { RUN_RECEIPT_LABEL } from "./RunReceiptPane";
import { RUN_PAUSED_OUTPUT_SENTENCE } from "../agent-harness/adapters/runWorkspace";
import { Transcript } from "../transcript";
import type {
  AgentCapabilityPreflight,
  AgentDetailView,
  AgentPage,
  AgentsClient,
  AgentsInteriorClient,
  AgentSummary,
  AgentWorkflowProfile,
  ConversationPage,
  ConversationSummary,
  RunReceipt,
  RunPage,
  RunStatus,
  StartAgentRunRequest,
} from "./types";

// The interior's field names are the server's field names. Both directions are
// pinned, because each alone is satisfiable by the drift it is meant to forbid:
// the absence guards below pass if a field is deleted outright, and the presence
// guards pass if an invented alias is added alongside the real name.
type AssertNever<T extends never> = T;
/** `K` must be a key of `T`, so renaming the field away stops compiling. */
type RequiresKeys<T, K extends keyof T> = K;

// PRESENT — the exact names `agents_wire.py` and `runs_wire.py` emit.
type _AgentServerNames = RequiresKeys<AgentSummary, "label" | "config_hash">;
type _RunServerNames = RequiresKeys<RunReceipt, "sequence" | "display_label" | "config_hash">;
// ABSENT — the invented names this unit exists to remove. `name` and
// `configuration_hash` were never emitted by any endpoint.
type _NoInventedAgentFields = AssertNever<
  Extract<keyof AgentSummary, "name" | "configuration_hash">
>;
type _NoInventedRunFields = AssertNever<
  Extract<keyof RunReceipt, "run_number" | "name" | "configuration_hash">
>;
type _NoInventedConversationTurns = AssertNever<Extract<keyof ConversationSummary, "turns">>;
type _NoInventedAttentionFeed = AssertNever<Extract<keyof AgentsInteriorClient, "listAttention">>;
type _NoClientOwnedIdentity = AssertNever<
  Extract<keyof StartAgentRunRequest, "principal_id" | "department">
>;
type _NoInventedBackendProducers = AssertNever<
  Extract<
    keyof AgentsClient,
    "createConversation" | "listAttention" | "listRecent" | "getAgentModelPreflight"
  >
>;
// INERT — 012A reads and cannot execute. The claim is load-bearing, so it is a
// guard rather than a comment: the seam carries no method that could start,
// continue or stop a Run, and no write of any kind.
type _NoExecutionSeam = AssertNever<
  Extract<
    keyof AgentsInteriorClient,
    "submitRun" | "createRun" | "startRun" | "cancelRun" | "sendMessage" | "createConversation"
  >
>;
// `next_cursor` is a REQUIRED key carrying a NULLABLE value, exactly as every
// `*Page` wire model declares it. An optional key would let a page that simply
// forgot the field read as a terminal page.
type _CursorNeverUndefined = AssertNever<Extract<undefined, AgentPage["next_cursor"]>>;
const terminalCursor: AgentPage["next_cursor"] = null;
void terminalCursor;
export type WireGuards = readonly [
  _AgentServerNames,
  _RunServerNames,
  _NoInventedAgentFields,
  _NoInventedRunFields,
  _NoInventedConversationTurns,
  _NoInventedAttentionFeed,
  _NoClientOwnedIdentity,
  _NoInventedBackendProducers,
  _NoExecutionSeam,
  _CursorNeverUndefined,
];
// @ts-expect-error The current Runs wire has no waiting_for_approval status.
const invalidRunStatus: RunStatus = "waiting_for_approval";
void invalidRunStatus;

const agent: AgentDetailView = {
  agent_id: "agent-meridian",
  version: 7,
  label: "Meridian Analyst",
  description: "Investigates governed account evidence.",
  owner_label: "Revenue Operations",
  lifecycle: "active",
  allowed_strategies: ["agentic"],
  default_strategy: "agentic",
  model_policy_id: "balanced",
  tool_grants: ["lookup"],
  knowledge_sources: [{ source_id: "meridian", tiers: ["t1", "t2"] }],
  skills: [{ skill_id: "research", version: 1, content_hash: "e".repeat(64) }],
  budgets: {
    max_steps: 16,
    max_tool_calls: 8,
    max_input_tokens: 32_000,
    max_output_tokens: 8_000,
  },
  // What the server makes of those: min(16, 8) tool calls, one more model call
  // than that, 16 x 10s of wall clock, and the two token budgets as one.
  enforced_caps: {
    max_tool_calls: 8,
    max_model_requests: 9,
    wall_clock_seconds: 160,
    max_total_tokens: 40_000,
    max_parked_writes: 1,
  },
  workflow: null,
  available: false,
  availability_reasons: ["knowledge-unavailable"],
  config_hash: "d".repeat(64),
};
const conversation: ConversationSummary = {
  conversation_id: "conversation-1",
  agent_id: agent.agent_id,
  title: "Renewal risk review",
  created_at: "2026-08-22T09:00:00Z",
  updated_at: "2026-08-22T09:01:00Z",
  last_sequence: 9,
};
const runs: RunPage = {
  next_cursor: null,
  items: [
    {
      run_id: "run-9",
      conversation_id: conversation.conversation_id,
      agent_id: agent.agent_id,
      agent_version: 7,
      sequence: 9,
      version: 3,
      predecessor_run_id: null,
      root_run_id: "run-9",
      retry_ordinal: 0,
      display_label: "Run 9 · Agentic · 10:15",
      status: "waiting_approval",
      strategy: "agentic",
      output_verdict: null,
      model_policy_id: "balanced",
      model_request_mode: "explicit",
      context_snapshot_hash: "9".repeat(64),
      requested_model: "frontier-primary",
      requested_model_lane: "frontier-api",
      served_model: "private-primary",
      route_position: "unavailable",
      fallback_from: null,
      output_preview: null,
      config_hash: "f".repeat(64),
      capability_snapshot_hash: "c".repeat(64),
      created_at: "2026-08-22T09:00:00Z",
      updated_at: "2026-08-22T09:01:00Z",
      terminal_at: null,
    },
  ],
};
const turns = {
  items: runs.items.map((run) => ({
    turn_id: `turn-${run.sequence}`,
    turn_sequence: run.sequence,
    user_content_state: "available" as const,
    user_content: "Review renewal risk",
    run,
  })),
  next_cursor: null,
};

const executableAgent: AgentDetailView = {
  ...agent,
  available: true,
  availability_reasons: [],
};
const preflight: AgentCapabilityPreflight = {
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
        name: "lookup",
        label: "Account lookup",
        read_write_class: "read" as const,
        declared_tier: 1,
        effective_tier: 1,
        connection_identity_label: "Meridian read identity",
        health: "healthy" as const,
        description: "Search the governed knowledge base",
        arguments: [],
        answer_columns: null,
      },
    ],
    connectors: [],
  },
  skills: [],
  model: {
    task: "governed-analysis",
    allowed_strategies: ["agentic" as const],
    default_strategy: "agentic" as const,
    max_steps: 16,
    max_tool_calls: 8,
    max_input_tokens: 32000,
    max_output_tokens: 8000,
    candidates: [
      {
        model: "frontier-primary",
        provider: "anthropic",
        lane: "frontier-api",
        evidence: "serving-ready",
        observed_at: "2026-08-23T03:00:00Z",
      },
    ],
    default_model: "frontier-primary",
  },
};

/** The same Agent with the SERVER saying deterministic — the one input the
 * workspace has for which surface to draw. */
const deterministicAgent: AgentDetailView = {
  ...executableAgent,
  allowed_strategies: ["deterministic"],
  default_strategy: "deterministic",
};
const deterministicPreflight: AgentCapabilityPreflight = {
  ...preflight,
  model: {
    ...preflight.model,
    allowed_strategies: ["deterministic"],
    default_strategy: "deterministic",
    candidates: [],
    default_model: null,
  },
};
/** A profile that matches the Agent on all four pinned fields. */
const matchingProfile: AgentWorkflowProfile = {
  agent_id: deterministicAgent.agent_id,
  agent_version: deterministicAgent.version,
  agent_config_hash: deterministicAgent.config_hash,
  strategy: "deterministic",
  workflow_id: "meridian-safe-review",
  workflow_version: 1,
  workflow_config_hash: "a".repeat(64),
  input_schema: {
    type: "object",
    required: ["max_subjects"],
    additionalProperties: false,
    properties: {
      max_subjects: { type: "integer", minimum: 1, maximum: 5 },
    },
  },
  fixed_inputs: {},
  fixed_plan: [],
};

function client(overrides: Partial<AgentsClient> = {}): AgentsClient {
  return {
    listAgents: vi.fn(async (): Promise<AgentPage> => ({ items: [agent], next_cursor: "next" })),
    getAgent: vi.fn(async () => agent),
    listAgentConversations: vi.fn(async (): Promise<ConversationPage> => ({
      items: [conversation],
      next_cursor: null,
    })),
    getConversation: vi.fn(async () => conversation),
    createAgentConversation: vi.fn(async () => conversation),
    listConversationTurns: vi.fn(async () => turns),
    listConversationRuns: vi.fn(async () => runs),
    listAgentRuns: vi.fn(async () => runs),
    getAgentPreflight: vi.fn(async () => preflight),
    getAgentWorkflowProfile: vi.fn(async () => Promise.reject({ status: 404 })),
    listAgentRunUpdates: vi.fn<AgentsClient["listAgentRunUpdates"]>(async () => ({
      view: "recent",
      window_start: "2026-08-22T00:00:00Z",
      window_end: "2026-08-23T00:00:00Z",
      items: [],
      next_cursor: null,
    })),
    startAgentRun: vi.fn<AgentsClient["startAgentRun"]>(async () => ({
      action: "start",
      run_id: "run-10",
      attempt_id: "attempt-10",
      version: 1,
      status: "submitted",
      enqueue_id: "enqueue-10",
      replayed: false,
    })),
    getAgentRun: vi.fn(async () => runs.items[0]!),
    listAgentRunEvents: vi.fn(async () => ({ items: [], next_cursor: null })),
    cancelAgentRun: vi.fn(),
    retryAgentRun: vi.fn(),
    ...overrides,
  };
}
/** The agentic thread, INJECTED exactly as `App.tsx` injects the real ChatPage.
 * The seam is the point: this suite drives the workspace's own reads and its
 * docked pane without dragging Ask's provider tree into every test. */
function stubThread(props: AgenticThreadInjection): ReactNode {
  return (
    <div>
      <span>{props.capsNote}</span>
      {props.unavailableReason === null ? null : <p role="status">{props.unavailableReason}</p>}
      {props.pastTurns === null ? null : (
        <Transcript turns={props.pastTurns} label="Conversation" />
      )}
      {props.receiptPane}
    </div>
  );
}

function mountWorkspace(
  overrides: Partial<AgentsClient> = {},
  {
    entry = `/agents/${agent.agent_id}`,
    renderChat,
  }: {
    entry?: string;
    renderChat?: (props: AgenticThreadInjection) => ReactNode;
  } = {},
): AgentsClient {
  const api = client(overrides);
  render(
    <MemoryRouter initialEntries={[entry]}>
      <AgentWorkspace
        client={api}
        agentId={agent.agent_id}
        identityKey="principal-a"
        platformConfigHash={"f".repeat(64)}
        configurationState="ready"
        {...(renderChat ? { renderChat } : {})}
      />
    </MemoryRouter>,
  );
  return api;
}

describe("PRE-012A Agent interior", () => {
  it("renders route-backed detail and leaves execution visibly inert", async () => {
    const api = mountWorkspace();
    expect(
      await screen.findByRole("heading", { level: 1, name: "Meridian Analyst" }),
    ).toBeInTheDocument();
    // §2.7: a hash truncates to 8 with a copy affordance, and the SERVED value
    // is never lost — it is the chip's `title` and its clipboard payload.
    expect(screen.getByTitle("d".repeat(64))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy agent definition hash" })).toBeInTheDocument();
    // The strategy is the server's `default_strategy`, said once.
    expect(screen.getByText("Agentic")).toBeInTheDocument();
    // Reading this screen starts nothing.
    expect(api.startAgentRun).not.toHaveBeenCalled();
    expect(api.createAgentConversation).not.toHaveBeenCalled();
  });

  it("names the two tabs for the served strategy and falls back to the thread on an unknown ?view=", async () => {
    // `Conversations` and `Runs` stopped being peer tabs — they are the same
    // question asked of two strategies and share one sidebar slot. What earns a
    // tab is a DIFFERENT question: what can this agent touch.
    mountWorkspace({}, { entry: `/agents/${agent.agent_id}?view=conversations` });
    expect(await screen.findByRole("tab", { name: "Conversation" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: /Capabilities/ })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    expect(screen.queryByRole("tab", { name: "Runs" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Chat" })).toBeNull();
  });

  it("renders the selected Conversation's canonical turns in the thread, not on a second route", async () => {
    // `/agents/:id/conversations/:cid` is deleted. The turns it was a whole
    // route for are read into THIS thread, and the read is the same canonical
    // one: `listConversationTurns`, for the selected Conversation only.
    const api = mountWorkspace(
      {},
      {
        entry: `/agents/${agent.agent_id}?conversation=${conversation.conversation_id}`,
        renderChat: stubThread,
      },
    );
    expect(await screen.findByText("Review renewal risk")).toBeInTheDocument();
    expect(api.listConversationTurns).toHaveBeenCalledWith(
      agent.agent_id,
      conversation.conversation_id,
    );
    // The turn's own meta is the server's sequence and stamp.
    expect(screen.getByText("Turn 9 · 2026-08-22 09:00:00")).toBeInTheDocument();
    // A read-back turn says what the turns index serves and no more: the served
    // model, and the RIGHT ONE of the four reasons the Output slot is empty —
    // never an empty answer, and never the wrong sentence. This fixture's Run
    // is `waiting_approval`, so it draws the paused note. It used to draw a
    // hard-coded "no output was served" here, which is a statement about a
    // FINISHED turn and was wrong for three of the four cases; the thread now
    // reads runOutputAbsentNote like every other Run surface.
    expect(screen.getByText("private-primary")).toBeInTheDocument();
    expect(screen.getByText(RUN_PAUSED_OUTPUT_SENTENCE)).toBeInTheDocument();
    // The turns index serves no per-step ledger, so the turn draws no work band
    // rather than one claiming the turn made no governed calls.
    expect(screen.queryByText(/steps?$/)).toBeNull();
  });

  it("opens the immutable server-authored Run receipt in the docked pane, without leaving the thread", async () => {
    const user = userEvent.setup();
    mountWorkspace(
      {},
      {
        entry: `/agents/${agent.agent_id}?conversation=${conversation.conversation_id}`,
        renderChat: stubThread,
      },
    );
    await user.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    const pane = await screen.findByRole("dialog", { name: "Run 9 · Agentic · 10:15" });
    // Every field is the server's own, and the names are the receipt's
    // (agents/RunReceiptPane.tsx) — one body of facts, docked here and rendered
    // standalone at the deep link below.
    expect(within(pane).getByText("run-9")).toBeInTheDocument();
    expect(within(pane).getByText("Status").closest("div")).toHaveTextContent("Awaiting approval");
    expect(within(pane).getByText("Sequence").closest("div")).toHaveTextContent(
      "Run 9 · retry 0 · run version 3",
    );
    expect(within(pane).getByText("Model").closest("div")).toHaveTextContent("private-primary");
    expect(within(pane).getByText("Requested model").closest("div")).toHaveTextContent(
      "frontier-primary",
    );
    expect(within(pane).getByTitle("c".repeat(64))).toBeInTheDocument();
    expect(within(pane).getByTitle("f".repeat(64))).toBeInTheDocument();
    expect(within(pane).getByText("Conversation").closest("div")).toHaveTextContent(
      "Renewal risk review",
    );
    // The wire carries no consumption counter, so the Budget field says the
    // number it shows is the SERVED cap rather than a spend.
    expect(within(pane).getByText("Caps").closest("div")).toHaveTextContent(
      "the wire carries no consumption counter",
    );
    // ...and the reader is still in the conversation they were reading.
    expect(screen.getByText("Review renewal risk")).toBeInTheDocument();
  });

  it("reopens the minimal canonical Run receipt at its stable deep link", async () => {
    render(
      <MemoryRouter>
        <AgentRunPage
          client={client()}
          agentId={agent.agent_id}
          runId="run-9"
          identityKey="principal-a"
        />
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: "Run 9 · Agentic · 10:15" }),
    ).toBeInTheDocument();
    // The docked pane and this route render the SAME component, so the facts
    // and their names are identical here.
    const facts = screen.getByRole("dialog", { name: RUN_RECEIPT_LABEL });
    expect(facts).toHaveTextContent("Awaiting approval");
    expect(facts).toHaveTextContent("run version 3");
    expect(facts).toHaveTextContent("frontier-primary");
    expect(facts).toHaveTextContent("private-primary");
    // "Back to Conversation" is DELETED with the conversation-turns route. The
    // way back is the crumb, and it carries the selection so the workspace
    // opens on the Conversation this Run came from.
    expect(screen.queryByRole("link", { name: "Back to Conversation" })).toBeNull();
    expect(screen.getByRole("link", { name: agent.label })).toHaveAttribute(
      "href",
      "/agents/agent-meridian?conversation=conversation-1",
    );
    // The transcript's Output slot on the (default) Summary tab: a paused Run
    // with no served preview draws the exact sentence, never "unavailable".
    expect(
      screen.getByText("No safe output preview is available while the Run is paused."),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Plan" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Timeline" })).toBeInTheDocument();
  });

  it("lists the agentic Agent's Conversations as its ONE history, and never its Runs beside them", async () => {
    const api = mountWorkspace({}, { renderChat: stubThread });
    const side = await screen.findByRole("complementary", { name: "Agents and conversations" });
    expect(await within(side).findByRole("button", { name: /Renewal risk review/ })).toBeVisible();
    expect(api.listAgentConversations).toHaveBeenCalledWith(agent.agent_id);
    expect(api.listAgentRuns).not.toHaveBeenCalled();
    // A Conversation carries no served status, so its row draws no glyph: an
    // invented ✓ beside a thread would be a claim about an outcome.
    expect(within(side).queryByText("✓")).toBeNull();
  });

  it("lists canonical Agent Run receipts as a deterministic Agent's ONE history", async () => {
    const listAgentRuns = vi.fn<AgentsInteriorClient["listAgentRuns"]>(async () => runs);
    const api = mountWorkspace({ getAgent: vi.fn(async () => deterministicAgent), listAgentRuns });
    const side = await screen.findByRole("complementary", { name: "Agents and runs" });
    // Read WITHOUT a Conversation selection — `/v1/agents/{id}/runs`, the
    // agent-wide index, exactly as before.
    expect(await within(side).findByRole("button", { name: /Run 9 · Agentic/ })).toBeVisible();
    expect(listAgentRuns).toHaveBeenCalledWith(agent.agent_id);
    expect(api.listAgentConversations).not.toHaveBeenCalled();
    // A Run DOES carry a served status, so its row says the server's word.
    expect(within(side).getByText(/Awaiting approval/)).toBeInTheDocument();
  });

  it("shows the served page and invents no total, no page number and no zero", async () => {
    // The interior's Conversations/Runs PAGERS are gone with their tabs; what
    // survives is the rule they existed for. The sidebar renders the page the
    // server served and says nothing it was not told: no count, no "1 of N",
    // and an agent with no history gets one factual sentence rather than a 0.
    const api = mountWorkspace({
      listAgentConversations: vi.fn(async () => ({ items: [], next_cursor: null })),
    });
    const side = await screen.findByRole("complementary", { name: "Agents and conversations" });
    expect(await within(side).findByText("No conversations with this agent yet.")).toBeVisible();
    expect(within(side).queryByText(/\b0\b/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Next page" })).toBeNull();
    // The ROSTER's cursor is still honoured: the server sent one, so this says
    // there is more rather than presenting a page as the whole catalog.
    expect(await within(side).findByText(/More agents than fit here/)).toBeVisible();
    expect(api.listAgents).toHaveBeenCalledTimes(1);
  });

  it("renders a sanitized 404 as neutral and unretryable", async () => {
    // routers/agents.py answers a hidden Agent and an unknown Agent with the
    // same 404, deliberately. One neutral presentation covers both, and there
    // is no Retry — a correctly governed boundary is not an outage.
    mountWorkspace({ getAgent: vi.fn(async () => Promise.reject({ status: 404 })) });
    expect(await screen.findByText("This Agent isn’t available.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("never substitutes a generic prompt for deterministic execution", async () => {
    // THE ASSERTION THAT STOPS THIS BEING COSMETIC. Not "the composer is
    // hidden", not "the button is disabled" — there is no free-text control on
    // the screen at all, on any branch, so no amount of CSS or state can
    // reveal one.
    mountWorkspace({
      getAgent: vi.fn(async () => deterministicAgent),
      getAgentPreflight: vi.fn(async () => deterministicPreflight),
      getAgentWorkflowProfile: vi.fn(async () => matchingProfile),
    });
    await screen.findByRole("region", { name: "Start a deterministic run" });
    expect(document.querySelector("textarea")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Instruction" })).toBeNull();
    expect(screen.queryByText(/Hybrid/i)).toBeNull();
    expect(screen.queryByRole("button", { name: "Continue Conversation" })).toBeNull();
  });

  it("renders the server's budgets verbatim, max_output_tokens included", async () => {
    // Whatever the registry serves renders as served: not rounded, not hidden
    // because it looks like a defect, not annotated with the console's opinion.
    mountWorkspace({
      getAgent: vi.fn(async () => ({
        ...deterministicAgent,
        budgets: {
          max_steps: 1,
          max_tool_calls: 100,
          max_input_tokens: 8_210,
          max_output_tokens: 1,
        },
        enforced_caps: null,
      })),
      getAgentPreflight: vi.fn(async () => deterministicPreflight),
      getAgentWorkflowProfile: vi.fn(async () => matchingProfile),
    });
    const budgets = await screen.findByLabelText("Budgets");
    expect(budgets).toHaveTextContent("1 step · 100 tool calls · 8,210 in · 1 out");
  });

  it("renders only the exact matching deterministic workflow profile", async () => {
    mountWorkspace({
      getAgent: vi.fn(async () => deterministicAgent),
      getAgentPreflight: vi.fn(async () => deterministicPreflight),
      getAgentWorkflowProfile: vi.fn<AgentsClient["getAgentWorkflowProfile"]>(async () => ({
        ...matchingProfile,
        agent_config_hash: "0".repeat(64),
        workflow_id: "renewal-risk-v2",
      })),
    });
    expect(
      await screen.findByText(/workflow profile does not match this Agent/),
    ).toBeInTheDocument();
    expect(screen.queryByText("renewal-risk-v2")).toBeNull();
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
  });

  it("submits the deterministic workflow's schema-labelled bounded input, and no generic prompt", async () => {
    const user = userEvent.setup();
    const startAgentRun = vi.fn<AgentsClient["startAgentRun"]>(async () => ({
      action: "start",
      run_id: "run-typed",
      attempt_id: "attempt-typed",
      version: 1,
      status: "submitted",
      enqueue_id: "enqueue-typed",
      replayed: false,
    }));
    mountWorkspace({
      getAgent: vi.fn(async () => deterministicAgent),
      getAgentPreflight: vi.fn(async () => deterministicPreflight),
      getAgentWorkflowProfile: vi.fn(async () => matchingProfile),
      startAgentRun,
    });
    // The field's accessible name comes from the SERVER's schema, not from
    // console copy: this fixture's property carries no `title`, so the name is
    // its property name made readable. A schema that names itself ("Max
    // Subjects", as Pydantic emits) would render that instead.
    const count = await screen.findByRole("spinbutton", { name: "Max subjects" });
    expect(screen.queryByRole("textbox", { name: "Instruction" })).toBeNull();
    await user.clear(count);
    await user.type(count, "5");
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(startAgentRun).toHaveBeenCalledWith(
      agent.agent_id,
      expect.objectContaining({
        conversation_id: conversation.conversation_id,
        expected_version: conversation.last_sequence,
        agent_version: deterministicAgent.version,
        strategy: "deterministic",
        config_hash: "f".repeat(64),
        capability_snapshot_hash: "c".repeat(64),
        input: '{"max_subjects":5}',
        // Deterministic execution is server-routed: the console names no model.
        requested_model: null,
        requested_model_lane: null,
      }),
      expect.stringMatching(/^[0-9a-f-]{36}$/),
    );
  });

  it("retries the exact frozen body and idempotency key after an ambiguous failure", async () => {
    const user = userEvent.setup();
    const createAgentConversation = vi.fn<AgentsClient["createAgentConversation"]>(
      async () => conversation,
    );
    const startAgentRun = vi
      .fn<AgentsClient["startAgentRun"]>()
      .mockRejectedValueOnce(Object.assign(new Error("offline"), { status: 503 }))
      .mockResolvedValueOnce({
        action: "start",
        run_id: "run-10",
        attempt_id: "attempt-10",
        version: 1,
        status: "submitted",
        enqueue_id: "enqueue-10",
        replayed: true,
      });
    mountWorkspace({
      getAgent: vi.fn(async () => deterministicAgent),
      getAgentPreflight: vi.fn(async () => deterministicPreflight),
      getAgentWorkflowProfile: vi.fn(async () => matchingProfile),
      createAgentConversation,
      startAgentRun,
    });
    await user.click(await screen.findByRole("button", { name: "Run" }));
    // THE COST OF ONE CLICK, PAID OUT LOUD. Two writes behind one button can
    // half-succeed, and the half that did says so rather than leaving an
    // unexplained empty Conversation behind.
    expect(
      await screen.findByText(/The Conversation was created, but the Run was not submitted/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry exact request" }));
    expect(startAgentRun).toHaveBeenCalledTimes(2);
    // Same body, same idempotency key, same expected_version — the server's
    // idempotency is what makes the retry safe, and regenerating either would
    // forfeit it. And no second Conversation.
    expect(startAgentRun.mock.calls[1]).toEqual(startAgentRun.mock.calls[0]);
    expect(createAgentConversation).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Idempotent replay")).toBeInTheDocument();
  });

  it("does not bypass an unavailable capability answer", async () => {
    const startAgentRun = vi.fn<AgentsClient["startAgentRun"]>();
    mountWorkspace({
      getAgent: vi.fn(async () => deterministicAgent),
      getAgentPreflight: vi.fn(async () => ({ ...deterministicPreflight, capabilities: null })),
      getAgentWorkflowProfile: vi.fn(async () => matchingProfile),
      startAgentRun,
    });
    expect(
      await screen.findByText("Unavailable — Eligible capabilities are unavailable."),
    ).toHaveAttribute("role", "status");
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    expect(startAgentRun).not.toHaveBeenCalled();
  });

  it("says the served availability_reasons verbatim and disables the surface rather than hiding it", async () => {
    mountWorkspace({ getAgent: vi.fn(async () => agent) }, { renderChat: stubThread });
    // Verbatim, joined — never a rewrite and never a generic sentence standing
    // in for the server's own words.
    expect(await screen.findByText("Unavailable — knowledge-unavailable")).toHaveAttribute(
      "role",
      "status",
    );
    // ...and the served ENFORCED caps are still legible beside it. Not the
    // declared 16 steps: nothing counts to that, and "steps" is the transcript
    // band's word for its own row count.
    expect(screen.getByText("Caps · 8 tool calls · 9 model calls · 160s")).toBeInTheDocument();
  });
});

// ===========================================================================
// THE PRE-SUBMIT CAPABILITY GUARD, lifted verbatim out of the deleted
// AgentLauncher into agents/capabilityGuard.ts. It computes no authorization of
// its own: every value it reads is a server field compared to another server
// field, and its only verdict is "these two answers disagree, do not send yet".
// ===========================================================================

describe("capabilityGuard", () => {
  it("accepts only an exact 64-hex capability hash", () => {
    expect(exactHash("c".repeat(64))).toBe(true);
    expect(exactHash("C".repeat(64))).toBe(false);
    expect(exactHash("c".repeat(63))).toBe(false);
    expect(exactHash("")).toBe(false);
    expect(exactHash(null)).toBe(false);
  });

  it("fails closed on every server disagreement it can see", () => {
    const capabilities = preflight.capabilities!;
    expect(capabilityMismatch(executableAgent, preflight)).toBeNull();
    // An Agent or a preflight the server called unavailable.
    expect(capabilityMismatch(agent, preflight)).toBe("This Agent is unavailable.");
    expect(capabilityMismatch(executableAgent, { ...preflight, available: false })).toBe(
      "This Agent is unavailable.",
    );
    // No eligible capability set is NOT an empty one.
    expect(capabilityMismatch(executableAgent, { ...preflight, capabilities: null })).toBe(
      "Eligible capabilities are unavailable.",
    );
    // The version or the definition hash moved under the reader.
    expect(capabilityMismatch(executableAgent, { ...preflight, version: 8 })).toBe(
      "Agent eligibility changed. Refresh before continuing.",
    );
    expect(
      capabilityMismatch(executableAgent, { ...preflight, definition_hash: "0".repeat(64) }),
    ).toBe("Agent eligibility changed. Refresh before continuing.");
    expect(
      capabilityMismatch(executableAgent, {
        ...preflight,
        capabilities: { ...capabilities, agent_version: 8 },
      }),
    ).toBe("Agent eligibility changed. Refresh before continuing.");
    // A capability hash that is not an exact hash is not a capability hash.
    expect(
      capabilityMismatch(executableAgent, {
        ...preflight,
        capabilities: { ...capabilities, capability_hash: "not-a-hash" },
      }),
    ).toBe("Eligible capabilities are unavailable.");
    // The model catalog authorizes a strategy the Agent definition does not.
    expect(
      capabilityMismatch(executableAgent, {
        ...preflight,
        model: { ...preflight.model, allowed_strategies: ["deterministic"] },
      }),
    ).toBe("Agent model eligibility changed. Refresh before continuing.");
  });
});
