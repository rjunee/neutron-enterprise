// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-WORKFLOWS — the screen that gives the workflow engine a door.
//
// The measured starting point was no route at all: the engine has been on the
// wire since PR #385 and was reachable only by curl. So the assertions below
// are about the things whose absence was the defect — the permission contract
// on screen BEFORE a run, an honest wait over a call that blocks for half a
// minute, the run's own steps/proposals/counts rendered verbatim, and the two
// links that make this a system (the trace, and the approval it parked) — and
// about the things whose PRESENCE would be the defect: a count inferred from a
// key the server did not send, a link on a proposal that parked no approval, a
// console-authored refusal standing in for the server's, and a result carried
// across a change of selection so one workflow's steps caption another's name.
//
// EVERY WIRE FIXTURE BELOW IS AN ANNOTATED `WorkflowRunView` / `WorkflowView`,
// never a cast (vacuous-guards §4 row 19): re-declaring any of the optional
// fields required is then a `tsc` error ON THE FIXTURE, which is the only
// mechanical thing standing between this file and the #14/#19 defect a third
// time.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  AgenticWorkflowRunStatusView,
  AgenticWorkflowRunView,
  WorkflowRunView,
  WorkflowView,
} from "../api/types";
import type { Access, Entitlements, Surface } from "../auth/entitlements";
import {
  GO_KEYS,
  NAV_SECTIONS,
  railSections,
  routeVisibility,
  shortcutRoutes,
} from "../rail/navSections";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { TRACE_ASSEMBLY } from "../test/mocks/traceFixtures";
import { WorkflowsPage } from "./WorkflowsPage";

