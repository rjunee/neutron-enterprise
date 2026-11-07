// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { excerpt, leadingHeading, parseResultRecord, resultIdentity } from "./resultRecord";

/** The shape two reviewers actually saw dumped on screen, escapes and all. */
const ENVELOPE = JSON.stringify({
  content: "# 2024 Renewal Agreement — Northwind Foods Ltd\n\nThis agreement renews the supply of…",
  doc_id: "doc-0316",
  owned_by: "per-0038",
  source_system: "acme-dms",
  title: "2024 Renewal Agreement — Northwind Foods Ltd",
  uri: "acme://corpus/doc-0316.md",
});

describe("parseResultRecord", () => {
  it("unwraps the envelope the server serialised into `text`", () => {
    const r = parseResultRecord(ENVELOPE);
    expect(r.wasEnvelope).toBe(true);
    expect(r.title).toBe("2024 Renewal Agreement — Northwind Foods Ltd");
    expect(r.docId).toBe("doc-0316");
    expect(r.sourceSystem).toBe("acme-dms");
    expect(r.ownedBy).toBe("per-0038");
    expect(r.uri).toBe("acme://corpus/doc-0316.md");
    // The body is the CONTENT, never the JSON.
    expect(r.body).not.toContain('"doc_id"');
    expect(r.body).toContain("This agreement renews");
  });

  it("INVENTS NOTHING when a field is absent", () => {
    const r = parseResultRecord(JSON.stringify({ content: "just the body" }));
    expect(r.wasEnvelope).toBe(true);
    expect(r.title).toBeUndefined();
    expect(r.docId).toBeUndefined();
    expect(r.sourceSystem).toBeUndefined();
    expect(r.ownedBy).toBeUndefined();
    expect(r.body).toBe("just the body");
  });

  it("returns plain prose UNCHANGED — the ordinary chunk case", () => {
    const plain = "Section 4.2 sets out the escalator mechanism.";
    const r = parseResultRecord(plain);
    expect(r.wasEnvelope).toBe(false);
    expect(r.body).toBe(plain);
    expect(r.title).toBeUndefined();
  });

  it("returns text that merely LOOKS like JSON unchanged rather than guessing", () => {
    for (const text of ["{not json", "[1,2,3]", '{"summary": "no content field"}']) {
      const r = parseResultRecord(text);
      expect(r.wasEnvelope, text).toBe(false);
      expect(r.body, text).toBe(text);
    }
  });
});

describe("resultIdentity — the pane headline, hash LAST (owner defect 2026-08-04)", () => {
  const HASH = "cbd4eaec540b509ce7624647adac0516";

  it("1: the envelope's own title wins", () => {
    const id = resultIdentity(
      parseResultRecord(ENVELOPE),
      { source_id: "acme/docs/doc-0316" },
      HASH,
    );
    expect(id).toEqual({ label: "2024 Renewal Agreement — Northwind Foods Ltd", kind: "title" });
  });

  it("2: with no title, the body's own leading heading — the owner's exact case", () => {
    const record = parseResultRecord("# 2025 Annual Engineering Summary\n\nThe year in review…");
    const id = resultIdentity(
      record,
      { source_id: "acme/docs/doc-0169", record_id: "doc-0169" },
      HASH,
    );
    expect(id).toEqual({ label: "2025 Annual Engineering Summary", kind: "heading" });
  });

  it("3: with neither, the connector's source path", () => {
    const record = parseResultRecord("Plain prose body with no heading.");
    const id = resultIdentity(
      record,
      { source_id: "acme/docs/doc-0169", record_id: "doc-0169" },
      HASH,
    );
    expect(id).toEqual({ label: "acme/docs/doc-0169", kind: "source-path" });
  });

  it("4: then the record id", () => {
    const record = parseResultRecord("Plain prose body with no heading.");
    const id = resultIdentity(record, { record_id: "doc-0169" }, HASH);
    expect(id).toEqual({ label: "doc-0169", kind: "record-id" });
  });

  it("5: the hash ONLY when nothing better was served — never fabricates", () => {
    const record = parseResultRecord("Plain prose body with no heading.");
    const id = resultIdentity(record, { page: 4 }, HASH);
    expect(id).toEqual({ label: HASH, kind: "document-id" });
  });

  it("an empty or whitespace metadata value is absent, not a headline", () => {
    const record = parseResultRecord("Plain prose body.");
    const id = resultIdentity(record, { source_id: "   ", record_id: "" }, HASH);
    expect(id.kind).toBe("document-id");
  });
});

describe("leadingHeading", () => {
  it("reads only a FIRST-line markdown heading, any level", () => {
    expect(leadingHeading("# Title\n\nBody")).toBe("Title");
    expect(leadingHeading("### Deep title\nBody")).toBe("Deep title");
    expect(leadingHeading("\n  # After leading whitespace\nBody")).toBe("After leading whitespace");
  });

  it("never invents one from prose, a mid-body heading, or a bare hash", () => {
    expect(leadingHeading("Prose first.\n# Heading later")).toBeUndefined();
    expect(leadingHeading("#hashtag not a heading")).toBeUndefined();
    expect(leadingHeading("#")).toBeUndefined();
    expect(leadingHeading("")).toBeUndefined();
  });
});

