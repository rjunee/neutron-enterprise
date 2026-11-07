// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// M1 — SURFACE ENTER/EXIT GRAMMAR (motion design §4.1), proved two ways.
//
// (A) A GUARDIAN over primitives/surface.css. Each rule is an exported pure
//     function over stylesheet text; the production test calls it on the real
//     file and the self-test calls THE SAME function on a planted violation and
//     on a legal control. A rule that only asserts "the class is present" is
//     satisfied by the wrong animation, so no rule here asserts presence: they
//     assert which keyframes run, for which token duration, that the base rules
//     never hide the surface, and that no class is defined without a call site
//     or used without a definition.
//
// (B) BEHAVIOURAL tests over the two surfaces in src/primitives — <DetailPane>
//     (which is also the span pane, the decision pane and the source pane) and
//     the <HashPlate> popover. These prove design §7's input-path budget: the
//     content and the focus of an opened surface are available in the same
//     commit, and no state is downstream of an animation.
//
// WHAT THIS CANNOT PROVE. jsdom does not run animations and does not apply
// stylesheets, so nothing here measures a frame rate and nothing here observes
// a fade. The reduced-motion evidence is therefore structural and behavioural,
// not visual: the DOM is proved identical with and without a reduced-motion
// preference, and the stylesheet is proved to leave the surface fully legible
// when the animation never runs at all.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DetailPane } from "./DetailPane";
import { HashPlate } from "./HashPlate";
import { SURFACE_EXIT, SURFACE_PANE, SURFACE_POP } from "./surface";

const PRIMITIVES_DIR = resolve(fileURLToPath(import.meta.url), "..");
const SRC_DIR = resolve(PRIMITIVES_DIR, "..");
const SURFACE_CSS = readFileSync(join(PRIMITIVES_DIR, "surface.css"), "utf-8");

/* ------------------------------------------------------------------ parse -- */

/** Comments are exempt: surface.css quotes the properties it forbids. */
function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

type Block = { head: string; body: string };

function blocks(css: string): Block[] {
  const out: Block[] = [];
  const re = /([^{}]*)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(stripComments(css))) !== null) {
    out.push({ head: (m[1] ?? "").trim(), body: m[2] ?? "" });
  }
  return out;
}

function decls(body: string): { prop: string; value: string }[] {
  const out: { prop: string; value: string }[] = [];
  for (const chunk of body.split(";")) {
    const i = chunk.indexOf(":");
    if (i < 0) continue;
    const prop = chunk.slice(0, i).trim().toLowerCase();
    if (!/^-{0,2}[a-z][a-z0-9-]*$/.test(prop)) continue;
    out.push({ prop, value: chunk.slice(i + 1).trim() });
  }
  return out;
}

/** `@keyframes NAME { … }` bodies, keyed by name. Nested percentage blocks
 * surface as their own blocks, which is what the property scan needs. */
function keyframeSteps(css: string): Map<string, { prop: string; value: string }[]> {
  const out = new Map<string, { prop: string; value: string }[]>();
  const text = stripComments(css);
  const re = /@keyframes\s+([A-Za-z0-9_-]+)\s*\{/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    let depth = 1;
    let j = re.lastIndex;
    while (j < text.length && depth > 0) {
      if (text[j] === "{") depth += 1;
      else if (text[j] === "}") depth -= 1;
      j += 1;
    }
    const body = text.slice(re.lastIndex, Math.max(j - 1, re.lastIndex));
    out.set(
      m[1] ?? "",
      blocks(body).flatMap((b) => decls(b.body)),
    );
    re.lastIndex = j;
  }
  return out;
}

/** The class rules (everything that is not an @keyframes block). */
function classRules(css: string): Block[] {
  const text = stripComments(css);
  const withoutKeyframes = text.replace(
    /@keyframes\s+[A-Za-z0-9_-]+\s*\{(?:[^{}]|\{[^{}]*\})*\}/g,
    " ",
  );
  return blocks(withoutKeyframes);
}

