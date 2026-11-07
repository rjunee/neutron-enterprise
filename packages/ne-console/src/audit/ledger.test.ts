// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §13's rules, without a DOM: what each class chip keeps, what the count and the
// pagination line say, what the export carries and what it is called.

import { describe, expect, it } from "vitest";
import type { AuditAnchorView, AuditRecordView } from "../api/types";
import { classifyEvent } from "./eventClass";
import {
  anchorNotice,
  anchorPageNote,
  applyClassFilters,
  breakBandText,
  countRefusals,
  countToolCalls,
  csvColumns,
  DEFAULT_CLASSES,
  exportFilename,
  filterLabels,
  NO_FILTERS,
  pageNote,
  parseAnchorParam,
  requestFilters,
  resolveLocalInstant,
  showingText,
  truncateMiddle,
  WINDOW_SIZES,
} from "./ledger";

function record(over: Partial<AuditRecordView>): AuditRecordView {
  return {
    id: 1,
    seq: 1,
    event_id: "evt",
    event_type: "runtime",
    config_hash: "c".repeat(64),
    actor: "user:alice@example.com",
    occurred_at: "2026-07-20T09:00:00Z",
    kpi_tag: null,
    trace_id: null,
    action: null,
    resource: null,
    outcome: null,
    payload: {},
    prev_hash: "p".repeat(64),
    record_hash: "r".repeat(64),
    ...over,
  };
}

const DECISION = record({ seq: 4021, event_id: "evt-decision", action: "pdp.decide" });
const TOOL = record({ seq: 4020, event_id: "evt-tool", action: "tool.execute" });
const REFUSAL = record({
  seq: 4019,
  event_id: "evt-refusal",
  event_type: "api.audit",
  action: "api.audit.unauthorized",
});
const OTHER = record({ seq: 4018, event_id: "evt-other", event_type: "connector.sync" });
const PAGE = [DECISION, TOOL, REFUSAL, OTHER];

describe("the class chips", () => {
  it("classes the audited tool actions as tool calls", () => {
    for (const action of [
      "tool.execute",
      "tool.invoke",
      "tool.approval",
      "tool.unknown",
      "tool.toolset_denied",
    ]) {
      expect(classifyEvent({ event_type: "runtime", action })).toBe("tool-call");
    }
  });

  it("classes the gate's REAL terminal verdicts as decisions — granted and expired were escaping the Decisions chip", () => {
    // The emitter vocabulary (`ne-runtime/approvals.py`): an approve writes
    // `approval.granted`; expiry writes `approval.expired`. Neither was in the
    // DECISION alternation, so a grant classified "other" — shown always,
    // governed by no chip — while a rejection was governed. Asymmetric filter.
    expect(classifyEvent({ event_type: "audit", action: "approval.granted" })).toBe("decision");
    expect(classifyEvent({ event_type: "audit", action: "approval.expired" })).toBe("decision");
    // CONTROL: the gate's payload READ is a read, not a verdict.
    expect(classifyEvent({ event_type: "audit", action: "approval.payload_read" })).toBe("other");
  });

  it("CONTROL: a verdict is still a decision and a refused read is still a refusal", () => {
    expect(classifyEvent({ event_type: "govern", action: "pdp.decide" })).toBe("decision");
    expect(classifyEvent({ event_type: "gate", action: "approval.approved" })).toBe("decision");
    expect(classifyEvent({ event_type: "api.audit", action: "api.audit.unauthorized" })).toBe(
      "access-refusal",
    );
    // A payload key that merely contains the word is NOT a tool call.
    expect(classifyEvent({ event_type: "runtime", action: "toolset.configured" })).toBe("other");
  });

  it("hides access refusals by default and keeps everything else, BY IDENTITY", () => {
    expect(applyClassFilters(PAGE, DEFAULT_CLASSES).map((r) => r.event_id)).toEqual([
      "evt-decision",
      "evt-tool",
      "evt-other",
    ]);
  });

  it("each chip governs its own class, and the unclassed record is never hidden", () => {
    const only = (over: Partial<typeof DEFAULT_CLASSES>) =>
      applyClassFilters(PAGE, { ...DEFAULT_CLASSES, ...over }).map((r) => r.event_id);
    expect(only({ decisions: false })).toEqual(["evt-tool", "evt-other"]);
    expect(only({ toolCalls: false })).toEqual(["evt-decision", "evt-other"]);
    expect(only({ refusals: true })).toEqual([
      "evt-decision",
      "evt-tool",
      "evt-refusal",
      "evt-other",
    ]);
    // Every chip off still leaves the record no chip accounts for.
    expect(only({ decisions: false, toolCalls: false })).toEqual(["evt-other"]);
  });

  it("counts what each chip hides", () => {
    expect(countRefusals(PAGE)).toBe(1);
    expect(countToolCalls(PAGE)).toBe(1);
  });
});