describe("excerpt", () => {
  it("resolves newlines and drops a heading the title already carries", () => {
    const e = excerpt(parseResultRecord(ENVELOPE));
    expect(e).not.toContain("\n");
    expect(e.startsWith("#")).toBe(false);
    expect(e).toContain("This agreement renews");
  });

  it("cuts on a word boundary and marks the cut", () => {
    const long = `${"word ".repeat(200)}end`;
    const e = excerpt(parseResultRecord(long), 40);
    expect(e.length).toBeLessThanOrEqual(41);
    expect(e.endsWith("…")).toBe(true);
    expect(e).not.toMatch(/wor…$/);
  });

  it("leaves a short body whole, with no ellipsis", () => {
    const e = excerpt(parseResultRecord("short body"), 40);
    expect(e).toBe("short body");
  });
});

/* -------------------------------------------- the m365 parsed-document -- */
//
// The owner's actual defect (agents citation pane, 2026-08-27): an m365
// SharePoint chunk carries `body` + `body_blocks` + `name` — no `content` key —
// so the old parser dumped the whole JSON envelope as the pane's body, titled
// with the raw source ref. The shape is ne-connect m365/contract.py's
// driveItem record over `ne.parsed_document/1` blocks.

describe("parseResultRecord — the m365 parsed-document envelope", () => {
  const M365 = JSON.stringify({
    id: "b!wBqUsy5qDUShBti-item",
    name: "Disaster Hardship Response SOP.md",
    web_url: "https://contoso.sharepoint.com/sites/ops/SOP.md",
    last_modified: "2026-08-01T00:00:00Z",
    graph_doc_type: "other",
    body_ingestion_status: "parsed",
    body: "Disaster Hardship Response SOP (2026)\n\nFor DR-4830, identify customers whose current address is in the pilot counties.",
    body_blocks: [
      { kind: "heading", text: "Disaster Hardship Response SOP (2026)", level: 1 },
      {
        kind: "paragraph",
        text: "For DR-4830, identify customers whose current address is in the pilot counties.",
      },
    ],
    body_block_count: 2,
    body_content_sha256: "a".repeat(64),
    body_schema_version: "ne.parsed_document/1",
    body_source_format: "md",
  });

  it("unwraps the envelope: prose from body_blocks, title from the display name", () => {
    const record = parseResultRecord(M365);
    expect(record.wasEnvelope).toBe(true);
    expect(record.title).toBe("Disaster Hardship Response SOP.md");
    expect(record.body).toBe(
      "Disaster Hardship Response SOP (2026)\n\nFor DR-4830, identify customers whose current address is in the pilot counties.",
    );
    expect(record.uri).toBe("https://contoso.sharepoint.com/sites/ops/SOP.md");
    // The pane must never dump the envelope again.
    expect(record.body).not.toContain("body_content_sha256");
    expect(record.body.trim().startsWith("{")).toBe(false);
  });

  it("titles the pane by the document's display name through the identity ladder", () => {
    const record = parseResultRecord(M365);
    const identity = resultIdentity(record, {}, "doc-hash");
    expect(identity).toEqual({ label: "Disaster Hardship Response SOP.md", kind: "title" });
  });

  it("falls back to the flat body when the blocks are malformed", () => {
    const record = parseResultRecord(
      JSON.stringify({
        name: "SOP.md",
        body: "flat body text",
        body_blocks: [{ kind: "paragraph" }],
        body_schema_version: "ne.parsed_document/1",
      }),
    );
    expect(record.wasEnvelope).toBe(true);
    expect(record.body).toBe("flat body text");
  });

  it("does NOT map the Graph driveItem id onto the knowledge document id", () => {
    // Different id space: the pane's Document field and the Documents deep
    // link resolve knowledge document_ids, and printing the driveItem id there
    // would be the console asserting an identity the server never stated.
    expect(parseResultRecord(M365).docId).toBeUndefined();
  });

  it("refuses to unwrap arbitrary JSON that merely has a `body` key", () => {
    const stranger = JSON.stringify({ body: "some text", other: 1 });
    const record = parseResultRecord(stranger);
    // No parsed-document marker, no unwrap: rendering it verbatim is the
    // console declining to decide what a document says.
    expect(record.wasEnvelope).toBe(false);
    expect(record.body).toBe(stranger);
  });

  it("returns re-embedded clean prose unchanged — the post-train-51 chunk case", () => {
    const prose = "For DR-4830, identify customers in the pilot counties.";
    expect(parseResultRecord(prose)).toEqual({ body: prose, wasEnvelope: false });
  });
});
