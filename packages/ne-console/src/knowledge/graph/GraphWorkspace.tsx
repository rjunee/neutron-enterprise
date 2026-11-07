// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link } from "react-router";
import type { ApiClient } from "../../api/client";
import type { AgentEvidenceEnvelopeWire, SubgraphWire } from "../../api/types";
import { usePrincipal } from "../../auth/PrincipalContext";
import { useApiClient } from "../../auth/useApiClient";
import { Empty, Loading, LoadError, Refused } from "../../primitives/states";
import { formatCount } from "../../primitives/format";
import type { ChamberNode } from "../chamberSim";
import { toChamberGraph } from "../chamberSim";
import { GraphChamber } from "../GraphChamber";
import { classifyGraphFailure, type ReadState } from "../readState";
import { EntityGlyph, EntityRail } from "./EntityRail";
import type { EntityRailFilters } from "./entityRailState";
import type { GraphEntityHref } from "./GraphPathsFinder";
import { GraphQuickDrawer } from "./GraphQuickDrawer";
import { GraphRecord } from "./GraphRecord";
import { graphModeHref } from "./graphLinks";
import type { GraphRouteState } from "./graphRouteState";
import "./graphWorkspace.css";

const QUERY_DEPTH_CAP = 2;
const FINDER_FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** The classes the legend names, derived from the nodes actually painted on the
 * canvas — the ACL-trimmed subgraph already in hand, so this costs no request.
 *
 * It used to be the hardcoded `["Account", "Person", "Document", "Ticket",
 * "Order"]` — the same compile-time ne-schema registry behind the dead Class
 * dropdown, and just as wrong against the live graph (Customer / Document /
 * AutoLoan / ServicingNote). A legend labels what is drawn; deriving it from
 * the drawn nodes makes that true by construction rather than by maintenance,
 * and it can never name a class the viewer cannot see. */
function legendClasses(nodes: readonly ChamberNode[]): string[] {
  return [...new Set(nodes.map((node) => node.entity_class).filter((name) => name !== ""))].sort(
    (a, b) => a.localeCompare(b, "en"),
  );
}

interface GraphRequest {
  readonly anchorKeys: readonly string[];
  readonly citations: readonly string[];
  readonly depth: number;
  readonly neighborhood: boolean;
}

interface GraphReadOwner {
  readonly client: ApiClient;
  readonly principalStatus: "loading" | "ok" | "error";
  readonly principalId: string | null;
  readonly requestKey: string;
  readonly attempt: number;
}

interface OwnedGraphRead {
  readonly owner: GraphReadOwner;
  readonly state: ReadState<SubgraphWire>;
}

interface EvidenceFocusOwner {
  readonly client: ApiClient;
  readonly principalStatus: "loading" | "ok" | "error";
  readonly principalId: string | null;
  readonly focusKey: string;
}

type EvidenceFocusState =
  | { readonly phase: "none" }
  | { readonly phase: "loading" }
  | { readonly phase: "unavailable" }
  | {
      readonly phase: "ok";
      readonly focus:
        | { readonly kind: "entity"; readonly entityKey: string }
        | { readonly kind: "path"; readonly anchorKeys: readonly string[] };
    };

interface OwnedEvidenceFocus {
  readonly owner: EvidenceFocusOwner;
  readonly state: EvidenceFocusState;
}

function opaqueEntityKey(value: unknown): string | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const key = Reflect.get(value, "entity_key");
  return typeof key === "string" && key !== "" ? key : null;
}

function exactGraphPathAnchors(value: unknown): readonly string[] | null {
  if (!Array.isArray(value) || value.length === 0 || value.length > 32) return null;
  const anchors: string[] = [];
  let previousObject: string | null = null;
  for (const rawHop of value) {
    if (typeof rawHop !== "object" || rawHop === null || Array.isArray(rawHop)) return null;
    const subject = opaqueEntityKey(Reflect.get(rawHop, "subject"));
    const obj = opaqueEntityKey(Reflect.get(rawHop, "obj"));
    if (
      subject === null ||
      obj === null ||
      (previousObject !== null && subject !== previousObject)
    ) {
      return null;
    }
    if (!anchors.includes(subject)) anchors.push(subject);
    if (!anchors.includes(obj)) anchors.push(obj);
    previousObject = obj;
  }
  return anchors;
}

