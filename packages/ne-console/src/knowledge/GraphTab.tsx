// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — LIBRARY › GRAPH. The visual explorer: anchor, hops, the canvas, and a
// node click that opens the same entity record the Entities tab shows.
//
// THE HOPS CONTROL OFFERS WHAT THE ROUTE ALLOWS AND NOTHING MORE. Two governed
// reads can draw a neighborhood and they cap differently: anchoring on ONE
// entity key uses the neighborhood route (up to 4 hops); anchoring on a
// citation — the shape Search's "View in Library" produces — must go through
// the subgraph route, which the server caps at 2. So the select's options are
// derived from the route the current anchor set actually uses. The alternative
// is offering 4, sending 4 and rendering 2 — a control that lies quietly.
//
// The truncation state is the toolbar's note, not a placard over the canvas:
// "you are looking at part of this" belongs beside the count of what you are
// looking at.
//
// THIN CLIENT (C7): the canvas lays out nodes the server returned. It fetches
// nothing itself, adds no node, and the truncation flag is the server's.

import { useCallback, useEffect, useMemo, useState } from "react";
import type { SubgraphWire } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { DetailPane } from "../primitives/DetailPane";
import { Empty, Loading, LoadError, Refused } from "../primitives/states";
import { FilterSelect, PrimaryButton, Toolbar } from "../primitives/Toolbar";
import { formatCount } from "../primitives/format";
import { toChamberGraph } from "./chamberSim";
import { GraphChamber } from "./GraphChamber";
import { EntityHeader, PropertiesTable, RelationsTable } from "./EntityRecord";
import { useEntityRecord } from "./useEntityRecord";
import { classifyGraphFailure } from "./readState";
import type { ReadState } from "./readState";
import { GRAPH_IDLE, GRAPH_OFF, noEntityWithKey, noGraphMatch } from "./copy";

/** The server's own caps, mirrored so the control cannot offer more than the
 * route will accept. */
const NEIGHBORHOOD_HOPS_CAP = 4;
const QUERY_HOPS_CAP = 2;

