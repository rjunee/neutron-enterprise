// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE DETERMINISTIC COMPOSER IS A FORM, AND THESE ARE THE ASSERTIONS THAT MAKE
// THAT MORE THAN A CSS CLAIM.
//
// Every test below was mutation-checked: break the mechanism in RunForm.tsx,
// watch the named test go RED, restore it, watch it go GREEN. A test that only
// checks the form renders passes just as happily over a form with a prompt box
// bolted beside it, a budget the console rounded up, or a plan numbered by the
// array index — which are precisely the four failures PRE-DEMO-REHEARSAL-012
// names.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RUN_LABEL, RunForm, TYPED_INPUT_UNAVAILABLE } from "./RunForm";
import type { AgentDetailView, AgentWorkflowProfile } from "./types";

const DET_AGENT = "agt_01k3hardshipreview0000000000";
const AGENT_HASH = "d".repeat(64);
const WORKFLOW_HASH = "e".repeat(64);

const agent: AgentDetailView = {
  agent_id: DET_AGENT,
  version: 5,
  label: "Meridian Hardship Review",
  description: "Runs the fixed Meridian B1 disaster-hardship population review.",
  owner_label: "Meridian Servicing Operations",
  lifecycle: "active",
  allowed_strategies: ["deterministic"],
  default_strategy: "deterministic",
  model_policy_id: "agent.meridian-hardship-review.v3",
  tool_grants: ["read.kg.customers_in_counties"],
  knowledge_sources: [{ source_id: "meridian-servicing", tiers: ["t1", "t2"] }],
  skills: [],
  // The registry's REAL numbers, `max_output_tokens: 1` included.
  budgets: { max_steps: 1, max_tool_calls: 100, max_input_tokens: 8_210, max_output_tokens: 1 },
  enforced_caps: null,
  workflow: null,
  available: true,
  availability_reasons: [],
  config_hash: AGENT_HASH,
};

const profile: AgentWorkflowProfile = {
  agent_id: DET_AGENT,
  agent_version: agent.version,
  agent_config_hash: AGENT_HASH,
  strategy: "deterministic",
  workflow_id: "meridian-b1-disaster-hardship",
  workflow_version: 1,
  workflow_config_hash: WORKFLOW_HASH,
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
    {
      ordinal: 2,
      step_id: "screen",
      kind: "deterministic-analysis",
      action: "affected-active-due-no-prior-offer-no-repossession",
      model_role: null,
    },
  ],
};

/** A schema this console will not render: the supported set is bounded
 * integers and booleans, and a string property is outside it. */
const unreadableSchema: AgentWorkflowProfile = {
  ...profile,
  input_schema: {
    type: "object",
    properties: { note: { type: "string" } },
  },
};

function mount(props: Partial<Parameters<typeof RunForm>[0]> = {}) {
  const onSubmit = vi.fn();
  const view = render(
    <RunForm agent={agent} profile={profile} onSubmit={onSubmit} idPrefix="t" {...props} />,
  );
  return { ...view, onSubmit };
}

function form() {
  return screen.getByRole("region", { name: "Start a deterministic run" });
}

// ===========================================================================
// 1 — the composer is a form, and there is no free-text control on any branch.
// ===========================================================================

it("the deterministic composer contains no textarea", async () => {
  // NOT "the composer is hidden", NOT "the button is disabled" — there is no
  // free-text control in the DOM at all, on ANY branch, so no amount of CSS or
  // state can reveal one.
  const branches: Partial<Parameters<typeof RunForm>[0]>[] = [
    {}, // schema present
    { profile: unreadableSchema }, // schema unreadable
    { blockedReason: "the connector is unreachable." }, // blocked
    { busy: true }, // busy
    { disabled: true },
    { profile: { ...profile, fixed_plan: [], fixed_inputs: {} } }, // nothing served
  ];
  for (const branch of branches) {
    const { container, unmount } = mount(branch);
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
    expect(container.querySelectorAll("textarea")).toHaveLength(0);
    expect(document.querySelector("textarea")).toBeNull();
    unmount();
  }
});

// ===========================================================================
// 2 — the numbers are the server's, including the one that reads like a bug.
// ===========================================================================

it("served budgets render verbatim, max_output_tokens 1 included", () => {
  mount();
  const budgets = screen.getByLabelText("Budgets");
  // 1 step / 100 tool calls / 8,210 in / 1 out — exactly what the registry
  // serves. `max_output_tokens: 1` is not clamped, not hidden, not annotated.
  expect(budgets.textContent).toContain("1 step");
  expect(budgets.textContent).toContain("100 tool calls");
  expect(budgets.textContent).toContain("8,210 in");
  expect(budgets.textContent).toContain("1 out");
  const emphasised = within(budgets)
    .getAllByText(/^[\d,]+$/)
    .map((node) => node.textContent);
  expect(emphasised).toEqual(["1", "100", "8,210", "1"]);
});

// ===========================================================================
// 3 — an unreadable schema produces no form at all.
// ===========================================================================

it("typed input unavailable disables Run and guesses no form", () => {
  mount({ profile: unreadableSchema });
  const region = form();
  expect(within(region).getByText(TYPED_INPUT_UNAVAILABLE, { exact: false })).toBeInTheDocument();
  // No guessed widget of any kind — not a default integer, not a checkbox, not
  // a text field.
  expect(within(region).queryAllByRole("spinbutton")).toHaveLength(0);
  expect(within(region).queryAllByRole("checkbox")).toHaveLength(0);
  expect(within(region).queryAllByRole("textbox")).toHaveLength(0);
  expect(region.querySelectorAll("input")).toHaveLength(0);
  expect(within(region).getByRole("button", { name: RUN_LABEL })).toBeDisabled();
});

