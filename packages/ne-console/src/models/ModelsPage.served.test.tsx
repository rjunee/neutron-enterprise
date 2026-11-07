// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 — MODELS on a deployment whose governed client CAN serve the policy
// router and the usage rollup.
//
// The screen is not built to be finished later: it asks the client whether each
// read is servable and renders the full §10 surface when it is. These tests
// drive that answer with a client that serves both, so the branch that lights
// up on merge is exercised now rather than hoped about — including the two
// honesty traps §10 names: a self-hosted lane prices at 0.0 and must read
// UNPRICED, and a token total the server will not vouch for stays an em dash.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { PostureView } from "../api/types";
import { AuthStub } from "../test/authStub";
import { POSTURE } from "../test/mocks/fixtures";
import { ModelsPage } from "./ModelsPage";
import type { ModelPolicyView } from "./policy";
import type { ModelsUsageView } from "./usage";

/** The client the mocked hook hands the screen. Reassigned per test. */
const stub = vi.hoisted(() => ({ client: {} as Record<string, unknown> }));

vi.mock("../auth/useApiClient", () => ({
  useApiClient: () => stub.client,
}));

const POLICY: ModelPolicyView = {
  air_gap: false,
  models: [
    {
      model_id: "qwen3-32b",
      provider: "vllm",
      lane: "private-vllm",
      configured_model: "Qwen/Qwen3-32B",
      context_window: 32768,
      binding: { endpoint_host: "vllm.internal", credential_last4: null },
      task_routes: [{ task: "drafting" }, { task: "extraction" }],
      pending_change: null,
    },
    {
      model_id: "claude-fable-5",
      provider: "anthropic",
      lane: "frontier-api",
      configured_model: "claude-fable-5",
      context_window: 1000000,
      binding: { endpoint_host: "frontier.example", credential_last4: "9f2c" },
      task_routes: [{ task: "review" }],
      pending_change: { request_id: "req-77", requested_by: "alice@example.com" },
    },
  ],
};

/** A window with one priced frontier lane, one lane whose token totals the
 * server will not vouch for, and one SELF-HOSTED lane the server marks
 * unpriced — the 0.0 that must never render as $0.00. */
/** ⚠️ THE WINDOW MUST TRACK THE CLOCK, NOT A LITERAL.
 *
 * `UsageTab` defaults its period to `monthKey(new Date())` — the CURRENT month —
 * and the page refuses a rollup whose declared `window` is not the one it asked
 * for (correctly: August data must never render under a September heading). A
 * fixture pinned to `"2026-08"` therefore serves a mismatch from the first
 * instant of every new month, the table renders empty, and SEVEN tests in this
 * file fail with `expected [] to have a length of 3`.
 *
 * That is exactly what happened at 00:00 UTC on 2026-09-01: this file went red
 * on `main`, on CI and locally, with no code change anywhere. Verified by
 * running it on a clean `origin/main` checkout. The guard is right; the literal
 * was wrong. */
const CURRENT_WINDOW = `${new Date().getFullYear()}-${String(new Date().getMonth() + 1).padStart(2, "0")}`;

const USAGE: ModelsUsageView = {
  window: CURRENT_WINDOW,
  models: [
    {
      model_id: "claude-fable-5",
      provider: "anthropic",
      lane: "frontier-api",
      calls: 412,
      input_tokens: 1284002,
      output_tokens: 98211,
      cost_usd: 1284.06,
      unpriced: false,
    },
    {
      model_id: "mistral-large",
      provider: "mistral",
      lane: "frontier-api",
      calls: 88,
      input_tokens: null,
      output_tokens: null,
      cost_usd: 12.4,
      unpriced: false,
    },
    {
      model_id: "qwen3-32b",
      provider: "vllm",
      lane: "private-vllm",
      calls: 9021,
      input_tokens: 44000000,
      output_tokens: 3100000,
      cost_usd: 0,
      unpriced: true,
    },
  ],
  totals: { calls: 9521, input_tokens: null, output_tokens: null, cost_usd: 1296.46 },
};

