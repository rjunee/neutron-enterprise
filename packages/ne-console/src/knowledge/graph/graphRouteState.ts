// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

export const GRAPH_PATH = "/knowledge/graph";
export const GRAPH_DEFAULT_DEPTH = 1;
export const GRAPH_MIN_DEPTH = 1;
export const GRAPH_MAX_DEPTH = 4;

export const GRAPH_MODES = ["graph", "record"] as const;
export const GRAPH_ASSURANCE_FILTERS = ["source", "confirmed", "extracted"] as const;

export type GraphMode = (typeof GRAPH_MODES)[number];
export type GraphAssuranceFilter = (typeof GRAPH_ASSURANCE_FILTERS)[number];

export type GraphEvidenceFocus =
  | {
      readonly state: "ready";
      readonly agentId: string;
      readonly runId: string;
      readonly evidenceId: string;
    }
  | { readonly state: "invalid" };

export interface GraphRouteState {
  /** Server-owned opaque key. Never trim, case-fold, or interpret it. */
  readonly entity: string | null;
  readonly mode: GraphMode;
  readonly depth: number;
  /** Governed graph entity anchors are repeatable, ordered opaque keys. */
  readonly anchors: readonly string[];
  /** Source references are repeatable and retain their incoming order. */
  readonly citations: readonly string[];
  readonly query: string;
  /** Generated entity-class identifier, stored under the human URL key `class`. */
  readonly entityClass: string;
  /** Server-owned source-system identifier. */
  readonly source: string;
  readonly assurance: GraphAssuranceFilter | null;
  /** Exact opaque Run-evidence tuple. Invalid/ambiguous tuples remain fail-closed. */
  readonly evidenceFocus?: GraphEvidenceFocus;
}

const OWNED_KEYS = [
  "entity",
  "mode",
  "depth",
  "anchor",
  "citation",
  "q",
  "class",
  "source",
  "assurance",
  // Discovery cursors are server-owned, filter-bound and deliberately
  // ephemeral. Any selected-entity, mode, depth or filter transition resets it.
  "cursor",
  "agent",
  "run",
  "evidence",
] as const;

function isOneOf<const T extends readonly string[]>(
  value: string | null,
  values: T,
): value is T[number] {
  return value !== null && values.some((candidate) => candidate === value);
}

function parseDepth(value: string | null): number {
  if (value === null || !/^\d+$/.test(value)) return GRAPH_DEFAULT_DEPTH;
  const depth = Number(value);
  return Number.isSafeInteger(depth) && depth >= GRAPH_MIN_DEPTH && depth <= GRAPH_MAX_DEPTH
    ? depth
    : GRAPH_DEFAULT_DEPTH;
}

function present(value: string | null): string | null {
  return value === null || value === "" ? null : value;
}

function parseEvidenceFocus(params: URLSearchParams): GraphEvidenceFocus | undefined {
  const keys = ["agent", "run", "evidence"] as const;
  if (!keys.some((key) => params.has(key))) return undefined;
  const values = keys.map((key) => params.getAll(key));
  const modes = params.getAll("mode");
  if (
    modes.length !== 1 ||
    modes[0] !== "graph" ||
    values.some((value) => value.length !== 1 || value[0] === "")
  ) {
    return { state: "invalid" };
  }
  if (params.has("entity") || params.has("anchor") || params.has("citation")) {
    return { state: "invalid" };
  }
  return {
    state: "ready",
    agentId: values[0]![0]!,
    runId: values[1]![0]!,
    evidenceId: values[2]![0]!,
  };
}

/**
 * Parse only canonical Graph-owned state. Legacy `/library` tab/anchor
 * compatibility belongs to PRE-DEMO-REHEARSAL-004's redirect boundary.
 */
export function parseGraphRouteState(input: URLSearchParams | string): GraphRouteState {
  const params = typeof input === "string" ? new URLSearchParams(input) : input;
  const mode = params.get("mode");
  const assurance = params.get("assurance");
  const evidenceFocus = parseEvidenceFocus(params);
  return {
    entity: present(params.get("entity")),
    mode: isOneOf(mode, GRAPH_MODES) ? mode : "graph",
    depth: parseDepth(params.get("depth")),
    anchors: params.getAll("anchor").filter((anchor) => anchor !== ""),
    citations: params.getAll("citation").filter((citation) => citation !== ""),
    query: params.get("q") ?? "",
    entityClass: params.get("class") ?? "",
    source: params.get("source") ?? "",
    assurance: isOneOf(assurance, GRAPH_ASSURANCE_FILTERS) ? assurance : null,
    ...(evidenceFocus === undefined ? {} : { evidenceFocus }),
  };
}

/**
 * Replace Graph-owned state while retaining unrelated query parameters. The
 * returned URLSearchParams performs standards-based encoding for every opaque
 * identifier; callers must not concatenate raw entity or citation values.
 */
export function serializeGraphRouteState(
  state: GraphRouteState,
  current: URLSearchParams | string = "",
): URLSearchParams {
  if (!isOneOf(state.mode, GRAPH_MODES)) throw new RangeError("invalid graph mode");
  if (
    !Number.isSafeInteger(state.depth) ||
    state.depth < GRAPH_MIN_DEPTH ||
    state.depth > GRAPH_MAX_DEPTH
  ) {
    throw new RangeError(`graph depth must be ${GRAPH_MIN_DEPTH}..${GRAPH_MAX_DEPTH}`);
  }
  if (state.assurance !== null && !isOneOf(state.assurance, GRAPH_ASSURANCE_FILTERS)) {
    throw new RangeError("invalid graph assurance filter");
  }

  const params = new URLSearchParams(typeof current === "string" ? current : current.toString());
  for (const key of OWNED_KEYS) params.delete(key);

  if (state.entity !== null && state.entity !== "") params.set("entity", state.entity);
  params.set("mode", state.mode);
  params.set("depth", String(state.depth));
  for (const anchor of state.anchors) {
    if (anchor !== "") params.append("anchor", anchor);
  }
  for (const citation of state.citations) {
    if (citation !== "") params.append("citation", citation);
  }
  if (state.query !== "") params.set("q", state.query);
  if (state.entityClass !== "") params.set("class", state.entityClass);
  if (state.source !== "") params.set("source", state.source);
  if (state.assurance !== null) params.set("assurance", state.assurance);
  if (
    state.evidenceFocus?.state === "ready" &&
    state.entity === null &&
    state.anchors.length === 0 &&
    state.citations.length === 0
  ) {
    params.set("agent", state.evidenceFocus.agentId);
    params.set("run", state.evidenceFocus.runId);
    params.set("evidence", state.evidenceFocus.evidenceId);
  }
  return params;
}