// ===========================================================================
// 4 — the plan is the server's, in the server's numbering.
// ===========================================================================

it("the fixed plan is the server's ordinals, in the server's order", () => {
  const offset: AgentWorkflowProfile = {
    ...profile,
    fixed_plan: [
      { ordinal: 3, step_id: "a", kind: "governed-tool", action: "read.kg.a", model_role: null },
      { ordinal: 4, step_id: "b", kind: "governed-tool", action: "read.kg.b", model_role: null },
      {
        ordinal: 7,
        step_id: "c",
        kind: "model-judgment",
        action: "judge.c",
        model_role: "hardship-classifier",
      },
    ],
  };
  const { container } = mount({ profile: offset });
  // A plan whose ordinals start at 3 renders 3, 4, 7 — never 1, 2, 3. The
  // server's numbering is a fact about the workflow, not a display index.
  const ordinals = [...container.querySelectorAll(".ag-plan__ord")].map((n) => n.textContent);
  expect(ordinals).toEqual(["3", "4", "7"]);
  const actions = [...container.querySelectorAll(".ag-plan__action")].map((n) => n.textContent);
  expect(actions).toEqual(["read.kg.a", "read.kg.b", "judge.c"]);
  // The served step count heads the column, and the model role rides in the
  // kind slot only when the server named one.
  expect(within(form()).getByText("Fixed plan · 3 steps")).toBeInTheDocument();
  const kinds = [...container.querySelectorAll(".ag-plan__kind")].map((n) => n.textContent);
  expect(kinds).toEqual(["governed-tool", "governed-tool", "model-judgment · hardship-classifier"]);
});

// ===========================================================================
// 5 — the plate is the workflow's identity.
// ===========================================================================

it("the plate is the workflow's identity, not the agent's", () => {
  const { container } = mount();
  const plate = container.querySelector(".ag-plate")!;
  expect(
    within(plate as HTMLElement).getByText("meridian-b1-disaster-hardship"),
  ).toBeInTheDocument();
  expect(
    within(plate as HTMLElement).getByText("agent.meridian-hardship-review.v3"),
  ).toBeInTheDocument();
  // The copyable hash is `workflow_config_hash` — the thing that pins the plan
  // and the frozen inputs — and NOT the Agent definition hash.
  expect(
    within(plate as HTMLElement).getByLabelText("Copy workflow profile hash"),
  ).toBeInTheDocument();
  expect(screen.getByTitle(WORKFLOW_HASH)).toBeInTheDocument();
  expect(screen.queryByTitle(AGENT_HASH)).toBeNull();
  expect(within(plate as HTMLElement).getByText(WORKFLOW_HASH.slice(0, 8))).toBeInTheDocument();
  expect(within(plate as HTMLElement).queryByText(AGENT_HASH.slice(0, 8))).toBeNull();
});

// ===========================================================================
// Guarantees carried forward from the panel this form replaces.
// ===========================================================================

describe("carried forward", () => {
  it("frozen inputs are readable values, headed by the version that froze them", () => {
    mount();
    const region = form();
    expect(within(region).getByText("Frozen inputs · at v1")).toBeInTheDocument();
    expect(within(region).getByText("Counties")).toBeInTheDocument();
    // An array is a list a reader can check against a county name, not a JSON
    // literal.
    expect(within(region).getByText("Columbia, Jefferson, Richmond")).toBeInTheDocument();
    expect(within(region).getByText("2026-08-18")).toBeInTheDocument();
  });

  it("says so, and infers nothing, when the profile served no plan and froze no inputs", () => {
    mount({ profile: { ...profile, fixed_plan: [], fixed_inputs: {} } });
    const region = form();
    expect(
      within(region).getByText("This profile served no ordered plan. Nothing here infers one."),
    ).toBeInTheDocument();
    expect(within(region).getByText("Fixed plan")).toBeInTheDocument();
    expect(within(region).queryByText(/Fixed plan · /)).toBeNull();
    expect(
      within(region).getByText(/This profile freezes no inputs of its own/),
    ).toBeInTheDocument();
  });

  it("Run submits the schema-ordered JSON body and nothing it re-derived", async () => {
    const user = userEvent.setup();
    const { onSubmit } = mount();
    const field = screen.getByRole("spinbutton", { name: "Max Subjects" });
    await user.clear(field);
    await user.type(field, "3");
    await user.click(screen.getByRole("button", { name: RUN_LABEL }));
    expect(onSubmit).toHaveBeenCalledWith('{"max_subjects":3}');
  });

  it("a blocked run says the server's reason and leaves Run disabled", () => {
    mount({ blockedReason: "the server does not authorize deterministic execution." });
    expect(
      screen.getByText(/Unavailable — the server does not authorize deterministic execution\./),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: RUN_LABEL })).toBeDisabled();
  });

  it("busy says Submitting… and cannot be pressed again", () => {
    mount({ busy: true });
    expect(screen.getByRole("button", { name: "Submitting…" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: RUN_LABEL })).toBeNull();
  });

  it("the lede names the profile as the thing that fixed the plan", () => {
    mount();
    expect(
      within(form()).getByText(
        /The plan below is fixed by the Workflow profile before the run starts\./,
      ),
    ).toBeInTheDocument();
    expect(within(form()).getByRole("heading", { name: "Start a run" })).toBeInTheDocument();
  });
});
