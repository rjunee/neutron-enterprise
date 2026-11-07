// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { AuthContext } from "react-oidc-context";
import type { AuthContextProps } from "react-oidc-context";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../App";
import { RuntimeConfigContext } from "../AppBootstrap";
import type { AgentEvidenceGraphHopWire } from "../api/types";
import { dismissTour } from "../demo/tourStorage";
import { makeAuthValue } from "../test/authStub";
import { PRINCIPAL, RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { GROUNDING_BOUND_NOTE } from "../chat/askCopy";
import { RUN_RECEIPT_LABEL } from "./RunReceiptPane";

const AGENT = "agt_01k3meridiananalyst000000000";
const HARDSHIP = "agt_01k3hardshipreview0000000000";
const CONVERSATION = "conversation-pre016-owner";
const RUN = "run-pre016-owner";
const NOW = "2026-08-23T18:00:00Z";

const graphPath = [
  {
    subject: {
      entity_class: "Customer",
      entity_key: "customer-a",
      display: "Customer A",
      assurance: "source",
      source_ref: "drive-meridian:item-customer-a",
    },
    edge_kind: "HAS_AUTO_LOAN",
    obj: {
      entity_class: "AutoLoan",
      entity_key: "loan-a",
      display: "Loan A",
      assurance: "confirmed",
      source_ref: "drive-meridian:item-loan-a",
    },
    source_system: "m365-sharepoint",
    source_ref: "drive-meridian:item-customer-a",
    assurance: "confirmed",
    review_state: null,
  },
  {
    subject: {
      entity_class: "AutoLoan",
      entity_key: "loan-a",
      display: "Loan A",
      assurance: "confirmed",
      source_ref: "drive-meridian:item-loan-a",
    },
    edge_kind: "HAS_PAYMENT_SCHEDULE",
    obj: {
      entity_class: "PaymentSchedule",
      entity_key: "payment-a",
      display: "Payment A",
      assurance: "source",
      source_ref: "drive-meridian:item-payment-a",
    },
    source_system: "m365-sharepoint",
    source_ref: "drive-meridian:item-loan-a",
    assurance: "source",
    review_state: null,
  },
] satisfies AgentEvidenceGraphHopWire[];

function auth(token: string, subject: string): AuthContextProps {
  return makeAuthValue({
    user: { access_token: token, profile: { sub: subject } } as AuthContextProps["user"],
  });
}

function renderAt(path: string, value = auth("owner-token", "user:pre016-owner")) {
  window.history.replaceState({}, "", path);
  return render(
    <AuthContext.Provider value={value}>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <App />
      </RuntimeConfigContext.Provider>
    </AuthContext.Provider>,
  );
}

const analyst = {
  agent_id: AGENT,
  version: 2,
  label: "Meridian Analyst",
  description: "Governed Meridian servicing analysis",
  owner_label: "Meridian Servicing Operations",
  visibility: "all authenticated",
  lifecycle: "active",
  allowed_strategies: ["agentic"],
  default_strategy: "agentic",
  model_policy_id: "agent.meridian-analyst.v2",
  tool_grants: [
    "read.skills.view",
    "read.knowledge.search",
    "read.knowledge.graph_path",
    "draft.email.create",
  ],
  knowledge_sources: [{ source_id: "meridian-servicing", tiers: ["t1", "t2"] }],
  skills: [
    {
      skill_id: "meridian-hardship-draft",
      version: 1,
      content_hash: "a".repeat(64),
    },
  ],
  tool_count: 4,
  knowledge_source_count: 1,
  skill_count: 1,
  budgets: {
    max_steps: 12,
    max_tool_calls: 10,
    max_input_tokens: 24000,
    max_output_tokens: 4000,
  },
  enforced_caps: {
    max_tool_calls: 10,
    max_model_requests: 11,
    wall_clock_seconds: 120,
    max_total_tokens: 28000,
    max_parked_writes: 1,
  },
  workflow: null,
  available: true,
  availability_reasons: [],
  config_hash: "1".repeat(64),
};

const hardship = {
  ...analyst,
  agent_id: HARDSHIP,
  version: 5,
  label: "Meridian Hardship Review",
  allowed_strategies: ["deterministic"],
  default_strategy: "deterministic",
  model_policy_id: "agent.meridian-hardship-review.v3",
  skills: [],
  skill_count: 0,
  // A workflow Agent runs no bounded agentic loop, so no ceilings are served.
  enforced_caps: null,
  workflow: {
    profile_id: "meridian-b1-disaster-hardship",
    version: 1,
    config_hash: "e".repeat(64),
  },
};

const conversation = {
  conversation_id: CONVERSATION,
  agent_id: HARDSHIP,
  title: "PRE-016 hardship control",
  created_at: NOW,
  updated_at: NOW,
  last_sequence: 1,
};

const run = {
  run_id: RUN,
  conversation_id: CONVERSATION,
  agent_id: HARDSHIP,
  agent_version: 5,
  sequence: 1,
  version: 3,
  predecessor_run_id: null,
  root_run_id: RUN,
  retry_ordinal: 0,
  display_label: "Run 1 · Deterministic · 09:00",
  strategy: "deterministic",
  status: "completed",
  output_verdict: "verified",
  model_policy_id: "agent.meridian-hardship-review.v3",
  model_request_mode: "explicit",
  context_snapshot_hash: "2".repeat(64),
  requested_model: "pre016-local-b",
  requested_model_lane: "private-vllm",
  served_model: "pre016-local-b-runtime",
  route_position: "unavailable",
  fallback_from: null,
  output_preview: "Eligible hardship population prepared.",
  config_hash: "3".repeat(64),
  capability_snapshot_hash: "4".repeat(64),
  created_at: NOW,
  updated_at: "2026-08-23T18:00:05Z",
  terminal_at: "2026-08-23T18:00:05Z",
  approval_id: null,
  agentic_plan: null,
  claims: [],
  claim_mapping_state: "complete",
  claims_truncated: false,
  // PRE-016A's Run-owned receipt. The current profile endpoint below is
  // deliberately rolled forward to v2; a thin client must render THESE bytes.
  execution_profile_receipt: {
    profile_receipt_id: "profile-receipt-pre016",
    run_id: RUN,
    attempt_id: "attempt-pre016",
    workflow_id: "meridian-b1-disaster-hardship",
    profile_version: 1,
    profile_hash: "e".repeat(64),
    input_schema: {
      type: "object",
      properties: { max_subjects: { type: "integer", minimum: 1, maximum: 5 } },
      required: ["max_subjects"],
      additionalProperties: false,
    },
    input_schema_hash: "d".repeat(64),
    typed_input_state: "available",
    typed_input: { max_subjects: 5 },
    ordered_plan: [
      {
        ordinal: 1,
        step_id: "original-step",
        kind: "governed-tool",
        action: "Original frozen customer enumeration",
        model_role: null,
      },
    ],
    ordered_plan_hash: "c".repeat(64),
    receipt_hash: "b".repeat(64),
    created_at: NOW,
  },
  skill_activation_receipt: null,
};

const evidence = {
  schema_version: 1,
  agent_id: AGENT,
  run_id: RUN,
  run_status: "completed",
  mapping_state: "complete",
  mapping_message: "Claim mapping complete",
  output: {
    verdict: "verified",
    state: "available",
    message: "Answer available",
    display: "Relief review required. Relief review required.",
  },
  claims: [
    {
      claim_id: "claim-a",
      claim_verdict: "verified",
      state: "available",
      message: "Claim available",
      display: "Relief review required.",
      evidence_id: "evidence-graph-a",
      association_hash: "a".repeat(64),
      verifier_event_id: "verifier-a",
      verifier_receipt_hash: "5".repeat(64),
    },
    {
      claim_id: "claim-b",
      claim_verdict: "verified",
      state: "available",
      message: "Claim available",
      display: "Relief review required.",
      evidence_id: "evidence-direct-b",
      association_hash: "b".repeat(64),
      verifier_event_id: "verifier-b",
      verifier_receipt_hash: "6".repeat(64),
    },
  ],
  // Reverse citation order is intentional: the console follows association,
  // never sentence/order inference.
  evidence: [
    {
      evidence_id: "evidence-direct-b",
      state: "available",
      message: "Direct supporting document",
      assurance: "direct",
      locator: { document_id: "document-b" },
    },
    {
      evidence_id: "evidence-graph-a",
      state: "available",
      message: "Governed customer to loan path",
      assurance: "inferred",
      locator: {
        entity_id: null,
        graph_path: graphPath,
      },
    },
  ],
  governance: {
    status: "complete",
    delegated_user_label: "PRE-016 Owner",
    agent_service_identity: "Meridian Analyst",
    connector_credential_owner_label: "Meridian M365",
    effective_scopes: ["read.knowledge.graph_path"],
    policy_environment: "pilot",
    tool_tier: 0,
    tool_decision: "allow",
    unavailable_reasons: [],
  },
};

/** The catalog + preflight reads the Agent WORKSPACE performs.
 *
 * New on 2026-08-27: `/agents/:id` grew a switcher rail (which lists) and a
 * Capabilities tab (which preflights), and `/ask` now redirects onto that
 * workspace. Both reads are principal-effective, so both go through the same
 * restricted-token refusal as everything else here. */
function workspaceReads() {
  return [
    http.get("*/api/v1/agents", ({ request }) => {
      if (request.headers.get("authorization") === "Bearer restricted-token") {
        return HttpResponse.json({ items: [], next_cursor: null });
      }
      return HttpResponse.json({ items: [analyst, hardship], next_cursor: null });
    }),
    http.get("*/api/v1/agents/:agentId/preflight", ({ params, request }) => {
      if (request.headers.get("authorization") === "Bearer restricted-token") {
        return HttpResponse.json({ detail: "not found" }, { status: 404 });
      }
      const agent = params.agentId === HARDSHIP ? hardship : analyst;
      return HttpResponse.json({
        agent_id: agent.agent_id,
        version: agent.version,
        definition_hash: agent.config_hash,
        available: true,
        availability_reasons: [],
        capabilities: {
          agent_version: agent.version,
          agent_definition_hash: agent.config_hash,
          tools: [],
          connectors: [],
          capability_hash: "c".repeat(64),
        },
        model: {
          task: agent.model_policy_id,
          allowed_strategies: agent.allowed_strategies,
          default_strategy: agent.default_strategy,
          max_steps: agent.budgets.max_steps,
          max_tool_calls: agent.budgets.max_tool_calls,
          max_input_tokens: agent.budgets.max_input_tokens,
          max_output_tokens: agent.budgets.max_output_tokens,
          candidates: [],
          default_model: null,
        },
      });
    }),
  ];
}

function ownerReads() {
  server.use(
    ...workspaceReads(),
    http.get("*/api/v1/me", () => HttpResponse.json({ ...PRINCIPAL, id: "user:pre016-owner" })),
    http.get("*/api/v1/agents/:agentId", ({ params, request }) => {
      if (request.headers.get("authorization") === "Bearer restricted-token") {
        return HttpResponse.json({ detail: "not found" }, { status: 404 });
      }
      return HttpResponse.json(params.agentId === HARDSHIP ? hardship : analyst);
    }),
    http.get("*/api/v1/agents/:agentId/conversations/:conversationId", ({ request }) => {
      if (request.headers.get("authorization") === "Bearer restricted-token") {
        return HttpResponse.json({ detail: "not found" }, { status: 404 });
      }
      return HttpResponse.json(conversation);
    }),
    http.get("*/api/v1/agents/:agentId/workflow-profile", () =>
      HttpResponse.json({
        agent_id: HARDSHIP,
        agent_version: 5,
        agent_config_hash: "9".repeat(64),
        strategy: "deterministic",
        workflow_id: "meridian-b1-disaster-hardship",
        workflow_version: 2,
        workflow_config_hash: "8".repeat(64),
        input_schema: {},
        fixed_inputs: {},
        fixed_plan: [
          {
            ordinal: 1,
            step_id: "rollover-step",
            kind: "governed-tool",
            action: "ROLLOVER-MUST-NOT-REWRITE-OLD-RUN",
            model_role: null,
          },
        ],
      }),
    ),
    http.get("*/api/v1/agents/:agentId/runs/:runId", ({ request }) => {
      if (request.headers.get("authorization") === "Bearer restricted-token") {
        return HttpResponse.json({ detail: "not found" }, { status: 404 });
      }
      return HttpResponse.json(run);
    }),
    http.get("*/api/v1/agents/:agentId/runs/:runId/events", () =>
      HttpResponse.json({
        items: [
          {
            event_id: "model-change",
            run_id: RUN,
            attempt_id: "attempt-pre016",
            sequence: 1,
            event_kind: "model.changed",
            event_status: "changed",
            parent_event_id: null,
            step_id: null,
            metadata: [
              { key: "before_requested_model", value: "pre016-local-a" },
              { key: "before_served_model", value: "pre016-local-a-runtime" },
              { key: "after_requested_model", value: "pre016-local-b" },
              { key: "after_authorized_model", value: "pre016-local-b" },
              { key: "context_snapshot_hash", value: "2".repeat(64) },
            ],
            detail_hash: "7".repeat(64),
            created_at: NOW,
          },
        ],
        next_cursor: null,
      }),
    ),
    http.get("*/api/v1/agents/:agentId/runs/:runId/evidence", () => HttpResponse.json(evidence)),
    // CR-B4: the run-list rail reads every sibling Run in the Conversation
    // (already-exposed client method, no new plumbing). This fixture's
    // Conversation carries exactly the one Run under test.
    http.get("*/api/v1/agents/:agentId/conversations/:conversationId/runs", () =>
      HttpResponse.json({ items: [run], next_cursor: null }),
    ),
  );
}

afterEach(() => {
  window.history.replaceState({}, "", "/");
});

describe("PRE-016 production Agent vertical", () => {
  it("reopens a deterministic Run from its frozen profile and model-boundary receipts", async () => {
    dismissTour();
    ownerReads();
    const user = userEvent.setup();
    renderAt(`/agents/${HARDSHIP}/runs/${RUN}`);

    expect(await screen.findByRole("heading", { name: run.display_label })).toBeInTheDocument();
    // Round 4 renamed the receipt's fields (agents/RunReceiptPane.tsx): the
    // docked pane and this deep-link route render the SAME component, so
    // "Requested model policy" is now "Requested model". The served value is
    // untouched, which is what this assertion is actually about.
    const requestedModel = screen.getByText("Requested model").closest("div");
    expect(requestedModel).not.toBeNull();
    expect(requestedModel).toHaveTextContent("pre016-local-b");
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    expect(screen.getByText(/Original frozen customer enumeration/)).toBeInTheDocument();
    expect(screen.queryByText(/ROLLOVER-MUST-NOT-REWRITE-OLD-RUN/)).toBeNull();
    expect(screen.queryByText(/agentic/i)).toBeNull();
    await user.click(screen.getByRole("tab", { name: "Timeline" }));
    expect(
      screen.getByRole("region", { name: "Model continuity receipt model-change" }),
    ).toHaveTextContent("pre016-local-b");
  });

  it("renders exact server claim associations, canonical Graph href, and clears on persona change", async () => {
    dismissTour();
    ownerReads();
    expect(analyst.tool_grants).toContain("read.knowledge.graph_path");
    expect(evidence.evidence[1]?.locator.entity_id).toBeNull();
    const restrictedRequests: string[] = [];
    server.use(
      http.get("*/api/v1/me", ({ request }) => {
        const authorization = request.headers.get("authorization") ?? "";
        if (authorization === "Bearer restricted-token") restrictedRequests.push(authorization);
        return HttpResponse.json({
          ...PRINCIPAL,
          id:
            authorization === "Bearer restricted-token"
              ? "user:pre016-restricted"
              : "user:pre016-owner",
        });
      }),
    );
    const user = userEvent.setup();
    const view = renderAt(`/agents/${AGENT}/runs/${RUN}`);
    await screen.findByRole("heading", { name: run.display_label });
    await user.click(screen.getByRole("button", { name: "Evidence" }));
    const drawer = await screen.findByRole("dialog", { name: "evidence drawer" });
    expect(within(drawer).getAllByRole("button", { name: "Relief review required." })).toHaveLength(
      2,
    );
    expect(within(drawer).getByText("Governed customer to loan path")).toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: "Open in Graph" })).toHaveAttribute(
      "href",
      `/knowledge/graph?mode=graph&agent=${AGENT}&run=${RUN}&evidence=evidence-graph-a`,
    );
    expect(within(drawer).queryByRole("list", { name: "Graph hop review states" })).toBeNull();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Evidence" })).toHaveFocus();

    view.rerender(
      <AuthContext.Provider value={auth("restricted-token", "user:pre016-restricted")}>
        <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
          <App />
        </RuntimeConfigContext.Provider>
      </AuthContext.Provider>,
    );
    expect(await screen.findByText("This Run isn’t available.")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("Relief review required");
    expect(restrictedRequests.length).toBeGreaterThan(0);
    expect(new Set(restrictedRequests)).toEqual(new Set(["Bearer restricted-token"]));
  });

  it("accepts canonical Ask IDs and opens the same Conversation and Run under Agents", async () => {
    dismissTour();
    const askSentinel = "PRE016-ASK-SENTINEL";
    server.use(
      // `/ask` redirects onto the Agent workspace, which mounts the real Ask
      // surface as this Agent's Chat tab. Same composer, same stream, same
      // canonical ids -- reached through the Agent that was always running it.
      ...workspaceReads(),
      http.get("*/api/v1/agents/:agentId", () => HttpResponse.json(analyst)),
      // The docked receipt reads the SAME canonical Run and Conversation the
      // Ask stream named, through the Agents endpoints.
      http.get("*/api/v1/agents/:agentId/runs/:runId", () => HttpResponse.json(run)),
      http.get("*/api/v1/agents/:agentId/conversations/:conversationId", () =>
        HttpResponse.json(conversation),
      ),
      http.get("*/api/v1/me", () => HttpResponse.json({ ...PRINCIPAL, id: "user:pre016-owner" })),
      http.get("*/api/v1/chat/capabilities", () => HttpResponse.json({ grounded: null })),
      http.get("*/api/v1/models/choices/ask", () =>
        HttpResponse.json({
          surface: "ask",
          task: "agent.meridian-analyst.v2",
          effective_default: "pre016-local-a",
          choices: [
            {
              model: "pre016-local-a",
              provider: "local",
              lane: "private-vllm",
              evidence: "serving-ready",
              observed_at: NOW,
            },
          ],
        }),
      ),
      http.get("*/api/v1/answer-ledger", () => HttpResponse.json({ question: null, events: [] })),
      http.post("*/api/v1/chat/sessions", () =>
        HttpResponse.json({ session_id: CONVERSATION, agent_name: AGENT }),
      ),
      http.post("*/api/v1/chat/sessions/:sessionId/messages", async ({ request }) => {
        const body = (await request.json()) as { prompt?: string };
        expect(body.prompt).toBe(askSentinel);
        const stream = [
          "event: accepted",
          `data: ${JSON.stringify({ agent_id: AGENT, conversation_id: CONVERSATION, run_id: RUN, attempt_id: "attempt-pre016", status: "submitted" })}`,
          "",
          "",
        ].join("\n");
        return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
      }),
    );
    renderAt("/ask");
    // The composer names the Agent it is talking to (chat/MessageInput.tsx
    // takes its aria-label from ChatPage's served `agentLabel`).
    await userEvent.type(await screen.findByLabelText(`Ask ${analyst.label}…`), askSentinel);
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    // OPENING THE RUN NO LONGER NAVIGATES. "Open Run" was a link to
    // /agents/:id/runs/:runId — "its so confusing opening a run and it takes me
    // to some other page". The canonical ids are still Ask's own: the receipt
    // action carries the run_id the stream accepted, and it fills the docked
    // pane beside the thread the reader is still in. Nothing about WHICH
    // Conversation and WHICH Run changed; only the frame did.
    await userEvent.click(await screen.findByRole("button", { name: RUN_RECEIPT_LABEL }));
    expect(screen.queryByText(/stream ended/i)).toBeNull();
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get("receipt")).toBe(RUN),
    );
    expect(window.location.pathname).toBe(`/agents/${AGENT}`);
    const pane = await screen.findByRole("dialog", { name: run.display_label });
    expect(within(pane).getByText(RUN)).toBeInTheDocument();
    expect(within(pane).getByText("Conversation").closest("div")).toHaveTextContent(
      conversation.title,
    );
  });
});

