// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 SERVING — the panel on the Models screen, driven by the governed read.
//
// The guards, each with its counter-direction (docs/testing/vacuous-guards.md):
// a reachable lane renders SERVING with the model the ENDPOINT reported; an
// unreachable one renders UNREACHABLE and NO model name — never a stale
// success re-labelled current; a stale observation is visibly stale ("Last
// answered …"); a deployment with no probeable lane renders NO section rather
// than a fabricated row (with a presence control in the tests above it); a
// server refusal renders nothing (the Home-tile omission precedent) and no
// console-authored refusal prose; a failed read is an outage note, never an
// empty deployment.

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { LaneServingView, PostureView, ServingStatusView } from "../api/types";
import { AuthStub } from "../test/authStub";
import { POSTURE } from "../test/mocks/fixtures";
import { ModelsPage } from "./ModelsPage";

const stub = vi.hoisted(() => ({ client: {} as Record<string, unknown> }));

vi.mock("../auth/useApiClient", () => ({
  useApiClient: () => stub.client,
}));

const posture: PostureView = { ...POSTURE };

function servingLane(over: Partial<LaneServingView> = {}): LaneServingView {
  return {
    model_id: "private-default",
    lane: "private-vllm",
    endpoint: "http://10.9.8.7:8000",
    configured_model: "qwen3-32b",
    state: "ok",
    checked_at: new Date(Date.now() - 4_000).toISOString(),
    latency_ms: 128,
    http_status: 200,
    served_models: ["qwen3-32b"],
    serves_configured_model: true,
    observed_context_window: 16384,
    engine_version: "0.12.0",
    last_ok_at: new Date(Date.now() - 4_000).toISOString(),
    ...over,
  };
}

function serve(view: ServingStatusView | (() => Promise<ServingStatusView>)) {
  stub.client = {
    posture: () => Promise.resolve(posture),
    modelsServing: typeof view === "function" ? view : () => Promise.resolve(view),
  };
}

function setup(path = "/models") {
  render(
    <AuthStub>
      <MemoryRouter initialEntries={[path]}>
        <ModelsPage />
      </MemoryRouter>
    </AuthStub>,
  );
}

async function section(): Promise<HTMLElement> {
  return await screen.findByRole("region", { name: "Serving health" });
}

beforeEach(() => {
  stub.client = { posture: () => Promise.resolve(posture) };
});

describe("§10 Serving — a reachable private lane", () => {
  it("renders SERVING with the model, window, engine and endpoint the server observed", async () => {
    const lane = servingLane();
    serve({ lanes: [lane] });
    setup();
    const panel = await section();
    expect(within(panel).getByLabelText("SERVING")).toBeInTheDocument();
    // The MODEL is the endpoint's own answer, not the table's configured id.
    expect(within(panel).getByText("qwen3-32b")).toBeInTheDocument();
    expect(within(panel).getByText("10.9.8.7:8000")).toBeInTheDocument();
    expect(within(panel).getByText("16,384 tokens")).toBeInTheDocument();
    expect(within(panel).getByText("vLLM 0.12.0")).toBeInTheDocument();
    expect(within(panel).getByText("128 ms")).toBeInTheDocument();
    expect(within(panel).getByRole("heading", { name: "Private serving health" })).toBeVisible();
    expect(
      within(panel).getByText(
        "A healthy endpoint is not the same as “available for this run in Agents.” The server decides availability for each run.",
      ),
    ).toBeVisible();
    // The verdict carries its AGE, machine-readable, with the absolute form
    // in the title (§ vocabulary: relative in the row, absolute in `title`).
    const time = panel.querySelector("time");
    expect(time?.textContent).toBe("just now");
    expect(time?.getAttribute("datetime")).toBe(lane.checked_at);
    // …and none of the failure vocabulary is anywhere near it.
    expect(within(panel).queryByLabelText("UNREACHABLE")).toBeNull();
    expect(within(panel).queryByText(/Hasn’t answered/)).toBeNull();
  });

  it.each(["/models", "/models?tab=routing"])(
    "keeps endpoint health before the %s content in the presenter viewport",
    async (path) => {
      serve({ lanes: [servingLane()] });
      setup(path);
      const panel = await section();
      const tabpanel = screen.getByRole("tabpanel");
      expect(panel.compareDocumentPosition(tabpanel) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(
        0,
      );
    },
  );

  it("keeps the server-served endpoint and authority distinction intact at mobile width", async () => {
    const previous = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    try {
      serve({ lanes: [servingLane()] });
      setup();
      const panel = await section();
      expect(within(panel).getByText("10.9.8.7:8000")).toBeVisible();
      expect(within(panel).getByText(/server decides availability for each run/)).toBeVisible();
    } finally {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: previous });
    }
  });

  it("renders a model DISAGREEMENT as both names, not as agreement", async () => {
    serve({
      lanes: [servingLane({ served_models: ["llama-guard-8b"], serves_configured_model: false })],
    });
    setup();
    const panel = await section();
    expect(
      within(panel).getByText("Reports llama-guard-8b — the policy names qwen3-32b"),
    ).toBeInTheDocument();
    expect(within(panel).getByLabelText("DEGRADED")).toBeInTheDocument();
    expect(within(panel).queryByLabelText("SERVING")).toBeNull();
  });

  it("flags an empty model list against the configured model", async () => {
    serve({
      lanes: [
        servingLane({
          served_models: [],
          serves_configured_model: false,
          observed_context_window: null,
        }),
      ],
    });
    setup();
    const panel = await section();
    expect(within(panel).getByLabelText("DEGRADED")).toBeInTheDocument();
    expect(
      within(panel).getByText("Reports no models — the policy names qwen3-32b"),
    ).toBeInTheDocument();
    expect(within(panel).queryByLabelText("SERVING")).toBeNull();
  });
});

