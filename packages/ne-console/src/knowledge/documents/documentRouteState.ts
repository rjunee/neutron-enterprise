// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

export const DOCUMENT_STAGES = ["parse", "chunk", "acl", "embed", "graph", "supersede"] as const;

export const DOCUMENT_TYPES = [
  "pdf",
  "document",
  "presentation",
  "spreadsheet",
  "text",
  "other",
] as const;

export type DocumentStage = (typeof DOCUMENT_STAGES)[number];
export type DocumentType = (typeof DOCUMENT_TYPES)[number];

export type DocumentSelector =
  | { kind: "latest" }
  | { kind: "version"; version: number }
  | { kind: "as_of"; asOf: string }
  | {
      kind: "invalid";
      reason:
        "ambiguous_owned_parameter" | "conflicting_selectors" | "invalid_version" | "invalid_as_of";
      rawVersion: string | null;
      rawAsOf: string | null;
    };

export interface DocumentRouteState {
  /** Server-owned opaque identifier. Never case-fold, trim, or interpret it. */
  document: string | null;
  selector: DocumentSelector;
  stage: DocumentStage | null;
  query: string;
  documentType: DocumentType | null;
  source: string;
  /**
   * INTERIM (PD-DOC-TITLES): structured-ness inferred from the
   * ".negraph.json" connector filename convention; REMOVE when the real
   * `structured` flag lands on the document wire. Default `false` EXCLUDES
   * operational-graph records — the directory's default is prose documents
   * only. `true` is the explicit opt-in that puts them back in the list.
   */
  includeStructured: boolean;
}

const OWNED_KEYS = [
  "document",
  "version",
  "as_of",
  "stage",
  "q",
  "type",
  "source",
  "structured",
  "cursor",
];
const ISO_INSTANT =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})$/;

function isOneOf<const T extends readonly string[]>(
  value: string | null,
  values: T,
): value is T[number] {
  return value !== null && values.some((candidate) => candidate === value);
}

function positiveVersion(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function validAsOf(value: string): boolean {
  const match = ISO_INSTANT.exec(value);
  if (match === null) return false;
  const [, rawYear, rawMonth, rawDay, rawHour, rawMinute, rawSecond = "0"] = match;
  const year = Number(rawYear);
  const month = Number(rawMonth);
  const day = Number(rawDay);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  const maxDay = daysInMonth[month - 1] ?? 0;
  return (
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= maxDay &&
    Number(rawHour) <= 23 &&
    Number(rawMinute) <= 59 &&
    Number(rawSecond) <= 59 &&
    Number.isFinite(Date.parse(value))
  );
}

function selectorFrom(params: URLSearchParams): DocumentSelector {
  const rawVersion = params.get("version");
  const rawAsOf = params.get("as_of");
  if (rawVersion !== null && rawAsOf !== null) {
    return { kind: "invalid", reason: "conflicting_selectors", rawVersion, rawAsOf };
  }
  if (rawVersion !== null) {
    const version = positiveVersion(rawVersion);
    return version === null
      ? { kind: "invalid", reason: "invalid_version", rawVersion, rawAsOf: null }
      : { kind: "version", version };
  }
  if (rawAsOf !== null) {
    return validAsOf(rawAsOf)
      ? { kind: "as_of", asOf: rawAsOf }
      : { kind: "invalid", reason: "invalid_as_of", rawVersion: null, rawAsOf };
  }
  return { kind: "latest" };
}

function hasAmbiguousOwnedParameter(params: URLSearchParams): boolean {
  return OWNED_KEYS.some((key) => params.getAll(key).length > 1);
}

/** Parse document-owned query state without consuming legacy or cross-surface parameters. */
export function parseDocumentRouteState(input: URLSearchParams | string): DocumentRouteState {
  const params = typeof input === "string" ? new URLSearchParams(input) : input;
  if (hasAmbiguousOwnedParameter(params)) {
    return {
      document: null,
      selector: {
        kind: "invalid",
        reason: "ambiguous_owned_parameter",
        rawVersion: null,
        rawAsOf: null,
      },
      stage: null,
      query: "",
      documentType: null,
      source: "",
      includeStructured: false,
    };
  }
  const stage = params.get("stage");
  const documentType = params.get("type");
  return {
    document: params.get("document"),
    selector: selectorFrom(params),
    stage: isOneOf(stage, DOCUMENT_STAGES) ? stage : null,
    query: params.get("q") ?? "",
    documentType: isOneOf(documentType, DOCUMENT_TYPES) ? documentType : null,
    source: params.get("source") ?? "",
    includeStructured: params.get("structured") === "1",
  };
}

/**
 * Replace only document-owned state while retaining unrelated legacy/deep-link
 * parameters. The server cursor is deliberately ephemeral and is always removed.
 */
export function serializeDocumentRouteState(
  state: DocumentRouteState,
  current: URLSearchParams | string = "",
): URLSearchParams {
  if (state.selector.kind === "invalid") {
    throw new RangeError("cannot serialize invalid document selector state");
  }
  const params = new URLSearchParams(typeof current === "string" ? current : current.toString());
  for (const key of OWNED_KEYS) params.delete(key);

  if (state.document !== null) params.set("document", state.document);
  if (state.selector.kind === "version") {
    if (!Number.isSafeInteger(state.selector.version) || state.selector.version < 1) {
      throw new RangeError("document version must be a positive safe integer");
    }
    params.set("version", String(state.selector.version));
  } else if (state.selector.kind === "as_of") {
    if (!validAsOf(state.selector.asOf)) {
      throw new RangeError("document as_of must be an ISO-8601 instant");
    }
    params.set("as_of", state.selector.asOf);
  }
  if (state.stage !== null) params.set("stage", state.stage);
  if (state.query !== "") params.set("q", state.query);
  if (state.documentType !== null) params.set("type", state.documentType);
  if (state.source !== "") params.set("source", state.source);
  // INTERIM (PD-DOC-TITLES): structured-ness inferred from the
  // ".negraph.json" connector filename convention; REMOVE when the real
  // `structured` flag lands on the document wire. Omitted (the default)
  // EXCLUDES operational-graph records from the list.
  if (state.includeStructured) params.set("structured", "1");
  return params;
}
