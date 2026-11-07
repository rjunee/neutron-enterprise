// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — LIBRARY › REFERENCE. Three read-only sections for the platform architect:
// the ontology classes, the compiled shapes, the changes the write gateway
// parked.
//
// EACH SECTION FAILS ON ITS OWN. Three independent governed reads; one refusal
// does not blank the other two, and one section's outage is not reported as a
// permission. A section that the server switched off (the quarantine lane's
// `configured:false`) says THAT — it is a deployment fact, and it is neither an
// error nor a role boundary.

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ApiClient } from "../api/client";
import type {
  OntologyClassesWire,
  OntologyClassWire,
  OntologyShapesWire,
  OntologyShapeWire,
  OntologyViolationsWire,
  OntologyViolationWire,
} from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { DataTable } from "../primitives/DataTable";
import type { Column } from "../primitives/DataTable";
import { Empty, LoadError, Refused } from "../primitives/states";
import { Toolbar, ToolbarButton } from "../primitives/Toolbar";
import { buildCsv, downloadCsv } from "../primitives/csv";
import { absoluteTime, formatCount, relativeTime } from "../primitives/format";
import { filterRows } from "../primitives/tableSort";
import { classifyFailure, tableStatus } from "./readState";
import type { ReadState } from "./readState";
import { QUARANTINE_UNCONFIGURED } from "./copy";

/** One governed section read, with the Retry the error state offers. `read` is
 * a module-level function so its identity is stable across renders. */