describe("§10 Serving — down is down, stale is stale", () => {
  it("an unreachable lane renders UNREACHABLE and never a model name", async () => {
    serve({
      lanes: [
        servingLane({
          state: "unreachable",
          served_models: [],
          serves_configured_model: null,
          observed_context_window: null,
          engine_version: null,
          latency_ms: null,
          http_status: null,
          last_ok_at: null,
        }),
      ],
    });
    setup();
    const panel = await section();
    expect(within(panel).getByLabelText("UNREACHABLE")).toBeInTheDocument();
    expect(
      within(panel).getByText("No successful answer observed since this service started."),
    ).toBeInTheDocument();
    // The counter-direction: no SERVING word, and no model name — rendering
    // the configured id under a dead endpoint would be the label this panel
    // exists to replace.
    expect(within(panel).queryByLabelText("SERVING")).toBeNull();
    expect(within(panel).queryByText("qwen3-32b")).toBeNull();
  });

  it("a lane that answered before is DOWN with its last answer's age — not up", async () => {
    serve({
      lanes: [
        servingLane({
          state: "unreachable",
          served_models: [],
          serves_configured_model: null,
          observed_context_window: null,
          engine_version: null,
          latency_ms: null,
          http_status: null,
          last_ok_at: new Date(Date.now() - 5 * 60_000).toISOString(),
        }),
      ],
    });
    setup();
    const panel = await section();
    expect(within(panel).getByLabelText("UNREACHABLE")).toBeInTheDocument();
    expect(within(panel).getByText("Last answered 5 min ago.")).toBeInTheDocument();
    expect(within(panel).queryByLabelText("SERVING")).toBeNull();
  });

  it("a wedged endpoint is NO ANSWER — distinct from unreachable", async () => {
    serve({
      lanes: [
        servingLane({
          state: "timeout",
          served_models: [],
          serves_configured_model: null,
          observed_context_window: null,
          engine_version: null,
          latency_ms: null,
          http_status: null,
          last_ok_at: null,
        }),
      ],
    });
    setup();
    const panel = await section();
    expect(within(panel).getByLabelText("NO ANSWER")).toBeInTheDocument();
    expect(within(panel).queryByLabelText("UNREACHABLE")).toBeNull();
  });
});

describe("§10 Serving — when the section does not exist", () => {
  it("a deployment with no probeable lane renders NO section, not an empty shell", async () => {
    serve({ lanes: [] });
    setup();
    // The page itself is up (control: the lanes table renders)…
    await screen.findByRole("table", { name: "Model policy" });
    // …and there is no Serving section and no fabricated row.
    expect(screen.queryByRole("region", { name: "Serving health" })).toBeNull();
    expect(screen.queryByText("Serving")).toBeNull();
  });

  it("a client without the read renders nothing — no dead section", async () => {
    stub.client = { posture: () => Promise.resolve(posture) };
    setup();
    await screen.findByRole("table", { name: "Model policy" });
    expect(screen.queryByRole("region", { name: "Serving health" })).toBeNull();
  });

  it("the SERVER's refusal renders nothing — and no console-authored prose", async () => {
    serve(() => Promise.reject(new ApiError("Forbidden", 403, "requires an ops key")));
    setup();
    await screen.findByRole("table", { name: "Model policy" });
    expect(screen.queryByRole("region", { name: "Serving health" })).toBeNull();
    expect(screen.queryByText(/doesn't include serving/)).toBeNull();
  });

  it("a FAILED read is an outage note with a retry — never an empty deployment", async () => {
    serve(() => Promise.reject(new Error("network down")));
    setup();
    const panel = await section();
    expect(within(panel).getByText("Couldn’t load serving status.")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // The counter-direction of the omission rule: an outage is NOT omitted.
    expect(screen.queryByText(/doesn't include/)).toBeNull();
  });
});
