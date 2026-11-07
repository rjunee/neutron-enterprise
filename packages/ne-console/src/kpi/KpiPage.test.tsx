// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §17 — INSIGHTS, as rendered.
//
// The measured starting point: eleven cards, three paragraphs arguing for the
// screen's own refusal to draw a top-line figure, an "honesty band" that turned
// four absent tags into the largest heading on the page, and not one sortable
// column. So the assertions below are about the table this screen now is — a
// stated default order, sortable headers, a filter that narrows the ROWS (by
// identity, because a count assertion passes for the wrong rows), and the four
// states in the spec's own words.
//
// And about the two failures that would be worse than an ugly screen: a gap
// rendered as a zero, and an unreachable read rendered as a role boundary.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { KpiEntryView } from "../api/types";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { KpiPage } from "./KpiPage";

const WINDOW = { window_start: "2026-07-01T00:00:00Z", window_end: "2026-07-27T00:00:00Z" };
const GENERATED = "2026-07-27T09:15:00Z";

function entry(
  kpi_tag: string,
  value: number | null,
  unit: string | null = "count",
  measurement_status?: "measured" | "undefined",
): KpiEntryView {
  return {
    kpi_tag,
    value,
    measurement_status,
    unit,
    dimensions: { period: "2026-07" },
    ...WINDOW,
  };
}

/** The served window used by most cases: every standard tag except
 * `events_total`, which is absent so the not-instrumented row is real. */
function servedKpis(): KpiEntryView[] {
  return [
    entry("utilization.agent_turns", 128),
    entry("utilization.tool_calls", 342),
    entry("utilization.active_users", 9),
    entry("utilization.sessions", 41),
    entry("proficiency.eval_runs", 6),
    entry("proficiency.eval_pass_rate", 0.833, "ratio"),
    entry("proficiency.correction_signals_total", 12),
    entry("proficiency.positive_signals", 30),
    entry("proficiency.negative_signals", 4),
    entry("proficiency.negative_signal_rate", 0.118, "ratio"),
    entry("business_value.hours_saved_floor", 41.5, "hours"),
  ];
}

function serve(kpis: KpiEntryView[]) {
  server.use(
    http.get("*/api/v1/kpi/summary", () => HttpResponse.json({ kpis, generated_at: GENERATED })),
  );
}

function serveStatus(status: number) {
  server.use(
    http.get("*/api/v1/kpi/summary", () => HttpResponse.json({ detail: "no" }, { status })),
  );
}

