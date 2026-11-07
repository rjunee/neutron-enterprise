// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

export interface DocumentDiffHunk {
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
  label: string;
}

export type DocumentDiffRow =
  | { kind: "hunk"; text: string; oldLine: null; newLine: null; hunk: DocumentDiffHunk }
  | {
      kind: "context" | "added" | "removed";
      text: string;
      oldLine: number | null;
      newLine: number | null;
    }
  | { kind: "metadata"; text: string; oldLine: null; newLine: null };

export interface ParsedDocumentDiff {
  state: "unchanged" | "changed";
  fromFile: string | null;
  toFile: string | null;
  rows: readonly DocumentDiffRow[];
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

function parseHunk(line: string): DocumentDiffHunk | null {
  const match = HUNK.exec(line);
  if (match === null) return null;
  return {
    oldStart: Number(match[1]),
    oldCount: match[2] === undefined ? 1 : Number(match[2]),
    newStart: Number(match[3]),
    newCount: match[4] === undefined ? 1 : Number(match[4]),
    label: match[5]?.trim() ?? "",
  };
}

/** Parse the server's Python ``difflib.unified_diff`` presentation bytes. */
export function parseDocumentDiff(diff: string): ParsedDocumentDiff {
  if (diff === "") return { state: "unchanged", fromFile: null, toFile: null, rows: [] };

  let fromFile: string | null = null;
  let toFile: string | null = null;
  let sawHunk = false;
  let oldLine = 0;
  let newLine = 0;
  const rows: DocumentDiffRow[] = [];

  for (const line of diff.split("\n")) {
    const hunk = parseHunk(line);
    if (hunk !== null) {
      sawHunk = true;
      oldLine = hunk.oldStart;
      newLine = hunk.newStart;
      rows.push({ kind: "hunk", text: line, oldLine: null, newLine: null, hunk });
      continue;
    }
    if (!sawHunk && fromFile === null && line.startsWith("--- ")) {
      fromFile = line.slice(4);
      continue;
    }
    if (!sawHunk && toFile === null && line.startsWith("+++ ")) {
      toFile = line.slice(4);
      continue;
    }
    if (!sawHunk) {
      rows.push({ kind: "metadata", text: line, oldLine: null, newLine: null });
      continue;
    }
    if (line.startsWith("+")) {
      rows.push({ kind: "added", text: line.slice(1), oldLine: null, newLine });
      newLine += 1;
    } else if (line.startsWith("-")) {
      rows.push({ kind: "removed", text: line.slice(1), oldLine, newLine: null });
      oldLine += 1;
    } else if (line.startsWith(" ")) {
      rows.push({ kind: "context", text: line.slice(1), oldLine, newLine });
      oldLine += 1;
      newLine += 1;
    } else {
      rows.push({ kind: "metadata", text: line, oldLine: null, newLine: null });
    }
  }

  return { state: "changed", fromFile, toFile, rows };
}
