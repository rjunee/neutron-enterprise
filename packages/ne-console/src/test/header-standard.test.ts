// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// HEADER-STANDARD GUARDIAN (SPEC §11 — "THE CONSOLE HAS ONE SCREEN-HEADER
// STANDARD"). The console titles every screen the same way: a breadcrumb the
// shell draws once (`RoutePresentation`), and ONE modest title row from the
// shared `PageHeader`/`ListScreen` primitive. There is no per-screen eyebrow
// repeating the breadcrumb's place above the title, and no description/lede
// sentence explaining a screen the reader navigated to on purpose.
//
// This fs-walk proves, by construction, that no screen re-grows that furniture:
//
//   (1) The standard title class `ne-titlerow__h1` is rendered in exactly one
//       place — the shared PageHeader. A screen hand-rolling `<header
//       className="ne-titlerow">` (the pre-standard Audit log) reused that class
//       and bypassed the primitive; that is the shape this catches.
//   (2) No eyebrow: the className tokens `eyebrow`/`kicker`/`overline` — the
//       words a re-added place-above-the-title would wear — appear nowhere.
//   (3) No header lede: no screen renders a prose `<p>` as the first child of a
//       `<ListScreen>` or directly under a `<PageHeader/>`. That slot is the
//       screen's content (toolbar/table), never a sentence about the screen.
//   (4) The deleted legacy frame stays dead: `PageFrame` and its purpose-line
//       classes (`ne-page__lead`/`ne-page__subhead`/`ne-page__purpose`) — the
//       old §1.5 header band — exist in no production source.
//
// THE ONE SANCTIONED EXCEPTION is the Knowledge Graph workbar
// (`src/knowledge/graph/`), which deliberately replaces the header with a 48px
// workbar (breadcrumb + Graph|Record control + depth + count). It wears its own
// `ne-workbar__*` classes, so checks (1)–(3) exempt it by construction; a
// dedicated assertion below pins that it is the workbar and not a title row.
//
// Every check is paired with a self-test that plants the violation and spares a
// compliant sample, so a rule can never pass by examining nothing.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");

function productionSources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "test") continue;
      out.push(...productionSources(full));
      continue;
    }
    if (/\.test\.tsx?$/.test(entry)) continue;
    if (/\.(tsx?|css)$/.test(entry)) out.push(full);
  }
  return out;
}

/** Strip line and block comments so a doc-comment that names a banned token in
 * order to forbid it — including this file — never trips its own guardian. */
function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const sources = productionSources(SRC_DIR);
const rel = (f: string) => relative(SRC_DIR, f);
const bodies = new Map(sources.map((f) => [f, stripComments(readFileSync(f, "utf-8"))]));
const body = (f: string) => bodies.get(f) ?? "";
const tsx = sources.filter((f) => f.endsWith(".tsx"));

// The one file allowed to render the standard title markup.
const PAGE_HEADER = resolve(SRC_DIR, "primitives", "PageHeader.tsx");
// The one screen allowed to skip the title row entirely (its own workbar).
const GRAPH_WORKSPACE = resolve(SRC_DIR, "knowledge", "graph", "GraphWorkspace.tsx");

// ===========================================================================
// (3) Structural: a prose <p> lede directly under the title.
//
// A screen's first child under the title row is its content. A <p> in that slot
// is a lede sentence about the screen — the furniture the standard deletes. A
// JSX expression ({cond ? … }) or any non-<p> element passes; only a literal
// paragraph as the first child bites.
// ===========================================================================

/** From `<Tag` at `start`, the index of the `>` that closes the opening tag,
 * plus whether it self-closed. Brace- and string-aware so a `>` inside a prop
 * expression or string is not mistaken for the tag end. */
function openingTagEnd(text: string, start: number): { end: number; selfClosing: boolean } | null {
  let depth = 0;
  let inStr: string | null = null;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (inStr) {
      if (c === inStr) inStr = null;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") inStr = c;
    else if (c === "{") depth++;
    else if (c === "}") depth--;
    else if (c === ">" && depth === 0) return { end: i, selfClosing: text[i - 1] === "/" };
  }
  return null;
}

/** True when the first child element after `contentStart` is a `<p>`. */
export function firstChildIsParagraph(text: string, contentStart: number): boolean {
  let i = contentStart;
  // Skip whitespace (comments are already stripped by the caller).
  while (i < text.length && /\s/.test(text[i] ?? "")) i++;
  return /^<p[\s/>]/.test(text.slice(i));
}

/** Every `<ListScreen>`/`<PageHeader>` whose immediate title-slot child is a
 * prose paragraph. `text` must be comment-stripped. */
export function ledeViolations(text: string): string[] {
  const out: string[] = [];
  for (const tag of ["ListScreen", "PageHeader"] as const) {
    const re = new RegExp(`<${tag}(?=[\\s/>])`, "g");
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
      const open = openingTagEnd(text, m.index);
      if (open === null) continue;
      // ListScreen carries children after `>`; PageHeader is self-closing and
      // its lede would be the sibling right after `/>`. Both are "the slot
      // under the title", checked the same way.
      if (firstChildIsParagraph(text, open.end + 1)) out.push(tag);
    }
  }
  return out;
}

