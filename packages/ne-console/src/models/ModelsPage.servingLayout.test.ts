// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(resolve(process.cwd(), "src/models/models.css"), "utf8");

describe("Models serving-health responsive layout", () => {
  it("keeps the desktop explanation beside its heading", () => {
    const start = CSS.indexOf(".ne-models__servingintro {");
    const end = CSS.indexOf(".ne-models__servingtitle", start);
    expect(start).toBeGreaterThan(-1);
    expect(CSS.slice(start, end)).toContain("grid-template-columns: max-content minmax(0, 1fr)");
  });

  it("stacks the explanation and wraps observed facts on a narrow screen", () => {
    const start = CSS.indexOf("@media (max-width: 720px)");
    const end = CSS.indexOf("/* ------------------------------- routing", start);
    const narrow = CSS.slice(start, end);
    expect(start).toBeGreaterThan(-1);
    expect(narrow).toMatch(/\.ne-models__servingintro\s*\{\s*grid-template-columns:\s*1fr;\s*\}/);
    expect(narrow).toMatch(
      /\.ne-models__servinghead,\s*\.ne-models__servingfoot\s*\{\s*flex-wrap:\s*wrap;\s*\}/,
    );
  });
});
