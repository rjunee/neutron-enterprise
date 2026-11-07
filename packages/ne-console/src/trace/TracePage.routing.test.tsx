// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// WHICH MODEL SERVED WHICH TASK, AND WHETHER IT WAS THE ONE THE ROUTE ASKED FOR.
//
// The gateway routes per task down a declared chain, so a single run can
// traverse three models by task complexity — and when the declared first
// choice is unreachable, a fallback answers instead. Both facts are governance
// facts: which model saw the customer's text, and whether the route held.
//
// This screen may only repeat what the server recorded (C7). So the three
// things proved here are:
//   * the served model, task and lane are on the row and in the pane;
//   * a FALLBACK is visibly not a primary, and names what was passed over;
//   * a record that never stated a position renders UNKNOWN — not "first
//     choice", which is the reassuring answer and the wrong one.
//
// C12: model names and lanes cross; an endpoint, key or credential never does
// — there is no such field on this wire and none is rendered.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it } from "vitest";
import type { TraceAssemblyView } from "../api/types";
import { AuthStub } from "../test/authStub";
import { TRACE_ASSEMBLY_ROUTED, TRACE_ID } from "../test/mocks/traceFixtures";
import { server } from "../test/mocks/server";
import { TracePage } from "./TracePage";

function serve(view: TraceAssemblyView) {
  server.use(http.get("*/api/v1/trace/:traceId", () => HttpResponse.json(view)));
}

