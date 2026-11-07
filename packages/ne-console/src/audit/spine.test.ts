// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — THE LEDGER SPINE: the rules, and the stylesheet that draws them.
//
// Two things are proved here and neither is satisfiable by "a spine element is
// present":
//
//   1. THE RULE IS A FUNCTION OF THE SERVED VERDICT. The same row draws a
//      different mark under a valid verdict and a failing one, the cut lands on
//      a different edge under Seq asc and Seq desc, and a `valid: true` verdict
//      carrying a stale `first_break_seq` draws no break at all. An
//      implementation that drew every row the same way fails all three.
//   2. THE SPINE IS STATIC, AND ITS GAP IS GEOMETRY. Two stylesheet rules, each
//      an exported pure function called BOTH over the shipped audit.css and over
//      planted/legal sources, so the self-test exercises the same code the
//      production assertion does. The static rule bites a transition and an
//      animation on a spine selector; the geometry rule bites a break that is
//      drawn by recolouring the hairline instead of cutting it.
//
// WHAT THIS CANNOT PROVE. jsdom applies no stylesheet (vitest runs with
// `css: false`), so nothing here measures a rendered pixel, a gap width or a
// contrast ratio. The geometry claims are claims about the CSS SOURCE; the
// on-screen result is a manual check.

import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { ChainVerificationView } from "../api/types";
import type { SortState } from "../primitives/tableSort";
import { chainBreakSeq, ledgerSpine } from "./spine";

const AUDIT_DIR = resolve(fileURLToPath(import.meta.url), "..");

const DESC: SortState = { key: "seq", dir: "desc" };
const ASC: SortState = { key: "seq", dir: "asc" };
const BY_ACTOR: SortState = { key: "actor", dir: "asc" };

function verification(over: Partial<ChainVerificationView>): ChainVerificationView {
  return {
    valid: true,
    basis: "full-chain",
    contradiction: false,
    checked: 12480,
    first_break_seq: null,
    first_break_event_id: null,
    reason: null,
    detail: "chain intact",
    ...over,
  };
}

/* ------------------------------- the verdict ------------------------------ */

describe("§5 — the break comes from the served verdict", () => {
  it("a failing verdict yields the seq the server named", () => {
    expect(chainBreakSeq(verification({ valid: false, first_break_seq: 4020 }))).toBe(4020);
  });

  it("a passing verdict yields nothing — even carrying a stale break seq", () => {
    // The console must never draw a gap from a field that only means something
    // under a failing verdict. This is the assertion an implementation that
    // read `first_break_seq` alone would fail.
    expect(chainBreakSeq(verification({ valid: true, first_break_seq: 4020 }))).toBeNull();
  });

  it("an unread window yields nothing", () => {
    expect(chainBreakSeq(null)).toBeNull();
    // Defensive: the server cannot send an invalid verdict with no seq
    // (`_break`, verify.py:180). Pinned because the field is `int | None`.
    expect(chainBreakSeq(verification({ valid: false, first_break_seq: null }))).toBeNull();
  });
});

/* -------------------------------- the rule -------------------------------- */

