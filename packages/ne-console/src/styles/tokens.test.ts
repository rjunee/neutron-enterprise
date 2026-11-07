// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Structural proof of the token layer (design §1.1/§1.2/§1.3). jsdom cannot
// exercise the real CSS cascade / media queries, so these are fs-reads of the
// committed tokens.css + primitives.css — the EXACT §1.1 values for BOTH themes,
// data-theme precedence in BOTH directions, the type/geometry/spacing/motion
// tokens, and the law-4 reduced-motion collapse.
//
// The frozen tables below are the round-3 approved render
// (design/round3/screens/home.html), which is the specification. Drifting any
// value in tokens.css without changing this file fails the suite.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const HERE = resolve(__dirname);
const tokensCss = readFileSync(resolve(HERE, "tokens.css"), "utf-8");
const primitivesCss = readFileSync(resolve(HERE, "primitives.css"), "utf-8");

/** Extract the body of a CSS rule whose selector chunk matches `needle`. */
function blockAfter(css: string, needle: string): string {
  const idx = css.indexOf(needle);
  expect(idx, `selector ${needle} present`).toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", idx);
  const close = css.indexOf("}", open);
  return css.slice(open, close);
}

const DARK: Record<string, string> = {
  // Elevation ramp — five steps, sunken through overlay.
  "--sunken": "#070b10",
  "--ground": "#10161e",
  "--panel": "#1a222d",
  "--panel-raised": "#232e3b",
  "--panel-overlay": "#2c3947",
  "--rule": "#2f3b4a",
  "--rule-strong": "#465569",
  "--text": "#eaf0f6",
  "--text-dim": "#a3b4c5",
  "--text-faint": "#68798d",
  "--cherenkov": "#43d9ff",
  "--cherenkov-deep": "#1e6e85",
  "--amber": "#f0a52e",
  "--red": "#ff7a80",
  "--green": "#42c795",
  "--glow": "rgba(67, 217, 255, 0.14)",
  // The filled-action pair: --cherenkov is tuned for text and hairlines, so a
  // fill carries its own colour and its own label colour.
  "--primary-fill": "#35c1e6",
  "--primary-label": "#04141c",
  // State vocabulary. test/contrast-guardian.test.ts is the test that holds
  // these honest — it MEASURES them against every surface they can sit on.
  "--state-affirm": "#5cb4d4",
  "--state-gated": "#a0adbb",
  // Interaction + status washes.
  "--hover-tint": "rgba(234, 240, 246, 0.05)",
  "--selected-tint": "rgba(67, 217, 255, 0.09)",
  "--red-tint": "rgba(255, 122, 128, 0.1)",
  "--amber-tint": "rgba(240, 165, 46, 0.12)",
  "--green-tint": "rgba(66, 199, 149, 0.12)",
  "--affirm-tint": "rgba(92, 180, 212, 0.12)",
  "--gated-tint": "rgba(160, 173, 187, 0.1)",
  "--dim-tint": "rgba(163, 180, 197, 0.1)",
  // Edge strengths of the two governance hues: a promoted gate card's border,
  // at half alpha so the edge never outshouts the state word inside it.
  "--amber-line": "rgba(240, 165, 46, 0.5)",
  "--red-line": "rgba(255, 122, 128, 0.5)",
  // Elevation: catchlight + the three depths + the recessed inverse.
  "--edge-highlight": "rgba(234, 240, 246, 0.07)",
  "--shadow-card": "0 1px 2px rgba(2, 5, 9, 0.5), 0 4px 16px rgba(2, 5, 9, 0.28)",
  "--shadow-pane": "-12px 0 32px rgba(2, 5, 9, 0.45), -1px 0 0 rgba(234, 240, 246, 0.04)",
  "--shadow-overlay": "0 2px 4px rgba(2, 5, 9, 0.5), 0 16px 40px rgba(2, 5, 9, 0.55)",
  "--well-inset": "inset 0 1px 3px rgba(2, 5, 9, 0.45)",
  "--scrim": "rgba(16, 22, 30, 0.85)",
};

