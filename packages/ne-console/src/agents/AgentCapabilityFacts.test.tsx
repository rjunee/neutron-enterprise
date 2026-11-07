// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE CAPABILITIES TAB, NE-CAPTAB — the table is the index, the drawer is the
// contract, and nothing on screen claims what the server did not state.
//
// The paired-assertion discipline from AgentWorkspace.test.tsx applies: every
// "the drawer shows X" is paired with "the table no longer shows Y", because
// the redesign's point is what LEFT the table (the 64-hex connection identity
// column, the mostly-"unknown" health column) as much as what the drawer adds.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { AgentToolCapabilityWire } from "../api/types";
import { AgentCapabilityFacts } from "./AgentCapabilityFacts";
import type { AgentCapabilityPreflight, AgentDetailView } from "./types";

const IDENTITY = `governed-local-t3:selected-release:${"9".repeat(64)}`;

const agent: AgentDetailView = {
  agent_id: "agt_01k3meridiananalyst000000000",
  version: 10,
  label: "Meridian Analyst",
  description: "Answers Meridian servicing questions.",
  owner_label: "Meridian Servicing Operations",
  lifecycle: "active",
  allowed_strategies: ["agentic"],
  default_strategy: "agentic",
  model_policy_id: "agent.meridian-analyst.v2",
  tool_grants: [
    "read.kg.payments_due_window",
    "read.knowledge.search",
    "read.kg.disaster_declarations",
  ],
  knowledge_sources: [{ source_id: "meridian-servicing", tiers: ["t1", "t2"] }],
  skills: [{ skill_id: "meridian-hardship-draft", version: 1, content_hash: "a".repeat(64) }],
  budgets: {
    max_steps: 40,
    max_tool_calls: 34,
    max_input_tokens: 1_219_607,
    max_output_tokens: 4_000,
  },
  enforced_caps: null,
  workflow: null,
  available: true,
  availability_reasons: [],
  config_hash: "d".repeat(64),
};

const paymentsTool: AgentToolCapabilityWire = {
  name: "read.kg.payments_due_window",
  label: "Read governed Meridian scheduled payments",
  read_write_class: "read",
  declared_tier: 0,
  effective_tier: 0,
  connection_identity_label: IDENTITY,
  health: "unknown",
  description: "Meridian payment-schedule rows in an inclusive validated date window.",
  arguments: [
    {
      name: "q_loan_id",
      type_label: "string",
      required: true,
      description: "Meridian auto-loan identifier, format mer-loan-NNN.",
      enum: null,
      pattern: "^mer-loan-[0-9]{3}$",
      max_length: 12,
      list_max: 16,
    },
    {
      name: "window_start",
      type_label: "date",
      required: true,
      description: "First due date of the window, YYYY-MM-DD. INCLUSIVE.",
      enum: null,
      pattern: null,
      max_length: null,
      list_max: null,
    },
  ],
  answer_columns: ["payment_id", "loan_id", "due_date", "amount_cents", "status"],
};

const searchTool: AgentToolCapabilityWire = {
  name: "read.knowledge.search",
  label: "Search Meridian servicing knowledge",
  read_write_class: "read",
  declared_tier: 0,
  effective_tier: 0,
  connection_identity_label: "m365:meridian-servicing",
  health: "healthy",
  description: "Search the governed knowledge base",
  arguments: [
    {
      name: "query",
      type_label: "string",
      required: true,
      description: null,
      enum: null,
      pattern: null,
      max_length: null,
      list_max: null,
    },
  ],
  answer_columns: null,
};