function useSectionRead<T>(read: (client: ApiClient) => Promise<T>): {
  state: ReadState<T>;
  retry: () => void;
} {
  const client = useApiClient();
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<ReadState<T>>({ phase: "loading" });

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      setState({ phase: "loading" });
      try {
        const value = await read(client);
        if (cancelled) return;
        setState({ phase: "ok", value });
      } catch (error) {
        if (cancelled) return;
        setState({ phase: classifyFailure(error) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, read, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { state, retry };
}

export function ReferenceTab() {
  return (
    <div className="ne-library__tab">
      <OntologyClassesSection />
      <ShapesSection />
      <QuarantineSection />
    </div>
  );
}

// ---------------------------------------------------------------- classes ---

const readClasses = (client: ApiClient) => client.ontologyClasses();

const CLASS_COLUMNS: ReadonlyArray<Column<OntologyClassWire>> = [
  {
    key: "label",
    header: "Label",
    cell: (row) => row.label,
    sortValue: (row) => row.label,
    searchText: (row) => row.label ?? "",
  },
  {
    key: "iri",
    header: "IRI",
    mono: true,
    cell: (row) => row.iri,
    sortValue: (row) => row.iri,
    searchText: (row) => row.iri,
  },
  {
    key: "source",
    header: "Source",
    width: "120px",
    cell: (row) => row.source,
    sortValue: (row) => row.source,
    searchText: (row) => row.source,
  },
  {
    key: "parents",
    header: "Parents",
    mono: true,
    cell: (row) => (row.parents.length === 0 ? null : row.parents.join(" · ")),
    // Sorted by the first parent it displays — the same value the eye lands on.
    sortValue: (row) => row.parents[0] ?? null,
    searchText: (row) => row.parents.join(" "),
  },
];

function OntologyClassesSection() {
  const { state, retry } = useSectionRead<OntologyClassesWire>(readClasses);
  const [search, setSearch] = useState("");

  const rows = useMemo(() => (state.phase === "ok" ? state.value.classes : []), [state]);
  const shown = useMemo(() => filterRows(rows, CLASS_COLUMNS, search), [rows, search]);

  /** §7: "Source versions from `sources` map render as a caption line". */
  const versions = useMemo(() => {
    if (state.phase !== "ok") return "";
    return Object.entries(state.value.sources)
      .map(([source, version]) => `${source} ${version}`)
      .join(" · ");
  }, [state]);

  const exportCsv = useCallback(() => {
    downloadCsv(
      "ontology-classes.csv",
      buildCsv<OntologyClassWire>({
        title: "Ontology classes",
        filters: search.trim() === "" ? [] : [`Search: ${search.trim()}`],
        columns: [
          { header: "Label", value: (row) => row.label ?? "" },
          { header: "IRI", value: (row) => row.iri },
          { header: "Source", value: (row) => row.source },
          { header: "Parents", value: (row) => row.parents.join(" ") },
        ],
        rows: shown,
      }),
    );
  }, [shown, search]);

  return (
    <section className="ne-library__section" aria-label="Ontology classes">
      <h3 className="ne-library__sectionhead">Ontology classes</h3>
      {versions === "" ? null : <p className="ne-library__caption">{versions}</p>}
      <Toolbar
        search={{ value: search, onChange: setSearch, placeholder: "Filter classes…" }}
        count={state.phase === "ok" ? `${formatCount(shown.length)} classes` : null}
        actions={
          <ToolbarButton label="Export CSV" onClick={exportCsv} disabled={shown.length === 0} />
        }
      />
      <DataTable<OntologyClassWire>
        columns={CLASS_COLUMNS}
        rows={shown}
        rowKey={(row) => row.iri}
        label="Ontology classes"
        things="ontology classes"
        defaultSort={{ key: "label", dir: "asc" }}
        status={tableStatus(state)}
        empty={
          rows.length === 0 ? (
            <Empty message="No ontology classes are loaded on this deployment." />
          ) : (
            <Empty
              message="No matching classes."
              action={
                <button
                  type="button"
                  className="ne-btn ne-btn--quiet"
                  onClick={() => setSearch("")}
                >
                  Show all {rows.length}
                </button>
              }
            />
          )
        }
        refused={<Refused thing="the ontology" page="Library" />}
        error={<LoadError things="ontology classes" onRetry={retry} />}
      />
    </section>
  );
}

// ----------------------------------------------------------------- shapes ---

const readShapes = (client: ApiClient) => client.ontologyShapes();

const SHAPE_COLUMNS: ReadonlyArray<Column<OntologyShapeWire>> = [
  {
    key: "shape",
    header: "Shape",
    mono: true,
    cell: (row) => row.iri,
    sortValue: (row) => row.iri,
    searchText: (row) => row.iri,
  },
  {
    key: "target",
    header: "Target class",
    mono: true,
    cell: (row) => row.target_class,
    sortValue: (row) => row.target_class,
    searchText: (row) => row.target_class ?? "",
  },
  {
    key: "scope",
    header: "Scope",
    width: "140px",
    cell: (row) => row.scope,
    sortValue: (row) => row.scope,
    searchText: (row) => row.scope,
  },
];

function ShapesSection() {
  const { state, retry } = useSectionRead<OntologyShapesWire>(readShapes);
  const [search, setSearch] = useState("");

  const rows = useMemo(() => (state.phase === "ok" ? state.value.shapes : []), [state]);
  const shown = useMemo(() => filterRows(rows, SHAPE_COLUMNS, search), [rows, search]);

  const exportCsv = useCallback(() => {
    downloadCsv(
      "shapes.csv",
      buildCsv<OntologyShapeWire>({
        title: "Shapes",
        filters: search.trim() === "" ? [] : [`Search: ${search.trim()}`],
        columns: [
          { header: "Shape", value: (row) => row.iri },
          { header: "Target class", value: (row) => row.target_class ?? "" },
          { header: "Scope", value: (row) => row.scope },
        ],
        rows: shown,
      }),
    );
  }, [shown, search]);

  return (
    <section className="ne-library__section" aria-label="Shapes">
      <h3 className="ne-library__sectionhead">Shapes</h3>
      <Toolbar
        search={{ value: search, onChange: setSearch, placeholder: "Filter shapes…" }}
        count={state.phase === "ok" ? `${formatCount(shown.length)} shapes` : null}
        actions={
          <ToolbarButton label="Export CSV" onClick={exportCsv} disabled={shown.length === 0} />
        }
      />
      <DataTable<OntologyShapeWire>
        columns={SHAPE_COLUMNS}
        rows={shown}
        rowKey={(row) => row.iri}
        label="Shapes"
        things="shapes"
        defaultSort={{ key: "shape", dir: "asc" }}
        status={tableStatus(state)}
        empty={<Empty message="No shapes are compiled on this deployment." />}
        refused={<Refused thing="the ontology" page="Library" />}
        error={<LoadError things="shapes" onRetry={retry} />}
      />
    </section>
  );
}

// ------------------------------------------------------------- quarantine ---

const readViolations = (client: ApiClient) => client.ontologyViolations();

const QUARANTINE_COLUMNS: ReadonlyArray<Column<OntologyViolationWire>> = [
  {
    key: "id",
    header: "Quarantine id",
    mono: true,
    cell: (row) => row.quarantine_id,
    sortValue: (row) => row.quarantine_id,
    searchText: (row) => row.quarantine_id,
  },
  {
    key: "created",
    header: "Created",
    numeric: true,
    cell: (row) => (
      <time dateTime={row.created_at} title={absoluteTime(row.created_at)}>
        {relativeTime(row.created_at)}
      </time>
    ),
    sortValue: (row) => Date.parse(row.created_at),
    firstSort: "desc",
  },
  {
    key: "graph",
    header: "Graph",
    mono: true,
    cell: (row) => row.graph_iri,
    sortValue: (row) => row.graph_iri,
    searchText: (row) => row.graph_iri ?? "",
  },
  {
    key: "violations",
    header: "Violations",
    numeric: true,
    width: "110px",
    cell: (row) => formatCount(row.violation_count),
    sortValue: (row) => row.violation_count,
    firstSort: "desc",
  },
  {
    key: "provenance",
    header: "Provenance",
    cell: (row) => row.provenance,
    sortValue: (row) => row.provenance,
    searchText: (row) => row.provenance,
  },
  {
    key: "whole",
    header: "Whole-graph?",
    width: "130px",
    // §7: "(yes/—)". The dash is right here: the flag is a boolean the server
    // sends, and "no" and "not applicable" are the same absence of a claim.
    cell: (row) => (row.quarantined_whole ? "Yes" : null),
    sortValue: (row) => row.quarantined_whole,
  },
];

function QuarantineSection() {
  const { state, retry } = useSectionRead<OntologyViolationsWire>(readViolations);
  const [search, setSearch] = useState("");

  const rows = useMemo(() => (state.phase === "ok" ? state.value.entries : []), [state]);
  const shown = useMemo(() => filterRows(rows, QUARANTINE_COLUMNS, search), [rows, search]);

  const exportCsv = useCallback(() => {
    downloadCsv(
      "quarantined-changes.csv",
      buildCsv<OntologyViolationWire>({
        title: "Quarantined changes",
        filters: search.trim() === "" ? [] : [`Search: ${search.trim()}`],
        columns: [
          { header: "Quarantine id", value: (row) => row.quarantine_id },
          { header: "Created", value: (row) => absoluteTime(row.created_at) },
          { header: "Graph", value: (row) => row.graph_iri ?? "" },
          { header: "Violations", value: (row) => String(row.violation_count) },
          { header: "Provenance", value: (row) => row.provenance },
          { header: "Whole-graph?", value: (row) => (row.quarantined_whole ? "Yes" : "") },
        ],
        rows: shown,
      }),
    );
  }, [shown, search]);

  if (state.phase === "ok" && !state.value.configured) {
    return (
      <section className="ne-library__section" aria-label="Quarantined changes">
        <h3 className="ne-library__sectionhead">Quarantined changes</h3>
        <Empty message={QUARANTINE_UNCONFIGURED} />
      </section>
    );
  }

  return (
    <section className="ne-library__section" aria-label="Quarantined changes">
      <h3 className="ne-library__sectionhead">Quarantined changes</h3>
      <Toolbar
        search={{ value: search, onChange: setSearch, placeholder: "Filter quarantined changes…" }}
        count={state.phase === "ok" ? `${formatCount(shown.length)} quarantined changes` : null}
        actions={
          <ToolbarButton label="Export CSV" onClick={exportCsv} disabled={shown.length === 0} />
        }
      />
      <DataTable<OntologyViolationWire>
        columns={QUARANTINE_COLUMNS}
        rows={shown}
        rowKey={(row) => row.quarantine_id}
        label="Quarantined changes"
        things="quarantined changes"
        defaultSort={{ key: "created", dir: "desc" }}
        status={tableStatus(state)}
        empty={<Empty message="Nothing is quarantined." />}
        refused={<Refused thing="quarantined changes" page="Library" />}
        error={<LoadError things="quarantined changes" onRetry={retry} />}
      />
    </section>
  );
}
