// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — LIBRARY › ENTITIES. The default tab, and the landing target when someone
// follows a citation from Ask or Search.
//
// THE DEGRADATION, EXACTLY AS SPECIFIED. Search-by-NAME does not exist: the
// graph read routes take an exact key or a citation anchor and nothing else. So
// the input takes an exact key, the empty result says where a key comes from,
// and this screen does not offer a capability the server has not got. When the
// name-search route lands, this input's submit changes and nothing else on the
// screen does.
//
// WITH NOTHING OPEN, THE SCREEN IS NOT A DEAD END. It was: an exact-key box and
// a sentence pointing at citations elsewhere, in front of a corpus that was
// indexed and had no door. The idle body is now the directory — the registered
// verified-answer templates that need no argument, run server-side, ACL-trimmed,
// each row carrying the `urn:ne:entity:<key>` citation that opens its record.
// Nothing about the exact-key path changed; it gained a way to be reached.
//
// The identity of the open entity lives in the URL, so a citation link from
// Ask, a relation link from this very table, and a typed key are all the same
// state — no effect has to copy one onto another.

import { useState } from "react";
import type { GraphNodeWire } from "../api/types";
import { Empty, LoadError, Refused } from "../primitives/states";
import { PrimaryButton, Toolbar } from "../primitives/Toolbar";
import { EntityDirectory } from "./EntityDirectory";
import { EntityHeader, PathsFinder, PropertiesTable, RelationsTable } from "./EntityRecord";
import { useEntityRecord } from "./useEntityRecord";
import type { RelationRow } from "./record";
import { GRAPH_OFF, NO_ENTITY } from "./copy";

export function EntitiesTab({
  entityKey,
  onOpenEntity,
  onOpenInGraph,
}: {
  /** The entity the URL names, or null when nothing is open. */
  entityKey: string | null;
  /** Submitting the lookup writes the key to the URL. */
  onOpenEntity: (key: string) => void;
  /** §7's "Open in graph" — switches to the Graph tab anchored here. */
  onOpenInGraph: (key: string) => void;
}) {
  const { state, retry } = useEntityRecord(entityKey);
  const [query, setQuery] = useState(entityKey ?? "");
  // The URL is the source of truth; when it changes under a MOUNTED tab (a
  // relation link, a citation, the browser's back button) the input follows it.
  // Adjusted during render rather than in an effect: an effect renders the
  // stale value first and then corrects it, which is a visible flicker on the
  // one control the reader is looking at.
  const [shownKey, setShownKey] = useState(entityKey);
  if (shownKey !== entityKey) {
    setShownKey(entityKey);
    setQuery(entityKey ?? "");
  }

  return (
    <div className="ne-library__tab">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const key = query.trim();
          if (key !== "") onOpenEntity(key);
        }}
      >
        <Toolbar
          search={{ value: query, onChange: setQuery, placeholder: "Find an entity…" }}
          count={null}
          actions={<PrimaryButton type="submit" label="Open" disabled={query.trim() === ""} />}
        />
      </form>
      <RecordBody
        entityKey={entityKey}
        state={state}
        retry={retry}
        onOpenEntity={onOpenEntity}
        onOpenInGraph={onOpenInGraph}
      />
    </div>
  );
}

function RecordBody({
  entityKey,
  state,
  retry,
  onOpenEntity,
  onOpenInGraph,
}: {
  entityKey: string | null;
  state: ReturnType<typeof useEntityRecord>["state"];
  retry: () => void;
  onOpenEntity: (key: string) => void;
  onOpenInGraph: (key: string) => void;
}) {
  // Nothing open: the way in, not a sentence about where a key comes from.
  if (entityKey === null) return <EntityDirectory onOpenEntity={onOpenEntity} />;
  // A 403 is the only thing that renders as a role boundary. A read that broke
  // says it broke; a deployment with the graph switched off says that.
  if (state.phase === "refused") return <Refused thing="the knowledge graph" page="Library" />;
  if (state.phase === "off") return <Empty message={GRAPH_OFF} />;
  if (state.phase === "error") return <LoadError things="the entity record" onRetry={retry} />;
  if (state.phase === "loading") {
    // §7: skeletons in each section's own geometry, plus the hidden
    // "Loading {section}" each table announces for itself.
    return (
      <Record
        node={null}
        relations={[]}
        status="loading"
        retry={retry}
        onOpenInGraph={onOpenInGraph}
      />
    );
  }
  if (state.value.node === null) return <Empty message={NO_ENTITY} />;
  return (
    <Record
      node={state.value.node}
      relations={state.value.relations}
      truncated={state.value.truncated}
      status="ok"
      retry={retry}
      onOpenInGraph={onOpenInGraph}
    />
  );
}

function Record({
  node,
  relations,
  truncated = false,
  status,
  retry,
  onOpenInGraph,
}: {
  node: GraphNodeWire | null;
  relations: readonly RelationRow[];
  truncated?: boolean;
  status: "loading" | "ok";
  retry: () => void;
  onOpenInGraph: (key: string) => void;
}) {
  return (
    <>
      {node === null ? null : (
        <div className="ne-library__record">
          <EntityHeader node={node} level="h2" />
          <div className="ne-library__actions">
            <button
              type="button"
              className="ne-btn ne-btn--quiet"
              onClick={() => onOpenInGraph(node.key)}
            >
              Open in graph
            </button>
            <PathsFinder from={node.key} />
          </div>
        </div>
      )}
      <PropertiesTable node={node} status={status} onRetry={retry} />
      <RelationsTable
        rows={relations}
        status={status}
        subject={node?.display_name ?? ""}
        onRetry={retry}
        truncated={truncated}
      />
    </>
  );
}