function exactEvidenceFocus(
  value: AgentEvidenceEnvelopeWire,
  agentId: string,
  runId: string,
  evidenceId: string,
): Extract<EvidenceFocusState, { phase: "ok" }>["focus"] | null {
  if (value.agent_id !== agentId || value.run_id !== runId) return null;
  const matches = value.evidence.filter((item) => item.evidence_id === evidenceId);
  if (matches.length !== 1) return null;
  const match = matches[0]!;
  if (match.state !== "available") return null;
  const entityId = match.locator?.entity_id;
  if (typeof entityId === "string" && entityId !== "") {
    return { kind: "entity", entityKey: entityId };
  }
  const anchorKeys = exactGraphPathAnchors(match.locator?.graph_path);
  return anchorKeys === null ? null : { kind: "path", anchorKeys };
}

function useEvidenceFocus(route: GraphRouteState): EvidenceFocusState {
  const client = useApiClient();
  const principal = usePrincipal();
  const principalId =
    principal.status === "ok" && principal.principal !== null ? principal.principal.id : null;
  const focus = route.evidenceFocus;
  const focusKey =
    focus?.state === "ready"
      ? JSON.stringify([focus.agentId, focus.runId, focus.evidenceId])
      : (focus?.state ?? "none");
  const owner = useMemo<EvidenceFocusOwner>(
    () => ({ client, principalStatus: principal.status, principalId, focusKey }),
    [client, focusKey, principal.status, principalId],
  );
  const [settled, setSettled] = useState<OwnedEvidenceFocus>({
    owner,
    state: { phase: "loading" },
  });

  useLayoutEffect(() => {
    if (focus?.state !== "ready" || principal.status !== "ok" || principalId === null) return;
    let active = true;
    void client.getAgentRunEvidence(focus.agentId, focus.runId).then(
      (value) => {
        if (!active) return;
        const exactFocus = exactEvidenceFocus(value, focus.agentId, focus.runId, focus.evidenceId);
        setSettled({
          owner,
          state:
            exactFocus === null ? { phase: "unavailable" } : { phase: "ok", focus: exactFocus },
        });
      },
      () => {
        if (active) setSettled({ owner, state: { phase: "unavailable" } });
      },
    );
    return () => {
      active = false;
    };
  }, [client, focus, owner, principal.status, principalId]);

  if (focus === undefined) return { phase: "none" };
  if (focus.state === "invalid") return { phase: "unavailable" };
  if (principal.status === "loading") return { phase: "loading" };
  if (principal.status !== "ok" || principalId === null) return { phase: "unavailable" };
  return settled.owner === owner ? settled.state : { phase: "loading" };
}

function requestFor(route: GraphRouteState): GraphRequest | null {
  if (route.mode !== "graph") return null;
  if (route.anchors.length === 0 && route.entity !== null) {
    return {
      anchorKeys: [route.entity],
      citations: [],
      depth: route.depth,
      neighborhood: true,
    };
  }
  const anchorKeys = route.anchors;
  if (anchorKeys.length === 0 && route.citations.length === 0) return null;
  const neighborhood = anchorKeys.length === 1 && route.citations.length === 0;
  return {
    anchorKeys,
    citations: route.citations,
    depth: neighborhood ? route.depth : Math.min(route.depth, QUERY_DEPTH_CAP),
    neighborhood,
  };
}

function exactCitationFocus(route: GraphRouteState, graph: SubgraphWire): string | null {
  if (
    route.mode !== "graph" ||
    route.entity !== null ||
    route.anchors.length > 0 ||
    route.citations.length === 0 ||
    graph.truncated
  ) {
    return null;
  }
  const citations = new Set(route.citations);
  const matches = new Set(
    graph.nodes.filter((node) => citations.has(node.source_ref)).map((node) => node.key),
  );
  return matches.size === 1 ? ([...matches][0] ?? null) : null;
}

function graphRequestKey(request: GraphRequest | null): string {
  if (request === null) return "idle";
  return JSON.stringify({
    anchorKeys: request.anchorKeys,
    citations: request.citations,
    depth: request.depth,
    neighborhood: request.neighborhood,
  });
}

