// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// TOKEN-HARDCODE GUARDIAN (NE-130 ⭐). An fs-walk of the PRODUCTION source proves,
// by construction, that NO component .ts/.tsx/.css hardcodes a raw color, a px
// font-size, or a non-token font-family — every style must resolve through a
// src/styles/tokens.css custom property. This makes the token layer the single
// point of aesthetic change and makes palette regression impossible to merge.
//
// Scope: all production .ts/.tsx/.css under src/, EXCLUDING src/test/**, *.test.*,
// and EXACTLY src/styles/tokens.css (the ONE file where raw hexes live). fonts.css
// IS scanned (it must carry no colors, no font-sizes, and no external URL).

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");
const TOKENS_CSS = resolve(SRC_DIR, "styles", "tokens.css");

// Banned patterns. (Assembled defensively where a literal could match this file,
// though this file is excluded from the walk as a *.test.* source.)
const RAW_HEX = /#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b/;
const COLOR_FN = /\b(?:rgba?|hsla?|oklch|color-mix)\s*\(/;
const CSS_PX_FONT_SIZE = /font-size\s*:\s*[\d.]+px/;
const TSX_PX_FONT_SIZE = /fontSize\s*:\s*["']?[\d.]+px/;
const FONT_FAMILY_DECL = /font-family\s*:\s*([^;}]+)/g;
const TOKEN_FONT = "var(--font-";

/** Collect production .ts/.tsx/.css under src/, skipping the test tree, test
 * files, and tokens.css. */
function scannedSources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "test") continue;
      out.push(...scannedSources(full));
      continue;
    }
    if (/\.test\.tsx?$/.test(entry)) continue;
    if (full === TOKENS_CSS) continue;
    if (/\.(tsx?|css)$/.test(entry)) out.push(full);
  }
  return out;
}

const sources = scannedSources(SRC_DIR);

function offenders(re: RegExp): string[] {
  return sources.filter((f) => re.test(readFileSync(f, "utf-8"))).map((f) => relative(SRC_DIR, f));
}

describe("token-hardcode guardian", () => {
  it("finds production sources to scan (and excludes tokens.css)", () => {
    expect(sources.length).toBeGreaterThan(5);
    expect(sources.map((f) => relative(SRC_DIR, f))).not.toContain("styles/tokens.css");
  });

  it("(1) no raw hex color outside tokens.css", () => {
    expect(offenders(RAW_HEX)).toEqual([]);
  });

  it("(2) no rgb/hsl/oklch/color-mix color function outside tokens.css", () => {
    expect(offenders(COLOR_FN)).toEqual([]);
  });

  it("(3) no px font-size in CSS or TSX", () => {
    expect(offenders(CSS_PX_FONT_SIZE)).toEqual([]);
    expect(offenders(TSX_PX_FONT_SIZE)).toEqual([]);
  });

  it("(4) every font-family declaration (outside tokens.css + fonts.css) resolves through a --font- token", () => {
    const bad: string[] = [];
    for (const f of sources) {
      if (f.endsWith(join("styles", "fonts.css"))) continue; // @font-face names the faces
      const text = readFileSync(f, "utf-8");
      for (const m of text.matchAll(FONT_FAMILY_DECL)) {
        const decl = m[1] ?? "";
        if (!decl.includes(TOKEN_FONT)) bad.push(`${relative(SRC_DIR, f)} :: ${m[0].trim()}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("(5) self-test: each banned pattern bites a planted violation", () => {
    // Fragments so the planted literals never trip the walk if this file were scanned.
    const plantedHex = `color: ${"#"}${"ff0000"};`;
    const plantedRgb = `color: rgb${"a"}(1, 2, 3, 0.5);`;
    const plantedCssPx = `font-size: ${"13"}px;`;
    const plantedTsxPx = `fontSize: "${"13"}px"`;
    const plantedFamily = `font-family: ${"Arial"}, sans-serif`;
    expect(RAW_HEX.test(plantedHex)).toBe(true);
    expect(COLOR_FN.test(plantedRgb)).toBe(true);
    expect(CSS_PX_FONT_SIZE.test(plantedCssPx)).toBe(true);
    expect(TSX_PX_FONT_SIZE.test(plantedTsxPx)).toBe(true);
    // The family check: a non-token family is flagged, a token family is not.
    expect(plantedFamily.includes(TOKEN_FONT)).toBe(false);
    expect(`font-family: ${TOKEN_FONT}ui)`.includes(TOKEN_FONT)).toBe(true);
  });

  it("(6) fonts.css contains no external URL (self-contained C1/C2)", () => {
    const fontsCss = readFileSync(resolve(SRC_DIR, "styles", "fonts.css"), "utf-8");
    expect(fontsCss.includes("htt" + "p")).toBe(false);
  });
});