const WORKFLOWS_CSS = readFileSync(
  join(fileURLToPath(import.meta.url), "..", "workflows.css"),
  "utf-8",
).replace(/\/\*[\s\S]*?\*\//g, "");
const TRACE_CSS = readFileSync(
  join(fileURLToPath(import.meta.url), "..", "..", "trace", "trace.css"),
  "utf-8",
).replace(/\/\*[\s\S]*?\*\//g, "");
const WORKFLOWS_SOURCE = readFileSync(
  join(fileURLToPath(import.meta.url), "..", "WorkflowsPage.tsx"),
  "utf-8",
);

// ===========================================================================
// The wire, as the server serves it
// ===========================================================================

/**
 * TWO workflows whose READ sets DIFFER from each other and are DISJOINT from
 * their write set. Every discriminating assertion below rests on that: a panel
 * that renders writes where reads belong, or the first row's contract where the
 * second row's belongs, fails on a value, not on a length.
 */
const WORKFLOWS: WorkflowView[] = [
  {
    workflow_id: "unresolved-ticket-review",
    title: "Unresolved ticket review",
    description: "Ranks accounts by unresolved volume and parks one CRM note per account.",
    reads: ["read.kg.tickets_by_status", "read.knowledge.search", "read.crm.account_lookup"],
    writes: ["write.internal.crm.note"],
  },
  {
    workflow_id: "recurring-issue-sweep",
    title: "Recurring issue sweep",
    description: "Finds a ticket subject filed verbatim by more than one account.",
    reads: ["read.kg.tickets_by_status", "read.crm.account_lookup"],
    writes: ["write.internal.crm.note"],
  },
];

/**
 * A whole run. The three counts are PAIRWISE DISTINCT (41 / 7 / 2) so a count
 * wired to the wrong field is a failed value comparison rather than a coincidence,
 * and the rows deliberately exercise the absent shapes the table must render as
 * the em dash: a step with `tool: null`, a step with an EMPTY `detail`, and a
 * proposal that parked NOTHING (no record, no approval).
 */
const RUN: WorkflowRunView = {
  // The server states its disclosure decision explicitly (015A0-ENFORCEMENT);
  // the console fails closed on a run that does not.
  disclosure_status: "available",
  workflow_id: "unresolved-ticket-review",
  trace_id: "0f9c2b7e41d8",
  steps: [
    {
      step: 1,
      kind: "read",
      tool: "read.kg.tickets_by_status",
      outcome: "ok",
      detail: { rows: 41 },
    },
    { step: 2, kind: "draft", tool: null, outcome: "ok", detail: { body_chars: 512 } },
    { step: 3, kind: "write", tool: "write.internal.crm.note", outcome: "parked", detail: {} },
  ],
  proposals: [
    {
      subject_ref: "ticket-cluster-118",
      crm_record_id: "0011a00001AbCdEf",
      approval_request_id: "a2bf6cea-2f1d-4a77-9c31-5f0e2b8d4411",
      outcome: "parked",
      body_chars: 512,
      evidence_refs: ["chunk-9021", "chunk-9044"],
    },
    {
      subject_ref: "ticket-cluster-204",
      crm_record_id: null,
      approval_request_id: null,
      outcome: "refused",
      reason: "no resolvable CRM record",
      body_chars: 0,
      evidence_refs: [],
    },
  ],
  read_row_count: 41,
  subjects_considered: 7,
  subjects_selected: 2,
  result: null,
};

const B3_WORKFLOW: WorkflowView = {
  workflow_id: "meridian-b3-fabrication-refusal",
  title: "Meridian B3 — fabrication-pressure refusal",
  description: "Refuses the requested late-credit total while calculating the answerable control.",
  reads: ["read.kg.disputes_directory"],
  writes: [],
};

const B3_RUN: WorkflowRunView = {
  // The server states its disclosure decision explicitly (015A0-ENFORCEMENT);
  // the console fails closed on a run that does not.
  disclosure_status: "available",
  workflow_id: B3_WORKFLOW.workflow_id,
  trace_id: "trace-b3-deterministic",
  steps: [
    {
      step: 1,
      kind: "read",
      tool: "read.kg.disputes_directory",
      outcome: "served-complete",
      detail: { rows: 6 },
    },
    {
      step: 2,
      kind: "analyse",
      tool: null,
      outcome: "refused",
      detail: { credited_rows: 2, control_amount_cents: 7750 },
    },
  ],
  proposals: [],
  read_row_count: 6,
  subjects_considered: 6,
  subjects_selected: 0,
  result: {
    outcome: "refused",
    summary:
      "Refuse to calculate a late-credit total: provisional-credit issue dates are absent from the source records, so timeliness cannot be determined.",
    findings: [],
    missing_fields: ["reg_e_disputes.provisional_credit_issue_date"],
    policy_artifact_id: null,
    policy_version: null,
    control_outcome: "complete",
    control_amount_cents: 7750,
    control_evidence_refs: [
      "urn:ne:meridian:dispute:mer-dsp-003",
      "urn:ne:meridian:dispute:mer-dsp-005",
    ],
  },
};

const B1_WORKFLOW: WorkflowView = {
  workflow_id: "meridian-b1-disaster-hardship",
  title: "Meridian B1 — disaster-hardship population",
  description: "Identifies the actionable population from complete governed reads.",
  reads: [
    "read.kg.customers_in_counties",
    "read.kg.autoloans_for_customer",
    "read.kg.payments_due_window",
    "read.kg.servicing_notes_for_loan",
  ],
  writes: [],
};

const B1_RUN: WorkflowRunView = {
  // The server states its disclosure decision explicitly (015A0-ENFORCEMENT);
  // the console fails closed on a run that does not.
  disclosure_status: "available",
  workflow_id: B1_WORKFLOW.workflow_id,
  trace_id: "trace-b1-deterministic",
  steps: [],
  proposals: [],
  read_row_count: 11,
  subjects_considered: 3,
  subjects_selected: 1,
  result: {
    outcome: "complete",
    summary:
      "Identified the actionable DR-4830 disaster-hardship population from complete verified reads.",
    findings: [
      {
        subject_ref: "loan-77",
        related_ref: "customer-12",
        outcome: "actionable",
        evidence_refs: ["customer-12", "loan-77", "payment-99"],
        detail: {
          county: "Columbia",
          loan_status: "active",
          next_scheduled_due_date: "2026-08-28",
        },
      },
    ],
    missing_fields: [],
    policy_artifact_id: null,
    policy_version: null,
    control_outcome: null,
    control_amount_cents: null,
    control_evidence_refs: [],
  },
};

/**
 * A SERVER OLDER THAN THIS CONSOLE, with every defaulted key genuinely ABSENT —
 * not null, not `[]`. This is the failing input #14 and #19 both missed twice by
 * serving the full key set in every fixture. `steps`, `proposals` and all three
 * counts are gone; only the two fields the server declares WITHOUT a default
 * remain.
 */
const OLD_WIRE_RUN: WorkflowRunView = {
  // The server states its disclosure decision explicitly (015A0-ENFORCEMENT);
  // the console fails closed on a run that does not.
  disclosure_status: "available",
  workflow_id: "unresolved-ticket-review",
  trace_id: "0f9c2b7e41d8",
};

/**
 * The same skew ONE LEVEL DOWN: the arrays arrived, the per-row defaulted keys
 * did not. `steps` reaches `detail`, `proposals` reaches `evidence_refs` and
 * `body_chars` — dereferences a row-absent fixture never touches.
 */
const OLD_WIRE_ROWS: WorkflowRunView = {
  // The server states its disclosure decision explicitly (015A0-ENFORCEMENT);
  // the console fails closed on a run that does not.
  disclosure_status: "available",
  workflow_id: "unresolved-ticket-review",
  trace_id: "0f9c2b7e41d8",
  steps: [{ step: 1, kind: "read", outcome: "ok" }],
  proposals: [{ subject_ref: "ticket-cluster-118", outcome: "refused" }],
};

const AGENTIC_RUN: AgenticWorkflowRunView = {
  // The server states its disclosure decision explicitly (015A0-ENFORCEMENT);
  // the console fails closed on a run that does not.
  disclosure_status: "available",
  run_id: "agentic-run-17",
  workflow_id: "unresolved-ticket-review",
  trace_id: "trace-agentic-17",
  principal_id: "user:meridian-ops",
  outcome: "complete",
  model_planned: true,
  answer: "The dates required for the requested late-credit total were not served, so I refuse it.",
  steps: [
    {
      step: 1,
      tool: "read.kg.disputes_directory",
      stated_intent: "Establish the complete dispute evidence directory.",
      outcome: "served",
      served_row_ids: [
        "urn:ne:meridian:dispute:mer-dsp-003",
        "urn:ne:meridian:dispute:mer-dsp-005",
      ],
      evidence_refs: [],
      principal_id: "user:meridian-ops",
    },
  ],
  served_row_ids: ["urn:ne:meridian:dispute:mer-dsp-003", "urn:ne:meridian:dispute:mer-dsp-005"],
  budgets: {
    max_tool_calls: 24,
    token_budget: 262144,
    wall_clock_seconds: 120,
    max_parked_writes: 5,
    requests_used: 2,
    tool_calls_used: 1,
    tokens_used: 99,
    exhausted_cap: null,
  },
};

// ===========================================================================
// Harness
// ===========================================================================

function serveList(workflows: WorkflowView[] = WORKFLOWS) {
  server.use(http.get("*/api/v1/workflows", () => HttpResponse.json(workflows)));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

/** Serve the run, and hand back every request URL the page actually sent. */
function serveRun(view: WorkflowRunView = RUN): string[] {
  const urls: string[] = [];
  server.use(
    http.post("*/api/v1/workflows/:id/run", ({ request }) => {
      urls.push(request.url);
      return HttpResponse.json(view);
    }),
  );
  return urls;
}

function serveAgenticRun(view: AgenticWorkflowRunView = AGENTIC_RUN) {
  const urls: string[] = [];
  const bodies: unknown[] = [];
  const ticket: AgenticWorkflowRunStatusView = {
    run_id: view.run_id,
    workflow_id: view.workflow_id,
    trace_id: view.trace_id,
    principal_id: view.principal_id,
    status: "running",
    result: null,
    error_class: null,
  };
  server.use(
    http.post("*/api/v1/agentic-workflows/:id/start", async ({ request }) => {
      urls.push(request.url);
      bodies.push(await request.json());
      return HttpResponse.json(ticket, { status: 202 });
    }),
    http.get("*/api/v1/agentic-workflows/runs/:runId", () =>
      HttpResponse.json({ ...ticket, status: view.outcome, result: view }),
    ),
    http.get("*/api/v1/trace/:traceId", () =>
      HttpResponse.json({ detail: "trace not found" }, { status: 404 }),
    ),
  );
  return { urls, bodies };
}

function setup() {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={["/workflows"]}>
        <WorkflowsPage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/** The workflow rows, by the title in their first cell. */
function workflowTitles(): string[] {
  const table = screen.getByRole("table", { name: "Workflows" });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
}

async function listed() {
  await waitFor(() => expect(workflowTitles()).toHaveLength(WORKFLOWS.length));
}

/** The tool names in one named grant list, in the order they render. */
function grant(name: "Reads" | "Writes"): string[] {
  return within(screen.getByRole("list", { name }))
    .getAllByRole("listitem")
    .map((item) => item.textContent ?? "");
}

/** Every cell of every body row of one named table, in render order. */
function rowsOf(label: string): string[][] {
  const table = screen.getByRole("table", { name: label });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) =>
      within(row)
        .getAllByRole("cell")
        .map((cell) => cell.textContent ?? ""),
    );
}

/** The run's three counts as ordered [label, value] pairs. */
function counts(): string[][] {
  return [...document.querySelectorAll(".ne-workflows__count")].map((node) => [
    node.querySelector("dt")?.textContent ?? "",
    node.querySelector("dd")?.textContent ?? "",
  ]);
}

async function runIt(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Run workflow" }));
}

async function runAgentic(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("combobox", { name: "Run model" });
  await user.click(screen.getByRole("button", { name: "Run agentic" }));
}

/** What the toolbar states about the size of the window, verbatim. */
function toolbarCount(): string {
  return document.querySelector(".ne-toolbar__count")?.textContent ?? "";
}

function runButton(): HTMLElement {
  return screen.getByRole("button", { name: "Run workflow" });
}

afterEach(() => {
  vi.useRealTimers();
});

// ===========================================================================
// 1 — the list, and the contract that renders before anything runs
// ===========================================================================

/** The fault card's own line and detail, addressed the way the reader meets
 *  them: the biggest text first. Reading the CARD rather than the call is how
 *  the untrue headline was found in the first place — a test that reaches only
 *  for the sub-sentence cannot see the largest text on the screen. */
function faultCard() {
  const card = document.querySelector(".ne-state--error");
  if (card === null) throw new Error("no fault card rendered");
  return {
    card,
    line: card.querySelector(".ne-state__line")?.textContent ?? null,
    detail: card.querySelector(".ne-state__detail")?.textContent ?? null,
  };
}

describe("the workflows this deployment offers", () => {
  it("lists each served workflow with its title and its description", async () => {
    serveList();
    setup();
    await listed();
    expect(rowsOf("Workflows")).toEqual([
      [WORKFLOWS[0]!.title, WORKFLOWS[0]!.description],
      [WORKFLOWS[1]!.title, WORKFLOWS[1]!.description],
    ]);
  });

  it("names the served reads and the served writes, in their own lists, with no run yet", async () => {
    serveList();
    setup();
    await listed();
    // THE MONEY SHOT: the permission contract, by value, before the operator
    // has pressed anything. Reads and writes are disjoint sets here, so a panel
    // that rendered one where the other belongs fails on the value.
    expect(grant("Reads")).toEqual(WORKFLOWS[0]!.reads);
    expect(grant("Writes")).toEqual(WORKFLOWS[0]!.writes);
    // …and nothing about a run is on screen, because none has happened.
    expect(screen.queryByRole("table", { name: "Run steps" })).not.toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Proposed writes" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "View trace" })).not.toBeInTheDocument();
  });

  it("counts the window the server served, and states NO count for one that failed", async () => {
    // Both directions in one test: `rows` is empty on a failed read exactly as
    // it is before one lands, so a count gated on "loading" alone reports a zero
    // nobody measured. The served leg is the control — a mutant that simply
    // never renders a count passes the first half and fails the second.
    server.use(http.get("*/api/v1/workflows", () => HttpResponse.error()));
    setup();
    await screen.findByText(/Couldn’t load workflows\./);
    expect(toolbarCount()).toBe("");
    cleanup();

    serveList();
    setup();
    await listed();
    expect(toolbarCount()).toBe(`${WORKFLOWS.length} workflows`);
  });

  it("renders the SELECTED workflow's contract when the second row is opened", async () => {
    serveList();
    const user = setup();
    await listed();
    await user.click(screen.getByText(WORKFLOWS[1]!.title));
    expect(screen.getByRole("heading", { name: WORKFLOWS[1]!.title })).toBeInTheDocument();
    // The second workflow reads TWO tools, not the first's three — a panel
    // pinned to row 0 fails here on the value it renders.
    expect(grant("Reads")).toEqual(WORKFLOWS[1]!.reads);
  });
});

// ===========================================================================
// 2 — the run: the wait, the wire, and the result
// ===========================================================================

describe("running one workflow", () => {
  it("synchronously clears prior workflow, run, model, and action state on client replacement", async () => {
    const adaWorkflow: WorkflowView = {
      ...WORKFLOWS[0]!,
      title: "ADA-WORKFLOW-SENTINEL",
      modes: [{ mode: "agentic", status: "available" }],
    };
    const bobWorkflow: WorkflowView = {
      ...WORKFLOWS[1]!,
      title: "BOB-WORKFLOW-CONTROL",
      modes: [{ mode: "agentic", status: "available" }],
    };
    const bobList = deferred<void>();
    const bobModels = deferred<void>();
    const adaRun = deferred<void>();
    let listRequests = 0;
    let modelRequests = 0;
    let runRequests = 0;
    server.use(
      http.get("*/api/v1/workflows", async () => {
        listRequests += 1;
        if (listRequests === 1) return HttpResponse.json([adaWorkflow]);
        await bobList.promise;
        return HttpResponse.json([bobWorkflow]);
      }),
      http.get("*/api/v1/models/choices/agentic-workflow", async () => {
        modelRequests += 1;
        if (modelRequests > 1) await bobModels.promise;
        const ada = modelRequests === 1;
        const model = ada ? "ADA-MODEL-SENTINEL" : "BOB-MODEL-CONTROL";
        return HttpResponse.json({
          surface: "agentic-workflow",
          task: "agentic-workflow",
          effective_default: model,
          choices: [
            {
              model,
              provider: ada ? "ada-provider" : "bob-provider",
              lane: "private-vllm",
              evidence: "served",
              observed_at: "2026-08-21T12:00:00Z",
            },
          ],
        });
      }),
      http.post("*/api/v1/workflows/:id/run", async () => {
        runRequests += 1;
        await adaRun.promise;
        return HttpResponse.json({
          ...RUN,
          trace_id: "ADA-RUN-TRACE-SENTINEL",
          steps: [{ ...RUN.steps![0]!, detail: { sentinel: "ADA-RUN-BYTES" } }],
        });
      }),
    );

    const view = render(
      <AuthStub>
        <MemoryRouter initialEntries={["/workflows"]}>
          <WorkflowsPage />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(
      await screen.findByRole("heading", { name: "ADA-WORKFLOW-SENTINEL" }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: /ADA-MODEL-SENTINEL/ })).toBeInTheDocument();
    const staleRunButton = screen.getByRole("button", { name: "Run deterministic" });
    await userEvent.click(staleRunButton);
    expect(runRequests).toBe(1);
    expect(await screen.findByText(/Running 1 subject/)).toBeInTheDocument();

    // AuthStub creates a replacement ApiClient during this render. Every byte
    // and callback still owned by the prior client must be gated immediately,
    // before effects and before Bob's list/model reads settle.
    view.rerender(
      <AuthStub>
        <MemoryRouter initialEntries={["/workflows"]}>
          <WorkflowsPage />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(screen.queryByText("ADA-WORKFLOW-SENTINEL")).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /ADA-MODEL-SENTINEL/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Running 1 subject/)).not.toBeInTheDocument();
    fireEvent.click(staleRunButton);
    expect(runRequests).toBe(1);

    await act(async () => {
      adaRun.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByText("ADA-RUN-BYTES")).not.toBeInTheDocument();
    expect(screen.queryByText("ADA-RUN-TRACE-SENTINEL")).not.toBeInTheDocument();

    await act(async () => {
      bobList.resolve();
      bobModels.resolve();
    });
    expect(
      await screen.findByRole("heading", { name: "BOB-WORKFLOW-CONTROL" }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: /BOB-MODEL-CONTROL/ })).toBeInTheDocument();
    expect(screen.queryByText("ADA-WORKFLOW-SENTINEL")).not.toBeInTheDocument();
    expect(screen.queryByText("ADA-RUN-BYTES")).not.toBeInTheDocument();
  });

  it("selects an available Anthropic alias and carries only that alias into the agentic run", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [{ mode: "agentic", status: "available" }],
      },
      WORKFLOWS[1]!,
    ]);
    server.use(
      http.get("*/api/v1/models/choices/agentic-workflow", () =>
        HttpResponse.json({
          surface: "agentic-workflow",
          task: "agentic-workflow",
          effective_default: "private-qwen",
          choices: [
            {
              model: "private-qwen",
              provider: "vllm",
              lane: "private-vllm",
              evidence: "private-serving-probe",
              observed_at: "2026-08-18T18:00:00Z",
            },
            {
              model: "frontier-sonnet",
              provider: "anthropic",
              lane: "frontier-api",
              evidence: "frontier-provider-attempt",
              observed_at: "2026-08-18T18:00:01Z",
            },
          ],
        }),
      ),
    );
    const { bodies } = serveAgenticRun();
    const user = setup();
    await listed();
    const picker = await screen.findByRole("combobox", { name: "Run model" });
    await user.selectOptions(picker, "frontier-sonnet");
    await user.click(screen.getByRole("button", { name: "Run agentic" }));

    await screen.findByRole("region", { name: "Stated intents" });
    expect(bodies).toEqual([{ requested_model: "frontier-sonnet" }]);
  });

  it("runs the served agentic mode and renders its intents, evidence, caps, and answer verbatim", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [
          {
            mode: "deterministic",
            status: "available",
            endpoint: "/v1/workflows/unresolved-ticket-review/run",
          },
          {
            mode: "agentic",
            status: "available",
            endpoint: "/v1/agentic-workflows/unresolved-ticket-review/run",
          },
        ],
      },
      WORKFLOWS[1]!,
    ]);
    const { urls, bodies } = serveAgenticRun();
    const user = setup();
    await listed();
    expect(screen.getByRole("button", { name: "Run deterministic" })).toBeInTheDocument();
    await runAgentic(user);

    await screen.findByRole("region", { name: "Stated intents" });
    expect(new URL(urls[0]!).pathname).toBe(
      "/api/v1/agentic-workflows/unresolved-ticket-review/start",
    );
    expect(bodies).toEqual([{ requested_model: "private-default" }]);
    const terminal = screen.getByRole("region", { name: "Terminal result" });
    // The server's enum, in words. The raw `complete` token is not on screen.
    expect(terminal).toHaveTextContent("Outcome: Completed");
    expect(terminal).not.toHaveTextContent(AGENTIC_RUN.outcome);
    expect(screen.getByRole("region", { name: "Final answer" })).toHaveTextContent(
      AGENTIC_RUN.answer!,
    );
    const intents = screen.getByRole("region", { name: "Stated intents" });
    const intent = within(intents).getByRole("listitem");
    expect(within(intent).getByText("Step 1")).toBeInTheDocument();
    expect(intent).toHaveTextContent("Establish the complete dispute evidence directory.");
    expect(intent).toHaveTextContent("read.kg.disputes_directory");
    expect(intent).toHaveTextContent("served");
    expect(counts()).toEqual([
      ["Tool calls", "1 / 24"],
      ["Tokens", "99 / 262,144"],
      ["Wall-clock cap", "120s"],
      ["Parked-write cap", "5"],
    ]);
    expect(screen.getByRole("region", { name: "Served evidence identities" })).toHaveTextContent(
      "urn:ne:meridian:dispute:mer-dsp-003",
    );
    expect(screen.getByRole("link", { name: "View full trace" })).toHaveAttribute(
      "href",
      "/trace/trace-agentic-17",
    );
  });

  it("polls the existing redacted trace while running and withholds a VERIFIED claim", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [{ mode: "agentic", status: "available" }],
      },
      WORKFLOWS[1]!,
    ]);
    const ticket: AgenticWorkflowRunStatusView = {
      run_id: "live-run-opaque",
      workflow_id: WORKFLOWS[0]!.workflow_id,
      trace_id: TRACE_ASSEMBLY.trace_id,
      principal_id: "user:meridian-ops",
      status: "running",
      result: null,
      error_class: null,
    };
    server.use(
      http.post("*/api/v1/agentic-workflows/:id/start", () =>
        HttpResponse.json(ticket, { status: 202 }),
      ),
      http.get("*/api/v1/agentic-workflows/runs/:runId", () => HttpResponse.json(ticket)),
      http.get("*/api/v1/trace/:traceId", () => HttpResponse.json(TRACE_ASSEMBLY)),
    );

    const user = setup();
    await listed();
    await runAgentic(user);

    expect(await screen.findByRole("region", { name: "Live governed trace" })).toBeInTheDocument();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();
    expect(screen.getByLabelText("Verification unavailable while run is active")).toHaveTextContent(
      "—",
    );
    // The fetched partial assembly happens to report a currently valid chain.
    // The run screen must still not turn that intermediate fact into a terminal
    // VERIFIED badge or a misleading count.
    expect(screen.queryByText("VALID")).not.toBeInTheDocument();
    expect(screen.queryByText(/records verified/i)).not.toBeInTheDocument();
    expect(screen.getByText("Redaction")).toBeInTheDocument();

    // SpanTree is an interactive tree. Its first row must really select and
    // disclose exact served fields; a null selectedId/no-op onSelect mutant
    // leaves no dialog and fails here for both keyboard and pointer semantics.
    const firstEvent = (await screen.findAllByRole("treeitem"))[0]!;
    expect(firstEvent).toHaveAttribute("aria-selected", "false");
    firstEvent.focus();
    await user.keyboard("{Enter}");
    const detail = screen.getByRole("dialog");
    expect(firstEvent).toHaveAttribute("aria-selected", "true");
    expect(detail).toHaveTextContent("agent://root");
    expect(detail).toHaveTextContent("policy ok");
    expect(screen.queryByRole("button", { name: /approve|deny|reject/i })).not.toBeInTheDocument();
  });

  it("keeps exact served trace, stated intents, and terminal result as separate ordered records", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [{ mode: "agentic", status: "available" }],
      },
      WORKFLOWS[1]!,
    ]);
    const view: AgenticWorkflowRunView = {
      ...AGENTIC_RUN,
      steps: [
        {
          step: 7,
          tool: "read.kg.first",
          stated_intent: "Read the first served directory.",
          outcome: "served-first",
          served_row_ids: ["urn:ne:first"],
          evidence_refs: [],
          principal_id: AGENTIC_RUN.principal_id,
        },
        {
          step: 3,
          tool: "read.kg.second",
          stated_intent: "Read the second served directory.",
          outcome: "served-second",
          served_row_ids: ["urn:ne:second"],
          evidence_refs: ["urn:ne:first"],
          principal_id: AGENTIC_RUN.principal_id,
        },
      ],
      budgets: { ...AGENTIC_RUN.budgets, tool_calls_used: 2 },
    };
    const ticket: AgenticWorkflowRunStatusView = {
      run_id: view.run_id,
      workflow_id: view.workflow_id,
      trace_id: view.trace_id,
      principal_id: view.principal_id,
      status: "running",
      result: null,
      error_class: null,
    };
    server.use(
      http.post("*/api/v1/agentic-workflows/:id/start", () =>
        HttpResponse.json(ticket, { status: 202 }),
      ),
      http.get("*/api/v1/agentic-workflows/runs/:runId", () =>
        HttpResponse.json({ ...ticket, status: "complete", result: view }),
      ),
      http.get("*/api/v1/trace/:traceId", () => HttpResponse.json(TRACE_ASSEMBLY)),
    );

    const user = setup();
    await listed();
    await runAgentic(user);

    const activity = await screen.findByRole("region", { name: "Governed trace" });
    const intents = screen.getByRole("region", { name: "Stated intents" });
    const terminal = screen.getByRole("region", { name: "Terminal result" });
    expect(
      activity.compareDocumentPosition(intents) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      intents.compareDocumentPosition(terminal) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    const intentItems = within(intents).getAllByRole("listitem");
    expect(within(intentItems[0]!).getByText("Step 7")).toBeInTheDocument();
    expect(intentItems[0]).toHaveTextContent("Read the first served directory.");
    expect(within(intentItems[1]!).getByText("Step 3")).toBeInTheDocument();
    expect(intentItems[1]).toHaveTextContent("Read the second served directory.");
    expect(within(intents).queryByText("Step 1")).not.toBeInTheDocument();
    expect(within(intents).queryByText("Step 2")).not.toBeInTheDocument();
    expect(within(activity).getByRole("tree", { name: "Spans" })).toBeInTheDocument();
    expect(within(terminal).getByRole("region", { name: "Final answer" })).toHaveTextContent(
      AGENTIC_RUN.answer!,
    );
  });

  it("keeps a terminal trace 503 distinct from trace absence and offers a truthful retry read", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [{ mode: "agentic", status: "available" }],
      },
      WORKFLOWS[1]!,
    ]);
    const ticket: AgenticWorkflowRunStatusView = {
      run_id: AGENTIC_RUN.run_id,
      workflow_id: AGENTIC_RUN.workflow_id,
      trace_id: AGENTIC_RUN.trace_id,
      principal_id: AGENTIC_RUN.principal_id,
      status: "running",
      result: null,
      error_class: null,
    };
    server.use(
      http.post("*/api/v1/agentic-workflows/:id/start", () =>
        HttpResponse.json(ticket, { status: 202 }),
      ),
      http.get("*/api/v1/agentic-workflows/runs/:runId", () =>
        HttpResponse.json({ ...ticket, status: "complete", result: AGENTIC_RUN }),
      ),
      http.get("*/api/v1/trace/:traceId", () =>
        HttpResponse.json({ detail: "temporarily unavailable" }, { status: 503 }),
      ),
    );

    const user = setup();
    await listed();
    await runAgentic(user);

    const activity = await screen.findByRole("region", { name: "Governed trace" });
    expect(within(activity).getByRole("status")).toHaveTextContent(
      "The terminal result is available, but the final trace refresh failed.",
    );
    expect(
      within(activity).getByRole("link", { name: "Open full trace to retry" }),
    ).toHaveAttribute("href", "/trace/trace-agentic-17");
    expect(
      within(activity).queryByText(
        "No governed trace assembly was served before this run completed.",
      ),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Final answer" })).toHaveTextContent(
      AGENTIC_RUN.answer!,
    );
  });

  it("renders a terminal trace 404 as truthful absence without a failure claim", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [{ mode: "agentic", status: "available" }],
      },
      WORKFLOWS[1]!,
    ]);
    serveAgenticRun();

    const user = setup();
    await listed();
    await runAgentic(user);

    const activity = await screen.findByRole("region", { name: "Governed trace" });
    expect(
      within(activity).getByText(
        "No governed trace assembly was served before this run completed.",
      ),
    ).toBeInTheDocument();
    expect(within(activity).queryByText(/final trace refresh failed/i)).not.toBeInTheDocument();
    expect(
      within(activity).queryByRole("link", { name: "Open full trace to retry" }),
    ).not.toBeInTheDocument();
  });

  it("pins desktop legibility and the production-composed 390px trace grid", () => {
    expect(WORKFLOWS_CSS).toContain(
      ".ne-workflows__intent {\n  display: grid;\n  grid-template-columns: minmax(0, 1fr) minmax(280px, 0.55fr);",
    );
    expect(WORKFLOWS_CSS).not.toContain("counter-reset");
    expect(WORKFLOWS_CSS).not.toContain("counter-increment");
    expect(WORKFLOWS_CSS).toContain("@media (max-width: 640px)");
    expect(WORKFLOWS_SOURCE.indexOf('import "../trace/trace.css"')).toBeLessThan(
      WORKFLOWS_SOURCE.indexOf('import "./workflows.css"'),
    );
    expect(TRACE_CSS).toContain(
      ".ne-trace__span {\n    grid-template-columns: 24px minmax(0, 1fr);",
    );
    const composed = `${TRACE_CSS}\n${WORKFLOWS_CSS}`;
    const spanGrids = [
      ...composed.matchAll(
        /(?:\.ne-trace__span|\.ne-workflows__trace \.ne-trace__span)\s*\{[^}]*grid-template-columns:\s*([^;]+);/g,
      ),
    ].map((match) => match[1]?.trim());
    expect(spanGrids.at(-1)).toBe("24px minmax(0, 1fr)");
    expect(WORKFLOWS_CSS).not.toContain("92px");
    expect(WORKFLOWS_CSS).toContain(".ne-workflows .ne-detail {\n  max-width: 100vw;");
    expect(WORKFLOWS_CSS).toContain(
      ".ne-workflows__tool {\n  font: var(--type-cell-mono);\n  color: var(--text);\n  min-width: 0;\n  overflow-wrap: anywhere;",
    );
    expect(WORKFLOWS_CSS).toContain(
      ".ne-workflows__trace .ne-trace__spanmeta {\n    display: none;",
    );
  });

  it("shows transient poll failures while keeping the workflow run non-terminal", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [{ mode: "agentic", status: "available" }],
      },
      WORKFLOWS[1]!,
    ]);
    const ticket: AgenticWorkflowRunStatusView = {
      run_id: "live-run-retrying",
      workflow_id: WORKFLOWS[0]!.workflow_id,
      trace_id: "live-trace-retrying",
      principal_id: "user:meridian-ops",
      status: "running",
      result: null,
      error_class: null,
    };
    server.use(
      http.post("*/api/v1/agentic-workflows/:id/start", () =>
        HttpResponse.json(ticket, { status: 202 }),
      ),
      http.get("*/api/v1/agentic-workflows/runs/:runId", () =>
        HttpResponse.json({ detail: "temporary" }, { status: 503 }),
      ),
      http.get("*/api/v1/trace/:traceId", () =>
        HttpResponse.json({ detail: "temporary" }, { status: 503 }),
      ),
    );

    const user = setup();
    await listed();
    await runAgentic(user);

    expect(
      await screen.findByText(
        "Run status refresh failed; the run is still active and polling will retry.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Live trace refresh failed; the run is still active and polling will retry.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run agentic" })).toBeDisabled();
    expect(screen.queryByText("AgenticRunFailed")).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // NEVER A FABRICATED ERROR IDENTITY, NEVER THE WIRE'S VOCABULARY RAW, AND
  // NEVER A HEADLINE THAT IS NOT TRUE OF THE SITUATION.
  //
  // The defects these pin: a failed background run showed the string
  // "AgenticRunFailed" — a class name no server in this repo emits, printed
  // under "Couldn't save. Try again." as though the deployment had raised it —
  // and a stopped run reported itself as `budget-exhausted (tool-calls)`, which
  // names a real ceiling in a vocabulary the person reading it does not have.
  //
  // AND THE HEADLINE ITSELF. Removing the fabricated class left the honest
  // sentence as SECONDARY text under `<SaveError>`'s built-in line — "Couldn't
  // save. Try again.", the largest text in the card — over a run that saved
  // nothing and offered a retry of a request that had already been accepted.
  // The two tests written for that fix asserted only the sub-sentence, so
  // nothing pinned the line above it and the untrue claim survived directly
  // over its own fix. EVERY TEST BELOW PINS THE LINE, NOT ONLY THE DETAIL.
  // -------------------------------------------------------------------------

  /** A background run whose ticket goes terminal as `failed`. */
  function serveFailedAgenticRun(errorClass: string | null) {
    const ticket: AgenticWorkflowRunStatusView = {
      run_id: "live-run-failed",
      workflow_id: WORKFLOWS[0]!.workflow_id,
      trace_id: "live-trace-failed",
      principal_id: "user:meridian-ops",
      status: "running",
      result: null,
      error_class: null,
    };
    server.use(
      http.post("*/api/v1/agentic-workflows/:id/start", () =>
        HttpResponse.json(ticket, { status: 202 }),
      ),
      http.get("*/api/v1/agentic-workflows/runs/:runId", () =>
        HttpResponse.json({ ...ticket, status: "failed", error_class: errorClass }),
      ),
      http.get("*/api/v1/trace/:traceId", () =>
        HttpResponse.json({ detail: "trace not found" }, { status: 404 }),
      ),
    );
  }

  it("names NO error class of its own when a background run fails without one", async () => {
    serveList([
      { ...WORKFLOWS[0]!, modes: [{ mode: "agentic", status: "available" }] },
      WORKFLOWS[1]!,
    ]);
    serveFailedAgenticRun(null);
    const user = setup();
    await listed();
    await runAgentic(user);

    expect(await screen.findByText("The server did not name a cause.")).toBeInTheDocument();
    // The invented identity is gone, not renamed.
    expect(screen.queryByText(/AgenticRunFailed/)).toBeNull();
    // AND THE LINE ABOVE IT IS THE RUN'S OWN FACT. The whole card, biggest
    // text first: a fabricated identity removed from the detail is worth
    // nothing if the headline over it still describes a different event.
    expect(faultCard().line).toBe("The agentic run failed.");
  });

  it("keeps a REAL error class the server sent, as detail behind a sentence", async () => {
    serveList([
      { ...WORKFLOWS[0]!, modes: [{ mode: "agentic", status: "available" }] },
      WORKFLOWS[1]!,
    ]);
    serveFailedAgenticRun("AgentOutputContractError");
    const user = setup();
    await listed();
    await runAgentic(user);

    // Kept verbatim — it is the only identity the server recorded, and the
    // reader forwarding this to an engineer needs it — but never alone.
    expect(
      await screen.findByText("The server reported AgentOutputContractError."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/AgenticRunFailed/)).toBeNull();
    expect(faultCard()).toMatchObject({
      line: "The agentic run failed.",
      detail: "The server reported AgentOutputContractError.",
    });
  });

  it("HEADLINES a failed run as a failed run — never as a failed save", async () => {
    serveList([
      { ...WORKFLOWS[0]!, modes: [{ mode: "agentic", status: "available" }] },
      WORKFLOWS[1]!,
    ]);
    serveFailedAgenticRun(null);
    const user = setup();
    await listed();
    await runAgentic(user);

    await screen.findByText("The server did not name a cause.");

    // THE CARD IS FOUND BY WHAT IT SAYS, NOT BY THE TONE THIS TEST ASSERTS.
    // `faultCard()` selects on `.ne-state--error`, so every tone assertion made
    // against the node it returns is guaranteed by the selector that produced
    // it — and a card re-toned into a neutral notice would not be found at all,
    // leaving those assertions to pass on a node that no longer exists. Reached
    // from the screen through the sentence, the block that answers here is
    // whichever block the screen actually drew.
    const card = screen.getByText("The agentic run failed.").closest<HTMLElement>(".ne-state");
    if (card === null) throw new Error("the failure line is not inside a state block");

    // The line is the fact, and it is the LINE — not the detail, not a caption.
    expect(card.querySelector(".ne-state__line")?.textContent).toBe("The agentic run failed.");
    expect(card.querySelector(".ne-state__detail")?.textContent).toBe(
      "The server did not name a cause.",
    );

    // NOTHING WAS BEING SAVED. The console never submitted a write here: the
    // start request was accepted, and the server then reported the run itself
    // terminal. A card claiming a failed save is a claim about an event that
    // did not occur.
    expect(screen.queryByText(/Couldn.t save/)).toBeNull();

    // AND NO ACTION IS OFFERED THAT THE READER CANNOT USEFULLY TAKE. The run
    // already ran and spent its budget; there is no request to re-send, so
    // "Try again" would point at nothing.
    expect(card.textContent).not.toMatch(/Try again/);
    expect(within(card).queryByRole("button")).toBeNull();

    // STILL A FAULT. A failed run is a real failure and is not softened into a
    // neutral notice to make the sentence fit: the fault block, the fault
    // colour and the assertive role are the primitive's, unchanged. Draw this
    // sentence with `Empty`, `Refused` or `PolicyDenied` instead and the block
    // carrying it is the block found above — with a different tone class, and
    // without the assertive role on the two that are not faults.
    expect(card).toHaveClass("ne-state", "ne-state--error");
    expect(card).toHaveAttribute("role", "alert");
    expect(card.className).not.toMatch(/ne-state--(refused|empty|denied|loading)/);
  });

  it("names WHICH ceiling stopped a tool-call-exhausted run, in words", async () => {
    serveList([
      { ...WORKFLOWS[0]!, modes: [{ mode: "agentic", status: "available" }] },
      WORKFLOWS[1]!,
    ]);
    serveAgenticRun({
      ...AGENTIC_RUN,
      outcome: "budget-exhausted",
      answer: null,
      budgets: { ...AGENTIC_RUN.budgets, exhausted_cap: "tool-calls" },
    });
    const user = setup();
    await listed();
    await runAgentic(user);

    expect(
      await screen.findByText(
        "Stopped at a limit — it reached the limit on tool calls. The partial answer was withheld.",
      ),
    ).toBeInTheDocument();
    const terminal = screen.getByRole("region", { name: "Terminal result" });
    expect(terminal).toHaveTextContent("Outcome: Stopped at a limit");
    expect(screen.queryByText(/budget-exhausted/)).toBeNull();
  });

  it("a context-exhausted run names the context limit, not the generic budget word", async () => {
    serveList([
      { ...WORKFLOWS[0]!, modes: [{ mode: "agentic", status: "available" }] },
      WORKFLOWS[1]!,
    ]);
    serveAgenticRun({
      ...AGENTIC_RUN,
      outcome: "context-exhausted",
      answer: null,
      budgets: { ...AGENTIC_RUN.budgets, exhausted_cap: "context" },
    });
    const user = setup();
    await listed();
    await runAgentic(user);

    expect(
      await screen.findByText(
        "Stopped at the context limit — it reached the context-window limit. The partial answer was withheld.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/context-exhausted/)).toBeNull();
  });

  it("does not call the DERIVED model-request ceiling a configured one", async () => {
    // `runner.py::model_request_limit` derives this cap from the tool-call cap
    // (`max_tool_calls + 1 + _OUTPUT_RETRIES`) and warns in the same docstring
    // that naming it as an operator ceiling is "a terminal naming the wrong
    // cause, since no ceiling the operator set was actually reached". It is
    // also the cap `runner.py` falls through to when a UsageLimitExceeded
    // matches none of the others, so a run that hit it is exactly the run that
    // must not be sent hunting for a setting nobody made.
    serveList([
      { ...WORKFLOWS[0]!, modes: [{ mode: "agentic", status: "available" }] },
      WORKFLOWS[1]!,
    ]);
    serveAgenticRun({
      ...AGENTIC_RUN,
      outcome: "budget-exhausted",
      answer: null,
      budgets: { ...AGENTIC_RUN.budgets, exhausted_cap: "requests" },
    });
    const user = setup();
    await listed();
    await runAgentic(user);

    expect(
      await screen.findByText(
        "Stopped at a limit — it reached the model-request ceiling, which the runtime derives " +
          "from the tool-call limit. The partial answer was withheld.",
      ),
    ).toBeInTheDocument();
    // Still cut short, still withheld — and never described as configured.
    const terminal = screen.getByRole("region", { name: "Terminal result" });
    expect(terminal.textContent).not.toMatch(/configured/i);
    expect(screen.queryByRole("region", { name: "Final answer" })).not.toBeInTheDocument();
    expect(screen.queryByText(/budget-exhausted/)).toBeNull();
  });

  it("never renders an INHERITED property or a nameless cap as the ceiling", async () => {
    // `exhausted_cap` is an open `string` off the wire and `CAP_WORDS` is an
    // object, so a bare `CAP_WORDS[cap] ?? cap` answered "constructor" with
    // `Object`'s constructor FUNCTION and "__proto__" with `Object.prototype`
    // — both non-nullish, so `??` never fired, and a function typed to return
    // a string returned neither. Interpolated into the stopped-run sentence
    // that reads "it reached function Object() { [native code] }".
    //
    // (Only the all-lowercase members of Object.prototype are reachable here;
    // the camelCase ones — toString, valueOf, hasOwnProperty — are not cap
    // tokens this wire uses. These two are.)
    //
    // The nameless cases are the other half: a fallback of the raw value keeps
    // the server's word, which is right, but a cap of spaces or of one
    // separator has no word to keep and left the sentence subject-less.
    async function sentenceFor(cap: string): Promise<string> {
      serveList([
        { ...WORKFLOWS[0]!, modes: [{ mode: "agentic", status: "available" }] },
        WORKFLOWS[1]!,
      ]);
      serveAgenticRun({
        ...AGENTIC_RUN,
        outcome: "budget-exhausted",
        answer: null,
        budgets: { ...AGENTIC_RUN.budgets, exhausted_cap: cap },
      });
      const user = setup();
      await listed();
      await runAgentic(user);
      const stopped = await screen.findByText(/^Stopped at a limit/);
      return stopped.textContent ?? "";
    }

    for (const cap of ["constructor", "__proto__"]) {
      const sentence = await sentenceFor(cap);
      expect(typeof sentence).toBe("string");
      expect(sentence.trim()).not.toBe("");
      // The server's own token, kept verbatim — and NOT the prototype member
      // that shares its name.
      expect(sentence).toBe(
        `Stopped at a limit — it reached ${cap}. The partial answer was withheld.`,
      );
      expect(sentence).not.toMatch(/native code|\[object Object\]|function Object/);
      cleanup();
    }

    for (const cap of ["   ", "—", "·"]) {
      const sentence = await sentenceFor(cap);
      expect(sentence).toBe(
        "Stopped at a limit — it reached an unnamed ceiling. The partial answer was withheld.",
      );
      cleanup();
    }
  });

  it("prints a ceiling it was never taught VERBATIM rather than dropping it", async () => {
    // A named ceiling nobody glossed is still a fact about why the run stopped;
    // losing it would be worse than showing the server's own token.
    serveList([
      { ...WORKFLOWS[0]!, modes: [{ mode: "agentic", status: "available" }] },
      WORKFLOWS[1]!,
    ]);
    serveAgenticRun({
      ...AGENTIC_RUN,
      outcome: "budget-exhausted",
      answer: null,
      budgets: { ...AGENTIC_RUN.budgets, exhausted_cap: "sub-agent-depth" },
    });
    const user = setup();
    await listed();
    await runAgentic(user);

    expect(
      await screen.findByText(
        "Stopped at a limit — it reached sub-agent-depth. The partial answer was withheld.",
      ),
    ).toBeInTheDocument();
  });

  /**
   * The wire with an outcome OUTSIDE the union — served as a plain object
   * rather than through `serveAgenticRun`'s annotated fixture, because the
   * point is that the annotation is a claim NOTHING CHECKS: `client.ts` casts
   * the parsed JSON straight into `AgenticWorkflowRunView`. A fixture that
   * type-checked here could not ask this question at all.
   */
  function serveWireOutcome(outcome: string) {
    const view = { ...AGENTIC_RUN, outcome, answer: null };
    const ticket = {
      run_id: view.run_id,
      workflow_id: view.workflow_id,
      trace_id: view.trace_id,
      principal_id: view.principal_id,
      status: outcome,
      result: null,
      error_class: null,
    };
    server.use(
      http.post("*/api/v1/agentic-workflows/:id/start", () =>
        HttpResponse.json(ticket, { status: 202 }),
      ),
      http.get("*/api/v1/agentic-workflows/runs/:runId", () =>
        HttpResponse.json({ ...ticket, result: view }),
      ),
      http.get("*/api/v1/trace/:traceId", () =>
        HttpResponse.json({ detail: "trace not found" }, { status: 404 }),
      ),
    );
  }

  /** Run one wire outcome and hand back the Terminal result section. */
  async function terminalFor(outcome: string): Promise<HTMLElement> {
    serveList([
      { ...WORKFLOWS[0]!, modes: [{ mode: "agentic", status: "available" }] },
      WORKFLOWS[1]!,
    ]);
    serveWireOutcome(outcome);
    const user = setup();
    await listed();
    await runAgentic(user);
    return await screen.findByRole("region", { name: "Terminal result" });
  }

  it("never renders a JavaScript internal or a blank as the terminal outcome", async () => {
    // `OUTCOME_WORDS` is typed by the server's closed union, and nothing on the
    // way in enforces it: `client.ts` casts the response body. So a bare
    // `OUTCOME_WORDS[outcome]` answered "constructor" with `Object`'s
    // constructor FUNCTION and "__proto__" with `Object.prototype`, and printed
    // them under the heading "Terminal result" — the same defect `capWords`
    // already carries a guard for, one rung up.
    //
    // The nameless cases are the other half: the fallback keeps the server's
    // own word, which is right, but an outcome of spaces or of a lone separator
    // has no word to keep and left the line reading "Outcome:" with nothing
    // after it.
    for (const outcome of ["constructor", "__proto__"]) {
      const terminal = await terminalFor(outcome);
      // The server's own token, kept verbatim — NOT the prototype member that
      // shares its name.
      expect(terminal).toHaveTextContent(`Outcome: ${outcome}`);
      expect(terminal).toHaveTextContent(`${outcome}. The partial answer was withheld.`);
      expect(terminal.textContent).not.toMatch(/native code|\[object Object\]|function Object/);
      cleanup();
    }

    for (const outcome of ["   ", "·"]) {
      const terminal = await terminalFor(outcome);
      expect(terminal).toHaveTextContent("Outcome: The server did not name the outcome");
      expect(terminal).toHaveTextContent(
        "The server did not name the outcome. The partial answer was withheld.",
      );
      expect(terminal.textContent).not.toMatch(/undefined/);
      cleanup();
    }
  });

  it("prints an outcome it was never taught VERBATIM rather than 'undefined'", async () => {
    // How a run ended is a fact about the run; a console that has not been
    // taught the word for it still has to say what the server said. The
    // partial answer is withheld either way — an outcome this screen cannot
    // read as "complete" is not one it may present a final answer for.
    const terminal = await terminalFor("cancelled-by-operator");
    expect(terminal).toHaveTextContent("Outcome: cancelled-by-operator");
    expect(terminal).toHaveTextContent("cancelled-by-operator. The partial answer was withheld.");
    expect(screen.queryByRole("region", { name: "Final answer" })).not.toBeInTheDocument();
  });

  it("withholds a cap-exhausted agentic partial answer instead of presenting it as complete", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [{ mode: "agentic", status: "available" }],
      },
      WORKFLOWS[1]!,
    ]);
    serveAgenticRun({
      ...AGENTIC_RUN,
      outcome: "budget-exhausted",
      answer: null,
      budgets: { ...AGENTIC_RUN.budgets, exhausted_cap: "wall-clock" },
    });
    const user = setup();
    await listed();
    await runAgentic(user);

    // THE CEILING IS NOT SOFTENED — "budget-exhausted" means the run met a
    // limit and was cut short there, and the gloss says exactly that. What
    // leaves is the wire token and the bare cap name, which told an operator
    // nothing about WHICH limit stopped the run.
    expect(
      await screen.findByText(
        "Stopped at a limit — it reached the time limit. The partial answer was withheld.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/budget-exhausted/)).toBeNull();
    expect(screen.queryByText(/wall-clock/)).toBeNull();
    expect(screen.queryByRole("region", { name: "Final answer" })).not.toBeInTheDocument();
  });

  it("sends the subject count the operator chose, on the selected workflow's own path", async () => {
    serveList();
    const urls = serveRun();
    const user = setup();
    await listed();
    const subjects = screen.getByLabelText("Subjects");
    await user.clear(subjects);
    await user.type(subjects, "3");
    await runIt(user);
    await screen.findByRole("table", { name: "Run steps" });
    expect(urls).toHaveLength(1);
    const sent = new URL(urls[0]!);
    expect(sent.pathname).toBe("/api/v1/workflows/unresolved-ticket-review/run");
    expect(sent.searchParams.get("max_subjects")).toBe("3");
  });

  it("never sends a subject count the operator did not type", async () => {
    // `type="number"` ACCEPTS "1.5", and a prefix parser turns it into a
    // runnable 1 — the box reads 1.5, the sweep runs 1, and nothing on screen
    // shows the difference. A count that disagrees with its own field is the
    // same defect class as a count the server never sent.
    serveList();
    const urls = serveRun();
    const user = setup();
    await listed();
    const subjects = screen.getByLabelText("Subjects");
    await user.clear(subjects);
    await user.type(subjects, "1.5");
    expect(subjects).toHaveValue(1.5);
    expect(runButton()).toBeDisabled();
    // The empty box is not a zero and not a one — it is nothing to run.
    // (Trailing garbage is NOT asserted here: `type="number"` refuses it at the
    // DOM, so it never reaches the parser. `Number` rejects it regardless.)
    await user.clear(subjects);
    expect(runButton()).toBeDisabled();
    // Nothing was sent by any of it.
    expect(urls).toHaveLength(0);

    // THE CONTROL: a whole number the box can show IS sent, verbatim — so this
    // is not passing because the control is simply never armed.
    await user.type(subjects, "2");
    await runIt(user);
    await screen.findByRole("table", { name: "Run steps" });
    expect(urls).toHaveLength(1);
    expect(new URL(urls[0]!).searchParams.get("max_subjects")).toBe("2");
  });

  it("states what is happening and how long it takes while the call is in flight", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTimeAsync });
    serveList();
    // The run is held open so the IN-FLIGHT state is a real state and not a
    // frame that happened to be caught between two resolved promises.
    const gate: { release: () => void } = { release: () => {} };
    const held = new Promise<void>((resolve) => {
      gate.release = resolve;
    });
    server.use(
      http.post("*/api/v1/workflows/:id/run", async () => {
        await held;
        return HttpResponse.json(RUN);
      }),
    );
    render(
      <AuthStub>
        <MemoryRouter initialEntries={["/workflows"]}>
          <WorkflowsPage />
        </MemoryRouter>
      </AuthStub>,
    );
    await waitFor(() => expect(workflowTitles()).toHaveLength(WORKFLOWS.length));
    await runIt(user);

    // The wait is NAMED, not a spinner: the subject count, the seconds actually
    // elapsed, and the stated expectation — in a live region, so it is
    // announced rather than merely drawn.
    const notice = await screen.findByText("Running 1 subject — 0s elapsed");
    expect(notice).toHaveAttribute("role", "status");
    expect(screen.getByText(/Expect about 30 seconds per subject\./)).toBeInTheDocument();
    // The control is closed while it runs — a second press cannot start a second run.
    expect(screen.getByRole("button", { name: "Run workflow" })).toBeDisabled();
    // Nothing about the result exists yet.
    expect(screen.queryByRole("table", { name: "Run steps" })).not.toBeInTheDocument();

    // The counter is LIVE, not a frozen zero: it moves with the clock.
    await vi.advanceTimersByTimeAsync(4_000);
    await waitFor(() => expect(notice.textContent).toBe("Running 1 subject — 4s elapsed"));

    gate.release();
    await screen.findByRole("table", { name: "Run steps" });
    expect(screen.queryByText(/^Running /)).not.toBeInTheDocument();
  });

  it("renders the served steps in the served order, with an absent tool and an empty detail as the em dash", async () => {
    serveList();
    serveRun();
    const user = setup();
    await listed();
    await runIt(user);
    await screen.findByRole("table", { name: "Run steps" });
    expect(rowsOf("Run steps")).toEqual([
      ["1", "read", "read.kg.tickets_by_status", "ok", "Rows: 41"],
      ["2", "draft", "—", "ok", "Body chars: 512"],
      ["3", "write", "write.internal.crm.note", "parked", "—"],
    ]);
  });

  it("renders the served proposals, and links ONLY the one that parked an approval", async () => {
    serveList();
    serveRun();
    const user = setup();
    await listed();
    await runIt(user);
    const table = await screen.findByRole("table", { name: "Proposed writes" });
    expect(rowsOf("Proposed writes")).toEqual([
      [
        "ticket-cluster-118",
        "0011a00001AbCdEf",
        "parked",
        "—",
        "512",
        "chunk-9021 · chunk-9044",
        "a2bf6cea-2f1d-4a77-9c31-5f0e2b8d4411",
      ],
      // The refused one says WHY, in the engine's own words, and the parked one
      // above carries no reason at all — so the column is discriminating.
      ["ticket-cluster-204", "—", "refused", "no resolvable CRM record", "0", "—", "—"],
    ]);
    // The parked one is a LINK into the queue that now holds it…
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByRole("link")).toHaveAttribute(
      "href",
      "/approvals?request=a2bf6cea-2f1d-4a77-9c31-5f0e2b8d4411",
    );
    // …and the proposal that parked nothing offers no link to open. A link here
    // would be an approval this console invented.
    expect(within(rows[1]!).queryByRole("link")).not.toBeInTheDocument();
  });

  it("reports the three counts the run served, each under its own label", async () => {
    serveList();
    serveRun();
    const user = setup();
    await listed();
    await runIt(user);
    await screen.findByRole("table", { name: "Run steps" });
    // Pairwise-distinct values: a count wired to a neighbouring field fails here.
    expect(counts()).toEqual([
      ["Rows read", "41"],
      ["Subjects considered", "7"],
      ["Subjects selected", "2"],
    ]);
  });

  it("announces the served deterministic outcome when an asynchronous run completes", async () => {
    // Mutation target: removing the concise status node turns this RED even
    // though the visible result and its tables continue to render.
    serveList([B3_WORKFLOW]);
    serveRun(B3_RUN);
    const user = setup();
    await waitFor(() => expect(workflowTitles()).toEqual([B3_WORKFLOW.title]));
    await runIt(user);

    const announcement = await screen.findByText(
      "Workflow complete — deterministic outcome: refused.",
    );
    expect(announcement).toHaveAttribute("role", "status");
    expect(announcement).toHaveClass("ne-visually-hidden");
  });

  it("renders B3's served refusal and answerable control as distinct deterministic results", async () => {
    // Mutation target: removing RunResult's `run.result` binding turns this RED
    // while the generic step/count tables continue to render GREEN.
    serveList([B3_WORKFLOW]);
    serveRun(B3_RUN);
    const user = setup();
    await waitFor(() => expect(workflowTitles()).toEqual([B3_WORKFLOW.title]));
    await runIt(user);

    const result = await screen.findByRole("region", { name: "Deterministic result" });
    expect(within(result).getByText("refused")).toBeInTheDocument();
    expect(within(result).getByText(B3_RUN.result!.summary)).toBeInTheDocument();
    expect(within(result).getByRole("list", { name: "Missing fields" })).toHaveTextContent(
      "reg_e_disputes.provisional_credit_issue_date",
    );

    const control = within(result).getByRole("region", { name: "Control result" });
    expect(within(control).getByText("complete")).toBeInTheDocument();
    expect(within(control).getByText("$77.50")).toBeInTheDocument();
    expect(within(control).getByText("Recorded-credit control total")).toBeInTheDocument();
    expect(
      within(control).getByRole("list", { name: "Control evidence" }).querySelectorAll("li"),
    ).toHaveLength(2);
    expect(within(control).getByText("urn:ne:meridian:dispute:mer-dsp-003")).toBeInTheDocument();
    expect(within(control).getByText("urn:ne:meridian:dispute:mer-dsp-005")).toBeInTheDocument();
    expect(screen.getByText("This run parked no writes for approval.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Agentic run result" })).not.toBeInTheDocument();
  });

  it("renders B1's served read-only findings without inventing a write or control", async () => {
    serveList([B1_WORKFLOW]);
    serveRun(B1_RUN);
    const user = setup();
    await waitFor(() => expect(workflowTitles()).toEqual([B1_WORKFLOW.title]));
    await runIt(user);

    await screen.findByRole("region", { name: "Deterministic result" });
    expect(rowsOf("Deterministic findings")).toEqual([
      [
        "loan-77",
        "customer-12",
        "actionable",
        "customer-12 · loan-77 · payment-99",
        "County: Columbia · Loan status: active · Next scheduled due date: 2026-08-28",
      ],
    ]);
    expect(screen.getByText(B1_RUN.result!.summary)).toBeInTheDocument();
    expect(screen.getByText("This run parked no writes for approval.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Control result" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /approval/i })).not.toBeInTheDocument();
  });

  it("opens the run's own trace at the served trace id", async () => {
    serveList();
    serveRun();
    const user = setup();
    await listed();
    await runIt(user);
    await screen.findByRole("table", { name: "Run steps" });
    expect(screen.getByRole("link", { name: "View trace" })).toHaveAttribute(
      "href",
      "/trace/0f9c2b7e41d8",
    );
  });

  it("keeps a run IN FLIGHT when another workflow is opened, and re-opens the control once it settles", async () => {
    // A run reads real records and parks real Tier-2 approvals for ~30s per
    // subject. Forgetting it on a change of selection re-opens the control over
    // a sweep that is still going, and the second press parks a SECOND set of
    // approvals against the same accounts. Held open so "in flight" is a real
    // state rather than a frame between two resolved promises.
    serveList();
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post("*/api/v1/workflows/:id/run", async () => {
        await held;
        return HttpResponse.json(RUN);
      }),
    );
    const user = setup();
    await listed();
    await runIt(user);
    await screen.findByText(/^Running /);

    // The operator looks at the OTHER workflow while the first run is going.
    await user.click(screen.getByText(WORKFLOWS[1]!.title));
    expect(runButton()).toBeDisabled();
    // …and the closed control says which run holds it, by the served TITLE —
    // a disabled button with no account of itself reads as a broken one.
    expect(
      screen.getByText(`${WORKFLOWS[0]!.title} is still running — one run at a time.`),
    ).toBeInTheDocument();
    // The other workflow's screen shows no RESULT, only the reason it is closed:
    // the run belongs to the workflow that started it.
    expect(screen.queryByRole("table", { name: "Run steps" })).not.toBeInTheDocument();

    // THE CONTROL, and the half that makes this more than "the button is always
    // off": when the run settles, the second workflow becomes runnable again and
    // the line goes away.
    release();
    await waitFor(() => expect(runButton()).toBeEnabled());
    expect(screen.queryByText(/is still running/)).not.toBeInTheDocument();
  });

  it("shows a finished run under its OWN workflow only, and still has it on return", async () => {
    // Two claims, and the second is why the first is not achieved by throwing
    // the result away. A run costs ~30s per subject and parks Tier-2 approvals,
    // so its steps, its trace link and its approval links must survive the
    // operator glancing at the next row — otherwise re-reading them costs a
    // second governed sweep against the same accounts.
    serveList();
    serveRun();
    const user = setup();
    await listed();
    await runIt(user);
    await screen.findByRole("table", { name: "Run steps" });

    await user.click(screen.getByText(WORKFLOWS[1]!.title));
    // Under the OTHER workflow it is absent — showing it would caption one
    // workflow's steps with another's name.
    expect(screen.queryByRole("table", { name: "Run steps" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "View trace" })).not.toBeInTheDocument();

    // Back on the workflow it belongs to, the whole result is still there —
    // hidden, never discarded. Asserted by VALUE, so a re-render that emptied
    // the tables would fail here rather than pass on their presence.
    await user.click(screen.getByText(WORKFLOWS[0]!.title));
    expect(rowsOf("Run steps")).toEqual([
      ["1", "read", "read.kg.tickets_by_status", "ok", "Rows: 41"],
      ["2", "draft", "—", "ok", "Body chars: 512"],
      ["3", "write", "write.internal.crm.note", "parked", "—"],
    ]);
    expect(screen.getByRole("link", { name: "View trace" })).toHaveAttribute(
      "href",
      "/trace/0f9c2b7e41d8",
    );
    expect(counts()).toEqual([
      ["Rows read", "41"],
      ["Subjects considered", "7"],
      ["Subjects selected", "2"],
    ]);
  });
});

