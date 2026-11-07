// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// SESSION-BOUND-CLIENT GUARDIAN.
//
// The idle-refresh defect was not one hook: FIVE hooks independently read the
// bearer off the react-oidc-context auth object and memoised a client on it
// (useApiClient, useAnswerLedgerClient, useConversationLedgerClient,
// useOntologyInspectionClient, useApprovalDecisionAudit). Each one rebuilt its
// client on every silent renew, and client identity is what invalidates every
// governed read in the console — so each was an independent four-minute blank,
// and a sixth copy would be another one.
//
// So the bearer is readable in exactly ONE place. An fs-walk of the production
// source proves it: `access_token` may appear only inside src/api/ (the client
// implementations that send it) and auth/useSessionBoundClient.ts (the single
// hook that binds a client to the live session and draws the
// retain-vs-invalidate line). Anywhere else is a new copy of the defect.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");
const BEARER = "access" + "_token";

/** The two places allowed to name the bearer, relative to src/. */
function permitted(relativePath: string): boolean {
  return relativePath.startsWith("api/") || relativePath === "auth/useSessionBoundClient.ts";
}

/** Production .ts/.tsx under src/ — the test tree and every *.test.* excluded
 * (a test may legitimately mint a token to assert what gets sent). */
function productionSources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "test" || entry === "vendor") continue;
      out.push(...productionSources(full));
      continue;
    }
    if (/\.test\.tsx?$/.test(entry)) continue;
    if (/\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

const sources = productionSources(SRC_DIR);

describe("session-bound-client guardian", () => {
  it("finds production sources to scan", () => {
    expect(sources.length).toBeGreaterThan(50);
  });

  it("reads the OIDC bearer in exactly one hook", () => {
    const offenders = sources
      .map((file) => relative(SRC_DIR, file))
      .filter((rel) => !permitted(rel))
      .filter((rel) => readFileSync(join(SRC_DIR, rel), "utf-8").includes(BEARER));
    expect(offenders).toEqual([]);
  });

  it("keeps the one permitted hook actually reading it", () => {
    // Guards the guard: if the bearer read moves, the rule above would pass
    // vacuously while every client silently lost its credential.
    const hook = readFileSync(join(SRC_DIR, "auth", "useSessionBoundClient.ts"), "utf-8");
    expect(hook).toContain(BEARER);
  });
});
