// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE GLOBAL-BUTTON-INHERITANCE GUARDIAN — nothing on a screen may take a
// visual property from a rule it does not name.
//
// WHY THIS EXISTS, and it is one defect that shipped with a proof attached.
//
// `styles.css` carries a bare element rule, `button { … }`, that declares
// `background`, `border` and `color`. It was retuned — from the primary fill to
// the raised-panel resting treatment — under the claim of "zero shipped visual
// delta today", and the claim was justified by a scan: all sixteen remaining
// BARE `<button>` elements in the package resolve to a scoped rule that sets
// their own background, border AND colour, so nothing was left leaning on the
// element rule.
//
// The count was right. The scope was wrong. `.ne-ann` — the Posture
// annunciator tile, twelve of them on station 6 of the guided tour — is a
// CLASSED button, so the scan never looked at it, and its rule declared
// `background` and no `border`. A classed element does not inherit an element
// rule all-or-nothing: it inherits it PROPERTY BY PROPERTY, for every property
// its own rules omit. So the twelve tiles changed outline colour, from
// --primary-fill to --rule-strong, in a train that said it changed nothing.
//
// And the sixteen were not all resolved correctly either. GraphChamber's
// per-node accessibility button is bare and NO rule reaches it — the nearest
// candidate, `.ne-chamber__viewport-controls button`, styles a sibling group
// further up the same component. It paints nothing only because its list is
// inside `.ne-visually-hidden`, which is a different reason from the one the
// scan gave. Two independent defects, so this guard answers both questions
// from the cascade: WHICH rules reach this element, and does this element draw.
//
// THE RULE THIS ENCODES, derived rather than enumerated — an allowlist of
// today's offenders would rot exactly the way the bare-button scan did:
//
//   For every element in shipped src/ that renders as a <button>, each of
//   `background`, `border` and `color` must be declared by some rule whose
//   SUBJECT is that button and which NAMES it — either through one of the
//   classes the button wears, or through an ancestor class that the rendering
//   component's own markup puts on the page. The bare `button` element rule is
//   never counted. Whatever it declares is a floor for a control nobody
//   designed, never a silent dependency of one somebody did.
//
// WHAT IT CANNOT SEE, stated so nobody trusts it further than it goes:
//
//   * ANCESTRY IS EXACT WITHIN A COMPONENT AND APPROXIMATE ACROSS ONE. The JSX
//     is parsed, so `.ne-ontology__tabs button` is credited to a button proven
//     to be inside that element and to no other button in the file. Above a
//     component's own root it is a UNION over that component's placements: a
//     primitive used on two pages sees both callers' containers, and a file
//     exporting two components lends each one's placement to the other. Every
//     one of those approximations is in the permissive direction — it can
//     credit a rule that does not really reach, never deny one that does — so
//     this guard can be blind, and cannot cry wolf.
//   * A CLIPPED SUBTREE IS EXEMPT, derived and not listed: a class whose rule
//     is `clip-path: inset(50%)` on a 1px box draws nothing, so nothing inside
//     it has a surface to get wrong. Un-clip such a rule and its contents
//     become this guard's business again on the next run, automatically.
//   * SPECIFICITY IS NOT COMPUTED, because it does not need to be: every rule
//     counted here carries at least one class, and one class outranks the bare
//     element selector. A second CLASS rule overriding a first is a decision
//     between two rules that both name the element, which is not this defect.
//   * ONLY THE RESTING STATE COUNTS. `:hover`, `:focus-visible` and
//     `[aria-selected="true"]` are states a control enters, not the surface it
//     draws at rest, and a tile whose only border arrives on hover is still a
//     tile whose resting border came from somewhere it did not name.

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");

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

// ---------------------------------------------------------------------------
// CSS: every rule, flattened out of its at-blocks.
// ---------------------------------------------------------------------------

export interface CssRule {
  readonly selector: string;
  readonly decls: string;
}

/**
 * Every rule in a stylesheet, with `@media`/`@supports` wrappers flattened
 * away. A media query narrows WHEN a rule applies, never whether the element
 * names it — a border that only exists above 1280px is still a border the
 * element's own rule declares, and one that only exists below is a viewport
 * where the element rule shows through. Both are in scope, so both count.
 */
