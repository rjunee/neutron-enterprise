// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import {
  GRAPH_PATH,
  parseGraphRouteState,
  serializeGraphRouteState,
  type GraphMode,
  type GraphRouteState,
} from "./graphRouteState";

type QueryInput = URLSearchParams | string;

function hrefFor(state: GraphRouteState, current: QueryInput = ""): string {
  const params = serializeGraphRouteState(state, current);
  const query = params.toString();
  return query === "" ? GRAPH_PATH : `${GRAPH_PATH}?${query}`;
}

/** Build a canonical Graph URL from complete typed state. */
export function graphHref(state: GraphRouteState, current: QueryInput = ""): string {
  return hrefFor(state, current);
}

/** Switch Graph/Record without losing selection, citations or discovery filters. */
export function graphModeHref(mode: GraphMode, current: QueryInput): string {
  return hrefFor({ ...parseGraphRouteState(current), mode }, current);
}

/**
 * Select an entity in either synchronized view. The existing depth, citation
 * context and discovery filters survive so Graph -> Record -> Graph can return
 * to the same exploration context.
 */
export function graphEntityHref(
  entity: string,
  options: { readonly mode?: GraphMode; readonly current?: QueryInput } = {},
): string {
  if (entity === "") throw new RangeError("graph entity key must not be empty");
  const current = options.current ?? "";
  const state = parseGraphRouteState(current);
  return hrefFor({ ...state, entity, mode: options.mode ?? state.mode }, current);
}

/**
 * Open citation/deep-link context without inventing an entity key. Repeated
 * source references remain distinct and ordered for the governed graph read.
 */
export function graphCitationsHref(
  citations: readonly string[],
  options: { readonly mode?: GraphMode; readonly current?: QueryInput } = {},
): string {
  const current = options.current ?? "";
  const state = parseGraphRouteState(current);
  return hrefFor(
    {
      ...state,
      citations: citations.filter((citation) => citation !== ""),
      mode: options.mode ?? "graph",
    },
    current,
  );
}
