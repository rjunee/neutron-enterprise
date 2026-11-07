// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §11's rules, without a DOM.
//
// Each assertion is paired with a control that would fail if the rule were
// stated the other way round, because a rule that only ever sees agreeable input
// is a rule nothing checks.

import { describe, expect, it } from "vitest";
import type { ServedStewardProposalView, StewardProposalView } from "../api/types";
import {
  applyFilters,
  enumLabel,
  findProposal,
  formatHours,
  isSkillProposal,
  matchesProposalParam,
  priorityRank,
  proposalCount,
  queueRowView,
  queueRowViews,
  shiftWeek,
  skillCount,
  skillStatusWord,
  statusWord,
  WITHHELD_LABEL,
} from "./queue";

function proposal(over: Partial<StewardProposalView> = {}): StewardProposalView {
  return {
    disclosure_status: "available",
    proposal_id: "p1",
    lane: "content",
    kind: "fix",
    status: "proposed",
    cluster_id: "c1",
    confidence: 0.5,
    priority: "high",
    evidence_event_ids: [],
    evidence_signal_ids: [],
    config_hash: "cfg",
    created_at: "2026-07-06T09:00:00Z",
    artifact: {
      proposal_id: "p1",
      lane: "content",
      kind: "git_unified_diff",
      path: "x",
      title: "t",
      patch: "",
      pr_body: "",
    },
    ...over,
  };
}

describe("enumLabel", () => {
  it("renders a wire enum as a sentence-case word", () => {
    expect(enumLabel("entity_resolution")).toBe("Entity resolution");
    expect(enumLabel("content")).toBe("Content");
    expect(enumLabel("golden_set_candidate")).toBe("Golden set candidate");
  });

  it("passes an unrecognised value through rather than bucketing it", () => {
    // The control: a value this console has never seen still renders ITSELF, so
    // a new server lane appears on screen instead of disappearing into "Other".
    expect(enumLabel("brand_new_lane")).toBe("Brand new lane");
  });
});

describe("statusWord", () => {
  it("maps the two non-terminal statuses §11 names", () => {
    expect(statusWord("proposed")).toBe("PENDING");
    expect(statusWord("approved")).toBe("APPROVED");
  });

  it("returns null for a status it was never given a pill for", () => {
    // The em dash, not a guessed pill: §2.3's vocabulary is closed.
    expect(statusWord("rejected")).toBeNull();
    expect(statusWord("something-else")).toBeNull();
  });
});

describe("priorityRank", () => {
  it("orders by severity, which alphabetical order does not", () => {
    expect(priorityRank("high")).toBeLessThan(priorityRank("medium"));
    expect(priorityRank("medium")).toBeLessThan(priorityRank("low"));
    // The control: sorted as plain strings, "high" would land between the two.
    expect("high" < "low").toBe(true);
    expect("low" < "medium").toBe(true);
  });

  it("sorts an unknown priority after all three", () => {
    expect(priorityRank("unheard-of")).toBeGreaterThan(priorityRank("low"));
  });
});

describe("applyFilters", () => {
  const rows = queueRowViews([
    proposal({ proposal_id: "a", lane: "content", status: "proposed" }),
    proposal({ proposal_id: "b", lane: "policy", status: "approved" }),
    proposal({ proposal_id: "c", lane: "policy", status: "proposed" }),
  ]);

  it("narrows by lane, by identity", () => {
    expect(applyFilters(rows, { lane: "policy", status: "all" }).map((r) => r.proposalId)).toEqual([
      "b",
      "c",
    ]);
  });

  it("narrows by status, by identity", () => {
    expect(
      applyFilters(rows, { lane: "all", status: "approved" }).map((r) => r.proposalId),
    ).toEqual(["b"]);
  });

  it("intersects the two filters", () => {
    expect(
      applyFilters(rows, { lane: "policy", status: "proposed" }).map((r) => r.proposalId),
    ).toEqual(["c"]);
  });

  it("keeps every row when neither filter is set", () => {
    expect(applyFilters(rows, { lane: "all", status: "all" })).toHaveLength(3);
  });
});

describe("counts", () => {
  it("names the things it counts", () => {
    expect(proposalCount(4)).toBe("4 proposals");
    expect(skillCount(0)).toBe("0 skills");
  });
});

describe("skillStatusWord", () => {
  it("maps the three lifecycle states", () => {
    expect(skillStatusWord("active")).toBe("ACTIVE");
    expect(skillStatusWord("retired")).toBe("RETIRED");
    expect(skillStatusWord("quarantined")).toBe("QUARANTINED");
  });

  it("gives no pill to a state outside the closed vocabulary", () => {
    expect(skillStatusWord("forging")).toBeNull();
  });
});

describe("formatHours", () => {
  it("renders server seconds as hours", () => {
    expect(formatHours(5400)).toBe("1.5 h");
    expect(formatHours(86400)).toBe("24.0 h");
  });

  it("renders an absent measurement as the em dash, never as zero", () => {
    expect(formatHours(null)).toBe("—");
    expect(formatHours(undefined)).toBe("—");
    // The control: a real zero is a measurement and renders as one.
    expect(formatHours(0)).toBe("0.0 h");
  });
});

