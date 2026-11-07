// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// TENANT-IDENTITY GUARDIAN. An fs-walk of the PRODUCTION source proves, by
// construction, that no shipped console file names the legacy Acme tenant — not
// in rendered copy, not in a placeholder, not in a comment.
//
// WHY THIS FILE EXISTS. The demo tenant became Meridian; the seed, the packaged
// verified templates and the fixture assets were all moved. The guided tour was
// not. `demo/stations.ts` kept shipping "Noor Inoue, an engineering manager at
// Acme" and "a ranked table of Acme documents" — statically imported into the
// main bundle (App.tsx) and opened BY ITSELF on a first run
// (demo/firstRun.test.tsx), so a Meridian demo on a fresh profile opened by
// displaying another company's name and one of its people. That wart was
// written down when the tenant was renamed and it survived anyway, because
// nothing asserted against it. This is the assertion.
//
// Scope: all production .ts/.tsx/.css/.html under src/, EXCLUDING src/test/**
// and *.test.* — the mock fixtures under src/test/mocks are never bundled, and
// this file must be able to spell the banned strings in order to test itself.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");
// packages/ne-console/src/test → the legacy Acme corpus, still in the tree but
// no longer staged by the demo composition.
const ACME_RECORDS_JSON = resolve(
  SRC_DIR,
  "..",
  "..",
  "..",
  "tools",
  "acme",
  "fixtures",
  "acme-v1",
  "records.json",
);

/** The tenant the console must not name. Assembled from pieces so this file's
 * own source is not what a future grep for the literal finds first. */
const FOREIGN_TENANT = new RegExp(`\\b${"ac" + "me"}\\b`, "i");

/** Collect production sources under src/, skipping the test tree and tests. */
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
    if (/\.(tsx?|css|html)$/.test(entry)) out.push(full);
  }
  return out;
}

const sources = scannedSources(SRC_DIR);

/** Distinctive record names from the legacy corpus: multi-word, long enough
 * that a match is an identity leak and not a coincidence. */
function legacyRecordNames(): string[] {
  const records = JSON.parse(readFileSync(ACME_RECORDS_JSON, "utf-8")) as {
    accounts: { name: string }[];
    tickets: { subject: string }[];
    people: { given_name: string; family_name: string }[];
  };
  return [
    ...records.accounts.map((a) => a.name),
    ...records.tickets.map((t) => t.subject),
    ...records.people.map((p) => `${p.given_name} ${p.family_name}`),
  ].filter((name) => name.length >= 8 && name.includes(" "));
}

describe("tenant-identity guardian", () => {
  it("finds production sources to scan, and excludes the test tree", () => {
    expect(sources.length).toBeGreaterThan(50);
    const rel = sources.map((f) => relative(SRC_DIR, f));
    expect(rel.some((f) => f.startsWith(`test${"/"}`))).toBe(false);
    // The guided tour's registry is IN scope — it is the file this guardian was
    // written for, and it is bundled with no flag of any kind.
    expect(rel).toContain(join("demo", "stations.ts"));
  });

  it("(1) no shipped console source names the legacy tenant", () => {
    const offenders = sources
      .filter((f) => FOREIGN_TENANT.test(readFileSync(f, "utf-8")))
      .map((f) => relative(SRC_DIR, f));
    expect(offenders).toEqual([]);
  });

  it("(2) no shipped console source names a record from the legacy corpus", () => {
    const names = legacyRecordNames();
    // Sanity: the corpus parsed, so an empty result below means "clean", not
    // "nothing was checked".
    expect(names.length).toBeGreaterThan(100);
    const offenders: string[] = [];
    for (const file of sources) {
      const text = readFileSync(file, "utf-8");
      for (const name of names) {
        if (text.includes(name)) offenders.push(`${relative(SRC_DIR, file)}: ${name}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("(3) the control — both guards bite on the exact copy the tour shipped", () => {
    // Verbatim from demo/stations.ts before the Meridian fix. If either of these
    // came back, the two tests above would fail rather than pass silently.
    const shipped = {
      role: `Noor Inoue, an engineering manager at ${"Ac" + "me"}`,
      see: `On Search, a ranked table of ${"Ac" + "me"} documents`,
      account: "Follow the citation on the row for Bluepeak Dynamics LLC.",
      ticket: "the open ticket Instant Tunnel Paint makes a strange noise",
    };
    expect(FOREIGN_TENANT.test(shipped.role)).toBe(true);
    expect(FOREIGN_TENANT.test(shipped.see)).toBe(true);
    // The record names carry no tenant word at all — they are only caught by the
    // corpus check, which is why (2) is not redundant with (1).
    expect(FOREIGN_TENANT.test(shipped.account)).toBe(false);
    expect(FOREIGN_TENANT.test(shipped.ticket)).toBe(false);
    const names = legacyRecordNames();
    expect(names.some((name) => shipped.role.includes(name))).toBe(true);
    expect(names.some((name) => shipped.account.includes(name))).toBe(true);
    expect(names.some((name) => shipped.ticket.includes(name))).toBe(true);
    // …and the Meridian copy that replaced them passes both.
    const fixed = "A servicing agent on Meridian's frontline desk";
    expect(FOREIGN_TENANT.test(fixed)).toBe(false);
    expect(names.some((name) => fixed.includes(name))).toBe(false);
  });
});