export function GraphTab({
  anchorKeys,
  anchorCitations,
  onAnchor,
}: {
  anchorKeys: readonly string[];
  anchorCitations: readonly string[];
  /** Anchoring writes the key to the URL, so the view is linkable. */
  onAnchor: (key: string) => void;
}) {
  const client = useApiClient();
  const [draft, setDraft] = useState(anchorKeys[0] ?? "");
  const [hops, setHops] = useState(1);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<ReadState<SubgraphWire>>({ phase: "loading" });
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  // Same render-phase adjustment as the Entities lookup: the anchor in the URL
  // is the truth, and the box shows it without a correcting second render.
  const [shownAnchor, setShownAnchor] = useState(anchorKeys[0] ?? "");
  if (shownAnchor !== (anchorKeys[0] ?? "")) {
    setShownAnchor(anchorKeys[0] ?? "");
    setDraft(anchorKeys[0] ?? "");
  }

  const anchored = anchorKeys.length > 0 || anchorCitations.length > 0;
  // One key and no citation is the neighborhood route; anything else has to go
  // through the subgraph route, whose hop cap is lower.
  const singleKey = anchorCitations.length === 0 && anchorKeys.length === 1;
  const hopCap = singleKey ? NEIGHBORHOOD_HOPS_CAP : QUERY_HOPS_CAP;
  const effectiveHops = Math.min(hops, hopCap);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      if (!anchored) {
        setState({ phase: "ok", value: { nodes: [], edges: [], truncated: false } });
        return;
      }
      setState({ phase: "loading" });
      try {
        const sub = singleKey
          ? await client.graphNeighborhood(anchorKeys[0] as string, { hops: effectiveHops })
          : await client.graphQuery({
              anchor_keys: [...anchorKeys],
              anchor_citations: [...anchorCitations],
              hops: effectiveHops,
            });
        if (cancelled) return;
        setState({ phase: "ok", value: sub });
      } catch (error) {
        if (cancelled) return;
        setState({ phase: classifyGraphFailure(error) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, anchored, singleKey, anchorKeys, anchorCitations, effectiveHops, attempt]);

  const graph = useMemo(() => (state.phase === "ok" ? toChamberGraph(state.value) : null), [state]);

  // The entity keys that opened this neighborhood are the reader's orientation
  // points. Keep them lit from the first settled frame instead of presenting a
  // constellation whose starting point is visually indistinguishable from every
  // related node. Citation-only anchors carry no entity key, so none is guessed.
  const litKeys = useMemo(() => new Set(anchorKeys), [anchorKeys]);
  const highlightEdgeKeys = useMemo(() => new Set<string>(), []);

  const hopOptions = useMemo(
    () =>
      Array.from({ length: hopCap }, (_, i) => ({
        value: String(i + 1),
        label: String(i + 1),
      })),
    [hopCap],
  );

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  const nodes = state.phase === "ok" ? state.value.nodes.length : null;
  const edges = state.phase === "ok" ? state.value.edges.length : null;

  return (
    <div className="ne-library__tab">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const key = draft.trim();
          if (key !== "") onAnchor(key);
        }}
      >
        <Toolbar
          search={{ value: draft, onChange: setDraft, placeholder: "Anchor an entity…" }}
          filters={
            <FilterSelect
              label="Hops"
              value={String(effectiveHops)}
              options={hopOptions}
              onChange={(next) => setHops(Number(next))}
            />
          }
          count={
            nodes === null || !anchored
              ? null
              : `${formatCount(nodes)} nodes · ${formatCount(edges)} edges`
          }
          actions={<PrimaryButton type="submit" label="Anchor" disabled={draft.trim() === ""} />}
        />
      </form>

      {state.phase === "ok" && state.value.truncated ? (
        <p className="ne-table__note" role="status">
          Showing the first {formatCount(nodes)} nodes — narrow the anchor to see the rest.
        </p>
      ) : null}

      {!anchored ? (
        <Empty message={GRAPH_IDLE} />
      ) : state.phase === "refused" ? (
        <Refused thing="the knowledge graph" page="Library" />
      ) : state.phase === "off" ? (
        <Empty message={GRAPH_OFF} />
      ) : state.phase === "error" ? (
        <LoadError things="the graph" onRetry={retry} />
      ) : state.phase === "loading" ? (
        <Loading things="the graph" />
      ) : graph === null || graph.nodes.length === 0 ? (
        // Loud, and specific: WHICH anchor found nothing, and why it can. The
        // generic invitation read like a design decision to the person who had
        // just followed a real citation here (owner defect, 2026-08-04).
        <Empty message={noGraphMatch(anchorKeys, anchorCitations)} />
      ) : (
        <GraphChamber
          graph={graph}
          view={{ selectedKey, litKeys, highlightEdgeKeys }}
          onSelectNode={setSelectedKey}
        />
      )}

      {selectedKey === null ? null : (
        <NodePane entityKey={selectedKey} onClose={() => setSelectedKey(null)} />
      )}
    </div>
  );
}

/**
 * §7: "Node click opens the same entity record in the detail pane."
 *
 * The SAME record — read the same way, from the entity's own neighborhood.
 * Deriving it from the drawn subgraph instead would be cheaper and wrong: at
 * more than one hop the window holds only the relations that happened to fall
 * inside it, and a partial relation list rendered under the heading "Relations"
 * is a wrong list.
 */
function NodePane({ entityKey, onClose }: { entityKey: string; onClose: () => void }) {
  const { state, retry } = useEntityRecord(entityKey);
  const node = state.phase === "ok" ? state.value.node : null;
  return (
    <DetailPane title={node?.display_name ?? entityKey} onClose={onClose}>
      {state.phase === "refused" ? (
        <Refused thing="the knowledge graph" page="Library" />
      ) : state.phase === "off" ? (
        <Empty message={GRAPH_OFF} />
      ) : state.phase === "error" ? (
        <LoadError things="the entity record" onRetry={retry} />
      ) : node === null && state.phase === "ok" ? (
        <Empty message={noEntityWithKey(entityKey)} />
      ) : (
        <>
          {node === null ? null : <EntityHeader node={node} level="h3" />}
          <PropertiesTable
            node={node}
            status={state.phase === "loading" ? "loading" : "ok"}
            onRetry={retry}
          />
          <RelationsTable
            rows={state.phase === "ok" ? state.value.relations : []}
            status={state.phase === "loading" ? "loading" : "ok"}
            subject={node?.display_name ?? entityKey}
            onRetry={retry}
            truncated={state.phase === "ok" && state.value.truncated}
            dense
          />
        </>
      )}
    </DetailPane>
  );
}