function setup() {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={["/insights"]}>
        <KpiPage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/** The metric names in a group's table, top to bottom. */
function metricsIn(heading: string): string[] {
  const table = screen.getByRole("table", { name: heading });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((r) => r.querySelectorAll("td")[0]?.textContent?.trim() ?? "");
}

/** One row of a group's table, as [metric, value, unit]. */
function rowCells(heading: string, metric: string): string[] {
  const table = screen.getByRole("table", { name: heading });
  const row = within(table)
    .getAllByRole("row")
    .find((r) => (r.querySelectorAll("td")[0]?.textContent ?? "").startsWith(metric));
  return [...(row?.querySelectorAll("td") ?? [])].map((c) => c.textContent?.trim() ?? "");
}

describe("§17 Insights", () => {
  it("renders the three groups in §17's order, with the tags in §17's order", async () => {
    serve(servedKpis());
    setup();
    expect(await screen.findByRole("table", { name: "Utilization" })).toBeInTheDocument();

    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual([
      "Utilization",
      "Proficiency",
      "Business value",
    ]);
    // The default order is the spec's, not the response's and not alphabetical.
    expect(metricsIn("Utilization")).toEqual([
      "Agent turns",
      "Tool calls",
      "Active users",
      "Sessions",
      "Events totalNot instrumented",
    ]);
  });

  it("sorts on a header click, with aria-sort on the active column", async () => {
    serve(servedKpis());
    const user = setup();
    expect(await screen.findByRole("table", { name: "Utilization" })).toBeInTheDocument();

    const table = screen.getByRole("table", { name: "Utilization" });
    const valueHeader = within(table).getByRole("columnheader", { name: /Value/ });
    expect(valueHeader).not.toHaveAttribute("aria-sort");

    await user.click(within(valueHeader).getByRole("button"));
    // Value's first click reads largest-first; the absent metric stays last in
    // both directions, because absent is not "the smallest value".
    expect(valueHeader).toHaveAttribute("aria-sort", "descending");
    expect(metricsIn("Utilization")).toEqual([
      "Tool calls",
      "Agent turns",
      "Sessions",
      "Active users",
      "Events totalNot instrumented",
    ]);

    await user.click(within(valueHeader).getByRole("button"));
    expect(valueHeader).toHaveAttribute("aria-sort", "ascending");
    expect(metricsIn("Utilization")).toEqual([
      "Active users",
      "Sessions",
      "Agent turns",
      "Tool calls",
      "Events totalNot instrumented",
    ]);
  });

  it("the search-within narrows the rendered rows, and the count counts them", async () => {
    serve(servedKpis());
    const user = setup();
    expect(await screen.findByRole("table", { name: "Utilization" })).toBeInTheDocument();
    expect(screen.getByText("12 metrics")).toBeInTheDocument();

    await user.type(screen.getByRole("searchbox"), "signal");
    // ASSERTED BY IDENTITY: these four rows and no others survive, and the two
    // groups with no match are not rendered as empty tables.
    expect(metricsIn("Proficiency")).toEqual([
      "Correction signals total",
      "Positive signals",
      "Negative signals",
      "Negative signal rate",
    ]);
    expect(screen.queryByRole("table", { name: "Utilization" })).not.toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Business value" })).not.toBeInTheDocument();
    expect(screen.getByText("4 metrics")).toBeInTheDocument();

    await user.clear(screen.getByRole("searchbox"));
    expect(screen.getByText("12 metrics")).toBeInTheDocument();
  });

  it("a filter that matches nothing offers its way out", async () => {
    serve(servedKpis());
    const user = setup();
    expect(await screen.findByRole("table", { name: "Utilization" })).toBeInTheDocument();

    await user.type(screen.getByRole("searchbox"), "zzz");
    expect(screen.getByText("No metrics match this filter.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(screen.getByRole("table", { name: "Utilization" })).toBeInTheDocument();
  });

  it("states a gap as a gap: an absent tag is an em dash and a note, never a zero", async () => {
    serve(servedKpis());
    setup();
    expect(await screen.findByRole("table", { name: "Utilization" })).toBeInTheDocument();

    const [metric, value, unit] = rowCells("Utilization", "Events total");
    expect(metric).toBe("Events totalNot instrumented");
    expect(value).toBe("—");
    expect(unit).toBe("—");
    // The zero this row must never render.
    expect(value).not.toBe("0");
  });

  it("renders a server-authored undefined rate as an em dash, and says which", async () => {
    serve([
      entry("utilization.agent_turns", 0),
      entry("proficiency.negative_signal_rate", null, "ratio", "undefined"),
    ]);
    setup();
    expect(await screen.findByRole("table", { name: "Proficiency" })).toBeInTheDocument();

    const [metric, value] = rowCells("Proficiency", "Negative signal rate");
    expect(metric).toBe("Negative signal rateNo agent turns recorded — no rate to compute");
    expect(value).toBe("—");
  });

  it("renders a served 0.0 negative-signal rate even when no correction signals exist", async () => {
    serve([
      entry("proficiency.correction_signals_total", 0),
      entry("proficiency.negative_signal_rate", 0, "ratio", "measured"),
    ]);
    setup();
    expect(await screen.findByRole("table", { name: "Proficiency" })).toBeInTheDocument();

    const [metric, value] = rowCells("Proficiency", "Negative signal rate");
    expect(metric).toBe("Negative signal rate");
    expect(value).toBe("0.000");
  });

  it("keeps the served zero and names the attribution gap, with its witness in the title", async () => {
    serve([entry("utilization.agent_turns", 41), entry("utilization.active_users", 0)]);
    setup();
    expect(await screen.findByRole("table", { name: "Utilization" })).toBeInTheDocument();

    const [metric, value] = rowCells("Utilization", "Active users");
    expect(metric).toBe("Active usersAttribution gap");
    // THE SERVER'S VALUE IS NOT OVERWRITTEN. Replacing it would be inventing a
    // verdict in the other direction.
    expect(value).toBe("0");
    expect(screen.getByTitle("0 recorded while 41 agent turns were counted")).toBeInTheDocument();
  });

  it("carries the two standing notes without presenting an inert period control", async () => {
    serve([...servedKpis(), entry("utilization.events_total", 6058)]);
    setup();
    expect(await screen.findByRole("table", { name: "Business value" })).toBeInTheDocument();

    expect(rowCells("Utilization", "Events total")[0]).toBe(
      "Events totalIncludes this console's own reads.",
    );
    expect(rowCells("Business value", "Hours saved floor")).toEqual([
      "Hours saved floorFloor estimate — configured rate assumptions are applied by the server.",
      "41.5",
      "hours",
    ]);
    expect(screen.queryByRole("combobox", { name: "Period" })).not.toBeInTheDocument();
    // The paragraphs this screen used to argue with are gone.
    expect(document.body.textContent).not.toMatch(/minutes.saved.per.turn|floor discount/i);
  });

  it("surfaces a non-standard tag in Other, never silently dropped", async () => {
    serve([entry("utilization.sessions", 4), entry("retrieval.chunk_hits", 12)]);
    setup();
    expect(await screen.findByRole("table", { name: "Other" })).toBeInTheDocument();
    expect(metricsIn("Other")).toEqual(["Chunk hits"]);
    expect(screen.getByText("Not in the standard metric set")).toBeInTheDocument();
  });

  it("renders the generated line from the served timestamp", async () => {
    serve(servedKpis());
    setup();
    expect(await screen.findByRole("table", { name: "Utilization" })).toBeInTheDocument();
    expect(screen.getByText(/^Generated/)).toBeInTheDocument();
    expect(document.querySelector("time")).toHaveAttribute("datetime", GENERATED);
  });

  it("an empty period renders §17's empty copy and no tables", async () => {
    serve([]);
    setup();
    expect(await screen.findByText("No metrics recorded for this period.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText("0 metrics")).toBeInTheDocument();
  });

  it("a refused read renders the SERVER's refusal — the role boundary, neutrally", async () => {
    serveStatus(403);
    setup();
    expect(
      await screen.findByText(
        /Your role doesn’t include metrics\. Contact your administrator for access\./,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Couldn’t load/)).not.toBeInTheDocument();
  });

  it("an unreachable read is NOT a role boundary — it errors, and offers a retry", async () => {
    // The exact false statement this screen must never make: saying a read was
    // refused for this role when the server never answered at all.
    serveStatus(503);
    const user = setup();
    expect(await screen.findByText("Couldn’t load metrics.")).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn’t include/)).not.toBeInTheDocument();

    serve(servedKpis());
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("table", { name: "Utilization" })).toBeInTheDocument();
  });

  it("exports the rows on screen, under a preamble naming the filter", async () => {
    serve(servedKpis());
    const user = setup();
    expect(await screen.findByRole("table", { name: "Utilization" })).toBeInTheDocument();

    const blobs: Blob[] = [];
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: (blob: Blob) => {
        blobs.push(blob);
        return "blob:test";
      },
      revokeObjectURL: () => {},
    });
    await user.type(screen.getByRole("searchbox"), "signal");
    await user.click(screen.getByRole("button", { name: "Export CSV" }));

    const text = await blobs[0]!.text();
    const lines = text.trim().split("\r\n");
    // The preamble names the period and the filter this file is a slice of.
    expect(lines[0]).toContain("Insights");
    expect(lines[0]).toContain("Period: 2026-07");
    expect(lines[0]).toContain("Filter: signal");
    // The `calls` column is load-bearing, not cosmetic: on a self-hosted lane
    // every spend row's price is 0, so the served call count is the ONLY evidence
    // of attribution the exported file carries.
    expect(lines[1]).toBe("tag,value,measurement_status,unit,calls,window_start,window_end");
    expect(lines.slice(2).map((line) => line.split(",")[2])).toEqual([
      "measured",
      "measured",
      "measured",
      "measured",
    ]);
    // The rows are the FILTERED window, in the order the screen rendered them.
    expect(lines.slice(2).map((l) => l.split(",")[0])).toEqual([
      "proficiency.correction_signals_total",
      "proficiency.positive_signals",
      "proficiency.negative_signals",
      "proficiency.negative_signal_rate",
    ]);
    vi.unstubAllGlobals();
  });

  it("offers the export, and does not offer one over nothing", async () => {
    serve([]);
    setup();
    expect(await screen.findByText("No metrics recorded for this period.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeDisabled();
  });
});
