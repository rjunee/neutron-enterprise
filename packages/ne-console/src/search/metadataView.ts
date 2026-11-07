// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// METADATA IS FIELDS, NOT A BLOB (owner defect, 2026-08-04). The chunk's
// `metadata.provenance` — provenance_class, source, actor, written_at,
// write_id, shape — rendered as one wrapped `JSON.stringify` string: every
// value meaningful, none of it readable. This module decides, per metadata
// value, whether it is a scalar to print or a structure to lay out as labelled
// rows.
//
// PRESENTATION ONLY, TOTAL, AND LOSSLESS. This is provenance: dropping any of
// it weakens the claim. A structured value keeps every key and every value —
// nothing is filtered, renamed, reformatted or summarised; a timestamp string
// stays the server's own string. A value that is not a plain object (arrays
// included) renders exactly as it did before, so nothing regresses to less
// than the blob.

export type MetadataView =
  | { kind: "scalar"; text: string }
  | { kind: "fields"; fields: ReadonlyArray<readonly [string, MetadataView]> };

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function scalarText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value) ?? "";
}

/**
 * How one metadata value renders. A plain object — or a string the server
 * chose to serialize an object into — becomes labelled rows, recursively.
 * Everything else is the same scalar text the pane always rendered.
 *
 * The string branch parses ONLY to a plain object: a string that is prose, a
 * JSON array, or malformed JSON is someone's value and renders verbatim.
 */
export function metadataView(value: unknown): MetadataView {
  if (isPlainObject(value)) {
    const entries = Object.entries(value);
    // An empty object still renders AS a value ("{}"), never as nothing — a
    // blank where a value stood would be an omission.
    if (entries.length === 0) return { kind: "scalar", text: "{}" };
    return {
      kind: "fields",
      fields: entries.map(([key, inner]) => [key, metadataView(inner)] as const),
    };
  }
  if (typeof value === "string" && value.trimStart().startsWith("{")) {
    try {
      const parsed: unknown = JSON.parse(value);
      if (isPlainObject(parsed)) return metadataView(parsed);
    } catch {
      // Not JSON after all — it is a string that happens to open with a brace.
    }
  }
  return { kind: "scalar", text: scalarText(value) };
}
