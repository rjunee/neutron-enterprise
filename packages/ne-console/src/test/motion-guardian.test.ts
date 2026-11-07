// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// MOTION GUARDIAN — law 4 ("motion is evidence", styles/primitives.css; design
// docs/design/ne-console-motion-design.md §2) made machine-checkable.
//
// The law was RIGHT and unenforceable: prose that review checked the code
// against. The utility that sat directly beneath it declared `transition: all`
// for three months. Each rule below therefore (a) fs-walks the production
// source through the SAME pure function its self-test calls, and (b) is paired
// with a bidirectional self-test that proves the scan BITES a planted violation
// and SPARES a legitimate one. A rule that examines nothing passes forever and
// reads as coverage; the non-vacuity test below asserts the walk actually finds
// stylesheets, keyframes and timing declarations to audit.
//
// WHAT THIS CANNOT PROVE. jsdom does not run animations. Every rule here is
// STRUCTURAL — which properties may animate, that nothing loops, that no
// duration escapes the token scale, that no application state waits on an
// animation event. None of it measures a frame rate, and a green run must never
// be reported as one. The 60fps claim is a manual demo-rig check (design §7).

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");

export type Source = { path: string; text: string };
type Decl = { prop: string; value: string };
type Rule = { selector: string; body: string };
type Keyframes = { name: string; body: string };

/* ------------------------------------------------------------------ walk -- */

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "test") continue;
      out.push(...walk(full));
      continue;
    }
    if (/\.test\.[jt]sx?$/.test(entry)) continue;
    // `.js` is INCLUDED: src/vendor/d3/**/*.js is live precedent for .js under
    // src, and a walk that filters it out is an extension-shaped bypass slot.
    if (/\.(m?[jt]sx?|css)$/.test(entry)) out.push(full);
  }
  return out;
}

/** Comments are EXEMPT from every rule: law 4 itself quotes `transition: all`
 * and `infinite` in order to forbid them, and must not trip its own guardian. */
function stripCssComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "");
}
function stripScriptComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const isCss = (path: string) => path.endsWith(".css");

const SOURCES: Source[] = walk(SRC_DIR).map((full) => {
  const path = relative(SRC_DIR, full).replaceAll("\\", "/");
  const raw = readFileSync(full, "utf-8");
  return { path, text: isCss(path) ? stripCssComments(raw) : stripScriptComments(raw) };
});

const STYLESHEETS = SOURCES.filter((s) => isCss(s.path));
const SCRIPTS = SOURCES.filter((s) => !isCss(s.path));

/* ----------------------------------------------------------------- parse -- */

/** Every declaration in a stylesheet, at any nesting depth. Selector chunks
 * (`:root`, `a:hover`, `*::after`, `@media (…: light)`, `0%, 100%`) fail the
 * property shape and are skipped. */
export function declarations(css: string): Decl[] {
  const out: Decl[] = [];
  for (const chunk of css.split(/[;{}]/)) {
    const i = chunk.indexOf(":");
    if (i < 0) continue;
    const prop = chunk.slice(0, i).trim().toLowerCase();
    if (!/^-{0,2}[a-z][a-z0-9-]*$/.test(prop)) continue;
    out.push({ prop, value: chunk.slice(i + 1).trim() });
  }
  return out;
}

/** Split a stylesheet into its `@keyframes` blocks and everything else. */
export function splitKeyframes(css: string): { keyframes: Keyframes[]; rest: string } {
  const keyframes: Keyframes[] = [];
  const re = /@keyframes\s+([A-Za-z0-9_-]+)\s*\{/g;
  let rest = "";
  let cut = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css)) !== null) {
    rest += css.slice(cut, m.index);
    let depth = 1;
    let j = re.lastIndex;
    while (j < css.length && depth > 0) {
      const ch = css[j];
      if (ch === "{") depth += 1;
      else if (ch === "}") depth -= 1;
      j += 1;
    }
    keyframes.push({
      name: m[1] ?? "",
      body: css.slice(re.lastIndex, Math.max(j - 1, re.lastIndex)),
    });
    cut = j;
    re.lastIndex = j;
  }
  rest += css.slice(cut);
  return { keyframes, rest };
}

/** Selector + body for every non-keyframes rule. An at-rule wrapper such as
 * `@media (…) {` never matches (its body is not brace-free), so the rules it
 * contains surface with their own selectors — which is what the allowlists
 * below need. */
export function cssRules(css: string): Rule[] {
  const { rest } = splitKeyframes(css);
  const out: Rule[] = [];
  const re = /([^{}]*)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(rest)) !== null) {
    const selector = (m[1] ?? "").trim();
    if (!selector || selector.startsWith("@")) continue;
    out.push({ selector, body: m[2] ?? "" });
  }
  return out;
}

function stripVars(value: string): string {
  let out = value;
  let prev = "";
  while (out !== prev) {
    prev = out;
    out = out.replace(/var\(\s*--[a-z0-9-]+\s*(?:,[^()]*)?\)/gi, " ");
  }
  return out;
}

/** Token values with `var()` and timing functions removed, so a comma split
 * yields transition segments rather than `cubic-bezier` arguments. */
function normalizeValue(value: string): string {
  return stripVars(value).replace(/(?:cubic-bezier|steps|calc)\([^()]*\)/gi, " ");
}

const TIMING_KEYWORDS = new Set([
  "linear",
  "ease",
  "ease-in",
  "ease-out",
  "ease-in-out",
  "step-start",
  "step-end",
  "both",
  "forwards",
  "backwards",
  "infinite",
  "alternate",
  "normal",
  "reverse",
  "running",
  "paused",
]);

/** The property each segment of a `transition` / `transition-property` value
 * actually transitions. */
export function transitionProps(value: string): string[] {
  const out: string[] = [];
  for (const seg of normalizeValue(value).split(",")) {
    for (const token of seg.trim().split(/\s+/)) {
      const t = token.toLowerCase();
      if (!t || /^[\d.]/.test(t) || TIMING_KEYWORDS.has(t)) continue;
      out.push(t);
      break;
    }
  }
  return out;
}

/** Split a raw declaration value on TOP-LEVEL commas only, so the commas inside
 * `var(--x, fallback)` and `cubic-bezier(…)` do not shear a segment in two. */
export function splitTopLevel(value: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let cur = "";
  for (const ch of value) {
    if (ch === "(") depth += 1;
    else if (ch === ")") depth = Math.max(0, depth - 1);
    if (ch === "," && depth === 0) {
      out.push(cur);
      cur = "";
      continue;
    }
    cur += ch;
  }
  out.push(cur);
  return out;
}

/** True when some comma-segment of a `transition` SHORTHAND value carries no
 * property token, so `transition-property` silently defaults to `all` — the
 * defect cl. 3 bans by name, in its most natural spelling. `transition: none`
 * keeps its `none` token and stays legal. */
