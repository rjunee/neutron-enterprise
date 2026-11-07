// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 SERVING — the row semantics, as pure functions.
//
// The load-bearing rules: the four verdict words do not collapse into each
// other; a non-SERVING row never carries a model name (rendering the
// configured one there would be the label this surface replaces); and "down
// since when" versus "not observed in this service process" are different
// sentences, chosen by the served last_ok_at, not guessed.

import { describe, expect, it } from "vitest";
import type { LaneServingView } from "../api/types";
import { availabilityLine, endpointHost, pillWord, reportedModel, servingReader } from "./serving";
import type { ApiClient } from "../api/client";

const NOW = Date.parse("2026-08-03T12:00:00Z");

function lane(over: Partial<LaneServingView> = {}): LaneServingView {
  return {
    model_id: "private-default",
    lane: "private-vllm",
    endpoint: "http://10.9.8.7:8000",
    configured_model: "qwen3-32b",
    state: "ok",
    checked_at: "2026-08-03T11:59:56Z",
    latency_ms: 128,
    http_status: 200,
    served_models: ["qwen3-32b"],
    serves_configured_model: true,
    observed_context_window: 16384,
    engine_version: "0.12.0",
    last_ok_at: "2026-08-03T11:59:56Z",
    ...over,
  };
}

describe("pillWord — four diagnoses that must not collapse", () => {
  it("maps each server verdict to its own word", () => {
    expect(pillWord("ok", true)).toBe("SERVING");
    expect(pillWord("ok", false)).toBe("DEGRADED");
    expect(pillWord("ok", null)).toBe("DEGRADED");
    expect(pillWord("unreachable")).toBe("UNREACHABLE");
    expect(pillWord("timeout")).toBe("NO ANSWER");
    expect(pillWord("http_error")).toBe("DEGRADED");
    expect(pillWord("malformed")).toBe("DEGRADED");
    expect(pillWord("not_probed")).toBe("NOT CHECKED");
  });

  it("no failure verdict ever reads SERVING", () => {
    for (const state of [
      "unreachable",
      "timeout",
      "http_error",
      "malformed",
      "not_probed",
    ] as const) {
      expect(pillWord(state)).not.toBe("SERVING");
    }
  });
});

describe("availabilityLine — stale and never-answered are different sentences", () => {
  it("says nothing under a lane that is up", () => {
    expect(availabilityLine(lane(), NOW)).toBeNull();
  });

  it("null history makes only a process-lifetime claim", () => {
    const down = lane({ state: "unreachable", served_models: [], last_ok_at: null });
    expect(availabilityLine(down, NOW)).toBe(
      "No successful answer observed since this service started.",
    );
  });

  it("a lane that answered before says WHEN — not that it is up", () => {
    const down = lane({
      state: "unreachable",
      served_models: [],
      last_ok_at: "2026-08-03T11:55:00Z",
    });
    expect(availabilityLine(down, NOW)).toBe("Last answered 5 min ago.");
  });

  it("a refused address gets its own factual line", () => {
    const refused = lane({ state: "not_probed", checked_at: null, served_models: [] });
    expect(availabilityLine(refused, NOW)).toBe("The configured address can’t be checked.");
  });
});

describe("reportedModel — the endpoint's word, only when it gave one", () => {
  it("renders the observed id on a serving lane", () => {
    expect(reportedModel(lane())).toEqual({ text: "qwen3-32b", mismatch: false });
  });

  it("a disagreement renders BOTH names, flagged", () => {
    const odd = lane({ served_models: ["llama-guard-8b"], serves_configured_model: false });
    expect(reportedModel(odd)).toEqual({
      text: "Reports llama-guard-8b — the policy names qwen3-32b",
      mismatch: true,
    });
  });

  it("an empty model list is an explicit flagged disagreement, not absence", () => {
    const empty = lane({ served_models: [], serves_configured_model: false });
    expect(reportedModel(empty)).toEqual({
      text: "Reports no models — the policy names qwen3-32b",
      mismatch: true,
    });
  });

  it("a non-serving lane renders NO model name — even if data leaked into the row", () => {
    // The defensive half of the server's own rule: were a failure row ever to
    // carry served_models, the console still must not caption a down lane
    // with a model name.
    const down = lane({ state: "unreachable", served_models: ["qwen3-32b"] });
    expect(reportedModel(down)).toBeNull();
    // Control (the guard is not just "always null"): the ok lane above rendered.
    expect(reportedModel(lane())).not.toBeNull();
  });
});

describe("endpointHost", () => {
  it("drops the scheme and keeps host:port", () => {
    expect(endpointHost("http://10.9.8.7:8000")).toBe("10.9.8.7:8000");
    expect(endpointHost("https://gpu.internal")).toBe("gpu.internal");
  });

  it("keeps an empty identity empty — never an invented address", () => {
    expect(endpointHost("")).toBe("");
  });
});

describe("servingReader — the probe pattern", () => {
  it("is null on a client without the read, and calls through on one with it", async () => {
    expect(servingReader({} as ApiClient)).toBeNull();
    const view = { lanes: [] };
    const client = { modelsServing: () => Promise.resolve(view) } as unknown as ApiClient;
    const reader = servingReader(client);
    expect(reader).not.toBeNull();
    await expect(reader?.()).resolves.toBe(view);
  });
});
