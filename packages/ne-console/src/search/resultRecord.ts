// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// RENDERING A RESULT AS A DOCUMENT, NOT AS A DEBUG DUMP.
//
// Review round 1, two independent reviewers: each result was headed with a
// 64-character chunk hash and its body was the raw JSON envelope printed as
// text, escape sequences and all —
//   {"content": "# 2024 Renewal Agreement…\n\nThis agreement…", "doc_id": …}
// — with the `title` the UI should have been showing sitting INSIDE the JSON it
// was dumping.
//
// This module is the parse. It is deliberately DEFENSIVE and deliberately
// NON-INVENTIVE:
//   - it reads only fields that are present on the record the server sent;
//   - it never synthesises a title, an owner or a source system;
//   - when `text` is not a JSON envelope (the ordinary case for a plain chunk)
//     it returns the text unchanged as the body and NO title, so the caller
//     falls back to the identifiers it already has.
// Nothing here is a client-side transform of MEANING — it is an unwrap of an
// envelope the server chose to serialize into a string field.

/** The presentable shape of one result body. Every field is optional because
 * every field is only present when the record actually carried it. */
export interface ResultRecord {
  /** The document's own title, when the envelope carried one. */
  title?: string;
  /** The readable body, with escaped newlines resolved. Always present. */
  body: string;
  /** Server-side identifiers the envelope carried, for the metadata row. */
  docId?: string;
  sourceSystem?: string;
  uri?: string;
  ownedBy?: string;
  /** True when `text` really was a JSON envelope (drives the Inspect note). */
  wasEnvelope: boolean;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() !== "" ? value : undefined;
}

/**
 * THE M365 PARSED-DOCUMENT ENVELOPE, unwrapped as readable prose.
 *
 * The m365-sharepoint connector's driveItem record carries no `content` field:
 * its parsed body rides in `body` (a string) beside `body_blocks` — the typed
 * heading/paragraph structure of `ne.parsed_document/1` (ne-connect
 * m365/contract.py; ne-brain ingest/contracts.py `DocBlock`) — and the
 * document's display name in `name`. Before this branch existed the pane
 * dumped that whole envelope as raw JSON, titled with the raw source ref.
 *
 * RECOGNISED EXPLICITLY, NOT GUESSED: this unwraps only an object whose `body`
 * is a string AND that carries a parsed-document marker (`body_blocks` array
 * or the `ne.parsed_document/…` schema tag). An arbitrary JSON object that
 * happens to have a `body` key is NOT an envelope this console knows, and
 * renders verbatim — the same restraint the `content` branch holds.
 *
 * `body_blocks` is preferred over the flat `body` because it is the document's
 * own structure: headings and paragraphs in order, each block's text verbatim.
 * Blocks are joined as plain lines; nothing is reworded, dropped, or added. A
 * malformed block (no string text) disqualifies the whole blocks path and the
 * flat `body` stands in.
 */
function parseM365Record(rec: Record<string, unknown>): ResultRecord | undefined {
  const flatBody = str(rec.body);
  if (flatBody === undefined) return undefined;
  const blocks = Array.isArray(rec.body_blocks) ? rec.body_blocks : undefined;
  const schemaTag = str(rec.body_schema_version);
  const isParsedDocument =
    blocks !== undefined ||
    (schemaTag !== undefined && schemaTag.startsWith("ne.parsed_document/"));
  if (!isParsedDocument) return undefined;
  let body = flatBody;
  if (blocks !== undefined && blocks.length > 0) {
    const lines: string[] = [];
    let wellFormed = true;
    for (const block of blocks) {
      if (typeof block !== "object" || block === null || Array.isArray(block)) {
        wellFormed = false;
        break;
      }
      const text = str((block as Record<string, unknown>).text);
      if (text === undefined) {
        wellFormed = false;
        break;
      }
      lines.push(text);
    }
    if (wellFormed && lines.length > 0) body = lines.join("\n\n");
  }
  return {
    // The document's display name ("Disaster Hardship Response SOP.md"),
    // verbatim — it feeds the identity ladder's first rung so the pane stops
    // headlining the source-ref hash. `id` is deliberately NOT mapped onto
    // `docId`: it is the Graph driveItem id, a different id space from the
    // knowledge document_id the pane's Document field and the Documents
    // deep-link resolve against.
    title: str(rec.name),
    body,
    uri: str(rec.web_url),
    wasEnvelope: true,
  };
}

