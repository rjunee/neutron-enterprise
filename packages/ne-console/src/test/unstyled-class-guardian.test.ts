// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE UNSTYLED-CLASS GUARDIAN — a class the markup wears must have a rule.
//
// WHY THIS EXISTS, and it is not hypothetical: it is two defects in one evening.
//
//   1. `.tr-receipt__quiet` and `.tr-receipt__fb` had NO RULE ANYWHERE in the
//      package. Every element wearing them — "How this turn ran" on the
//      workspace, "Open trace", Evidence, Governance — rendered as a raw
//      browser default inside a designed footer. The rule had been written; it
//      was sitting in chat/chat.css under the OTHER surface's name, with a
//      comment reading "(render: .tr-receipt__quiet)". The intent was recorded
//      and never reached the element, and the comment is what made the gap
//      survive review: it reads like the class is handled.
//
//   2. `.ag-card--lead { grid-column: 1 / -1 }` was real, and applied to
//      nothing, because it sat on the <article> INSIDE the grid item while the
//      <li> was the grid item. The lead catalog card silently never spanned.
//
// Both are the same failure — a styling intent that exists and does not reach
// the screen — and neither is visible to the token guardians beside this file,
// which check that a colour resolves, not that a rule is ever matched. Nor to
// jsdom, which performs no layout and no cascade.
//
// WHAT IT CANNOT SEE, stated so nobody trusts it further than it goes. It
// proves a class has A RULE, not that the rule APPLIES: case 2 above would
// still pass here, because `.ag-card--lead` did have a rule. Element-, id- and
// attribute-scoped styling is invisible to it, and so is any class assembled
// entirely out of an interpolation. It is a floor, not a proof.
//
// THE BASELINE IS A RATCHET. Thirty-eight classes were already unstyled when
// this landed; they are listed below rather than silently tolerated, and the
// assertion is EXACT EQUALITY, so the list can only shrink. Fixing one means
// deleting its line. Adding one more fails.
//
// NINE HAVE COME OFF IT ALREADY, which is the mechanism working: `ne-spinner`
// and `ne-sr-only` — the two that were visible defects rather than merely plain
// — plus `ne-btn--danger` and `ne-btn--sm`, where a destructive control looked
// identical to an ordinary one, plus `ne-cap__health` and `ne-evidence__error`,
// plus `ne-run-timeline`, `ne-run-timeline-title` and `ne-config-error`: the
// governed Run's event list drawing with the browser's decimal markers, its
// heading row stacking instead of sitting on one line, and the fail-closed
// bootstrap screen reading as a crash page. TWENTY-NINE remain, and none is
// hidden.
//
// THE COUNT IN THIS PROSE HAD DRIFTED, which is worth a sentence because it is
// the same failure this file exists to catch, one level up: a comment that
// reads like the state of things and is not. It said "seven off, thirty-one
// remain" over a list of twenty-nine, having said "thirty-four" over thirty-two
// before that — the arithmetic was carried forward each time instead of being
// read off the list. The list is the fact; `KNOWN_UNSTYLED.length` is the
// count, and the test below asserts EXACT EQUALITY against it, so the list has
// never been wrong. Only the sentence about it was.

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** Every file under src/ with one of these extensions, minus the two trees that
 * are not this package's own authored markup. */
function filesUnder(dir: string, ext: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "vendor" || entry.name === "node_modules") continue;
      filesUnder(full, ext, out);
      continue;
    }
    // A test file's markup is a fixture, not a screen.
    if (entry.name.endsWith(ext) && !entry.name.includes(".test.")) out.push(full);
  }
  return out;
}

/** Every class name that any selector in any stylesheet MENTIONS. Deliberately
 * loose — `.a .b`, `.a.b`, `.a > .b` all count for both — because the claim
 * being made is only "a rule mentions this class", and a stricter parse would
 * start reporting false defects on descendant and compound selectors. */
export function classesDefinedIn(css: string): Set<string> {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  return new Set([...withoutComments.matchAll(/\.(-?[A-Za-z_][A-Za-z0-9_-]*)/g)].map((m) => m[1]!));
}

