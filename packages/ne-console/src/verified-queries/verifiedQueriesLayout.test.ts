// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(
  resolve(process.cwd(), "src/verified-queries/verifiedQueries.css"),
  "utf8",
);
const PAGE = readFileSync(
  resolve(process.cwd(), "src/verified-queries/VerifiedQueriesPage.tsx"),
  "utf8",
);

describe("Verified Queries responsive and token layout", () => {
  it("collapses the two-pane layout at the supported narrow breakpoint", () => {
    expect(CSS).toContain("@media (max-width: 48rem)");
    expect(CSS).toMatch(
      /@media \(max-width: 48rem\)[\s\S]*\.ne-verified-queries__layout\s*\{[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/,
    );
    expect(CSS).toContain("overflow-y: auto");
  });

  it("uses theme tokens, preserves focus, and removes motion on request", () => {
    expect(CSS).not.toMatch(/#[0-9a-f]{3,8}|rgba?\(/i);
    expect(CSS).toContain("var(--panel)");
    expect(CSS).toContain(":focus-visible");
    expect(CSS).toMatch(/prefers-reduced-motion:\s*reduce/);
    expect(PAGE).toContain('title="Verified Queries"');
    expect(PAGE).toContain("Authoring is not enabled");
  });
});
