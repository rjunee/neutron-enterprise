// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { principalOwnerReady, type PrincipalOwner } from "../../auth/PrincipalContext";
import { Loading } from "../../primitives/states";
import type { EntityRailFilters } from "./entityRailState";
import { graphEntityHref, graphHref } from "./graphLinks";
import { parseGraphRouteState, type GraphMode, type GraphRouteState } from "./graphRouteState";
import { GraphWorkspace } from "./GraphWorkspace";

const GRAPH_OWNED_QUERY_KEYS = [
  "entity",
  "mode",
  "depth",
  "anchor",
  "citation",
  "q",
  "class",
  "source",
  "assurance",
  "cursor",
  "agent",
  "run",
  "evidence",
] as const;

function withoutGraphState(search: string): string {
  const params = new URLSearchParams(search);
  for (const key of GRAPH_OWNED_QUERY_KEYS) params.delete(key);
  const query = params.toString();
  return query === "" ? "" : `?${query}`;
}

export function GraphPage({ owner }: { readonly owner: PrincipalOwner }) {
  const location = useLocation();
  const navigate = useNavigate();
  const route = parseGraphRouteState(location.search);
  const currentGeneration = useRef(owner.generation);
  const ready = principalOwnerReady(owner);
  const [ownership, setOwnership] = useState<{
    readonly rendered: string;
    readonly established: string | null;
  }>({ rendered: owner.generation, established: ready ? owner.generation : null });
  if (ownership.rendered !== owner.generation) {
    // React restarts this render before commit, so an old-owner subtree is
    // never presented for one frame while the replacement owner is observed.
    setOwnership({
      rendered: owner.generation,
      established: ownership.established ?? (ready ? owner.generation : null),
    });
  }
  const sanitizedSearch = withoutGraphState(location.search);
  const ownerChanged = ownership.established !== null && ownership.established !== owner.generation;
  const routeNeedsReset = ownerChanged && sanitizedSearch !== location.search;

  // Close retained callbacks in the same commit that presents a new owner.
  useLayoutEffect(() => {
    currentGeneration.current = owner.generation;
  }, [owner.generation]);

  useLayoutEffect(() => {
    if (!routeNeedsReset) return;
    void navigate(
      {
        pathname: location.pathname,
        search: sanitizedSearch,
        hash: location.hash,
      },
      { replace: true },
    );
  }, [location.hash, location.pathname, navigate, routeNeedsReset, sanitizedSearch]);

  if (
    ownership.rendered === owner.generation &&
    ready &&
    !routeNeedsReset &&
    ownership.established !== owner.generation
  ) {
    // The inherited route is clean. Claim this generation before committing
    // its keyed workspace so its own future route updates are not re-scrubbed.
    setOwnership({ rendered: owner.generation, established: owner.generation });
  }

  const ownsCallback = useCallback(
    () => ready && !routeNeedsReset && currentGeneration.current === owner.generation,
    [owner.generation, ready, routeNeedsReset],
  );

  const updateRoute = useCallback(
    (next: GraphRouteState, replace = false) => {
      if (!ownsCallback()) return;
      void navigate(graphHref(next, location.search), { replace });
    },
    [location.search, navigate, ownsCallback],
  );

  const entityHref = useCallback(
    (entityKey: string, mode: GraphMode) =>
      graphEntityHref(entityKey, { current: location.search, mode }),
    [location.search],
  );

  const onFiltersChange = useCallback(
    (filters: EntityRailFilters) => {
      updateRoute(
        {
          ...route,
          query: filters.query,
          entityClass: filters.entityClass,
          source: filters.source,
          assurance: filters.assurance,
        },
        true,
      );
    },
    [route, updateRoute],
  );

  const onSelectEntity = useCallback(
    (entityKey: string) => updateRoute({ ...route, entity: entityKey, anchors: [], mode: "graph" }),
    [route, updateRoute],
  );

  const onFirstVisibleEntity = useCallback(
    (entityKey: string) => {
      if (route.entity !== null || route.anchors.length > 0 || route.citations.length > 0) return;
      updateRoute({ ...route, entity: entityKey, mode: "graph" }, true);
    },
    [route, updateRoute],
  );

  const onCitationFocusEntity = useCallback(
    (entityKey: string) => updateRoute({ ...route, entity: entityKey, mode: "graph" }, true),
    [route, updateRoute],
  );

  // Never retain or issue governed Graph reads while a replacement identity
  // is unsettled, or while its inherited route state is still being removed.
  if (!ready || routeNeedsReset) return <Loading things="the verified Graph owner" />;

  return (
    <>
      <GraphWorkspace
        key={owner.generation}
        route={route}
        currentSearch={location.search}
        entityHref={entityHref}
        onFiltersChange={onFiltersChange}
        onSelectEntity={onSelectEntity}
        onFirstVisibleEntity={onFirstVisibleEntity}
        onCitationFocusEntity={onCitationFocusEntity}
        onDepthChange={(depth) => updateRoute({ ...route, depth }, true)}
      />
    </>
  );
}
