// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import type { GraphEntityListWire } from "../../api/types";
import {
  EMPTY_ENTITY_RAIL_FILTERS,
  FACET_ABSENT_LABEL,
  appendEntityRailPage,
  classifyEntityRailFailure,
  entityRailFilterKey,
  entityRailOwnerId,
  entityRailQuery,
  facetControlOptions,
  facetVocabularyNote,
  ownsEntityRailRequest,
  settleEntityRailPage,
  settleFacetVocabulary,
} from "./entityRailState";

const FILTERS = {
  query: " Acme + West ",
  entityClass: "Account",
  source: "M365 SharePoint",
  assurance: "confirmed" as const,
};

const FIRST: GraphEntityListWire = {
  entities: [
    {
      key: "entity-zulu",
      entity_class: "Account",
      display_name: "Zulu Holdings",
      source_system: "salesforce",
      assurance: "source",
    },
  ],
  next_cursor: "opaque/+ cursor",
};

describe("entity rail state", () => {
  it("maps the complete filter tuple and opaque cursor without defaults or normalization", () => {
    expect(entityRailQuery(FILTERS, "opaque/+ cursor")).toEqual({
      q: " Acme + West ",
      entity_class: "Account",
      source: "M365 SharePoint",
      assurance: "confirmed",
      cursor: "opaque/+ cursor",
    });
    expect(entityRailQuery(EMPTY_ENTITY_RAIL_FILTERS)).toEqual({});
  });

  it("binds ownership to client identity, principal, request key and sequence", () => {
    const clientA = {};
    const clientB = {};
    const candidate = {
      ownerId: entityRailOwnerId(clientA, "alice"),
      filterKey: "filters",
      requestKey: "filters:cursor",
      sequence: 7,
    };
    expect(ownsEntityRailRequest(candidate, { ...candidate })).toBe(true);
    expect(
      ownsEntityRailRequest(candidate, {
        ...candidate,
        ownerId: entityRailOwnerId(clientB, "alice"),
      }),
    ).toBe(false);
    expect(
      ownsEntityRailRequest(candidate, {
        ...candidate,
        ownerId: entityRailOwnerId(clientA, "bob"),
      }),
    ).toBe(false);
    expect(ownsEntityRailRequest(candidate, { ...candidate, requestKey: "other" })).toBe(false);
    expect(ownsEntityRailRequest(candidate, { ...candidate, filterKey: "other" })).toBe(false);
    expect(ownsEntityRailRequest(candidate, { ...candidate, sequence: 8 })).toBe(false);
  });

  it("keeps genuine empty and filtered no-match states distinct", () => {
    const page = { entities: [], next_cursor: null };
    const ownership = {
      ownerId: "owner",
      filterKey: "filters",
      requestKey: "initial",
      sequence: 1,
    };
    expect(settleEntityRailPage(EMPTY_ENTITY_RAIL_FILTERS, page, ownership)).toEqual({
      phase: "empty",
    });
    expect(
      settleEntityRailPage({ ...EMPTY_ENTITY_RAIL_FILTERS, query: "missing" }, page, ownership),
    ).toEqual({ phase: "no-match" });
  });

  it("preserves served order and appends only to the same filter window", () => {
    const ownership = {
      ownerId: "owner",
      filterKey: entityRailFilterKey(FILTERS),
      requestKey: "initial",
      sequence: 1,
    };
    const first = settleEntityRailPage(FILTERS, FIRST, ownership);
    const key = entityRailFilterKey(FILTERS);
    const second: GraphEntityListWire = {
      entities: [
        {
          key: "entity-alpha",
          entity_class: "Person",
          display_name: "Alpha Contact",
          source_system: "m365",
          assurance: "confirmed",
        },
      ],
      next_cursor: null,
    };
    expect(appendEntityRailPage(first, "old-filter", second, ownership)).toBeNull();
    expect(appendEntityRailPage(first, key, second, { ...ownership, sequence: 2 })).toMatchObject({
      phase: "ready",
      entities: [FIRST.entities[0], second.entities[0]],
      nextCursor: null,
    });
  });

  it("classifies deployment-off, refusal and broken reads independently", () => {
    expect(classifyEntityRailFailure(new ApiError("off", 404))).toBe("off");
    expect(classifyEntityRailFailure(new ApiError("off", 503))).toBe("off");
    expect(classifyEntityRailFailure(new ApiError("no", 403))).toBe("refused");
    expect(classifyEntityRailFailure(new ApiError("bad", 500))).toBe("error");
    expect(classifyEntityRailFailure(new Error("network"))).toBe("error");
  });
});

describe("served filter vocabulary", () => {
  it("offers only the served values, in served order, under an Any lead", () => {
    expect(facetControlOptions(["AutoLoan", "Customer", "Document"], "Any class", "")).toEqual({
      disabled: false,
      options: [
        { value: "", label: "Any class" },
        { value: "AutoLoan", label: "AutoLoan" },
        { value: "Customer", label: "Customer" },
        { value: "Document", label: "Document" },
      ],
    });
  });

  it("renders an em dash on an inert control for an empty served list", () => {
    // Absent is an em dash, never a 0 and never a "No" — and never the
    // compile-time class registry sneaking back in as a fallback.
    expect(facetControlOptions([], "Any class", "")).toEqual({
      disabled: true,
      options: [{ value: "", label: FACET_ABSENT_LABEL }],
    });
    expect(facetControlOptions(null, "Any source", "")).toEqual({
      disabled: true,
      options: [{ value: "", label: FACET_ABSENT_LABEL }],
    });
  });

  it("echoes back an active filter the server did not serve, and only that", () => {
    const control = facetControlOptions(["Customer"], "Any class", "Ticket");
    expect(control.disabled).toBe(false);
    expect(control.options.map((option) => option.value)).toEqual(["", "Customer", "Ticket"]);
    // Not duplicated when the server DID serve it.
    expect(
      facetControlOptions(["Customer"], "Any class", "Customer").options.map((o) => o.value),
    ).toEqual(["", "Customer"]);
    // An inert control still carries the route's own filter.
    expect(facetControlOptions([], "Any class", "Ticket").options.map((o) => o.value)).toEqual([
      "",
      "Ticket",
    ]);
  });

  it("distinguishes loading, unavailable, empty and capped in its one factual note", () => {
    expect(facetVocabularyNote({ phase: "loading" })).toBeNull();
    expect(facetVocabularyNote({ phase: "unavailable" })).toContain("unavailable");
    expect(
      facetVocabularyNote(
        settleFacetVocabulary({ entity_classes: [], source_systems: [], truncated: false }),
      ),
    ).toContain("no entities are visible to you");
    expect(
      facetVocabularyNote(
        settleFacetVocabulary({
          entity_classes: ["Customer"],
          source_systems: ["m365"],
          truncated: true,
        }),
      ),
    ).toContain("capped its scan");
    expect(
      facetVocabularyNote(
        settleFacetVocabulary({
          entity_classes: ["Customer"],
          source_systems: ["m365"],
          truncated: false,
        }),
      ),
    ).toBeNull();
  });
});
