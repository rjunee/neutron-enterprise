// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const TABLE = readFileSync(resolve(process.cwd(), "src/styles/table.css"), "utf8");
const WORKFLOWS = readFileSync(resolve(process.cwd(), "src/workflows/workflows.css"), "utf8");
const LIBRARY = readFileSync(resolve(process.cwd(), "src/knowledge/library.css"), "utf8");
const TOOLBAR_SOURCE = readFileSync(resolve(process.cwd(), "src/primitives/Toolbar.tsx"), "utf8");
const WORKFLOWS_SOURCE = readFileSync(
  resolve(process.cwd(), "src/workflows/WorkflowsPage.tsx"),
  "utf8",
);
const LIBRARY_SOURCE = readFileSync(
  resolve(process.cwd(), "src/knowledge/LibraryPage.tsx"),
  "utf8",
);
const DOCUMENTS_SOURCE = readFileSync(
  resolve(process.cwd(), "src/knowledge/DocumentsTab.tsx"),
  "utf8",
);

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "");
}

function blockAfter(source: string, marker: string): string {
  const markerAt = source.indexOf(marker);
  expect(markerAt, `missing ${marker}`).toBeGreaterThanOrEqual(0);
  const open = source.indexOf("{", markerAt + marker.length);
  expect(open, `missing block for ${marker}`).toBeGreaterThan(markerAt);
  let depth = 1;
  for (let cursor = open + 1; cursor < source.length; cursor += 1) {
    if (source[cursor] === "{") depth += 1;
    if (source[cursor] === "}") depth -= 1;
    if (depth === 0) return source.slice(open + 1, cursor);
  }
  throw new Error(`unterminated block for ${marker}`);
}

function declarations(source: string, selector: string): Record<string, string> {
  const clean = stripComments(source);
  let cursor = 0;
  while (cursor < clean.length) {
    const open = clean.indexOf("{", cursor);
    if (open < 0) break;
    const selectors = clean
      .slice(cursor, open)
      .trim()
      .split(",")
      .map((part) => part.trim().replace(/\s+/g, " "));
    let depth = 1;
    let close = open + 1;
    for (; close < clean.length && depth > 0; close += 1) {
      if (clean[close] === "{") depth += 1;
      if (clean[close] === "}") depth -= 1;
    }
    if (selectors.includes(selector)) {
      return Object.fromEntries(
        clean
          .slice(open + 1, close - 1)
          .split(";")
          .map((line) => line.trim())
          .filter(Boolean)
          .map((line) => {
            const colon = line.indexOf(":");
            return [line.slice(0, colon).trim(), line.slice(colon + 1).trim()];
          }),
      );
    }
    cursor = close;
  }
  throw new Error(`missing rule ${selector}`);
}

describe("390px shared layout contracts", () => {
  const phone = blockAfter(TABLE, "@media (max-width: 48rem)");

  it("wraps toolbar actions and the live count without widening the document", () => {
    for (const productionClass of [
      "ne-toolbar",
      "ne-toolbar__spacer",
      "ne-toolbar__filters",
      "ne-toolbar__count",
      "ne-toolbar__actions",
    ]) {
      expect(TOOLBAR_SOURCE).toContain(productionClass);
    }
    expect(declarations(phone, ".ne-toolbar")).toMatchObject({
      "flex-wrap": "wrap",
      "min-width": "0",
    });
    for (const selector of [".ne-toolbar__filters", ".ne-toolbar__actions"]) {
      expect(declarations(phone, selector)).toMatchObject({
        "flex-wrap": "wrap",
        "min-width": "0",
        "max-width": "100%",
      });
    }
    expect(declarations(phone, ".ne-toolbar__spacer")).toEqual({ display: "none" });
    expect(declarations(phone, ".ne-toolbar__count")).toMatchObject({
      "min-width": "0",
      "max-width": "100%",
      "white-space": "normal",
    });
    expect(declarations(TABLE, ".ne-toolbar__count")).toMatchObject({
      "white-space": "nowrap",
    });
  });

  it("lets long search and FilterSelect labels shrink inside the phone toolbar", () => {
    expect(TOOLBAR_SOURCE).toContain('className="ne-filter"');
    expect(TOOLBAR_SOURCE).toContain('className="ne-filter__select"');
    expect(declarations(phone, ".ne-searchwithin__input")).toEqual({
      "min-width": "0",
      "max-width": "100%",
    });
    expect(declarations(phone, ".ne-filter")).toMatchObject({
      flex: "1 1 12rem",
      "flex-wrap": "wrap",
      "min-width": "0",
      "max-width": "100%",
    });
    expect(declarations(phone, ".ne-filter__select")).toEqual({
      flex: "1 1 12rem",
      "min-width": "0",
      "max-width": "100%",
    });
  });

  it("leaves wide data tables in their existing local scrollport", () => {
    expect(declarations(TABLE, ".ne-table__wrap")).toMatchObject({
      "min-width": "0",
      "overflow-x": "auto",
    });
  });
});