function useOwnedGraphRead(route: GraphRouteState): {
  readonly owner: GraphReadOwner;
  readonly request: GraphRequest | null;
  readonly state: ReadState<SubgraphWire> | null;
  readonly retry: () => void;
} {
  const client = useApiClient();
  const principal = usePrincipal();
  const principalId =
    principal.status === "ok" && principal.principal !== null ? principal.principal.id : null;
  const [attempt, setAttempt] = useState(0);
  const request = useMemo(() => requestFor(route), [route]);
  const requestKey = graphRequestKey(request);
  const owner = useMemo<GraphReadOwner>(
    () => ({ client, principalStatus: principal.status, principalId, requestKey, attempt }),
    [attempt, client, principal.status, principalId, requestKey],
  );
  const [settled, setSettled] = useState<OwnedGraphRead>({
    owner,
    state: { phase: "loading" },
  });

  useLayoutEffect(() => {
    if (request === null || principal.status !== "ok" || principalId === null) return;
    let active = true;
    const read = request.neighborhood
      ? client.graphNeighborhood(request.anchorKeys[0] as string, { hops: request.depth })
      : client.graphQuery({
          anchor_keys: [...request.anchorKeys],
          anchor_citations: [...request.citations],
          hops: request.depth,
        });
    void read
      .then((value) => {
        if (active) setSettled({ owner, state: { phase: "ok", value } });
      })
      .catch((error: unknown) => {
        if (active) setSettled({ owner, state: { phase: classifyGraphFailure(error) } });
      });
    return () => {
      active = false;
    };
  }, [client, owner, principal.status, principalId, request]);

  const state =
    request === null
      ? null
      : principal.status === "loading"
        ? ({ phase: "loading" } as const)
        : principal.status !== "ok" || principalId === null
          ? ({ phase: "error" } as const)
          : settled.owner === owner
            ? settled.state
            : ({ phase: "loading" } as const);

  return {
    owner,
    request,
    state,
    retry: useCallback(() => setAttempt((value) => value + 1), []),
  };
}