// ===========================================================================
// 3 — a server older than this console (vacuous-guards §4 row 19)
// ===========================================================================

describe("a run payload whose defaulted keys are absent", () => {
  it("renders each of the three absent counts as the em dash, not a zero and not 'undefined'", async () => {
    serveList();
    serveRun(OLD_WIRE_RUN);
    const user = setup();
    await listed();
    await runIt(user);
    await screen.findByRole("link", { name: "View trace" });
    expect(counts()).toEqual([
      ["Rows read", "—"],
      ["Subjects considered", "—"],
      ["Subjects selected", "—"],
    ]);
    // Both tables render their own §2.4 empty line rather than crashing, and
    // neither invents a row.
    expect(screen.getByText("This run reported no steps.")).toBeInTheDocument();
    expect(screen.getByText("This run parked no writes for approval.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Deterministic result" })).not.toBeInTheDocument();
  });

  it("renders rows whose own defaulted keys are absent", async () => {
    serveList();
    serveRun(OLD_WIRE_ROWS);
    const user = setup();
    await listed();
    await runIt(user);
    await screen.findByRole("table", { name: "Run steps" });
    expect(rowsOf("Run steps")).toEqual([["1", "read", "—", "ok", "—"]]);
    expect(rowsOf("Proposed writes")).toEqual([
      ["ticket-cluster-118", "—", "refused", "—", "—", "—", "—"],
    ]);
  });
});

