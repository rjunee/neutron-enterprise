// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §15's rules, without a DOM. Every one of them is a place the screen could
// quietly state something the server did not: a count that disagrees with the
// rows beside it, a filter that keeps a principal the server did not grant, an
// empty list sorted as though "no groups" were a group name, or a membership
// write that sends the whole list and reverts somebody else's change.

import { describe, expect, it } from "vitest";
import {
  applySurfaceFilter,
  approverCount,
  chipOverflow,
  grantLines,
  listSortValue,
  membershipDelta,
  peopleCount,
  roleCount,
  roleNames,
} from "./access";
import type { AccessPrincipalEntry, AccessRoleEntry } from "./access";

function principal(over: Partial<AccessPrincipalEntry> = {}): AccessPrincipalEntry {
  return {
    principal_id: "alice@example.com",
    groups: [],
    surfaces: [],
    grants: [],
    pending_change: null,
    ...over,
  };
}

describe("the toolbar counts", () => {
  it("agree with themselves at one and at many", () => {
    expect(peopleCount(0)).toBe("0 people");
    expect(peopleCount(1)).toBe("1 person");
    expect(peopleCount(12)).toBe("12 people");
    expect(roleCount(1)).toBe("1 role");
    expect(roleCount(3)).toBe("3 roles");
    expect(approverCount(1)).toBe("1 approver");
    expect(approverCount(2)).toBe("2 approvers");
  });
});

describe("the surface filter", () => {
  const rows = [
    principal({ principal_id: "mira@example.com", surfaces: ["Approvals", "Ops"] }),
    principal({ principal_id: "alice@example.com", surfaces: ["Improvement"] }),
    principal({ principal_id: "zed@example.com", surfaces: [] }),
  ];

  it("keeps exactly the principals the SERVER granted the surface", () => {
    // By identity, not cardinality: a length of one passes for the wrong one.
    expect(applySurfaceFilter(rows, "Ops").map((r) => r.principal_id)).toEqual([
      "mira@example.com",
    ]);
    expect(applySurfaceFilter(rows, "Improvement").map((r) => r.principal_id)).toEqual([
      "alice@example.com",
    ]);
  });

  it("never infers a grant: a principal the server granted nothing matches nothing", () => {
    for (const surface of ["Approvals", "Improvement", "Ops"]) {
      expect(applySurfaceFilter(rows, surface).map((r) => r.principal_id)).not.toContain(
        "zed@example.com",
      );
    }
  });

  it("CONTROL: 'all' passes the served window through untouched", () => {
    expect(applySurfaceFilter(rows, "all")).toBe(rows);
  });
});

describe("the chip list", () => {
  it("shows the limit and hands the rest to the overflow", () => {
    const { shown, rest } = chipOverflow(["a", "b", "c", "d", "e"], 3);
    expect(shown).toEqual(["a", "b", "c"]);
    // The overflow is a count of NAMED things, and the names come back so the
    // `+n` can carry them in its title rather than swallowing them.
    expect(rest).toEqual(["d", "e"]);
  });

  it("CONTROL: a list at or under the limit overflows nothing", () => {
    expect(chipOverflow(["a", "b"], 3)).toEqual({ shown: ["a", "b"], rest: [] });
  });
});

describe("the chip column's sort key", () => {
  it("is the served list, joined", () => {
    expect(listSortValue(["ops", "approvers"])).toBe("ops approvers");
  });

  it("is ABSENT for an empty list — 'no groups' is not the first group A–Z", () => {
    // sortRows() puts nulls last in BOTH directions, so a principal in no group
    // never floats to the top of a column the reader sorted to find groups.
    expect(listSortValue([])).toBeNull();
  });
});

describe("the grant provenance", () => {
  it("names the group that carries each surface, in the served order", () => {
    const entry = principal({
      grants: [
        { surface: "Approvals", via_group: "approvers" },
        { surface: "Ops", via_group: "ops" },
      ],
    });
    expect(grantLines(entry)).toEqual([
      { surface: "Approvals", via: "via group approvers" },
      { surface: "Ops", via: "via group ops" },
    ]);
  });

  it("CONTROL: a principal with no grants produces no lines, not an invented one", () => {
    expect(grantLines(principal())).toEqual([]);
  });
});

describe("the role checklist", () => {
  const roles: AccessRoleEntry[] = [
    { group: "stewards", surfaces: [], member_count: 0, members: [] },
    { group: "approvers", surfaces: [], member_count: 0, members: [] },
    { group: "ops", surfaces: [], member_count: 0, members: [] },
  ];

  it("offers exactly the groups the roles read served, A–Z", () => {
    expect(roleNames(roles)).toEqual(["approvers", "ops", "stewards"]);
  });

  it("CONTROL: a deployment with no roles offers nothing to tick", () => {
    expect(roleNames([])).toEqual([]);
  });
});

describe("the membership delta", () => {
  it("submits what CHANGED, in both directions", () => {
    expect(membershipDelta(["ops", "stewards"], ["ops", "approvers"])).toEqual({
      add: ["approvers"],
      remove: ["stewards"],
    });
  });

  it("is empty when nothing moved, whatever the order", () => {
    // The submit control is disabled off this emptiness, so an order-sensitive
    // comparison would arm a write that changes nothing.
    expect(membershipDelta(["ops", "stewards"], ["stewards", "ops"])).toEqual({
      add: [],
      remove: [],
    });
  });
});