/**
 * Every COMPLETE class token a component actually puts on an element.
 *
 * The subtlety is the template literal. `` `tr-step tr-step--${verdict}` ``
 * yields "tr-step", which is a real class, and "tr-step--", which is half of
 * one — reporting the half as unstyled would be a false defect on every
 * modifier in the package. So a token touching an interpolation boundary is
 * dropped: only tokens with whitespace or a quote on both sides are complete.
 */
export function classesUsedIn(source: string): Set<string> {
  const used = new Set<string>();
  const attr = /className=(?:"([^"]*)"|\{`([^`]*)`\})/g;
  for (const match of source.matchAll(attr)) {
    const literal = match[1];
    if (literal !== undefined) {
      for (const token of literal.split(/\s+/).filter(Boolean)) used.add(token);
      continue;
    }
    const segments = match[2]!.split(/\$\{[^}]*\}/);
    segments.forEach((segment, i) => {
      const tokens = segment.split(/\s+/).filter(Boolean);
      const leadTouches = i > 0 && !/^\s/.test(segment);
      const tailTouches = i < segments.length - 1 && !/\s$/.test(segment);
      tokens.forEach((token, j) => {
        if (j === 0 && leadTouches) return;
        if (j === tokens.length - 1 && tailTouches) return;
        used.add(token);
      });
    });
  }
  return used;
}

/**
 * KNOWN UNSTYLED CLASSES — the state of the package when this guard landed.
 *
 * Every one is a real element rendering as a browser default. Some are merely
 * plain (`ne-library__hop`). The ones that were outright defects are fixed and
 * gone from this list — `ne-spinner`, which furnished nothing during the two
 * longest waits in the product, and `ne-sr-only`, which put screen-reader-only
 * text on the screen. What is left is unfixed, not hidden.
 *
 * EXACT EQUALITY BELOW. Delete a line when you style it; the test fails if you
 * do not, and fails if you add one.
 */
const KNOWN_UNSTYLED: readonly string[] = [
  "ne-access__id", // access/AccessPage.tsx
  "ne-callback", // auth/AuthCallback.tsx
  "ne-chamber__nodes", // knowledge/GraphChamber.tsx
  "ne-chamber__relationships", // knowledge/GraphChamber.tsx
  "ne-compare-control__reason", // knowledge/compare/CompareControl.tsx
  "ne-depth", // knowledge/graph/GraphWorkspace.tsx
  "ne-documents__bandpill", // knowledge/documents/DocumentsWorkspace.tsx
  "ne-documents__numeric-preview", // knowledge/documents/DocumentStagePane.tsx
  "ne-egressvalue", // posture/PosturePage.tsx
  "ne-hashplate__end", // primitives/HashPlate.tsx
  "ne-hashplate__full", // primitives/HashPlate.tsx
  "ne-inspect__word", // primitives/InspectSwitch.tsx
  "ne-knowledge__sweep", // knowledge/compare/ComparePane.tsx
  "ne-library__hop", // knowledge/EntityRecord.tsx
  "ne-library__hops", // knowledge/EntityRecord.tsx
  "ne-models__networkaction", // models/ModelsPage.tsx
  "ne-models__networkconfirm", // models/ModelsPage.tsx
  "ne-palette__label", // rail/CommandPalette.tsx
  "ne-refusal__unverified", // primitives/RefusalProof.tsx
  "ne-state--empty", // primitives/states.tsx
  "ne-state--refused", // approvals/DecisionPane.tsx
  "ne-state__action", // primitives/states.tsx
  "ne-strategy__word", // agents/StrategyBadge.tsx
  "ne-th--menu", // primitives/DataTable.tsx
  "ne-titlerow__action", // primitives/PageHeader.tsx
  "ne-topbar__searchtext", // rail/TopBar.tsx
  "ne-tr--skeleton", // primitives/DataTable.tsx
  "ne-tr--state", // primitives/DataTable.tsx
  "ne-verified-queries", // verified-queries/VerifiedQueriesPage.tsx
];

