// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §4 — the recent-activity fold: newest first, refusals out, ten at most.

import { describe, expect, it } from "vitest";
import type { AuditRecordView } from "../api/types";
import { recentActivity, RECENT_ACTIVITY_LIMIT } from "./homeActivity";

function record(seq: number, action: string): AuditRecordView {
  return {
    id: seq,
    seq,
    event_id: `evt-${seq}`,
    event_type: action,
    config_hash: "a".repeat(64),
    actor: "agent://sales-drafts",
    occurred_at: new Date(Date.parse("2026-07-07T12:00:00Z") + seq * 1000).toISOString(),
    kpi_tag: null,
    trace_id: null,
    action,
    resource: null,
    outcome: "success",
    payload: {},
    prev_hash: "0".repeat(64),
    record_hash: "b".repeat(64),
  };
}

describe("recentActivity", () => {
  it("orders newest first, whatever order the page arrived in", () => {
    const got = recentActivity([record(1, "pdp.decide"), record(3, "pdp.decide"), record(2, "x")]);
    expect(got.map((e) => e.seq)).toEqual([3, 2, 1]);
  });

  it("drops access refusals — a declined read decided nothing about an action", () => {
    const got = recentActivity([
      record(1, "approval.decided"),
      record(2, "api.audit.unauthorized"),
      record(3, "api.approval.unauthorized"),
    ]);
    expect(got.map((e) => e.action)).toEqual(["approval.decided"]);
  });

  it("keeps ten and no eleventh", () => {
    const got = recentActivity(Array.from({ length: 25 }, (_, i) => record(i + 1, "tool.execute")));
    expect(got).toHaveLength(RECENT_ACTIVITY_LIMIT);
    expect(got[0]?.seq).toBe(25);
    expect(got.at(-1)?.seq).toBe(16);
  });

  it("does not mutate the poll's own array", () => {
    const served = [record(1, "a"), record(2, "b")];
    recentActivity(served);
    expect(served.map((e) => e.seq)).toEqual([1, 2]);
  });

  it("returns nothing for a window that held only refusals — never a filler row", () => {
    expect(recentActivity([record(1, "api.chat.unauthorized")])).toEqual([]);
  });
});
