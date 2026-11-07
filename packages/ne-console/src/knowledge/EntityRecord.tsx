// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — THE ENTITY RECORD. The landing target when someone follows a citation,
// and the answer to "where did this fact come from" without a graph query
// language.
//
// One governed read builds the whole record: the entity's 1-hop neighborhood.
// Its own node is the header and the properties; the edges around it are the
// relations. Nothing on this record is computed — the class, the assurance, the
// direction of every relation and the provenance ref are all served fields
// (C7).
//
// THE UNCONFIRMED PILL IS A SERVER FACT, NOT A JUDGEMENT. It renders when the
// server's `assurance` is not one a human has vouched for, and an UNRECOGNISED
// label counts as unconfirmed — failing toward flagging, never toward trust,
// which is the server's own stance and the only safe direction when the label
// is the thing that might be wrong.
//
// A FAILED READ IS NOT A REFUSAL. The refused block appears for a 403 and for
// nothing else (readState.ts); a read that broke says it broke and offers Retry.

import { useCallback, useMemo, useState } from "react";
import { Link } from "react-router";
import type { GraphNodeWire, GraphPathWire, GraphPathsWire } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { DataTable } from "../primitives/DataTable";
import type { Column } from "../primitives/DataTable";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { StatusPill } from "../primitives/StatePill";
import { Empty, Loading, LoadError, Refused } from "../primitives/states";
import { FilterChip, FilterSelect, Toolbar, ToolbarButton } from "../primitives/Toolbar";
import { buildCsv, downloadCsv } from "../primitives/csv";
import { isProvisionalAssurance } from "../primitives/assurance";
import { formatCount } from "../primitives/format";
import { applyClassFilter, propertyRows, provenance, relationClasses } from "./record";
import type { PropertyRow, RelationRow } from "./record";
import { classifyGraphFailure } from "./readState";
import type { ReadState } from "./readState";

/** The pill's `title`, spelled once. */
const UNCONFIRMED_TITLE = "Extracted by a model and not yet confirmed against a source";

/** The UNCONFIRMED pill with the sentence that says what it means, in `title`. */
export function UnconfirmedPill() {
  return (
    <span title={UNCONFIRMED_TITLE}>
      <StatusPill word="UNCONFIRMED" />
    </span>
  );
}

/** §7's record header: display name, class chip, and the pill when the server's
 * assurance is not one a human vouched for. */
export function EntityHeader({ node, level }: { node: GraphNodeWire; level: "h2" | "h3" }) {
  const Heading = level;
  return (
    <div className="ne-library__recordhead">
      <Heading className="ne-library__recordname">{node.display_name}</Heading>
      <span className="ne-library__class">{node.entity_class}</span>
      {isProvisionalAssurance(node.assurance) ? <UnconfirmedPill /> : null}
    </div>
  );
}

const PROPERTY_COLUMNS: ReadonlyArray<Column<PropertyRow>> = [
  {
    key: "key",
    header: "Key",
    cell: (row) => row.key,
    sortValue: (row) => row.key,
    searchText: (row) => row.key,
  },
  {
    key: "value",
    header: "Value",
    cell: (row) => row.value,
    sortValue: (row) => row.value,
    searchText: (row) => row.value ?? "",
  },
];

/** §7's Properties table. Values are rendered inert: whatever the opaque map
 * holds arrives as text and is never interpreted. */
export function PropertiesTable({
  node,
  status = "ok",
  onRetry,
}: {
  node: GraphNodeWire | null;
  status?: "loading" | "ok" | "refused" | "error";
  /** Not optional: an error state with no way out of it is a dead end. */
  onRetry: () => void;
}) {
  const rows = useMemo(() => (node === null ? [] : propertyRows(node)), [node]);
  return (
    <section className="ne-library__section" aria-label="Properties">
      <h3 className="ne-library__sectionhead">Properties</h3>
      <DataTable<PropertyRow>
        columns={PROPERTY_COLUMNS}
        rows={rows}
        rowKey={(row) => row.key}
        label="Properties"
        things="properties"
        defaultSort={{ key: "key", dir: "asc" }}
        status={status}
        empty={<Empty message="This entity carries no properties." />}
        refused={<Refused thing="the knowledge graph" page="Library" />}
        error={<LoadError things="properties" onRetry={onRetry} />}
      />
    </section>
  );
}

