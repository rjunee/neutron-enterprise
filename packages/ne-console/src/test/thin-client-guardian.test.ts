// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// STRUCTURAL GUARDIAN (C6/C7 at the frontend seam). An fs-walk of the PRODUCTION
// source proves, independent of ESLint, that: (a) network primitives occur only
// under src/api/; (b) no client secret is embedded anywhere; (c) no data-plane
// endpoint string is present; (d) the governed API base env var is named only
// in src/api/config.ts. Test files and this guardian are excluded from the walk
// (they legitimately reference these tokens to assert their absence).

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = resolve(fileURLToPath(import.meta.url), "..", "..");
const PACKAGE_DIR = resolve(SRC_DIR, "..");

// Tokens are assembled from fragments so the guardian's own body never matches
// the (b)/(d) literal checks even if it were ever scanned.
const CLIENT_SECRET = ["client", "secret"].join("_");
const API_BASE_ENV = ["VITE", "API", "BASE", "URL"].join("_");
const DANGEROUS_HTML = ["dangerously", "SetInnerHTML"].join("");

/** Strip line and block comments so a doc-comment that names a banned token in
 * order to forbid it never trips its own guardian. Same helper, same reason, as
 * header-standard.test.ts. */
function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}
// NE-CONSOLE-ISSUER: the baked-issuer build-time constant is structurally banned —
// the console reads its issuer at runtime from /config.json, never a Vite env.
const VITE_OIDC = ["VITE", "OIDC"].join("_");

const NETWORK_PRIMITIVES = /\bfetch\s*\(|new\s+EventSource|new\s+WebSocket|XMLHttpRequest/;
const DATA_PLANE = /:6333|:6334|:5432|qdrant|postgres:\/\//i;

/** Recursively collect production .ts/.tsx files under src/ (skip the test tree). */
function productionSources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "test") continue; // src/test/** is fixtures/harness, not shipped
      out.push(...productionSources(full));
      continue;
    }
    if (/\.test\.tsx?$/.test(entry)) continue;
    if (/\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

const sources = productionSources(SRC_DIR);

describe("thin-client structural guardian", () => {
  it("finds production sources to scan", () => {
    expect(sources.length).toBeGreaterThan(5);
  });

  it("(a) network primitives appear only under src/api/", () => {
    const offenders = sources.filter((f) => {
      const rel = relative(SRC_DIR, f);
      if (rel.startsWith("api/")) return false;
      return NETWORK_PRIMITIVES.test(readFileSync(f, "utf-8"));
    });
    expect(offenders).toEqual([]);
  });

  it("(b) no client secret is embedded in src/, index.html, or vite.config.ts", () => {
    const files = [
      ...sources,
      join(PACKAGE_DIR, "index.html"),
      join(PACKAGE_DIR, "vite.config.ts"),
    ];
    const offenders = files.filter((f) => readFileSync(f, "utf-8").includes(CLIENT_SECRET));
    expect(offenders).toEqual([]);
  });

  it("(c) no direct data-plane endpoint strings anywhere in src/", () => {
    const offenders = sources.filter((f) => DATA_PLANE.test(readFileSync(f, "utf-8")));
    expect(offenders).toEqual([]);
  });

  it("(d) the governed API base env var is named only in src/api/config.ts", () => {
    const referencing = sources.filter((f) => readFileSync(f, "utf-8").includes(API_BASE_ENV));
    const rels = referencing.map((f) => relative(SRC_DIR, f)).sort();
    expect(rels).toEqual(["api/config.ts"]);
  });

  it("(e) proposal/API-carried content is never injected as live HTML (no dangerouslySetInnerHTML anywhere in src/)", () => {
    // Comment-stripped, exactly as header-standard.test.ts scans: a comment
    // stating the code never injects live HTML is compliance PROSE, not a
    // violation. The rule is about what the JSX does.
    const offenders = sources.filter((f) =>
      stripComments(readFileSync(f, "utf-8")).includes(DANGEROUS_HTML),
    );
    expect(offenders).toEqual([]);
  });

  it("(f) the baked OIDC issuer env var appears in NO production source", () => {
    const offenders = sources.filter((f) => readFileSync(f, "utf-8").includes(VITE_OIDC));
    expect(offenders).toEqual([]);
  });
});
