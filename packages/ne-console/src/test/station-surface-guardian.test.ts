// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// STATION-SURFACE GUARDIAN. The guided tour may only name surfaces this console
// actually has.
//
// WHY THIS FILE EXISTS. Station 1 told the room to "open Ask", and linked to
// `/ask` under the label "Open Ask", for every release after Ask stopped being a
// rail destination (`rail/navSections.ts`: "ASK IS NOT A RAIL DESTINATION ANY
// MORE"). The route still redirects, so nothing 404s and no test failed — but
// this is a customer-facing tour read ALOUD on stage, and the presenter who
// hears "open Ask" scans a sidebar that says Knowledge Layer / Execution /
// Operations / Governance / Insights and finds no such thing. The copy was the
// defect; the rail's removal was deliberate.
//
// THE CLASS, NOT THE INSTANCE. "Ask" is not special. Any rail rename, any tab
// rename, any deleted destination puts the tour back in the same state, and the
// tour is the one file in the product nobody re-reads. So the two vocabularies
// below are DERIVED from the shipped sources — never listed here:
//
//   DESTINATIONS — every label in `NAV_SECTIONS` plus the pinned foot. Rename
//     "Verified Queries" and this set renames with it.
//   PANELS — the in-page tab strips and segmented VIEW controls, harvested from
//     the production sources themselves: `{ id, label }` tab descriptors
//     (`models/ModelsPage.tsx`, `agents/AgentWorkspace.tsx`), segmented view
//     arms (`verified-queries/VerifiedQueriesPage.tsx`), and the computed
//     workspace thread label, which is imported and CALLED for both strategies
//     rather than transcribed.
//
// A PANEL IS NOT A CONTROL. A panel is a pane a reader can BE ON; a control is a
// button a reader PRESSES, and `aria-pressed` on its own is the signature of the
// SECOND. This vocabulary used to admit every literal-text `aria-pressed` button,
// which made the Run timeline's "Follow live" toggle a valid surface and let
// "Open Follow live" pass as a sentence a station could say. A view arm's pressed
// state is a COMPARISON — `view === "browse"`, the current pane against this
// arm's identity — while a toggle's is a bare flag (`followLive`); the comparison
// is what `SEGMENTED_VIEW_BUTTON` requires. See that constant for what the
// derivation still does not see.
//
// A hand-written list of "the real names" would rot exactly the way the station
// copy rotted, which is the whole reason this guardian exists.
//
// WHAT COUNTS AS NAMING A SURFACE. A navigation verb — open / on / in / from /
// switch to / go to — followed by a phrase that begins with a capital. That is
// how a station tells a reader where to go ("open Ask", "On Search", "switch to
// Browse records"), and the capital is what separates a claimed proper name from
// ordinary prose ("open the first row", "put the question in your own words").
// The phrase is matched greedily, up to four words, so sentence-case names like
// "Audit log" and "Browse records" are recognised whole.
//
// WHAT THIS GUARDIAN DOES NOT COVER — stated because the version before this one
// over-claimed, and a false absolute in a guardian is worse than a narrow one:
//   · WHICH FIELDS. Only `path`, `see` and each link's `label` are read
//     (`navigableCopy`). `title`, `role` and `job` are NOT, and deliberately —
//     see that function.
//   · WHICH VERBS. Only `NAV_VERB`'s list. "choose Re-verify" names a control
//     with a verb this does not know, and is not examined.
//   · HOW FAR A NAME EXTENDS. Resolution is by longest real PREFIX, so a phrase
//     that begins with a real surface passes as that surface — see
//     `surfaceClaims`.
// What it DOES establish: no `path`, `see` or link label sends a reader, with one
// of those verbs, to a capitalised name that is neither a shipped rail
// destination nor a shipped panel — and no link label naming a destination
// points at a different destination's route.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { threadTabLabel } from "../agents/AgentWorkspace";
import type { AgentDetailView } from "../agents/types";
import { NAV_FOOT, NAV_SECTIONS } from "../rail/navSections";
import type { NavSection } from "../rail/navSections";
import { STATIONS } from "../demo/stations";
import type { StationDef } from "../demo/stations";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");

// ---------------------------------------------------------------------------
// The derived vocabularies
// ---------------------------------------------------------------------------

/** Every rail destination the console renders, plus the pinned foot. */
function destinationNames(sections: readonly NavSection[]): Map<string, string> {
  const out = new Map<string, string>();
  const add = (label: string, to: string) => out.set(label.toLowerCase(), to);
  for (const section of sections) {
    if (section.items) {
      for (const item of section.items) add(item.label, item.to);
      continue;
    }
    add(section.label, section.to);
  }
  add(NAV_FOOT.label, NAV_FOOT.to);
  return out;
}

