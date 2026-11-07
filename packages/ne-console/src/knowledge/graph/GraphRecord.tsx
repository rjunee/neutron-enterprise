// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { Link } from "react-router";
import type { ApiClient } from "../../api/client";
import type { GraphNodeWire } from "../../api/types";
import { useApiClient } from "../../auth/useApiClient";
import { StatusPill } from "../../primitives/StatePill";
import { Empty, Loading, LoadError, Refused } from "../../primitives/states";
import { isProvisionalAssurance } from "../../primitives/assurance";
import { findEntity, propertyRows, provenance, relationRows } from "../record";
import type { RelationRow } from "../record";
import type { ReadState } from "../readState";
import { classifyGraphFailure } from "../readState";
import type { EntityRecordData } from "../useEntityRecord";
import { GraphPathsFinder } from "./GraphPathsFinder";
import type { GraphEntityHref } from "./GraphPathsFinder";
import "./graphRecord.css";

const QUICK_PROPERTY_LIMIT = 4;
const QUICK_RELATION_LIMIT = 5;

interface EntityReadOwner {
  readonly client: ApiClient;
  readonly entityKey: string;
  readonly attempt: number;
}

interface OwnedEntityRead {
  readonly owner: EntityReadOwner;
  readonly state: ReadState<EntityRecordData>;
}

/**
 * The record read used by both depths of the canonical Graph experience.
 *
 * `useEntityRecord` remains the legacy Library seam. This successor binds the
 * settled value to the exact client, entity, and retry request that produced
 * it. The render-time owner comparison removes an already-served prior
 * principal/entity before effects run; the layout effect invalidates the old
 * request synchronously at commit, before the browser can paint it.
 */
function useOwnedEntityRecord(entityKey: string): {
  state: ReadState<EntityRecordData>;
  retry: () => void;
} {
  const client = useApiClient();
  const [attempt, setAttempt] = useState(0);
  const owner = useMemo<EntityReadOwner>(
    () => ({ client, entityKey, attempt }),
    [attempt, client, entityKey],
  );
  const [settled, setSettled] = useState<OwnedEntityRead>({
    owner,
    state: { phase: "loading" },
  });

  useLayoutEffect(() => {
    let active = true;
    void client
      .graphNeighborhood(entityKey, { hops: 1 })
      .then((sub) => {
        if (!active) return;
        setSettled({
          owner,
          state: {
            phase: "ok",
            value: {
              node: findEntity(sub, entityKey),
              relations: relationRows(sub, entityKey),
              truncated: sub.truncated,
            },
          },
        });
      })
      .catch((error: unknown) => {
        if (!active) return;
        setSettled({ owner, state: { phase: classifyGraphFailure(error) } });
      });
    return () => {
      active = false;
    };
  }, [client, entityKey, owner]);

  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  return {
    state: settled.owner === owner ? settled.state : { phase: "loading" },
    retry,
  };
}

export function OwnedEntityRecordRead({
  entityKey,
  children,
}: {
  entityKey: string;
  children: (read: ReturnType<typeof useOwnedEntityRecord>) => ReactNode;
}) {
  return children(useOwnedEntityRecord(entityKey));
}

/**
 * The provenance ref, as a copyable chip (screens/graph.html: "provenance
 * leads — the source system's own pointer as a copyable chip"). Reuses the
 * shared `.ne-hashchip` class (src/styles/table.css) rather than the
 * `HashChip` component: that component truncates to 8 characters for actual
 * hashes, which would mangle a short human-legible ref like `crm/mer-hh-001`.
 * The FULL ref is always in `title` and always what gets copied.
 */
function ProvenanceChip({ sourceRef }: { sourceRef: string }) {
  const ref = provenance(sourceRef);
  const [copied, setCopied] = useState(false);
  const copy = useCallback(() => {
    void navigator.clipboard?.writeText(ref.title).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      },
      () => {
        // Clipboard unavailable — the full ref is still in `title`.
      },
    );
  }, [ref.title]);
  return (
    <span className="ne-hashchip">
      <span className="ne-mono" title={ref.title}>
        {ref.text}
      </span>
      <button
        type="button"
        className="ne-hashchip__copy"
        aria-label="Copy provenance reference"
        onClick={copy}
      >
        <span aria-hidden="true">{copied ? "✓" : "⧉"}</span>
      </button>
    </span>
  );
}

/** The entity's own class · key line, plus its assurance/provenance — shared
 * by the docked pane's head (GraphQuickDrawer) and Record mode's own header
 * (GraphRecordView below). */
export function RecordIdentity({ node }: { node: GraphNodeWire }) {
  return (
    <>
      <p className="ne-record__subtitle">
        {node.entity_class} · {node.key}
      </p>
      <p className="ne-record__assur">
        {isProvisionalAssurance(node.assurance) ? <StatusPill word="UNCONFIRMED" /> : null}
        <ProvenanceChip sourceRef={node.source_ref} />
      </p>
    </>
  );
}