export function flattenRules(css: string): CssRule[] {
  const source = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const out: CssRule[] = [];
  const stack: (string | null)[] = [];
  let buffer = "";
  for (const ch of source) {
    if (ch === "{") {
      const head = buffer.trim();
      buffer = "";
      stack.push(head.startsWith("@") ? null : head);
      continue;
    }
    if (ch === "}") {
      const head = stack.pop();
      if (head !== undefined && head !== null) out.push({ selector: head, decls: buffer });
      buffer = "";
      continue;
    }
    buffer += ch;
  }
  return out;
}

/** The three properties the bare element rule declares, and therefore the three
 * a designed control can silently take from it. `background-color` and
 * `border-color` count as the shorthand does; `border: none` counts, because
 * "no border" is an answer. */
export const GLOBAL_PROPERTIES = ["background", "border", "color"] as const;
export type GlobalProperty = (typeof GLOBAL_PROPERTIES)[number];

const PROPERTY_PATTERN: Record<GlobalProperty, RegExp> = {
  background: /(?:^|[;{\s])background(?:-color)?\s*:/,
  border: /(?:^|[;{\s])border(?:-color)?\s*:/,
  // `font:` is a shorthand that cannot set colour, so a bare `color:` is the
  // whole surface — the same reasoning contrast-guardian.test.ts states.
  color: /(?:^|[;{\s])color\s*:/,
};

export function declares(decls: string, property: GlobalProperty): boolean {
  return PROPERTY_PATTERN[property].test(decls);
}

// ---------------------------------------------------------------------------
// Selector matching.
// ---------------------------------------------------------------------------

/** A state, not the resting surface. */
const NON_RESTING =
  /:(?:hover|active|focus|focus-visible|focus-within|disabled|checked|target)\b|::|\[/;

function compounds(selector: string): string[] {
  return selector
    .trim()
    .split(/\s*[>+~]\s*|\s+/)
    .filter(Boolean);
}

function classesOf(compound: string): string[] {
  return [...compound.matchAll(/\.(-?[A-Za-z_][A-Za-z0-9_-]*)/g)].map((m) => m[1]!);
}

/**
 * Could this ONE selector (no commas) style a resting `<button>` wearing
 * `tokens`, rendered somewhere `ancestors` are in scope — and does it NAME it?
 *
 * The bare `button` element rule is rejected here, which is the whole point:
 * it is a single compound with no class, so it names nothing.
 */
export function ruleApplies(
  selector: string,
  tokens: readonly string[],
  ancestors: ReadonlySet<string>,
): boolean {
  if (NON_RESTING.test(selector)) return false;
  if (selector.includes("#")) return false;
  const parts = compounds(selector);
  const subject = parts[parts.length - 1];
  if (subject === undefined) return false;
  const element = /^[A-Za-z][\w-]*/.exec(subject);
  // A subject that is some other element (`li`, `span`) is not our button.
  if (element !== null && element[0] !== "button") return false;
  const own = classesOf(subject);
  // No class anywhere in the selector ⇒ the global element rule itself.
  if (own.length === 0 && parts.length === 1) return false;
  if (element === null && own.length === 0) return false;
  if (!own.every((name) => tokens.includes(name))) return false;
  return parts.slice(0, -1).every((part) => classesOf(part).every((name) => ancestors.has(name)));
}

// ---------------------------------------------------------------------------
// TSX: every <button> the package renders, where it sits, and what it wears.
//
// This is a REAL PARSE, not a scrape, and that is the second lesson of the
// defect. A file-scoped approximation of "what might be above this button"
// credited `.ne-chamber__viewport-controls button` to GraphChamber's per-node
// accessibility button — which is not inside the viewport controls at all, it
// is inside `.ne-chamber__nodes` further down the same file. An ancestor check
// that answers "somewhere in this file" answers the wrong question, and it is
// the same mistake in miniature as counting bare buttons: the count was of a
// real thing that was not the thing being claimed.
// ---------------------------------------------------------------------------

import ts from "typescript";

function literalTokens(expression: string): Set<string> {
  const out = new Set<string>();
  for (const quoted of expression.matchAll(/"([^"]*)"|'([^']*)'/g)) {
    for (const token of (quoted[1] ?? quoted[2] ?? "").split(/\s+/).filter(Boolean)) out.add(token);
  }
  for (const template of expression.matchAll(/`([^`]*)`/g)) {
    for (const segment of template[1]!.split(/\$\{[^}]*\}/)) {
      for (const token of segment.split(/\s+/).filter(Boolean)) out.add(token);
    }
  }
  return out;
}

/**
 * Every class in this package is lower-case-initial kebab/BEM, and the test
 * below proves it of every stylesheet — so a token that is not is not a class.
 * It is the other operand of the comparison that chose one:
 * `` `ne-ann${state === "FAILED" ? " ne-ann--fail" : ""}` `` yields "FAILED"
 * to any literal scrape, and a token no rule can ever name has no business in
 * a report whose whole value is naming things precisely.
 */
export function isClassToken(token: string): boolean {
  return /^[a-z]/.test(token);
}

export interface ButtonSite {
  readonly line: number;
  readonly tokens: string[];
  /** The classes on the button's real JSX ancestors, within its own component. */
  readonly ancestors: Set<string>;
  /** The component function that renders it — the unit placement is tracked by,
   * because a file holds several and they are not each other's context. */
  readonly owner: string;
}

export interface ComponentUse {
  readonly tag: string;
  readonly ancestors: Set<string>;
  readonly owner: string;
}

export interface FileScan {
  readonly buttons: ButtonSite[];
  /** `<SomeComponent>` use sites: tag name → the ancestor classes above it. */
  readonly componentUses: ComponentUse[];
  /** Local name a component was imported under → the module specifier. */
  readonly imports: Map<string, string>;
  /** Component functions this file declares, so a local `<Evidence />` resolves. */
  readonly declares: Set<string>;
}

function attributeClasses(
  attributes: ts.JsxAttributes,
  source: ts.SourceFile,
): string[] | undefined {
  for (const attribute of attributes.properties) {
    if (!ts.isJsxAttribute(attribute) || attribute.name.getText(source) !== "className") continue;
    const initializer = attribute.initializer;
    if (initializer === undefined) return [];
    if (ts.isStringLiteral(initializer)) {
      return initializer.text.split(/\s+/).filter(Boolean);
    }
    return [...literalTokens(initializer.getText(source))].filter(isClassToken);
  }
  return undefined;
}

/** Parse one .tsx and report every button, every component use site, and the
 * chain of classes actually above each — from the syntax tree, so "inside" is
 * a fact about the markup rather than a guess about the file. */
export function scanTsx(fileName: string, source: string): FileScan {
  const tree = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const buttons: ButtonSite[] = [];
  const componentUses: ComponentUse[] = [];
  const imports = new Map<string, string>();
  const declared = new Set<string>();
  const chain: string[] = [];
  const owners: string[] = [];
  const owner = (): string => owners[owners.length - 1] ?? "(module)";

  const visit = (node: ts.Node): void => {
    let opened: number | undefined;
    let named = false;
    const declaredName =
      ts.isFunctionDeclaration(node) && node.name !== undefined
        ? node.name.text
        : ts.isVariableDeclaration(node) &&
            ts.isIdentifier(node.name) &&
            node.initializer !== undefined &&
            (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))
          ? node.name.text
          : undefined;
    if (declaredName !== undefined) {
      declared.add(declaredName);
      owners.push(declaredName);
      named = true;
    }
    let tag: string | undefined;
    let attributes: ts.JsxAttributes | undefined;
    if (ts.isJsxElement(node)) {
      tag = node.openingElement.tagName.getText(tree);
      attributes = node.openingElement.attributes;
    } else if (ts.isJsxSelfClosingElement(node)) {
      tag = node.tagName.getText(tree);
      attributes = node.attributes;
    } else if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      const clause = node.importClause;
      const specifier = node.moduleSpecifier.text;
      if (clause?.name !== undefined) imports.set(clause.name.text, specifier);
      const named = clause?.namedBindings;
      if (named !== undefined && ts.isNamedImports(named)) {
        for (const element of named.elements) imports.set(element.name.text, specifier);
      }
    }
    if (tag !== undefined && attributes !== undefined) {
      const own = attributeClasses(attributes, tree) ?? [];
      const ancestors = new Set(chain);
      if (tag === "button") {
        buttons.push({
          line: tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1,
          tokens: own,
          ancestors,
          owner: owner(),
        });
      } else if (/^[A-Z]/.test(tag)) {
        componentUses.push({ tag, ancestors, owner: owner() });
      }
      opened = chain.length;
      chain.push(...own);
    }
    ts.forEachChild(node, visit);
    if (opened !== undefined) chain.length = opened;
    if (named) owners.pop();
  };

  visit(tree);
  return { buttons, componentUses, imports, declares: declared };
}

// ---------------------------------------------------------------------------
// The scan.
// ---------------------------------------------------------------------------

const CSS_FILES = filesUnder(SRC_DIR, ".css");
const TSX_FILES = filesUnder(SRC_DIR, ".tsx");
const SCAN = new Map(
  TSX_FILES.map((file) => [file, scanTsx(file, readFileSync(file, "utf-8"))] as const),
);

/**
 * A class whose rule clips it to nothing. DERIVED, not listed: the rule is
 * `clip-path: inset(50%)` on a 1px box, which is the one pattern in this
 * package that puts an element in the accessibility tree and off the screen.
 * An element inside such a subtree paints nothing, so it has no surface to take
 * from the element rule — and the day someone un-clips one of these, it stops
 * being exempt here automatically instead of staying on a stale allowlist.
 */
export function clippedClasses(rules: readonly CssRule[]): Set<string> {
  const out = new Set<string>();
  for (const rule of rules) {
    if (!/clip-path\s*:\s*inset\(\s*50%\s*\)/.test(rule.decls)) continue;
    for (const one of rule.selector.split(",")) {
      const parts = compounds(one);
      const subject = parts[parts.length - 1];
      if (subject === undefined || parts.length !== 1) continue;
      for (const name of classesOf(subject)) out.add(name);
    }
  }
  return out;
}

/**
 * Classes above a COMPONENT's own root — a primitive is placed inside its
 * caller's markup, so `.ne-annunciator` is an ancestor of AnnunciatorRow's
 * button even though that class is written in PosturePage.tsx. Keyed by
 * `file::component` rather than by file, because a file holds several
 * components and they are NOT each other's context: GraphChamber.tsx renders
 * both `.ne-chamber__viewport-controls` and `.ne-chamber__nodes`, and a
 * file-scoped answer credits the viewport controls' rule to the node list's
 * buttons — the exact mis-resolution that let a second reviewer and a first
 * disagree about this file with neither of them wrong about what they read.
 *
 * Computed to a fixpoint so a primitive three components deep still sees the
 * page's containers, and so a component used in two places gets the union.
 */
function externalAncestors(): Map<string, Set<string>> {
  const key = (file: string, component: string): string => `${file}::${component}`;
  const external = new Map<string, Set<string>>();
  const at = (name: string): Set<string> => {
    const found = external.get(name);
    if (found !== undefined) return found;
    const fresh = new Set<string>();
    external.set(name, fresh);
    return fresh;
  };
  const uses: { from: string; to: string; ancestors: Set<string> }[] = [];
  for (const [file, scan] of SCAN) {
    for (const use of scan.componentUses) {
      const specifier = scan.imports.get(use.tag);
      const targets: string[] = [];
      if (specifier === undefined) {
        // A component declared in this same file — `<Evidence />` inside
        // EvidenceDrawer.tsx, whose button is styled by `.ne-evidence-drawer
        // li > button` off a container the DRAWER writes, not the Evidence
        // body. Without this the local hand-off is invisible.
        if (scan.declares.has(use.tag)) targets.push(key(file, use.tag));
      } else if (specifier.startsWith(".")) {
        const base = resolve(dirname(file), specifier);
        for (const candidate of [`${base}.tsx`, join(base, "index.tsx")]) {
          if (SCAN.has(candidate)) targets.push(key(candidate, use.tag));
        }
      }
      for (const target of targets) {
        uses.push({ from: key(file, use.owner), to: target, ancestors: use.ancestors });
      }
    }
  }
  // An imported component's declared name is the local name at the use site;
  // where a file exports it under a different name the edge simply does not
  // form, which loses context and can only ADD a finding, never hide one.
  for (let pass = 0; pass < 16; pass++) {
    let changed = false;
    for (const use of uses) {
      const target = at(use.to);
      for (const name of [...use.ancestors, ...at(use.from)]) {
        if (!target.has(name)) {
          target.add(name);
          changed = true;
        }
      }
    }
    if (!changed) break;
  }
  return external;
}

