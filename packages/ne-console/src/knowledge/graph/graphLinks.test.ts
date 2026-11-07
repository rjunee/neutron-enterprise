// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { graphCitationsHref, graphEntityHref, graphHref, graphModeHref } from "./graphLinks";
import { parseGraphRouteState } from "./graphRouteState";

function parsed(href: string) {
  const url = new URL(href, "https://console.invalid");
  expect(url.pathname).toBe("/knowledge/graph");
  return { url, state: parseGraphRouteState(url.searchParams) };
}

describe("canonical Graph links", () => {
  it("preserves entity, citations, depth and filters across Graph/Record mode switches", () => {
    const current =
      "entity=acct%2F7&mode=graph&depth=3&anchor=acct%2Froot&anchor=%20opaque%20&anchor=acct%2Froot" +
      "&citation=doc%2Fone&citation=doc%2Ftwo" +
      "&q=Meridian&class=Account&source=crm&assurance=source&trace_id=t%2F9";

    const record = parsed(graphModeHref("record", current));
    expect(record.state).toEqual({
      entity: "acct/7",
      mode: "record",
      depth: 3,
      anchors: ["acct/root", " opaque ", "acct/root"],
      citations: ["doc/one", "doc/two"],
      query: "Meridian",
      entityClass: "Account",
      source: "crm",
      assurance: "source",
    });
    expect(record.url.searchParams.get("trace_id")).toBe("t/9");

    expect(parsed(graphModeHref("graph", record.url.searchParams)).state).toEqual({
      ...record.state,
      mode: "graph",
    });
  });

  it("changes only the selected entity/view and safely encodes opaque relation keys", () => {
    const link = parsed(
      graphEntityHref("party / ?x=1&y=2 + #", {
        mode: "record",
        current:
          "entity=old&mode=graph&depth=2&q=party&class=Party&anchor=root%2F1&anchor=root%2F1&citation=source%2F1",
      }),
    );
    expect(link.state).toMatchObject({
      entity: "party / ?x=1&y=2 + #",
      mode: "record",
      depth: 2,
      query: "party",
      entityClass: "Party",
      anchors: ["root/1", "root/1"],
      citations: ["source/1"],
    });
    expect(link.url.search).toContain("entity=party+%2F+%3Fx%3D1%26y%3D2+%2B+%23");
  });

  it("preserves repeated citation identity while retaining discovery filters", () => {
    const link = parsed(
      graphCitationsHref(["doc/one?x=1", "urn:source:two&part"], {
        current:
          "entity=acct-7&mode=record&depth=4&anchor=%20root%20&anchor=%20root%20&q=risk&source=m365&unrelated=keep",
      }),
    );
    expect(link.state).toMatchObject({
      entity: "acct-7",
      mode: "graph",
      depth: 4,
      anchors: [" root ", " root "],
      citations: ["doc/one?x=1", "urn:source:two&part"],
      query: "risk",
      source: "m365",
    });
    expect(link.url.searchParams.get("unrelated")).toBe("keep");
  });

  it("builds a canonical path from complete state and refuses an empty entity key", () => {
    expect(
      graphHref({
        entity: null,
        mode: "graph",
        depth: 1,
        anchors: [],
        citations: [],
        query: "",
        entityClass: "",
        source: "",
        assurance: null,
      }),
    ).toBe("/knowledge/graph?mode=graph&depth=1");
    expect(() => graphEntityHref("")).toThrow("graph entity key must not be empty");
  });
});
