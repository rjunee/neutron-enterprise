// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// AGENTS TOKEN-RESOLUTION GUARDIAN.
//
// The global token-hardcode guardian (src/test/token-hardcode-guardian.test.ts)
// proves no raw colour is written into a component. It cannot prove the token a
// component names actually EXISTS: `color: var(--ne-text-dim)` is a hex-free
// declaration referring to nothing, and CSS resolves an undefined custom
// property to the fallback if one is written and to nothing at all if not.
//
// That is not hypothetical. This interior shipped `var(--ne-border, #d7d7d7)`
// and `var(--ne-text-dim, #666)`. `--ne-border` IS defined (styles.css:13, as
// `var(--rule)`), so those borders resolved correctly in both themes and the
// hex never applied. `--ne-text-dim` is defined NOWHERE — the alias is
// `--ne-muted` — so every dim-text declaration silently fell through to the
// hardcoded #666, which is a dark-mode contrast failure and invisible to a
// guardian that only greps for hexes.
//
// So this guard resolves references against the token layer the app actually
// loads, discovered from main.tsx's own import list rather than a hardcoded
// path, because a definition may be indirect and may live in any of them.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const AGENTS_DIR = dirname(fileURLToPath(import.meta.url));
const SRC_DIR = resolve(AGENTS_DIR, "..");
const MAIN_TSX = resolve(SRC_DIR, "main.tsx");
const AGENTS_CSS = resolve(AGENTS_DIR, "agents.css");

/** The stylesheets `main.tsx` imports, in load order. */
export function loadedStylesheets(mainSource: string): string[] {
  return [...mainSource.matchAll(/^import\s+"(\.[^"]*\.css)";$/gm)].map((m) =>
    resolve(SRC_DIR, m[1]!),
  );
}

/** A stylesheet with its `/* ... *\/` comments removed.
 *
 * VERIFIED GAP IN THIS GUARDIAN, found 2026-08-27. `definedTokens` anchors a
 * definition to the `{` or `;` before it, so a declaration whose preceding
 * sibling is a COMMENT was invisible to it. `--radius` is exactly that case
 * (styles/tokens.css: a two-line comment sits between `:root {` and the
 * declaration), so this guard reported the console's own radius token as
 * undefined and would have pushed a caller onto a different token to get green.
 * The `--radius-lg` on the next line, preceded by `;`, resolved fine — which is
 * why the gap survived: it hides exactly one token per comment block.
 *
 * Stripping comments first is strictly more accurate than widening the anchor,
 * which would have started matching `--name:` inside prose. */
export function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

/** Every custom property DEFINED (`--name:`) in a stylesheet. */
export function definedTokens(css: string): Set<string> {
  return new Set(
    [...stripComments(css).matchAll(/(^|[;{]\s*)(--[A-Za-z0-9_-]+)\s*:/g)].map((m) => m[2]!),
  );
}

/** Every custom property REFERENCED via `var(--name)` in a stylesheet. */
export function referencedTokens(css: string): string[] {
  return [...css.matchAll(/var\(\s*(--[A-Za-z0-9_-]+)/g)].map((m) => m[1]!);
}

const available = new Set<string>();
for (const sheet of loadedStylesheets(readFileSync(MAIN_TSX, "utf-8"))) {
  for (const token of definedTokens(readFileSync(sheet, "utf-8"))) available.add(token);
}
const agentsCss = readFileSync(AGENTS_CSS, "utf-8");
for (const token of definedTokens(agentsCss)) available.add(token);

describe("agents token-resolution guardian", () => {
  it("discovers a real token layer to resolve against", () => {
    // Anti-vacuity: if the discovery above silently found nothing, every
    // reference would resolve against an empty set and the guard below would
    // be satisfiable by any spelling at all.
    const sheets = loadedStylesheets(readFileSync(MAIN_TSX, "utf-8"));
    expect(sheets.length).toBeGreaterThan(1);
    expect(available.size).toBeGreaterThan(20);
    expect(available).toContain("--rule");
    expect(available).toContain("--text-dim");
    // Indirect definitions count: this one is defined as `var(--rule)`.
    expect(available).toContain("--ne-border");
    // A token whose declaration is preceded by a comment rather than by `{` or
    // `;` still counts — the gap this guardian shipped with (see stripComments).
    expect(available).toContain("--radius");
    // And a name nothing defines is genuinely absent, or the set is a sieve.
    expect(available).not.toContain("--ne-text-dim");
  });

  it("self-test: a definition hidden behind a comment is found, and prose is not a definition", () => {
    // The bug, reproduced in miniature: without the strip, the `{`/`;` anchor
    // cannot reach across the comment to `--hidden`.
    expect(definedTokens(":root {\n  /* note */\n  --hidden: 4px;\n}")).toContain("--hidden");
    // …and the strip must not turn commented-out prose into a definition.
    expect(definedTokens(":root {\n  /* --pretend: 1px; */\n}")).not.toContain("--pretend");
  });

  it("finds the references it is meant to check", () => {
    expect(referencedTokens(agentsCss).length).toBeGreaterThan(0);
  });

  it("every token agents.css names is defined in the loaded token layer", () => {
    const unresolved = [...new Set(referencedTokens(agentsCss))]
      .filter((token) => !available.has(token))
      .sort();
    expect(unresolved).toEqual([]);
  });
});
