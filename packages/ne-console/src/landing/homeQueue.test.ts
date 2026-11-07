// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §4 — the merge, the order, the cap and the status word, without a DOM.

import { describe, expect, it } from "vitest";
import type {
  PendingApprovalView,
  ServedStewardProposalView,
  StewardProposalView,
} from "../api/types";
import {
  HOME_QUEUE_LIMIT,
  mergeQueue,
  overflowCounts,
  PROPOSAL_WITHHELD_LABEL,
  queueStatusWord,
  statusRank,
} from "./homeQueue";

const NOW = Date.parse("2026-07-07T12:00:00Z");
const at = (deltaMinutes: number) => new Date(NOW + deltaMinutes * 60_000).toISOString();

function approval(
  over: Partial<PendingApprovalView> & { request_id: string },
): PendingApprovalView {
  return {
    action: "crm.create_note",
    tier: 2,
    principal_id: "agent://sales-drafts",
    requested_at: at(-10),
    sla_deadline: at(30),
    expires_at: at(600),
    escalated: false,
    ...over,
  };
}

function proposal(
  over: Partial<StewardProposalView> & { proposal_id: string },
): StewardProposalView {
  const id = over.proposal_id;
  return {
    // The server states its disclosure decision explicitly
    // (015A0-ENFORCEMENT); Home fails closed on a row that does not.
    disclosure_status: "available",
    lane: "content",
    kind: "fix",
    status: "proposed",
    cluster_id: `cluster-${id}`,
    confidence: 0.5,
    priority: "medium",
    evidence_event_ids: [],
    evidence_signal_ids: [],
    config_hash: "cfg-1",
    created_at: at(-600),
    artifact: {
      proposal_id: id,
      lane: "content",
      kind: "git_unified_diff",
      path: `proposals/content/${id}.md`,
      title: `title ${id}`,
      patch: "",
      pr_body: "",
    },
    ...over,
  };
}

describe("mergeQueue — the urgency order §4 states", () => {
  it("puts approvals first, by deadline ascending", () => {
    const rows = mergeQueue(
      [
        approval({ request_id: "later", sla_deadline: at(120) }),
        approval({ request_id: "breached", sla_deadline: at(-5) }),
        approval({ request_id: "soon", sla_deadline: at(20) }),
      ],
      [],
    );
    expect(rows.map((r) => r.key)).toEqual(["breached", "soon", "later"]);
  });

  it("puts proposals after every approval, by age ascending", () => {
    const rows = mergeQueue(
      [approval({ request_id: "req", sla_deadline: at(1) })],
      [
        proposal({ proposal_id: "new", created_at: at(-60) }),
        proposal({ proposal_id: "old", created_at: at(-6000) }),
      ],
    );
    // The approval sorts ahead of BOTH proposals even though its deadline is a
    // minute away and the oldest proposal has been waiting four days: a decision
    // with a clock on it outranks a review without one.
    expect(rows.map((r) => r.key)).toEqual(["req", "old", "new"]);
  });

  it("keeps the served order for rows sharing an instant", () => {
    const rows = mergeQueue(
      [
        approval({ request_id: "first", sla_deadline: at(10) }),
        approval({ request_id: "second", sla_deadline: at(10) }),
      ],
      [],
    );
    expect(rows.map((r) => r.key)).toEqual(["first", "second"]);
  });

  it("carries each row to the screen that owns it, with the row's own id", () => {
    const rows = mergeQueue(
      [approval({ request_id: "req-t3-wire" })],
      [proposal({ proposal_id: "prop-policy-2" })],
    );
    expect(rows[0]?.to).toBe("/approvals?request=req-t3-wire");
    expect(rows[1]?.to).toBe("/improvement?proposal=prop-policy-2");
  });

  it("reads the fields §4 names, and invents no deadline for a review", () => {
    const rows = mergeQueue(
      [approval({ request_id: "r", action: "payments.wire_transfer", principal_id: "agent://f" })],
      [proposal({ proposal_id: "p", lane: "policy" })],
    );
    expect(rows[0]).toMatchObject({
      typeLabel: "Approval",
      item: "payments.wire_transfer",
      from: "agent://f",
      due: approval({ request_id: "r" }).sla_deadline,
    });
    expect(rows[1]).toMatchObject({ typeLabel: "Review", item: "title p", from: "policy" });
    expect(rows[1]?.due).toBeNull();
  });

  it("a queue that contributed nothing contributes NOTHING — not a placeholder", () => {
    expect(mergeQueue([], [])).toEqual([]);
    expect(mergeQueue([approval({ request_id: "r" })], [])).toHaveLength(1);
  });
});

