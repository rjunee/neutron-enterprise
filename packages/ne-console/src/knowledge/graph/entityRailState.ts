// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { ApiError } from "../../api/client";
import type {
  GraphEntityFacetsWire,
  GraphEntityListQuery,
  GraphEntityListWire,
  GraphEntitySummaryWire,
} from "../../api/types";
import type { GraphAssuranceFilter } from "./graphRouteState";

export interface EntityRailFilters {
  readonly query: string;
  readonly entityClass: string;
  readonly source: string;
  readonly assurance: GraphAssuranceFilter | null;
}

export const EMPTY_ENTITY_RAIL_FILTERS: EntityRailFilters = {
  query: "",
  entityClass: "",
  source: "",
  assurance: null,
};

export function hasEntityRailFilters(filters: EntityRailFilters): boolean {
  return (
    filters.query !== "" ||
    filters.entityClass !== "" ||
    filters.source !== "" ||
    filters.assurance !== null
  );
}

/** A stable identity for the exact server-owned filter tuple. */
export function entityRailFilterKey(filters: EntityRailFilters): string {
  return JSON.stringify([filters.query, filters.entityClass, filters.source, filters.assurance]);
}

/** Build only the governed query fields the user supplied; the server owns defaults. */
export function entityRailQuery(filters: EntityRailFilters, cursor?: string): GraphEntityListQuery {
  const query: GraphEntityListQuery = {};
  if (filters.query !== "") query.q = filters.query;
  if (filters.entityClass !== "") query.entity_class = filters.entityClass;
  if (filters.source !== "") query.source = filters.source;
  if (filters.assurance !== null) query.assurance = filters.assurance;
  if (cursor !== undefined) query.cursor = cursor;
  return query;
}

const CLIENT_IDS = new WeakMap<object, number>();
let nextClientId = 1;

export function entityRailOwnerId(client: object, principalId: string): string {
  let clientId = CLIENT_IDS.get(client);
  if (clientId === undefined) {
    clientId = nextClientId;
    nextClientId += 1;
    CLIENT_IDS.set(client, clientId);
  }
  return `${clientId}:${principalId}`;
}

export interface OwnedEntityRailRequest {
  readonly ownerId: string;
  readonly filterKey: string;
  readonly requestKey: string;
  readonly sequence: number;
}

export function ownsEntityRailRequest(
  active: OwnedEntityRailRequest | null,
  candidate: OwnedEntityRailRequest,
): boolean {
  return (
    active?.ownerId === candidate.ownerId &&
    active.filterKey === candidate.filterKey &&
    active.requestKey === candidate.requestKey &&
    active.sequence === candidate.sequence
  );
}

export type EntityRailState =
  | { readonly phase: "loading" }
  | {
      readonly phase: "ready";
      readonly ownership: OwnedEntityRailRequest;
      readonly filterKey: string;
      readonly entities: readonly GraphEntitySummaryWire[];
      readonly nextCursor: string | null;
      readonly loadingMore: boolean;
    }
  | { readonly phase: "empty" }
  | { readonly phase: "no-match" }
  | { readonly phase: "off" }
  | { readonly phase: "refused" }
  | { readonly phase: "error" };

export function settleEntityRailPage(
  filters: EntityRailFilters,
  page: GraphEntityListWire,
  ownership: OwnedEntityRailRequest,
): EntityRailState {
  if (page.entities.length === 0) {
    return { phase: hasEntityRailFilters(filters) ? "no-match" : "empty" };
  }
  return {
    phase: "ready",
    ownership,
    filterKey: entityRailFilterKey(filters),
    entities: page.entities,
    nextCursor: page.next_cursor,
    loadingMore: false,
  };
}

