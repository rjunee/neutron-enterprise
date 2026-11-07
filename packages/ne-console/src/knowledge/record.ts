// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — the entity record, as pure functions.
//
// The record is assembled from ONE governed read: the entity's 1-hop
// neighborhood. Its own node carries the header (display name, class,
// assurance) and the properties; the edges around it carry the relations. These
// functions do the assembling and NOTHING else — no fetch, no DOM — so the two
// rules that matter are testable without either:
//
//   * A RELATION'S DIRECTION IS READ, NEVER GUESSED. The arrow comes from
//     comparing the edge's own from/to keys against the anchor. An edge whose
//     other end is not in the returned window still renders — its key is a real
//     served field — rather than being dropped, because a silently short list is
//     a wrong list.
//   * PROVENANCE IS SHOWN, NEVER REWRITTEN. A writer-stamped `llm:<sha16>:<ref>`
//     ref shows the underlying evidence ref first and keeps the FULL string for
//     the `title`. Nothing is reformatted beyond choosing which part leads.
//
// THIN CLIENT (C7): every field below is a field the graph routes returned.

import type { GraphNodeWire, SubgraphWire } from "../api/types";
import { isProvisionalAssurance, parseExtractedSourceRef } from "../primitives/assurance";

/** The anchored entity's own node in its neighborhood, or null when the window
 * holds no node with that key — which is what an unknown OR an invisible key
 * both look like from here. The console does not tell those apart, because the
 * server deliberately does not: an existence-leaking answer is the thing the
 * fail-closed empty exists to avoid. */
export function findEntity(sub: SubgraphWire, key: string): GraphNodeWire | null {
  return sub.nodes.find((node) => node.key === key) ?? null;
}

/** One row of the Properties table. `value` is the server's opaque map value,
 * rendered inert as text; an absent one stays null so the table renders the em
 * dash rather than the string "null". */
export interface PropertyRow {
  key: string;
  value: string | null;
}

/** An opaque property value as inert text. Objects and arrays serialise rather
 * than rendering "[object Object]"; empty string, null and undefined are
 * absent. Nothing here can produce a value the server did not send. */
export function propertyText(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value === "" ? null : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return null;
  }
}

/** The Properties table's rows, in the key order the server sent. */
export function propertyRows(node: GraphNodeWire): PropertyRow[] {
  return Object.entries(node.properties).map(([key, value]) => ({
    key,
    value: propertyText(value),
  }));
}

/** One row of the Relations table. */
export interface RelationRow {
  /** The edge's own key — the row identity. */
  key: string;
  edgeKind: string;
  /** "out": the anchor is the edge's subject. "in": the anchor is its object. */
  direction: "out" | "in";
  /** The entity at the other end. */
  otherKey: string;
  /** Its display name when the window returned its node; otherwise null, and
   * the table falls back to the key it does have. */
  otherName: string | null;
  otherClass: string | null;
  sourceSystem: string;
  sourceRef: string;
  /** The ASSERTING relation's own assurance — independent of either endpoint's. */
  assurance: string;
  provisional: boolean;
}

/**
 * The relations of `key`, one row per edge that touches it.
 *
 * A self-edge yields exactly one row (direction "out"): two rows for one
 * asserted relation would be the same fact counted twice, and the count above
 * the table is what a reader trusts.
 */
export function relationRows(sub: SubgraphWire, key: string): RelationRow[] {
  const byKey = new Map(sub.nodes.map((node) => [node.key, node]));
  const rows: RelationRow[] = [];
  for (const edge of sub.edges) {
    const outgoing = edge.from_key === key;
    const incoming = edge.to_key === key;
    if (!outgoing && !incoming) continue;
    const otherKey = outgoing ? edge.to_key : edge.from_key;
    const other = byKey.get(otherKey) ?? null;
    rows.push({
      key: edge.key,
      edgeKind: edge.edge_kind,
      direction: outgoing ? "out" : "in",
      otherKey,
      otherName: other?.display_name ?? null,
      otherClass: other?.entity_class ?? null,
      sourceSystem: edge.source_system,
      sourceRef: edge.source_ref,
      assurance: edge.assurance,
      provisional: isProvisionalAssurance(edge.assurance),
    });
  }
  return rows;
}

/** The distinct classes present among a relation set's other ends, sorted, for
 * the class filter. A relation whose other end is outside the window
 * contributes no class — it has none to contribute. */
export function relationClasses(rows: readonly RelationRow[]): string[] {
  const seen = new Set<string>();
  for (const row of rows) if (row.otherClass !== null) seen.add(row.otherClass);
  return [...seen].sort((a, b) => a.localeCompare(b, "en"));
}

/** The class filter, applied. "all" is the no-op. */
export function applyClassFilter(
  rows: readonly RelationRow[],
  entityClass: string,
): readonly RelationRow[] {
  if (entityClass === "all") return rows;
  return rows.filter((row) => row.otherClass === entityClass);
}

/** A `source_ref` split into what a person reads and what the `title` holds. */
export interface Provenance {
  /** The human-facing portion — the underlying evidence ref for a stamped
   * extraction, the ref itself otherwise. */
  text: string;
  /** ALWAYS the full, unmodified ref. */
  title: string;
}

/**
 * §7's provenance treatment. A writer-stamped `llm:<sha16>:<ref>` leads with
 * `<ref>` — the evidencing document is the part a reader can act on, and the
 * proposal digest in front of it is not. The full string survives in `title`
 * and is never shortened away.
 */
export function provenance(sourceRef: string): Provenance {
  const parsed = parseExtractedSourceRef(sourceRef);
  return { text: parsed === null ? sourceRef : parsed.evidenceRef, title: sourceRef };
}
