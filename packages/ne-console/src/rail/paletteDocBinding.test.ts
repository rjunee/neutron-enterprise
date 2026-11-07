// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE §3 BINDING. `docs/design/ne-console-redesign-spec.md` §3 is the palette's
// specification, and until this file existed nothing held the two together: the
// document went on describing a bare-UUID approval route months after the code
// stopped resolving one, and quoted a no-match line the code no longer rendered.
// Neither drift could fail anything, because no test read the document.
//
// WHAT THIS FILE ASSERTS, and why each shape was chosen (docs/testing/vacuous-guards.md):
//
//   * The no-match line is compared to `NO_MATCH_COPY` by EXACT EQUALITY, not
//     containment (V1). A substring assertion over that sentence is monotone
//     under extension — the very edit that added the approval form to the copy
//     would have left it green.
//   * The rank-1 grammars are not read as regexes and eyeballed; every row's
//     query is EXECUTED through `idResults` and its route and type label
//     compared to the cells beside it. A doc row is therefore a claim the code
//     has to satisfy, not prose sitting next to code.
//   * The counter-example table is executed the same way and must resolve
//     NOTHING, so re-widening a grammar (what PRE-012C removed) fails here even
//     though the positive table would still pass.
//   * The table's rows are matched ONE-TO-ONE against the module's rank-1
//     BRANCHES — the `return [{…}]` sites inside `idResults` — by the pair
//     (type label, route template), never by type label alone. A set of labels
//     cannot see a second grammar that reuses an existing one: a `req <anything>`
//     branch returning `kind: "Approval request"` onto `/approvals?request=…`
//     routes arbitrary run and conversation ids at an approvals lookup, is
//     strictly worse than what PRE-012C removed, and a label-set check is green
//     against it. The bijection is red on both legs — an unclaimed branch, and a
//     row two branches answer to.
//   * WHAT THIS DOES NOT PROVE, stated because the word it replaces
//     ("exhaustively") read as if it did. The bijection bounds the NUMBER of
//     grammars, never the WIDTH of one: loosening a branch's own matcher leaves
//     the branch count and every route template unchanged and passes it. Width
//     is carried entirely by the counter-example table, one row per edge —
//     widening the trace grammar to 16-64 hex or to uppercase is red only
//     because a row names each of those edges. TWO RESIDUALS FOLLOW, and
//     neither is closed here: an edge no row names is not covered, and deleting
//     a row does not itself fail, because that table is a declared SAMPLE and
//     nothing can say which samples it ought to hold.
//   * Every extraction asserts its own anchor matched before comparing, and the
//     parsers are additionally run over a fixture document with planted
//     disagreements (the last describe) — an anchor that silently matches
//     nothing has produced false greens in this repo before, and "the parse
//     returned nothing so nothing disagreed" is the shape that does it.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { idResults, NO_MATCH_COPY, PALETTE_ACTIONS } from "./paletteIndex";

const REPO_ROOT = resolve(fileURLToPath(import.meta.url), "..", "..", "..", "..", "..");
const SPEC_PATH = resolve(REPO_ROOT, "docs/design/ne-console-redesign-spec.md");
const MODULE_PATH = resolve(REPO_ROOT, "packages/ne-console/src/rail/paletteIndex.ts");
const HEADING = "## 3. The command palette";

/** The body of one `## ` section, up to the next one. `""` when the heading is
 * absent, which every caller asserts against rather than iterating over. */