describe("the copy", () => {
  it("says what is on screen and what was fetched", () => {
    expect(showingText(3, 5)).toBe("Showing 3 of 5");
  });

  it("states the window rather than drawing paging with no cursor behind it", () => {
    expect(pageNote(500)).toBe(
      "Showing the most recent 500 records. Narrow with filters or raise the window.",
    );
  });

  it("states a break as a fact about the chain", () => {
    expect(breakBandText(4020)).toBe(
      "Chain break at entry 4020. Records after this point failed verification.",
    );
  });

  it("shortens a long identifier from the middle, and leaves a short one alone", () => {
    expect(truncateMiddle("crm.create_note")).toBe("crm.create_note");
    const long = truncateMiddle("payments.wire_transfer.international.settlement", 20);
    expect(long).toHaveLength(20);
    expect(long.startsWith("payments.")).toBe(true);
    expect(long.endsWith("ment")).toBe(true);
  });
});

describe("the window request", () => {
  it("offers only sizes the ledger read accepts", () => {
    // The read caps `limit` at 500 and answers 422 above it. A larger entry
    // would be a control that can only replace the reader's window with an error.
    expect(WINDOW_SIZES.map((w) => Number(w.value))).toEqual([100, 500]);
    expect(Math.max(...WINDOW_SIZES.map((w) => Number(w.value)))).toBeLessThanOrEqual(500);
  });

  it("sends only the boxes that were filled in", () => {
    expect(requestFilters(NO_FILTERS)).toEqual({ limit: 100 });
    expect(
      requestFilters({
        ...NO_FILTERS,
        actor: "  user:dana  ",
        to: "2026-07-20T07:00:00.000Z",
      }),
    ).toEqual({ actor: "user:dana", to: "2026-07-20T07:00:00.000Z", limit: 100 });
  });

  it("resolves non-UTC local wall clocks to the intended instant", () => {
    expect(resolveLocalInstant("2026-07-20T09:15", "From", "America/New_York")).toEqual({
      kind: "instant",
      value: "2026-07-20T13:15:00.000Z",
    });
    // The same named zone is UTC-05 in winter, not a hard-coded -04 offset.
    expect(resolveLocalInstant("2026-01-20T09:15", "To", "America/New_York")).toEqual({
      kind: "instant",
      value: "2026-01-20T14:15:00.000Z",
    });
    // A non-whole-hour zone catches implementations that merely subtract a
    // rounded browser offset.
    expect(resolveLocalInstant("2026-07-20T09:15", "From", "Asia/Kathmandu")).toEqual({
      kind: "instant",
      value: "2026-07-20T03:30:00.000Z",
    });
  });

  it("rejects both sides of the DST ambiguity instead of silently choosing", () => {
    const gap = resolveLocalInstant("2026-03-08T02:30", "From", "America/New_York");
    expect(gap).toEqual({
      kind: "error",
      message:
        "From does not exist in America/New_York because the clock changes at that time. Choose another time.",
    });
    const fold = resolveLocalInstant("2026-11-01T01:30", "To", "America/New_York");
    expect(fold).toEqual({
      kind: "error",
      message:
        "To occurs twice in America/New_York because the clock changes at that time. Choose another time.",
    });
    // Bidirectional control: unambiguous instants immediately on either side
    // still resolve, so this is not a blanket rejection of transition days.
    expect(resolveLocalInstant("2026-03-08T01:30", "From", "America/New_York")).toEqual({
      kind: "instant",
      value: "2026-03-08T06:30:00.000Z",
    });
    expect(resolveLocalInstant("2026-03-08T03:30", "To", "America/New_York")).toEqual({
      kind: "instant",
      value: "2026-03-08T07:30:00.000Z",
    });
  });

  it("rejects malformed civil dates and accepts an empty open bound", () => {
    expect(resolveLocalInstant("2026-02-30T09:00", "From", "UTC")).toEqual({
      kind: "error",
      message: "From must be a valid local date and time.",
    });
    expect(resolveLocalInstant("", "To", "UTC")).toEqual({ kind: "empty" });
  });
});

describe("the export", () => {
  it("names every filter in force, including the class narrowing", () => {
    expect(
      filterLabels(
        { actor: "user:dana", action: "pdp.decide", from: "2026-07-01T00:00", to: "", limit: 500 },
        { decisions: true, toolCalls: false, refusals: true },
        " wire ",
      ),
    ).toEqual([
      "Actor: user:dana",
      "Action: pdp.decide",
      "From: 2026-07-01T00:00",
      "Window: 500 records",
      "Tool calls: hidden",
      "Access refusals: shown",
      "Filter: wire",
    ]);
  });

  it("says so when nothing was narrowed but the window itself", () => {
    expect(filterLabels(NO_FILTERS, DEFAULT_CLASSES, "")).toEqual([
      "Window: 100 records",
      "Access refusals: hidden",
    ]);
  });

  it("carries the whole record — the three hashes included", () => {
    const headers = csvColumns().map((c) => c.header);
    expect(headers).toEqual([
      "seq",
      "occurred_at",
      "actor",
      "action",
      "event_type",
      "resource",
      "outcome",
      "event_id",
      "kpi_tag",
      "trace_id",
      "config_hash",
      "prev_hash",
      "record_hash",
    ]);
    const cells = csvColumns().map((c) => c.value(OTHER));
    // An absent field exports EMPTY — never a dash, which a spreadsheet reads
    // as data, and never a zero.
    expect(cells[headers.indexOf("resource")]).toBe("");
    expect(cells[headers.indexOf("outcome")]).toBe("");
    expect(cells[headers.indexOf("record_hash")]).toBe("r".repeat(64));
  });

  it("names the file after the deployment and the span it holds", () => {
    expect(exportFilename("Example Manufacturing Co", NO_FILTERS, [DECISION, OTHER])).toBe(
      "audit-example-manufacturing-co-2026-07-20-2026-07-20.csv",
    );
  });

  it("uses the reader's own window when one was set", () => {
    expect(
      exportFilename(
        "acme.example.com",
        { ...NO_FILTERS, from: "2026-06-01T00:00", to: "2026-06-30T23:59" },
        [DECISION],
      ),
    ).toBe("audit-acme-example-com-2026-06-01-2026-06-30.csv");
  });
});

