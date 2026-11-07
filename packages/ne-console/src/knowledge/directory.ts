// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — LIBRARY › ENTITIES › THE WAY IN, as pure functions.
//
// THE PROBLEM THESE RULES SOLVE. The graph read routes take an exact key or a
// citation anchor and nothing else, so Entities was reachable only by someone
// who already held a key. The corpus was indexed and unreachable.
//
// THE SERVER ALREADY SERVES A BROWSE, and it is not the graph routes: a
// verified-answer template that declares NO arguments enumerates its own rows,
// ACL-trimmed, and hands back one `urn:ne:entity:<key>` citation per row. That
// citation is the SAME anchor a citation from Ask carries. So the way in is the
// citation path — deliberately, from a list the server itself decided, instead
// of by accident from somewhere else in the product.
//
// NOTHING HERE INVENTS A CATALOGUE. The rows are the server's rows, in the
// server's order; the columns are the template's own declared columns; the row
// count is the server's `returned_rows`; the cap is the server's `truncated`.
// This module reshapes served fields for a table and decides nothing else. When
// a deployment registers no zero-argument template there is NO browse, and the
// screen says so rather than assembling one out of something else's results.

import type { ArgsJsonSchema, VerifiedAnswerWire, VerifiedTemplateWire } from "../api/types";
import { ApiError } from "../api/client";
import { formatCount } from "../primitives/format";
import { parseEntityUrn } from "./entityUrn";

/**
 * True when a template declares no arguments at all, so it can be run WITHOUT
 * the console inventing an input.
 *
 * This is the whole eligibility rule and it is deliberately narrow. A template
 * with even one declared property needs a value chosen for it, and a value the
 * console chose is a filter the caller never asked for — the result would be a
 * subset presented as the whole. Those templates stay where they already are,
 * behind the run form in Reference, where a human supplies the argument.
 */
export function isZeroArg(schema: ArgsJsonSchema): boolean {
  return Object.keys(schema.properties ?? {}).length === 0 && (schema.required ?? []).length === 0;
}

/**
 * The registered templates that can open the corpus, by their own titles A–Z.
 *
 * The title is the server's `description`, rendered verbatim: it is the only
 * honest name for what the rows will be, because it is the sentence the person
 * who registered the template wrote about them.
 */
export function directoryTemplates(
  templates: readonly VerifiedTemplateWire[],
): VerifiedTemplateWire[] {
  return templates
    .filter((template) => isZeroArg(template.args_schema))
    .slice()
    .sort((a, b) => a.title.localeCompare(b.title, "en"));
}

/**
 * The DISPLAY order of a template result's columns: the human label leads, the
 * machine identifier trails, everything else keeps the template's own order.
 *
 * THE RULE, and why it lives here rather than in each table: the template
 * contract names two roles — `citation_var` is the identifier (the citation
 * anchor an auditor quotes) and `label_var` is the human label. A template
 * author writes columns in query order, which put 120 rows of bare key in the
 * first column of a screen a person reads. So WHEN a label is declared, it is
 * hoisted first and the identifier is demoted to the LAST column — demoted,
 * never dropped: the identifier stays rendered, monospaced and copyable,
 * because it is what the audit chain records.
 *
 * WHEN NO LABEL IS DECLARED (`labelVar` null or ABSENT, or naming no served
 * column) the served order is returned verbatim — identifier-led, because the
 * identifier is then the only identity the server vouched for, and inventing a
 * label out of some other column would be a substitution, not a formatting.
 *
 * ABSENT AND NULL ARE THE SAME DECLARATION HERE. An API serving the wire shape
 * from before `label_var` joined the template contract omits the key, so
 * `label_var` arrives `undefined` during a rolling upgrade; `VerifiedAnswers`
 * passes the served value through unguarded. The parameter admits it and the
 * nullish check covers both, so the degrade is a decision rather than a
 * coincidence of `indexOf(undefined)` returning -1.
 *
 * Returns indices into `columns`; every index appears exactly once (a
 * reordering, never a projection).
 */
export function displayOrder(
  columns: readonly string[],
  citationVar: string,
  labelVar: string | null | undefined,
): number[] {
  const label = labelVar == null ? -1 : columns.indexOf(labelVar);
  const citation = columns.indexOf(citationVar);
  if (label === -1 || label === citation) return columns.map((_, i) => i);
  const middle = columns.map((_, i) => i).filter((i) => i !== label && i !== citation);
  return citation === -1 ? [label, ...middle] : [label, ...middle, citation];
}

/** One row of the directory. Every field is a served field. */
export interface DirectoryRow {
  /** The row's position in the SERVER's result set — the row's identity here,
   * never a rank this console decided. */
  index: number;
  /** `rows[index]`, the template's own columns in the template's own order. */
  cells: readonly string[];
  /** `citations[index]` verbatim; `null` when the server sent none for it. */
  citation: string | null;
  /** The graph node key the citation names, or `null` when the citation is not
   * an entity urn. A row with no key is NOT a route into the record — it is
   * rendered and left alone, never linked to a key guessed from a cell. */
  entityKey: string | null;
}

/** The served answer as table rows, in the server's order. */
export function directoryRows(answer: VerifiedAnswerWire): DirectoryRow[] {
  return answer.rows.map((cells, index) => {
    const citation = answer.citations[index] ?? null;
    return {
      index,
      cells,
      citation,
      entityKey: citation === null ? null : parseEntityUrn(citation),
    };
  });
}

/**
 * The toolbar count: "{n} rows", or "{shown} of {returned} rows" while the
 * filter is narrowing them.
 *
 * `returned` is the server's `returned_rows`, so the second number is always
 * the size of the window the filter actually ran over — never a total the
 * server capped away. The cap itself is stated separately by
 * {@link truncationNote}, beside the table it shortened.
 */
export function directoryCountText(shown: number, returned: number): string {
  return shown === returned
    ? `${formatCount(returned)} rows`
    : `${formatCount(shown)} of ${formatCount(returned)} rows`;
}

/**
 * §7: `Showing {returned_rows}{total_rows ? " of {total_rows}" : ""} rows.` —
 * shown when the SERVER capped the result, and never otherwise.
 *
 * `total_rows` is `null` when the server could not vouch for a total; the
 * sentence then stops after the number it does have rather than printing a
 * ceiling nobody served.
 */
export function truncationNote(answer: VerifiedAnswerWire): string | null {
  if (!answer.truncated) return null;
  const total = answer.total_rows === null ? "" : ` of ${formatCount(answer.total_rows)}`;
  return `Showing ${formatCount(answer.returned_rows)}${total} rows.`;
}

/** What a failed directory read was. */
export type DirectoryFailure = "refused" | "off" | "error";

/**
 * Classify a failed verified-answer read.
 *
 * 403 is the role boundary and the ONLY thing that renders as one. 503 is the
 * verified-answer surface switched off in this deployment — a deployment fact,
 * not a permission and not a fault. Everything else, including a 404 from a
 * registry that changed under a loaded catalog and a network drop with no
 * status at all, is an error with a Retry.
 */
export function classifyDirectoryFailure(error: unknown): DirectoryFailure {
  if (error instanceof ApiError && error.status === 403) return "refused";
  if (error instanceof ApiError && error.status === 503) return "off";
  return "error";
}