describe("overflowCounts — the footer links", () => {
  const rows = mergeQueue(
    Array.from({ length: 10 }, (_, i) => approval({ request_id: `req-${i}`, sla_deadline: at(i) })),
    Array.from({ length: 3 }, (_, i) => proposal({ proposal_id: `prop-${i}`, created_at: at(-i) })),
  );

  it("counts the truncated tail per owning screen", () => {
    expect(HOME_QUEUE_LIMIT).toBe(8);
    expect(overflowCounts(rows)).toEqual({ approvals: 2, reviews: 3 });
  });

  it("is zero on both when nothing is truncated", () => {
    expect(overflowCounts(rows.slice(0, 4))).toEqual({ approvals: 0, reviews: 0 });
  });
});

describe("queueStatusWord — one word, from the server's own timestamps", () => {
  const row = (over: Partial<PendingApprovalView> & { request_id: string }) =>
    mergeQueue([approval(over)], [])[0]!;

  it("is OVERDUE past the deadline", () => {
    expect(queueStatusWord(row({ request_id: "a", sla_deadline: at(-1) }), NOW)).toBe("OVERDUE");
  });

  it("is OVERDUE even when the gate also escalated it — the breach is what is true now", () => {
    expect(
      queueStatusWord(row({ request_id: "a", sla_deadline: at(-1), escalated: true }), NOW),
    ).toBe("OVERDUE");
  });

  it("is ESCALATED when the gate escalated a row that is still inside its deadline", () => {
    expect(
      queueStatusWord(row({ request_id: "a", sla_deadline: at(30), escalated: true }), NOW),
    ).toBe("ESCALATED");
  });

  it("is PENDING otherwise, and for every review (a proposal has no clock)", () => {
    expect(queueStatusWord(row({ request_id: "a", sla_deadline: at(30) }), NOW)).toBe("PENDING");
    const review = mergeQueue([], [proposal({ proposal_id: "p" })])[0]!;
    expect(queueStatusWord(review, NOW)).toBe("PENDING");
  });

  it("ranks the most urgent word first when the column is sorted ascending", () => {
    const overdue = row({ request_id: "a", sla_deadline: at(-1) });
    const escalated = row({ request_id: "b", sla_deadline: at(30), escalated: true });
    const pending = row({ request_id: "c", sla_deadline: at(30) });
    expect(statusRank(overdue, NOW)).toBeLessThan(statusRank(escalated, NOW));
    expect(statusRank(escalated, NOW)).toBeLessThan(statusRank(pending, NOW));
  });
});

// ===========================================================================
// 015A0-ENFORCEMENT — an undisclosed proposal on Home
// ===========================================================================

describe("mergeQueue — a proposal the server did not disclose", () => {
  /** The withheld branch as the server sends it: no artifact, no lane. */
  const withheld: ServedStewardProposalView = {
    disclosure_status: "content_withheld",
    proposal_id: "p-withheld",
    status: "proposed",
    created_at: at(-600),
  };

  it("builds its row without touching an artifact that is not there", () => {
    // The pre-ENFORCEMENT reader threw here: it read `p.artifact.title` off a
    // row whose artifact the server had withheld.
    const rows = mergeQueue([], [withheld]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      key: "p-withheld",
      typeLabel: "Review",
      item: PROPOSAL_WITHHELD_LABEL,
      from: "",
      due: null,
      to: "/improvement?proposal=p-withheld",
    });
  });

  it("withholds a proposal that states NO disclosure status, complete though it looks", () => {
    const stated = proposal({ proposal_id: "p" });
    const unstated = { ...stated, disclosure_status: undefined };
    expect(mergeQueue([], [unstated])[0]?.item).toBe(PROPOSAL_WITHHELD_LABEL);
    // Control: the identical row WITH the status keeps its served title.
    expect(mergeQueue([], [stated])[0]?.item).toBe("title p");
  });

  it("keeps the withheld row in the queue, and in the served time order", () => {
    const rows = mergeQueue(
      [],
      [proposal({ proposal_id: "newer", created_at: at(-10) }), withheld],
    );
    expect(rows.map((row) => row.key)).toEqual(["p-withheld", "newer"]);
  });

  it("says the same word Improvement says, so one row is not two things on two screens", () => {
    expect(PROPOSAL_WITHHELD_LABEL).toBe("Content withheld");
  });
});
