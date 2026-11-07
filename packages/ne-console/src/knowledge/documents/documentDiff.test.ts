// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { parseDocumentDiff } from "./documentDiff";

describe("server unified document diff presentation", () => {
  it("keeps file headers separate and assigns old/new gutters across hunks", () => {
    const parsed = parseDocumentDiff(
      [
        "--- v1",
        "+++ v2",
        "@@ -2,3 +2,4 @@ policy",
        " retained",
        "-old rule",
        "+new rule",
        "+new evidence",
        " tail",
        "@@ -20 +21 @@",
        "-before",
        "+after",
      ].join("\n"),
    );

    expect(parsed.state).toBe("changed");
    expect([parsed.fromFile, parsed.toFile]).toEqual(["v1", "v2"]);
    expect(parsed.rows).toEqual([
      {
        kind: "hunk",
        text: "@@ -2,3 +2,4 @@ policy",
        oldLine: null,
        newLine: null,
        hunk: { oldStart: 2, oldCount: 3, newStart: 2, newCount: 4, label: "policy" },
      },
      { kind: "context", text: "retained", oldLine: 2, newLine: 2 },
      { kind: "removed", text: "old rule", oldLine: 3, newLine: null },
      { kind: "added", text: "new rule", oldLine: null, newLine: 3 },
      { kind: "added", text: "new evidence", oldLine: null, newLine: 4 },
      { kind: "context", text: "tail", oldLine: 4, newLine: 5 },
      {
        kind: "hunk",
        text: "@@ -20 +21 @@",
        oldLine: null,
        newLine: null,
        hunk: { oldStart: 20, oldCount: 1, newStart: 21, newCount: 1, label: "" },
      },
      { kind: "removed", text: "before", oldLine: 20, newLine: null },
      { kind: "added", text: "after", oldLine: null, newLine: 21 },
    ]);
  });

  it("treats literal +++ and --- content after the first hunk as added and removed lines", () => {
    const parsed = parseDocumentDiff(
      [
        "--- v1",
        "+++ v2",
        "@@ -1 +1 @@",
        "--- literal old content",
        "+++ literal new content",
      ].join("\n"),
    );

    expect([parsed.fromFile, parsed.toFile]).toEqual(["v1", "v2"]);
    expect(parsed.rows.slice(1)).toEqual([
      { kind: "removed", text: "-- literal old content", oldLine: 1, newLine: null },
      { kind: "added", text: "++ literal new content", oldLine: null, newLine: 1 },
    ]);
  });

  it("represents an empty server diff as unchanged without manufacturing rows", () => {
    expect(parseDocumentDiff("")).toEqual({
      state: "unchanged",
      fromFile: null,
      toFile: null,
      rows: [],
    });
  });

  it("keeps unexpected pre-hunk and no-newline markers as non-numbered metadata", () => {
    const parsed = parseDocumentDiff(
      ["server note", "--- v4", "+++ v5", "@@ -0,0 +1,2 @@", "+first", "\\ No newline"].join("\n"),
    );
    expect(parsed.rows).toEqual([
      { kind: "metadata", text: "server note", oldLine: null, newLine: null },
      {
        kind: "hunk",
        text: "@@ -0,0 +1,2 @@",
        oldLine: null,
        newLine: null,
        hunk: { oldStart: 0, oldCount: 0, newStart: 1, newCount: 2, label: "" },
      },
      { kind: "added", text: "first", oldLine: null, newLine: 1 },
      { kind: "metadata", text: "\\ No newline", oldLine: null, newLine: null },
    ]);
  });
});