function DirectionCell({ direction }: { direction: RelationRow["direction"] }) {
  return (
    <span className="ne-library__dir">
      <span aria-hidden="true">{direction === "out" ? "→" : "←"}</span>
      <span className="ne-visually-hidden">{direction === "out" ? "Outgoing" : "Incoming"}</span>
    </span>
  );
}

const RELATION_COLUMNS: ReadonlyArray<Column<RelationRow>> = [
  {
    key: "relation",
    header: "Relation",
    mono: true,
    cell: (row) => row.edgeKind,
    sortValue: (row) => row.edgeKind,
    searchText: (row) => row.edgeKind,
  },
  {
    key: "direction",
    header: "Direction",
    width: "96px",
    cell: (row) => <DirectionCell direction={row.direction} />,
  },
  {
    key: "entity",
    header: "Entity",
    cell: (row) => (
      // The link RE-ANCHORS the record on the other end (§7). It stops the
      // row's own click, which opens this relation's provenance — one row,
      // two destinations, and the reader chooses by where they aim.
      <Link
        to={`/library?tab=entities&entity=${encodeURIComponent(row.otherKey)}`}
        className={row.otherName === null ? "ne-mono" : undefined}
        onClick={(event) => event.stopPropagation()}
      >
        {row.otherName ?? row.otherKey}
      </Link>
    ),
    sortValue: (row) => row.otherName ?? row.otherKey,
    searchText: (row) => `${row.otherName ?? ""} ${row.otherKey}`,
  },
  {
    key: "source",
    header: "Source",
    cell: (row) => row.sourceSystem,
    sortValue: (row) => row.sourceSystem,
    searchText: (row) => row.sourceSystem,
  },
  {
    key: "status",
    header: "Status",
    width: "148px",
    // §7: "UNCONFIRMED pill or nothing." Not an em dash — the dash is this
    // console's mark for a field the server did not send, and `assurance` is
    // always sent. A vouched relation has a status; its status is unremarkable.
    cell: (row) => (row.provisional ? <UnconfirmedPill /> : <></>),
  },
];

/**
 * §7's Relations table: sortable on Relation, Entity and Source, default
 * Relation A–Z, with the class filter and the export beside its count.
 *
 * A row click opens the ASSERTING record's provenance in the detail pane —
 * which is the whole reason this screen exists.
 */