const EXTERNAL = externalAncestors();

function rulesOf(overrides: ReadonlyMap<string, string> = new Map()): CssRule[] {
  return CSS_FILES.flatMap((file) =>
    flattenRules(overrides.get(file) ?? readFileSync(file, "utf-8")),
  );
}

/** Every button site that resolves at least one of the three properties from the
 * global element rule, as `path:line [classes] → property, property`. */
export function offendingSites(overrides?: ReadonlyMap<string, string>): string[] {
  const rules = rulesOf(overrides);
  const clipped = clippedClasses(rules);
  const out: string[] = [];
  for (const [file, scan] of SCAN) {
    for (const site of scan.buttons) {
      const ancestors = new Set([
        ...site.ancestors,
        ...(EXTERNAL.get(`${file}::${site.owner}`) ?? []),
      ]);
      // Inside a clipped subtree there is no surface to get wrong.
      if ([...site.ancestors].some((name) => clipped.has(name))) continue;
      const matched = rules.filter((rule) =>
        rule.selector.split(",").some((one) => ruleApplies(one, site.tokens, ancestors)),
      );
      const inherited = GLOBAL_PROPERTIES.filter(
        (property) => !matched.some((rule) => declares(rule.decls, property)),
      );
      if (inherited.length === 0) continue;
      const worn = site.tokens.length === 0 ? "(no class)" : site.tokens.join(" ");
      out.push(`${relative(SRC_DIR, file)}:${site.line} [${worn}] → ${inherited.join(", ")}`);
    }
  }
  return out.sort();
}

