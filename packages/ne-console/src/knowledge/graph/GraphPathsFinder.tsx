// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import type { ApiClient } from "../../api/client";
import type { GraphPathWire, GraphPathsWire } from "../../api/types";
import { useApiClient } from "../../auth/useApiClient";
import { Empty, Loading, LoadError, Refused } from "../../primitives/states";
import { classifyGraphFailure } from "../readState";
import type { ReadState } from "../readState";
import type { GraphMode } from "./graphRouteState";

export type GraphEntityHref = (entityKey: string, mode: GraphMode) => string;

const EMPTY_PATHS: GraphPathsWire = { paths: [], truncated: false };

interface PathsOwner {
  readonly client: ApiClient;
  readonly from: string;
}

interface PathsView {
  readonly owner: PathsOwner;
  readonly open: boolean;
  readonly target: string;
  readonly lastTarget: string;
  readonly ran: boolean;
  readonly state: ReadState<GraphPathsWire>;
}

function emptyView(owner: PathsOwner): PathsView {
  return {
    owner,
    open: false,
    target: "",
    lastTarget: "",
    ran: false,
    state: { phase: "ok", value: EMPTY_PATHS },
  };
}

function GraphPath({ path, entityHref }: { path: GraphPathWire; entityHref: GraphEntityHref }) {
  const nodes = new Map(path.nodes.map((node) => [node.key, node]));
  if (path.edges.length === 0) {
    const node = path.nodes[0];
    return node === undefined ? null : (
      <Link to={entityHref(node.key, "record")}>{node.display_name}</Link>
    );
  }
  return (
    <ol className="ne-graph-record__path-edges" aria-label="Path relationships">
      {path.edges.map((edge) => {
        const source = nodes.get(edge.from_key);
        const target = nodes.get(edge.to_key);
        return (
          <li key={edge.key}>
            <Link to={entityHref(edge.from_key, "record")}>
              {source?.display_name ?? edge.from_key}
            </Link>
            <span aria-hidden="true"> — {edge.edge_kind} → </span>
            <span className="ne-visually-hidden"> {edge.edge_kind}, directed to </span>
            <Link to={entityHref(edge.to_key, "record")}>
              {target?.display_name ?? edge.to_key}
            </Link>
          </li>
        );
      })}
    </ol>
  );
}

export function GraphPathsFinder({
  from,
  entityHref,
}: {
  from: string;
  entityHref: GraphEntityHref;
}) {
  const client = useApiClient();
  const owner = useMemo<PathsOwner>(() => ({ client, from }), [client, from]);
  const sequenceRef = useRef(0);
  const [settledView, setSettledView] = useState<PathsView>(() => emptyView(owner));
  const view = settledView.owner === owner ? settledView : emptyView(owner);

  useLayoutEffect(() => {
    sequenceRef.current += 1;
    return () => {
      sequenceRef.current += 1;
    };
  }, [owner]);

  const run = useCallback(
    async (to: string) => {
      const sequence = ++sequenceRef.current;
      setSettledView((current) => ({
        ...(current.owner === owner ? current : emptyView(owner)),
        owner,
        ran: true,
        lastTarget: to,
        state: { phase: "loading" },
      }));
      try {
        const value = await client.graphPaths(from, to);
        if (sequenceRef.current !== sequence) return;
        setSettledView((current) =>
          current.owner === owner ? { ...current, state: { phase: "ok", value } } : current,
        );
      } catch (error) {
        if (sequenceRef.current !== sequence) return;
        setSettledView((current) =>
          current.owner === owner
            ? { ...current, state: { phase: classifyGraphFailure(error) } }
            : current,
        );
      }
    },
    [client, from, owner],
  );

  if (!view.open) {
    return (
      <button
        type="button"
        className="ne-btn ne-btn--quiet"
        onClick={() => {
          setSettledView({ ...view, owner, open: true });
        }}
      >
        Paths to another entity…
      </button>
    );
  }

  return (
    <section className="ne-graph-record__paths" aria-label="Paths to another entity">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (view.target !== "") void run(view.target);
        }}
      >
        <label>
          <span>Entity key</span>
          <input
            value={view.target}
            onChange={(event) => {
              const target = event.target.value;
              setSettledView((current) =>
                current.owner === owner ? { ...current, target } : current,
              );
            }}
          />
        </label>
        <button type="submit" className="ne-btn ne-btn--quiet" disabled={view.target === ""}>
          Find paths
        </button>
      </form>
      {!view.ran ? null : view.state.phase === "loading" ? (
        <Loading things="paths" />
      ) : view.state.phase === "refused" ? (
        <Refused thing="graph paths" page="Graph" />
      ) : view.state.phase === "off" ? (
        <Empty message="The knowledge graph isn't enabled on this deployment." />
      ) : view.state.phase === "error" ? (
        <LoadError things="paths" onRetry={() => void run(view.lastTarget)} />
      ) : (
        <>
          {view.state.value.truncated ? (
            <p className="ne-graph-record__note" role="status">
              These path results were capped by the server. More visible paths may exist.
            </p>
          ) : null}
          {view.state.value.paths.length === 0 ? (
            <Empty message="No visible path was returned." />
          ) : (
            <ol className="ne-graph-record__paths-list">
              {view.state.value.paths.map((path, index) => (
                <li key={`path-${index}`}>
                  <GraphPath path={path} entityHref={entityHref} />
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </section>
  );
}
