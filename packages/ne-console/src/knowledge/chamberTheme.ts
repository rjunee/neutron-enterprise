// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The ONLY bridge between the NE-130 token layer and the <canvas>. Canvas has no
// CSS cascade, so we resolve the design tokens at DRAW TIME via
// getComputedStyle(document.documentElement) and hand the raw values to the
// draw functions. This is why the chamber follows the light/dark theme (and any
// future palette edit) automatically with ZERO color literals in TS — design
// law 1 (chroma is evidence) + the token-hardcode guardian. Luminance grading is
// done with ctx.globalAlpha (see drawChamber.ts), never color math, so no raw
// colour-function or hex literal ever appears here.

/** The resolved token values the chamber paints with (all are CSS color strings
 * the browser produced from tokens.css — never authored here). */
export interface ChamberTheme {
  ground: string;
  panel: string;
  cherenkov: string;
  cherenkovDeep: string;
  text: string;
  textDim: string;
  rule: string;
  typeCell: string;
  typeSmall: string;
}

/** Read the live token values off :root. In jsdom (no layout engine) these come
 * back empty; empty strings are harmless (assigning "" to a canvas style is a
 * no-op that keeps the prior value), so the null-ctx-guarded renderer degrades
 * cleanly under test. */
export function readChamberTheme(): ChamberTheme {
  const root = typeof document !== "undefined" ? document.documentElement : undefined;
  const cs = root ? getComputedStyle(root) : undefined;
  const tok = (name: string): string => (cs ? cs.getPropertyValue(name).trim() : "");
  return {
    ground: tok("--ground"),
    panel: tok("--panel"),
    cherenkov: tok("--cherenkov"),
    cherenkovDeep: tok("--cherenkov-deep"),
    text: tok("--text"),
    textDim: tok("--text-dim"),
    rule: tok("--rule"),
    typeCell: tok("--type-cell"),
    typeSmall: tok("--type-small"),
  };
}