describe("§5 — what each row draws", () => {
  it("a valid page draws one unbroken chain: every row linked, no cut", () => {
    for (const seq of [4021, 4020, 4019, 4018]) {
      expect(ledgerSpine(seq, null, DESC, false)).toEqual({ tier: "chain", cut: null });
    }
  });

  it("a broken page draws three tiers, in the server's terms", () => {
    expect(ledgerSpine(4021, 4020, DESC, true)).toEqual({ tier: "after", cut: null });
    expect(ledgerSpine(4020, 4020, DESC, true)).toEqual({ tier: "break", cut: "down" });
    expect(ledgerSpine(4019, 4020, DESC, true)).toEqual({ tier: "chain", cut: null });
    expect(ledgerSpine(4018, 4020, DESC, true)).toEqual({ tier: "chain", cut: null });
  });

  it("the cut lands on the edge facing the last VERIFIED entry, in either direction", () => {
    // Seq desc renders the older neighbour below the break row; seq asc renders
    // it above. A hardcoded side is wrong in exactly one of these.
    expect(ledgerSpine(4020, 4020, DESC, true)?.cut).toBe("down");
    expect(ledgerSpine(4020, 4020, ASC, true)?.cut).toBe("up");
  });

  it("the SAME row draws differently under a valid and a failing verdict", () => {
    // The bidirectional core: a spine that always drew the same thing would
    // satisfy either assertion alone and fails this one.
    expect(ledgerSpine(4020, null, DESC, false)).not.toEqual(ledgerSpine(4020, 4020, DESC, true));
    expect(ledgerSpine(4021, null, DESC, false)).not.toEqual(ledgerSpine(4021, 4020, DESC, true));
    // …and the rows the server DID verify are untouched by the break above them.
    expect(ledgerSpine(4019, null, DESC, false)).toEqual(ledgerSpine(4019, 4020, DESC, true));
  });

  it("exactly one row in a page carries the cut", () => {
    const page = [4022, 4021, 4020, 4019, 4018];
    const cuts = page
      .map((seq) => ledgerSpine(seq, 4020, DESC, true)?.cut)
      .filter((c) => c !== null);
    expect(cuts).toEqual(["down"]);
  });

  it("a break outside the rendered window draws no cut and no dimmed tail", () => {
    // The verdict still says BREAK — at an entry this window does not contain.
    // Every row here is one the server verified, so the chain is whole on screen
    // and the strip carries the fact. Inventing a gap would be inventing a
    // location for a break the server put somewhere else.
    const page = [4021, 4020, 4019];
    expect(page.map((seq) => ledgerSpine(seq, 9001, DESC, true))).toEqual([
      { tier: "chain", cut: null },
      { tier: "chain", cut: null },
      { tier: "chain", cut: null },
    ]);
  });

  it("a STANDING break the server never located draws no chain at all", () => {
    // THE WORST PICTURE THIS MODULE CAN DRAW — and a DEFENSIVE case, not one
    // the server reaches. An earlier revision cited "the race's own answer
    // names NO ENTRY" as reproduced fact; `_break` (verify.py:180) takes
    // `seq: int`, so an invalid verdict always names an entry and this input
    // does not arrive today. It is pinned because `breakSeq` is `number | null`
    // in `ledgerSpine`'s own signature: were a seq-less break to stand, every
    // row below it would draw a full-strength hairline and node — a whole,
    // verified chain, underneath the word BREAK. Nothing here can be vouched
    // for and nothing can be pointed at, so nothing is drawn.
    for (const seq of [4021, 4020, 4019]) {
      expect(ledgerSpine(seq, null, DESC, true)).toBeNull();
      expect(ledgerSpine(seq, null, ASC, true)).toBeNull();
    }
    // The control, and the whole reason `broken` is a second input: the SAME
    // null seq with no break standing is a chain the server verified whole.
    expect(ledgerSpine(4020, null, DESC, false)).toEqual({ tier: "chain", cut: null });
  });

  it("no spine at all when the rendered order is not the chain's order", () => {
    expect(ledgerSpine(4020, 4020, BY_ACTOR, true)).toBeNull();
    expect(ledgerSpine(4019, null, BY_ACTOR, false)).toBeNull();
    expect(ledgerSpine(4019, null, { key: "outcome", dir: "desc" }, false)).toBeNull();
    expect(ledgerSpine(4019, null, null, false)).toBeNull();
    // The control that must stay green: seq order in BOTH directions draws.
    expect(ledgerSpine(4019, null, DESC, false)).not.toBeNull();
    expect(ledgerSpine(4019, null, ASC, false)).not.toBeNull();
  });
});

/* --------------------------- the stylesheet rules -------------------------- */

type Source = { path: string; text: string };
type CssRule = { selector: string; body: string };

/** Comments are exempt: the stylesheet's own prose says the word "animation" in
 * order to state that there is none. */
function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

/** Selector + body for every brace-free rule. Written for this file's two
 * rules only — narrow on purpose, so it is read rather than trusted. */
function cssRules(css: string): CssRule[] {
  const out: CssRule[] = [];
  const re = /([^{}]*)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css)) !== null) {
    const selector = (m[1] ?? "").trim();
    if (selector === "" || selector.startsWith("@")) continue;
    out.push({ selector, body: m[2] ?? "" });
  }
  return out;
}

function properties(body: string): string[] {
  return body
    .split(";")
    .map((decl) => decl.slice(0, decl.indexOf(":")).trim().toLowerCase())
    .filter((prop) => /^[a-z-]+$/.test(prop));
}

const mentionsSpine = (selector: string) => /spine/i.test(selector);
const mentionsCut = (selector: string) => /spine--cut|spinecut/i.test(selector);

/** Every rule that draws a spine part carries exactly the geometry it declares.
 * Not a timing, not a duration, not a keyframe: §5 is the STATIC layer, and a
 * signal with nothing to collapse cannot lose anything under reduced motion. */
export function ruleSpineIsStatic(sources: Source[]): string[] {
  const out: string[] = [];
  for (const source of sources) {
    for (const rule of cssRules(source.text)) {
      if (!mentionsSpine(rule.selector)) continue;
      for (const prop of properties(rule.body)) {
        if (/^(?:animation|transition)/.test(prop)) {
          out.push(`${source.path} :: ${rule.selector} :: \`${prop}\``);
        }
      }
    }
    for (const m of source.text.matchAll(/@keyframes\s+([A-Za-z0-9_-]*spine[A-Za-z0-9_-]*)/gi)) {
      out.push(`${source.path} :: @keyframes ${m[1] ?? ""}`);
    }
  }
  return out;
}