export function RelationsTable({
  rows,
  status,
  subject,
  onRetry,
  truncated = false,
  dense,
}: {
  rows: readonly RelationRow[];
  status: "loading" | "ok" | "refused" | "error";
  /** The entity these relations belong to — the export names it. */
  subject: string;
  onRetry: () => void;
  /** The server capped the neighborhood that supplied these relations. */
  truncated?: boolean;
  /** Inside the 420px detail pane: no toolbar, the table alone. */
  dense?: boolean;
}) {
  const [entityClass, setEntityClass] = useState("all");
  const [openKey, setOpenKey] = useState<string | null>(null);

  const classes = useMemo(() => relationClasses(rows), [rows]);
  const shown = useMemo(() => applyClassFilter(rows, entityClass), [rows, entityClass]);
  const open = useMemo(() => shown.find((row) => row.key === openKey) ?? null, [shown, openKey]);

  // A filter that has narrowed to a class no longer present would strand the
  // reader on an empty table with no visible cause; the chip below is the cause,
  // and it is always dismissible.
  const classOptions = useMemo(
    () => [
      { value: "all", label: "All classes" },
      ...classes.map((name) => ({ value: name, label: name })),
    ],
    [classes],
  );

  const exportCsv = useCallback(() => {
    downloadCsv(
      "library-relations.csv",
      buildCsv<RelationRow>({
        title: `Relations of ${subject}`,
        filters: entityClass === "all" ? [] : [`Class: ${entityClass}`],
        columns: [
          { header: "Relation", value: (row) => row.edgeKind },
          { header: "Direction", value: (row) => (row.direction === "out" ? "out" : "in") },
          { header: "Entity", value: (row) => row.otherName ?? "" },
          { header: "Entity key", value: (row) => row.otherKey },
          { header: "Source", value: (row) => row.sourceSystem },
          { header: "Provenance", value: (row) => row.sourceRef },
          { header: "Status", value: (row) => (row.provisional ? "UNCONFIRMED" : "") },
        ],
        rows: shown,
      }),
    );
  }, [shown, entityClass, subject]);

  return (
    <section className="ne-library__section" aria-label="Relations">
      <h3 className="ne-library__sectionhead">Relations</h3>
      {dense ? null : (
        <Toolbar
          filters={
            <>
              <FilterSelect
                label="Class"
                value={entityClass}
                options={classOptions}
                onChange={setEntityClass}
              />
              {entityClass === "all" ? null : (
                <FilterChip
                  label={`Class: ${entityClass}`}
                  onDismiss={() => setEntityClass("all")}
                />
              )}
            </>
          }
          count={status === "ok" ? `${formatCount(shown.length)} relations` : null}
          actions={
            <ToolbarButton label="Export CSV" onClick={exportCsv} disabled={shown.length === 0} />
          }
        />
      )}
      {status === "ok" && truncated ? (
        <p className="ne-table__note" role="status">
          This neighborhood was capped by the server. More visible relations may exist.
        </p>
      ) : null}
      <DataTable<RelationRow>
        columns={RELATION_COLUMNS}
        rows={shown}
        rowKey={(row) => row.key}
        label="Relations"
        things="relations"
        defaultSort={{ key: "relation", dir: "asc" }}
        status={status}
        onRowActivate={(row) => setOpenKey(row.key)}
        selectedKey={openKey}
        empty={
          entityClass === "all" ? (
            <Empty message="This entity has no relations." />
          ) : (
            <Empty
              message="No relations in that class."
              action={
                <button
                  type="button"
                  className="ne-btn ne-btn--quiet"
                  onClick={() => setEntityClass("all")}
                >
                  Show all {rows.length}
                </button>
              }
            />
          )
        }
        refused={<Refused thing="the knowledge graph" page="Library" />}
        error={<LoadError things="relations" onRetry={onRetry} />}
      />
      {open === null ? null : (
        <RelationPane row={open} subject={subject} onClose={() => setOpenKey(null)} />
      )}
    </section>
  );
}

/** §7: "Provenance line per row via detail: `source_ref` shown human-readable
 * portion first, full ref in `title`." */
function RelationPane({
  row,
  subject,
  onClose,
}: {
  row: RelationRow;
  subject: string;
  onClose: () => void;
}) {
  const ref = provenance(row.sourceRef);
  const other = row.otherName ?? row.otherKey;
  return (
    <DetailPane
      title={row.edgeKind}
      subtitle={row.direction === "out" ? `${subject} → ${other}` : `${other} → ${subject}`}
      onClose={onClose}
    >
      <DetailFields>
        <DetailField label="Relation" mono>
          {row.edgeKind}
        </DetailField>
        <DetailField label="Direction">
          {row.direction === "out" ? "Outgoing" : "Incoming"}
        </DetailField>
        <DetailField label="Entity">
          <Link to={`/library?tab=entities&entity=${encodeURIComponent(row.otherKey)}`}>
            {other}
          </Link>
        </DetailField>
        <DetailField label="Source">{row.sourceSystem}</DetailField>
        <DetailField label="Provenance" mono>
          <span title={ref.title}>{ref.text}</span>
        </DetailField>
        {row.provisional ? (
          <DetailField label="Status">
            <UnconfirmedPill />
          </DetailField>
        ) : null}
      </DetailFields>
    </DetailPane>
  );
}

