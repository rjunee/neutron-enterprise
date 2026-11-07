// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 Policy — the row rules, without a DOM.
//
// Every assertion below is paired with the case that would pass a weaker rule:
// an ordering test that a served-order pass-through would satisfy, a filter test
// asserted by identity rather than by count, and a masking test that proves the
// absent case renders nothing rather than four dots.

import { describe, expect, it } from "vitest";
import type { ModelLaneView } from "../api/types";
import { sortRows } from "../primitives/tableSort";
import {
  applyLaneFilter,
  laneOptions,
  maskedCredential,
  modelCount,
  policyRowsFromEntries,
  policyRowsFromLanes,
} from "./policy";
import type { ModelEntry, PolicyRow } from "./policy";

const LANES: ModelLaneView[] = [
  {
    model_id: "qwen3-32b",
    provider: "vllm",
    lane: "private-vllm",
    configured_model: "Qwen/Qwen3-32B",
    context_window: 32768,
  },
  {
    model_id: "mistral-large",
    provider: "mistral",
    lane: "frontier-api",
    configured_model: "mistral-large-latest",
    context_window: 128000,
  },
  {
    model_id: "llama-3-70b",
    provider: "vllm",
    lane: "private-vllm",
    configured_model: "meta-llama/Llama-3-70B",
    context_window: 8192,
  },
  {
    model_id: "claude-fable-5",
    provider: "anthropic",
    lane: "frontier-api",
    configured_model: "claude-fable-5",
    context_window: 1000000,
  },
];

/** The one column the default sort names, as <DataTable> declares it. */
const LANE_COLUMN = [{ key: "lane", sortValue: (row: PolicyRow) => row.lane }];

const ids = (rows: readonly PolicyRow[]) => rows.map((row) => row.model_id);

describe("the lane rows", () => {
  it("carries only the five fields the posture read serves", () => {
    const [row] = policyRowsFromLanes([LANES[0] as ModelLaneView]);
    expect(row).toMatchObject({
      model_id: "qwen3-32b",
      provider: "vllm",
      lane: "private-vllm",
      // The alias and the model it resolves to are DIFFERENT facts: a row that
      // echoed model_id here would answer "what is this lane?" with its own
      // name, which is the defect this pair of fields closed.
      configured_model: "Qwen/Qwen3-32B",
      context_window: 32768,
      binding: null,
      tasks: null,
      pending: null,
    });
    expect(row?.configured_model).not.toBe(row?.model_id);
  });

  it("names the model for a FRONTIER lane on the same terms as a private one", () => {
    // The serving panel can never cover a frontier lane (ne-api must not probe
    // a provider host), so configuration is that row's only source — and it is
    // carried for every lane, not special-cased onto the frontier ones.
    const rows = policyRowsFromLanes(LANES);
    const frontier = rows.find((row) => row.model_id === "mistral-large");
    const priv = rows.find((row) => row.model_id === "qwen3-32b");
    expect(frontier?.configured_model).toBe("mistral-large-latest");
    expect(frontier?.context_window).toBe(128000);
    // Symmetry: the same two keys are populated on both, from the same builder.
    expect(Object.keys(frontier ?? {}).sort()).toEqual(Object.keys(priv ?? {}).sort());
  });

  it("keys a row by all three fields, so one model in two lanes is two rows", () => {
    const rows = policyRowsFromLanes([
      {
        model_id: "qwen3-32b",
        provider: "vllm",
        lane: "private-vllm",
        configured_model: "Qwen/Qwen3-32B",
        context_window: 32768,
      },
      {
        model_id: "qwen3-32b",
        provider: "vllm",
        lane: "batch",
        configured_model: "Qwen/Qwen3-32B",
        context_window: 32768,
      },
    ]);
    expect(new Set(rows.map((row) => row.key)).size).toBe(2);
  });

  it("orders by Model, so the table's stable sort by Lane reads Lane then Model", () => {
    // The served order is neither the answer nor its reverse: a builder that
    // passed its input through would fail here.
    expect(ids(policyRowsFromLanes(LANES))).toEqual([
      "claude-fable-5",
      "llama-3-70b",
      "mistral-large",
      "qwen3-32b",
    ]);
    const sorted = sortRows(policyRowsFromLanes(LANES), LANE_COLUMN, { key: "lane", dir: "asc" });
    expect(ids(sorted)).toEqual(["claude-fable-5", "mistral-large", "llama-3-70b", "qwen3-32b"]);
    expect(sorted.map((row) => row.lane)).toEqual([
      "frontier-api",
      "frontier-api",
      "private-vllm",
      "private-vllm",
    ]);
  });

  it("applies the same ordering to the policy router's entries", () => {
    const entries: ModelEntry[] = LANES.map((lane) => ({
      ...lane,
      binding: null,
      task_routes: [],
      pending_change: null,
    }));
    expect(ids(policyRowsFromEntries(entries))).toEqual([
      "claude-fable-5",
      "llama-3-70b",
      "mistral-large",
      "qwen3-32b",
    ]);
  });

  it("carries the router's binding, routes and parked change onto the row", () => {
    const [row] = policyRowsFromEntries([
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
    ]);
    expect(row?.binding?.endpoint_host).toBe("frontier.example");
    expect(row?.tasks).toEqual([{ task: "review" }]);
    expect(row?.pending?.request_id).toBe("req-77");
    // ONE ROW SHAPE: the router path carries the model identity too, so which
    // read answered never changes whether a lane can be resolved to a model.
    expect(row?.configured_model).toBe("claude-fable-5");
    expect(row?.context_window).toBe(1000000);
  });
});

describe("the lane filter", () => {
  const rows = policyRowsFromLanes(LANES);

  it("offers the all-lanes default plus each SERVED lane, once, A–Z", () => {
    expect(laneOptions(rows)).toEqual([
      { value: "all", label: "All lanes" },
      { value: "frontier-api", label: "frontier-api" },
      { value: "private-vllm", label: "private-vllm" },
    ]);
  });

  it("offers no lane at all when the deployment routes to none", () => {
    expect(laneOptions([])).toEqual([{ value: "all", label: "All lanes" }]);
  });

  it("narrows to the rows OF THAT LANE, by identity", () => {
    expect(ids(applyLaneFilter(rows, "private-vllm"))).toEqual(["llama-3-70b", "qwen3-32b"]);
    expect(ids(applyLaneFilter(rows, "frontier-api"))).toEqual(["claude-fable-5", "mistral-large"]);
  });

  it("CONTROL: the default narrows nothing", () => {
    expect(applyLaneFilter(rows, "all")).toHaveLength(4);
  });
});

describe("the masked credential", () => {
  it("shows the last four the server sent, and nothing more", () => {
    expect(maskedCredential({ endpoint_host: "h", credential_last4: "9f2c" })).toBe("•••• 9f2c");
  });

  it("returns nothing when there is no credential reference — not four dots", () => {
    expect(maskedCredential({ endpoint_host: "h", credential_last4: null })).toBeNull();
    expect(maskedCredential(null)).toBeNull();
  });
});

describe("the count", () => {
  it("agrees with itself about the noun", () => {
    expect(modelCount(0)).toBe("0 models");
    expect(modelCount(1)).toBe("1 model");
    expect(modelCount(12)).toBe("12 models");
  });
});