const DEFINED = filesUnder(SRC_DIR, ".css").reduce<Set<string>>((all, file) => {
  for (const name of classesDefinedIn(readFileSync(file, "utf-8"))) all.add(name);
  return all;
}, new Set());

/** Every used class, with the files that use it — so a failure names where. */
const USED = new Map<string, string[]>();
for (const file of filesUnder(SRC_DIR, ".tsx")) {
  for (const name of classesUsedIn(readFileSync(file, "utf-8"))) {
    USED.set(name, [...(USED.get(name) ?? []), relative(SRC_DIR, file)]);
  }
}

describe("every class the markup wears has a rule", () => {
  it("MUTATION `styling-intent-reaches-the-element`: no NEW unstyled class", () => {
    const unstyled = [...USED.keys()].filter((name) => !DEFINED.has(name)).sort();
    // Named, so a failure reads as "this class, used here, has no rule"
    // rather than as a count that moved.
    const withSites = unstyled.map((name) => `${name} (${USED.get(name)![0]})`);
    // ⚠️ A STALE ALLOWLIST ENTRY USED TO CRASH THIS TEST. When a class named
    // here stops appearing in the markup — which is what happens when someone
    // pays the debt off — `USED.get(name)` is undefined and `![0]` threw a
    // TypeError, so the guardian reported "Cannot read properties of undefined"
    // instead of "this entry is stale, delete it". A guard whose failure does
    // not name its own cause costs a debugging round every time it fires, and
    // the message is the whole point of naming the sites below.
    const stale = [...KNOWN_UNSTYLED].filter((name) => !USED.has(name)).sort();
    expect(stale, "these are allowlisted but no longer in the markup — delete them").toEqual([]);
    const expected = [...KNOWN_UNSTYLED].sort().map((name) => `${name} (${USED.get(name)![0]})`);
    expect(withSites).toEqual(expected);
  });

  it("MUTATION `the-prose-count-is-the-list-count`: the header cannot drift again", () => {
    // The header used to say "Thirty-one remain" over a list of twenty-nine,
    // and "Thirty-four" over thirty-two before that: each pass subtracted from
    // the last SENTENCE instead of counting the list. A number in prose that
    // nothing checks is exactly the shape of defect this file exists to catch,
    // so it is checked. Shortening the list now requires updating the sentence,
    // which is the only moment anyone is looking at it.
    const ONES = [
      "zero",
      "one",
      "two",
      "three",
      "four",
      "five",
      "six",
      "seven",
      "eight",
      "nine",
      "ten",
      "eleven",
      "twelve",
      "thirteen",
      "fourteen",
      "fifteen",
      "sixteen",
      "seventeen",
      "eighteen",
      "nineteen",
    ];
    const TENS = ["", "", "twenty", "thirty", "forty", "fifty"];
    const spell = (n: number): string =>
      n < 20 ? ONES[n]! : `${TENS[Math.floor(n / 10)]!}${n % 10 === 0 ? "" : `-${ONES[n % 10]!}`}`;
    expect(spell(29)).toBe("twenty-nine");
    expect(spell(30)).toBe("thirty");
    const header = readFileSync(fileURLToPath(import.meta.url), "utf-8").slice(0, 4000);
    const said = `${spell(KNOWN_UNSTYLED.length)} remain`;
    expect(header.toLowerCase(), `the header must say "${said}"`).toContain(said);
  });

  it("MUTATION `receipt-controls-are-styled`: the two the agents surfaces wear", () => {
    // The subject of the packet, asserted first-class rather than by absence
    // from a list of thirty-eight. Both are used by AgentWorkspace,
    // DeterministicThread and AgentRunPage, and both had no rule at all.
    for (const name of ["tr-receipt__quiet", "tr-receipt__fb"]) {
      expect(USED.has(name)).toBe(true);
      expect(DEFINED.has(name)).toBe(true);
    }
  });

  it("self-test: it detects a class with no rule, and a partial token with one", () => {
    // THE PAIRED HALF of the guard itself. If either half of this stops
    // holding, the assertion above is either blind or crying wolf.
    const used = classesUsedIn('<div className="tr-real tr-invented" />');
    expect(used).toEqual(new Set(["tr-real", "tr-invented"]));
    const defined = classesDefinedIn(".tr-real { color: red } /* .tr-invented */");
    expect(defined.has("tr-real")).toBe(true);
    // A class named only inside a COMMENT is not a rule — which is exactly the
    // shape that hid `.tr-receipt__quiet` in chat/chat.css.
    expect(defined.has("tr-invented")).toBe(false);
    // And a modifier assembled from an interpolation contributes its stem only,
    // never the half-token that would be a false defect.
    const dynamic = classesUsedIn("<li className={`tr-step tr-step--${step.verdict}`} />");
    expect(dynamic).toEqual(new Set(["tr-step"]));
  });
});