const LIGHT: Record<string, string> = {
  "--sunken": "#dde3ea",
  "--ground": "#e9edf2",
  "--panel": "#ffffff",
  "--panel-raised": "#f3f6f9",
  "--panel-overlay": "#ffffff",
  "--rule": "#d2dae2",
  "--rule-strong": "#aab8c6",
  "--text": "#152029",
  "--text-dim": "#4c5e6e",
  "--text-faint": "#8595a5",
  // The light ramp is measured against --panel and --panel-raised, not only
  // against --ground; see the MEASURED assertions in contrast-guardian.
  "--cherenkov": "#08628a",
  "--cherenkov-deep": "#0a5c78",
  "--amber": "#8f5407",
  "--red": "#b3262d",
  "--green": "#177753",
  "--glow": "rgba(8, 98, 138, 0.14)",
  "--primary-fill": "#0b6a8b",
  "--primary-label": "#ffffff",
  "--state-affirm": "#08628a",
  "--state-gated": "#495764",
  "--hover-tint": "rgba(21, 32, 41, 0.045)",
  "--selected-tint": "rgba(8, 98, 138, 0.07)",
  "--red-tint": "rgba(179, 38, 45, 0.07)",
  "--amber-tint": "rgba(143, 84, 7, 0.09)",
  "--green-tint": "rgba(23, 119, 83, 0.09)",
  "--affirm-tint": "rgba(8, 98, 138, 0.08)",
  "--gated-tint": "rgba(73, 87, 100, 0.07)",
  "--dim-tint": "rgba(76, 94, 110, 0.07)",
  "--amber-line": "rgba(143, 84, 7, 0.45)",
  "--red-line": "rgba(179, 38, 45, 0.45)",
  // Light has no catchlight: a lifted surface reads by shadow alone, so the
  // token is declared fully transparent rather than dropped (every consumer
  // keeps one code path across both themes).
  "--edge-highlight": "rgba(255, 255, 255, 0)",
  "--shadow-card": "0 1px 2px rgba(21, 32, 41, 0.07), 0 4px 14px rgba(21, 32, 41, 0.05)",
  "--shadow-pane": "-12px 0 32px rgba(21, 32, 41, 0.1)",
  "--shadow-overlay": "0 2px 4px rgba(21, 32, 41, 0.08), 0 16px 40px rgba(21, 32, 41, 0.14)",
  "--well-inset": "inset 0 1px 2px rgba(21, 32, 41, 0.06)",
  "--scrim": "rgba(233, 237, 242, 0.85)",
};

/** The §1.2 type scale and §1.4 geometry, frozen exactly as declared. */
const TYPE_AND_GEOMETRY = [
  '--font-ui: "Source Sans 3", system-ui, sans-serif;',
  '--font-mono: "Fira Mono", ui-monospace, SFMono-Regular, monospace;',
  "--stretch-signage: normal;",
  "--type-title: 600 22px/28px var(--font-ui);",
  "--type-section: 600 16px/24px var(--font-ui);",
  "--type-lead: 400 15px/24px var(--font-ui);",
  "--type-body: 400 14px/22px var(--font-ui);",
  "--type-small: 400 12.5px/18px var(--font-ui);",
  "--type-label: 600 11px/16px var(--font-ui);",
  "--type-mono: 400 13px/20px var(--font-mono);",
  "--type-mono-plate: 700 15px/20px var(--font-mono);",
  "--type-display: 700 clamp(40px, 5vw, 56px) / 1.06 var(--font-ui);",
  "--type-figure: 700 36px/40px var(--font-mono);",
  "--type-cell: 400 13.5px/20px var(--font-ui);",
  "--type-cell-mono: 400 13px/20px var(--font-mono);",
  "--type-nav: 400 13.5px/20px var(--font-ui);",
  "--tracking-display: -0.01em;",
  "--tracking-label: 0.07em;",
  "--radius: 4px;",
  "--radius-lg: 8px;",
  "--rail-width: 248px;",
  "--rail-width-collapsed: 56px;",
  "--topbar-height: 52px;",
  "--titlerow-height: 40px;",
  "--toolbar-height: 40px;",
  "--nav-row-height: 32px;",
  "--detail-width: 392px;",
  "--palette-width: 560px;",
  "--row-height: 40px;",
  "--space-4: 4px;",
  "--space-8: 8px;",
  "--space-12: 12px;",
  "--space-16: 16px;",
  "--space-24: 24px;",
  "--space-32: 32px;",
  "--space-48: 48px;",
  "--space-64: 64px;",
  "--ease-damped: cubic-bezier(0.2, 0, 0, 1);",
  "--motion-snap: 90ms;",
  "--motion-rod: 180ms;",
  "--motion-settle: 200ms;",
  "--motion-chain: 60ms;",
  "--motion-drum: 40ms;",
  "--motion-breathe: 4s;",
  "--motion-pulse: 400ms;",
  "--motion-transit: 260ms;",
];