const TOTAL_SITES = [...SCAN.values()].reduce((n, scan) => n + scan.buttons.length, 0);
const POSTURE_CSS_PATH = resolve(SRC_DIR, "posture", "posture.css");
const STYLES_CSS = readFileSync(resolve(SRC_DIR, "styles.css"), "utf-8");

describe("no button takes a visual property from the global element rule", () => {
  it("is scanning the package, not an empty tree", () => {
    // Non-vacuity. A walk that finds nothing passes the assertion below for the
    // worst possible reason, and that is how a guard becomes decoration.
    expect(TOTAL_SITES).toBeGreaterThan(100);
    expect(CSS_FILES.length).toBeGreaterThan(20);
    // And the rule it is about must still exist: if the bare `button` rule ever
    // stops declaring these, this file is guarding nothing.
    const bare = flattenRules(STYLES_CSS).find((rule) => rule.selector === "button");
    expect(bare, "styles.css no longer has a bare `button` rule").toBeDefined();
    for (const property of GLOBAL_PROPERTIES) expect(declares(bare!.decls, property)).toBe(true);
  });

  it("MUTATION `every-button-names-its-own-surface`: no site inherits", () => {
    expect(offendingSites()).toEqual([]);
  });
});

describe("the guard bites — and bites the defect that shipped", () => {
  it("MUTATION `the-ne-ann-regression-is-detected`: removing its border fails this", () => {
    // THE ACTUAL DEFECT, replayed against the real stylesheet. `.ne-ann` shipped
    // with `background` and no `border`, so the tile's outline came from the
    // element rule and moved when the element rule moved. Delete the
    // declaration the fix added and this must report it — by name, by property.
    const real = readFileSync(POSTURE_CSS_PATH, "utf-8");
    const mutated = real.replace(/(\.ne-ann \{[^}]*?)\n\s*border: none;/, "$1");
    expect(mutated, "the `.ne-ann` border declaration moved — re-point this").not.toBe(real);
    const reported = offendingSites(new Map([[POSTURE_CSS_PATH, mutated]]));
    expect(reported).toHaveLength(1);
    expect(reported[0]).toMatch(/AnnunciatorRow\.tsx:\d+ \[.*\bne-ann\b.*\] → border$/);
  });

  it("MUTATION `the-same-shape-is-detected-in-colour`: so it is not a border rule", () => {
    // The property list is not decoration either. The second finding of the
    // audit was `.ag-hist__item`, which set background and border and left
    // COLOUR to the element rule, invisibly — every word on that row sits in a
    // span with its own colour. Invisible today is the point: it is the same
    // dependency, one markup change away from being visible.
    const path = resolve(SRC_DIR, "agents", "agents.css");
    const real = readFileSync(path, "utf-8");
    const mutated = real.replace(/(\.ag-hist__item \{[^}]*?)\n\s*color: var\(--text\);/, "$1");
    expect(mutated, "the `.ag-hist__item` colour declaration moved").not.toBe(real);
    const reported = offendingSites(new Map([[path, mutated]]));
    expect(reported).toHaveLength(1);
    expect(reported[0]).toMatch(/WorkspaceSidebar\.tsx:\d+ \[.*\bag-hist__item\b.*\] → color$/);
  });

  it("MUTATION `the-global-rule-is-never-the-answer`: it names nothing", () => {
    // If this returned true the whole guard is vacuous — the element rule
    // declares all three, so it would cover every button in the package. This
    // is the single assertion the defect hinged on.
    expect(ruleApplies("button", ["ne-ann"], new Set(["ne-annunciator"]))).toBe(false);
    expect(ruleApplies("button", [], new Set())).toBe(false);
  });
});

