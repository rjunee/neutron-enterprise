// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE STATION REGISTRY'S GUARDS.
//
// The tour is the one place explanation belongs, which makes it the one place a
// stale sentence can survive a rebuild. These are the rules that stop it:
//
//   (a) every link is a route the router actually registers — read out of
//       App.tsx, so a rename that lands without the tour is a failing test;
//   (b) no station names a control that was deleted from the screen it sends
//       someone to;
//   (c) no station predicts a reading this deployment's own screens could
//       contradict — no counts, no scores, no verdicts;
//   (d) the copy obeys §0 — sentence case titles, no endpoint strings, no
//       ticket ids, none of the banned vocabulary;
//   (e) every station is a JOB: a role, a task, a click path and a result,
//       all four present, none of them a description of the software;
//   (f) every record name in the copy is carried in the MERIDIAN fixture — the
//       release this deployment actually seeds. An invented customer is the one
//       defect this product cannot ship, and a real customer belonging to a
//       different tenant is that same defect wearing a plausible face. This
//       guard used to read the legacy Acme corpus, which is no longer staged
//       (the deploy gate pins the 11 Meridian templates and
//       nothing else), so it was checking the tour against records the
//       deployment does not hold;
//   (g) the prose is labelled as authored, because that is what it is.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { claimSourceIsHonest, clampIndex, STATIONS } from "./stations";

const HERE = resolve(fileURLToPath(import.meta.url), "..");
const APP_TSX = resolve(HERE, "..", "App.tsx");
// packages/ne-console/src/demo → the Meridian release this deployment seeds
// (NE_API_DEMO_MERIDIAN_FIXTURE_PATH=/seed/meridian-v1).
const RECORDS_JSON = resolve(
  HERE,
  "..",
  "..",
  "..",
  "..",
  "tools",
  "meridian",
  "fixtures",
  "meridian-v1",
  "records.json",
);

/** Strip a query string, leaving the pathname (the routable part). */
function pathnameOf(to: string): string {
  const q = to.indexOf("?");
  return q === -1 ? to : to.slice(0, q);
}

/** Every sentence a station renders, as one string. */
function copyOf(station: (typeof STATIONS)[number]): string {
  return `${station.title} ${station.role} ${station.job} ${station.path} ${station.see}`;
}

const ALL_COPY = STATIONS.map(copyOf).join(" ");

