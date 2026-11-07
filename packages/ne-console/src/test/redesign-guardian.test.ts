// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// REDESIGN GUARDIAN (§7 acceptance checks). An fs-walk of the production source
// proves, by construction, the structural rules the redesign rests on:
//
//   (1) No empty-checkbox glyph anywhere. `☐ AIR_GAP` / `☐ SANCTIONED EGRESS` /
//       `☐ REQUIRES OPS KEY` read as OFF or FAILING while signifying the
//       opposite. Every state goes through <StatePill> — glyph plus word.
//   (2) Every <Readout> call site supplies a non-empty `source`. A value with no
//       source line is a bug (L1).
//   (3) No FRONTIER/PRIVATE lane-switch control exists. `ApiClient` has no lane
//       parameter and no setLane, and /v1/posture reports lanes read-only —
//       shipping such a control would ship a fabricated CAPABILITY, which is
//       worse than a fabricated number because it survives the demo and fails in
//       the pilot. The real router proof is POST /v1/demo/delegate + /trace.
//   (4) The forbidden claims never appear in production source.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");

function productionSources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "test") continue;
      out.push(...productionSources(full));
      continue;
    }
    if (/\.test\.tsx?$/.test(entry)) continue;
    if (/\.(tsx?|css)$/.test(entry)) out.push(full);
  }
  return out;
}

const sources = productionSources(SRC_DIR);
const rel = (f: string) => relative(SRC_DIR, f);

/** Strip // line comments and block comments. The rules below govern what the
 * console RENDERS; a doc-comment naming a banned glyph or phrase in order to
 * forbid it must not trip its own guardian. */
function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const bodies = new Map(sources.map((f) => [f, stripComments(readFileSync(f, "utf-8"))]));
const body = (f: string) => bodies.get(f) ?? "";

/** Assembled so this guardian's own body never matches the walk it performs. */
const EMPTY_CHECKBOX = String.fromCodePoint(0x2610);

/**
 * Every `<Readout …/>` element's prop block. Prettier closes a multi-line
 * self-closing JSX element with `/>` at the SAME indentation as its opening
 * tag, so that is the terminator — nested `<StatePill … />` inside a `value`
 * prop is always more deeply indented and never mistaken for the end.
 */
export function readoutPropBlocks(text: string): string[] {
  const out: string[] = [];
  const re = /^([ \t]*)(?:.*?)<Readout(?=[\s>])/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const indent = m[1] ?? "";
    const rest = text.slice(re.lastIndex);
    const close = new RegExp(`^${indent}/>`, "m");
    const oneLine = rest.match(/^[^\n]*\/>/);
    const multi = rest.match(close);
    if (oneLine) out.push(oneLine[0]);
    else if (multi && multi.index !== undefined) out.push(rest.slice(0, multi.index));
    else out.push(rest.slice(0, 800));
  }
  return out;
}

