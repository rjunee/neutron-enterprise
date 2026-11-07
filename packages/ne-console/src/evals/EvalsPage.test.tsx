// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §12 — EVALUATIONS.
//
// The assertions that matter here are about IDENTITY and AUTHORITY:
//
//   * every ordering and filtering claim is checked by WHICH ROWS are on screen,
//     never by how many. A count assertion passes for the wrong rows, and this
//     screen's whole job is telling one run from another;
//   * the verdict is the server's `passed`. The fixture holds a run whose
//     metrics numerically beat every threshold and whose served verdict is
//     false; it must render FAILED;
//   * a refusal, an outage and an unconfigured store are three different facts
//     and must never be rendered as each other. Telling a reader their role is
//     the problem when a service is down is a false statement about
//     authorization — exactly the class of defect this product exists to remove.

import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "../test/mocks/server";
import { AuthStub } from "../test/authStub";
import { EVAL_RUNS } from "../test/mocks/fixtures";
import { EvalsPage } from "./EvalsPage";

function setup() {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter>
        <EvalsPage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/** The runs on screen, BY IDENTITY, in the order the table renders them. The
 * Run cell is middle-truncated for width, so identity is read from the FULL
 * value the cell carries in `title`. */
function runIds(): string[] {
  const table = screen.getByRole("table", { name: "Evaluation runs" });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[1]?.querySelector("span")?.title ?? "");
}

/** The row for a run, found by its full id rather than its truncated cell. */
function rowFor(runId: string): HTMLElement {
  const found = screen.getByTitle(runId).closest("tr");
  if (!found) throw new Error(`no row for ${runId}`);
  return found;
}

function header(name: string): HTMLElement {
  return screen.getByRole("columnheader", { name });
}

describe("§12 — the runs table", () => {
  it("defaults to When descending — newest run first, not served order", async () => {
    setup();
    await screen.findByTitle("run-pass");
    // The fixture is served run-pass (2 d ago) FIRST, so served order and the
    // spec's default sort disagree; only the sort produces this.
    expect(runIds()).toEqual(["run-fail-server-verdict", "run-pass"]);
    expect(header("When")).toHaveAttribute("aria-sort", "descending");
  });

  it("makes Suite, When, Verdict and Judge error rate sortable, and nothing else", async () => {
    setup();
    await screen.findByTitle("run-pass");
    for (const name of ["Suite", "When", "Verdict", "Judge error rate"]) {
      expect(within(header(name)).getByRole("button", { name })).toBeInTheDocument();
    }
    // Run, Judge and Config are opaque identifiers; §12 names no sort for them.
    for (const name of ["Run", "Judge", "Config"]) {
      expect(within(header(name)).queryByRole("button")).toBeNull();
    }
  });

  it("sorting by Suite reorders the ROWS and moves aria-sort onto that header", async () => {
    const user = setup();
    await screen.findByTitle("run-pass");
    await user.click(within(header("Suite")).getByRole("button", { name: "Suite" }));
    expect(runIds()).toEqual(["run-pass", "run-fail-server-verdict"]);
    expect(header("Suite")).toHaveAttribute("aria-sort", "ascending");
    expect(header("When")).not.toHaveAttribute("aria-sort");
  });

  it("renders the SERVER's verdict — metrics that beat every threshold do not make a pass", async () => {
    setup();
    await screen.findByTitle("run-fail-server-verdict");
    const failRow = rowFor("run-fail-server-verdict");
    // Served metrics 0.99 / 0.95 are ABOVE thresholds 0.85 / 0.8; `passed` is false.
    expect(within(failRow).getByLabelText("FAILED")).toBeInTheDocument();
    expect(within(failRow).getByText("12.0%")).toBeInTheDocument();
    expect(within(rowFor("run-pass")).getByLabelText("PASSED")).toBeInTheDocument();
  });

  it("truncates a long run id in the middle and keeps the whole one addressable", async () => {
    const longId = "run-2026-07-25-retrieval-baseline-9c31f0a2";
    server.use(
      http.get("*/api/v1/eval/runs", () =>
        HttpResponse.json({ runs: [{ ...EVAL_RUNS[0], run_id: longId }] }),
      ),
    );
    setup();
    const cell = await screen.findByText("run-2026…31f0a2");
    expect(cell).toHaveAttribute("title", longId);
  });

  it("discloses a FULL window — a sort over a page is not a sort over the table", async () => {
    let askedLimit: string | null = null;
    server.use(
      http.get("*/api/v1/eval/runs", ({ request }) => {
        askedLimit = new URL(request.url).searchParams.get("limit");
        return HttpResponse.json({
          runs: Array.from({ length: 500 }, (_, i) => ({
            ...EVAL_RUNS[0],
            run_id: `run-${i}`,
          })),
        });
      }),
    );
    setup();
    await screen.findByTitle("run-0");
    expect(askedLimit).toBe("500");
    expect(screen.getByText("Sorted within the loaded 500 rows")).toBeInTheDocument();
  });

  it("CONTROL: a window under the cap claims nothing about paging", async () => {
    setup();
    await screen.findByTitle("run-pass");
    expect(screen.queryByText(/Sorted within the loaded/)).toBeNull();
  });

  it("renders the judge fallback and never a fabricated one", async () => {
    server.use(
      http.get("*/api/v1/eval/runs", () =>
        HttpResponse.json({
          runs: [
            { ...EVAL_RUNS[0], run_id: "run-family", judge_model_id: null },
            {
              ...EVAL_RUNS[0],
              run_id: "run-nojudge",
              judge_model_id: null,
              judge_family: null,
            },
          ],
        }),
      ),
    );
    setup();
    await screen.findByTitle("run-family");
    const family = rowFor("run-family");
    expect(within(family).getByText("claude")).toBeInTheDocument();
    const none = rowFor("run-nojudge");
    expect(within(none).getAllByText("—").length).toBeGreaterThan(0);
  });
});