/* ---------------------------------------------------------------------------
 * THE GROUNDING CONTROL, ON THE RENDERED AGENT SCREEN.
 *
 * The owner: "where is the grounding toggle in the agent ask screen? it was
 * there and now gone" — the second regression, and both times every grounding
 * test was green, because they all exercised ChatPage standalone. These tests
 * render the REAL /agents/:id route through App, so they cover the surface the
 * owner is actually looking at: the workspace that mounts ChatPage as its
 * Conversation tab. The gate itself is real — grounding is bound at session
 * creation — so on an open conversation the control must be VISIBLE AND
 * DISABLED, never absent.
 * ------------------------------------------------------------------------ */

describe("the grounding control on the agent workspace composer", () => {
  function groundingReads() {
    server.use(
      ...workspaceReads(),
      http.get("*/api/v1/me", () => HttpResponse.json({ ...PRINCIPAL, id: "user:pre016-owner" })),
      http.get("*/api/v1/agents/:agentId", () => HttpResponse.json(analyst)),
      http.get("*/api/v1/agents/:agentId/conversations", () =>
        HttpResponse.json({ items: [{ ...conversation, agent_id: AGENT }], next_cursor: null }),
      ),
      http.get("*/api/v1/agents/:agentId/conversations/:conversationId/turns", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
      http.get("*/api/v1/chat/capabilities", () =>
        HttpResponse.json({ grounded: { agent_name: "served-grounding-mode" } }),
      ),
      http.get("*/api/v1/answer-ledger", () => HttpResponse.json({ question: null, events: [] })),
    );
  }

  it("offers the advertised grounding mode on a NEW conversation, as one compact switch", async () => {
    dismissTour();
    groundingReads();
    renderAt(`/agents/${AGENT}`);
    await screen.findByLabelText(`Ask ${analyst.label}…`);
    const control = await screen.findByRole("switch", { name: "Grounding" });
    expect(control).toBeEnabled();
    // The off state is NAMED — the word beside the track — so nobody has to
    // flip the control to learn what its resting position means.
    expect(control).toHaveAttribute("aria-checked", "false");
    expect(control).toHaveTextContent("Ungrounded");
    await userEvent.click(control);
    expect(control).toHaveAttribute("aria-checked", "true");
    expect(control).toHaveTextContent("Grounded");
  });

  it("keeps the control VISIBLE and disabled on an OPEN conversation — bound, not vanished", async () => {
    dismissTour();
    groundingReads();
    renderAt(`/agents/${AGENT}?conversation=${CONVERSATION}`);
    await screen.findByLabelText(`Ask ${analyst.label}…`);
    // The regression this test exists for: the control used to disappear the
    // moment a conversation existed, and the owner read that as a lost
    // feature. It now stays, disabled, showing what the conversation is bound
    // to, with the reason on hover.
    const control = await screen.findByRole("switch", { name: "Grounding" });
    expect(control).toBeDisabled();
    // A canonical read-back conversation was created on the ungrounded
    // default-Ask path (ne-api routers/chat.py), and the indicator says so.
    expect(control).toHaveAttribute("aria-checked", "false");
    expect(control).toHaveTextContent("Ungrounded");
    expect(control).toHaveAttribute("title", GROUNDING_BOUND_NOTE);
  });
});