/**
 * Unwrap a chunk's `text`. Pure; exported for its own test.
 *
 * A chunk whose text is a JSON object with a string `content` field is treated
 * as an envelope and unwrapped, and the m365 parsed-document record shape is
 * recognised explicitly beside it (see `parseM365Record`). Anything else —
 * plain prose (including the clean prose the re-embedded chunks now store), a
 * JSON array, an object matching neither known envelope — is returned verbatim
 * as the body, because guessing further would be the console deciding what a
 * document says.
 */
export function parseResultRecord(text: string): ResultRecord {
  const candidate = text.trim();
  if (!candidate.startsWith("{")) return { body: text, wasEnvelope: false };
  let parsed: unknown;
  try {
    parsed = JSON.parse(candidate);
  } catch {
    return { body: text, wasEnvelope: false };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { body: text, wasEnvelope: false };
  }
  const rec = parsed as Record<string, unknown>;
  const content = str(rec.content);
  if (content === undefined) {
    const m365 = parseM365Record(rec);
    if (m365 !== undefined) return m365;
    return { body: text, wasEnvelope: false };
  }
  return {
    title: str(rec.title),
    body: content,
    docId: str(rec.doc_id),
    sourceSystem: str(rec.source_system),
    uri: str(rec.uri),
    ownedBy: str(rec.owned_by),
    wasEnvelope: true,
  };
}

/**
 * THE HEADLINE A PERSON CAN READ (owner defect, 2026-08-04): the pane was
 * titled `cbd4eaec540b…` — the document_id, a SHA-256 over the record's
 * identity — while the record's own title sat four lines below it in the TEXT
 * field. Every candidate here is a SERVED value; none is synthesised:
 *
 *   1. the envelope's own `title` field;
 *   2. the body's leading markdown heading — the document's own first line,
 *      shown verbatim minus the `#` marks;
 *   3. `metadata.source_id` — the connector's resource path
 *      (`meridian/docs/art-dispute-sop-v4`), which a person can locate in the
 *      source system;
 *   4. `metadata.record_id` — the connector's record id;
 *   5. the document_id hash, LAST — it is what the audit chain records, so it
 *      stays available in the pane's Document field either way, but it is an
 *      identity for machines and it headlines only when nothing better was
 *      served.
 */
export interface ResultIdentity {
  label: string;
  /** Which served field the label came from — pinned so a fallback reordering
   * is a visible behaviour change, not a silent one. */
  kind: "title" | "heading" | "source-path" | "record-id" | "document-id";
}

/** The body's first line, when that line is a markdown heading. Returns the
 * heading TEXT — never invents one for a body that opens with prose. */
export function leadingHeading(body: string): string | undefined {
  const firstLine = body.trimStart().split("\n", 1)[0] ?? "";
  const match = /^#{1,6}\s+(.+?)\s*$/.exec(firstLine);
  return match ? match[1] : undefined;
}

export function resultIdentity(
  record: ResultRecord,
  metadata: Record<string, unknown>,
  documentId: string,
): ResultIdentity {
  if (record.title !== undefined) return { label: record.title, kind: "title" };
  const heading = leadingHeading(record.body);
  if (heading !== undefined) return { label: heading, kind: "heading" };
  const sourcePath = str(metadata["source_id"]);
  if (sourcePath !== undefined) return { label: sourcePath, kind: "source-path" };
  const recordId = str(metadata["record_id"]);
  if (recordId !== undefined) return { label: recordId, kind: "record-id" };
  return { label: documentId, kind: "document-id" };
}

/** A readable opening excerpt: the first `max` characters of the body, cut on a
 * word boundary, with a leading markdown heading line dropped when the title
 * already says the same thing. */
export function excerpt(record: ResultRecord, max = 320): string {
  let body = record.body.trim();
  if (record.title !== undefined) {
    const firstLine = body.split("\n", 1)[0] ?? "";
    if (/^#{1,6}\s/.test(firstLine)) body = body.slice(firstLine.length).trim();
  }
  const oneLine = body.replace(/\s*\n+\s*/g, " ").trim();
  if (oneLine.length <= max) return oneLine;
  const cut = oneLine.slice(0, max);
  const lastSpace = cut.lastIndexOf(" ");
  return `${(lastSpace > max * 0.6 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}