function setup(path = "/models") {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={[path]}>
        <ModelsPage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

function rowsOf(label: string): string[] {
  const table = screen.getByRole("table", { name: label });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
}

const posture: PostureView = { ...POSTURE, air_gap: true };

beforeEach(() => {
  stub.client = {
    posture: () => Promise.resolve(posture),
    models: () => Promise.resolve(POLICY),
    modelsUsage: () => Promise.resolve(USAGE),
  };
});

describe("§10 Policy — the columns the policy router serves", () => {
  it("adds Endpoint, Tasks routed and Status, still sorted Lane A–Z then Model", async () => {
    setup();
    await waitFor(() => expect(rowsOf("Model policy")).toHaveLength(2));
    expect(rowsOf("Model policy")).toEqual(["claude-fable-5", "qwen3-32b"]);
    const headers = screen
      .getAllByRole("columnheader")
      .map((header) => header.textContent?.replace(/[▲▼]/g, "") ?? "");
    expect(headers).toEqual([
      "Model",
      "Provider",
      "Lane",
      // Configuration facts served on BOTH read paths — the row shape does not
      // change with which read answered.
      "Configured model",
      "Declared window",
      "Endpoint",
      "Tasks routed",
      "Status",
    ]);
  });

  it("masks the credential and never renders a secret", async () => {
    setup();
    await waitFor(() => expect(rowsOf("Model policy")).toHaveLength(2));
    expect(screen.getByText("•••• 9f2c")).toBeInTheDocument();
    // The lane whose binding carries no credential reference renders the em
    // dash for it, never four dots standing in for nothing.
    const row = screen.getByText("vllm.internal").closest("tr");
    expect(row?.textContent).not.toMatch(/••••/);
  });

  it("renders a parked change on the OBJECT, with the old value still displayed", async () => {
    setup();
    await waitFor(() => expect(rowsOf("Model policy")).toHaveLength(2));
    // The alias and the model it resolves to are the same string on this lane,
    // so both cells carry it — take the row, either cell reaches it.
    const row = screen.getAllByText("claude-fable-5")[0]?.closest("tr");
    expect(row?.textContent).toMatch(/Change requested by alice@example.com/);
    expect(within(row as HTMLElement).getByRole("link", { name: "View request" })).toHaveAttribute(
      "href",
      "/approvals?request=req-77",
    );
    // The lane is still described by its in-force binding — the console never
    // pre-renders an unapplied change.
    expect(row?.textContent).toMatch(/frontier\.example/);
  });

  it("opens the binding in a pane, with the tasks routed to that lane", async () => {
    const user = setup();
    await waitFor(() => expect(rowsOf("Model policy")).toHaveLength(2));
    await user.click(screen.getByText("qwen3-32b"));
    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("vllm.internal")).toBeInTheDocument();
    expect(within(pane).getByText("drafting")).toBeInTheDocument();
    expect(within(pane).getByText("extraction")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("still offers no write control: the governed write endpoints are not wired", async () => {
    setup();
    await waitFor(() => expect(rowsOf("Model policy")).toHaveLength(2));
    expect(screen.queryByRole("button", { name: /allow a model/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /request change/i })).toBeNull();
  });
});

describe("§10 Usage — the rollup, and the two things it must not say", () => {
  it("renders the per-model table sorted Spend desc with the unpriced lane last", async () => {
    setup("/models?tab=usage");
    await waitFor(() => expect(rowsOf("Usage by model")).toHaveLength(3));
    // By identity: spend order, and the unpriced lane is last despite its
    // 9,021 calls — it is not a cheap lane, it is an unpriced one.
    expect(rowsOf("Usage by model")).toEqual([
      "claude-fable-5 frontier-api",
      "mistral-large frontier-api",
      "qwen3-32b private-vllm",
    ]);
    expect(screen.getByRole("columnheader", { name: "Spend" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  it("keeps the unpriced lane last when the sort is REVERSED", async () => {
    const user = setup("/models?tab=usage");
    await waitFor(() => expect(rowsOf("Usage by model")).toHaveLength(3));
    await user.click(screen.getByRole("button", { name: "Spend" }));
    expect(rowsOf("Usage by model")).toEqual([
      "mistral-large frontier-api",
      "claude-fable-5 frontier-api",
      "qwen3-32b private-vllm",
    ]);
  });

  it("prices the self-hosted lane as UNPRICED, and $0.00 appears nowhere", async () => {
    setup("/models?tab=usage");
    await waitFor(() => expect(rowsOf("Usage by model")).toHaveLength(3));
    const row = screen.getByText("qwen3-32b").closest("tr");
    expect(within(row as HTMLElement).getByText("UNPRICED")).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toMatch(/\$0\.00/);
    expect(screen.getByText("$1,284.06")).toBeInTheDocument();
  });

  it("leaves a token total the server will not vouch for as an em dash", async () => {
    setup("/models?tab=usage");
    await waitFor(() => expect(rowsOf("Usage by model")).toHaveLength(3));
    const row = screen.getByText("mistral-large").closest("tr");
    const cells = within(row as HTMLElement)
      .getAllByRole("cell")
      .map((cell) => cell.textContent);
    // Model · Lane, Calls, Input, Output, Spend.
    expect(cells[2]).toBe("—");
    expect(cells[3]).toBe("—");
    // …and the summary tiles do the same rather than summing a partial column:
    // the served totals, verbatim, with the absent ones absent.
    const tiles = [...document.querySelectorAll(".ne-models__tile")].map((t) => t.textContent);
    expect(tiles).toEqual([
      "Calls9521",
      "Input tokens—",
      "Output tokens—",
      "Spend$1,296.46 + unpriced",
    ]);
  });

  it("says the spend total is partial when any lane is unpriced", async () => {
    setup("/models?tab=usage");
    await waitFor(() => expect(rowsOf("Usage by model")).toHaveLength(3));
    const spend = screen.getByText("$1,296.46 + unpriced");
    expect(spend).toHaveAttribute("title", "Self-hosted lanes report no market price");
  });

  it("states the per-principal gap where that table would be, and renders no such table", async () => {
    setup("/models?tab=usage");
    await waitFor(() => expect(rowsOf("Usage by model")).toHaveLength(3));
    expect(screen.getByText("Per-user usage isn’t recorded yet.")).toBeInTheDocument();
    expect(screen.getAllByRole("table")).toHaveLength(1);
  });

  it("offers the window control, defaulting to the current month", async () => {
    setup("/models?tab=usage");
    await waitFor(() => expect(rowsOf("Usage by model")).toHaveLength(3));
    const now = new Date();
    const current = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
    expect(screen.getByLabelText("Month")).toHaveValue(current);
  });

  it("clears the previous month while the newly selected month is in flight", async () => {
    const asked: string[] = [];
    let resolveNext: ((view: ModelsUsageView) => void) | undefined;
    stub.client = {
      posture: () => Promise.resolve(posture),
      models: () => Promise.resolve(POLICY),
      modelsUsage: (window: string) => {
        asked.push(window);
        if (asked.length === 1) return Promise.resolve({ ...USAGE, window });
        return new Promise<ModelsUsageView>((resolve) => {
          resolveNext = resolve;
        });
      },
    };
    const user = setup("/models?tab=usage");
    await screen.findByText("claude-fable-5");
    const select = screen.getByLabelText("Month") as HTMLSelectElement;
    const previous = select.options.item(1)?.value;
    expect(previous).toBeTruthy();

    await user.selectOptions(select, previous!);
    await waitFor(() => expect(asked).toHaveLength(2));
    expect(screen.queryByText("claude-fable-5")).toBeNull();
    expect(screen.queryByText("$1,296.46 + unpriced")).toBeNull();

    resolveNext?.({
      window: previous!,
      models: [{ ...USAGE.models[0]!, model_id: "new-window-model" }],
      totals: { ...USAGE.totals, calls: 7 },
    });
    expect(await screen.findByText("new-window-model")).toBeInTheDocument();
  });

  it("renders the SERVER's refusal of the usage read, not a client-side denial", async () => {
    stub.client = {
      posture: () => Promise.resolve(posture),
      models: () => Promise.resolve(POLICY),
      modelsUsage: () => Promise.reject(new ApiError("Forbidden", 403, "requires an ops key")),
    };
    setup("/models?tab=usage");
    expect(
      await screen.findByText(
        "Your role doesn’t include model usage. Contact your administrator for access.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText("0 models")).toBeNull();
  });

  it("a FAILED usage read is an outage, never a role boundary", async () => {
    stub.client = {
      posture: () => Promise.resolve(posture),
      models: () => Promise.resolve(POLICY),
      modelsUsage: () => Promise.reject(new Error("network down")),
    };
    setup("/models?tab=usage");
    expect(await screen.findByText("Couldn’t load usage.")).toBeInTheDocument();
    expect(screen.queryByText(/doesn’t include model usage/)).toBeNull();
  });
});