export function specSection(markdown: string, heading: string): string {
  const at = markdown.indexOf(`\n${heading}\n`);
  if (at === -1) return "";
  const rest = markdown.slice(at + heading.length + 2);
  const next = rest.search(/\n## /);
  return next === -1 ? rest : rest.slice(0, next);
}

/** A cell written as a code span, with the delimiters removed. `null` when the
 * cell is NOT a code span — a bare cell is a formatting change that would
 * otherwise silently alter what is being compared. */
export function codeSpan(cell: string): string | null {
  const match = /^`(.+)`$/.exec(cell.trim());
  return match === null ? null : match[1]!;
}

/** Rows of the one table in `section` whose header is exactly `header`. */
export function specTable(section: string, header: readonly string[]): string[][] {
  const lines = section.split("\n").map((line) => line.trim());
  const at = lines.indexOf(`| ${header.join(" | ")} |`);
  if (at === -1) return [];
  const rows: string[][] = [];
  for (let i = at + 2; i < lines.length; i += 1) {
    const line = lines[i]!;
    if (!line.startsWith("|") || !line.endsWith("|")) break;
    rows.push(
      line
        .slice(1, -1)
        .split("|")
        .map((cell) => cell.trim()),
    );
  }
  return rows;
}

/** Every ```text fenced block in `section`, verbatim. */
export function fencedText(section: string): string[] {
  const out: string[] = [];
  const fence = /(?:^|\n)```text\n([\s\S]*?)\n```(?:\n|$)/g;
  let match = fence.exec(section);
  while (match !== null) {
    out.push(match[1]!);
    match = fence.exec(section);
  }
  return out;
}

/** The double-quoted strings inside the numbered item `n` of the rank list. */
export function rankItemQuotes(section: string, n: number, bold: string): string[] {
  const item = new RegExp(`\\n${n}\\. \\*\\*${bold}\\*\\*([\\s\\S]*?)\\n${n + 1}\\. `).exec(
    section,
  );
  if (item === null) return [];
  return [...item[1]!.matchAll(/"([^"]+)"/g)].map((m) => m[1]!);
}

/** One rank-1 branch of `idResults`, as the module's source states it: the type
 * label it labels a row with, and the route template it sends Enter to. The
 * pair is the branch's SIGNATURE — two branches can share a label, which is
 * exactly the case a label-set comparison cannot see. */
export interface RankOneBranch {
  kind: string;
  route: string;
}

/** Every `return [{ … }]` inside `idResults` that produces a rank-1 row. The
 * bare `return []` paths carry no `kind:` and are not branches of this list. */
export function rankOneBranches(moduleSource: string): RankOneBranch[] {
  const start = moduleSource.indexOf("export function idResults");
  if (start === -1) return [];
  const end = moduleSource.indexOf("\n}\n", start);
  if (end === -1) return [];
  const body = moduleSource.slice(start, end);
  const out: RankOneBranch[] = [];
  for (const match of body.matchAll(/return \[([\s\S]*?)\];/g)) {
    const chunk = match[1]!;
    const kind = /kind:\s*"([^"]+)"/.exec(chunk);
    const route = /to:\s*`([^`]*)`/.exec(chunk);
    if (kind === null && route === null) continue;
    out.push({ kind: kind === null ? "" : kind[1]!, route: route === null ? "" : route[1]! });
  }
  return out;
}

/** A `to:` template as the routes it can produce. `${…}` stands for the id the
 * branch interpolates, so the doc's worked example instantiates the template it
 * came from and no other. */
export function routeMatcher(template: string): RegExp {
  const literals = template
    .split(/\$\{[^}]*\}/g)
    .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return new RegExp(`^${literals.join("[^\\s]+")}$`);
}

const SPEC = readFileSync(SPEC_PATH, "utf-8");
const SECTION = specSection(SPEC, HEADING);
const MODULE_SOURCE = readFileSync(MODULE_PATH, "utf-8");

const GRAMMAR_HEADER = ["Query", "Enter goes to", "Type label"] as const;
const NON_GRAMMAR_HEADER = ["Query", "Why it resolves nothing"] as const;

describe("§3 of the console redesign spec is bound to the palette module", () => {
  it("finds the section it claims to be checking", () => {
    // The anchor, asserted before anything is extracted from it. Renaming the
    // heading fails HERE rather than emptying every extraction below into a
    // vacuous green.
    expect(SECTION).not.toBe("");
    expect(SECTION).toContain("NO_MATCH_COPY");
  });

  it("quotes the no-match line exactly as the module exports it", () => {
    const blocks = fencedText(SECTION);
    // One block, so "the line" in the prose above it has a single referent.
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toBe(NO_MATCH_COPY);
  });

  it("resolves every documented rank-1 grammar to the documented route and label", () => {
    const rows = specTable(SECTION, GRAMMAR_HEADER);
    expect(rows.length).toBeGreaterThanOrEqual(4);
    for (const row of rows) {
      expect(row).toHaveLength(GRAMMAR_HEADER.length);
      const [queryCell, routeCell, label] = row as [string, string, string];
      const query = codeSpan(queryCell);
      const route = codeSpan(routeCell);
      expect(query, `query cell is not a code span: ${queryCell}`).not.toBeNull();
      expect(route, `route cell is not a code span: ${routeCell}`).not.toBeNull();

      const results = idResults(query!);
      expect(results, `no rank-1 row for the documented query ${query!}`).toHaveLength(1);
      expect(results[0]!.to).toBe(route!);
      expect(results[0]!.kind).toBe(label);
      expect(results[0]!.group).toBe("id");
    }
  });

  it("resolves nothing for every query the section says is not a grammar", () => {
    const rows = specTable(SECTION, NON_GRAMMAR_HEADER);
    expect(rows.length).toBeGreaterThanOrEqual(4);
    for (const row of rows) {
      expect(row).toHaveLength(NON_GRAMMAR_HEADER.length);
      const query = codeSpan(row[0]!);
      expect(query, `query cell is not a code span: ${row[0]!}`).not.toBeNull();
      expect(idResults(query!), `${query!} resolved a rank-1 row the section forbids`).toEqual([]);
    }
  });

  it("matches every rank-1 branch to one table row, and every row to one branch", () => {
    // Signature, not label. A fifth branch reusing "Approval request" to route
    // arbitrary strings at the approvals lookup passes a label-set comparison
    // and fails both legs of this one: it is claimed by no row, and the row it
    // imitates is answered by two branches.
    const branches = rankOneBranches(MODULE_SOURCE);
    expect(branches.length).toBeGreaterThan(0);
    for (const branch of branches) {
      // Extraction liveness. A branch whose label or template failed to parse
      // would be claimed by nothing and read as a missing row rather than as a
      // broken parse.
      expect(branch.kind, `branch has no kind literal: ${branch.route}`).not.toBe("");
      expect(branch.route, `branch has no to: template: ${branch.kind}`).not.toBe("");
    }

    const rows = specTable(SECTION, GRAMMAR_HEADER);
    expect(rows).toHaveLength(branches.length);

    const claimed = new Set<number>();
    for (const row of rows) {
      const route = codeSpan(row[1]!);
      expect(route, `route cell is not a code span: ${row[1]!}`).not.toBeNull();
      const label = row[2]!;
      const answering = branches
        .map((branch, index) => ({ branch, index }))
        .filter(({ branch }) => branch.kind === label && routeMatcher(branch.route).test(route!));
      expect(
        answering.map(({ branch }) => branch.route),
        `${label} → ${route!} is answered by ${answering.length} branches, not one`,
      ).toHaveLength(1);
      const index = answering[0]!.index;
      expect(claimed.has(index), `two rows claim ${branches[index]!.route}`).toBe(false);
      claimed.add(index);
    }
    expect(claimed.size).toBe(branches.length);
  });

  it("lists the rank-3 action labels the palette actually offers", () => {
    // `PALETTE_ACTIONS`'s own comment claims its labels are "SPEC COPY,
    // VERBATIM (§3)". Nothing read the spec, so the claim was an argument.
    const quoted = rankItemQuotes(SECTION, 3, "Actions");
    expect(quoted.length).toBeGreaterThan(0);
    expect(quoted).toEqual(PALETTE_ACTIONS.map((action) => action.label));
  });
});

describe("the extractions are live — planted disagreements are caught", () => {
  // A should-PASS control for every parser above, plus its should-FAIL twin, so
  // "the doc and the code agree" is distinguishable from "the parser found
  // nothing to compare". Without this, deleting the table would be green.
  const FIXTURE = [
    "",
    "## 3. The command palette",
    "",
    "Prose mentioning NO_MATCH_COPY.",
    "",
    "1. **Typed object ids** — text.",
    "2. **Destinations** — text.",
    '3. **Actions** — "Ask a question", "New search".',
    "4. **Recents** — text.",
    "",
    "| Query | Enter goes to | Type label |",
    "|---|---|---|",
    "| `seq 4210` | `/audit?seq=9999` | Audit entry |",
    "",
    "| Query | Why it resolves nothing |",
    "|---|---|",
    "| `seq 4210` | it resolves fine, actually |",
    "",
    "```text",
    "Not the exported copy.",
    "```",
    "",
    "## 4. Home",
    "",
  ].join("\n");

  const section = specSection(FIXTURE, HEADING);

  it("reads the fixture section at all", () => {
    expect(section).not.toBe("");
  });

  it("would fail on a doc whose fenced copy disagrees with the export", () => {
    expect(fencedText(section)).toEqual(["Not the exported copy."]);
    expect(fencedText(section)[0]).not.toBe(NO_MATCH_COPY);
  });

  it("would fail on a doc whose grammar row names the wrong route", () => {
    const rows = specTable(section, GRAMMAR_HEADER);
    expect(rows).toEqual([["`seq 4210`", "`/audit?seq=9999`", "Audit entry"]]);
    const results = idResults(codeSpan(rows[0]![0]!)!);
    expect(results).toHaveLength(1);
    expect(results[0]!.to).not.toBe(codeSpan(rows[0]![1]!));
  });

  it("would fail on a doc that calls a live grammar a non-match", () => {
    const rows = specTable(section, NON_GRAMMAR_HEADER);
    expect(rows).toEqual([["`seq 4210`", "it resolves fine, actually"]]);
    expect(idResults(codeSpan(rows[0]![0]!)!)).not.toEqual([]);
  });

  it("would fail on a doc that lists the wrong action labels", () => {
    const quoted = rankItemQuotes(section, 3, "Actions");
    expect(quoted).toEqual(["Ask a question", "New search"]);
    expect(quoted).not.toEqual(PALETTE_ACTIONS.map((action) => action.label));
  });

  it("returns nothing — loudly, never a passing empty comparison — off its anchors", () => {
    expect(specSection(FIXTURE, "## 9. Nowhere")).toBe("");
    expect(specTable(section, ["Query", "Renamed column"])).toEqual([]);
    expect(rankItemQuotes(section, 3, "Renamed")).toEqual([]);
    expect(codeSpan("Approval request")).toBeNull();
    expect(rankOneBranches("export const nothing = 1;\n")).toEqual([]);
  });

  it("would fail on a module whose fifth branch reuses an existing type label", () => {
    // The load-bearing case, run as a fixture so the discrimination is pinned
    // permanently rather than only at the moment someone mutates the module.
    const mutant = [
      "export function idResults(query: string): PaletteResult[] {",
      "  if (approval) {",
      '    return [{ id: `id:approval:${q}`, kind: "Approval request", group: "id", to: `/approvals?request=${q}` }];',
      "  }",
      "  if (req) {",
      '    return [{ id: `id:req:${q}`, kind: "Approval request", group: "id", to: `/approvals?request=${q}` }];',
      "  }",
      "  return [];",
      "}",
      "",
    ].join("\n");
    const branches = rankOneBranches(mutant);
    expect(branches).toHaveLength(2);
    // What a label-set comparison sees: one label, agreeing with one row.
    expect(new Set(branches.map((branch) => branch.kind)).size).toBe(1);
    // What the signature comparison sees: one documented row, two answers.
    const answering = branches.filter(
      (branch) =>
        branch.kind === "Approval request" &&
        routeMatcher(branch.route).test("/approvals?request=8f14e45fceea167a5a36dedd4bea2543"),
    );
    expect(answering).toHaveLength(2);
  });

  it("instantiates a route template only from the branch that produced it", () => {
    expect(routeMatcher("/trace/${q}").test("/trace/4f6b0c8d1e2a3b4c5d6e7f8091a2b3c4")).toBe(true);
    expect(routeMatcher("/trace/${q}").test("/approvals?request=abc")).toBe(false);
    expect(routeMatcher("/audit?seq=${anchor.seq}").test("/audit?seq=4210")).toBe(true);
    expect(routeMatcher("/audit?seq=${anchor.seq}").test("/audit?entry=4210")).toBe(false);
    // The interpolation stands for an id, never for nothing.
    expect(routeMatcher("/trace/${q}").test("/trace/")).toBe(false);
  });
});