describe("§12 — the toolbar", () => {
  it("counts the rendered window", async () => {
    setup();
    await screen.findByTitle("run-pass");
    expect(screen.getByText("2 runs")).toBeInTheDocument();
  });

  it("the search-within narrows to the MATCHING rows and recounts them", async () => {
    const user = setup();
    await screen.findByTitle("run-pass");
    await user.type(screen.getByLabelText("Filter runs…"), "safety");
    expect(runIds()).toEqual(["run-fail-server-verdict"]);
    expect(screen.getByText("1 run")).toBeInTheDocument();
  });

  it("the suite filter is SERVER-SIDE: it re-reads with the suite param", async () => {
    const asked: Array<string | null> = [];
    server.use(
      http.get("*/api/v1/eval/runs", ({ request }) => {
        const suite = new URL(request.url).searchParams.get("suite");
        asked.push(suite);
        return HttpResponse.json({
          runs: suite ? EVAL_RUNS.filter((r) => r.suite === suite) : EVAL_RUNS,
        });
      }),
    );
    const user = setup();
    await screen.findByTitle("run-pass");
    await user.selectOptions(screen.getByLabelText("Suite"), "retrieval-baseline");
    await waitFor(() => expect(runIds()).toEqual(["run-pass"]));
    expect(asked).toContain("retrieval-baseline");
    // The narrowing is visible next to the list, and reversible.
    await user.click(screen.getByRole("button", { name: "Clear Suite: retrieval-baseline" }));
    await waitFor(() => expect(runIds()).toHaveLength(2));
  });

  it("exports the rendered window with a preamble naming the applied filter", async () => {
    const blobs: Blob[] = [];
    vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
      blobs.push(blob as Blob);
      return "blob:evaluations";
    });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const user = setup();
    await screen.findByTitle("run-pass");
    await user.type(screen.getByLabelText("Filter runs…"), "safety");
    await user.click(screen.getByRole("button", { name: "Export CSV" }));

    const text = (await blobs[0]?.text()) ?? "";
    const lines = text.trim().split("\r\n");
    expect(lines[0]).toContain("Evaluations — Search: safety — exported");
    expect(lines[1]).toBe(
      "suite,run_id,created_at,passed,judge_error_rate,judge_model_id,config_hash,faithfulness,recall_at_10",
    );
    // Exactly the rendered window: one row, and it is the searched-for one.
    expect(lines).toHaveLength(3);
    expect(lines[2]).toContain("run-fail-server-verdict");
    expect(lines[2]).toContain("false");
  });
});