/** The `--motion-*` token an `animation` shorthand resolves its duration to,
 * plus the @keyframes it names. Returns null when either is missing — a
 * shorthand the guardian cannot read is a shorthand it must not pass. */
export function animationSpec(value: string): { name: string; duration: string } | null {
  const tokens = value.trim().split(/\s+/);
  const duration = tokens.find((t) => /^var\(--motion-[a-z-]+\)$/.test(t));
  const name = tokens.find((t) => /^[a-z][a-z0-9-]*$/i.test(t) && !/^var\(/.test(t));
  if (!duration || !name) return null;
  return { name, duration };
}

/* ------------------------------------------------------------------ rules -- */

/** The token scale (styles/tokens.css §3), in ms, so the asymmetry rule can
 * compare durations rather than compare strings. */
const SCALE_MS: Record<string, number> = {
  "var(--motion-drum)": 40,
  "var(--motion-chain)": 60,
  "var(--motion-snap)": 90,
  "var(--motion-rod)": 180,
  "var(--motion-settle)": 200,
  "var(--motion-transit)": 260,
  "var(--motion-pulse)": 400,
  "var(--motion-breathe)": 4000,
};

/** Every animated class in the sheet, mapped to what it actually runs. */
export function surfaceAnimations(css: string): Map<string, { name: string; duration: string }> {
  const out = new Map<string, { name: string; duration: string }>();
  for (const rule of classRules(css)) {
    for (const d of decls(rule.body)) {
      if (d.prop !== "animation") continue;
      const spec = animationSpec(d.value);
      if (spec) out.set(rule.head, spec);
    }
  }
  return out;
}

/** (R1) §3 asymmetry — leaving is NEVER slower than arriving. Exits are on the
 * operator's critical path; a slow exit is a surface arguing with a decision
 * that has already been made. */
export function ruleExitNeverSlowerThanEnter(css: string): string[] {
  const anims = surfaceAnimations(css);
  const exits = [...anims].filter(([sel]) => sel.includes("exit"));
  const enters = [...anims].filter(([sel]) => !sel.includes("exit"));
  const out: string[] = [];
  if (exits.length === 0) out.push("no exit animation is defined");
  for (const [exitSel, exit] of exits) {
    const exitMs = SCALE_MS[exit.duration];
    if (exitMs === undefined) {
      out.push(`${exitSel} :: duration \`${exit.duration}\` is outside the --motion-* scale`);
      continue;
    }
    for (const [enterSel, enter] of enters) {
      const enterMs = SCALE_MS[enter.duration];
      if (enterMs === undefined) {
        out.push(`${enterSel} :: duration \`${enter.duration}\` is outside the --motion-* scale`);
        continue;
      }
      if (exitMs > enterMs) {
        out.push(`${exitSel} (${exitMs}ms) leaves slower than ${enterSel} (${enterMs}ms) arrives`);
      }
    }
  }
  return out;
}

/** (R2) law 4 cl. 3 — every surface keyframe animates transform/opacity ONLY.
 * M0's guardian bans layout properties globally and paint on LOOPING keyframes;
 * a one-shot keyframe painting `background-color` slips between the two, and
 * this grammar has no business painting anything. */
export function ruleSurfaceKeyframesAreCompositorOnly(css: string): string[] {
  const out: string[] = [];
  const steps = keyframeSteps(css);
  if (steps.size === 0) out.push("no @keyframes found to audit");
  for (const [name, props] of steps) {
    if (props.length === 0) out.push(`@keyframes ${name} declares nothing`);
    for (const d of props) {
      if (d.prop !== "transform" && d.prop !== "opacity") {
        out.push(`@keyframes ${name} animates \`${d.prop}\``);
      }
    }
  }
  return out;
}

/** (R3) law 4 cl. 6 — the surface is never HIDDEN at rest. This is the
 * reduced-motion co-signal made structural: if the animation never runs (an old
 * engine, a screenshot, CSS that failed to load, an animation the OS suppressed
 * outright), the base rules must still leave the surface whole and readable.
 * A grammar that sets `opacity: 0` and reveals by keyframe is a surface that
 * exists only in movement. `pointer-events: none` on the EXIT is not hiding —
 * that node is on its way out by the operator's own action. */
export function ruleNeverHiddenAtRest(css: string): string[] {
  const out: string[] = [];
  for (const rule of classRules(css)) {
    const exiting = rule.head.includes("exit");
    for (const d of decls(rule.body)) {
      const v = d.value.toLowerCase();
      if (d.prop === "opacity" && parseFloat(v) < 1) out.push(`${rule.head} :: opacity: ${v}`);
      if (d.prop === "visibility" && v.startsWith("hidden")) out.push(`${rule.head} :: ${v}`);
      if (d.prop === "display" && v.startsWith("none")) out.push(`${rule.head} :: display: none`);
      if (d.prop === "transform") out.push(`${rule.head} :: base transform \`${v}\``);
      if (d.prop === "pointer-events" && v.startsWith("none") && !exiting) {
        out.push(`${rule.head} :: pointer-events: none on a surface that is not exiting`);
      }
    }
  }
  return out;
}

/** (R4) the join, both directions: no class is defined without a call site
 * (that is how a `transition: all` utility sat under law 4 for three months),
 * and no `ne-surface-*` string is used that the stylesheet does not define
 * (that is a class name typo which silently animates nothing). */
export function ruleClassJoin(css: string, sources: { path: string; text: string }[]): string[] {
  const defined = new Set(
    classRules(css)
      .flatMap((r) => r.head.split(","))
      .map((s) => s.trim())
      .filter((s) => s.startsWith("."))
      .map((s) => s.slice(1)),
  );
  const used = new Map<string, string[]>();
  for (const s of sources) {
    for (const m of s.text.matchAll(/["'`](ne-surface-[a-z-]+)["'`]/g)) {
      const cls = m[1] ?? "";
      used.set(cls, [...(used.get(cls) ?? []), s.path]);
    }
  }
  const out: string[] = [];
  for (const cls of defined) {
    if (!used.has(cls)) out.push(`.${cls} is defined but nothing renders it`);
  }
  for (const cls of used.keys()) {
    if (!defined.has(cls)) {
      out.push(`\`${cls}\` is rendered but the stylesheet defines no such class`);
    }
  }
  return out;
}

/** (R5) law 4 cl. 6, the strong form — no primitive BRANCHES on a motion
 * preference. Reduced motion is handled once, in CSS, by the global collapse in
 * styles/primitives.css. If a component asked the media query instead, it could
 * render a different DOM for a reduced-motion reader — and a signal that exists
 * in one DOM and not the other is precisely the state that fails a screenshot.
 * The co-signal must be the content itself, which is why it is unconditional. */
export function ruleNoMotionBranchInPrimitives(
  sources: { path: string; text: string }[],
): string[] {
  const query = "prefers-" + "reduced-motion";
  return sources
    .filter((s) => s.path.startsWith("primitives/") && s.text.includes(query))
    .map((s) => `${s.path} :: branches on the motion preference in JS`);
}

/* ------------------------------------------------------- production walk -- */

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "test") continue;
      out.push(...walk(full));
      continue;
    }
    if (/\.test\.tsx?$/.test(entry)) continue;
    if (/\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

const SCRIPTS = walk(SRC_DIR).map((full) => ({
  path: relative(SRC_DIR, full).replaceAll("\\", "/"),
  text: readFileSync(full, "utf-8"),
}));

/* ------------------------------------------------------ guardian: the CSS -- */

describe("M1 guardian — the surface grammar (primitives/surface.css)", () => {
  it("audits a stylesheet that actually declares the grammar (non-vacuity)", () => {
    // Every rule below filters these collections. If the file moved or was
    // emptied, the rules would pass while examining nothing.
    expect(SCRIPTS.length).toBeGreaterThan(50);
    expect(keyframeSteps(SURFACE_CSS).size).toBe(3);
    expect([...surfaceAnimations(SURFACE_CSS).keys()].sort()).toEqual([
      `.${SURFACE_EXIT}`,
      `.${SURFACE_PANE}`,
      `.${SURFACE_POP}`,
    ]);
  });

  it("(R0) each class runs the §4.1 effect at the §4.1 duration — not merely SOME animation", () => {
    const anims = surfaceAnimations(SURFACE_CSS);
    // Pane: translateX + fade over --motion-rod (the longest a surface takes).
    expect(anims.get(`.${SURFACE_PANE}`)).toEqual({
      name: "ne-surface-pane-in",
      duration: "var(--motion-rod)",
    });
    // Popover: scale + fade over --motion-snap.
    expect(anims.get(`.${SURFACE_POP}`)).toEqual({
      name: "ne-surface-pop-in",
      duration: "var(--motion-snap)",
    });
    // Every exit: fade over --motion-snap (§3).
    expect(anims.get(`.${SURFACE_EXIT}`)).toEqual({
      name: "ne-surface-out",
      duration: "var(--motion-snap)",
    });

    // And the effects are the ones §4.1 names, property by property.
    const steps = keyframeSteps(SURFACE_CSS);
    const pane = steps.get("ne-surface-pane-in") ?? [];
    expect(pane.filter((d) => d.prop === "transform").map((d) => d.value)).toEqual([
      "translateX(var(--space-8))",
      "translateX(0)",
    ]);
    expect(pane.filter((d) => d.prop === "opacity").map((d) => d.value)).toEqual(["0", "1"]);
    const pop = steps.get("ne-surface-pop-in") ?? [];
    expect(pop.filter((d) => d.prop === "transform").map((d) => d.value)).toEqual([
      "scale(0.98)",
      "scale(1)",
    ]);
    // The exit is a fade and NOTHING else — no travel, no scale, no re-staging
    // of a surface the operator already dismissed.
    expect(steps.get("ne-surface-out")).toEqual([
      { prop: "opacity", value: "1" },
      { prop: "opacity", value: "0" },
    ]);
  });

  it("(R1) an exit never leaves more slowly than an enter arrives", () => {
    expect(ruleExitNeverSlowerThanEnter(SURFACE_CSS)).toEqual([]);
  });

  it("(R1b) self-test: the asymmetry rule bites a slow exit and an off-scale duration", () => {
    const slow =
      ".ne-surface-pop { animation: ne-a var(--motion-snap) var(--ease-damped); }\n" +
      ".ne-surface-exit { animation: ne-b var(--motion-settle) var(--ease-damped) forwards; }";
    expect(ruleExitNeverSlowerThanEnter(slow)).toHaveLength(1);
    // Equal is fine; only SLOWER is the defect.
    const equal =
      ".ne-surface-pop { animation: ne-a var(--motion-snap) var(--ease-damped); }\n" +
      ".ne-surface-exit { animation: ne-b var(--motion-snap) var(--ease-damped) forwards; }";
    expect(ruleExitNeverSlowerThanEnter(equal)).toEqual([]);
    // A duration outside the scale is a parallel vocabulary (law 4 cl. 5), and
    // it is also a duration this rule cannot compare — it must not pass by
    // failing to parse.
    const offScale =
      ".ne-surface-pane { animation: ne-a var(--motion-rod) var(--ease-damped); }\n" +
      ".ne-surface-exit { animation: ne-b var(--motion-swoosh) var(--ease-damped) forwards; }";
    expect(ruleExitNeverSlowerThanEnter(offScale)).toHaveLength(1);
    // A sheet with enters and no exit is the shape that ships "we'll add the
    // exit later" — the rule must not read silence as compliance.
    expect(
      ruleExitNeverSlowerThanEnter(".ne-surface-pane { animation: ne-a var(--motion-rod); }"),
    ).toHaveLength(1);
  });

  it("(R2) every surface keyframe animates transform/opacity only", () => {
    expect(ruleSurfaceKeyframesAreCompositorOnly(SURFACE_CSS)).toEqual([]);
  });

  it("(R2b) self-test: the keyframe scan bites paint and layout, spares transform/opacity", () => {
    expect(
      ruleSurfaceKeyframesAreCompositorOnly(
        "@keyframes ne-x { from { background-color: var(--panel); } to { background-color: var(--panel-raised); } }",
      ),
    ).toHaveLength(2);
    // The shape that actually ships as "slide the pane in": an animated width.
    expect(
      ruleSurfaceKeyframesAreCompositorOnly(
        "@keyframes ne-x { from { width: 0; opacity: 0; } to { width: var(--detail-width); opacity: 1; } }",
      ),
    ).toHaveLength(2);
    expect(
      ruleSurfaceKeyframesAreCompositorOnly(
        "@keyframes ne-x { from { opacity: 0; transform: translateX(var(--space-8)); } to { opacity: 1; transform: translateX(0); } }",
      ),
    ).toEqual([]);
    // An empty sheet must fail, not pass: this rule cannot be satisfied by
    // deleting the thing it audits.
    expect(ruleSurfaceKeyframesAreCompositorOnly(".x { color: red; }")).toHaveLength(1);
  });

  it("(R3) the base rules never hide the surface — the reduced-motion co-signal, structurally", () => {
    expect(ruleNeverHiddenAtRest(SURFACE_CSS)).toEqual([]);
  });

  it("(R3b) self-test: the hidden-at-rest scan bites the four ways to hide and spares the exit's pointer-events", () => {
    expect(ruleNeverHiddenAtRest(".ne-surface-pane { opacity: 0; animation: ne-a; }")).toHaveLength(
      1,
    );
    expect(ruleNeverHiddenAtRest(".ne-surface-pop { visibility: hidden; }")).toHaveLength(1);
    expect(ruleNeverHiddenAtRest(".ne-surface-pop { display: none; }")).toHaveLength(1);
    // A base transform is the subtler shape: the surface is "there" but parked
    // 8px off, and it never arrives if the animation does not run.
    expect(
      ruleNeverHiddenAtRest(".ne-surface-pane { transform: translateX(var(--space-8)); }"),
    ).toHaveLength(1);
    // Controls that must pass: the exit's pointer-events, and a fully-opaque
    // base declaration.
    expect(ruleNeverHiddenAtRest(".ne-surface-exit { pointer-events: none; }")).toEqual([]);
    expect(ruleNeverHiddenAtRest(".ne-surface-pane { opacity: 1; }")).toEqual([]);
    // …and pointer-events: none on a NON-exiting surface is still a defect.
    expect(ruleNeverHiddenAtRest(".ne-surface-pop { pointer-events: none; }")).toHaveLength(1);
  });

  it("(R4) every class defined is rendered, and every class rendered is defined", () => {
    expect(ruleClassJoin(SURFACE_CSS, SCRIPTS)).toEqual([]);
  });

  it("(R4b) self-test: the join scan bites dead CSS and a typo'd class, spares the real pair", () => {
    const sheet = ".ne-surface-pane { animation: ne-a var(--motion-rod); }";
    // Dead CSS — the `.ne-motion-settle` failure mode by name.
    expect(ruleClassJoin(sheet, [{ path: "x.tsx", text: "const A = 1;" }])).toHaveLength(1);
    // A rendered class the sheet does not define animates nothing at all.
    expect(
      ruleClassJoin(sheet, [
        { path: "x.tsx", text: '"ne-surface-pane"' },
        { path: "y.tsx", text: '"ne-surface-pane-slow"' },
      ]),
    ).toHaveLength(1);
    expect(ruleClassJoin(sheet, [{ path: "x.tsx", text: 'className="ne-surface-pane"' }])).toEqual(
      [],
    );
  });

  it("(R5) no primitive branches its DOM on a motion preference — CSS owns the collapse", () => {
    expect(ruleNoMotionBranchInPrimitives(SCRIPTS)).toEqual([]);
    // …and the collapse the primitives are relying on really is there, in the
    // file M0 owns. This grammar is only safe to leave unconditional because
    // that universal rule exists.
    const law = readFileSync(join(SRC_DIR, "styles", "primitives.css"), "utf-8");
    expect(law).toContain("@media (prefers-reduced-motion: reduce)");
    expect(law).toContain("animation-duration: 0.01ms !important");
  });

  it("(R5b) self-test: the motion-branch scan bites a primitive and spares a screen", () => {
    const branch = `if (matchMedia("(prefers-` + `reduced-motion: reduce)").matches) return null;`;
    expect(
      ruleNoMotionBranchInPrimitives([{ path: "primitives/DetailPane.tsx", text: branch }]),
    ).toHaveLength(1);
    // Out of scope by design: this rule is the M1 grammar's contract, not a
    // product-wide ban a later unit would have to argue with.
    expect(ruleNoMotionBranchInPrimitives([{ path: "trace/TracePage.tsx", text: branch }])).toEqual(
      [],
    );
    expect(
      ruleNoMotionBranchInPrimitives([
        { path: "primitives/DetailPane.tsx", text: "const w = 420;" },
      ]),
    ).toEqual([]);
  });
});

/* -------------------------------------------------- behaviour: the panes -- */

/** jsdom implements no Web Animations API, so `getAnimations` has to be
 * installed to exercise the real-browser exit path — and removed again, or the
 * next test inherits a surface that never finishes leaving. */
function withRunningAnimation(finished: Promise<unknown>): () => void {
  const original = Object.getOwnPropertyDescriptor(Element.prototype, "getAnimations");
  Object.defineProperty(Element.prototype, "getAnimations", {
    value: () => [{ finished } as unknown as Animation],
    configurable: true,
    writable: true,
  });
  return () => {
    if (original) Object.defineProperty(Element.prototype, "getAnimations", original);
    else delete (Element.prototype as Partial<Element>).getAnimations;
  };
}

describe("M1 — <DetailPane> surface (§4.1, §7 input path)", () => {
  it("carries the PANE grammar and not the popover's — the class maps to a proved effect", () => {
    const { container } = render(
      <DetailPane title="Span detail" onClose={() => {}}>
        <p>body</p>
      </DetailPane>,
    );
    const pane = container.querySelector(".ne-detail");
    expect(pane).toHaveClass(SURFACE_PANE);
    // Presence alone would pass on the wrong animation, so the wrong grammars
    // are asserted ABSENT too — and (R0) above pins what SURFACE_PANE runs, so
    // this class name resolves to a proved effect rather than to a hope.
    expect(pane).not.toHaveClass(SURFACE_POP);
    expect(pane).not.toHaveClass(SURFACE_EXIT);
  });

  it("is fully interactive in the commit it opens — content and focus, nothing waits on animation", () => {
    // Design §7, input path: "the animation dresses a surface that is already
    // interactive". Nothing below is inside waitFor/findBy — if any of it
    // needed another tick, this test fails.
    render(
      <DetailPane
        title="Span detail"
        subtitle="req_01"
        onClose={() => {}}
        footer={<button type="button">Approve</button>}
      >
        <p>body</p>
      </DetailPane>,
    );
    expect(screen.getByRole("heading", { name: "Span detail" })).toBeInTheDocument();
    expect(screen.getByText("req_01")).toBeInTheDocument();
    expect(screen.getByText("body")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    // Focus has already moved into the pane, in the same commit.
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close" }));
  });

  it("renders an identical DOM with and without a reduced-motion preference (cl. 6)", () => {
    // The reduced-motion co-signal is the pane's own content, and it is
    // unconditional. This bites the day a motion query starts deciding what
    // renders: a state present in one DOM and absent from the other is the
    // state that fails a screenshot and fails a reader. (R5 proves the same
    // thing statically; this proves it through the component.)
    const matchMedia = (reduce: boolean) =>
      vi.fn().mockImplementation((query: string) => ({
        matches: reduce && query.includes("reduced-motion"),
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }));
    const pane = (
      <DetailPane title="Span detail" subtitle="req_01" onClose={() => {}}>
        <p>body</p>
      </DetailPane>
    );
    // useId is per-render and deliberately unstable; it is not a signal.
    const normalise = (html: string) => html.replace(/(id|aria-labelledby)="[^"]*"/g, '$1="_"');

    vi.stubGlobal("matchMedia", matchMedia(false));
    const moving = normalise(render(pane).container.innerHTML);
    vi.stubGlobal("matchMedia", matchMedia(true));
    const still = normalise(render(pane).container.innerHTML);
    vi.unstubAllGlobals();

    expect(still).toBe(moving);
    // …so the equality above is not two empty strings agreeing.
    expect(still).toContain("Span detail");
    expect(still).toContain(SURFACE_PANE);
  });
});

describe("M1 — <HashPlate> popover surface (§4.1)", () => {
  const VALUE = "abcdef" + "0".repeat(52) + "123456";
  const trigger = () => screen.getByRole("button", { name: `hash ${VALUE}` });

  it("pops in place on the operator's action, with the full hash in the same commit", () => {
    const { container } = render(<HashPlate value={VALUE} />);
    expect(container.querySelector(".ne-hashplate__popover")).toBeNull();

    fireEvent.focus(trigger());
    const popover = container.querySelector(".ne-hashplate__popover");
    expect(popover).toHaveClass(SURFACE_POP);
    expect(popover).not.toHaveClass(SURFACE_PANE);
    expect(popover).not.toHaveClass(SURFACE_EXIT);
    // The static co-signal — the full value and the copy affordance — is in the
    // DOM in the same commit, not after an animation.
    expect(popover).toHaveTextContent(VALUE);
    expect(screen.getByLabelText("copy full hash")).toBeInTheDocument();
  });

  it("with nothing animating, the exit costs nothing: the surface is gone at once", () => {
    // jsdom runs no animations — the degenerate case law 4 cl. 7 must survive.
    // No animation means no `animationend`, so a hook that waited on one would
    // strand this popover on screen forever. The list is empty and the node
    // goes in the same flush.
    const { container } = render(<HashPlate value={VALUE} />);
    fireEvent.focus(trigger());
    fireEvent.blur(trigger());
    expect(container.querySelector(".ne-hashplate__popover")).toBeNull();
  });

  it("with an animation running, it fades under the EXIT grammar and drops when that finishes", async () => {
    let settle!: () => void;
    const finished = new Promise<void>((resolve) => {
      settle = resolve;
    });
    const restore = withRunningAnimation(finished);
    try {
      const { container } = render(<HashPlate value={VALUE} />);
      fireEvent.focus(trigger());
      fireEvent.blur(trigger());

      // Still painted, now carrying the EXIT grammar and not the entrance — and
      // inert, so a still-tabbable COPY button cannot survive inside a surface
      // the operator already dismissed.
      const ghost = container.querySelector(".ne-hashplate__popover");
      expect(ghost).toHaveClass(SURFACE_EXIT);
      expect(ghost).not.toHaveClass(SURFACE_POP);
      expect(ghost).toHaveAttribute("inert");

      await act(async () => {
        settle();
        await finished;
      });
      expect(container.querySelector(".ne-hashplate__popover")).toBeNull();
    } finally {
      restore();
    }
  });

  it("re-opening mid-fade cancels the exit instead of stacking a second surface", () => {
    // A never-settling animation: if reopening did not cancel the exit, the
    // popover would be stuck inert and unreadable for the rest of the session.
    const restore = withRunningAnimation(new Promise<void>(() => {}));
    try {
      const { container } = render(<HashPlate value={VALUE} />);
      fireEvent.focus(trigger());
      fireEvent.blur(trigger());
      expect(container.querySelector(".ne-hashplate__popover")).toHaveClass(SURFACE_EXIT);

      fireEvent.focus(trigger());
      const popovers = container.querySelectorAll(".ne-hashplate__popover");
      expect(popovers).toHaveLength(1);
      expect(popovers[0]).toHaveClass(SURFACE_POP);
      expect(popovers[0]).not.toHaveAttribute("inert");
    } finally {
      restore();
    }
  });
});