/* --------------------------------------------------------------------------
 * THE TWO THAT WERE DEFECTS, NOT DECORATION.
 *
 * These are asserted first-class rather than by absence from the ratchet list,
 * because "has a rule" is a much weaker claim than what each of them needs to
 * be true. Both are paired: the fix, and the thing the fix must not have cost.
 * ------------------------------------------------------------------------ */

/** One rule's declarations, matched at the start of a line so a compound
 * selector cannot answer for the bare one. */
function ruleBody(css: string, selector: string): string {
  // Comments stripped FIRST, on this file's own principle: a class named in
  // prose is not a rule. Without it, "NOT --cherenkov, which the base rule
  // uses" would satisfy an assertion that --cherenkov is absent.
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const hit = new RegExp(`^${escaped}\\s*\\{([^}]*)\\}`, "m").exec(bare);
  if (hit?.[1] === undefined) throw new Error(`no ${selector} rule`);
  return hit[1];
}

const SHELL_CSS = readFileSync(resolve(SRC_DIR, "styles", "shell.css"), "utf-8");
const SYSTEM_CSS = readFileSync(resolve(SRC_DIR, "styles", "system.css"), "utf-8");
const TABLE_CSS = readFileSync(resolve(SRC_DIR, "styles", "table.css"), "utf-8");

describe("screen-reader-only text is not on the screen", () => {
  it("MUTATION `sr-only-is-not-visible`: the clip rectangle exists", () => {
    const body = ruleBody(SYSTEM_CSS, ".ne-sr-only");
    expect(body).toMatch(/position:\s*absolute/);
    expect(body).toMatch(/clip-path:\s*inset\(50%\)/);
    expect(body).toMatch(/width:\s*1px/);
    expect(body).toMatch(/height:\s*1px/);
    // A long announcement must not reflow the page around a 1px column.
    expect(body).toMatch(/white-space:\s*nowrap/);
  });

  it("MUTATION `sr-only-is-still-announced`: it is hidden from eyes, not from AT", () => {
    // THE PAIRED HALF, and the one that matters most. `display: none` and
    // `visibility: hidden` both remove the node from the ACCESSIBILITY TREE as
    // well as the screen — that would fix the visible half by deleting the
    // half that is the entire point. AgentRunPage puts its aria-live run
    // announcements here; an aria-live region nothing can announce is a worse
    // bug than a visible one.
    const body = ruleBody(SYSTEM_CSS, ".ne-sr-only");
    expect(body).not.toMatch(/display:\s*none/);
    expect(body).not.toMatch(/visibility:\s*hidden/);
    expect(body).not.toMatch(/content-visibility/);
  });

  it("MUTATION `sr-only-un-clips-when-focused`: it is not a keyboard trap", () => {
    // The pattern's classic failure: a control the keyboard reader has just
    // focused and cannot see. Nothing wears this interactively today, so this
    // pins the property before something does.
    const focused = ruleBody(SYSTEM_CSS, ".ne-sr-only:focus-visible,\n.ne-sr-only:focus-within");
    expect(focused).toMatch(/clip-path:\s*none/);
    expect(focused).toMatch(/position:\s*static/);
    // Both selectors, so neither can be dropped: `:focus-visible` for the
    // control itself, `:focus-within` for a wrapper that holds one.
    expect(focused).toMatch(/width:\s*auto/);
  });
});