function setup() {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={[`/trace/${TRACE_ID}`]}>
        <Routes>
          <Route path="/trace/:traceId" element={<TracePage />} />
          <Route path="/audit" element={<h1>Audit log</h1>} />
        </Routes>
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/** The span rows' name cells, in render order. */
function spanNames(): string[] {
  return screen
    .getAllByRole("treeitem")
    .map((r) => r.querySelector(".ne-trace__spanname")?.textContent?.trim() ?? "");
}

/** A pane field's value, by its label. */
function paneField(label: string): string {
  const pane = screen.getByRole("dialog");
  const field = [...pane.querySelectorAll(".ne-detail__field")].find(
    (f) => f.querySelector(".ne-detail__label")?.textContent === label,
  );
  return field?.querySelector(".ne-detail__value")?.textContent?.trim() ?? "";
}

/** The row's fallback mark, or null when it carries none. */
function fallbackMark(index: number): Element | null {
  return screen.getAllByRole("treeitem")[index]?.querySelector(".ne-trace__spanfallback") ?? null;
}

function modelFact(index: number, key: "model-requested" | "model-served"): string | null {
  const row = screen.getAllByRole("treeitem")[index];
  return (
    row?.querySelector(`[data-fact="${key}"]`)?.textContent?.replace(/\s+/g, " ").trim() ?? null
  );
}

async function openRow(user: ReturnType<typeof userEvent.setup>, index: number) {
  const row = screen.getAllByRole("treeitem")[index];
  if (row === undefined) throw new Error(`no span row at index ${index}`);
  await user.click(row);
}

describe("trace — which model served which task", () => {
  it("names the served task beside the model on every model-call row", async () => {
    serve(TRACE_ASSEMBLY_ROUTED);
    setup();
    expect(await screen.findByText("Run timeline")).toBeInTheDocument();

    // EXACT, not containment. A name cell that grew a stray label, lost its
    // task, or rendered the task of a neighbouring row still "contains" the
    // right substrings; only the whole rendered string forbids all of that.
    // The routing beat, readable without opening a single span: a cheap on-box
    // triage, a reason call that fell back, a frontier adjudication, an assist.
    expect(spanNames()).toEqual([
      "aux-modeltriage",
      "aux-modelreasonfallback",
      "frontier-bestadjudicate",
      "private-defaultgrounding-assist",
    ]);
  });

  it("renders the model the server said SERVED, not the route's declared first choice", async () => {
    serve(TRACE_ASSEMBLY_ROUTED);
    const user = setup();
    expect(await screen.findByText("Run timeline")).toBeInTheDocument();

    // `reason` declares private-default first with an on-box fallback. THIS
    // call was served by the fallback, and the pane says so — the console has
    // no route table and must never reach for one.
    await openRow(user, 1);
    expect(paneField("Task")).toBe("reason");
    expect(paneField("Requested model")).toBe("private-default");
    expect(paneField("Served model")).toBe("aux-model");
    expect(paneField("Lane")).toBe("private-vllm");
  });

  it("labels requested and served model identities separately on the timeline", async () => {
    serve({
      ...TRACE_ASSEMBLY_ROUTED,
      nodes: TRACE_ASSEMBLY_ROUTED.nodes.map((node, index) =>
        index === 0 && node.model_call
          ? { ...node, model_call: { ...node.model_call, requested_model: "aux-model" } }
          : node,
      ),
    });
    setup();
    expect(await screen.findByText("Run timeline")).toBeInTheDocument();

    expect(modelFact(0, "model-requested")).toBe("Requested model · aux-model");
    expect(modelFact(0, "model-served")).toBe("Served model · aux-model");
    expect(modelFact(1, "model-requested")).toBe("Requested model · private-default");
    expect(modelFact(1, "model-served")).toBe("Served model · aux-model");
    expect(modelFact(3, "model-requested")).toBeNull();
    expect(modelFact(3, "model-served")).toBe("Served model · private-default");
  });

  it("marks a fallback call on its row and names the candidate passed over in the pane", async () => {
    serve(TRACE_ASSEMBLY_ROUTED);
    const user = setup();
    expect(await screen.findByText("Run timeline")).toBeInTheDocument();

    expect(fallbackMark(1)).not.toBeNull();
    // The primary-served row carries no such mark.
    expect(fallbackMark(0)).toBeNull();

    await openRow(user, 1);
    const routing = paneField("Routing");
    expect(routing).toMatch(/[Ff]allback/);
    expect(routing).toContain("private-default");
  });

  it("renders a served primary as a first ATTEMPT, never as a fallback", async () => {
    serve(TRACE_ASSEMBLY_ROUTED);
    const user = setup();
    expect(await screen.findByText("Run timeline")).toBeInTheDocument();

    await openRow(user, 0);
    // "First attempt", not "First choice": the server said nothing was
    // attempted before this model, which is not the same claim as "the route's
    // declared first choice served" — a lane-filtered request can record
    // `primary` for a candidate the route lists second. See format.ts.
    expect(paneField("Routing")).toBe("First attempt");
    expect(paneField("Routing")).not.toMatch(/fallback/i);
    expect(paneField("Routing")).not.toMatch(/choice/i);
  });

  it("renders a served-null position as unknown, never as first choice", async () => {
    serve(TRACE_ASSEMBLY_ROUTED);
    const user = setup();
    expect(await screen.findByText("Run timeline")).toBeInTheDocument();

    await openRow(user, 2);
    // The em dash: the server did not say. A console that answers "First
    // choice" here is asserting the route held on a record that never claimed
    // it did.
    expect(paneField("Routing")).toBe("—");
    expect(fallbackMark(2)).toBeNull();
  });

  it("renders an older wire's absent routing keys as unknown without crashing", async () => {
    serve(TRACE_ASSEMBLY_ROUTED);
    const user = setup();
    expect(await screen.findByText("Run timeline")).toBeInTheDocument();

    // Row 3's model_call omits route_position/fallback_from ENTIRELY (rolling
    // upgrade). It renders, it shows its task, and it claims no position.
    await openRow(user, 3);
    expect(paneField("Task")).toBe("grounding-assist");
    expect(paneField("Requested model")).toBe("—");
    expect(paneField("Served model")).toBe("private-default");
    expect(paneField("Routing")).toBe("—");
  });

  it("renders a fallback with no named candidate as a fallback, without inventing one", async () => {
    serve({
      ...TRACE_ASSEMBLY_ROUTED,
      nodes: TRACE_ASSEMBLY_ROUTED.nodes.map((n) =>
        n.model_call?.route_position === "fallback"
          ? { ...n, model_call: { ...n.model_call, fallback_from: null } }
          : n,
      ),
    });
    const user = setup();
    expect(await screen.findByText("Run timeline")).toBeInTheDocument();

    await openRow(user, 1);
    expect(paneField("Routing")).toBe("Fallback");
  });

  it("lists the tasks each model served in the run's Models table", async () => {
    serve(TRACE_ASSEMBLY_ROUTED);
    setup();
    const table = await screen.findByRole("table", { name: "Models" });
    const rows = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((r) => [...r.querySelectorAll("td")].map((c) => c.textContent?.trim() ?? ""));

    const tasksColumn = within(table)
      .getAllByRole("columnheader")
      .findIndex((h) => h.textContent?.includes("Tasks"));
    expect(tasksColumn).toBeGreaterThan(-1);
    expect(rows.map((r) => r[0])).toEqual(["aux-model", "frontier-best", "private-default"]);
    expect(rows.map((r) => r[tasksColumn])).toEqual([
      "triage, reason",
      "adjudicate",
      "grounding-assist",
    ]);
  });

  it("renders an empty task list as absent, never as an empty cell", async () => {
    serve({
      ...TRACE_ASSEMBLY_ROUTED,
      models: TRACE_ASSEMBLY_ROUTED.models.map((m) => ({ ...m, tasks: [] })),
    });
    setup();
    const table = await screen.findByRole("table", { name: "Models" });
    const rows = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((r) => [...r.querySelectorAll("td")].map((c) => c.textContent?.trim() ?? ""));

    // POSITIONALLY PINNED. `toContain("—")` over the whole row passes on any
    // row where ANY cell happens to render an em dash — a sibling column's
    // absent value satisfies it, and the Tasks cell could go blank unnoticed.
    // The assertion is about ONE column, so it indexes that column.
    const headers = within(table).getAllByRole("columnheader");
    const tasksColumn = headers.findIndex((h) => h.textContent?.includes("Tasks"));
    expect(tasksColumn).toBeGreaterThan(-1);
    expect(rows.map((r) => r[0])).toEqual(["aux-model", "frontier-best", "private-default"]);
    expect(rows.map((r) => r[tasksColumn])).toEqual(["—", "—", "—"]);
  });

  // ABSENT IS NOT EMPTY — the rolling-upgrade case, and the one no fixture in
  // this repo served.
  //
  // Every trace fixture sends `tasks`, and the empty-list test above sends
  // `tasks: []`. An API deployed BEFORE this field omits the key entirely, so
  // the value a reader gets is `undefined`. `undefined.length` is a TypeError,
  // and a TypeError thrown inside a column's `cell` takes the whole Models
  // table down — the aggregate view a reader came for, gone, on the exact
  // deployment window where the console is newer than the server. This is a
  // repeat of catalogue #14 (PR #407), the identical absent-key hole in this
  // same file family, so it gets a fixture with the key genuinely ABSENT.
  it("renders a pre-tasks wire, with the key ABSENT from the aggregates, without crashing", async () => {
    serve({
      ...TRACE_ASSEMBLY_ROUTED,
      models: TRACE_ASSEMBLY_ROUTED.models.map(({ tasks: _absent, ...older }) => older),
    });
    const user = setup();

    // The table renders AT ALL — the assertion that fails as an unhandled
    // TypeError, not as a mismatch, when the optional chain goes.
    const table = await screen.findByRole("table", { name: "Models" });
    const rows = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((r) => [...r.querySelectorAll("td")].map((c) => c.textContent?.trim() ?? ""));
    expect(rows.map((r) => r[0])).toEqual(["aux-model", "frontier-best", "private-default"]);

    const headers = within(table).getAllByRole("columnheader");
    const tasksColumn = headers.findIndex((h) => h.textContent?.includes("Tasks"));
    // Every row states "no tasks recorded" as the em dash — never a blank cell,
    // never the string "undefined".
    expect(rows.map((r) => r[tasksColumn])).toEqual(["—", "—", "—"]);

    // ...and the column's SORT reader survives it too: `sortValue` is a
    // SECOND, independent `row.tasks` dereference, so a guard on `cell` alone
    // would leave it live and crash the table on the first header click.
    const sortButton = headers[tasksColumn]?.querySelector("button");
    expect(sortButton).not.toBeNull();
    await user.click(sortButton as HTMLElement);
    expect(
      within(await screen.findByRole("table", { name: "Models" }))
        .getAllByRole("row")
        .slice(1)
        .map((r) => r.querySelector("td")?.textContent?.trim()),
    ).toEqual(["aux-model", "frontier-best", "private-default"]);

    // THE COLUMN'S THIRD `row.tasks` READER, `searchText`, IS CLOSED BY
    // `tsc -b` — NOT BY THIS TEST, AND NOT A RESIDUAL.
    //
    // No render can reach it. `DataTable` never calls `searchText`; it only
    // declares the field (DataTable.tsx:58). The single reader is
    // `rowSearchText` (tableSort.ts:77), reached only from a page that calls
    // `filterRows` — and `MODEL_COLUMNS` is module-local to TracePage.tsx,
    // which imports neither. The Models table deliberately renders no toolbar
    // (TracePage.tsx's own note: a trace is one object, not a list).
    //
    // What kills the mutant is the compiler: with `tasks?: string[]`,
    // `row.tasks.join(" ")` is `TS18048 — 'row.tasks' is possibly
    // 'undefined'`, and `npm run typecheck` is a required step of the
    // `frontend` CI context (.github/workflows/ci.yml). Catalogue row 18(b)
    // sets the precedent: closed by typing is a closure, and this comment is
    // the standing declaration that no RUNTIME test pins it.
  });
});
