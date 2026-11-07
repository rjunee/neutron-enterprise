// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §9's rules, without a DOM.

import { describe, expect, it } from "vitest";
import type { ConnectorStatusView } from "../api/types";
import {
  ALL_KINDS,
  applyFilters,
  checkedRecencyOf,
  countText,
  filterLabels,
  kindsOf,
  lastCheckedAt,
  newestRecordAt,
  quarantineEmptyLine,
  quarantineHeading,
  recordRecencyOf,
} from "./sources";

function connector(over: Partial<ConnectorStatusView> = {}): ConnectorStatusView {
  return {
    id: "m365",
    kind: "m365",
    posture: "live",
    // The two freshness facts DIVERGE in the default fixture, deliberately: any
    // rule that quietly reads the wrong field produces the wrong instant.
    cursor_age_s: 340,
    last_checked_age_s: 120,
    acl_mirror: "ok",
    last_sync_seq: 41,
    quarantined_count: 0,
    sync_allowed: false,
    ...over,
  };
}

describe("§9 sources rules", () => {
  it("offers the DISTINCT served kinds, A–Z, and nothing else", () => {
    const rows = [
      connector({ id: "jira", kind: "jira" }),
      connector({ id: "debezium", kind: "cdc" }),
      connector({ id: "jira-2", kind: "jira" }),
    ];
    // No hardcoded lane list: "slack" is not offered because no row has it.
    expect(kindsOf(rows)).toEqual(["cdc", "jira"]);
  });

  it("narrows to the rows of the chosen kind, by identity", () => {
    const rows = [
      connector({ id: "m365", kind: "m365" }),
      connector({ id: "debezium", kind: "cdc" }),
      connector({ id: "jira", kind: "jira" }),
    ];
    expect(applyFilters(rows, { kind: "jira" }).map((r) => r.id)).toEqual(["jira"]);
    expect(applyFilters(rows, { kind: ALL_KINDS }).map((r) => r.id)).toEqual([
      "m365",
      "debezium",
      "jira",
    ]);
  });

  it("reads each age as the instant it describes, measured from the read — from ITS OWN field", () => {
    const at = Date.parse("2026-08-01T12:00:00.000Z");
    // §12 item 9's live-box shape: the cycle ran 2 min ago and found nothing;
    // the newest record is ~a day old. TWO instants, one per fact — a rule
    // reading cursor_age_s for both would put 11:59:58 in the wrong slot.
    const quiet = connector({ last_checked_age_s: 120, cursor_age_s: 86_400 });
    expect(lastCheckedAt(quiet, at)).toBe("2026-08-01T11:58:00.000Z");
    expect(newestRecordAt(quiet, at)).toBe("2026-07-31T12:00:00.000Z");
    // Never checked is an absence, not an old timestamp — and it is NOT implied
    // by a missing record cursor (the run fact stands on its own).
    expect(lastCheckedAt(connector({ last_checked_age_s: null }), at)).toBeNull();
    expect(newestRecordAt(connector({ cursor_age_s: null }), at)).toBeNull();
    expect(lastCheckedAt(connector({ cursor_age_s: null }), at)).not.toBeNull();
  });

  it("sorts each time column as a RECENCY on its own field, so descending is newest first", () => {
    const fresh = connector({ id: "fresh", last_checked_age_s: 22, cursor_age_s: 9200 });
    const stale = connector({ id: "stale", last_checked_age_s: 9200, cursor_age_s: 22 });
    // The two keys ORDER THE SAME PAIR OPPOSITE WAYS — a sort key reading the
    // other column's field inverts one of these.
    expect(checkedRecencyOf(fresh)).toBeGreaterThan(checkedRecencyOf(stale) as number);
    expect(recordRecencyOf(stale)).toBeGreaterThan(recordRecencyOf(fresh) as number);
    expect(checkedRecencyOf(connector({ last_checked_age_s: null }))).toBeNull();
    expect(recordRecencyOf(connector({ cursor_age_s: null }))).toBeNull();
  });

  it("counts the window in the words §2.2 gives", () => {
    expect(countText(3)).toBe("3 sources");
    expect(countText(0)).toBe("0 sources");
  });

  it("names the applied filters for the export preamble", () => {
    expect(filterLabels({ kind: ALL_KINDS }, "")).toEqual([]);
    expect(filterLabels({ kind: "jira" }, "  deb ")).toEqual(["Kind: jira", "Search: deb"]);
  });

  it("says the quarantine total is UNAVAILABLE when the store could not be read", () => {
    expect(quarantineHeading(3)).toBe("Quarantine — 3 items");
    expect(quarantineHeading(0)).toBe("Quarantine — 0 items");
    // Never "Quarantine — 0 items" for an unreadable store: that is a
    // measurement nobody took.
    expect(quarantineHeading(null)).toBe("Quarantine — total unavailable");
  });

  it("only claims the quarantine is empty when it is", () => {
    expect(quarantineEmptyLine(0)).toBe("Nothing in quarantine.");
    expect(quarantineEmptyLine(null)).toBe("Nothing in quarantine.");
    // 3 items parked with no reason ledger: saying "nothing in quarantine" here
    // would contradict the count printed directly above it.
    expect(quarantineEmptyLine(3)).toBe("No reasons recorded.");
  });
});
