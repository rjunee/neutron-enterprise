// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// TRANSPORT-PROVENANCE GUARDIAN. Proves, by construction rather than by
// enumeration, that the console's client-composed transport string cannot reach
// a rendered slot.
//
// WHY THIS FILE EXISTS. ApiError's only string was the client's own — "request
// to /v1/approvals/8f3a…/decision failed with HTTP 502" — and twenty call sites
// across ten screens each wrote `err.detail ?? err.message`. Against a governed
// refusal that reads fine, because the server sends a `detail`; against a
// gateway 502, a proxy timeout or an HTML error page there is no `detail` and
// the developer string is what the room reads, in the approval decision pane,
// at the moment the audience is watching an approval go through. Every one of
// those twenty sites was individually defensible and collectively the same bug.
//
// THE RULE IS DERIVED, NOT LISTED. Enumerating today's render sites would rot
// on the twenty-first. Instead:
//
//   (1) BEHAVIOURAL. Across a status matrix, every transport error's `message`
//       and `userMessage` are house sentences — no endpoint path, no status
//       code, no transport grammar. This is the structural half: a site that
//       reaches `.message` out of habit, as all twenty did, now gets a sentence
//       a reader can read. There is no property left holding the raw string
//       that a render site would reach for by accident.
//   (2) THE SEAM IS THE ONLY AUTHOR. Both classes' constructors must derive
//       `message` from transportUserMessage() — so a new construction site
//       anywhere cannot opt out, and reverting the seam fails here.
//   (3) SHIPPED SOURCES MAY NOT NAME `developerMessage`. The raw string has
//       exactly one home and only src/api/ may touch it. Deliberately rendering
//       it is the one remaining way back to the defect, and this closes it.
//   (4) SHIPPED SOURCES MAY NOT COMPOSE THE GRAMMAR THEMSELVES. No file outside
//       src/api/ may build a "… failed with HTTP …" string of its own.
//   (5) A COMPOSED TRANSPORT STRING GOES TO A SPLITTING CLASS OR NOWHERE —
//       and this rule scans src/api/ TOO. Being inside the seam was never a
//       licence to throw a raw string; the seam is where the SENTENCE is
//       written. src/api/runtimeConfig.ts threw a plain `Error` reading
//       "runtime config fetch failed: HTTP 500", AppBootstrap renders that
//       `message` full-viewport above a Retry button, and rules (1)–(4) were
//       all blind to it: (1)–(2) only know the classes they name, and (3)–(4)
//       skip the seam entirely. The hole was the EXCLUSION, so the fix is a
//       rule without one. Two bites: handing a composed transport string to any
//       constructor that is not a splitting class is a defect ANYWHERE (`new
//       Error` above all), and inside the seam composing one outside a
//       splitting construction is a defect even with no constructor in sight.
//
//   (6) A REJECTION NEEDS NO CONSTRUCTOR TO GET OUT. Rule (5) matches a
//       composed string being HANDED to the wrong constructor — and the next
//       two escapes had no string and no constructor at all. `fetchRuntimeConfig`
//       left its `await fetch(...)` and its `await res.json()` bare, so a
//       dropped network put the engine's "Failed to fetch" on the bootstrap
//       page full-viewport, and a 200 carrying an HTML error page put V8's
//       "Unexpected token '<', \"<!doctype \"... is not valid JSON" there.
//       Rules (1)–(5) are ALL blind to that: there is no syntax to match.
//       So this rule matches the UNGUARDED AWAIT itself, and it finds the
//       functions to check rather than listing them — a source that renders an
//       error's own `.message` WITHOUT narrowing to a splitting class is a
//       verbatim render site (AppBootstrap is one), and every seam function it
//       imports must guard every network/parse await with a `try` whose
//       `catch` throws a splitting class or nothing. Add a render site, or
//       point one at a new reader, and the rule follows it.
//   (7) THE SERVER'S SLOT TAKES THE SERVER'S WORDS. `TurnFailure.detail` is
//       documented "the server's VERBATIM words" and is disclosed under
//       "what the server said" — so a string that is the CONSOLE'S is a
//       forgery there, and since rule (1) made `message` a house sentence,
//       `err.message` is now exactly that. The poll loop's `detailOf` returned
//       it: an ApiError(503) disclosed "The server is unavailable." as the
//       server's own account, and a TypeError disclosed "Failed to fetch".
//       The rule takes its scope from the documentation, not from a list: find
//       the module that declares the VERBATIM slot, and in ITS tree no source
//       may read `.message`/`.userMessage` off anything. Server words come
//       from `.detail`; the console's own lines come from the copy module.
//
// Scope: rules (1)–(4) read production .ts/.tsx under src/, EXCLUDING
// src/test/** and *.test.* — this file must be able to spell the banned
// identifiers to test itself — and EXCLUDING src/api/**, which is the seam
// those rules are about. Rule (5) reads EVERY production source, seam included.
// Rule (6) reads the seam functions a verbatim render site imports. Rule (7)
// reads the tree that owns the VERBATIM slot.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { SseError } from "../api/sse";
import { RuntimeConfigError } from "../api/runtimeConfig";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");
const API_DIR = join(SRC_DIR, "api");