export function hasPropertylessSegment(value: string): boolean {
  return normalizeValue(value)
    .split(",")
    .some((seg) => {
      const tokens = seg.trim().split(/\s+/).filter(Boolean);
      return !tokens.some((t) => !/^[\d.]/.test(t) && !TIMING_KEYWORDS.has(t.toLowerCase()));
    });
}

/** A curve is PRESENT in a shorthand segment when one is actually named: the
 * token, a literal easing function, or a keyword. Absence hands the browser
 * its default `ease` — a second curve nobody wrote (cl. 4). */
const CURVE_PRESENT =
  /var\(\s*--ease-|\b(?:cubic-bezier|steps|linear)\s*\(|\b(?:ease-in-out|ease-in|ease-out|ease|linear|step-start|step-end)(?![\w(-])/i;

/** A duration is present: a `--motion-*` token or a raw ms/s literal. A
 * duration-free shorthand (`transition: none`, `animation: none`) animates
 * nothing, so it owes no curve. */
const DURATION_PRESENT = /var\(\s*--motion-|(?:^|[^\w.-])\d*\.?\d+\s*m?s(?![\w-])/i;

/** Timing smuggled into a NON-vocabulary custom property (`--sneaky: 500ms
 * ease-in-out`): a raw duration or a curve, under a prefix rules 7/8 cannot
 * see through `var()`. `linear-gradient(…)` is not `linear` the curve. */
const SMUGGLED_TIMING =
  /(?:^|[^\w.-])\d*\.?\d+\s*m?s(?![\w-])|\b(?:cubic-bezier|steps|linear)\s*\(|\b(?:ease-in-out|ease-in|ease-out|ease|linear|step-start|step-end)(?![\w(-])/i;

/* ----------------------------------------------------------- vocabularies -- */

/** Animating any of these is a defect BY NAME (law 4 cl. 3): each one forces
 * layout on the main thread every frame. */
const LAYOUT_PROPS = new Set([
  "width",
  "min-width",
  "max-width",
  "height",
  "min-height",
  "max-height",
  "top",
  "right",
  "bottom",
  "left",
  "inset",
  "inset-block",
  "inset-inline",
  "margin",
  "margin-top",
  "margin-right",
  "margin-bottom",
  "margin-left",
  "margin-block",
  "margin-inline",
  "padding",
  "padding-top",
  "padding-right",
  "padding-bottom",
  "padding-left",
  "padding-block",
  "padding-inline",
  "font-size",
  "line-height",
  "letter-spacing",
  "word-spacing",
  "flex",
  "flex-basis",
  "flex-grow",
  "flex-shrink",
  "grid-template-columns",
  "grid-template-rows",
  "grid-auto-rows",
  "grid-auto-columns",
  "gap",
  "row-gap",
  "column-gap",
  "border-width",
  "border-top-width",
  "border-right-width",
  "border-bottom-width",
  "border-left-width",
]);

/** Law 4 cl. 3: continuous / enter / exit motion is transform+opacity only. */
const COMPOSITOR_PROPS = new Set(["transform", "opacity", "none"]);

/** Law 4 cl. 1: the loading skeleton is the ONE selector allowed to loop, and
 * the one allowed a non-damped (uniform) curve, because a breathe has no
 * arrival to damp. Loading is a state, not rest. */
const LOOP_ALLOWLIST = new Set([".ne-skeleton"]);

const TIMING_DECLS = new Set([
  "animation",
  "animation-duration",
  "animation-delay",
  "transition",
  "transition-duration",
  "transition-delay",
]);

/** The reduced-motion collapse (law 4 cl. 6) is the ONE raw duration in the
 * product: it exists to make everything instant. Exempted by exact value, not
 * by file, so nothing else can hide behind it. */
const COLLAPSE_VALUE = /^0\.01ms\s*!important$/;

const TOKENS_CSS = "styles/tokens.css";

/* ----------------------------------------------------------------- rules -- */

/** (1) cl. 3 — `transition: all` is a defect by name, and so is its implicit
 * spelling: a shorthand segment with NO property token, which CSS defaults to
 * `transition-property: all`. */
export function ruleTransitionAll(sources: Source[]): string[] {
  const out: string[] = [];
  for (const s of sources) {
    if (isCss(s.path)) {
      for (const d of declarations(s.text)) {
        if (d.prop !== "transition" && d.prop !== "transition-property") continue;
        if (transitionProps(d.value).includes("all")) {
          out.push(`${s.path} :: ${d.prop}: ${d.value}`);
          continue;
        }
        if (d.prop === "transition" && hasPropertylessSegment(d.value)) {
          out.push(`${s.path} :: ${d.prop}: ${d.value} — no property token, defaults to \`all\``);
        }
      }
      continue;
    }
    const re = /\btransition(Property)?\s*:\s*(["'`])([^"'`]*)\2/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(s.text)) !== null) {
      const value = m[3] ?? "";
      if (transitionProps(value).includes("all")) {
        out.push(`${s.path} :: transition: ${value}`);
        continue;
      }
      if (!m[1] && hasPropertylessSegment(value)) {
        out.push(`${s.path} :: transition: ${value} — no property token, defaults to \`all\``);
      }
    }
  }
  return out;
}

/** (2) cl. 3 — no layout property is transitioned or keyframed, anywhere. */
export function ruleNoLayoutAnimation(sources: Source[]): string[] {
  const out: string[] = [];
  for (const s of sources.filter((x) => isCss(x.path))) {
    const { keyframes, rest } = splitKeyframes(s.text);
    for (const d of declarations(rest)) {
      if (d.prop !== "transition" && d.prop !== "transition-property") continue;
      for (const p of transitionProps(d.value)) {
        if (LAYOUT_PROPS.has(p)) out.push(`${s.path} :: transition of layout property \`${p}\``);
      }
    }
    for (const k of keyframes) {
      for (const d of declarations(k.body)) {
        if (LAYOUT_PROPS.has(d.prop)) {
          out.push(`${s.path} :: @keyframes ${k.name} animates layout property \`${d.prop}\``);
        }
      }
    }
  }
  return out;
}

/** (3) cl. 3 — transitions carry transform/opacity only. Paint changes are
 * one-shot keyframes on a single element, never a transition. */
export function ruleTransitionsAreCompositorOnly(sources: Source[]): string[] {
  const out: string[] = [];
  for (const s of sources.filter((x) => isCss(x.path))) {
    for (const d of declarations(s.text)) {
      if (d.prop !== "transition" && d.prop !== "transition-property") continue;
      for (const p of transitionProps(d.value)) {
        if (!COMPOSITOR_PROPS.has(p)) out.push(`${s.path} :: transition of \`${p}\``);
      }
    }
  }
  return out;
}

/** (4) cl. 1 — `infinite` only on the allowlisted loading skeleton, and a
 * finite iteration count over 1 is still a loop: `animation-iteration-count:
 * 9999` is `infinite` minus the word. Nothing continuous on a screen at rest. */
export function ruleInfiniteAllowlist(sources: Source[]): string[] {
  const out: string[] = [];
  for (const s of sources.filter((x) => isCss(x.path))) {
    for (const r of cssRules(s.text)) {
      for (const d of declarations(r.body)) {
        if (d.prop !== "animation" && d.prop !== "animation-iteration-count") continue;
        if (LOOP_ALLOWLIST.has(r.selector)) continue;
        if (/\binfinite\b/i.test(d.value)) {
          out.push(`${s.path} :: ${r.selector} loops forever`);
          continue;
        }
        if (d.prop === "animation-iteration-count") {
          const n = parseFloat(stripVars(d.value));
          if (Number.isFinite(n) && n > 1) out.push(`${s.path} :: ${r.selector} loops ${n}×`);
          continue;
        }
        // Shorthand: after vars and easing functions are stripped, a bare
        // number token can only be an iteration count.
        for (const token of normalizeValue(d.value).split(/\s+/)) {
          if (/^\d*\.?\d+$/.test(token) && parseFloat(token) > 1) {
            out.push(`${s.path} :: ${r.selector} loops ${token}×`);
          }
        }
      }
    }
  }
  return out;
}

/** (5) cl. 1 + cl. 3 — whatever IS allowed to loop must be compositor-only:
 * a looping paint animation is a per-frame repaint on an idle screen. */
export function ruleLoopingAnimationIsCompositorOnly(sources: Source[]): string[] {
  const out: string[] = [];
  for (const s of sources.filter((x) => isCss(x.path))) {
    const { keyframes } = splitKeyframes(s.text);
    const byName = new Map(keyframes.map((k) => [k.name, k]));
    for (const r of cssRules(s.text)) {
      for (const d of declarations(r.body)) {
        if (d.prop !== "animation" || !/\binfinite\b/i.test(d.value)) continue;
        const named = normalizeValue(d.value)
          .split(/\s+/)
          .map((t) => byName.get(t))
          .filter((k): k is Keyframes => k !== undefined);
        if (named.length === 0) {
          out.push(`${s.path} :: ${r.selector} loops an unresolvable @keyframes`);
          continue;
        }
        for (const k of named) {
          for (const decl of declarations(k.body)) {
            if (decl.prop.startsWith("--")) continue;
            if (!COMPOSITOR_PROPS.has(decl.prop)) {
              out.push(`${s.path} :: looping @keyframes ${k.name} paints \`${decl.prop}\``);
            }
          }
        }
      }
    }
  }
  return out;
}

/** (6) cl. 7 — no application state gates on an animation event. NO exempt
 * files: the one permitted wait (holding a node until its own animations
 * finish before removal) is event-free — `getAnimations()` `finished`
 * promises, primitives/surface.ts — so an `animationend`/`transitionend`
 * handler has no legitimate spelling anywhere. The rule is case-insensitive:
 * React's `onAnimationEnd` and the DOM's `el.onanimationend` are one hole. */
export function ruleNoAnimationGating(sources: Source[]): string[] {
  const out: string[] = [];
  const handler = /\bon(?:animation|transition)end\b|["'`](?:animation|transition)end["'`]/i;
  for (const s of sources.filter((x) => !isCss(x.path))) {
    const m = s.text.match(handler);
    if (m) out.push(`${s.path} :: ${m[0]}`);
  }
  return out;
}

/** (7) cl. 5 — every duration resolves through a `--motion-*` token. A raw
 * `ms`/`s` in an animation or transition is a component-local constant, which
 * is a parallel motion vocabulary (design §6.12). */
export function ruleNoRawDurations(sources: Source[]): string[] {
  const out: string[] = [];
  for (const s of sources.filter((x) => isCss(x.path) && x.path !== TOKENS_CSS)) {
    for (const d of declarations(s.text)) {
      if (!TIMING_DECLS.has(d.prop)) continue;
      if (COLLAPSE_VALUE.test(d.value)) continue;
      // The RAW value is scanned, not the var()-stripped one: a raw duration
      // hiding in a fallback (`var(--motion-nope, 300ms)`) is still a raw
      // duration — stripping vars first deleted exactly the evidence.
      if (/(?:^|[^\w.-])\d*\.?\d+\s*m?s(?![\w-])/i.test(d.value)) {
        out.push(`${s.path} :: ${d.prop}: ${d.value}`);
      }
    }
  }
  return out;
}

/** (8) cl. 4 — `var(--ease-damped)` is the only curve in the product. A second
 * curve (and every spring/bounce/overshoot) is a defect; `linear` survives only
 * on the allowlisted skeleton breathe, which has no arrival to damp. The
 * implicit spelling is a defect too: a shorthand that carries a duration but
 * NO curve token runs on the browser default `ease` — a second curve nobody
 * wrote. So is a curve re-declared inside a `@keyframes` step, which cssRules
 * never surfaces as a rule. */
export function ruleOneCurve(sources: Source[]): string[] {
  const out: string[] = [];
  const banned = /\b(cubic-bezier|steps|ease-in-out|ease-out|ease-in|ease)\b/i;
  for (const s of sources.filter((x) => isCss(x.path) && x.path !== TOKENS_CSS)) {
    for (const r of cssRules(s.text)) {
      for (const d of declarations(r.body)) {
        if (!/^(?:animation|transition)(?:-timing-function)?$/.test(d.prop)) continue;
        const v = stripVars(d.value);
        const hit = v.match(banned);
        if (hit) out.push(`${s.path} :: ${r.selector} :: second curve \`${hit[0]}\``);
        if (/\blinear\b/i.test(v) && !LOOP_ALLOWLIST.has(r.selector)) {
          out.push(`${s.path} :: ${r.selector} :: second curve \`linear\``);
        }
        if (d.prop === "animation" || d.prop === "transition") {
          for (const seg of splitTopLevel(d.value)) {
            if (DURATION_PRESENT.test(seg) && !CURVE_PRESENT.test(seg)) {
              out.push(
                `${s.path} :: ${r.selector} :: no curve token in \`${d.prop}: ${d.value}\` — the browser default \`ease\` is a second curve`,
              );
            }
          }
        }
      }
    }
    for (const k of splitKeyframes(s.text).keyframes) {
      for (const d of declarations(k.body)) {
        if (d.prop !== "animation-timing-function") continue;
        const v = stripVars(d.value);
        const hit = v.match(banned);
        if (hit) {
          out.push(`${s.path} :: @keyframes ${k.name} :: second curve \`${hit[0]}\` in a step`);
        }
        if (/\blinear\b/i.test(v)) {
          out.push(`${s.path} :: @keyframes ${k.name} :: second curve \`linear\` in a step`);
        }
      }
    }
  }
  return out;
}

/** (9) cl. 5 — the motion vocabulary is declared ONCE, in tokens.css. No
 * component mints `--motion-*` / `--ease-*`, no OTHER custom property smuggles
 * a duration or curve in under a prefix the vocabulary rules cannot see
 * through `var()`, and no timing is authored in JS/TSX — neither as a style
 * string, nor written onto `el.style` at runtime, nor through the Web
 * Animations API, all of which escape the token layer entirely. */
export function ruleSingleVocabulary(sources: Source[]): string[] {
  const out: string[] = [];
  const jsTiming =
    /\b(?:transition|animation)(?:Property|Duration|Delay|Name|TimingFunction|IterationCount)?\s*:\s*["'`]/g;
  const styleWrite =
    /\.style\.(?:transition|animation)[A-Za-z]*\s*=|\.setProperty\s*\(\s*["'`](?:transition|animation)/g;
  const waapi = /\.animate\s*\(|\bnew\s+(?:Animation|KeyframeEffect)\s*\(/g;
  for (const s of sources) {
    if (isCss(s.path)) {
      if (s.path === TOKENS_CSS) continue;
      for (const d of declarations(s.text)) {
        if (/^--(?:motion|ease)-/.test(d.prop)) {
          out.push(`${s.path} :: mints \`${d.prop}\``);
          continue;
        }
        if (d.prop.startsWith("--") && SMUGGLED_TIMING.test(d.value)) {
          out.push(`${s.path} :: \`${d.prop}: ${d.value}\` smuggles timing past the vocabulary`);
        }
      }
      continue;
    }
    for (const re of [jsTiming, styleWrite, waapi]) {
      re.lastIndex = 0;
      let m: RegExpExecArray | null;
      while ((m = re.exec(s.text)) !== null) {
        out.push(`${s.path} :: timing authored in JS \`${m[0].trim()}\``);
      }
    }
  }
  return out;
}

/** (10) cl. 6 — the reduced-motion collapse exists STRUCTURALLY: inside the
 * `prefers-reduced-motion: reduce` media block of styles/primitives.css, on
 * the universal selector `*, *::before, *::after`, carrying all three collapse
 * declarations. Presence of the substrings is not enough — rescoping the
 * selector to a class nobody carries keeps every substring and kills the
 * collapse (V4: presence without structure). */
export function ruleReducedMotionCollapse(sources: Source[]): string[] {
  const path = "styles/primitives.css";
  const sheet = sources.find((s) => s.path === path);
  if (!sheet) return [`${path} :: missing from the walk`];
  const m = /@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{/.exec(sheet.text);
  if (!m) return [`${path} :: no @media (prefers-reduced-motion: reduce) block`];
  let depth = 1;
  let j = m.index + m[0].length;
  const start = j;
  while (j < sheet.text.length && depth > 0) {
    const ch = sheet.text[j];
    if (ch === "{") depth += 1;
    else if (ch === "}") depth -= 1;
    j += 1;
  }
  const body = sheet.text.slice(start, Math.max(j - 1, start));
  const universal = cssRules(body).find(
    (r) => r.selector.replace(/\s+/g, "") === "*,*::before,*::after",
  );
  if (!universal) {
    return [`${path} :: collapse is not on the universal selector \`*, *::before, *::after\``];
  }
  const out: string[] = [];
  const decls = declarations(universal.body);
  const REQUIRED: ReadonlyArray<readonly [string, RegExp]> = [
    ["animation-duration", /^0\.01ms\s*!important$/],
    ["animation-iteration-count", /^1\s*!important$/],
    ["transition-duration", /^0\.01ms\s*!important$/],
  ];
  for (const [prop, shape] of REQUIRED) {
    if (!decls.some((d) => d.prop === prop && shape.test(d.value))) {
      out.push(`${path} :: universal collapse missing \`${prop}\``);
    }
  }
  return out;
}

/* ------------------------------------------------------------------ tests -- */

const MOTION_TOKENS: Record<string, string> = {
  "--ease-damped": "cubic-bezier(0.2, 0, 0, 1)",
  "--motion-drum": "40ms",
  "--motion-chain": "60ms",
  "--motion-snap": "90ms",
  "--motion-rod": "180ms",
  "--motion-settle": "200ms",
  "--motion-transit": "260ms",
  "--motion-pulse": "400ms",
  "--motion-breathe": "4s",
};

const css = (path: string, text: string): Source[] => [{ path, text: stripCssComments(text) }];
const tsx = (path: string, text: string): Source[] => [{ path, text: stripScriptComments(text) }];

describe("motion guardian — law 4 (styles/primitives.css)", () => {
  it("finds the style layer it audits (non-vacuity)", () => {
    // Every rule below is a filter over these collections. If the walk goes
    // empty — a moved directory, a renamed extension — the rules would all pass
    // while examining nothing, which is the failure mode this test exists for.
    expect(STYLESHEETS.length).toBeGreaterThan(15);
    expect(SCRIPTS.length).toBeGreaterThan(50);

    // The `.js` extension hole is closed: src/vendor/d3/**/*.js is live
    // precedent for .js under src, and the walk must reach it.
    expect(SCRIPTS.some((s) => s.path.endsWith(".js"))).toBe(true);

    const keyframes = STYLESHEETS.flatMap((s) => splitKeyframes(s.text).keyframes);
    expect(keyframes.length).toBeGreaterThanOrEqual(3);

    const timing = STYLESHEETS.flatMap((s) =>
      declarations(s.text).filter((d) => TIMING_DECLS.has(d.prop)),
    );
    expect(timing.length).toBeGreaterThanOrEqual(8);

    // Exactly one rule in the product loops, and it is the loading skeleton.
    const looping = STYLESHEETS.flatMap((s) =>
      cssRules(s.text)
        .filter((r) => declarations(r.body).some((d) => /\binfinite\b/.test(d.value)))
        .map((r) => r.selector),
    );
    expect(looping).toEqual([".ne-skeleton"]);
  });

  it("(1) cl. 3 — no `transition: all` survives anywhere", () => {
    expect(ruleTransitionAll(SOURCES)).toEqual([]);
    // The named defect, by name: `.ne-motion-settle` was deleted, and the class
    // may not return in any file, comment-stripped or not.
    expect(SOURCES.filter((s) => s.text.includes("ne-motion-settle")).map((s) => s.path)).toEqual(
      [],
    );
  });

  it("(1b) self-test: the `transition: all` scan bites and spares", () => {
    expect(
      ruleTransitionAll(css("styles/planted.css", ".x { transition: all var(--motion-snap); }")),
    ).toHaveLength(1);
    expect(
      ruleTransitionAll(css("styles/planted.css", ".x { transition-property: all; }")),
    ).toHaveLength(1);
    // The JS shape — an inline style object — is a different syntax and the
    // rule must reach it too.
    expect(
      ruleTransitionAll(tsx("x/Planted.tsx", 'style={{ transition: "all 200ms" }}')),
    ).toHaveLength(1);
    // The IMPLICIT spelling — a shorthand with no property token — defaults
    // `transition-property` to `all`. This is the escape a contributor writes
    // first, and it must go RED by the same rule, in both syntaxes.
    expect(
      ruleTransitionAll(
        css("styles/planted.css", ".x { transition: var(--motion-snap) var(--ease-damped); }"),
      ),
    ).toHaveLength(1);
    expect(ruleTransitionAll(tsx("x/Planted.tsx", 'style={{ transition: "0.2s" }}'))).toHaveLength(
      1,
    );
    // Spares the legal shape, and spares a property token that merely
    // CONTAINS "all" (exact element match, not substring — V1).
    expect(
      ruleTransitionAll(
        css(
          "styles/legal.css",
          ".x { transition: transform var(--motion-snap) var(--ease-damped); }",
        ),
      ),
    ).toEqual([]);
    expect(
      ruleTransitionAll(css("styles/legal.css", ".x { transition-property: install; }")),
    ).toEqual([]);
    expect(
      ruleTransitionAll(css("styles/legal.css", ".x { transition: opacity var(--motion-snap); }")),
    ).toEqual([]);
    // Controls a naive "every segment needs a property token" version would
    // flag: `none` IS the property token, in CSS and in JS alike.
    expect(ruleTransitionAll(css("styles/legal.css", ".x { transition: none; }"))).toEqual([]);
    expect(ruleTransitionAll(tsx("x/Legal.tsx", 'style={{ transition: "none" }}'))).toEqual([]);
  });

  it("(2) cl. 3 — no layout property is transitioned or keyframed", () => {
    expect(ruleNoLayoutAnimation(SOURCES)).toEqual([]);
  });

  it("(2b) self-test: the layout scan bites both shapes and spares transform", () => {
    expect(
      ruleNoLayoutAnimation(
        css("styles/planted.css", ".x { transition: width var(--motion-rod); }"),
      ),
    ).toHaveLength(1);
    // The shape that actually ships as "animate a row in": a height keyframe.
    expect(
      ruleNoLayoutAnimation(
        css(
          "styles/planted.css",
          "@keyframes ne-grow { from { height: 0; } to { height: 36px; } }",
        ),
      ),
    ).toHaveLength(2);
    expect(
      ruleNoLayoutAnimation(
        css(
          "styles/legal.css",
          "@keyframes ne-rise { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }\n.x { transition: transform var(--motion-rod) var(--ease-damped); }",
        ),
      ),
    ).toEqual([]);
    // A control that must pass: STATIC layout declarations are the whole
    // stylesheet. Only transitions and keyframes are in scope.
    expect(
      ruleNoLayoutAnimation(
        css("styles/legal.css", ".x { width: 240px; padding: 8px; gap: 4px; }"),
      ),
    ).toEqual([]);
  });

  it("(3) cl. 3 — transitions carry transform/opacity only", () => {
    expect(ruleTransitionsAreCompositorOnly(SOURCES)).toEqual([]);
  });

  it("(3b) self-test: the compositor scan bites paint and spares transform/opacity", () => {
    expect(
      ruleTransitionsAreCompositorOnly(
        css("styles/planted.css", ".x { transition: background-color var(--motion-snap); }"),
      ),
    ).toHaveLength(1);
    expect(
      ruleTransitionsAreCompositorOnly(
        css("styles/planted.css", ".x { transition: box-shadow var(--motion-pulse); }"),
      ),
    ).toHaveLength(1);
    expect(
      ruleTransitionsAreCompositorOnly(
        css(
          "styles/legal.css",
          ".x { transition: transform var(--motion-drum) var(--ease-damped); }",
        ),
      ),
    ).toEqual([]);
    expect(
      ruleTransitionsAreCompositorOnly(
        css(
          "styles/legal.css",
          ".x { transition-property: transform, opacity; }\n.y { transition: none; }",
        ),
      ),
    ).toEqual([]);
  });

  it("(4) cl. 1 — `infinite` appears only on the loading skeleton", () => {
    expect(ruleInfiniteAllowlist(SOURCES)).toEqual([]);
  });

  it("(4b) self-test: the loop scan bites off-allowlist loops and spares the skeleton", () => {
    expect(
      ruleInfiniteAllowlist(
        css(
          "styles/planted.css",
          ".ne-pending { animation: ne-x var(--motion-breathe) linear infinite; }",
        ),
      ),
    ).toHaveLength(1);
    expect(
      ruleInfiniteAllowlist(
        css("styles/planted.css", ".ne-dot { animation-iteration-count: infinite; }"),
      ),
    ).toHaveLength(1);
    // `infinite` minus the word: a finite count over 1 is still a loop, in
    // the longhand and buried in the shorthand.
    expect(
      ruleInfiniteAllowlist(
        css("styles/planted.css", ".ne-dot { animation-iteration-count: 9999; }"),
      ),
    ).toHaveLength(1);
    expect(
      ruleInfiniteAllowlist(
        css("styles/planted.css", ".ne-dot { animation: ne-x var(--motion-pulse) 3; }"),
      ),
    ).toHaveLength(1);
    // Controls a naive "any numeric iteration count" version would flag: a
    // one-shot count of 1, spelled out or defaulted.
    expect(
      ruleInfiniteAllowlist(css("styles/legal.css", ".x { animation-iteration-count: 1; }")),
    ).toEqual([]);
    expect(
      ruleInfiniteAllowlist(
        css("styles/legal.css", ".x { animation: ne-x var(--motion-pulse) var(--ease-damped); }"),
      ),
    ).toEqual([]);
    expect(
      ruleInfiniteAllowlist(
        css(
          "styles/legal.css",
          ".ne-skeleton { animation: ne-shimmer var(--motion-breathe) linear infinite; }",
        ),
      ),
    ).toEqual([]);
    // The global reduced-motion collapse sets iteration-count to 1 — it is the
    // opposite of a loop and must not be mistaken for one.
    expect(
      ruleInfiniteAllowlist(
        css(
          "styles/legal.css",
          "@media (prefers-reduced-motion: reduce) { * { animation-iteration-count: 1 !important; } }",
        ),
      ),
    ).toEqual([]);
  });

  it("(5) cl. 1+3 — the one looping animation is compositor-only", () => {
    expect(ruleLoopingAnimationIsCompositorOnly(SOURCES)).toEqual([]);
  });

  it("(5b) self-test: the looping scan resolves keyframes, bites paint, spares opacity", () => {
    const paint =
      ".ne-skeleton { animation: ne-glow var(--motion-breathe) linear infinite; }\n@keyframes ne-glow { 0% { background-color: var(--panel); } 100% { background-color: var(--panel-raised); } }";
    expect(ruleLoopingAnimationIsCompositorOnly(css("styles/planted.css", paint))).toHaveLength(2);
    const legal =
      ".ne-skeleton { animation: ne-shimmer var(--motion-breathe) linear infinite; }\n@keyframes ne-shimmer { 0%, 100% { opacity: 0.5; } 50% { opacity: 1; } }";
    expect(ruleLoopingAnimationIsCompositorOnly(css("styles/legal.css", legal))).toEqual([]);
    // A loop whose keyframes cannot be resolved is also a failure: the rule must
    // never pass by silently finding nothing to inspect.
    expect(
      ruleLoopingAnimationIsCompositorOnly(
        css(
          "styles/planted.css",
          ".ne-skeleton { animation: ne-absent var(--motion-breathe) linear infinite; }",
        ),
      ),
    ).toHaveLength(1);
  });

  it("(6) cl. 7 — no application state gates on an animation event", () => {
    expect(ruleNoAnimationGating(SOURCES)).toEqual([]);
  });

  it("(6b) self-test: the gating scan bites every handler shape, exempting nothing", () => {
    const planted = "onAnimationEnd={() => setDecided(true)}";
    expect(ruleNoAnimationGating(tsx("approvals/ApprovalsPage.tsx", planted))).toHaveLength(1);
    expect(
      ruleNoAnimationGating(
        tsx("audit/AuditPage.tsx", 'el.addEventListener("transitionend", flush);'),
      ),
    ).toHaveLength(1);
    // The lowercase DOM property is the same hole in a third spelling.
    expect(
      ruleNoAnimationGating(tsx("audit/AuditPage.tsx", "el.onanimationend = flush;")),
    ).toHaveLength(1);
    // There is NO exempt file. A pre-authorized bypass slot once existed here
    // (`primitives/oneShot.ts`, exempted by path while the file did not
    // exist); creating that file with gating in it must be RED like any other.
    expect(ruleNoAnimationGating(tsx("primitives/oneShot.ts", planted))).toHaveLength(1);
    // The REAL permitted pattern needs no exemption because it uses no event:
    // primitives/surface.ts holds the node on its `getAnimations()` `finished`
    // promises, and the scan — run over the actual walked source — spares it.
    const surface = SOURCES.find((s) => s.path === "primitives/surface.ts");
    expect(surface, "primitives/surface.ts is walked").toBeDefined();
    expect(surface?.text).toContain("getAnimations");
    expect(ruleNoAnimationGating([surface!])).toEqual([]);
    // Controls that must pass: a comment naming the ban, and ordinary state.
    expect(
      ruleNoAnimationGating(
        tsx("audit/AuditPage.tsx", "// never gate on animationend\nsetRows(next);"),
      ),
    ).toEqual([]);
    expect(
      ruleNoAnimationGating(tsx("audit/AuditPage.tsx", "useEffect(() => setRows(next), [next]);")),
    ).toEqual([]);
  });

  it("(7) cl. 5 — every duration resolves through a --motion-* token", () => {
    expect(ruleNoRawDurations(SOURCES)).toEqual([]);
  });

  it("(7b) self-test: the raw-duration scan bites shorthands, delays and the collapse's lookalikes", () => {
    expect(
      ruleNoRawDurations(
        css("styles/planted.css", ".x { animation: ne-x 300ms var(--ease-damped); }"),
      ),
    ).toHaveLength(1);
    expect(
      ruleNoRawDurations(
        css("styles/planted.css", ".x { transition: opacity 0.2s var(--ease-damped); }"),
      ),
    ).toHaveLength(1);
    // The exact shape deleted from primitives.css with the dead annunciator wake.
    expect(
      ruleNoRawDurations(
        css("styles/planted.css", ".x { animation-delay: calc(var(--i, 0) * 80ms); }"),
      ),
    ).toHaveLength(1);
    // A raw duration hiding in a var() FALLBACK is still a raw duration — the
    // scan reads the raw value, so stripping vars cannot delete the evidence.
    expect(
      ruleNoRawDurations(
        css(
          "styles/planted.css",
          ".x { transition: opacity var(--motion-nope, 300ms) var(--ease-damped); }",
        ),
      ),
    ).toHaveLength(1);
    // Control a naive "any fallback is raw" version would flag: a duration-free
    // nested-var fallback carries no duration literal.
    expect(
      ruleNoRawDurations(
        css(
          "styles/legal.css",
          ".x { transition: opacity var(--motion-snap, var(--motion-rod)) var(--ease-damped); }",
        ),
      ),
    ).toEqual([]);
    expect(
      ruleNoRawDurations(
        css(
          "styles/legal.css",
          ".x { animation: ne-shimmer var(--motion-breathe) linear infinite; }",
        ),
      ),
    ).toEqual([]);
    expect(
      ruleNoRawDurations(
        css(
          "styles/legal.css",
          ".x { transition: transform var(--motion-snap) var(--ease-damped); }",
        ),
      ),
    ).toEqual([]);
    // The reduced-motion collapse is exempt by EXACT value — a different raw
    // duration in the same block is not.
    expect(
      ruleNoRawDurations(css("styles/legal.css", "* { animation-duration: 0.01ms !important; }")),
    ).toEqual([]);
    expect(
      ruleNoRawDurations(css("styles/planted.css", "* { animation-duration: 120ms !important; }")),
    ).toHaveLength(1);
  });

  it("(8) cl. 4 — --ease-damped is the only curve in the product", () => {
    expect(ruleOneCurve(SOURCES)).toEqual([]);
  });

  it("(8b) self-test: the curve scan bites literals and off-allowlist linear, spares the token", () => {
    // The bounce this bans by construction: an overshooting cubic-bezier.
    expect(
      ruleOneCurve(
        css(
          "styles/planted.css",
          ".x { transition: transform var(--motion-rod) cubic-bezier(0.34, 1.56, 0.64, 1); }",
        ),
      ),
    ).toHaveLength(1);
    expect(
      ruleOneCurve(
        css("styles/planted.css", ".x { transition: opacity var(--motion-snap) ease-in-out; }"),
      ),
    ).toHaveLength(1);
    expect(
      ruleOneCurve(
        css("styles/planted.css", ".x { transition: opacity var(--motion-snap) linear; }"),
      ),
    ).toHaveLength(1);
    // The IMPLICIT second curve: a shorthand with a duration and no curve
    // token runs on the browser default `ease`. The natural spelling, RED.
    expect(
      ruleOneCurve(css("styles/planted.css", ".x { animation: ne-x var(--motion-settle); }")),
    ).toHaveLength(1);
    expect(
      ruleOneCurve(css("styles/planted.css", ".x { transition: opacity var(--motion-snap); }")),
    ).toHaveLength(1);
    // …including one curve-less segment hiding behind a legal one.
    expect(
      ruleOneCurve(
        css(
          "styles/planted.css",
          ".x { transition: transform var(--motion-rod) var(--ease-damped), opacity var(--motion-snap); }",
        ),
      ),
    ).toHaveLength(1);
    // A curve re-declared inside a @keyframes step, which cssRules never
    // surfaces — the per-step re-easing hole.
    expect(
      ruleOneCurve(
        css(
          "styles/planted.css",
          "@keyframes ne-x { from { opacity: 0; animation-timing-function: ease-in; } to { opacity: 1; } }",
        ),
      ),
    ).toHaveLength(1);
    expect(
      ruleOneCurve(
        css(
          "styles/legal.css",
          ".x { transition: transform var(--motion-rod) var(--ease-damped); }",
        ),
      ),
    ).toEqual([]);
    // Controls a naive "no curve literal in the segment → flag" version would
    // trip on: the curve arriving via the token, a `none` shorthand (nothing
    // animates, no curve owed), longhand pairs, and the damped token in a step.
    expect(
      ruleOneCurve(css("styles/legal.css", ".x { transition: none; }\n.y { animation: none; }")),
    ).toEqual([]);
    expect(
      ruleOneCurve(
        css(
          "styles/legal.css",
          ".x { transition-property: opacity; transition-duration: var(--motion-snap); transition-timing-function: var(--ease-damped); }",
        ),
      ),
    ).toEqual([]);
    expect(
      ruleOneCurve(
        css(
          "styles/legal.css",
          "@keyframes ne-y { from { animation-timing-function: var(--ease-damped); } }",
        ),
      ),
    ).toEqual([]);
    // `linear` survives on the skeleton breathe only. Note the token name
    // itself contains "ease-" and must not read as a second curve.
    expect(
      ruleOneCurve(
        css(
          "styles/legal.css",
          ".ne-skeleton { animation: ne-shimmer var(--motion-breathe) linear infinite; }",
        ),
      ),
    ).toEqual([]);
  });

  it("(9) cl. 5 — the motion vocabulary is declared once, in tokens.css", () => {
    expect(ruleSingleVocabulary(SOURCES)).toEqual([]);

    const tokens = STYLESHEETS.find((s) => s.path === TOKENS_CSS);
    expect(tokens, "styles/tokens.css is walked").toBeDefined();
    const declared = new Map(
      declarations(tokens?.text ?? "")
        .filter((d) => /^--(?:motion|ease)-/.test(d.prop))
        .map((d) => [d.prop, d.value]),
    );
    // The scale is EXACTLY the design's — no more (a parallel vocabulary) and
    // no fewer (an effect reaching outside it).
    expect([...declared.keys()].sort()).toEqual(Object.keys(MOTION_TOKENS).sort());
    for (const [token, value] of Object.entries(MOTION_TOKENS)) {
      expect(declared.get(token), token).toBe(value);
    }
  });

  it("(9b) self-test: the vocabulary scan bites a minted token and JS timing, spares consumption", () => {
    expect(
      ruleSingleVocabulary(css("audit/audit.css", ".x { --motion-swoosh: 700ms; }")),
    ).toHaveLength(1);
    expect(
      ruleSingleVocabulary(tsx("audit/AuditPage.tsx", 'style={{ animation: "ne-x 300ms" }}')),
    ).toHaveLength(1);
    // Smuggling: a custom property OUTSIDE the --motion-/--ease- prefixes
    // holding a duration or a curve is the same parallel vocabulary under a
    // name the prefix check cannot see.
    expect(
      ruleSingleVocabulary(css("audit/audit.css", ".x { --sneaky-wait: 500ms ease-in-out; }")),
    ).toHaveLength(1);
    // Runtime style writes and the Web Animations API author timing where no
    // CSS rule can ever see it.
    expect(
      ruleSingleVocabulary(tsx("audit/AuditPage.tsx", 'el.style.transition = "opacity 90ms";')),
    ).toHaveLength(1);
    expect(
      ruleSingleVocabulary(
        tsx("audit/AuditPage.tsx", 'el.style.setProperty("transition", "all 300ms");'),
      ),
    ).toHaveLength(1);
    expect(
      ruleSingleVocabulary(
        tsx(
          "audit/AuditPage.tsx",
          'el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 300, easing: "ease-in", iterations: Infinity });',
        ),
      ),
    ).toHaveLength(1);
    // Controls a naive version of each new scan would flag: `linear-gradient`
    // is not the `linear` curve, and a bare `animate(` call is not WAAPI.
    expect(
      ruleSingleVocabulary(
        css("audit/audit.css", ".x { --panel-sheen: linear-gradient(180deg, #000, #111); }"),
      ),
    ).toEqual([]);
    expect(ruleSingleVocabulary(tsx("audit/AuditPage.tsx", "animate(frame);"))).toEqual([]);
    // Consuming the scale is the point; only DECLARING outside tokens.css is a
    // defect, and an inline custom property that feeds a tokenised CSS rule is
    // how a FLIP travel distance reaches the stylesheet.
    expect(
      ruleSingleVocabulary(
        css(
          "audit/audit.css",
          ".x { transition: transform var(--motion-transit) var(--ease-damped); }",
        ),
      ),
    ).toEqual([]);
    expect(
      ruleSingleVocabulary(tsx("audit/AuditPage.tsx", 'style={{ "--transit-dx": `${dx}px` }}')),
    ).toEqual([]);
  });

  it("(10) cl. 6 — the global reduced-motion collapse is present, universal, and complete", () => {
    expect(ruleReducedMotionCollapse(SOURCES)).toEqual([]);
  });

  it("(10b) self-test: the collapse check bites descoping and spares formatting", () => {
    const legal =
      "@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; } }";
    expect(ruleReducedMotionCollapse(css("styles/primitives.css", legal))).toEqual([]);
    // The adversarial-review mutation: rescope the collapse to a class nobody
    // carries. All four substrings survive; the collapse is dead; RED.
    expect(
      ruleReducedMotionCollapse(
        css("styles/primitives.css", legal.replace("*, *::before, *::after", ".zz-nothing")),
      ),
    ).toHaveLength(1);
    // Losing a declaration, the media block, or the right media value is RED.
    expect(
      ruleReducedMotionCollapse(
        css("styles/primitives.css", legal.replace("transition-duration: 0.01ms !important;", "")),
      ),
    ).toHaveLength(1);
    expect(
      ruleReducedMotionCollapse(
        css("styles/primitives.css", legal.replace(": reduce", ": no-preference")),
      ),
    ).toHaveLength(1);
    expect(
      ruleReducedMotionCollapse(css("styles/primitives.css", ".x { opacity: 1; }")),
    ).toHaveLength(1);
    // Control: formatting is free — a naive exact-substring assertion dies on
    // this minified spelling; the structural check does not.
    expect(
      ruleReducedMotionCollapse(
        css(
          "styles/primitives.css",
          "@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:0.01ms !important;animation-iteration-count:1 !important;transition-duration:0.01ms !important}}",
        ),
      ),
    ).toEqual([]);
  });

  it("(11) the law is stated where it is enforced: all seven clauses, numbered, and cites no phantom", () => {
    // A reviewer rejects a bad animation by quoting a clause NUMBER. If the law
    // is not in the stylesheet, the guardian is a rule set with no rule.
    const law = readFileSync(join(SRC_DIR, "styles", "primitives.css"), "utf-8");
    for (const clause of [
      "1. CAUSED, OR ABSENT",
      "2. DEPICTS ONLY WHAT HAPPENED",
      "3. COMPOSITOR GRAMMAR",
      "4. A DENIAL NEVER EASES",
      "5. CEILINGS",
      "6. REDUCED MOTION LOSES NOTHING",
      "7. NO STATE GATES ON ANIMATION",
    ]) {
      expect(law, clause).toContain(clause);
    }
    // The law once cited primitives/oneShot.ts as clause 7's permitted site —
    // a file that did not exist, i.e. a pre-authorized bypass slot whose future
    // body nothing would have checked. Every file the law cites must be real.
    for (const cited of law.matchAll(/\bprimitives\/[A-Za-z0-9_-]+\.tsx?\b/g)) {
      const path = cited[0].replace(/\.tsx?$/, "");
      expect(
        SOURCES.some((s) => s.path === `${path}.ts` || s.path === `${path}.tsx`),
        `${cited[0]} is cited by the law and must exist`,
      ).toBe(true);
    }
  });

  /* ----------------------------------------------- declared residuals -- */

  describe("declared residuals — what this guardian does NOT catch (vacuous-guards.md §5)", () => {
    // Each entry proves its evasion is OPEN by running the real scan over the
    // evading fixture and asserting it comes back empty, so the perimeter is a
    // map, not a false sense of a seal. These assertions pin the BOUNDARY,
    // not its desirability: when a later unit hardens the named rule, MOVE the
    // fixture to the bite side of that rule's (Nb) self-test — do not delete
    // it, and do not read these as a defense of the gap.

    it("R1: JS-numeric choreography is invisible — with two LIVE instances", () => {
      // A duration constant and a setTimeout pacing a scene carry no event
      // name and no quoted style string, so rules 6 and 9 see nothing.
      const evader = tsx(
        "x/Scene.tsx",
        "const EXIT_MS = 600;\nconst t = setTimeout(onExit, EXIT_MS);",
      );
      expect(ruleNoAnimationGating(evader)).toEqual([]);
      expect(ruleSingleVocabulary(evader)).toEqual([]);
      // The live instances, pinned so this residual cannot rot silently:
      // knowledge/compare/compareMotion.ts (STAGGER_MS/IMPLODE_MS — a parallel
      // duration vocabulary in JS, clause 5), CompareChamber.tsx
      // (B_ONLY_FADE_MS + the setTimeout schedule), and ComparePane.tsx
      // (KEY_WITHDRAWN_EXIT_MS setTimeout gating the pane's exit — clause 7's
      // intent violated via a timer rather than an event). Owner: follow-up
      // NE-141b — bring the compare choreography under the token scale and
      // replace the exit timer with the surface.ts finished-promise wait.
      // When NE-141b lands, these three pins go RED and this entry retires.
      const live: Array<[string, string]> = [
        ["knowledge/compare/compareMotion.ts", "STAGGER_MS"],
        ["knowledge/compare/CompareChamber.tsx", "B_ONLY_FADE_MS"],
        ["knowledge/compare/ComparePane.tsx", "KEY_WITHDRAWN_EXIT_MS"],
      ];
      for (const [path, constant] of live) {
        const src = SOURCES.find((s) => s.path === path);
        expect(src, `${path} is walked`).toBeDefined();
        expect(src?.text, `${path} still holds ${constant}`).toContain(constant);
      }
    });

    it("R2: aliased or computed WAAPI access evades the literal member match", () => {
      // Rule 9 matches the literal `.animate(` spelling (and the Animation/
      // KeyframeEffect constructors). A bound alias or a computed member is
      // the same API with no matchable spelling. Backstop: review; the
      // product has zero WAAPI call sites.
      expect(
        ruleSingleVocabulary(tsx("x/Scene.tsx", "const play = el.animate.bind(el);\nplay(kf);")),
      ).toEqual([]);
      expect(ruleSingleVocabulary(tsx("x/Scene.tsx", 'el["ani" + "mate"](kf, opts);'))).toEqual([]);
    });

    it("R3: runtime CSS text (insertRule/cssText/<style> textContent) is never parsed as CSS", () => {
      // The walk parses .css FILES; a stylesheet built from a string at
      // runtime is script to every rule here, and the JS regexes require a
      // quote directly after the colon, which inline CSS text lacks.
      // Backstop: review; the product builds no runtime stylesheets.
      const evader = tsx(
        "x/Scene.tsx",
        'sheet.insertRule(".x { transition: all 4s ease-in-out }");',
      );
      expect(ruleTransitionAll(evader)).toEqual([]);
      expect(ruleSingleVocabulary(evader)).toEqual([]);
    });

    it("R4: reflection and indirection are outside every regex here", () => {
      // String-built property names, and `transition-property: var(--x)` with
      // the property LIST stored in a custom property (a list is not timing,
      // so the smuggling scan rightly stays quiet). Backstop: review.
      expect(
        ruleSingleVocabulary(tsx("x/Scene.tsx", 'el.style["trans" + "ition"] = fromConfig;')),
      ).toEqual([]);
      expect(
        ruleTransitionAll(
          css(
            "styles/evade.css",
            ":root { --props: all; }\n.x { transition-property: var(--props); }",
          ),
        ),
      ).toEqual([]);
    });

    it("R5: tokens.css itself is exempt from rules 7/8/9 wholesale", () => {
      // A non-motion custom property holding timing INSIDE tokens.css would
      // pass the smuggling scan. Backstop: test (9) pins the exact
      // --motion-*/--ease-* set and values, and tokens.css is the single,
      // small, reviewed vocabulary file.
      expect(ruleSingleVocabulary(css(TOKENS_CSS, ":root { --sneaky: 300ms; }"))).toEqual([]);
    });

    it("R6: a curve-less LONGHAND pair still defaults to `ease`", () => {
      // `transition-property` + `transition-duration` with no
      // `transition-timing-function` runs on the browser default. The
      // shorthand check deliberately does not reach longhand pairs — pairing
      // declarations across a rule needs a model of the cascade, and a naive
      // per-declaration version would flag every legal longhand. Backstop:
      // review; the product authors all timing as shorthand.
      expect(
        ruleOneCurve(
          css(
            "styles/evade.css",
            ".x { transition-property: opacity; transition-duration: var(--motion-snap); }",
          ),
        ),
      ).toEqual([]);
    });
  });
});