describe("redesign guardian", () => {
  it("finds production sources to scan", () => {
    expect(sources.length).toBeGreaterThan(20);
  });

  it("(1) no empty-checkbox glyph is used as a status indicator", () => {
    const offenders = sources.filter((f) => body(f).includes(EMPTY_CHECKBOX)).map(rel);
    expect(offenders).toEqual([]);
  });

  // RULES (2) and (2b) DELETED with the primitive they guarded.
  //
  // Rule (2) required every <Readout> to carry a non-empty source. The redesign
  // demolishes the Readout hero strips entirely (spec §1.4, §20), so after the
  // rebuild there were ZERO call sites and the rule guarded nothing.
  //
  // Its self-test (2b) — "the scan actually finds the call sites it audits" —
  // is what caught this: it asserted the scan saw >10 call sites and went RED
  // when the count hit zero. That is the self-test doing precisely its job.
  // Deleting (2b) alone to get green would have left a rule that passes because
  // it examines nothing, which reads as coverage and is worse than no rule.
  // Both go, together with the primitive.

  it("(3) no lane-switch control: nothing offers to change the model lane", () => {
    // The wire has no such endpoint. A control with no endpoint behind it is a
    // fabricated capability (L3).
    const bad: string[] = [];
    for (const f of sources) {
      if (/\.setLane\s*\(|\bsetLane\s*[=:]|\bswitchLane\b|\blaneToggle\b/.test(body(f))) {
        bad.push(`${rel(f)} :: lane-switch handler`);
      }
    }
    expect(bad).toEqual([]);
    // And the one client seam declares no lane parameter.
    const client = stripComments(readFileSync(join(SRC_DIR, "api", "client.ts"), "utf-8"));
    expect(client).not.toMatch(/lane\s*[?:]\s*string/);
  });

  it("(4) the forbidden claims appear in no production source", () => {
    const banned = [
      "100% accurate",
      "no hallucination",
      "you own the stack",
      "own the stack",
      "no ongoing costs",
      "no per-seat license",
    ];
    const bad: string[] = [];
    for (const f of sources) {
      const text = body(f).toLowerCase();
      for (const phrase of banned) {
        if (text.includes(phrase)) bad.push(`${rel(f)} :: "${phrase}"`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("(5) the chat answer surface carries NO raw error string", () => {
    // REVIEW ROUND 1, the most-cited blocker. `useAgentStream` assigned
    // `error: event.detail` and `MessageList` rendered `{message.error}` inside a
    // bare div, so a Python exception class name — `RedactionError` — became the
    // assistant's answer: no legend, no state, no source, no remedy, and a policy
    // refusal indistinguishable from a system fault.
    //
    // Scoped DELIBERATELY to the chat turn, not to the word "detail" everywhere:
    // `verification.detail` is a server-authored sentence and `StatePill`'s
    // `detail` prop is a UI label. The rule is about the ANSWER SURFACE.
    const stream = stripComments(readFileSync(join(SRC_DIR, "chat", "useAgentStream.ts"), "utf-8"));
    // The classifier must stand between the wire and the message.
    expect(stream).toMatch(/classifyTurnFailure\(/);
    // …and nothing may put the raw detail onto the message.
    expect(stream).not.toMatch(/error:\s*event\.detail/);
    expect(stream).not.toMatch(/\berror\?:\s*string/);

    const list = stripComments(readFileSync(join(SRC_DIR, "chat", "MessageList.tsx"), "utf-8"));
    expect(list).not.toMatch(/\{\s*message\.error\s*\}/);
    expect(list).toMatch(/TurnFailureReadout/);

    // The classified failure is the only thing the message carries, and the raw
    // detail lives inside it — in the RAW layer, never in the answer bubble.
    const readout = stripComments(
      readFileSync(join(SRC_DIR, "chat", "TurnFailureReadout.tsx"), "utf-8"),
    );
    const detailIdx = readout.indexOf("failure.detail");
    const drawerIdx = readout.indexOf("<EvidenceDrawer");
    expect(detailIdx).toBeGreaterThan(-1);
    expect(drawerIdx).toBeGreaterThan(-1);
    expect(detailIdx).toBeGreaterThan(drawerIdx);
  });

  it("(6) the classifier is the ONE seam that reads a stream error's detail", () => {
    // Structural: if a second call site starts mapping `event.detail` itself, the
    // two will drift and one of them will be the bare-class-name bug again.
    const readers = sources.filter(
      (f) => /\bevent\.detail\b/.test(body(f)) && !f.endsWith("turnFailure.ts"),
    );
    expect(
      readers.map(rel).filter((r) => !r.startsWith("api/")),
      "only chat/turnFailure.ts (via useAgentStream's classify call) may consume event.detail",
    ).toEqual(["chat/useAgentStream.ts"]);
  });

  it("(7) self-test: each scan bites a planted violation and spares a legal one", () => {
    expect(`${EMPTY_CHECKBOX} AIR_GAP`.includes(EMPTY_CHECKBOX)).toBe(true);
    const bare = readoutPropBlocks(`<Readout legend="X" value={1} />`);
    expect(bare).toHaveLength(1);
    expect(/\bsource=/.test(bare[0] ?? "")).toBe(false);
    const nested = readoutPropBlocks(
      [
        "      <Readout",
        '        value={<StatePill word="VALID" />}',
        '        source="x"',
        "      />",
      ].join("\n"),
    );
    expect(nested).toHaveLength(1);
    expect(/\bsource=/.test(nested[0] ?? "")).toBe(true);
    expect(/\.setLane\s*\(/.test("client.setLane('frontier')")).toBe(true);
    expect(stripComments('// never "own the stack"\nconst x = 1;')).not.toContain("own the stack");
    // (5) bites the exact assignment and render the blocker was made of.
    expect(/error:\s*event\.detail/.test("patch((m) => ({ ...m, error: event.detail }))")).toBe(
      true,
    );
    expect(/error:\s*event\.detail/.test("failure: classifyTurnFailure(event.detail)")).toBe(false);
    expect(/\{\s*message\.error\s*\}/.test("<div>{message.error}</div>")).toBe(true);
    expect(
      /\{\s*message\.error\s*\}/.test("<TurnFailureReadout failure={message.failure} />"),
    ).toBe(false);
  });

  // NE-CONSOLE-IA — the competitor comparison table (owner, 2026-07-31: "the
  // whole 'posture' section that lists out the constraints and documents how
  // they are implemented in various python files. What the fuck is that?" —
  // asked about the adjacent boundary section carrying this table, which is a
  // sales slide with a competitor's per-seat pricing shipped inside admin
  // software). DELETED ONCE; this rule keeps it deleted.
  it("(5) the competitor comparison table cannot return: no 'Microsoft 365' and no 'ne-e7' token", () => {
    const offenders: string[] = [];
    for (const f of sources) {
      const text = body(f);
      if (text.includes("Microsoft 365")) offenders.push(`${rel(f)} :: "Microsoft 365"`);
      if (text.includes("ne-e7")) offenders.push(`${rel(f)} :: "ne-e7"`);
    }
    expect(offenders).toEqual([]);
  });

  // NE-CONSOLE-IA round 2 — internal build-ticket ids on customer-facing
  // screens. Same objection as the source-file paths on Posture: an operator
  // cannot act on "NE-133", and a screen that names its own backlog reads as
  // unfinished software. Ten survived the first pass because the two
  // assertions written for it were page-scoped; a repo-wide rule is what
  // actually holds the line, exactly as rule (5) does for the comparison table.
  //
  // COMMENTS ARE EXEMPT (body() strips them) — a ticket id is legitimate
  // provenance for a maintainer reading the source, and illegitimate on screen.
  // Scans ALL non-comment source, exactly like rule (5) — not just string
  // literals. My first draft matched only quoted literals, and its own
  // self-test caught the hole: `<span>AWAITING NE-136</span>` puts the ticket
  // in JSX TEXT, which is not a literal, so the shape that actually shipped
  // would have walked straight through the rule written to ban it.
  it("(6) no internal build-ticket id survives in production source", () => {
    const TICKET = /NE-\d{2,}/;
    const offenders: string[] = [];
    for (const f of sources) {
      const m = body(f).match(TICKET);
      if (m) offenders.push(`${rel(f)} :: "${m[0]}"`);
    }
    expect(offenders).toEqual([]);
  });

  it("(6b) self-test: the rule-(6) scan bites both shapes and spares a comment", () => {
    const TICKET = /NE-\d{2,}/;
    // JSX text — the shape that actually shipped.
    expect(TICKET.test(stripComments('<span className="ne-label">AWAITING NE-136</span>'))).toBe(
      true,
    );
    // String literal — the shape a first draft would catch.
    expect(TICKET.test(stripComments('const flag = "awaiting NE-139";'))).toBe(true);
    // A ticket id in a COMMENT is provenance for a maintainer, not screen text;
    // firing on it would break every honest code comment in the package.
    expect(TICKET.test(stripComments('// NE-136 will fill this in\nconst x = "live";'))).toBe(
      false,
    );
  });

  it("(5b) self-test: the rule-(5) scan bites a planted violation", () => {
    const planted = stripComments(
      '<section className="ne-e7">Microsoft 365 E5 + Agent 365</section>',
    );
    expect(planted.includes("Microsoft 365")).toBe(true);
    expect(planted.includes("ne-e7")).toBe(true);
    // …and spares a legal mention that names neither the banned class nor token.
    const legal = stripComments('<section className="ne-band">Sovereign infrastructure</section>');
    expect(legal.includes("Microsoft 365")).toBe(false);
    expect(legal.includes("ne-e7")).toBe(false);
  });
});
