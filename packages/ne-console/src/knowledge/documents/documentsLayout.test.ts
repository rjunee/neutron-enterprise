// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const directory = resolve(process.cwd(), "src/knowledge/documents");
const css = readFileSync(resolve(directory, "documents.css"), "utf8");
const page = readFileSync(resolve(directory, "DocumentsPage.tsx"), "utf8");
const workspace = readFileSync(resolve(directory, "DocumentsWorkspace.tsx"), "utf8");

describe("Documents workspace layout", () => {
  it("uses a bounded two-column layout that collapses at 64 and 48 rem", () => {
    expect(css).toContain("grid-template-columns: minmax(14rem, 19rem) minmax(0, 1fr)");
    expect(css).toContain("@media (max-width: 64rem)");
    expect(css).toContain("@media (max-width: 48rem)");
    expect(css).toMatch(
      /@media \(max-width: 48rem\)[\s\S]*?\.ne-documents__layout\s*{[^}]*grid-template-columns: minmax\(0, 1fr\)/,
    );
    expect(css).toMatch(
      /@media \(max-width: 48rem\)[\s\S]*?\.ne-documents__graph\s*{[^}]*grid-template-columns: minmax\(0, 1fr\)/,
    );
  });

  it("keeps long content and tables locally scrollable", () => {
    expect(css).toMatch(/\.ne-documents__tableoverflow,[\s\S]*?overflow-x: auto/);
    // The reading surface — not a <pre> any more — is the block that scrolls,
    // so the provenance region below it is never pushed off the page by a long
    // document. The directory list scrolls inside its own panel for the same
    // reason.
    expect(css).toMatch(/\.ne-documents__contentscroll\s*{[^}]*overflow-y: auto/);
    expect(css).toMatch(/\.ne-documents__documentlist\s*{[^}]*overflow-y: auto/);
    expect(css).toContain("overflow-wrap: anywhere");
    expect(css).toContain("min-width: 0");
  });

  // CONTENT LEADS (design/round3/screens/documents.html). The served text is
  // the hero reading surface: real reading typography on a bounded measure,
  // with every newline the server sent preserved because this same field also
  // carries structured records. Each assertion is paired with its complement —
  // the shape the composition must NOT take.
  it("gives the served content a reading surface, not a monospace dump", () => {
    expect(css).toMatch(/\.ne-documents__prose\s*{[^}]*font: var\(--type-lead\)/);
    expect(css).toMatch(/\.ne-documents__prose\s*{[^}]*max-width: 68ch/);
    expect(css).toMatch(/\.ne-documents__prose\s*{[^}]*white-space: pre-wrap/);
    // Complement: the reading surface is not set in the mono face, and the old
    // <pre> block it replaced is gone from the stylesheet entirely.
    expect(css).not.toMatch(/\.ne-documents__prose\s*{[^}]*--font-mono/);
    expect(css).not.toContain(".ne-documents__section pre");
  });

  it("keeps the pre-recomposition head and its stacked evidence block deleted", () => {
    // The three-row head (title block, hash block, selector row) collapsed into
    // one identity band; its classes must not survive as dead style.
    expect(css).not.toContain(".ne-documents__recordhead");
    expect(css).not.toContain(".ne-documents__selectedevidence");
    expect(css).toMatch(/\.ne-documents__band\s*{[^}]*flex-wrap: wrap/);
    expect(workspace).toContain('className="ne-documents__band"');
    expect(workspace).not.toContain("ne-documents__recordhead");
    expect(workspace).not.toContain("ne-documents__selectedevidence");
  });

  it("uses theme tokens and no motion that could hide content at rest", () => {
    expect(css).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(css).not.toMatch(/@keyframes|animation(?:-name)?:|transition:/);
    expect(css).not.toMatch(/color:\s*(?:red|green|blue|orange|white|black)\b/i);
  });

  it("keeps URL and governed-client ownership at the page boundary", () => {
    expect(page).toContain("useSearchParams()");
    expect(page).toContain("useApiClient()");
    expect(page).toContain("usePrincipal()");
    expect(page).toContain("serializeDocumentRouteState(next, searchParams)");
    expect(workspace).not.toMatch(/fetch\(|XMLHttpRequest|localStorage|sessionStorage/);
    expect(workspace).toContain("client.listDocuments(listQuery, controller.signal)");
    expect(workspace).toContain("visibleJourney.value.selected_version");
  });
});