const DESTINATIONS = destinationNames(NAV_SECTIONS);

/** Production sources under src/, skipping the test tree and every test file —
 * the same scope the tenant-identity guardian walks. */
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
    if (/\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

/** Comments are where this codebase keeps its history — including the names of
 * surfaces that have been DELETED. Harvesting them would re-admit exactly the
 * vocabulary this guardian exists to reject, so they are stripped first. */
function withoutComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

/** `{ id: "policy", label: "Policy" }` — this codebase's tab descriptor. */
const TAB_DESCRIPTOR = /\{\s*id:\s*"[^"]+"\s*,\s*label:\s*"([^"]+)"/g;

/** A segmented VIEW button: one arm of a control that chooses WHICH PANE is on
 * screen ("Approved queries", "Browse records").
 *
 * `aria-pressed` alone is NOT that signature — it is the signature of a toggle,
 * and this guard's vocabulary must not admit one. A reader can be ON a pane; a
 * reader PRESSES a toggle, and "Open Follow live" is not a sentence any station
 * should be allowed to pass. The two are separated by what the pressed state is
 * computed FROM:
 *
 *   `aria-pressed={view === "browse"}`  — the current view COMPARED against this
 *                                         arm's identity. A pane. Admitted.
 *   `aria-pressed={followLive}`         — a bare boolean flag. A toggle, and the
 *                                         "Follow live" button on the Run
 *                                         timeline is exactly this. Refused.
 *
 * So the comparison is required inside the braces. Anchored on the attribute
 * rather than on `<button`, because the attributes between them hold arrow
 * functions and a `>` of their own; `[^<]` keeps the match inside the one
 * element, so it can never borrow a neighbouring button's words.
 *
 * What this does NOT catch: an arm whose label is computed rather than literal
 * (`>{className}<` in `T3Browser.tsx`) is outside the `[^<>{}]` body and is not
 * harvested — such a label is DATA, not a surface name a station could claim.
 * Nor does it claim every pane in the console is reachable this way: a pane
 * built some third way, neither a `{ id, label }` tab descriptor nor a compared
 * `aria-pressed` arm, is not in this vocabulary and a station naming it would
 * fail guard (1) until the derivation learns that shape. */
const SEGMENTED_VIEW_BUTTON =
  /\baria-pressed=\{[^{}]*?===[^{}]*?\}[^<]*?>\s*([A-Z][^<>{}]*?)\s*<\/button>/g;

function panelNames(): Set<string> {
  const out = new Set<string>();
  for (const file of productionSources(SRC_DIR)) {
    const text = withoutComments(readFileSync(file, "utf-8"));
    for (const match of text.matchAll(TAB_DESCRIPTOR)) out.add(match[1]!.toLowerCase());
    for (const match of text.matchAll(SEGMENTED_VIEW_BUTTON)) out.add(match[1]!.toLowerCase());
  }
  // The Agent workspace's first tab is COMPUTED from the agent's strategy, so it
  // is asked rather than read: one label per strategy the console can be given.
  for (const strategy of ["agentic", "deterministic"] as const) {
    out.add(threadTabLabel({ default_strategy: strategy } as AgentDetailView).toLowerCase());
  }
  return out;
}

const PANELS = panelNames();

/** Anywhere a station may send a reader: a rail destination or an in-page tab. */
function isRealSurface(name: string): boolean {
  const key = name.toLowerCase();
  return DESTINATIONS.has(key) || PANELS.has(key);
}

// ---------------------------------------------------------------------------
// What the copy claims
// ---------------------------------------------------------------------------

const NAV_VERB = /\b(?:open|on|in|from|switch to|go to)\s+(?:the\s+|its\s+|this\s+)?/gi;

/** A word that could be a claimed proper name rather than ordinary prose.
 *
 * Any initial capital, which is deliberately WIDER than the `/^[A-Z][a-z]/` this
 * used to be. That form required the second character to be lower case, so it
 * silently skipped every name whose second character is not — and `KPIs` is a
 * real rail destination (`navSections.ts`: Insights → KPIs). A station that
 * wrote "open KPIs" was not checked against anything; a station that mislinked
 * it could not be caught. Acronym-initial names are now visible to the guard.
 *
 * What this still does NOT distinguish: a capital that begins a sentence rather
 * than a name. It is only reached after a navigation verb, where a sentence
 * boundary does not fall in this copy, so the ambiguity has no instance here —
 * but the rule is positional, not grammatical, and would misread one if it did. */
const CAPITALISED = /^[A-Z]/;
const LONGEST_SURFACE_NAME = 4;

/** Trim the punctuation a sentence puts around a name, not the name itself. */
function bareWord(word: string): string {
  return word.replace(/^[("'“‘]+/, "").replace(/[).,:;!?"'”’…—]+$/, "");
}

/**
 * Every surface this text claims: the capitalised phrase after a navigation
 * verb, resolved to the LONGEST prefix that is a real surface. A phrase with no
 * real prefix is returned whole — that is the violation.
 *
 * PREFIX ACCEPTANCE, AND WHAT IT COSTS. Resolution is by prefix because the name
 * is embedded in a sentence and nothing marks where it ends: "On its Conversation
 * tab, put the question…" must resolve to `Conversation`, and "open Agents, then"
 * to `Agents`. The price is that a LONGER phrase beginning with a real surface is
 * accepted as that surface — "open Search history" resolves to `Search` and
 * passes, even though no `Search history` exists. So this guard catches a name
 * the console does not have AT ALL (the "open Ask" class, which is what it was
 * built for); it does NOT catch a real name extended into a surface that does not
 * exist. Closing that would need a rule for where a name ends in prose, and a
 * wrong one would fail legitimate copy.
 *
 * Two more limits worth stating rather than discovering. Only a phrase following
 * one of `NAV_VERB`'s verbs is examined at all — a surface named with any other
 * verb ("choose Re-verify") is invisible here. And at most
 * `LONGEST_SURFACE_NAME` words are considered, so a five-word surface name would
 * resolve to a shorter prefix or not at all.
 */
function surfaceClaims(text: string): { claim: string; real: boolean }[] {
  const out: { claim: string; real: boolean }[] = [];
  for (const match of text.matchAll(NAV_VERB)) {
    const rest = text.slice(match.index + match[0].length);
    const words = rest.split(/\s+/, LONGEST_SURFACE_NAME).map(bareWord).filter(Boolean);
    if (words.length === 0) continue;
    // Ordinary prose, not a name being claimed.
    if (!CAPITALISED.test(words[0]!)) continue;
    let resolved: string | null = null;
    for (let n = 1; n <= words.length; n += 1) {
      const candidate = words.slice(0, n).join(" ");
      if (isRealSurface(candidate)) resolved = candidate;
    }
    out.push(
      resolved === null ? { claim: words[0]!, real: false } : { claim: resolved, real: true },
    );
  }
  return out;
}

/** The NAVIGABLE prose a station puts in front of a presenter, plus its link
 * labels — each tagged with where it lives, so a failure names the field to edit.
 *
 * THREE FIELDS, NOT SIX, AND ON PURPOSE. `path` is the click path, `see` is what
 * the reader should then be looking at, and a link label is a button that goes
 * somewhere: all three send a reader to a surface, so a name in one is a claim
 * about the navigation. `title`, `role` and `job` are DESCRIPTION — they say what
 * the job is and who does it — and scanning them flags legitimate content,
 * because `NAV_VERB`'s short words occur in ordinary English: the role "A
 * servicing agent on Meridian's frontline desk" would report `Meridian's`, this
 * tenant's own name, as a missing surface.
 *
 * So the invariant this guardian establishes is about the three navigable fields,
 * NOT about every proper name the registry contains. A surface named only in a
 * `title`, `role` or `job` is unchecked. The right response to a future mislink
 * there is to move the name into `path` or a link label — where it belongs
 * anyway, because that is where a reader is being told to go — not to widen this
 * scan onto prose it would misread. */
function navigableCopy(station: StationDef): { field: string; text: string }[] {
  return [
    { field: "path", text: station.path },
    { field: "see", text: station.see },
    ...station.links.map((link) => ({ field: `link "${link.label}"`, text: link.label })),
  ];
}

// ---------------------------------------------------------------------------
// The guards
// ---------------------------------------------------------------------------

describe("station-surface guardian", () => {
  it("derives both vocabularies from the shipped sources, and they are not empty", () => {
    // Sanity, so that a broken derivation reads as a failure and not as "every
    // station is clean". An empty set would pass every check below vacuously.
    expect(DESTINATIONS.size).toBeGreaterThan(10);
    expect(DESTINATIONS.get("agents")).toBe("/agents");
    expect(DESTINATIONS.get("search")).toBe("/search");
    expect(PANELS.size).toBeGreaterThan(4);
    // Derived, never listed: these come out of ModelsPage's tab descriptors,
    // VerifiedQueriesPage's segmented control and threadTabLabel respectively.
    expect(PANELS.has("policy")).toBe(true);
    expect(PANELS.has("browse records")).toBe(true);
    expect(PANELS.has("conversation")).toBe(true);
    expect(PANELS.has("runs")).toBe(true);
  });

  it("(1) every surface a station names exists in the shipped console", () => {
    const offenders: string[] = [];
    let checked = 0;
    for (const station of STATIONS) {
      for (const { field, text } of navigableCopy(station)) {
        for (const { claim, real } of surfaceClaims(text)) {
          checked += 1;
          if (!real) {
            offenders.push(`${station.id}.${field}: "${claim}" is no destination and no tab`);
          }
        }
      }
    }
    // The tour must actually be naming surfaces; zero claims would make the
    // assertion below true by checking nothing.
    expect(checked).toBeGreaterThan(STATIONS.length);
    expect(offenders).toEqual([]);
  });

  it("(2) a link labelled with a destination goes to that destination's route", () => {
    const offenders: string[] = [];
    for (const station of STATIONS) {
      for (const link of station.links) {
        const named = surfaceClaims(link.label).find((entry) => entry.real);
        if (named === undefined) continue;
        const to = DESTINATIONS.get(named.claim.toLowerCase());
        if (to === undefined) continue; // a tab name, not a destination
        if (link.to !== to) {
          offenders.push(
            `${station.id}: "${link.label}" → ${link.to}, but ${named.claim} is ${to}`,
          );
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("(3) the control — the copy the tour actually shipped fails both guards", () => {
    // Verbatim from demo/stations.ts before this fix. If any of it came back,
    // the two tests above would fail rather than pass silently.
    const shipped = {
      path: "Sign in as the frontline agent, open Ask, and put the question in your own words — what is the provisional credit deadline for a debit card dispute. Read the answer. Then open Search, run the same words, and open the first row.",
      see: "An answer on Ask. On Search, a ranked table of Meridian documents, and the row you open shows the passage itself, the document it sits in and its citation.",
      label: "Open Ask",
      to: "/ask",
    };
    expect(isRealSurface("Ask")).toBe(false);
    expect(
      surfaceClaims(shipped.path)
        .filter((c) => !c.real)
        .map((c) => c.claim),
    ).toEqual(["Ask"]);
    expect(
      surfaceClaims(shipped.see)
        .filter((c) => !c.real)
        .map((c) => c.claim),
    ).toEqual(["Ask"]);
    expect(surfaceClaims(shipped.label)).toEqual([{ claim: "Ask", real: false }]);
    // …and the same sentences, pointed at surfaces that exist, are clean.
    const fixed =
      "Sign in as the frontline agent and open Agents, then open the workspace for the Meridian analyst. On its Conversation tab, put the question in your own words.";
    expect(surfaceClaims(fixed).every((c) => c.real)).toBe(true);
    expect(surfaceClaims("Open Agents")).toEqual([{ claim: "Agents", real: true }]);

    // The route half: a label naming a real destination that links somewhere
    // else is caught even though both halves exist on their own.
    const mislinked: StationDef = {
      ...STATIONS[0]!,
      links: [{ label: "Open Search", to: "/ask" }],
    };
    const named = surfaceClaims(mislinked.links[0]!.label).find((entry) => entry.real);
    expect(named?.claim).toBe("Search");
    expect(DESTINATIONS.get("search")).not.toBe(mislinked.links[0]!.to);
  });

  it("(4) a deleted destination takes the tour down with it, not silently", () => {
    // The mechanism, proved on a synthetic rail: the guard reads whatever
    // navigation the console currently ships, so removing a destination makes
    // every station that names it fail — which is what did NOT happen when Ask
    // was removed, and the reason this file exists.
    const rail = destinationNames([
      { label: "Home", to: "/" },
      { label: "Execution", items: [{ label: "Search", to: "/search" }] },
    ]);
    expect(rail.has("search")).toBe(true);
    expect(rail.has("agents")).toBe(false);
    const trimmed = destinationNames([{ label: "Home", to: "/" }]);
    expect(trimmed.has("search")).toBe(false);
  });

  it("(5) a PANEL is a pane you can be on, not a button you press", () => {
    // The Run timeline's "Follow live" is a toggle carrying `aria-pressed`, and
    // the vocabulary used to admit it — so "Open Follow live" passed as a valid
    // surface claim. It is a control: pressing it changes what the pane does, it
    // is not a pane you can be on.
    expect(PANELS.has("follow live")).toBe(false);
    expect(isRealSurface("Follow live")).toBe(false);
    // Reported as `Follow` — an unresolved claim names its first word, which is
    // this function's existing contract for a phrase with no real prefix.
    expect(surfaceClaims("Open Follow live")).toEqual([{ claim: "Follow", real: false }]);

    // The other direction: the view arms beside it, which ARE panes, are still
    // derived — so this narrowed the vocabulary without emptying it.
    expect(PANELS.has("approved queries")).toBe(true);
    expect(PANELS.has("browse records")).toBe(true);

    // …and the discriminator is the pressed EXPRESSION, proved on the two shapes
    // rather than on the two files, so the rule is what is pinned.
    const arms = (source: string) => [...source.matchAll(SEGMENTED_VIEW_BUTTON)].map((m) => m[1]);
    expect(arms('<button aria-pressed={view === "browse"} onClick={go}>Browse records</button>')) //
      .toEqual(["Browse records"]);
    expect(arms("<button aria-pressed={followLive} onClick={go}>Follow live</button>")) //
      .toEqual([]);
  });

  it("(6) an ALL-CAPS-initial destination is visible to the guard — KPIs is real", () => {
    // The discriminator was `/^[A-Z][a-z]/`, which required a lower-case SECOND
    // character — so `KPIs`, a shipped rail destination, produced no claim at all
    // and a station could have mislinked it unchecked. Both halves are asserted:
    // the destination exists, and a claim on it is now made and resolved.
    expect(DESTINATIONS.get("kpis")).toBe("/insights");
    expect(surfaceClaims("Open KPIs")).toEqual([{ claim: "KPIs", real: true }]);
    // It is caught in the same two directions any other name is: an ALL-CAPS
    // name the rail does NOT have is now a reported violation rather than
    // silence. (The lookup is case-folded, so `KPIS` would resolve to `KPIs` —
    // the absent name has to be a different word.)
    expect(surfaceClaims("Open SLAs")).toEqual([{ claim: "SLAs", real: false }]);
    // Ordinary prose is still not a claim: the capital is what selects one.
    expect(surfaceClaims("open the first row")).toEqual([]);
    expect(surfaceClaims("put the question in your own words")).toEqual([]);
  });

  it("(7) the scan covers the three NAVIGABLE fields, and says so", () => {
    // `navigableCopy` reads `path`, `see` and each link label — not `title`,
    // `role` or `job`. That exclusion is correct, and this is the evidence:
    // scanning a role would report this tenant's own name as a missing surface.
    const station = STATIONS[0]!;
    expect(navigableCopy(station).map((entry) => entry.field)).toEqual([
      "path",
      "see",
      ...station.links.map((link) => `link "${link.label}"`),
    ]);
    expect(navigableCopy(station).some((entry) => entry.text === station.role)).toBe(false);

    // Why it is excluded, not an oversight — the copy that would be flagged is
    // legitimate, and it is the tenant's real name (see tenant-identity-guardian).
    expect(station.role).toContain("on Meridian's");
    expect(
      surfaceClaims(station.role)
        .filter((c) => !c.real)
        .map((c) => c.claim),
    ).toEqual(["Meridian's"]);
  });

  it("(8) prefix acceptance is a stated limit, not a claim of full coverage", () => {
    // A phrase BEGINNING with a real surface resolves to that surface. This is
    // what makes names embedded in sentences readable at all — and it is also
    // what this guard cannot catch, asserted here so the limit is a fact in the
    // suite rather than a sentence in a comment.
    expect(surfaceClaims("On its Conversation tab, put the question")).toEqual([
      { claim: "Conversation", real: true },
    ]);
    expect(surfaceClaims("open Search history")).toEqual([{ claim: "Search", real: true }]);
    expect(isRealSurface("Search history")).toBe(false);
    // A name with no real prefix is still the violation this was built for.
    expect(surfaceClaims("open Ask history")).toEqual([{ claim: "Ask", real: false }]);
    // And a surface named with a verb outside NAV_VERB is not examined at all.
    expect(surfaceClaims("choose Re-verify, and Export CSV")).toEqual([]);
  });

  it("(9) scans the station registry as shipped — bundled, unflagged, in scope", () => {
    const scanned = productionSources(SRC_DIR).map((file) => relative(SRC_DIR, file));
    expect(scanned).toContain(join("demo", "stations.ts"));
    expect(scanned.some((file) => file.startsWith(`test${"/"}`))).toBe(false);
    expect(STATIONS.length).toBeGreaterThan(3);
  });
});
