// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.2 — the export preamble. A CSV of a FILTERED list handed on with no record
// of the filter is a claim about a whole table made from a slice of one. The
// preamble is the honest part, so it is the part that gets a test.

import { describe, expect, it } from "vitest";
import { buildCsv, csvField, csvSafeCell } from "./csv";

interface Row {
  seq: number;
  actor: string;
  note: string | null;
}

const COLUMNS = [
  { header: "Seq", value: (r: Row) => String(r.seq) },
  { header: "Actor", value: (r: Row) => r.actor },
  // An absent value exports EMPTY, never the em dash: a dash is a rendering
  // convention, and a spreadsheet would treat it as data.
  { header: "Note", value: (r: Row) => r.note ?? "" },
];

const NOW = new Date("2026-07-01T09:30:00");

describe("buildCsv", () => {
  it("names the applied filters and the export time on line 1", () => {
    const csv = buildCsv({
      title: "Audit log",
      filters: ["Outcome: denied", "Search: wire"],
      columns: COLUMNS,
      rows: [{ seq: 4812, actor: "alice", note: null }],
      now: NOW,
    });
    const [preamble, header, first] = csv.split("\r\n");
    expect(preamble).toContain("Audit log");
    expect(preamble).toContain("Outcome: denied; Search: wire");
    expect(preamble).toContain("2026-07-01 09:30:00");
    expect(header).toBe("Seq,Actor,Note");
    expect(first).toBe("4812,alice,");
  });

  it("says so plainly when nothing was filtered", () => {
    const csv = buildCsv({ title: "Audit log", filters: [], columns: COLUMNS, rows: [], now: NOW });
    expect(csv.split("\r\n")[0]).toContain("no filters applied");
  });
});

describe("csvField", () => {
  it("quotes only when it must, and doubles an embedded quote", () => {
    expect(csvField("plain")).toBe("plain");
    expect(csvField("a,b")).toBe('"a,b"');
    expect(csvField('say "hi"')).toBe('"say ""hi"""');
    expect(csvField("line\nbreak")).toBe('"line\nbreak"');
  });
});

// CWE-1236 — formula injection. Audit CSV is server-authored, but Approvals
// and the other client-built governance exports share this primitive. An
// attacker-controlled cell must remain inert when the artifact is opened in a
// spreadsheet; these synthetic payloads reach no network or live system.
describe("csvSafeCell (CWE-1236)", () => {
  it("neutralises every formula-leading character the server recognises", () => {
    expect(csvSafeCell("=1+1")).toBe("'=1+1");
    expect(csvSafeCell("+1")).toBe("'+1");
    expect(csvSafeCell("-1")).toBe("'-1");
    expect(csvSafeCell("@SUM(A1)")).toBe("'@SUM(A1)");
    expect(csvSafeCell("\t=1+1")).toBe("'\t=1+1");
    expect(csvSafeCell("\r=1+1")).toBe("'\r=1+1");
  });

  it("leaves ordinary and non-leading characters byte-identical", () => {
    expect(csvSafeCell("alice")).toBe("alice");
    expect(csvSafeCell("")).toBe("");
    expect(csvSafeCell("a=b")).toBe("a=b");
    expect(csvSafeCell("2026-07-01 09:30:00")).toBe("2026-07-01 09:30:00");
  });
});

describe("buildCsv neutralises formulas in every client-built export", () => {
  interface Cell {
    text: string;
  }
  const ONE = [{ header: "Reason", value: (row: Cell) => row.text }];

  it("escapes an attacker-supplied approval reason inside RFC-4180 quoting", () => {
    const payload = '=HYPERLINK("http://synthetic.invalid/x","click")';
    const csv = buildCsv({
      title: "Approvals",
      filters: [],
      columns: ONE,
      rows: [{ text: payload }],
      now: NOW,
    });
    expect(csv.split("\r\n")[2]).toBe(`"'=HYPERLINK(""http://synthetic.invalid/x"",""click"")"`);
  });

  it("neutralises before quoting a formula-leading value with a comma", () => {
    const csv = buildCsv({
      title: "Audit log",
      filters: [],
      columns: ONE,
      rows: [{ text: "=1,2" }],
      now: NOW,
    });
    expect(csv.split("\r\n")[2]).toBe(`"'=1,2"`);
  });

  it("neutralises formula-leading column headers", () => {
    const csv = buildCsv({
      title: "Audit log",
      filters: [],
      columns: [{ header: "-Delta", value: () => "x" }],
      rows: [{ text: "x" }],
      now: NOW,
    });
    expect(csv.split("\r\n")[1]).toBe("'-Delta");
  });
});
