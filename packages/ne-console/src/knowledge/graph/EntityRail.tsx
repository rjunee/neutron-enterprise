// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useApiClient } from "../../auth/useApiClient";
import { usePrincipal } from "../../auth/PrincipalContext";
import { isProvisionalAssurance } from "../../primitives/assurance";
import { FilterSelect } from "../../primitives/Toolbar";
import {
  appendEntityRailPage,
  classifyEntityRailFailure,
  entityRailFilterKey,
  entityRailOwnerId,
  entityRailQuery,
  facetControlOptions,
  facetVocabularyNote,
  ownsEntityRailRequest,
  settleEntityRailPage,
  settleFacetVocabulary,
  type EntityRailFilters,
  type EntityRailState,
  type FacetVocabularyState,
  type OwnedEntityRailRequest,
} from "./entityRailState";
import "./entityRail.css";

const SEARCH_DEBOUNCE_MS = 250;

// The Class and Source option lists are SERVER-DERIVED (GET /v1/graph/facets):
// exactly the classes and source systems present in this principal's own
// visible slice. There is no client-side fallback list on purpose. The previous
// Class list was glyphs.ts's ENTITY_GLYPHS — the compile-time ne-schema
// registry — which on the live graph offered Account/Order/Person/Ticket
// (nothing behind any of them) and could not offer Customer/AutoLoan/
// ServicingNote at all; Source was a free-text box over a closed set of
// crawled systems. Both were guesses dressed as controls.

const ASSURANCE_OPTIONS = [
  { value: "", label: "Any assurance" },
  { value: "source", label: "Source record" },
  { value: "confirmed", label: "Confirmed" },
  { value: "extracted", label: "Extracted" },
];

/** A quiet, shape-only glyph per canonical class (design §Knowledge Graph: the
 * shipped registry, class-is-shape, never colour). An unmapped class falls
 * back to a plain outlined square — visibly "unknown", never a borrowed
 * shape. Mirrors the shapes glyphs.ts paints on the canvas (CR-B10). */
export function EntityGlyph({ entityClass }: { entityClass: string }) {
  const common = {
    viewBox: "0 0 16 16",
    className: "ne-entity__glyph",
    "aria-hidden": true,
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.4,
  } as const;
  switch (entityClass) {
    case "Account":
      return (
        <svg {...common}>
          <rect x="3" y="3" width="10" height="10" />
        </svg>
      );
    case "Person":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="5" />
        </svg>
      );
    case "Document":
      return (
        <svg {...common}>
          <path d="M3.5 3h6l3 3v7h-9z" strokeLinejoin="miter" />
        </svg>
      );
    case "Ticket":
      return (
        <svg {...common}>
          <path d="M8 2.5L13.5 8L8 13.5L2.5 8z" />
        </svg>
      );
    case "Order":
      return (
        <svg {...common}>
          <path d="M8 2.2l5 2.9v5.8l-5 2.9-5-2.9V5.1z" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <rect x="5" y="5" width="6" height="6" />
        </svg>
      );
  }
}

export interface EntityRailProps {
  readonly filters: EntityRailFilters;
  readonly selectedEntityKey: string | null;
  /** True while anchor/citation context owns the route and rejects a default. */
  readonly defaultBlocked?: boolean;
  readonly onFiltersChange: (filters: EntityRailFilters) => void;
  readonly onSelectEntity: (entityKey: string) => void;
  /** The host may establish the bare-route focus from the first SERVED row only. */
  readonly onFirstVisibleEntity: (entityKey: string) => void;
}

function requestKey(filterKey: string, cursor?: string): string {
  return `${filterKey}\n${cursor === undefined ? "initial" : cursor}`;
}