function preflightWith(tools: AgentToolCapabilityWire[]): AgentCapabilityPreflight {
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
      tools,
      connectors: [
        {
          connection_identity_label: IDENTITY,
          label: "Meridian governed local T3",
          kind: "governed-local-t3",
          credential_owner_label: "Credential-free selected governed local T3 release",
          health: "unknown",
          source_id: "meridian-governed-local-t3",
          tiers: ["t3"],
        },
        {
          connection_identity_label: "m365:meridian-servicing",
          label: "Meridian servicing Microsoft 365",
          kind: "microsoft-365",
          credential_owner_label: "Configured Microsoft 365 application",
          health: "healthy",
          source_id: "meridian-servicing",
          tiers: ["t1", "t2"],
        },
      ],
    },
    skills: [
      {
        skill_id: "meridian-hardship-draft",
        version: 1,
        content_hash: "a945555f".padEnd(64, "0"),
        description:
          "Draft a reviewable disaster-hardship outreach message from governed evidence.",
        requires_tools: ["draft.email.create", "read.knowledge.search"],
        acl_labels: ["department:servicing"],
        triggers: ["draft disaster hardship outreach"],
        category: "servicing",
        body: "## When to Use\nUse this only after governed evidence identifies eligibility.\n\n## Procedure\n1. Confirm the served evidence.\n2. Keep every statement within evidence.\n\n## Pitfalls\nDo not infer eligibility.",
      },
    ],
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
  };
}

function mount(tools: AgentToolCapabilityWire[] = [paymentsTool, searchTool]) {
  return render(
    <AgentCapabilityFacts
      agent={agent}
      preflight={{ phase: "ready", value: preflightWith(tools) }}
      onRetryPreflight={vi.fn()}
    />,
  );
}

describe("the tools table", () => {
  it("carries no connection-identity and no health column", () => {
    mount();
    const toolsRegion = screen.getByRole("region", { name: "Tools" });
    expect(within(toolsRegion).queryByRole("columnheader", { name: /connection/i })).toBeNull();
    expect(within(toolsRegion).queryByRole("columnheader", { name: /health/i })).toBeNull();
    // The 64-hex release pin no longer appears anywhere in the table…
    expect(within(toolsRegion).queryByText(new RegExp("9".repeat(64)))).toBeNull();
    // …and "unknown" is not rendered as a value anywhere on the page.
    expect(screen.queryByText("unknown")).toBeNull();
  });

  it("keeps the declared-but-ineligible row, which preflight alone would hide", () => {
    mount();
    expect(screen.getByText("Granted, not currently eligible for you")).toBeInTheDocument();
  });
});

describe("the tool contract drawer", () => {
  it("opens on the row and shows description, arguments and returned columns", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(
      screen.getByRole("button", { name: /Read governed Meridian scheduled payments/ }),
    );
    const drawer = screen.getByRole("dialog", {
      name: "Read governed Meridian scheduled payments details",
    });
    expect(
      within(drawer).getByText(
        "Meridian payment-schedule rows in an inclusive validated date window.",
      ),
    ).toBeInTheDocument();
    // The argument that produced the wrong live answer: its semantics are now
    // ON SCREEN, with the stated constraints beside it.
    expect(within(drawer).getByText("window_start")).toBeInTheDocument();
    expect(
      within(drawer).getByText("First due date of the window, YYYY-MM-DD. INCLUSIVE."),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("^mer-loan-[0-9]{3}$")).toBeInTheDocument();
    expect(
      within(drawer).getByText(/string · required · single value or a list of up to 16/),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("due_date")).toBeInTheDocument();
    // The connection identity lives HERE now, joined to its human label.
    expect(within(drawer).getByText("Meridian governed local T3")).toBeInTheDocument();
    expect(within(drawer).getByText(IDENTITY)).toBeInTheDocument();
    // This tool's health is unstated, so the drawer says NOTHING about health.
    expect(within(drawer).queryByText(/health/i)).toBeNull();
    expect(within(drawer).queryByText("unknown")).toBeNull();
  });

  it("names a health state only when the producer stated one", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: /Search Meridian servicing knowledge/ }));
    const drawer = screen.getByRole("dialog", {
      name: "Search Meridian servicing knowledge details",
    });
    expect(within(drawer).getByText("healthy")).toBeInTheDocument();
    // An unstated argument description is an honest gap, not a placeholder.
    expect(within(drawer).getByText("query")).toBeInTheDocument();
    expect(within(drawer).queryByText(/no description/i)).toBeNull();
    // No declared answer columns → no Returns section at all.
    expect(within(drawer).queryByText("Returns")).toBeNull();
  });

  it("distinguishes 'takes no arguments' from 'arguments not stated'", async () => {
    const user = userEvent.setup();
    const { unmount } = mount([{ ...searchTool, arguments: [] }]);
    await user.click(screen.getByRole("button", { name: /Search Meridian servicing knowledge/ }));
    expect(screen.getByText("This tool declares no arguments.")).toBeInTheDocument();
    unmount();
    mount([{ ...searchTool, arguments: null }]);
    await user.click(screen.getByRole("button", { name: /Search Meridian servicing knowledge/ }));
    const drawer = screen.getByRole("dialog");
    expect(within(drawer).queryByText("Arguments")).toBeNull();
    expect(within(drawer).queryByText("This tool declares no arguments.")).toBeNull();
  });

  it("closes on Escape and returns focus to the row that opened it", async () => {
    const user = userEvent.setup();
    mount();
    const opener = screen.getByRole("button", {
      name: /Read governed Meridian scheduled payments/,
    });
    await user.click(opener);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    await vi.waitFor(() => expect(opener).toHaveFocus());
  });
});