function PropertyTable({ node, quick }: { node: GraphNodeWire; quick: boolean }) {
  const allRows = useMemo(() => propertyRows(node), [node]);
  const rows = quick ? allRows.slice(0, QUICK_PROPERTY_LIMIT) : allRows;
  const headingId = useId();
  return (
    <section className="ne-graph-record__section" aria-labelledby={headingId}>
      <h3 id={headingId} className="ne-paneheading">
        Properties
      </h3>
      {rows.length === 0 ? (
        <Empty message="This entity carries no properties." />
      ) : (
        <div className="ne-graph-record__table-scroll">
          <table className="ne-proptable">
            <tbody>
              {rows.map((row) => (
                <tr key={row.key}>
                  <td>{row.key}</td>
                  <td>{row.value ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {quick && allRows.length > rows.length ? (
        <p className="ne-quicknote">
          Showing {rows.length} of {allRows.length} properties in quick view.
        </p>
      ) : null}
    </section>
  );
}

function RelationEntry({
  row,
  entityHref,
  mode,
  quick,
}: {
  row: RelationRow;
  entityHref: GraphEntityHref;
  mode: "graph" | "record";
  quick: boolean;
}) {
  return (
    <div className="ne-rel">
      <span className="ne-rel__dir" aria-hidden="true">
        {row.direction === "out" ? "→" : "←"}
      </span>
      <span className="ne-visually-hidden">
        {row.direction === "out" ? "Outgoing" : "Incoming"}
      </span>
      <span className="ne-rel__kind">{row.edgeKind}</span>
      <span className="ne-rel__other">
        <Link to={entityHref(row.otherKey, mode)}>{row.otherName ?? row.otherKey}</Link>
        {quick ? null : <ProvenanceChip sourceRef={row.sourceRef} />}
        {row.provisional ? <StatusPill word="UNCONFIRMED" /> : null}
      </span>
    </div>
  );
}

function RelationList({
  rows: allRows,
  entityHref,
  mode,
  quick,
}: {
  rows: readonly RelationRow[];
  entityHref: GraphEntityHref;
  mode: "graph" | "record";
  quick: boolean;
}) {
  const rows = quick ? allRows.slice(0, QUICK_RELATION_LIMIT) : allRows;
  const headingId = useId();
  return (
    <section className="ne-graph-record__section" aria-labelledby={headingId}>
      <h3 id={headingId} className="ne-paneheading">
        Relationships
      </h3>
      {rows.length === 0 ? (
        <Empty message="This entity has no relationships." />
      ) : (
        rows.map((row) => (
          <RelationEntry
            key={row.key}
            row={row}
            entityHref={entityHref}
            mode={mode}
            quick={quick}
          />
        ))
      )}
      {quick && allRows.length > rows.length ? (
        <p className="ne-quicknote">
          Showing {rows.length} of {allRows.length} relationships in quick view.
        </p>
      ) : null}
    </section>
  );
}

export function GraphRecordContent({
  data,
  entityHref,
  quick = false,
}: {
  data: EntityRecordData & { node: GraphNodeWire };
  entityHref: GraphEntityHref;
  quick?: boolean;
}) {
  return (
    <div
      className={
        quick
          ? "ne-graph-record__content ne-graph-record__content--quick"
          : "ne-graph-record__content"
      }
    >
      <PropertyTable node={data.node} quick={quick} />
      <RelationList
        rows={data.relations}
        entityHref={entityHref}
        mode={quick ? "graph" : "record"}
        quick={quick}
      />
      {data.truncated ? (
        <p className="ne-graph-record__note" role="status">
          This neighborhood was capped by the server. More visible relationships may exist.
        </p>
      ) : null}
    </div>
  );
}

export function GraphRecord({
  entityKey,
  entityHref,
}: {
  entityKey: string;
  entityHref: GraphEntityHref;
}) {
  return (
    <OwnedEntityRecordRead entityKey={entityKey}>
      {(read) => <GraphRecordView {...read} entityKey={entityKey} entityHref={entityHref} />}
    </OwnedEntityRecordRead>
  );
}

function GraphRecordView({
  entityKey,
  entityHref,
  state,
  retry,
}: {
  entityKey: string;
  entityHref: GraphEntityHref;
  state: ReadState<EntityRecordData>;
  retry: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useLayoutEffect(() => {
    headingRef.current?.focus();
  }, [entityKey]);
  const node = state.phase === "ok" ? state.value.node : null;
  return (
    <article className="ne-graph-record" aria-busy={state.phase === "loading"}>
      <RecordHeading node={node} headingRef={headingRef} />
      {state.phase === "loading" ? (
        <Loading things="the entity record" />
      ) : state.phase === "refused" ? (
        <Refused thing="the knowledge graph" page="Graph" />
      ) : state.phase === "off" ? (
        <Empty message="The knowledge graph isn't enabled on this deployment." />
      ) : state.phase === "error" ? (
        <LoadError things="the entity record" onRetry={retry} />
      ) : state.value.node === null ? (
        <Empty message="No visible entity record was returned." />
      ) : (
        <>
          <GraphRecordContent
            data={{ ...state.value, node: state.value.node }}
            entityHref={entityHref}
          />
          <div className="ne-graph-record__actions">
            <Link className="ne-btn ne-btn--quiet" to={entityHref(entityKey, "graph")}>
              Explore in graph
            </Link>
            <GraphPathsFinder key={entityKey} from={entityKey} entityHref={entityHref} />
          </div>
        </>
      )}
    </article>
  );
}

function RecordHeading({
  node,
  headingRef,
}: {
  node: GraphNodeWire | null;
  headingRef?: RefObject<HTMLHeadingElement | null>;
}) {
  return (
    <header className="ne-graph-record__header">
      <h2 ref={headingRef} tabIndex={-1}>
        {node?.display_name ?? "Entity record"}
      </h2>
      {node === null ? null : <RecordIdentity node={node} />}
    </header>
  );
}