export function EntityRail({
  filters,
  selectedEntityKey,
  defaultBlocked = false,
  onFiltersChange,
  onSelectEntity,
  onFirstVisibleEntity,
}: EntityRailProps) {
  const client = useApiClient();
  const principal = usePrincipal();
  const [state, setState] = useState<EntityRailState>({ phase: "loading" });
  const [facets, setFacets] = useState<FacetVocabularyState>({ phase: "loading" });
  const [attempt, setAttempt] = useState(0);
  const sequenceRef = useRef(0);
  const activeRef = useRef<OwnedEntityRailRequest | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const firstVisibleOwnerRef = useRef<string | null>(null);
  const renderOwnershipRef = useRef<{ ownerId: string | null; filterKey: string }>({
    ownerId: null,
    filterKey: "",
  });
  const [rovingStop, setRovingStop] = useState<{
    entityKey: string;
    ownerId: string | null;
    filterKey: string;
    selectedEntityKey: string | null;
  } | null>(null);

  const principalId =
    principal.status === "ok" && principal.principal !== null ? principal.principal.id : null;
  const ownerId = principalId === null ? null : entityRailOwnerId(client, principalId);
  const requestFilters = useMemo<EntityRailFilters>(
    () => ({
      query: filters.query,
      entityClass: filters.entityClass,
      source: filters.source,
      assurance: filters.assurance,
    }),
    [filters.assurance, filters.entityClass, filters.query, filters.source],
  );
  const filterKey = entityRailFilterKey(requestFilters);

  // Promise continuations can run after a new render commits but before the
  // preceding passive-effect cleanup. Commit these ownership facts in the
  // layout phase so that interval has no authority to publish rows/callbacks.
  useLayoutEffect(() => {
    renderOwnershipRef.current = { ownerId, filterKey };
  }, [filterKey, ownerId]);

  function ownsCurrentRender(candidate: OwnedEntityRailRequest): boolean {
    const current = renderOwnershipRef.current;
    return current.ownerId === candidate.ownerId && current.filterKey === candidate.filterKey;
  }

  useEffect(() => {
    abortRef.current?.abort();
    activeRef.current = null;
    let disposed = false;

    if (principal.status === "loading") {
      queueMicrotask(() => {
        if (!disposed) setState({ phase: "loading" });
      });
      return () => {
        disposed = true;
      };
    }
    if (principal.status === "error" || ownerId === null) {
      queueMicrotask(() => {
        if (!disposed) setState({ phase: "error" });
      });
      return () => {
        disposed = true;
      };
    }

    // The network call is debounced, but the previous filter window must stop
    // presenting immediately under the newly-edited controls.
    queueMicrotask(() => {
      if (!disposed) setState({ phase: "loading" });
    });
    const controller = new AbortController();
    abortRef.current = controller;
    let owned: OwnedEntityRailRequest | null = null;
    const timer = window.setTimeout(() => {
      owned = {
        ownerId,
        filterKey,
        requestKey: requestKey(filterKey),
        sequence: ++sequenceRef.current,
      };
      activeRef.current = owned;
      void client
        .graphEntities(entityRailQuery(requestFilters), controller.signal)
        .then((page) => {
          if (
            owned === null ||
            !ownsCurrentRender(owned) ||
            !ownsEntityRailRequest(activeRef.current, owned)
          ) {
            return;
          }
          setState(settleEntityRailPage(requestFilters, page, owned));
        })
        .catch((error: unknown) => {
          if (
            owned === null ||
            !ownsCurrentRender(owned) ||
            !ownsEntityRailRequest(activeRef.current, owned)
          ) {
            return;
          }
          setState({ phase: classifyEntityRailFailure(error) });
        });
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      disposed = true;
      window.clearTimeout(timer);
      controller.abort();
      if (owned !== null && ownsEntityRailRequest(activeRef.current, owned)) {
        activeRef.current = null;
      }
    };
  }, [attempt, client, filterKey, ownerId, principal.status, requestFilters]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      activeRef.current = null;
    },
    [],
  );

  // ONE facets request per principal per mount — the served vocabulary is a
  // property of what this principal can see, not of the filters applied, so it
  // is deliberately NOT keyed on filterKey and never debounced alongside the
  // search box. It is also never cached across principals: the effect re-runs
  // on ownerId, and a served list is dropped the moment the owner changes.
  useEffect(() => {
    let disposed = false;
    // Deferred like the rows effect above: a synchronous setState inside an
    // effect cascades renders (react-hooks/set-state-in-effect).
    const settle = (next: FacetVocabularyState) => {
      queueMicrotask(() => {
        if (!disposed) setFacets(next);
      });
    };
    if (principal.status === "loading") {
      settle({ phase: "loading" });
      return () => {
        disposed = true;
      };
    }
    if (principal.status === "error" || ownerId === null) {
      settle({ phase: "unavailable" });
      return () => {
        disposed = true;
      };
    }
    const controller = new AbortController();
    settle({ phase: "loading" });
    void client
      .graphFacets(controller.signal)
      .then((wire) => {
        if (!disposed) setFacets(settleFacetVocabulary(wire));
      })
      .catch(() => {
        // Off, refused and failed all land here. None of them licenses an
        // invented option list, so all three go inert rather than empty —
        // "refused" is not "this graph has no classes".
        if (!disposed) setFacets({ phase: "unavailable" });
      });
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [client, ownerId, principal.status]);

  function loadMore() {
    if (
      ownerId === null ||
      state.phase !== "ready" ||
      state.nextCursor === null ||
      state.filterKey !== filterKey ||
      state.loadingMore
    ) {
      return;
    }
    const cursor = state.nextCursor;
    const requestedFilterKey = state.filterKey;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const owned: OwnedEntityRailRequest = {
      ownerId,
      filterKey: requestedFilterKey,
      requestKey: requestKey(requestedFilterKey, cursor),
      sequence: ++sequenceRef.current,
    };
    activeRef.current = owned;
    setState({ ...state, ownership: owned, loadingMore: true });
    void client
      .graphEntities(entityRailQuery(requestFilters, cursor), controller.signal)
      .then((page) => {
        if (!ownsCurrentRender(owned) || !ownsEntityRailRequest(activeRef.current, owned)) return;
        setState(
          (current) => appendEntityRailPage(current, requestedFilterKey, page, owned) ?? current,
        );
      })
      .catch((error: unknown) => {
        if (!ownsCurrentRender(owned) || !ownsEntityRailRequest(activeRef.current, owned)) return;
        setState({ phase: classifyEntityRailFailure(error) });
      });
  }

  function moveOptionFocus(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (state.phase !== "ready") return;
    let next: number;
    if (event.key === "ArrowDown") next = Math.min(index + 1, state.entities.length - 1);
    else if (event.key === "ArrowUp") next = Math.max(index - 1, 0);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = state.entities.length - 1;
    else return;
    event.preventDefault();
    const entityKey = state.entities[next]?.key;
    if (entityKey !== undefined) {
      setRovingStop({ entityKey, ownerId, filterKey, selectedEntityKey });
    }
    optionRefs.current[next]?.focus();
  }

  // A ready window is presentable only while both the render-time context and
  // exact active request still own it. This closes the render/cleanup gap and
  // prevents old rows from flashing beneath newly committed controls.
  const presentedState = useMemo<EntityRailState>(
    () =>
      state.phase === "ready" &&
      (state.filterKey !== filterKey ||
        state.ownership.filterKey !== filterKey ||
        state.ownership.ownerId !== ownerId)
        ? { phase: "loading" }
        : state,
    [filterKey, ownerId, state],
  );

  // Default selection belongs to the exact ready window being presented, not
  // to the network continuation that happened to settle it. A non-null
  // selection consumes that default; returning to a bare route deliberately
  // opens a fresh consumption window without repeating discovery.
  useEffect(() => {
    if (selectedEntityKey !== null || defaultBlocked) {
      firstVisibleOwnerRef.current = null;
      return;
    }
    if (presentedState.phase !== "ready") return;
    const first = presentedState.entities[0];
    if (first === undefined) return;
    const firstVisibleOwner = `${presentedState.ownership.ownerId}\n${presentedState.filterKey}`;
    if (firstVisibleOwnerRef.current === firstVisibleOwner) return;
    firstVisibleOwnerRef.current = firstVisibleOwner;
    onFirstVisibleEntity(first.key);
  }, [defaultBlocked, onFirstVisibleEntity, presentedState, selectedEntityKey]);

  const tabbableEntityKey =
    presentedState.phase !== "ready"
      ? null
      : rovingStop !== null &&
          rovingStop.ownerId === ownerId &&
          rovingStop.filterKey === filterKey &&
          rovingStop.selectedEntityKey === selectedEntityKey &&
          presentedState.entities.some((entity) => entity.key === rovingStop.entityKey)
        ? rovingStop.entityKey
        : (presentedState.entities.find((entity) => entity.key === selectedEntityKey)?.key ??
          presentedState.entities[0]?.key ??
          null);
  const served = facets.phase === "ready" ? facets : null;
  const classControl = facetControlOptions(
    served === null ? null : served.entityClasses,
    "Any class",
    filters.entityClass,
  );
  const sourceControl = facetControlOptions(
    served === null ? null : served.sourceSystems,
    "Any source",
    filters.source,
  );
  const facetNote = facetVocabularyNote(facets);
  const loading = presentedState.phase === "loading";
  return (
    <aside className="ne-erail" aria-label="Entity finder" aria-busy={loading}>
      <form
        className="ne-erail__filters"
        role="search"
        onSubmit={(event) => event.preventDefault()}
      >
        <input
          className="ne-input"
          type="search"
          value={filters.query}
          placeholder="Name or exact key…"
          aria-label="Find an entity"
          autoComplete="off"
          onChange={(event) => onFiltersChange({ ...filters, query: event.target.value })}
        />
        <div className="ne-erail__selects">
          <FilterSelect
            label="Class"
            value={filters.entityClass}
            options={classControl.options}
            disabled={classControl.disabled}
            onChange={(entityClass) => onFiltersChange({ ...filters, entityClass })}
          />
          <FilterSelect
            label="Source"
            value={filters.source}
            options={sourceControl.options}
            disabled={sourceControl.disabled}
            onChange={(source) => onFiltersChange({ ...filters, source })}
          />
          <FilterSelect
            label="Assurance"
            value={filters.assurance ?? ""}
            options={ASSURANCE_OPTIONS}
            onChange={(assurance) =>
              onFiltersChange({
                ...filters,
                assurance:
                  assurance === "source" || assurance === "confirmed" || assurance === "extracted"
                    ? assurance
                    : null,
              })
            }
          />
        </div>
        {facetNote === null ? null : <p className="ne-erail__facet-note">{facetNote}</p>}
      </form>

      <div className="ne-erail__list" aria-live="polite">
        {presentedState.phase === "loading" ? (
          <p role="status">Loading visible entities…</p>
        ) : presentedState.phase === "empty" ? (
          <p>No visible entities are available.</p>
        ) : presentedState.phase === "no-match" ? (
          <p>No visible entities match these filters.</p>
        ) : presentedState.phase === "off" ? (
          <p>The knowledge graph isn&rsquo;t enabled on this deployment.</p>
        ) : presentedState.phase === "refused" ? (
          <p role="alert">Your role doesn&rsquo;t include graph entity discovery.</p>
        ) : presentedState.phase === "error" ? (
          <div role="alert">
            <p>Couldn&rsquo;t load visible entities.</p>
            <button
              type="button"
              className="ne-btn ne-btn--quiet"
              onClick={() => setAttempt((n) => n + 1)}
            >
              Retry
            </button>
          </div>
        ) : (
          <>
            <div role="listbox" aria-label="Visible entities">
              {presentedState.entities.map((entity, index) => (
                <button
                  key={`${entity.key}:${index}`}
                  ref={(node) => {
                    optionRefs.current[index] = node;
                  }}
                  type="button"
                  role="option"
                  aria-selected={entity.key === selectedEntityKey}
                  className="ne-entity"
                  title={entity.key}
                  tabIndex={entity.key === tabbableEntityKey ? 0 : -1}
                  onClick={() => onSelectEntity(entity.key)}
                  onKeyDown={(event) => moveOptionFocus(event, index)}
                >
                  <EntityGlyph entityClass={entity.entity_class} />
                  <span className="ne-entity__name">{entity.display_name}</span>
                  <span className="ne-entity__meta">
                    {entity.entity_class} · {entity.source_system}
                    {isProvisionalAssurance(entity.assurance) ? " · ◇ unconfirmed" : ""}
                  </span>
                </button>
              ))}
            </div>
            {presentedState.nextCursor === null ? null : (
              <button
                type="button"
                className="ne-btn ne-btn--quiet ne-erail__more"
                disabled={presentedState.loadingMore}
                onClick={loadMore}
              >
                {presentedState.loadingMore ? "Loading more…" : "Load more"}
              </button>
            )}
          </>
        )}
      </div>
      <div className="ne-erail__note">
        Find visible business records by human name or exact key.
      </div>
    </aside>
  );
}
