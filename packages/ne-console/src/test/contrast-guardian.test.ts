// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// CONTRAST GUARDIAN (redesign §1.3, §5). `--text-faint` is #8595a5 on #ffffff
// (~3.1:1) and #68798d on #1a222d (~3.6:1). BOTH FAIL WCAG AA for body text, and
// it was the colour of every provenance/source line — precisely the text this
// redesign makes load-bearing. It is demoted to hairlines and disabled borders
// only: an fs-walk proves no production stylesheet uses it in a `color:`
// declaration (including the `color:` inside a `font:`-adjacent shorthand is not
// possible, so `color:` is the whole surface).
//
// This is a deliberate, flagged trade: it visibly darkens a lot of small type in
// a colour scheme the owner said they liked. It is the correct trade and it is
// not slipped in silently.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");

/** `color: … var(--text-faint) …;` — a text use of the banned token. */
const FAINT_TEXT = /(?:^|[;{\s])color\s*:\s*[^;}]*--text-faint/;

function stylesheets(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "test") continue;
      out.push(...stylesheets(full));
      continue;
    }
    if (entry.endsWith(".css")) out.push(full);
  }
  return out;
}

const sheets = stylesheets(SRC_DIR);

// ===========================================================================
// MEASURED CONTRAST ON THE STATE TOKENS (round-2 finding).
//
// The guardian above bans ONE token by name. That is a rule about a symbol, and
// it caught nothing when the failures moved to other symbols: a reviewer measured
// SEALED at 2.99:1 and ATTESTED at 3.30:1 in dark (the affirm word drew in
// --cherenkov-deep, a token tuned as a FILL), the light amber OPEN word at
// 3.50:1, the light citation chip at 4.21:1 and the dark HELD word at 4.43:1 —
// every one of them the STATE slot, which the four-part readout rule makes the
// part a reader acts on.
//
// So this computes the actual WCAG relative-luminance ratio for every colour the
// state vocabulary can draw in, against every surface it can draw on, and fails
// below 4.5:1 — the threshold for text at --type-label's 11px. The values are
// parsed out of the committed tokens.css, so editing a token to a failing value
// breaks this test rather than quietly shipping.
// ===========================================================================

const TOKENS_CSS = readFileSync(resolve(SRC_DIR, "styles", "tokens.css"), "utf-8");

/** The declared value of `--name` inside a given rule block. */
function tokenIn(block: string, name: string): string {
  const m = new RegExp(`${name}:\\s*(#[0-9a-f]{6})\\s*;`, "i").exec(block);
  if (m === null || m[1] === undefined) throw new Error(`token ${name} not found`);
  return m[1];
}

function blockFor(selector: string): string {
  const idx = TOKENS_CSS.indexOf(selector);
  if (idx < 0) throw new Error(`selector ${selector} not found`);
  const open = TOKENS_CSS.indexOf("{", idx);
  const close = TOKENS_CSS.indexOf("}", open);
  return TOKENS_CSS.slice(open, close);
}

/** WCAG 2.x relative luminance of an #rrggbb colour. */
export function luminance(hex: string): number {
  const c = hex.replace("#", "");
  const channels = [0, 2, 4].map((i) => {
    const v = parseInt(c.slice(i, i + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * channels[0]! + 0.7152 * channels[1]! + 0.0722 * channels[2]!;
}

/** WCAG 2.x contrast ratio between two #rrggbb colours. */
export function contrastRatio(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/** Every colour a <StatePill> tone can resolve to (see system.css .ne-pill--*). */
const STATE_TOKENS = ["--state-affirm", "--state-gated", "--red", "--amber", "--text-dim"];
/** Every surface a state word is drawn on. --panel-raised is the worst case and
 * was the one the previous pass never checked. */
const SURFACES = ["--ground", "--panel", "--panel-raised"];

/** 11px bold is below the WCAG "large text" threshold, so normal-text AA applies. */
const AA_NORMAL = 4.5;

describe("contrast guardian — --text-faint is banned for text", () => {
  it("finds the stylesheets to scan", () => {
    expect(sheets.length).toBeGreaterThan(5);
  });

  it("no production stylesheet sets a text colour to --text-faint", () => {
    const offenders = sheets
      .filter((f) => FAINT_TEXT.test(readFileSync(f, "utf-8")))
      .map((f) => relative(SRC_DIR, f));
    expect(offenders).toEqual([]);
  });

  it("self-test: the pattern bites a planted violation and spares a border use", () => {
    expect(FAINT_TEXT.test("  color: var(--text-faint);")).toBe(true);
    expect(FAINT_TEXT.test("  border-top: 1px solid var(--text-faint);")).toBe(false);
    expect(FAINT_TEXT.test("  color: var(--text-dim);")).toBe(false);
  });
});

describe("contrast guardian — every STATE colour clears AA on every surface", () => {
  it("self-test: the ratio function agrees with two known reference pairs", () => {
    // Black on white is exactly 21:1; a colour against itself is exactly 1:1.
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 1);
    expect(contrastRatio("#5cb4d4", "#5cb4d4")).toBeCloseTo(1, 5);
  });

  it("self-test: it FAILS the values that shipped, so it is not vacuous", () => {
    // The measured defects, re-pointed at the surfaces they would sit on today:
    // the dark affirm word drawn in --cherenkov-deep (a FILL colour) on
    // --panel, the light amber word on --ground, and the pre-round-2 red on
    // --panel. If any of these ever passes, the assertions below have stopped
    // biting. Literal hexes, deliberately: a non-vacuity control read out of
    // tokens.css would move whenever tokens.css moved.
    expect(contrastRatio("#1e6e85", "#1a222d")).toBeLessThan(AA_NORMAL); // 2.77:1
    expect(contrastRatio("#b87413", "#e9edf2")).toBeLessThan(AA_NORMAL); // 3.22:1
    expect(contrastRatio("#e5484d", "#1a222d")).toBeLessThan(AA_NORMAL); // 4.09:1
  });

  for (const [theme, selector] of [
    ["dark", ":root {"],
    ["light", ':root[data-theme="light"] {'],
  ] as const) {
    it(`${theme}: every state colour clears ${AA_NORMAL}:1 on every panel surface`, () => {
      const block = blockFor(selector);
      const failures: string[] = [];
      for (const token of STATE_TOKENS) {
        for (const surface of SURFACES) {
          const ratio = contrastRatio(tokenIn(block, token), tokenIn(block, surface));
          if (ratio < AA_NORMAL) {
            failures.push(`${token} on ${surface}: ${ratio.toFixed(2)}:1`);
          }
        }
      }
      expect(failures).toEqual([]);
    });
  }

  it("dark and light both declare the two state tokens the pill tones resolve to", () => {
    for (const selector of [":root {", ':root[data-theme="light"] {']) {
      const block = blockFor(selector);
      expect(() => tokenIn(block, "--state-affirm")).not.toThrow();
      expect(() => tokenIn(block, "--state-gated")).not.toThrow();
    }
  });
});
