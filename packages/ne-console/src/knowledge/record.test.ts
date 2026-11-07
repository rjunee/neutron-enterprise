// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The record assembly rules, tested where they live: no DOM, no fixture screen,
// no provider. These are the parts of §7 that can be quietly wrong — a relation
// pointing the wrong way, a property that turns absence into a number, a
// provenance ref shortened past the point of being the ref.

import { describe, expect, it } from "vitest";
import type { GraphEdgeWire, GraphNodeWire, SubgraphWire } from "../api/types";
import {
  applyClassFilter,
  findEntity,
  propertyRows,
  propertyText,
  provenance,
  relationClasses,
  relationRows,
} from "./record";

function node(key: string, overrides: Partial<GraphNodeWire> = {}): GraphNodeWire {
  return {
    key,
    entity_class: "Account",
    display_name: key.toUpperCase(),
    source_system: "crm",
    source_ref: `crm/${key}`,
    properties: {},
    assurance: "source",
    ...overrides,
  };
}

function edge(
  key: string,
  from: string,
  to: string,
  overrides: Partial<GraphEdgeWire> = {},
): GraphEdgeWire {
  return {
    key,
    edge_kind: "WORKS_AT",
    from_key: from,
    to_key: to,
    source_system: "workday",
    source_ref: `hr/${key}`,
    assurance: "source",
    ...overrides,
  };
}

const SUB: SubgraphWire = {
  nodes: [
    node("acme"),
    node("dana", { entity_class: "Person", display_name: "Dana Ops" }),
    node("sam", { entity_class: "Person", display_name: "Sam Rep", assurance: "extracted" }),
  ],
  edges: [
    edge("e1", "dana", "acme"),
    edge("e2", "acme", "doc-7", { edge_kind: "ABOUT" }),
    edge("e3", "sam", "acme", { assurance: "extracted", source_ref: "llm:0123456789abcdef:sp/x" }),
    edge("e4", "dana", "sam", { edge_kind: "PEER" }),
  ],
  truncated: false,
};

describe("findEntity", () => {
  it("returns the anchor's own node", () => {
    expect(findEntity(SUB, "acme")?.display_name).toBe("ACME");
  });

  it("returns null for a key the window does not hold", () => {
    expect(findEntity(SUB, "nope")).toBeNull();
  });
});

describe("relationRows", () => {
  it("keeps only the edges that touch the anchor, and reads each one's direction", () => {
    const rows = relationRows(SUB, "acme");
    expect(rows.map((r) => r.key)).toEqual(["e1", "e2", "e3"]);
    expect(rows.map((r) => r.direction)).toEqual(["in", "out", "in"]);
    // e4 connects two other entities and is not this entity's relation.
    expect(rows.some((r) => r.key === "e4")).toBe(false);
  });

  it("names the other end from its node, and falls back to the key when the window has none", () => {
    const rows = relationRows(SUB, "acme");
    const inbound = rows.find((r) => r.key === "e1");
    expect(inbound?.otherKey).toBe("dana");
    expect(inbound?.otherName).toBe("Dana Ops");
    expect(inbound?.otherClass).toBe("Person");

    // doc-7 is an edge endpoint the server did not return a node for. The row
    // still renders — its key is a real served field — and it claims no name.
    const outbound = rows.find((r) => r.key === "e2");
    expect(outbound?.otherKey).toBe("doc-7");
    expect(outbound?.otherName).toBeNull();
    expect(outbound?.otherClass).toBeNull();
  });

  it("flags the ASSERTING relation's own assurance, not its endpoints'", () => {
    const rows = relationRows(SUB, "acme");
    // e3's edge is extracted -> provisional.
    expect(rows.find((r) => r.key === "e3")?.provisional).toBe(true);
    // e1's edge is vouched-for, even though nothing about sam is involved.
    expect(rows.find((r) => r.key === "e1")?.provisional).toBe(false);
  });

  it("yields exactly one row for a self-edge", () => {
    const loop: SubgraphWire = {
      nodes: [node("acme")],
      edges: [edge("self", "acme", "acme", { edge_kind: "SUCCEEDS" })],
      truncated: false,
    };
    const rows = relationRows(loop, "acme");
    expect(rows).toHaveLength(1);
    expect(rows[0]?.direction).toBe("out");
  });

  it("returns nothing for an entity with no edges", () => {
    expect(relationRows(SUB, "unknown")).toEqual([]);
  });
});

describe("the class filter", () => {
  it("lists only the classes actually present, sorted", () => {
    const rows = relationRows(SUB, "acme");
    expect(relationClasses(rows)).toEqual(["Person"]);
  });

  it("narrows to the rows of that class, by row identity", () => {
    const rows = relationRows(SUB, "acme");
    expect(applyClassFilter(rows, "Person").map((r) => r.key)).toEqual(["e1", "e3"]);
    expect(applyClassFilter(rows, "all").map((r) => r.key)).toEqual(["e1", "e2", "e3"]);
  });
});

describe("propertyText", () => {
  it("passes through what the server sent", () => {
    expect(propertyText("acme")).toBe("acme");
    expect(propertyText(42)).toBe("42");
    expect(propertyText(false)).toBe("false");
    expect(propertyText({ a: 1 })).toBe('{"a":1}');
  });

  it("never turns an absence into a value", () => {
    expect(propertyText(null)).toBeNull();
    expect(propertyText(undefined)).toBeNull();
    expect(propertyText("")).toBeNull();
  });

  it("keeps a real zero, which is a value and not an absence", () => {
    expect(propertyText(0)).toBe("0");
  });
});

describe("propertyRows", () => {
  it("keeps the server's key order", () => {
    const rows = propertyRows(node("acme", { properties: { region: "EU", tier: 2, note: null } }));
    expect(rows.map((r) => r.key)).toEqual(["region", "tier", "note"]);
    expect(rows.map((r) => r.value)).toEqual(["EU", "2", null]);
  });
});

describe("provenance", () => {
  it("leads with the evidencing ref of a stamped extraction and keeps the whole ref", () => {
    const p = provenance("llm:0123456789abcdef:sharepoint/runbook#3");
    expect(p.text).toBe("sharepoint/runbook#3");
    expect(p.title).toBe("llm:0123456789abcdef:sharepoint/runbook#3");
  });

  it("shows an ordinary ref as it is", () => {
    expect(provenance("crm/acct-1")).toEqual({ text: "crm/acct-1", title: "crm/acct-1" });
  });

  it("does not treat a near-miss as a stamp", () => {
    // A short digest is not the grammar; guessing at it would print half a ref.
    const p = provenance("llm:abc:sharepoint/x");
    expect(p.text).toBe("llm:abc:sharepoint/x");
  });
});