describe("guided tour — the stations", () => {
  it("(a) every station link is a route the router registers", () => {
    const app = readFileSync(APP_TSX, "utf-8");
    const registered = new Set([...app.matchAll(/path="([^"]+)"/g)].map((m) => m[1]));
    const linked = new Set(STATIONS.flatMap((s) => s.links.map((l) => pathnameOf(l.to))));
    expect(linked.size).toBeGreaterThan(3);
    for (const path of linked) expect(registered.has(path), path).toBe(true);
  });

  it("(a2) and none of them is a route the rename left behind", () => {
    // The redirects still work; a tour that walks someone through them is still
    // pointing at the old product.
    const stale = ["/chat", "/knowledge", "/connect", "/flywheel", "/evals", "/kpi", "/steward"];
    const linked = STATIONS.flatMap((s) => s.links.map((l) => pathnameOf(l.to)));
    for (const path of stale) expect(linked).not.toContain(path);
  });

  it("(a3) and none of them is a screen this deployment serves empty", () => {
    // These are the CUTS, held in place. The connector list comes back empty
    // here, so a station sending a reader to read connector freshness lands on a
    // blank screen; the period-metrics screen serves numbers but no job is
    // performed on it. Either one returning to the tour must be a deliberate act
    // with this line deleted, not an edit that slipped past review.
    const cut = ["/sources", "/insights", "/improvement", "/evaluations"];
    const linked = STATIONS.flatMap((s) => s.links.map((l) => pathnameOf(l.to)));
    for (const path of cut) expect(linked, path).not.toContain(path);
  });

  it("points the record beat at reads this deployment actually serves", () => {
    // The legacy `accounts-directory` template is NOT staged — the demo
    // composition pins every verified read to the Meridian release, which holds
    // only the three bank.* graphs, so the old instruction ("choose the account
    // directory") walked a room into a catalog that has no such entry. Both
    // reads named here are in the pinned Meridian roster.
    const station = STATIONS.find((candidate) => candidate.id === "station-record");
    expect(station?.links).toEqual([
      { label: "Open the disputes directory", to: "/verified-queries" },
    ]);
    expect(station?.path).toMatch(/^Open Verified Queries and/);
    expect(station?.path).not.toMatch(/account directory|Reference|Verified answers/);
    expect(station?.path).toContain("disputes directory");
    expect(station?.path).toContain("RegEDispute");
  });

  it("(b) no station names a control that was deleted from the screen it opens", () => {
    // Each of these was a real instruction in this registry, pointing at
    // something the rebuilt screens no longer have.
    const deleted = [
      "pull-to-prove",
      "quarantine vessel",
      "verification sweep",
      "scram",
      "t3 drawer",
      "breeding",
      "operator amber",
      "trim lens",
      "chamber",
    ];
    const text = ALL_COPY.toLowerCase();
    for (const phrase of deleted) expect(text.includes(phrase), phrase).toBe(false);
  });

  it("(b2) and no station names a path this deployment does not serve", () => {
    // Both were instructions in the previous registry, and both come back empty
    // on this deployment: the answer stream carries no citations, and a record
    // read anchored on a search result's citation returns no nodes. The source
    // check happens where the passage IS served — the search result's own pane.
    const text = ALL_COPY.toLowerCase();
    expect(text).not.toMatch(/citation from the answer|open a citation from/);
    expect(text).not.toMatch(/view in library/);
  });

  it("(c) no station predicts a reading — no egress claim, and no figures", () => {
    const predicts = [
      "nothing leaves",
      "zero external requests",
      "no egress",
      "reads sealed",
      "all assets local",
    ];
    for (const station of STATIONS) {
      const text = copyOf(station).toLowerCase();
      for (const phrase of predicts) {
        expect(text.includes(phrase), `${station.id} asserts "${phrase}"`).toBe(false);
      }
      // A count, a score or a date in the copy is a reading the product's own
      // screens can contradict. The ONLY digits allowed are the ones inside a
      // named Meridian record, so those are removed before the check.
      let residue = copyOf(station);
      for (const name of station.tenantRecords) residue = residue.split(name).join(" ");
      expect(residue, `${station.id} states a figure`).not.toMatch(/\d/);
    }
    // …and the station that talks about reach renders the deployment's own
    // figure instead of asserting one.
    const live = STATIONS.filter((s) => s.liveReadout === "egress");
    expect(live).toHaveLength(1);
    expect(live[0]!.see.toLowerCase()).toContain("host");
  });

  it("(d) the copy obeys the vocabulary rules", () => {
    for (const station of STATIONS) {
      // Sentence case: a title is not a signage shout.
      expect(station.title).not.toBe(station.title.toUpperCase());
      // No endpoint strings, verbs, status codes, ticket ids or file paths.
      const text = copyOf(station);
      expect(text).not.toMatch(/\/v1\/|GET |POST |HTTP \d|NE-\d|\.py|\.tsx?\b/);
      expect(text.toLowerCase()).not.toMatch(
        /\bproven\b|\bgoverned\b|\bsovereign\b|\bverbatim\b|\bhonest|\bfabricat|\btrimmed\b/,
      );
      // The forbidden sales claims.
      expect(text.toLowerCase()).not.toMatch(
        /own the stack|100% accurate|no hallucination|no ongoing costs/,
      );
    }
    for (const link of STATIONS.flatMap((s) => s.links)) {
      expect(link.label).not.toBe(link.label.toUpperCase());
    }
  });

  it("(e) numerals are unique, in order, and ids are stable check-off keys", () => {
    const numerals = STATIONS.map((s) => s.numeral);
    expect(numerals).toEqual(STATIONS.map((_, i) => String(i + 1)));
    expect(new Set(STATIONS.map((s) => s.id)).size).toBe(STATIONS.length);
  });

  it("(e2) every station is a job — a person, a task, a path and a result", () => {
    expect(STATIONS.length).toBeGreaterThan(3);
    for (const station of STATIONS) {
      for (const [field, value] of Object.entries({
        role: station.role,
        job: station.job,
        path: station.path,
        see: station.see,
      })) {
        expect(value.trim().length, `${station.id}.${field} is empty`).toBeGreaterThan(20);
      }
      // The click path names a screen the reader can open, and tells them to act
      // on it. A path that only describes the screen is a feature tour again.
      expect(station.path, station.id).toMatch(/\b(open|sign in)\b/i);
      // The role is a person or a named role doing the work, never the software.
      expect(station.role.toLowerCase(), station.id).not.toMatch(
        /\bthe console\b|\bthe product\b|\bthe platform\b|\bthe system\b/,
      );
    }
  });

  it("(e3) the tour walks the jobs, in the order the work happens", () => {
    // `/agents`, not `/ask`: Ask stopped being a rail destination (navSections.ts
    // — "ASK IS NOT A RAIL DESTINATION ANY MORE"), so a tour that said "open
    // Ask" named a surface the presenter could not find in the sidebar. The
    // route still redirects; the copy was the defect. Held by
    // test/station-surface-guardian.test.ts, which derives the legal names.
    const linked = STATIONS.flatMap((s) => s.links.map((l) => pathnameOf(l.to)));
    for (const route of [
      "/agents",
      "/search",
      "/verified-queries",
      "/approvals",
      "/audit",
      "/posture",
      "/models",
    ]) {
      expect(linked, route).toContain(route);
    }
    // The decision comes before its reconstruction: an audit station placed
    // before the approval station walks a room through an empty filter.
    const at = (id: string) => STATIONS.findIndex((s) => s.id === id);
    expect(at("station-decide")).toBeGreaterThan(-1);
    expect(at("station-reconstruct")).toBeGreaterThan(at("station-decide"));
    // …and the second-identity comparison follows the query it re-runs.
    expect(at("station-second-identity")).toBeGreaterThan(at("station-answer"));
  });

  it("(f) every record name in the copy is one this deployment holds", () => {
    const records = JSON.parse(readFileSync(RECORDS_JSON, "utf-8")) as {
      accounts: { id: string; name: string }[];
      documents: { id: string; title: string }[];
      tickets: { id: string; subject: string }[];
      orders: { order_number: string }[];
      people: { id: string; given_name: string; family_name: string }[];
    };
    // Meridian records are addressed BY ID on the surfaces the tour sends people
    // to (the record browser renders `urn:ne:meridian:dispute:mer-dsp-001`), so
    // an id is as much a name here as an account label is.
    const known = new Set<string>([
      ...records.accounts.flatMap((a) => [a.id, a.name]),
      ...records.documents.flatMap((d) => [d.id, d.title]),
      ...records.tickets.flatMap((t) => [t.id, t.subject]),
      ...records.orders.map((o) => o.order_number),
      ...records.people.flatMap((p) => [p.id, `${p.given_name} ${p.family_name}`]),
    ]);
    // Sanity: the fixture parsed and is the release, not a stub.
    expect(known.size).toBeGreaterThan(50);

    const declared = STATIONS.flatMap((s) => s.tenantRecords);
    expect(declared.length).toBeGreaterThan(0);
    for (const station of STATIONS) {
      for (const name of station.tenantRecords) {
        // It exists in the corpus…
        expect(known.has(name), `${station.id} names "${name}", which is in no record`).toBe(true);
        // …and it is actually spelled in the copy, so the declaration cannot
        // drift into a list of names the station stopped mentioning.
        expect(copyOf(station).includes(name), `${station.id} declares an unused "${name}"`).toBe(
          true,
        );
      }
    }
    // The control: a plausible-looking record that is NOT in the release is
    // caught by the same check that passes for the real ones — including the
    // Acme account this station named until the tenant-identity fix.
    expect(known.has("mer-dsp-001")).toBe(true);
    expect(known.has("mer-dsp-999")).toBe(false);
    expect(known.has("Bluepeak Dynamics LLC")).toBe(false);
  });

  it("(g) authored prose is labelled authored — both ways round", () => {
    for (const station of STATIONS) {
      expect(station.claimSource, station.id).toBe("authored");
      expect(claimSourceIsHonest(station), station.id).toBe(true);
    }
    // The control: the same station, relabelled as something a server sent, is
    // a lie about copy that lives in this file — and the guard says so.
    expect(claimSourceIsHonest({ ...STATIONS[0]!, claimSource: "served" })).toBe(false);
  });

  it("(h) clampIndex clamps at both ends, no wraparound", () => {
    expect(clampIndex(-1, 8)).toBe(0);
    expect(clampIndex(0, 8)).toBe(0);
    expect(clampIndex(4, 8)).toBe(4);
    expect(clampIndex(7, 8)).toBe(7);
    expect(clampIndex(8, 8)).toBe(7);
    expect(clampIndex(100, 8)).toBe(7);
    expect(clampIndex(3, 0)).toBe(0);
  });
});