describe("390px page bindings", () => {
  it("shrinks the workflow run row and its served model picker", () => {
    const phone = blockAfter(WORKFLOWS, "@media (max-width: 640px)");
    expect(WORKFLOWS_SOURCE).toContain('className="ne-workflows__run"');
    expect(WORKFLOWS_SOURCE).toContain("<RunModelPicker");
    expect(declarations(phone, ".ne-workflows__run")).toMatchObject({
      "align-items": "stretch",
      "flex-wrap": "wrap",
      "min-width": "0",
    });
    expect(declarations(phone, ".ne-workflows__run .ne-filter")).toEqual({
      flex: "1 1 14rem",
      "min-width": "0",
      "max-width": "100%",
    });
    expect(declarations(phone, ".ne-workflows__run .ne-filter__select")).toEqual({
      width: "100%",
      "min-width": "0",
      "max-width": "100%",
    });
  });

  it("wraps Library tabs and shrinks the document chooser without stealing local scroll", () => {
    const phone = blockAfter(LIBRARY, "@media (max-width: 48rem)");
    expect(LIBRARY_SOURCE).toContain('className="ne-library__tabs"');
    expect(DOCUMENTS_SOURCE).toContain('className="ne-docjourney__chooser"');
    expect(declarations(phone, ".ne-library__tabs")).toEqual({
      "flex-wrap": "wrap",
      "min-width": "0",
      "max-width": "100%",
    });
    expect(declarations(phone, ".ne-docjourney__chooser")).toEqual({
      "flex-wrap": "wrap",
      "min-width": "0",
      "max-width": "100%",
    });
    expect(declarations(phone, ".ne-docjourney__chooser select")).toEqual({
      flex: "1 1 16rem",
      "min-width": "0",
      "max-width": "100%",
    });
    expect(declarations(LIBRARY, ".ne-library .ne-table__wrap")).toMatchObject({
      "overflow-x": "auto",
    });
    expect(declarations(LIBRARY, ".ne-docjourney__pipeline")).toMatchObject({
      "overflow-x": "auto",
    });
  });

  it("contains the document as-of controls in DOM order without changing pipeline scrolling", () => {
    const phone = blockAfter(LIBRARY, "@media (max-width: 48rem)");
    const asOfRow = DOCUMENTS_SOURCE.indexOf('className="ne-docjourney__asof"');
    const asOfLabel = DOCUMENTS_SOURCE.indexOf('htmlFor="ne-document-asof"', asOfRow);
    const asOfInput = DOCUMENTS_SOURCE.indexOf('id="ne-document-asof"', asOfLabel);
    const retrieve = DOCUMENTS_SOURCE.indexOf("Retrieve", asOfInput);
    const latest = DOCUMENTS_SOURCE.indexOf("Latest", retrieve);

    expect(asOfRow).toBeGreaterThanOrEqual(0);
    expect(asOfLabel).toBeGreaterThan(asOfRow);
    expect(asOfInput).toBeGreaterThan(asOfLabel);
    expect(retrieve).toBeGreaterThan(asOfInput);
    expect(latest).toBeGreaterThan(retrieve);
    expect(declarations(phone, ".ne-docjourney__asof")).toEqual({
      "flex-wrap": "wrap",
      "min-width": "0",
      "max-width": "100%",
    });
    expect(declarations(phone, ".ne-docjourney__asof input")).toEqual({
      flex: "1 1 14rem",
      "min-width": "0",
      "max-width": "100%",
    });
    expect(declarations(LIBRARY, ".ne-docjourney__pipeline")).toMatchObject({
      "overflow-x": "auto",
    });
  });
});