describe("shiftWeek", () => {
  it("moves whole weeks in UTC", () => {
    expect(shiftWeek("2026-06-29", -1)).toBe("2026-06-22");
    expect(shiftWeek("2026-06-29", 1)).toBe("2026-07-06");
  });

  it("crosses a year boundary", () => {
    expect(shiftWeek("2026-01-04", -1)).toBe("2025-12-28");
  });

  it("returns null for an unparseable week", () => {
    expect(shiftWeek("not-a-date", -1)).toBeNull();
  });
});

describe("matchesProposalParam", () => {
  it("matches a byte-exact id", () => {
    expect(matchesProposalParam("prop-er-4", "prop-er-4")).toBe(true);
  });

  it("matches a content-addressed id by its digest prefix", () => {
    expect(matchesProposalParam("sha256:0123456789abcdef99", "0123456789abcdef")).toBe(true);
  });

  it("does not match a digest that merely appears inside the id", () => {
    expect(matchesProposalParam("sha256:ff0123456789abcdef", "0123456789abcdef")).toBe(false);
  });

  it("finds the named row and nothing else", () => {
    const rows = queueRowViews([proposal({ proposal_id: "a" }), proposal({ proposal_id: "b" })]);
    expect(findProposal(rows, "b")?.proposalId).toBe("b");
    expect(findProposal(rows, "z")).toBeNull();
    expect(findProposal(rows, null)).toBeNull();
  });
});

describe("isSkillProposal", () => {
  const skill = {
    name: "n",
    version: 1,
    description: "d",
    triggers: [],
    suggested_acl_labels: [],
    provenance_line: "p",
    body: "b",
  };

  it("is true only for a skill kind that actually carries a candidate", () => {
    expect(isSkillProposal(proposal({ kind: "skill_candidate", skill }))).toBe(true);
    expect(isSkillProposal(proposal({ kind: "skill_update_candidate", skill }))).toBe(true);
    // A skill kind with no candidate on the wire is NOT rendered as one: the
    // pane would have nothing to show and would invent the fields.
    expect(isSkillProposal(proposal({ kind: "skill_candidate", skill: null }))).toBe(false);
    expect(isSkillProposal(proposal({ kind: "fix", skill }))).toBe(false);
  });
});

// ===========================================================================
// 015A0-ENFORCEMENT — the queue row, resolved through the server's status
// ===========================================================================

describe("queueRowView", () => {
  /** The withheld branch as the server sends it: id, safe status, time, status. */
  function withheld(
    status: "content_withheld" | "retryable_unknown" = "content_withheld",
  ): ServedStewardProposalView {
    return {
      disclosure_status: status,
      proposal_id: "p-withheld",
      status: "proposed",
      created_at: "2026-07-06T09:00:00Z",
    };
  }

  it("discloses a row the server stated as available", () => {
    const view = queueRowView(proposal({ proposal_id: "a" }));
    expect(view.proposal?.proposal_id).toBe("a");
    expect(view.permanentlyWithheld).toBe(false);
  });

  it("keeps only id, safe status and time for a content_withheld row", () => {
    expect(queueRowView(withheld())).toEqual({
      proposalId: "p-withheld",
      status: "proposed",
      createdAt: "2026-07-06T09:00:00Z",
      proposal: null,
      permanentlyWithheld: true,
    });
  });

  it("withholds a retryable_unknown row too, and does not call it a refusal", () => {
    const view = queueRowView(withheld("retryable_unknown"));
    expect(view.proposal).toBeNull();
    expect(view.permanentlyWithheld).toBe(false);
  });

  it("withholds a row that states NO disclosure status, complete though it looks", () => {
    const stated = proposal({ proposal_id: "a" });
    const unstated = { ...stated, disclosure_status: undefined };
    expect(queueRowView(unstated).proposal).toBeNull();
    // Control: the identical row WITH the status is disclosed, so the rule is
    // about the status and not about the row's contents.
    expect(queueRowView(stated).proposal).not.toBeNull();
  });

  it("withholds a row whose stated status this console cannot read", () => {
    const row = { ...proposal(), disclosure_status: "partially_available" } as unknown;
    expect(queueRowView(row as ServedStewardProposalView).proposal).toBeNull();
  });

  it("matches an undisclosed row against no lane filter, rather than any lane", () => {
    const rows = queueRowViews([proposal({ proposal_id: "a", lane: "policy" }), withheld()]);
    expect(applyFilters(rows, { lane: "policy", status: "all" }).map((r) => r.proposalId)).toEqual([
      "a",
    ]);
    expect(applyFilters(rows, { lane: "content", status: "all" })).toHaveLength(0);
    // The row is still IN the queue: an undisclosed proposal is not deleted.
    expect(applyFilters(rows, { lane: "all", status: "all" })).toHaveLength(2);
  });

  it("reports no skill candidate for an undisclosed row", () => {
    expect(isSkillProposal(queueRowView(withheld()).proposal)).toBe(false);
  });

  it("uses one withheld literal for the whole surface", () => {
    expect(WITHHELD_LABEL).toBe("Content withheld");
  });
});