/** "Paths to another entity…" (§7): a second key, then `POST /v1/graph/paths`,
 * rendered as hop chains. */
export function PathsFinder({ from }: { from: string }) {
  const client = useApiClient();
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState("");
  const [state, setState] = useState<ReadState<GraphPathsWire>>({
    phase: "ok",
    value: { paths: [], truncated: false },
  });
  const [ran, setRan] = useState(false);

  const run = useCallback(
    async (to: string) => {
      setRan(true);
      setState({ phase: "loading" });
      try {
        const result = await client.graphPaths(from, to);
        setState({ phase: "ok", value: result });
      } catch (error) {
        setState({ phase: classifyGraphFailure(error) });
      }
    },
    [client, from],
  );

  if (!open) {
    return (
      <button type="button" className="ne-btn ne-btn--quiet" onClick={() => setOpen(true)}>
        Paths to another entity…
      </button>
    );
  }

  return (
    <div className="ne-library__paths">
      <form
        className="ne-library__pathsform"
        onSubmit={(event) => {
          event.preventDefault();
          const to = target.trim();
          if (to !== "") void run(to);
        }}
      >
        <label className="ne-library__label" htmlFor="ne-paths-to">
          Entity key
        </label>
        <input
          id="ne-paths-to"
          type="text"
          className="ne-searchwithin__input ne-mono"
          value={target}
          onChange={(event) => setTarget(event.target.value)}
        />
        <button type="submit" className="ne-btn ne-btn--quiet" disabled={target.trim() === ""}>
          Find paths
        </button>
      </form>
      {!ran ? null : state.phase === "loading" ? (
        <Loading things="paths" />
      ) : state.phase === "refused" ? (
        <Refused thing="the knowledge graph" page="Library" />
      ) : state.phase === "off" ? (
        <p className="ne-state__line">
          The knowledge graph isn&rsquo;t enabled on this deployment.
        </p>
      ) : state.phase === "error" ? (
        <LoadError things="paths" onRetry={() => void run(target.trim())} />
      ) : (
        <>
          {state.value.truncated ? (
            <p className="ne-table__note" role="status">
              These path results were capped by the server. More visible paths may exist.
            </p>
          ) : null}
          {state.value.paths.length === 0 ? (
            <Empty message="No visible path was returned. The destination may be unknown, outside your access, disconnected, or beyond the served search window." />
          ) : (
            <ol className="ne-library__hopchains">
              {state.value.paths.map((path, index) => (
                <li key={`path-${index}`} className="ne-library__hopchain">
                  <HopChain path={path} />
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </div>
  );
}

/** One path, as the chain it is: entity, the relation asserted between them,
 * the next entity. Rendered from the served nodes and edges in traversal order
 * — the console orders nothing itself. */
function HopChain({ path }: { path: GraphPathWire }) {
  return (
    <span className="ne-library__hops">
      {path.nodes.map((node, index) => {
        const edge = index < path.edges.length ? path.edges[index] : undefined;
        return (
          <span key={`${node.key}-${index}`} className="ne-library__hop">
            <Link to={`/library?tab=entities&entity=${encodeURIComponent(node.key)}`}>
              {node.display_name}
            </Link>
            {edge ? (
              <span className="ne-library__hopedge ne-mono">
                <span aria-hidden="true"> —{edge.edge_kind}→ </span>
                <span className="ne-visually-hidden"> {edge.edge_kind} </span>
              </span>
            ) : null}
          </span>
        );
      })}
    </span>
  );
}