describe("the sections below the tools table", () => {
  it("shows connection health only where stated, and keeps the identity code inside the row", () => {
    mount();
    const connections = screen.getByRole("region", { name: "Connections" });
    expect(within(connections).queryByRole("columnheader", { name: /health/i })).toBeNull();
    expect(within(connections).getByText("healthy")).toBeInTheDocument();
    expect(within(connections).queryByText("unknown")).toBeNull();
    expect(within(connections).getByText(IDENTITY)).toBeInTheDocument();
  });

  it("renders knowledge sources and skills as labeled tables, and budgets formatted", () => {
    mount();
    const knowledge = screen.getByRole("region", { name: "Knowledge sources" });
    expect(within(knowledge).getByRole("columnheader", { name: "Source" })).toBeInTheDocument();
    const skills = screen.getByRole("region", { name: "Skills" });
    expect(within(skills).getByRole("columnheader", { name: "Version" })).toBeInTheDocument();
    expect(screen.getByText("1,219,607")).toBeInTheDocument();
  });
});

describe("the skill viewer", () => {
  it("opens the pinned skill and renders its procedure, pin and access boundary", async () => {
    const user = userEvent.setup();
    mount();
    const skills = screen.getByRole("region", { name: "Skills" });
    await user.click(within(skills).getByRole("button", { name: "meridian-hardship-draft" }));
    const drawer = screen.getByRole("dialog", { name: "meridian-hardship-draft skill details" });
    // The pinned version + hash, and the guarantee stated in one line.
    expect(within(drawer).getByText("1")).toBeInTheDocument();
    expect(within(drawer).getByText(/if the file changes, the pin breaks/i)).toBeInTheDocument();
    // The access boundary — ACL label + required tools.
    expect(within(drawer).getByText("department:servicing")).toBeInTheDocument();
    expect(within(drawer).getByText("draft.email.create")).toBeInTheDocument();
    // The procedure body renders as real structure, NOT literal "##".
    expect(
      within(drawer).getByRole("heading", { name: "Procedure", level: 3 }),
    ).toBeInTheDocument();
    expect(
      within(drawer).getByRole("heading", { name: "When to Use", level: 4 }),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("Confirm the served evidence.")).toBeInTheDocument();
    expect(within(drawer).queryByText(/^##/)).toBeNull();
  });

  it("shows the pin but no open control when the skill body is not visible to the reader", () => {
    // skills = [] means the producer answered "no skill detail visible to you".
    render(
      <AgentCapabilityFacts
        agent={agent}
        preflight={{ phase: "ready", value: { ...preflightWith([paymentsTool]), skills: [] } }}
        onRetryPreflight={vi.fn()}
      />,
    );
    const skills = screen.getByRole("region", { name: "Skills" });
    // The pin (id + hash) is still shown from the catalog…
    expect(within(skills).getByText("meridian-hardship-draft")).toBeInTheDocument();
    // …but there is no button to open a body the server did not disclose.
    expect(within(skills).queryByRole("button", { name: "meridian-hardship-draft" })).toBeNull();
  });
});