// ===========================================================================
// 4 — the server's refusals, in the server's own words
// ===========================================================================

describe("a run the server does not carry out", () => {
  it("does not retry or fall back when the selected agentic model is policy-refused", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [{ mode: "agentic", status: "available" }],
      },
      WORKFLOWS[1]!,
    ]);
    const bodies: unknown[] = [];
    server.use(
      http.get("*/api/v1/models/choices/agentic-workflow", () =>
        HttpResponse.json({
          surface: "agentic-workflow",
          task: "agentic-workflow",
          effective_default: "frontier-sonnet",
          choices: [
            {
              model: "frontier-sonnet",
              provider: "anthropic",
              lane: "frontier-api",
              evidence: "frontier-provider-attempt",
              observed_at: "2026-08-18T18:00:00Z",
            },
            {
              model: "private-qwen",
              provider: "vllm",
              lane: "private-vllm",
              evidence: "private-serving-probe",
              observed_at: "2026-08-18T18:00:01Z",
            },
          ],
        }),
      ),
      http.post("*/api/v1/agentic-workflows/:id/start", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(
          { detail: "frontier model selection is unavailable under AIR_GAP" },
          { status: 403 },
        );
      }),
    );
    const user = setup();
    await listed();
    await runAgentic(user);

    expect(await screen.findByText("Denied by policy.")).toBeInTheDocument();
    expect(
      screen.getByText("frontier model selection is unavailable under AIR_GAP"),
    ).toBeInTheDocument();
    expect(bodies).toEqual([{ requested_model: "frontier-sonnet" }]);
    expect(screen.queryByRole("combobox", { name: "Run model" })).not.toBeInTheDocument();
    expect(screen.getByText("Model choices changed. Refresh to continue.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run agentic" })).toBeDisabled();
  });

  it("invalidates a stale agentic alias after one 409 request without falling back", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [{ mode: "agentic", status: "available" }],
      },
      WORKFLOWS[1]!,
    ]);
    const bodies: unknown[] = [];
    server.use(
      http.get("*/api/v1/models/choices/agentic-workflow", () =>
        HttpResponse.json({
          surface: "agentic-workflow",
          task: "agentic-workflow",
          effective_default: "frontier-sonnet",
          choices: [
            {
              model: "frontier-sonnet",
              provider: "anthropic",
              lane: "frontier-api",
              evidence: "frontier-provider-attempt",
              observed_at: "2026-08-18T18:00:00Z",
            },
            {
              model: "private-qwen",
              provider: "vllm",
              lane: "private-vllm",
              evidence: "private-serving-probe",
              observed_at: "2026-08-18T18:00:01Z",
            },
          ],
        }),
      ),
      http.post("*/api/v1/agentic-workflows/:id/start", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ detail: "requested model is unavailable" }, { status: 409 });
      }),
    );
    const user = setup();
    await listed();
    await runAgentic(user);

    // A START REQUEST THAT NEVER LANDED — not a save, and not a run that
    // failed. Nothing on this screen saves anything, so the card names the
    // thing the reader actually pressed; "Try again" is honest here because
    // the request genuinely did not happen.
    expect(await screen.findByText("Couldn’t start the run. Try again.")).toBeInTheDocument();
    expect(screen.queryByText(/Couldn.t save/)).toBeNull();
    expect(screen.getByText("requested model is unavailable")).toBeInTheDocument();
    expect(bodies).toEqual([{ requested_model: "frontier-sonnet" }]);
    expect(screen.queryByRole("combobox", { name: "Run model" })).not.toBeInTheDocument();
    expect(screen.getByText("Model choices changed. Refresh to continue.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run agentic" })).toBeDisabled();
  });

  it("fails closed and invalidates the agentic choice when model evidence is unavailable", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [{ mode: "agentic", status: "available" }],
      },
      WORKFLOWS[1]!,
    ]);
    const bodies: unknown[] = [];
    server.use(
      http.post("*/api/v1/agentic-workflows/:id/start", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ detail: "model evidence is unavailable" }, { status: 503 });
      }),
    );
    const user = setup();
    await listed();
    await runAgentic(user);

    expect(await screen.findByText("Couldn’t start the run. Try again.")).toBeInTheDocument();
    expect(screen.queryByText(/Couldn.t save/)).toBeNull();
    expect(screen.getByText("model evidence is unavailable")).toBeInTheDocument();
    expect(bodies).toEqual([{ requested_model: "private-default" }]);
    expect(screen.queryByRole("combobox", { name: "Run model" })).not.toBeInTheDocument();
    expect(screen.getByText("Model choices changed. Refresh to continue.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run agentic" })).toBeDisabled();
  });

  it("renders the server's 403 reason verbatim, and no result", async () => {
    serveList();
    server.use(
      http.post("*/api/v1/workflows/:id/run", () =>
        HttpResponse.json({ detail: "tool-denied" }, { status: 403 }),
      ),
    );
    const user = setup();
    await listed();
    await runIt(user);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("tool-denied");
    expect(screen.queryByRole("table", { name: "Run steps" })).not.toBeInTheDocument();
  });

  it("states a 403 the gate gave no reason for, and writes none for it", async () => {
    // A proxy 403 carries no JSON body, so the gate stated nothing. The card
    // headline IS the denial; a console sentence under it would be read as the
    // gate's account of why (primitives/states.tsx, approvals/detail.ts).
    serveList();
    server.use(
      http.post("*/api/v1/workflows/:id/run", () => new HttpResponse(null, { status: 403 })),
    );
    const user = setup();
    await listed();
    await runIt(user);
    expect(await screen.findByText("Denied by policy.")).toBeInTheDocument();
    expect(screen.queryByText("Your role doesn’t include this action.")).toBeNull();
    expect(screen.queryByRole("table", { name: "Run steps" })).not.toBeInTheDocument();
  });

  it("renders the server's 422 detail verbatim rather than a bound this console guessed", async () => {
    serveList();
    server.use(
      http.post("*/api/v1/workflows/:id/run", () =>
        HttpResponse.json({ detail: "Input should be less than or equal to 5" }, { status: 422 }),
      ),
    );
    const user = setup();
    await listed();
    const subjects = screen.getByLabelText("Subjects");
    await user.clear(subjects);
    await user.type(subjects, "9");
    await runIt(user);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Input should be less than or equal to 5");
    // The line over it names the thing that failed. There is no save on this
    // screen, and a card headlined "Couldn't save." would describe an action
    // the reader never took.
    expect(faultCard().line).toBe("Couldn’t run the workflow. Try again.");
  });

  it("answers a 404 on its STATUS alone, and every other failure as an outage", async () => {
    // `ApiSettings.workflows_enabled` ships FALSE and BOTH routes then 404, so
    // this is the SHIPPED DEFAULT, not an edge. Rendering it as "Couldn't load"
    // over a Retry that can never succeed calls a healthy server broken.
    //
    // THE 404 FIXTURE CARRIES AN UNRELATED `detail` ON PURPOSE. The production
    // path never reads that string, so seeding it with "workflows not enabled"
    // would let this guard pass by matching prose the code does not match —
    // a guard satisfiable without the mechanism it claims to pin. Seeding an
    // unrelated one instead ASSERTS the contract: the status decides, and the
    // screen does not claim a cause it cannot read.
    const NOT_FOUND = "Not Found";
    server.use(
      http.get("*/api/v1/workflows", () =>
        HttpResponse.json({ detail: NOT_FOUND }, { status: 404 }),
      ),
    );
    setup();
    expect(
      await screen.findByText("The workflow engine is not available in this deployment."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Couldn’t load workflows\./)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    cleanup();

    // CONTROL 1 — a transport failure is still an outage with a way out. Without
    // it, a mutant answering EVERY failed read with "not available" — the
    // opposite lie, an outage rendered as a deliberate configuration — passes.
    server.use(http.get("*/api/v1/workflows", () => HttpResponse.error()));
    setup();
    expect(await screen.findByText(/Couldn’t load workflows\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(
      screen.queryByText("The workflow engine is not available in this deployment."),
    ).not.toBeInTheDocument();
    cleanup();

    // CONTROL 2 — a 5xx is an HTTP failure like the 404 and must NOT take the
    // same arm. This is what makes the claim "on its STATUS" load-bearing: a
    // mutant widening the catch to any `ApiError` reaches here, not just the
    // transport control above, which is not an ApiError at all.
    server.use(
      http.get("*/api/v1/workflows", () =>
        HttpResponse.json({ detail: NOT_FOUND }, { status: 500 }),
      ),
    );
    setup();
    expect(await screen.findByText(/Couldn’t load workflows\./)).toBeInTheDocument();
    expect(
      screen.queryByText("The workflow engine is not available in this deployment."),
    ).not.toBeInTheDocument();
  });

  it("renders the SERVER's refusal of the list, not a permission this console decided", async () => {
    server.use(
      http.get("*/api/v1/workflows", () =>
        HttpResponse.json({ detail: "requires ops" }, { status: 403 }),
      ),
    );
    setup();
    const refusal = await screen.findByText(/Contact your administrator for access/);
    expect(refusal.textContent).toBe(
      "Your role doesn’t include the workflow engine. Contact your administrator for access.",
    );
    expect(screen.queryByRole("table", { name: "Workflows" })).not.toBeInTheDocument();
  });
});

// ===========================================================================
// 5 — the rail entry, and the gate on it
// ===========================================================================

/** The server's verdict, in the shape navSections.test.ts uses: the surfaces it
 * says it would REFUSE this key. Built at the seam, so no provider and no cast. */
function known(refusedSurfaces: Surface[]): Entitlements {
  const refused = new Set(refusedSurfaces);
  const access = (surface: Surface): Access => (refused.has(surface) ? "refused" : "granted");
  return { access, refused: (s) => access(s) === "refused", known: true, pending: false };
}

describe("/workflows in the rail", () => {
  it("is hidden from a key the server refused ops, and shown to one it granted", () => {
    expect(routeVisibility("/workflows", known(["ops"]))).toBe("hidden");
    expect(routeVisibility("/workflows", known([]))).toBe("shown");
    // The control an over-broad "hide everything" rule would flag: an ungated
    // destination stays visible to the SAME refused key.
    expect(routeVisibility("/search", known(["ops"]))).toBe("shown");
  });

  it("leaves the Operations group without a Workflows link for that refused key", () => {
    const operations = railSections(NAV_SECTIONS, known(["ops"])).find(
      (section) => section.kind === "group" && section.label === "Operations",
    );
    expect(operations).toBeDefined();
    const links =
      operations && operations.kind === "group"
        ? operations.slots.flatMap((slot) => (slot.kind === "link" ? [slot.to] : []))
        : [];
    expect(links).not.toContain("/workflows");
    // Control: the same group's ungated destinations are still there, so this
    // is not passing because the group vanished.
    expect(links).toContain("/sources");
  });

  it("is where the 'g' chord's w goes, and no second letter claims the same route", () => {
    expect(GO_KEYS.w).toBe("/workflows");
    // `w` was FREE, and stays a single claim on the route: a second letter
    // mapped here is two chords for one destination, which is how a letter map
    // rots. Exact pair list, so a duplicate fails on the value.
    expect(Object.entries(GO_KEYS).filter(([, to]) => to === "/workflows")).toEqual([
      ["w", "/workflows"],
    ]);
    // …and the chord is only ARMED where the rail shows the link. `useShellKeys`
    // checks every press against this set, so the letter honours the server's
    // verdict in both directions rather than routing somewhere the rail denies.
    //
    // WORKFLOWS LEFT THE RAIL (owner-directed 2026-08-28 — SPEC §13.1 item 8,
    // PD-WORKFLOWS-REMOVE, applied in 6480ea04). The route, the page and this
    // suite deliberately remain; only the NAV_SECTIONS entry went. So `w` is
    // now armed in NEITHER state — not because the server refused anything,
    // but because `shortcutRoutes` is derived from the rail and the rail no
    // longer offers the destination. That is the mechanism working, not a hole.
    expect(shortcutRoutes(known([])).has("/workflows")).toBe(false);
    expect(shortcutRoutes(known(["ops"])).has("/workflows")).toBe(false);
    // A pair of `false`s would also be satisfied by a filter that armed
    // NOTHING, so the both-directions claim this test exists for is kept alive
    // on a route that is still a gated rail destination: `m` → /models is
    // armed when granted and unarmed when refused, on the SAME `ops` surface
    // /workflows is gated by.
    expect(GO_KEYS.m).toBe("/models");
    expect(shortcutRoutes(known([])).has("/models")).toBe(true);
    expect(shortcutRoutes(known(["ops"])).has("/models")).toBe(false);
  });
});

// ===========================================================================
// 015A0-ENFORCEMENT — a run whose source authority changed
// ===========================================================================

describe("015A0-ENFORCEMENT — an undisclosed run result", () => {
  it("withholds every source-derived part of a deterministic run and offers no trace link", async () => {
    serveList();
    serveRun({ ...RUN, disclosure_status: "content_withheld" });
    const user = setup();
    await listed();
    await runIt(user);

    expect(await screen.findByText("Answer withheld — access changed")).toBeVisible();
    // Steps, parked writes and the counts are all source-derived and all gone.
    expect(screen.queryByRole("table", { name: "Run steps" })).toBeNull();
    expect(screen.queryByRole("table", { name: "Proposed writes" })).toBeNull();
    expect(screen.queryByText("ticket-cluster-118")).toBeNull();
    expect(screen.queryByRole("link", { name: "View trace" })).toBeNull();
    // The run's own identity survives, because the run owns it.
    expect(screen.getByText("0f9c2b7e41d8")).toBeVisible();
  });

  it("withholds a deterministic run that states NO disclosure status, complete though it looks", async () => {
    serveList();
    serveRun({ ...RUN, disclosure_status: undefined });
    const user = setup();
    await listed();
    await runIt(user);

    expect(
      await screen.findByText(
        "The server stated no disclosure decision for this run. Its answer, steps, parked writes and served identities are withheld.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("table", { name: "Run steps" })).toBeNull();
  });

  it("withholds an agentic run's answer, intents, served identities and governed trace", async () => {
    serveList([
      {
        ...WORKFLOWS[0]!,
        modes: [
          {
            mode: "agentic",
            status: "available",
            endpoint: "/v1/agentic-workflows/unresolved-ticket-review/run",
          },
        ],
      },
      WORKFLOWS[1]!,
    ]);
    serveAgenticRun({ ...AGENTIC_RUN, disclosure_status: "content_withheld" });
    const user = setup();
    await listed();
    await runAgentic(user);

    expect(await screen.findByText("Answer withheld — access changed")).toBeVisible();
    expect(screen.queryByText(AGENTIC_RUN.answer!)).toBeNull();
    expect(screen.queryByRole("region", { name: "Stated intents" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Served evidence identities" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Governed trace" })).toBeNull();
    expect(screen.queryByRole("link", { name: "View full trace" })).toBeNull();
  });

  it("still renders a run the server stated as available", async () => {
    serveList();
    serveRun();
    const user = setup();
    await listed();
    await runIt(user);
    expect(await screen.findByRole("table", { name: "Run steps" })).toBeVisible();
    expect(screen.queryByText("Answer withheld — access changed")).toBeNull();
  });
});