/** The break is a CUT, and a cut is geometry. A rule that marks the severance
 * has to move an edge or hold a size — a rule that only changes a colour would
 * make the one fact this screen exists to show perceptible by hue alone. */
export function ruleCutIsGeometric(sources: Source[]): string[] {
  const geometry = /^(?:top|right|bottom|left|inset|width|height|margin|padding)/;
  const out: string[] = [];
  for (const source of sources) {
    for (const rule of cssRules(source.text)) {
      if (!mentionsCut(rule.selector)) continue;
      if (!properties(rule.body).some((prop) => geometry.test(prop))) {
        out.push(`${source.path} :: ${rule.selector} :: marks the break without cutting anything`);
      }
    }
  }
  return out;
}

const AUDIT_CSS: Source[] = [
  {
    path: "audit/audit.css",
    text: stripComments(readFileSync(join(AUDIT_DIR, "audit.css"), "utf-8")),
  },
];

const planted = (text: string): Source[] => [
  { path: "audit/planted.css", text: stripComments(text) },
];

describe("§5 — the spine's stylesheet", () => {
  it("finds the spine rules it audits (non-vacuity)", () => {
    // Both rules below are filters over these. If the class names move and the
    // filters go empty, the rules would pass while examining nothing.
    const rules = AUDIT_CSS.flatMap((s) => cssRules(s.text));
    expect(rules.filter((r) => mentionsSpine(r.selector)).length).toBeGreaterThanOrEqual(6);
    expect(rules.filter((r) => mentionsCut(r.selector)).length).toBeGreaterThanOrEqual(4);
  });

  it("the shipped spine declares no animation, transition or keyframe", () => {
    expect(ruleSpineIsStatic(AUDIT_CSS)).toEqual([]);
  });

  it("self-test: the static rule bites motion on a spine rule and spares its geometry", () => {
    expect(
      ruleSpineIsStatic(
        planted(".ne-audit__spineline { transition: opacity var(--motion-snap); }"),
      ),
    ).toHaveLength(1);
    expect(
      ruleSpineIsStatic(
        planted(
          ".ne-audit__spinecut { animation: ne-sweep var(--motion-pulse) var(--ease-damped); }",
        ),
      ),
    ).toHaveLength(1);
    // §6.2's demolished verification sweep, by name.
    expect(
      ruleSpineIsStatic(planted("@keyframes ne-spine-sweep { from { opacity: 0; } }")),
    ).toHaveLength(1);
    // Controls that must pass: the spine's real declarations, and a transition
    // on something that is not the spine (this rule is scoped, not global — the
    // motion guardian owns the product-wide property law).
    expect(
      ruleSpineIsStatic(
        planted(
          ".ne-audit__spine { position: absolute; width: 12px; }\n.ne-audit__spinenode { background: var(--rule-strong); transform: translate(-50%, -50%); }",
        ),
      ),
    ).toEqual([]);
    expect(
      ruleSpineIsStatic(planted(".ne-audit__band { transition: opacity var(--motion-snap); }")),
    ).toEqual([]);
  });

  it("the shipped break is cut into the hairline, not painted onto it", () => {
    expect(ruleCutIsGeometric(AUDIT_CSS)).toEqual([]);
  });

  it("self-test: the geometry rule bites a recoloured break and spares a real gap", () => {
    // The mutant this exists for: the spine kept whole and the break "shown" by
    // turning the hairline red. Legible to nobody who cannot see red, and gone
    // in a greyscale print of the page.
    expect(
      ruleCutIsGeometric(
        planted(".ne-audit__spine--cut-down .ne-audit__spineline { background: var(--red); }"),
      ),
    ).toHaveLength(1);
    expect(
      ruleCutIsGeometric(planted(".ne-audit__spinecut { background: var(--red); }")),
    ).toHaveLength(1);
    expect(
      ruleCutIsGeometric(
        planted(
          ".ne-audit__spine--cut-down .ne-audit__spineline { bottom: 6px; }\n.ne-audit__spinecut { height: 2px; background: var(--red); }",
        ),
      ),
    ).toEqual([]);
  });

  it("nothing under src/audit reads a motion preference", () => {
    // The spine is identical under `prefers-reduced-motion: reduce` because no
    // code branches on it: there is one rendering, and it is the static one.
    // Comments are exempt — the source says the words in order to state that it
    // does not consult them — so they are stripped first, exactly as the motion
    // guardian strips them.
    for (const file of ["spine.ts", "LedgerSpine.tsx", "AuditPage.tsx", "audit.css"]) {
      const raw = readFileSync(join(AUDIT_DIR, file), "utf-8");
      const code = stripComments(raw).replace(/(^|[^:])\/\/.*$/gm, "$1");
      expect(code, file).not.toContain("matchMedia");
      expect(code, file).not.toContain("prefers-reduced-motion");
      // …and the strip did not eat the file.
      expect(code.length, file).toBeGreaterThan(200);
    }
  });
});