function useNarrowLayout(): boolean {
  const query = "(max-width: 64rem)";
  const [narrow, setNarrow] = useState(
    () => typeof window !== "undefined" && window.matchMedia?.(query).matches === true,
  );
  useEffect(() => {
    if (typeof window === "undefined" || window.matchMedia === undefined) return;
    const media = window.matchMedia(query);
    const update = () => setNarrow(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);
  return narrow;
}

/** The canvas legend (screens/graph.html §canvas): one entry per class present
 * on the canvas, plus the dashed = unconfirmed rule, so the shapes never need
 * colour. Rendered only alongside a non-empty canvas, so an empty entry list is
 * unreachable here — the empty case is the canvas's own Empty state. */
function ChamberLegend({ entityClasses }: { entityClasses: readonly string[] }) {
  return (
    <div className="ne-legend" aria-label="Entity classes">
      {entityClasses.map((entityClass) => (
        <span key={entityClass} className="ne-legend__item">
          <EntityGlyph entityClass={entityClass} />
          {entityClass}
        </span>
      ))}
      <span className="ne-legend__item ne-legend__unconfirmed">
        <svg className="ne-legend__glyph" viewBox="0 0 16 16" aria-hidden="true">
          <line
            x1="2"
            y1="8"
            x2="14"
            y2="8"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeDasharray="3 2.5"
          />
        </svg>
        Unconfirmed
      </span>
    </div>
  );
}

export interface GraphWorkspaceProps {
  readonly route: GraphRouteState;
  readonly currentSearch: string;
  readonly entityHref: GraphEntityHref;
  readonly onFiltersChange: (filters: EntityRailFilters) => void;
  readonly onSelectEntity: (entityKey: string) => void;
  readonly onFirstVisibleEntity: (entityKey: string) => void;
  readonly onCitationFocusEntity: (entityKey: string) => void;
  readonly onDepthChange: (depth: number) => void;
}

export function GraphWorkspace({
  route,
  currentSearch,
  entityHref,
  onFiltersChange,
  onSelectEntity,
  onFirstVisibleEntity,
  onCitationFocusEntity,
  onDepthChange,
}: GraphWorkspaceProps) {
  const evidenceFocus = useEvidenceFocus(route);
  const effectiveRoute =
    evidenceFocus.phase === "ok"
      ? evidenceFocus.focus.kind === "entity"
        ? { ...route, entity: evidenceFocus.focus.entityKey, anchors: [], citations: [] }
        : { ...route, entity: null, anchors: evidenceFocus.focus.anchorKeys, citations: [] }
      : evidenceFocus.phase === "none"
        ? route
        : { ...route, entity: null, anchors: [], citations: [] };
  const graphRead = useOwnedGraphRead(effectiveRoute);
  const principal = usePrincipal();
  const principalId =
    principal.status === "ok" && principal.principal !== null ? principal.principal.id : null;
  const principalReady = principal.status === "ok" && principalId !== null;
  const principalKey = `${principal.status}:${principalId ?? "none"}`;
  const narrow = useNarrowLayout();
  const [finderOpen, setFinderOpen] = useState(false);
  const finderToggleRef = useRef<HTMLButtonElement>(null);
  const finderRef = useRef<HTMLDivElement>(null);
  const [drawer, setDrawer] = useState<{
    owner: GraphReadOwner;
    entityKey: string;
    opener: HTMLElement | null;
  } | null>(null);

  const filters = useMemo<EntityRailFilters>(
    () => ({
      query: effectiveRoute.query,
      entityClass: effectiveRoute.entityClass,
      source: effectiveRoute.source,
      assurance: effectiveRoute.assurance,
    }),
    [
      effectiveRoute.assurance,
      effectiveRoute.entityClass,
      effectiveRoute.query,
      effectiveRoute.source,
    ],
  );
  const effectiveDepth = graphRead.request?.depth ?? route.depth;
  const depthCap = graphRead.request?.neighborhood === false ? QUERY_DEPTH_CAP : 4;
  const visibleDrawer = drawer?.owner === graphRead.owner ? drawer : null;
  const finderModalOpen = narrow && finderOpen;
  const paneOpen = route.mode === "graph" && visibleDrawer !== null;

  useLayoutEffect(() => {
    if (!narrow || !finderOpen) return;
    finderRef.current?.querySelector<HTMLInputElement>('input[type="search"]')?.focus();
  }, [finderOpen, narrow]);

  const closeFinder = useCallback(() => {
    setFinderOpen(false);
    queueMicrotask(() => finderToggleRef.current?.focus());
  }, []);

  const trapFinderFocus = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (!finderModalOpen) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeFinder();
        return;
      }
      if (event.key !== "Tab") return;
      const stops = [...(finderRef.current?.querySelectorAll<HTMLElement>(FINDER_FOCUSABLE) ?? [])];
      const first = stops[0];
      const last = stops[stops.length - 1];
      if (first === undefined || last === undefined) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [closeFinder, finderModalOpen],
  );

  const chamberGraph = useMemo(
    () => (graphRead.state?.phase === "ok" ? toChamberGraph(graphRead.state.value) : null),
    [graphRead.state],
  );
  const litKeys = useMemo(() => new Set(graphRead.request?.anchorKeys ?? []), [graphRead.request]);
  const noHighlightedEdges = useMemo(() => new Set<string>(), []);
  const citationFocus =
    graphRead.state?.phase === "ok"
      ? exactCitationFocus(effectiveRoute, graphRead.state.value)
      : null;

  useEffect(() => {
    if (citationFocus !== null) onCitationFocusEntity(citationFocus);
  }, [citationFocus, onCitationFocusEntity]);

  return (
    <section className="ne-graphwork" aria-label="Knowledge graph explorer">
      <div className="ne-workbar">
        <div className="ne-workbar__title">
          <span className="ne-workbar__crumb">Knowledge Layer ›</span>
          <h1 className="ne-workbar__h1">Graph</h1>
        </div>
        <nav className="ne-segs" aria-label="Graph display" inert={finderModalOpen}>
          <Link
            className="ne-seg"
            aria-current={route.mode === "graph" ? "page" : undefined}
            to={graphModeHref("graph", currentSearch)}
          >
            Graph
          </Link>
          <Link
            className="ne-seg"
            aria-current={route.mode === "record" ? "page" : undefined}
            to={graphModeHref("record", currentSearch)}
          >
            Record
          </Link>
        </nav>
        {route.mode === "graph" && graphRead.request !== null ? (
          <label className="ne-depth">
            Depth
            <select
              aria-label="Graph depth"
              value={effectiveDepth}
              onChange={(event) => onDepthChange(Number(event.target.value))}
            >
              {Array.from({ length: depthCap }, (_, index) => index + 1).map((depth) => (
                <option key={depth} value={depth}>
                  {depth}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <button
          ref={finderToggleRef}
          type="button"
          className="ne-btn ne-btn--quiet ne-workbar__findbtn"
          aria-expanded={narrow ? finderOpen : true}
          aria-controls="ne-graph-entity-finder"
          onClick={() => setFinderOpen(true)}
        >
          Find entities
        </button>
        <div className="ne-workbar__spacer" />
        {route.mode === "graph" && graphRead.state?.phase === "ok" ? (
          <output className="ne-workbar__count" aria-live="polite">
            {formatCount(graphRead.state.value.nodes.length)} nodes ·{" "}
            {formatCount(graphRead.state.value.edges.length)} edges
          </output>
        ) : null}
      </div>

      <div className="ne-graphwork__floor" data-pane-open={paneOpen}>
        <div
          id="ne-graph-entity-finder"
          ref={finderRef}
          className="ne-graphwork__finder"
          role={narrow ? "dialog" : undefined}
          aria-modal={narrow ? "true" : undefined}
          aria-label={narrow ? "Entity finder sheet" : undefined}
          hidden={narrow && !finderOpen}
          onKeyDown={trapFinderFocus}
        >
          <button
            type="button"
            className="ne-btn ne-btn--quiet ne-graphwork__finder-close"
            onClick={closeFinder}
          >
            Close finder
          </button>
          {evidenceFocus.phase === "loading" || evidenceFocus.phase === "unavailable" ? null : (
            <EntityRail
              filters={filters}
              selectedEntityKey={effectiveRoute.entity}
              defaultBlocked={
                route.evidenceFocus !== undefined ||
                route.anchors.length > 0 ||
                route.citations.length > 0
              }
              onFiltersChange={onFiltersChange}
              onSelectEntity={(entityKey) => {
                onSelectEntity(entityKey);
                if (narrow) closeFinder();
              }}
              onFirstVisibleEntity={onFirstVisibleEntity}
            />
          )}
        </div>

        <section className="ne-graphwork__main" aria-label="Graph result" inert={finderModalOpen}>
          {evidenceFocus.phase === "loading" ? (
            <Loading things="the governed Run evidence" />
          ) : evidenceFocus.phase === "unavailable" ? (
            <Empty message="This governed evidence link is unavailable." />
          ) : effectiveRoute.mode === "record" ? (
            effectiveRoute.entity === null ? (
              <Empty message="Select a visible entity to open its record." />
            ) : principal.status === "loading" ? (
              <Loading things="the verified principal" />
            ) : !principalReady ? (
              <Empty message="No verified principal is available for this record." />
            ) : (
              <GraphRecord
                key={`${principalKey}:${effectiveRoute.entity}`}
                entityKey={effectiveRoute.entity}
                entityHref={entityHref}
              />
            )
          ) : graphRead.state === null ? (
            <Empty message="Choose a visible entity or follow a citation to explore the graph." />
          ) : graphRead.state.phase === "loading" ? (
            <Loading things="the graph" />
          ) : graphRead.state.phase === "refused" ? (
            <Refused thing="the knowledge graph" page="Graph" />
          ) : graphRead.state.phase === "off" ? (
            <Empty message="The knowledge graph isn't enabled on this deployment." />
          ) : graphRead.state.phase === "error" ? (
            <LoadError things="the graph" onRetry={graphRead.retry} />
          ) : chamberGraph === null || chamberGraph.nodes.length === 0 ? (
            <Empty message="No visible graph was returned for this selection." />
          ) : (
            <div className="ne-graphwork__canvas">
              <GraphChamber
                graph={chamberGraph}
                view={{
                  selectedKey: visibleDrawer?.entityKey ?? null,
                  litKeys,
                  highlightEdgeKeys: noHighlightedEdges,
                }}
                onSelectNode={(entityKey) => {
                  setDrawer({
                    owner: graphRead.owner,
                    entityKey,
                    opener:
                      document.activeElement instanceof HTMLElement ? document.activeElement : null,
                  });
                }}
              />
              <ChamberLegend entityClasses={legendClasses(chamberGraph.nodes)} />
              {graphRead.state.value.truncated ? (
                <div className="ne-chamber__notice" role="status">
                  This graph was capped by the server.
                </div>
              ) : null}
            </div>
          )}
        </section>

        {!paneOpen ? null : (
          <div className="ne-graphwork__pane-slot" inert={finderModalOpen}>
            <GraphQuickDrawer
              key={`${principalKey}:${visibleDrawer!.entityKey}`}
              entityKey={visibleDrawer!.entityKey}
              entityHref={entityHref}
              returnFocusTo={visibleDrawer!.opener}
              onClose={() => setDrawer(null)}
            />
          </div>
        )}
      </div>
    </section>
  );
}