/** Production sources under src/, minus the test tree, the tests, and the seam. */
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
    if (/\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

/** Every production source, seam included — rule (5)'s scope. */
const allSources = scannedSources(SRC_DIR);

const sources = allSources.filter((f) => !f.startsWith(API_DIR + sep));

/**
 * THE TRANSPORT-ERROR CLASSES: every class whose `message` is a reader's
 * sentence and whose raw string is on the developer slot. Rule (2) proves each
 * one derives that sentence from the seam helper; rule (5) requires a composed
 * transport string to be one of these constructors' first argument and nothing
 * else. A new transport client adds its class here or fails rule (5).
 */
const SPLITTING_CLASSES = ["ApiError", "SseError", "RuntimeConfigError"] as const;

/**
 * The narrow, unambiguous shape of a CLIENT-COMPOSED transport string: the
 * grammar itself, or an HTTP code interpolated into one.
 *
 * Deliberately narrower than DEVELOPER_GRAMMAR, which rule (5) cannot use: its
 * bare-status-code arm matches every `status === 503` in the seam, and rule (5)
 * scans the seam.
 */
const COMPOSED_GRAMMAR = /failed with HTTP|HTTP \$\{|HTTP \d{3}/gi;

/** Source with whole-line and block comments removed. A docstring may SPELL the
 * grammar — this file's own header does, and so does the seam's — because
 * describing the defect is not committing it. Only composed code is scanned. */
function codeOnly(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^[ \t]*\/\/.*$/gm, "");
}

/** The class being constructed immediately before `index`, if any. Looks back a
 * short way only: a composed string is an ARGUMENT, so its constructor is a few
 * characters behind it, on the same line or the one above. */
function constructorBefore(text: string, index: number): string | null {
  const window = text.slice(Math.max(0, index - 200), index);
  const calls = [...window.matchAll(/new\s+([A-Za-z_$][\w$]*)\s*\(/g)];
  const last = calls.at(-1);
  return last === undefined ? null : (last[1] ?? null);
}

/** Comments blanked but every byte kept, so an index into the result is an
 * index into the file and a reported line number is the file's own. */
function codeOnlyKeepingLines(text: string): string {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "))
    .replace(/^[ \t]*\/\/.*$/gm, (m) => " ".repeat(m.length));
}

function lineAt(text: string, index: number): number {
  return text.slice(0, index).split("\n").length;
}

/** The index of the `}` closing the `{` at `open`, or null. */
function matchBrace(text: string, open: number): number | null {
  let depth = 0;
  for (let i = open; i < text.length; i += 1) {
    if (text[i] === "{") depth += 1;
    else if (text[i] === "}") {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return null;
}

/** The body of `export async function <name>` as an index range. */
function functionBody(text: string, name: string): { start: number; end: number } | null {
  const head = new RegExp(`export\\s+async\\s+function\\s+${name}\\s*\\(`).exec(text);
  if (head === null) return null;
  const open = text.indexOf("{", head.index + head[0].length);
  if (open === -1) return null;
  const end = matchBrace(text, open);
  return end === null ? null : { start: open, end };
}

/**
 * Ranges a foreign rejection cannot escape from: a `try` whose `catch` throws
 * only splitting classes, or throws nothing at all. A bare rethrow (`throw
 * error`) is NOT one of them — that is the unguarded await with extra steps.
 */
function guardedRanges(text: string, from: number, to: number): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  const tries = /\btry\s*\{/g;
  tries.lastIndex = from;
  for (let m = tries.exec(text); m !== null && m.index < to; m = tries.exec(text)) {
    const open = text.indexOf("{", m.index);
    const close = matchBrace(text, open);
    if (close === null) continue;
    const after = text.slice(close + 1, close + 400);
    if (!/^\s*catch\s*(?:\([^)]*\)\s*)?\{/.test(after)) continue;
    const catchOpen = close + 1 + after.indexOf("{");
    const catchClose = matchBrace(text, catchOpen);
    if (catchClose === null) continue;
    const thrown = [
      ...text.slice(catchOpen, catchClose).matchAll(/throw\s+(?:new\s+)?([A-Za-z_$][\w$]*)/g),
    ];
    if (thrown.every((x) => (SPLITTING_CLASSES as readonly string[]).includes(x[1] ?? ""))) {
      out.push([open, close]);
    }
  }
  return out;
}

/** The awaits whose rejection is somebody else's exception: the network's and
 * the body parser's. Neither has a string this console wrote, and neither has
 * a constructor for rule (5) to find. */
const RISKY_AWAIT: ReadonlyArray<{ what: string; re: string }> = [
  { what: "await fetch(…)", re: "await\\s+(?:globalThis\\.)?fetch\\s*\\(" },
  {
    what: "await <response>.json()/.text()",
    re: "await\\s+[A-Za-z_$][\\w$]*\\s*\\.\\s*(?:json|text|arrayBuffer|blob)\\s*\\(",
  },
];

/** An error's own sentence, read off a receiver. `message` is the CONSOLE's
 * house sentence for a splitting class and the ENGINE's words for anything
 * else — never the server's, which is `detail`. */
const ERROR_SENTENCE_READ = /(?<![.\w$])[A-Za-z_$][\w$]*\s*\.\s*(?:message|userMessage)\b/g;

/** A source that renders an error's own sentence without narrowing to a
 * splitting class first — so whatever rejects at it is what a reader reads. */
function rendersRawErrorSentence(text: string): boolean {
  for (const m of text.matchAll(/(?<![.\w$])([A-Za-z_$][\w$]*)\s*\.\s*message\b/g)) {
    const ident = m[1] ?? "";
    const before = text.slice(Math.max(0, m.index - 200), m.index);
    const narrowed = SPLITTING_CLASSES.some((cls) =>
      new RegExp(`\\b${ident}\\s+instanceof\\s+${cls}\\b`).test(before),
    );
    if (!narrowed) return true;
  }
  return false;
}

/** Every `{ name, seamFile }` a source imports out of the seam. Resolved from
 * the import specifier, so pointing a render site at a different reader moves
 * the rule with it. */
function seamImports(file: string, text: string): Array<{ seamFile: string; name: string }> {
  const out: Array<{ seamFile: string; name: string }> = [];
  for (const m of text.matchAll(/import\s+(?:type\s+)?\{([^}]*)\}\s*from\s*["']([^"']+)["']/g)) {
    const spec = m[2] ?? "";
    if (!spec.startsWith(".")) continue;
    const base = resolve(dirname(file), spec);
    if (!base.startsWith(API_DIR + sep)) continue;
    const seamFile = [".ts", ".tsx"].map((e) => base + e).find((f) => allSources.includes(f));
    if (seamFile === undefined) continue;
    for (const clause of (m[1] ?? "").split(",")) {
      const name = (
        clause
          .trim()
          .replace(/^type\s+/, "")
          .split(/\s+as\s+/)[0] ?? ""
      ).trim();
      if (name !== "") out.push({ seamFile, name });
    }
  }
  return out;
}

/** The documentation the VERBATIM slot carries, and the label the screen puts
 * on it. Rule (7) takes its scope from these rather than from a file list. */
const VERBATIM_MARK = "VERBATIM words";
const SERVER_DISCLOSURE = 'summary="what the server said"';

/**
 * The shapes a client-composed transport string takes. Derived from the grammar
 * ("<verb phrase> failed with HTTP <code>"), an endpoint path, or a bare status
 * code — never from the current wording of any one of them.
 */
const DEVELOPER_GRAMMAR: ReadonlyArray<{ what: string; re: RegExp }> = [
  { what: "transport grammar", re: /failed with HTTP/i },
  { what: "an endpoint path", re: /\/v\d+\//i },
  { what: "an HTTP status code", re: /\bHTTP\b|\b[45]\d{2}\b/ },
];

/** Every status class the console can be handed, including the impossible ones. */
const STATUS_MATRIX = [0, 400, 401, 403, 404, 408, 409, 418, 422, 429, 500, 502, 503, 504] as const;

/** The exact strings the seam composes today, for the behavioural sweep. */
function developerStrings(status: number): string[] {
  return [
    `request to /v1/approvals/8f3a-decision failed with HTTP ${status}`,
    `request to /v1/audit/export failed with HTTP ${status}`,
    `stream failed with HTTP ${status}`,
  ];
}

describe("transport-provenance guardian", () => {
  it("finds production sources to scan, and excludes the test tree and the seam", () => {
    expect(sources.length).toBeGreaterThan(50);
    const rel = sources.map((f) => relative(SRC_DIR, f));
    expect(rel.some((f) => f.startsWith(`test${sep}`))).toBe(false);
    expect(rel.some((f) => f.startsWith(`api${sep}`))).toBe(false);
    // The screens the defect shipped on are IN scope.
    expect(rel).toContain(join("approvals", "ApprovalsPage.tsx"));
    expect(rel).toContain(join("approvals", "DecisionPane.tsx"));
  });

  it("(1) no transport error's rendered strings carry developer provenance", () => {
    const offenders: string[] = [];
    for (const status of STATUS_MATRIX) {
      for (const developer of developerStrings(status)) {
        for (const served of [undefined, "not in the Tier-3 named-approver list", "", "   "]) {
          for (const err of [
            new ApiError(developer, status, served),
            new SseError(developer, status, served),
            new RuntimeConfigError(developer, status, served),
          ]) {
            for (const [slot, value] of [
              ["message", err.message],
              ["userMessage", err.userMessage],
            ] as const) {
              if (value.trim() === "") {
                offenders.push(`${err.name}(${status}).${slot} is empty`);
                continue;
              }
              for (const { what, re } of DEVELOPER_GRAMMAR) {
                if (re.test(value)) {
                  offenders.push(`${err.name}(${status}).${slot} carries ${what}: ${value}`);
                }
              }
            }
            // The raw string is not discarded — telemetry still has it.
            expect(err.developerMessage).toBe(developer);
          }
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("(1b) the sentence for a status with no served detail is the house grammar", () => {
    const offenders: string[] = [];
    for (const status of STATUS_MATRIX) {
      const sentence = new ApiError(`x failed with HTTP ${status}`, status).userMessage;
      // One clause, ends in a full stop, short enough to read at a glance.
      if (!/\.$/.test(sentence)) offenders.push(`${status}: no full stop — ${sentence}`);
      if (sentence.split(/\s+/).length > 12) offenders.push(`${status}: too long — ${sentence}`);
      if (!/^[A-Z]/.test(sentence)) offenders.push(`${status}: not a sentence — ${sentence}`);
    }
    expect(offenders).toEqual([]);
    // The server's own sentence is preferred to all of them, verbatim.
    const served = "not in the Tier-3 named-approver list";
    expect(new ApiError("x failed with HTTP 403", 403, served).userMessage).toBe(served);
    expect(new SseError("stream failed with HTTP 403", 403, served).userMessage).toBe(served);
    expect(new RuntimeConfigError("x failed with HTTP 403", 403, served).userMessage).toBe(served);
  });

  it("(2) both transport error classes derive `message` from the one seam helper", () => {
    const offenders: string[] = [];
    for (const [file, cls] of [
      ["client.ts", "ApiError"],
      ["sse.ts", "SseError"],
      ["runtimeConfig.ts", "RuntimeConfigError"],
    ] as const) {
      expect(SPLITTING_CLASSES).toContain(cls);
      const text = readFileSync(join(API_DIR, file), "utf-8");
      const body = new RegExp(`export class ${cls} extends Error \\{[\\s\\S]*?\\n\\}`).exec(text);
      if (body === null) {
        offenders.push(`${file}: ${cls} class body not found`);
        continue;
      }
      const superCall = /super\(([^)]*)\)/.exec(body[0])?.[1];
      if (superCall === undefined) {
        offenders.push(`${file}: ${cls} has no super() call`);
        continue;
      }
      if (!/^transportUserMessage\(/.test(superCall.trim())) {
        offenders.push(`${file}: ${cls} passes \`${superCall}\` to super(), not the seam helper`);
      }
      if (!body[0].includes("readonly userMessage: string;")) {
        offenders.push(`${file}: ${cls} has no always-populated userMessage`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("(3) no shipped source outside the seam names the raw developer string", () => {
    const banned = "developer" + "Message";
    const offenders = sources
      .filter((f) => readFileSync(f, "utf-8").includes(banned))
      .map((f) => relative(SRC_DIR, f));
    expect(offenders).toEqual([]);
  });

  it("(4) no shipped source outside the seam composes the transport grammar", () => {
    const offenders: string[] = [];
    for (const file of sources) {
      const text = readFileSync(file, "utf-8");
      if (/failed with HTTP/i.test(text)) offenders.push(relative(SRC_DIR, file));
    }
    expect(offenders).toEqual([]);
  });

  it("(5) a composed transport string is a splitting class's argument, or it is a defect", () => {
    const offenders: string[] = [];
    for (const file of allSources) {
      const text = codeOnly(readFileSync(file, "utf-8"));
      const inSeam = file.startsWith(API_DIR + sep);
      for (const match of text.matchAll(COMPOSED_GRAMMAR)) {
        const cls = constructorBefore(text, match.index);
        if (cls !== null && (SPLITTING_CLASSES as readonly string[]).includes(cls)) continue;
        // Outside the seam, a grammar match with no constructor at all is
        // rendered prose, which is rules (3) and (4)'s ground and is judged
        // there. This rule is about where a COMPOSED string is handed.
        if (!inSeam && cls === null) continue;
        const line = text.slice(0, match.index).split("\n").length;
        offenders.push(`${relative(SRC_DIR, file)}:${line} -> ${cls ?? "no constructor"}`);
      }
    }
    expect(offenders).toEqual([]);
    // The seam IS in scope for this one — that is the whole point of it.
    expect(allSources.some((f) => f.startsWith(API_DIR + sep))).toBe(true);
  });

  it("(6) a seam read a viewer reads verbatim guards every network and parse await", () => {
    const offenders: string[] = [];
    const checked: string[] = [];
    for (const file of sources) {
      const text = readFileSync(file, "utf-8");
      if (!rendersRawErrorSentence(codeOnlyKeepingLines(text))) continue;
      for (const { seamFile, name } of seamImports(file, text)) {
        const seam = codeOnlyKeepingLines(readFileSync(seamFile, "utf-8"));
        const body = functionBody(seam, name);
        // Not an exported async function — a class, a type, a constant. The
        // rule is about what a read can REJECT with.
        if (body === null) continue;
        checked.push(`${relative(SRC_DIR, seamFile)}:${name}`);
        const guarded = guardedRanges(seam, body.start, body.end);
        for (const { what, re } of RISKY_AWAIT) {
          const scan = new RegExp(re, "g");
          scan.lastIndex = body.start;
          for (let m = scan.exec(seam); m !== null && m.index < body.end; m = scan.exec(seam)) {
            if (guarded.some(([a, b]) => m.index > a && m.index < b)) continue;
            offenders.push(
              `${relative(SRC_DIR, seamFile)}:${lineAt(seam, m.index)} ${what} is unguarded, and ` +
                `${relative(SRC_DIR, file)} renders whatever it rejects with`,
            );
          }
        }
      }
    }
    expect(offenders).toEqual([]);
    // An empty sweep proves nothing: the rule must have found the bootstrap's
    // reader, which is the one that put "Failed to fetch" on the first screen.
    expect(checked).toContain(`${join("api", "runtimeConfig.ts")}:fetchRuntimeConfig`);
  });

  it("(7) nothing in the VERBATIM slot's own tree reads an error's own sentence", () => {
    // SCOPE FROM THE DOCUMENTATION. The module that declares the slot as the
    // server's verbatim words owns the tree this rule reads.
    const owner = sources.find((f) => readFileSync(f, "utf-8").includes(VERBATIM_MARK));
    expect(owner === undefined ? "no VERBATIM slot found" : owner).not.toBe(
      "no VERBATIM slot found",
    );
    const tree = dirname(owner ?? SRC_DIR);
    // …and the slot really is ATTRIBUTED on screen — that is what makes the
    // console's own sentence a forgery in it rather than merely a wrong word.
    const attributing = sources.filter((f) => readFileSync(f, "utf-8").includes(SERVER_DISCLOSURE));
    expect(attributing.map((f) => relative(SRC_DIR, f)).length).toBeGreaterThan(0);
    expect(attributing.every((f) => f.startsWith(tree + sep))).toBe(true);

    const offenders: string[] = [];
    const inTree = sources.filter((f) => f.startsWith(tree + sep));
    expect(inTree.length).toBeGreaterThan(1);
    for (const file of inTree) {
      const text = codeOnlyKeepingLines(readFileSync(file, "utf-8"));
      for (const m of text.matchAll(ERROR_SENTENCE_READ)) {
        offenders.push(`${relative(SRC_DIR, file)}:${lineAt(text, m.index)} -> ${m[0].trim()}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("(8) the control — every rule bites on the exact shape that shipped", () => {
    // Verbatim what the old ApiError put on `message`, and what the decision
    // pane rendered under "Couldn't submit the decision. Try again.".
    const shipped = "request to /v1/approvals/8f3a-decision failed with HTTP 502";
    expect(DEVELOPER_GRAMMAR.some(({ re }) => re.test(shipped))).toBe(true);
    // …and all three arms of the grammar are independently live, so rule (1) is
    // not resting on one of them.
    for (const { what, re } of DEVELOPER_GRAMMAR) {
      expect([what, re.test(shipped)]).toEqual([what, true]);
    }
    // Rule (3) bites on a render of the raw property.
    const regression = `{err.${"developer" + "Message"}}`;
    expect(regression.includes("developer" + "Message")).toBe(true);
    // The sentence that replaced it passes every rule.
    const fixed = new ApiError(shipped, 502).userMessage;
    expect(fixed).toBe("The server is unavailable.");
    expect(DEVELOPER_GRAMMAR.some(({ re }) => re.test(fixed))).toBe(false);

    // RULE (5)'S CONTROL: the exact plain-Error throw that hid inside the seam,
    // and the shape that replaces it. Written as text rather than run, because
    // running it is what rule (5) does to every source.
    const hid = "throw new Error(`runtime config fetch failed: HTTP ${res.status}`);";
    const grammar = new RegExp(COMPOSED_GRAMMAR.source, "gi");
    const hidMatch = [...hid.matchAll(grammar)];
    expect(hidMatch.length).toBe(1);
    expect(constructorBefore(hid, hidMatch[0]?.index ?? 0)).toBe("Error");
    expect((SPLITTING_CLASSES as readonly string[]).includes("Error")).toBe(false);
    const repaired =
      "throw new RuntimeConfigError(`runtime config fetch failed: HTTP ${res.status}`, res.status);";
    const repairedMatch = [...repaired.matchAll(new RegExp(COMPOSED_GRAMMAR.source, "gi"))];
    expect(constructorBefore(repaired, repairedMatch[0]?.index ?? 0)).toBe("RuntimeConfigError");
    // …and the bootstrap page's viewer reads a sentence, not that string.
    expect(new RuntimeConfigError(hid, 500).userMessage).toBe("The server is unavailable.");

    // RULE (6)'S CONTROL: the two bare awaits that shipped, and the guarded
    // shape that replaces them. Run through the rule's own machinery.
    const bare = [
      "export async function read(): Promise<void> {",
      '  const res = await fetch("/config.json");',
      "  const raw = await res.json();",
      "  return raw;",
      "}",
    ].join("\n");
    const guardedSource = [
      "export async function read(): Promise<void> {",
      "  let res: Response;",
      "  try {",
      '    res = await fetch("/config.json");',
      "  } catch {",
      '    throw new RuntimeConfigError("rejected", 0);',
      "  }",
      "  let raw: unknown;",
      "  try {",
      "    raw = await res.json();",
      "  } catch {",
      '    throw new RuntimeConfigError("unreadable", res.status);',
      "  }",
      "  return raw;",
      "}",
    ].join("\n");
    const bite = (src: string): number => {
      const body = functionBody(src, "read");
      expect(body).not.toBeNull();
      if (body === null) return -1;
      const guarded = guardedRanges(src, body.start, body.end);
      let hits = 0;
      for (const { re } of RISKY_AWAIT) {
        const scan = new RegExp(re, "g");
        scan.lastIndex = body.start;
        for (let m = scan.exec(src); m !== null && m.index < body.end; m = scan.exec(src)) {
          if (!guarded.some(([a, b]) => m.index > a && m.index < b)) hits += 1;
        }
      }
      return hits;
    };
    expect(bite(bare)).toBe(2);
    expect(bite(guardedSource)).toBe(0);
    // A catch that rethrows the engine's own error is not a guard.
    expect(
      bite(guardedSource.replace('throw new RuntimeConfigError("rejected", 0);', "throw e;")),
    ).toBe(1);
    // And the render site that made those two awaits a viewer's problem is
    // found by its shape, not by its name.
    expect(rendersRawErrorSentence("err instanceof Error ? err.message : String(err)")).toBe(true);
    expect(rendersRawErrorSentence("err instanceof ApiError ? err.message : null")).toBe(false);

    // RULE (7)'S CONTROL: the exact line the poll loop shipped, and what it
    // disclosed under "what the server said" for the two throws a reviewer fed
    // it. Both strings are forgeries in that slot; neither is the server's.
    const shippedDetail = 'return error instanceof Error ? error.message : "run read failed";';
    expect([...shippedDetail.matchAll(ERROR_SENTENCE_READ)].length).toBe(1);
    expect(new ApiError("x failed with HTTP 503", 503).message).toBe("The server is unavailable.");
    expect(new ApiError("x failed with HTTP 503", 503).detail).toBeUndefined();
    expect(new TypeError("Failed to fetch").message).toBe("Failed to fetch");
    // The repair reads the one slot that IS the server's, and is empty when the
    // server said nothing.
    const repairedDetail =
      'const transport = e instanceof ApiError ? e : null;\nreturn transport?.detail ?? "";';
    expect([...repairedDetail.matchAll(ERROR_SENTENCE_READ)].length).toBe(0);
  });
});