// ===========================================================================
// The scans, as pure predicates over comment-stripped source.
// ===========================================================================

const TITLE_CLASS = "ne-titlerow__h1";
const EYEBROW = /class[Nn]ame\s*=\s*["'{][^"'}]*(?:eyebrow|kicker|overline)/;
const LEGACY_FRAME = /\bPageFrame\b|\bne-page__(?:lead|subhead|purpose)\b/;

describe("header-standard guardian", () => {
  it("finds the screens it audits (never passes by examining nothing)", () => {
    expect(sources.length).toBeGreaterThan(20);
    const usesShared = tsx.filter(
      (f) => f !== PAGE_HEADER && /\b(?:ListScreen|PageHeader)\b/.test(body(f)),
    );
    // The shared header path is actually the path: most screens go through it.
    expect(usesShared.length).toBeGreaterThan(15);
    // …and the structural scan sees real ListScreen/PageHeader sites to reason
    // about, so check (3) is not vacuous.
    const sites = tsx.reduce(
      (n, f) => n + (body(f).match(/<(?:ListScreen|PageHeader)(?=[\s/>])/g)?.length ?? 0),
      0,
    );
    expect(sites).toBeGreaterThan(15);
  });

  it("(1) the standard title row is rendered only by the shared PageHeader", () => {
    // `.tsx` only: shell.css legitimately DEFINES the `.ne-titlerow__h1` style;
    // the rule is that only the shared primitive RENDERS the element.
    const offenders = tsx
      .filter((f) => f !== PAGE_HEADER && body(f).includes(TITLE_CLASS))
      .map(rel);
    expect(offenders).toEqual([]);
  });

  it("(2) no screen renders an eyebrow above its title", () => {
    const offenders = tsx.filter((f) => EYEBROW.test(body(f))).map(rel);
    expect(offenders).toEqual([]);
  });

  it("(3) no screen renders a description/lede sentence in its header", () => {
    const offenders: string[] = [];
    for (const f of tsx) {
      if (ledeViolations(body(f)).length > 0) offenders.push(rel(f));
    }
    expect(offenders).toEqual([]);
  });

  it("(4) the deleted §1.5 page-frame header stays dead", () => {
    const offenders = sources.filter((f) => LEGACY_FRAME.test(body(f))).map(rel);
    expect(offenders).toEqual([]);
  });

  it("the sanctioned exception is the Graph workbar, not a title row", () => {
    const graph = body(GRAPH_WORKSPACE);
    // It replaces the header with a workbar carrying its OWN place + title…
    expect(graph).toMatch(/ne-workbar__crumb/);
    expect(graph).toMatch(/ne-workbar__h1/);
    // …and therefore never touches the standard title class, so checks (1)-(3)
    // exempt it by construction rather than by a special case.
    expect(graph.includes(TITLE_CLASS)).toBe(false);
    expect(ledeViolations(graph)).toEqual([]);
  });

  it("self-test: each scan bites a planted violation and spares a compliant one", () => {
    // (1) hand-rolled title row.
    expect(`<h1 className="${TITLE_CLASS}">Audit log</h1>`.includes(TITLE_CLASS)).toBe(true);
    expect(`<h1 className="ne-workbar__h1">Graph</h1>`.includes(TITLE_CLASS)).toBe(false);

    // (2) eyebrow / kicker / overline.
    expect(EYEBROW.test('<span className="ne-x__eyebrow">Knowledge Layer</span>')).toBe(true);
    expect(EYEBROW.test('<p className="ne-models__lifecycleintro">…</p>')).toBe(false);
    expect(EYEBROW.test('<h1 className="ne-titlerow__h1">Models</h1>')).toBe(false);

    // (3) a prose <p> in the title slot bites; content and expressions spare.
    expect(
      ledeViolations('<ListScreen title="X"><p className="y">A lede.</p></ListScreen>'),
    ).toEqual(["ListScreen"]);
    expect(ledeViolations('<PageHeader title="X" /><p className="y">A lede.</p>')).toEqual([
      "PageHeader",
    ]);
    expect(ledeViolations('<ListScreen title="X"><Toolbar search={q} /></ListScreen>')).toEqual([]);
    expect(
      ledeViolations('<ListScreen title="X">{loading ? <p>Loading…</p> : <Table />}</ListScreen>'),
    ).toEqual([]);
    // A ListScreen with a `tabs={<Tabs a={x > y} />}` prop (a `>` inside an
    // expression) must not confuse the opening-tag scan.
    expect(
      ledeViolations('<ListScreen title="X" tabs={<Tabs a={x > y} />}><Toolbar /></ListScreen>'),
    ).toEqual([]);

    // (4) the legacy frame.
    expect(LEGACY_FRAME.test("import { PageFrame } from '../primitives/PageFrame';")).toBe(true);
    expect(LEGACY_FRAME.test('<p className="ne-page__lead">purpose</p>')).toBe(true);
    expect(LEGACY_FRAME.test('<div className="ne-screen__body">')).toBe(false);
  });
});