describe("the guard spares what is legitimately styled", () => {
  it("a class rule that names the button counts", () => {
    expect(ruleApplies(".ne-ann", ["ne-ann", "ne-ann--fail"], new Set())).toBe(true);
    // A compound needs ALL its classes on the element, or it is a different one.
    expect(ruleApplies(".ne-ann.ne-ann--fail", ["ne-ann"], new Set())).toBe(false);
  });

  it("a scoped BARE button is credited — that is the package's convention", () => {
    // Sixteen bare buttons ship, and every one of them is styled by a
    // descendant rule off a container class. Denying those would make this
    // guard cry wolf sixteen times on day one.
    const anc = new Set(["ne-ontology__tabs"]);
    expect(ruleApplies(".ne-ontology__tabs button", [], anc)).toBe(true);
    expect(
      ruleApplies(".ne-evidence-drawer li > button", [], new Set(["ne-evidence-drawer"])),
    ).toBe(true);
    // …but only where that container is actually in scope.
    expect(ruleApplies(".ne-ontology__tabs button", [], new Set())).toBe(false);
  });

  it("a rule for some OTHER element is not credited to the button", () => {
    expect(ruleApplies(".ne-ann__name", ["ne-ann"], new Set())).toBe(false);
    expect(ruleApplies(".ne-ann span", ["ne-ann"], new Set(["ne-ann"]))).toBe(false);
  });

  it("a state is not the resting surface", () => {
    expect(ruleApplies(".ne-ann:hover", ["ne-ann"], new Set())).toBe(false);
    expect(ruleApplies('.ne-ann[aria-selected="true"]', ["ne-ann"], new Set())).toBe(false);
    expect(ruleApplies(".ne-ann:focus-visible", ["ne-ann"], new Set())).toBe(false);
  });

  it("self-test: the property test reads shorthands, longhands and `none`", () => {
    expect(declares("border: none;", "border")).toBe(true);
    expect(declares("border-color: var(--rule);", "border")).toBe(true);
    expect(declares("background: transparent;", "background")).toBe(true);
    expect(declares("background-color: var(--panel);", "background")).toBe(true);
    expect(declares("color: var(--text);", "color")).toBe(true);
    // A colour word inside another property is not a colour declaration, and
    // `border-radius` is geometry — crediting either would be a false pass.
    expect(declares("border-radius: var(--radius);", "border")).toBe(false);
    expect(declares("box-shadow: inset 2px 0 0 var(--red);", "border")).toBe(false);
    expect(declares("font: 600 12px/17px var(--font-ui);", "color")).toBe(false);
    expect(declares("-webkit-line-clamp: 2;", "color")).toBe(false);
  });

  it("self-test: the parse finds classed and bare buttons, and prose is not markup", () => {
    const scan = scanTsx(
      "fixture.tsx",
      [
        "function Panel() {",
        "  // a sortable header is a <button> INSIDE its <th>",
        '  return <div className="ne-annunciator">',
        '    <button type="button" className={`ne-ann${s === "FAILED" ? " ne-ann--fail" : ""}`}>x</button>',
        "    <button>y</button>",
        '    <div className="not-a-button" />',
        "  </div>;",
        "}",
      ].join("\n"),
    );
    expect(scan.buttons).toHaveLength(2);
    expect(scan.buttons[0]!.tokens.sort()).toEqual(["ne-ann", "ne-ann--fail"]);
    expect(scan.buttons[0]!.line).toBe(4);
    // The real ancestor, from the tree — and NOT the sibling's class.
    expect([...scan.buttons[0]!.ancestors]).toEqual(["ne-annunciator"]);
    expect(scan.buttons[1]!.tokens).toEqual([]);
    expect(scan.buttons[0]!.owner).toBe("Panel");
  });

  it("MUTATION `a-sibling-container-is-not-an-ancestor`: GraphChamber, resolved", () => {
    // TWO REVIEWERS DISAGREED ABOUT THIS FILE and neither was wrong about what
    // they read: GraphChamber renders `.ne-chamber__viewport-controls` (four
    // bare buttons, styled by a descendant rule) AND, further down the SAME
    // component, `.ne-chamber__nodes.ne-visually-hidden` (one bare button per
    // graph node, styled by nothing). A file-scoped ancestor answer credits the
    // viewport controls' rule to the node buttons and reports the file clean; a
    // scan that misses the clip reports a regression on an element that paints
    // no pixels. Both halves are asserted here so neither answer can come back.
    const scan = SCAN.get(resolve(SRC_DIR, "knowledge", "GraphChamber.tsx"))!;
    const node = scan.buttons.find((site) => site.ancestors.has("ne-chamber__nodes"));
    expect(node, "the per-node accessibility button moved").toBeDefined();
    expect(node!.ancestors.has("ne-chamber__viewport-controls")).toBe(false);
    // …and it is exempt because it is CLIPPED, a fact read out of the cascade.
    const clipped = clippedClasses(rulesOf());
    expect(clipped.has("ne-visually-hidden")).toBe(true);
    expect([...node!.ancestors].some((name) => clipped.has(name))).toBe(true);
    // The exemption is only as good as the clip. `.ne-btn` is not clipped, so
    // this cannot become a blanket excuse.
    expect(clipped.has("ne-btn")).toBe(false);
    expect(clippedClasses([{ selector: ".x", decls: "position: absolute;" }])).toEqual(new Set());
  });

  it("self-test: the class-token convention the literal scrape leans on holds", () => {
    // If a stylesheet ever names an upper-case-initial class, `isClassToken`
    // starts dropping a real class and this guard goes blind to that element.
    const defined = CSS_FILES.flatMap((file) => [
      ...readFileSync(file, "utf-8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .matchAll(/\.(-?[A-Za-z_][A-Za-z0-9_-]*)/g),
    ]).map((m) => m[1]!);
    expect(defined.length).toBeGreaterThan(500);
    expect([...new Set(defined.filter((name) => !isClassToken(name)))].sort()).toEqual([]);
    expect(isClassToken("FAILED")).toBe(false);
    expect(isClassToken("ne-ann--fail")).toBe(true);
  });

  it("self-test: @media rules are flattened, comments are not rules", () => {
    const rules = flattenRules(
      "@media (max-width: 900px) { .ne-ann { border: none } } /* .ne-fake { border: none } */",
    );
    expect(rules.map((r) => r.selector)).toEqual([".ne-ann"]);
  });
});