describe("§12 — the run detail pane", () => {
  it("opens on a row and carries the run id, the golden set hash and the rubrics", async () => {
    const user = setup();
    await screen.findByTitle("run-pass");
    await user.click(rowFor("run-pass"));

    const pane = screen.getByRole("dialog");
    expect(within(pane).getByRole("heading", { name: "retrieval-baseline" })).toBeInTheDocument();
    // The FULL id, where a reader can copy it — the table truncates for width.
    expect(within(pane).getByText("run-pass")).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Copy golden set hash" })).toBeInTheDocument();
    // The rubric versions as PAIRS — the rubric and the version it was pinned
    // to, read out of the field rather than from anywhere they happen to co-occur.
    const rubrics = within(pane).getByText("Rubric versions").closest(".ne-detail__field");
    expect(rubrics?.textContent).toContain("faithfulness");
    expect(rubrics?.textContent).toContain("v3");
  });

  it("renders the metrics table with the SERVER's status word per metric", async () => {
    const user = setup();
    await screen.findByTitle("run-fail-server-verdict");
    await user.click(rowFor("run-fail-server-verdict"));

    const metrics = screen.getByRole("table", { name: "Metrics for run run-fail-server-verdict" });
    const rows = within(metrics)
      .getAllByRole("row")
      .slice(1)
      .map((row) =>
        within(row)
          .getAllByRole("cell")
          .map((c) => c.textContent),
      );
    expect(rows).toEqual([
      ["faithfulness", "0.950", "0.800", "fail"],
      ["recall_at_10", "0.990", "0.850", "pass"],
    ]);
  });

  it("tints the row whose SERVED status failed, and only that row", async () => {
    const user = setup();
    await screen.findByTitle("run-fail-server-verdict");
    await user.click(rowFor("run-fail-server-verdict"));
    const metrics = screen.getByRole("table", { name: "Metrics for run run-fail-server-verdict" });

    const failing = within(metrics).getByText("fail").closest("tr");
    expect(failing?.querySelector(".ne-evaluations__status--fail")).not.toBeNull();
    // CONTROL: the passing metric's row carries no failure marker, even though
    // this console never compared either value to its threshold.
    const passing = within(metrics).getByText("pass").closest("tr");
    expect(passing?.querySelector(".ne-evaluations__status--fail")).toBeNull();
  });

  it("closes on Esc and leaves the list where it was", async () => {
    const user = setup();
    await screen.findByTitle("run-pass");
    await user.click(rowFor("run-pass"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(runIds()).toEqual(["run-fail-server-verdict", "run-pass"]);
  });
});

describe("§12 — the states", () => {
  it("empty is the spec's sentence, and no sample row", async () => {
    server.use(http.get("*/api/v1/eval/runs", () => HttpResponse.json({ runs: [] })));
    setup();
    expect(
      await screen.findByText("No evaluation runs recorded. Runs appear after a suite executes."),
    ).toBeInTheDocument();
    expect(screen.getByText("0 runs")).toBeInTheDocument();
  });

  it("a suite with no runs names the suite and offers the way back", async () => {
    server.use(
      http.get("*/api/v1/eval/runs", ({ request }) => {
        const suite = new URL(request.url).searchParams.get("suite");
        return HttpResponse.json({ runs: suite ? [] : EVAL_RUNS });
      }),
    );
    const user = setup();
    await screen.findByTitle("run-pass");
    await user.selectOptions(screen.getByLabelText("Suite"), "safety-redteam");

    expect(await screen.findByText('No runs for suite "safety-redteam".')).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filter" }));
    await waitFor(() => expect(runIds()).toEqual(["run-fail-server-verdict", "run-pass"]));
  });

  it("a REFUSAL renders the server's refusal — no table, no count, no export", async () => {
    server.use(
      http.get("*/api/v1/eval/runs", () =>
        HttpResponse.json({ detail: "not permitted" }, { status: 403 }),
      ),
    );
    setup();
    expect(
      await screen.findByText(
        /Your role doesn’t include evaluation runs\. Contact your administrator for access\./,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Evaluation runs" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Export CSV" })).toBeNull();
    // A refused read must never produce a confident "0 runs".
    expect(screen.queryByText("0 runs")).toBeNull();
  });

  it("an OUTAGE is never dressed as a role boundary, and Retry asks again", async () => {
    let calls = 0;
    server.use(
      http.get("*/api/v1/eval/runs", () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json({ detail: "upstream" }, { status: 500 })
          : HttpResponse.json({ runs: EVAL_RUNS });
      }),
    );
    const user = setup();
    expect(await screen.findByText("Couldn’t load evaluation runs.")).toBeInTheDocument();
    // THE BUG THIS GUARDS: a 500 is not a 403. Nothing on screen may claim the
    // reader's role is why they cannot see this.
    expect(screen.queryByText(/Your role doesn’t include/)).toBeNull();
    expect(screen.queryByText("0 runs")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(runIds()).toEqual(["run-fail-server-verdict", "run-pass"]));
  });

  it("an unconfigured store says so, and blames neither the reader nor the network", async () => {
    server.use(
      http.get("*/api/v1/eval/runs", () =>
        HttpResponse.json({ detail: "eval store not configured" }, { status: 503 }),
      ),
    );
    setup();
    expect(
      await screen.findByText("Evaluation storage isn't configured on this deployment."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn’t include/)).toBeNull();
    expect(screen.queryByText("Couldn’t load evaluation runs.")).toBeNull();
    expect(screen.queryByRole("table", { name: "Evaluation runs" })).toBeNull();
  });

  it("a FAILED re-read clears the prior rows — stale verdicts never sit under an error", async () => {
    let calls = 0;
    server.use(
      http.get("*/api/v1/eval/runs", () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json({ runs: EVAL_RUNS })
          : new HttpResponse(null, { status: 500 });
      }),
    );
    const user = setup();
    await screen.findByTitle("run-pass");
    await user.selectOptions(screen.getByLabelText("Suite"), "safety-redteam");

    expect(await screen.findByText("Couldn’t load evaluation runs.")).toBeInTheDocument();
    expect(screen.queryByTitle("run-pass")).toBeNull();
    expect(screen.queryByTitle("run-fail-server-verdict")).toBeNull();
  });

  it("announces the load without a spinner paragraph", () => {
    setup();
    expect(screen.getByText("Loading evaluation runs")).toHaveAttribute("role", "status");
  });
});