describe("the shell's waiting state is furnished", () => {
  it("MUTATION `loading-renders-something`: it is placed, sized and given a measure", () => {
    const body = ruleBody(SHELL_CSS, ".ne-spinner");
    // A bare line in the top-left corner is what shipped. This is a state, so
    // it occupies the space a state occupies.
    expect(body).toMatch(/min-height:/);
    expect(body).toMatch(/justify-content:\s*center/);
    expect(body).toMatch(/align-items:\s*center/);
    // The skeleton is `display: block` with no width; centred in a column flex
    // it would be zero-wide and invisible again.
    expect(ruleBody(SHELL_CSS, ".ne-spinner__bar")).toMatch(/width:\s*\d/);
  });

  it("MUTATION `loading-respects-reduced-motion`: it borrows the one sanctioned loop", () => {
    // THE PAIRED HALF. Law 4 cl. 1 allows exactly ONE looping selector
    // (motion-guardian's LOOP_ALLOWLIST), and `.ne-skeleton` is it — which is
    // also the only rule already covered by a reduce block. Minting a second
    // breathe here would have been a second thing to remember to silence, and
    // the motion guardian would have failed it. So: this rule animates nothing
    // of its own, and the markup wears the skeleton.
    const body = ruleBody(SHELL_CSS, ".ne-spinner");
    expect(body).not.toMatch(/animation/);
    expect(ruleBody(SHELL_CSS, ".ne-spinner__bar")).not.toMatch(/animation/);
    // The skeleton loops, and its reduce block stops it. Both halves, so this
    // fails if either the loop or its silencing is removed.
    expect(ruleBody(TABLE_CSS, ".ne-skeleton")).toMatch(/animation:.*infinite/);
    expect(TABLE_CSS).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\.ne-skeleton \{\s*animation:\s*none/,
    );
  });
});

describe("a destructive control reads as destructive", () => {
  it("MUTATION `danger-is-distinguishable`: it does not resolve to a plain button", () => {
    const body = ruleBody(TABLE_CSS, ".ne-btn--danger");
    // Border AND colour, so it never depends on hue alone, and the label word
    // ("Cancel Run") carries the meaning regardless.
    expect(body).toMatch(/border-color:\s*var\(--red-line\)/);
    expect(body).toMatch(/color:\s*var\(--red\)/);
    // THE PAIRED HALF: it is a TINT, not a fill. --primary-fill is the
    // single-primary treatment and this is never the primary action on the
    // screen it appears on; two filled buttons is two primaries.
    expect(body).not.toMatch(/--primary-fill/);
    expect(body).not.toMatch(/--cherenkov/);
  });

  it("MUTATION `danger-hover-is-not-selection-blue`: the base hover is overridden", () => {
    // `.ne-btn:hover` borders in --cherenkov. On a destructive control that
    // says "chosen" at the moment it means "about to happen irreversibly".
    const hover = ruleBody(TABLE_CSS, ".ne-btn--danger:hover:not(:disabled)");
    expect(hover).toMatch(/border-color:\s*var\(--red\)/);
    expect(hover).not.toMatch(/--cherenkov/);
  });

  it("MUTATION `a-button-never-reads-as-a-link`: the anchor underline is unset", () => {
    // Roughly half of `.ne-btn`'s callers are <Link>s — "Open workspace" on
    // every catalog card, "Review in Approvals" on a Run's gate — and an
    // anchor underlines itself, so they drew as underlined text inside a
    // bordered button. Caught in a browser, not by a test: jsdom applies no
    // UA stylesheet, so nothing here could have seen it.
    expect(ruleBody(TABLE_CSS, ".ne-btn")).toMatch(/text-decoration:\s*none/);
  });

  it("MUTATION `sm-is-only-a-size`: the compact button keeps every other state", () => {
    // A smaller button is the same button. Anything else here would be a
    // second button quietly diverging from the first.
    const body = ruleBody(TABLE_CSS, ".ne-btn--sm");
    expect(body).toMatch(/height:/);
    expect(body).toMatch(/padding:/);
    expect(body).not.toMatch(/color:/);
    expect(body).not.toMatch(/border-color:/);
    expect(body).not.toMatch(/background:/);
  });
});