/** Append a served page only to the exact filter window that requested its cursor. */
export function appendEntityRailPage(
  current: EntityRailState,
  requestedFilterKey: string,
  page: GraphEntityListWire,
  ownership: OwnedEntityRailRequest,
): EntityRailState | null {
  if (current.phase !== "ready" || current.filterKey !== requestedFilterKey) return null;
  return {
    ...current,
    ownership,
    entities: [...current.entities, ...page.entities],
    nextCursor: page.next_cursor,
    loadingMore: false,
  };
}

// --- the server-derived filter vocabulary (Class / Source) -----------------
//
// The defect this closes: both controls were client-authored. Class listed the
// compile-time ne-schema registry (Account/Document/Order/Person/Ticket) while
// the live graph held Customer/Document/AutoLoan/ServicingNote — four of five
// options filtered to nothing and three real classes could not be filtered at
// all — and Source was a free-text box, i.e. a guessing game. Both now render
// ONLY what GET /v1/graph/facets served for THIS principal. There is
// deliberately no client-side fallback list: a fallback is exactly how an
// invented vocabulary gets back onto the screen.

/** Em dash: the honest "nothing to offer" option label. Never a 0, never "No". */
export const FACET_ABSENT_LABEL = "\u2014";

export type FacetVocabularyState =
  | { readonly phase: "loading" }
  | {
      readonly phase: "ready";
      readonly entityClasses: readonly string[];
      readonly sourceSystems: readonly string[];
      readonly truncated: boolean;
    }
  /** The graph tier is off, the principal is refused, or the read failed. The
   * controls go inert — never repopulated from a built-in list. */
  | { readonly phase: "unavailable" };

export function settleFacetVocabulary(wire: GraphEntityFacetsWire): FacetVocabularyState {
  return {
    phase: "ready",
    entityClasses: wire.entity_classes,
    sourceSystems: wire.source_systems,
    truncated: wire.truncated,
  };
}

export interface FacetControl {
  readonly options: ReadonlyArray<{ value: string; label: string }>;
  readonly disabled: boolean;
}

/**
 * Build one filter control from the SERVED values only.
 *
 * `served` is `null` while the vocabulary is loading or unavailable, and `[]`
 * when the server honestly has nothing for this principal; both render a single
 * em-dash option on an inert control. A non-empty list renders "Any …" plus the
 * served values, verbatim and in served order.
 *
 * `active` (a filter already carried on the route) is echoed back as its own
 * option when the served list does not contain it — otherwise a deep link would
 * silently render as a different filter than the one being applied. That is the
 * ONLY value this function ever adds, and it came from the caller, not from a
 * built-in vocabulary.
 */
export function facetControlOptions(
  served: readonly string[] | null,
  anyLabel: string,
  active: string,
): FacetControl {
  const values = served ?? [];
  const extra = active !== "" && !values.includes(active) ? [active] : [];
  if (values.length === 0 && extra.length === 0) {
    return { options: [{ value: "", label: FACET_ABSENT_LABEL }], disabled: true };
  }
  return {
    options: [
      { value: "", label: anyLabel },
      ...[...values, ...extra].map((value) => ({ value, label: value })),
    ],
    disabled: false,
  };
}

/** The one factual line under the filters, or null when there is nothing to say. */
export function facetVocabularyNote(state: FacetVocabularyState): string | null {
  if (state.phase === "loading") return null;
  if (state.phase === "unavailable") {
    return "Class and Source are unavailable \u2014 the server didn\u2019t return the values in this graph.";
  }
  if (state.entityClasses.length === 0 && state.sourceSystems.length === 0) {
    return "Class and Source are empty \u2014 no entities are visible to you in this graph.";
  }
  if (state.truncated) {
    return "Class and Source list the values found so far; the server capped its scan, so there may be more.";
  }
  return null;
}

export type EntityRailFailurePhase = "off" | "refused" | "error";

export function classifyEntityRailFailure(error: unknown): EntityRailFailurePhase {
  if (error instanceof ApiError && error.status === 403) return "refused";
  if (error instanceof ApiError && (error.status === 404 || error.status === 503)) return "off";
  return "error";
}
