// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import {
  GRAPH_DEFAULT_DEPTH,
  parseGraphRouteState,
  serializeGraphRouteState,
  type GraphRouteState,
} from "./graphRouteState";

const DEFAULT_STATE: GraphRouteState = {
  entity: null,
  mode: "graph",
  depth: GRAPH_DEFAULT_DEPTH,
  anchors: [],
  citations: [],
  query: "",
  entityClass: "",
  source: "",
  assurance: null,
};

describe("canonical Graph route state", () => {
  it("parses shared selection, view and discovery filters without interpreting opaque values", () => {
    expect(
      parseGraphRouteState(
        "entity=acct%2Fwest%3Fsegment%3Da%26b&mode=record&depth=4" +
          "&anchor=acct%2Fwest&anchor=%20opaque%20&anchor=acct%2Fwest" +
          "&citation=doc%2Fone&citation=urn%3Aref%3Atwo" +
          "&q=%20Meridian%20&class=cco%3AAccount&source=m365%2Fbank&assurance=confirmed",
      ),
    ).toEqual({
      entity: "acct/west?segment=a&b",
      mode: "record",
      depth: 4,
      anchors: ["acct/west", " opaque ", "acct/west"],
      citations: ["doc/one", "urn:ref:two"],
      query: " Meridian ",
      entityClass: "cco:Account",
      source: "m365/bank",
      assurance: "confirmed",
    });
  });

  it.each(["depth=0", "depth=5", "depth=-1", "depth=1.5", "depth=NaN"])(
    "defaults an out-of-contract depth from %s",
    (query) => {
      expect(parseGraphRouteState(query).depth).toBe(GRAPH_DEFAULT_DEPTH);
    },
  );

  it("defaults unknown enum values and does not consume unrelated Library state", () => {
    expect(
      parseGraphRouteState(
        "tab=entities&library_anchor=legacy-key&mode=topology&assurance=guessed&anchor=&citation=&citation=kept",
      ),
    ).toEqual({ ...DEFAULT_STATE, citations: ["kept"] });
  });

  it("round-trips repeated citations and safely encodes reserved characters", () => {
    const state: GraphRouteState = {
      entity: "customer / ? & + # %",
      mode: "record",
      depth: 3,
      anchors: [" key /?# ", "duplicate", "duplicate"],
      citations: ["doc/one?x=1&y=2", "urn:ref:+/#"],
      query: "name + risk&",
      entityClass: "cco:Commercial Organization",
      source: "CRM/EU?tenant=1",
      assurance: "extracted",
    };

    const params = serializeGraphRouteState(
      state,
      "trace_id=trace%2F7&unrelated=one&unrelated=two&cursor=opaque-token",
    );
    expect(params.get("entity")).toBe(state.entity);
    expect(params.getAll("anchor")).toEqual(state.anchors);
    expect(params.getAll("citation")).toEqual(state.citations);
    expect(params.getAll("unrelated")).toEqual(["one", "two"]);
    expect(params.get("trace_id")).toBe("trace/7");
    expect(params.has("cursor")).toBe(false);
    expect(parseGraphRouteState(params)).toEqual(state);

    const encoded = params.toString();
    expect(encoded).toContain("entity=customer+%2F+%3F+%26+%2B+%23+%25");
    expect(encoded).toContain("anchor=+key+%2F%3F%23+");
    expect(encoded).not.toContain("entity=customer / ?");
  });

  it("refuses to mint out-of-contract depth or assurance state", () => {
    expect(() => serializeGraphRouteState({ ...DEFAULT_STATE, depth: 5 })).toThrow(
      "graph depth must be 1..4",
    );
    expect(() =>
      serializeGraphRouteState({ ...DEFAULT_STATE, assurance: "guessed" as "source" }),
    ).toThrow("invalid graph assurance filter");
  });

  it("accepts only one complete unambiguous opaque Run-evidence tuple", () => {
    const parsed = parseGraphRouteState(
      "mode=graph&agent=agent%2Fopaque&run=run%3F1&evidence=evidence%23a",
    );
    expect(parsed.evidenceFocus).toEqual({
      state: "ready",
      agentId: "agent/opaque",
      runId: "run?1",
      evidenceId: "evidence#a",
    });
    expect(serializeGraphRouteState(parsed).toString()).toBe(
      "mode=graph&depth=1&agent=agent%2Fopaque&run=run%3F1&evidence=evidence%23a",
    );
  });

  it.each([
    "agent=a&run=r&evidence=e",
    "mode=record&agent=a&run=r&evidence=e",
    "mode=graph&mode=graph&agent=a&run=r&evidence=e",
    "mode=graph&agent=a&run=r",
    "mode=graph&agent=a&run=r&evidence=",
    "mode=graph&agent=a&agent=a&run=r&evidence=e",
    "mode=graph&agent=a&run=r&evidence=e&entity=entity",
    "mode=graph&agent=a&run=r&evidence=e&anchor=entity",
    "mode=graph&agent=a&run=r&evidence=e&citation=source",
  ])("marks malformed or competing Run-evidence state unavailable: %s", (query) => {
    const parsed = parseGraphRouteState(query);
    expect(parsed.evidenceFocus).toEqual({ state: "invalid" });
    const serialized = serializeGraphRouteState(parsed);
    expect(serialized.has("agent")).toBe(false);
    expect(serialized.has("run")).toBe(false);
    expect(serialized.has("evidence")).toBe(false);
  });
});
