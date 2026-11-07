// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { currentEntityHref, verifiedCitationLink } from "./verifiedQueryLinks";

describe("verified query citation links", () => {
  it("leads with the served human label while retaining the immutable id", () => {
    expect(
      verifiedCitationLink(
        "urn:ne:entity:acct / 7",
        ["urn:ne:entity:acct / 7", "Meridian Auto"],
        ["account", "name"],
        { label_var: "name" },
      ),
    ).toEqual({
      immutableId: "urn:ne:entity:acct / 7",
      entityKey: "acct / 7",
      humanLabel: "Meridian Auto",
      href: "/knowledge/graph?entity=acct+%2F+7&mode=record&depth=1",
    });
  });

  it("degrades to the immutable id and never invents a route for non-entity citations", () => {
    expect(
      verifiedCitationLink("source:row-9", ["Friendly"], ["name"], { label_var: "name" }),
    ).toEqual({
      immutableId: "source:row-9",
      entityKey: null,
      humanLabel: "Friendly",
      href: null,
    });
    expect(currentEntityHref("acct?7")).toBe(
      "/knowledge/graph?entity=acct%3F7&mode=record&depth=1",
    );
  });
});