describe("the ?seq= anchor — parse", () => {
  it("parses a plain entry number", () => {
    expect(parseAnchorParam("13252")).toEqual({ kind: "seq", seq: 13252 });
  });

  it("no parameter is no anchor", () => {
    expect(parseAnchorParam(null)).toEqual({ kind: "none" });
  });

  it("anything that is not a positive integer is MALFORMED, never silently none", () => {
    // Each of these is a link that named no entry; degrading to "none" would
    // render the unfiltered list with no indication anything was attempted.
    for (const raw of ["bananas", "-3", "0", "1.5", "1e3", ""]) {
      expect(parseAnchorParam(raw)).toEqual({ kind: "malformed", raw });
    }
  });
});

describe("the ?seq= anchor — the loud-failure notice", () => {
  const verdict = (over: Partial<AuditAnchorView>): AuditAnchorView => ({
    seq: 13252,
    status: "found",
    head_seq: 15700,
    ...over,
  });
  const SEQ = { kind: "seq", seq: 13252 } as const;

  it("a malformed link says the link named no entry", () => {
    expect(anchorNotice({ kind: "malformed", raw: "bananas" }, null, false, false)).toEqual({
      text: "“bananas” isn't an audit entry number, so there is no entry to jump to.",
      action: null,
    });
  });

  it("a seq past the head names the entry AND where the log actually ends", () => {
    expect(
      anchorNotice(SEQ, verdict({ status: "not_found", head_seq: 12000 }), false, false),
    ).toEqual({
      text: "Entry 13,252 isn't in the audit log — the log ends at entry 12,000.",
      action: null,
    });
  });

  it("an in-range seq the log does not hold is stated as MISSING — a dense chain fact", () => {
    expect(
      anchorNotice(
        { kind: "seq", seq: 700 },
        verdict({ seq: 700, status: "not_found", head_seq: 15700 }),
        false,
        false,
      ),
    ).toEqual({
      text: "Entry 700 is missing from the audit log, which currently ends at entry 15,700.",
      action: null,
    });
  });

  it("filtered_out is distinguished from not_found and offers the recovery", () => {
    expect(anchorNotice(SEQ, verdict({ status: "filtered_out" }), false, false)).toEqual({
      text: "Entry 13,252 exists but the applied filters exclude it.",
      action: "clear-filters",
    });
  });

  it("found-but-view-hidden says the CONSOLE hid it and offers to show it", () => {
    expect(anchorNotice(SEQ, verdict({}), true, false)).toEqual({
      text: "Entry 13,252 is in the loaded window but hidden by the current view filters.",
      action: "show-entry",
    });
  });

  it("a found, visible anchor is the ONE case with no notice — the highlight is the answer", () => {
    expect(anchorNotice(SEQ, verdict({}), true, true)).toBeNull();
  });

  it("a verdict-less response still may not read as a successful jump", () => {
    // The read answered without an anchor verdict AND the row is absent: loud.
    expect(anchorNotice(SEQ, null, false, false)).toEqual({
      text: "Entry 13,252 couldn't be located in the loaded records.",
      action: null,
    });
    // …but if the row IS on screen, the highlight already answered.
    expect(anchorNotice(SEQ, null, true, true)).toBeNull();
    // Loaded but view-hidden is the CONSOLE's own fact — stated with the same
    // notice and recovery whether or not the server sent a verdict.
    expect(anchorNotice(SEQ, null, true, false)).toEqual({
      text: "Entry 13,252 is in the loaded window but hidden by the current view filters.",
      action: "show-entry",
    });
  });
});

describe("the ?seq= anchor — the pagination line", () => {
  it("an anchored window is 'around' its entry, never 'the most recent'", () => {
    expect(anchorPageNote(100, 13252, true)).toBe(
      "Showing 100 records around entry 13,252. Narrow with filters or raise the window.",
    );
    // A missed anchor's window is the rows NEAREST the position asked for.
    expect(anchorPageNote(100, 13252, false)).toBe(
      "Showing 100 records near entry 13,252. Narrow with filters or raise the window.",
    );
  });
});
