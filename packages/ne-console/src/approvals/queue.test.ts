// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §8's queue rules, tested where they live — no DOM, no provider, no fixture
// file. Every case here is one a reader of the screen can check by eye.

import { describe, expect, it } from "vitest";
import type { PendingApprovalView } from "../api/types";
import {
  applyFilters,
  clock,
  countText,
  dueCell,
  isExpired,
  isOverdue,
  NO_FILTERS,
  requesters,
  statusWord,
  timeOrDate,
} from "./queue";

const NOW = Date.parse("2026-07-27T12:00:00Z");
const at = (deltaMs: number) => new Date(NOW + deltaMs).toISOString();
const MIN = 60_000;
const HOUR = 60 * MIN;

function approval(over: Partial<PendingApprovalView> = {}): PendingApprovalView {
  return {
    request_id: "req-1",
    action: "crm.create_note",
    tier: 2,
    principal_id: "agent://sales",
    requested_at: at(-10 * MIN),
    sla_deadline: at(30 * MIN),
    expires_at: at(24 * HOUR),
    escalated: false,
    ...over,
  };
}

describe("§8 — overdue and expiry are comparisons against server instants", () => {
  it("is overdue from the instant the deadline passes, and not before", () => {
    expect(isOverdue(approval({ sla_deadline: at(1) }), NOW)).toBe(false);
    expect(isOverdue(approval({ sla_deadline: at(0) }), NOW)).toBe(true);
    expect(isOverdue(approval({ sla_deadline: at(-1) }), NOW)).toBe(true);
  });

  it("expiry is read from expires_at, never from the deadline", () => {
    expect(isExpired(approval({ expires_at: at(1), sla_deadline: at(-5 * HOUR) }), NOW)).toBe(
      false,
    );
    expect(isExpired(approval({ expires_at: at(-1) }), NOW)).toBe(true);
  });
});

describe("§8 — the Status pill vocabulary", () => {
  it("reads PENDING while the deadline holds", () => {
    expect(statusWord(approval(), NOW)).toBe("PENDING");
  });

  it("reads ESCALATED on the server's own flag while still in time", () => {
    expect(statusWord(approval({ escalated: true }), NOW)).toBe("ESCALATED");
  });

  it("reads OVERDUE past the deadline — ahead of ESCALATED, so the overdue filter never shows a row whose pill denies it", () => {
    expect(statusWord(approval({ sla_deadline: at(-1), escalated: true }), NOW)).toBe("OVERDUE");
  });

  it("reads EXPIRED past expires_at, whatever the deadline says", () => {
    expect(statusWord(approval({ expires_at: at(-1), sla_deadline: at(-1) }), NOW)).toBe("EXPIRED");
  });

  it("reads AWAITING SECOND when the gate answered a first Tier-3 approval that way", () => {
    expect(statusWord(approval({ tier: 3, sla_deadline: at(-1) }), NOW, true)).toBe(
      "AWAITING SECOND",
    );
  });
});

describe("§8 — the Due cell", () => {
  it("counts down in mm:ss inside the hour", () => {
    expect(dueCell(at(5 * MIN + 4000), NOW)).toBe("05:04");
  });

  it("marks a past-due countdown with a sign, so the state is not carried by the tint alone", () => {
    expect(dueCell(at(-5 * MIN), NOW)).toBe("−05:00");
  });

  it("falls back to time/date beyond the hour", () => {
    // Same local day as NOW ⇒ the time alone; a different day carries the date.
    const twoHours = dueCell(at(2 * HOUR), NOW);
    expect(twoHours).toMatch(/^\d{2}:\d{2}$/);
    expect(dueCell(at(48 * HOUR), NOW)).toMatch(/^\d{2}-\d{2} \d{2}:\d{2}$/);
  });

  it("clock() reports magnitude, and grows an hours field past an hour", () => {
    expect(clock(-125_000)).toBe("02:05");
    expect(clock(3 * HOUR + 4 * MIN + 5000)).toBe("3:04:05");
  });

  it("timeOrDate leaves an unparseable instant exactly as the server sent it", () => {
    expect(timeOrDate("not-a-time", NOW)).toBe("not-a-time");
  });
});

describe("§8 — the toolbar filters", () => {
  const rows = [
    approval({
      request_id: "a",
      tier: 2,
      principal_id: "agent://sales",
      sla_deadline: at(30 * MIN),
    }),
    approval({ request_id: "b", tier: 3, principal_id: "agent://finance", sla_deadline: at(-MIN) }),
    approval({ request_id: "c", tier: 2, principal_id: "agent://finance", sla_deadline: at(HOUR) }),
  ];

  it("narrows by tier, by IDENTITY of the surviving rows", () => {
    expect(applyFilters(rows, { ...NO_FILTERS, tier: "3" }, NOW).map((r) => r.request_id)).toEqual([
      "b",
    ]);
  });

  it("narrows by requester", () => {
    expect(
      applyFilters(rows, { ...NO_FILTERS, requester: "agent://finance" }, NOW).map(
        (r) => r.request_id,
      ),
    ).toEqual(["b", "c"]);
  });

  it("narrows to the overdue rows", () => {
    expect(
      applyFilters(rows, { ...NO_FILTERS, overdueOnly: true }, NOW).map((r) => r.request_id),
    ).toEqual(["b"]);
  });

  it("composes: tier AND requester AND overdue", () => {
    expect(
      applyFilters(rows, { tier: "2", requester: "agent://finance", overdueOnly: true }, NOW),
    ).toEqual([]);
    expect(
      applyFilters(rows, { tier: "2", requester: "agent://finance", overdueOnly: false }, NOW).map(
        (r) => r.request_id,
      ),
    ).toEqual(["c"]);
  });

  it("CONTROL: the default filters drop nothing", () => {
    expect(applyFilters(rows, NO_FILTERS, NOW).map((r) => r.request_id)).toEqual(["a", "b", "c"]);
  });

  it("offers each requester once, in a stable order", () => {
    expect(requesters(rows)).toEqual(["agent://finance", "agent://sales"]);
  });
});

describe("§8 — the live count", () => {
  it("names the overdue tail only when there is one", () => {
    expect(countText(4, 0)).toBe("4 pending");
    expect(countText(4, 1)).toBe("4 pending · 1 overdue");
  });

  it("groups thousands from 10,000 up (§2.7)", () => {
    expect(countText(12000, 0)).toBe("12,000 pending");
  });
});