describe("tokens.css — Cold Criticality palette (§1.1)", () => {
  it("declares every dark palette token with EXACT values under :root", () => {
    const rootBlock = blockAfter(tokensCss, ":root {");
    for (const [token, value] of Object.entries(DARK)) {
      expect(rootBlock, `${token} dark`).toContain(`${token}: ${value};`);
    }
  });

  it("declares the brand mark ONCE, never re-themed", () => {
    // A mark that recolours with the surrounding UI is a context-tinted glyph,
    // not a mark. Its invariance is structural, not conventional.
    expect(tokensCss.match(/--brand-teal:/g)?.length).toBe(1);
    expect(blockAfter(tokensCss, ":root {")).toContain("--brand-teal: #6fe3d4;");
  });

  it("declares the light theme under BOTH the prefers-color-scheme media block and :root[data-theme=light]", () => {
    // (b) media block scoped so data-theme=dark keeps dark on an OS-light display.
    expect(tokensCss).toContain("@media (prefers-color-scheme: light)");
    expect(tokensCss).toContain(':root:not([data-theme="dark"])');
    // Search for the selector followed by its opening brace so the doc-comment
    // occurrences of these selectors are skipped.
    const mediaBlock = blockAfter(tokensCss, ':root:not([data-theme="dark"]) {');
    // (c) explicit override wins over an OS-dark preference.
    const explicitBlock = blockAfter(tokensCss, ':root[data-theme="light"] {');
    for (const [token, value] of Object.entries(LIGHT)) {
      expect(mediaBlock, `${token} media-light`).toContain(`${token}: ${value};`);
      expect(explicitBlock, `${token} explicit-light`).toContain(`${token}: ${value};`);
    }
  });

  it("themes every palette token in both directions (no dark value leaks into light)", () => {
    // Every dark token except the brand mark must have a light counterpart in
    // BOTH light blocks — a token declared only in :root silently keeps its
    // dark value on a light display.
    const mediaBlock = blockAfter(tokensCss, ':root:not([data-theme="dark"]) {');
    const explicitBlock = blockAfter(tokensCss, ':root[data-theme="light"] {');
    for (const token of Object.keys(DARK)) {
      expect(Object.keys(LIGHT), `${token} has a light value`).toContain(token);
      expect(mediaBlock, `${token} redeclared in media-light`).toContain(`${token}:`);
      expect(explicitBlock, `${token} redeclared in explicit-light`).toContain(`${token}:`);
    }
  });

  it("carries the type/geometry/spacing/motion/font tokens with EXACT values", () => {
    for (const decl of TYPE_AND_GEOMETRY) {
      expect(tokensCss, decl).toContain(decl);
    }
  });

  it("names only families that fonts.css actually declares", () => {
    // A token naming a face no @font-face declares does not error — it silently
    // renders in the fallback stack. So this checks the CLAIM (every quoted
    // family in a --font-* stack is a declared @font-face) rather than the
    // spelling of any one retired name. The third signage stack is gone with it:
    // the round-3 title rung is set in --font-ui.
    const fontsCss = readFileSync(resolve(HERE, "fonts.css"), "utf-8");
    const declared = new Set([...fontsCss.matchAll(/font-family:\s*"([^"]+)"/g)].map((m) => m[1]));
    expect(declared).toEqual(new Set(["Source Sans 3", "Fira Mono"]));

    const stacks = [...tokensCss.matchAll(/--font-[a-z-]+:\s*([^;]+);/g)].map((m) => m[1] ?? "");
    expect(stacks.length, "tokens.css declares font stacks").toBe(2);
    for (const stack of stacks) {
      for (const quoted of [...stack.matchAll(/"([^"]+)"/g)].map((m) => m[1]!)) {
        expect(declared, `${quoted} has an @font-face`).toContain(quoted);
      }
    }
    expect(tokensCss).not.toContain("--font-signage");
    expect(tokensCss).toContain("--type-title: 600 22px/28px var(--font-ui);");
  });

  it("scales the comfortable density above the compact default", () => {
    const comfortable = blockAfter(tokensCss, ':root[data-density="comfortable"] {');
    expect(comfortable).toContain("--row-height: 48px;");
  });
});

describe("primitives.css — law-4 reduced-motion collapse (§0)", () => {
  it("forces all animation/transition durations to ~instant under prefers-reduced-motion", () => {
    expect(primitivesCss).toContain("@media (prefers-reduced-motion: reduce)");
    const block = blockAfter(primitivesCss, "prefers-reduced-motion: reduce");
    expect(block).toContain("animation-duration: 0.01ms !important");
    expect(block).toContain("transition-duration: 0.01ms !important");
  });
});
