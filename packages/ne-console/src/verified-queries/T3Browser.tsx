// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The governed Tier-3 record browser. This is NOT an ad-hoc query surface: every
// read here is one of two fixed, reviewed verified-answer templates —
// `t3-records-in-class` (enumerate a class) and `t3-record` (open one record) —
// executed through the SAME governed /v1/verified/invoke path as every other
// verified query (PDP + audit + replay + the caller's ACL graph scoping). The
// class roster itself is the server-advertised closed enum on the enumeration
// template's args schema — the console never invents a class name.

import { useEffect, useMemo, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import type { VerifiedInvokeWire, VerifiedTemplateWire, VerifiedTemplatesWire } from "../api/types";
import { Empty, LoadError, Refused } from "../primitives/states";
import { classifyVerifiedFailure } from "./verifiedQueriesState";

export const T3_CLASS_TEMPLATE_ID = "t3-records-in-class";
export const T3_RECORD_TEMPLATE_ID = "t3-record";

const MERIDIAN_SCHEMA_PREFIX = "https://neutron.enterprise/schema/meridian/";
const RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";

/** Compact display for a served predicate/class IRI — presentation only, the
 * full IRI stays in the cell's title. Never invents a name: an IRI outside the
 * known prefixes renders verbatim. */
export function shortIri(iri: string): string {
  if (iri === RDF_TYPE) return "type";
  if (iri.startsWith(MERIDIAN_SCHEMA_PREFIX)) return iri.slice(MERIDIAN_SCHEMA_PREFIX.length);
  return iri;
}

/** The server-stated class roster: the closed enum advertised on the
 * enumeration template's `q_class` arg. Empty when the template (or its enum)
 * is not served — the browser then says so instead of inventing classes. */
export function t3ClassRoster(catalog: VerifiedTemplatesWire): string[] {
  const template = catalog.templates.find((t) => t.template_id === T3_CLASS_TEMPLATE_ID);
  return template?.args_schema.properties?.q_class?.enum ?? [];
}

type CatalogState =
  | { phase: "loading" }
  | { phase: "ok"; value: VerifiedTemplatesWire }
  | { phase: "refused" }
  | { phase: "error" };

type InvokeState =
  | { phase: "idle"; key: string }
  | { phase: "loading"; key: string }
  | { phase: "refused"; key: string }
  | { phase: "error"; key: string; detail: string | null }
  | { phase: "ok"; key: string; result: VerifiedInvokeWire };

function useInvoke(client: ApiClient) {
  const [state, setState] = useState<InvokeState>({ phase: "idle", key: "" });
  const sequence = useRef(0);
  const active = useRef(0);
  const start = (key: string, template: VerifiedTemplateWire, args: Record<string, unknown>) => {
    const ticket = ++sequence.current;
    active.current = ticket;
    setState({ phase: "loading", key });
    void client
      .verifiedInvoke({ template_id: template.template_id, version: template.version, args })
      .then(
        (result) => {
          if (active.current === ticket) setState({ phase: "ok", key, result });
        },
        (error: unknown) => {
          if (active.current !== ticket) return;
          const failure = classifyVerifiedFailure(error);
          setState(
            failure.kind === "refused"
              ? { phase: "refused", key }
              : { phase: "error", key, detail: failure.detail },
          );
        },
      );
  };
  const reset = () => {
    active.current = 0;
    setState({ phase: "idle", key: "" });
  };
  return { state, start, reset };
}

export function T3Browser({
  client,
  selectedClass,
  selectedRecord,
  onSelectClass,
  onSelectRecord,
}: {
  client: ApiClient;
  selectedClass: string | null;
  selectedRecord: string | null;
  onSelectClass: (className: string | null) => void;
  onSelectRecord: (recordIri: string | null) => void;
}) {
  const [catalog, setCatalog] = useState<CatalogState>({ phase: "loading" });
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const records = useInvoke(client);
  const fields = useInvoke(client);

  useEffect(() => {
    let owned = true;
    void client.verifiedTemplates().then(
      (value) => {
        if (owned) setCatalog({ phase: "ok", value });
      },
      (error: unknown) => {
        if (!owned) return;
        setCatalog({
          phase: classifyVerifiedFailure(error).kind === "refused" ? "refused" : "error",
        });
      },
    );
    return () => {
      owned = false;
    };
  }, [catalogAttempt, client]);

  const classTemplate = useMemo(
    () =>
      catalog.phase === "ok"
        ? (catalog.value.templates.find((t) => t.template_id === T3_CLASS_TEMPLATE_ID) ?? null)
        : null,
    [catalog],
  );
  const recordTemplate = useMemo(
    () =>
      catalog.phase === "ok"
        ? (catalog.value.templates.find((t) => t.template_id === T3_RECORD_TEMPLATE_ID) ?? null)
        : null,
    [catalog],
  );
  const roster = useMemo(
    () => (catalog.phase === "ok" ? t3ClassRoster(catalog.value) : []),
    [catalog],
  );

  // Deep links (?cls=… / ?record=…): run the governed read for the selection.
  useEffect(() => {
    if (selectedClass === null || classTemplate === null) return;
    if (!roster.includes(selectedClass)) return;
    records.start(selectedClass, classTemplate, { q_class: selectedClass });
    // records.start is stable enough for this effect: it only closes over refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedClass, classTemplate]);
  useEffect(() => {
    if (selectedRecord === null || recordTemplate === null) {
      fields.reset();
      return;
    }
    fields.start(selectedRecord, recordTemplate, { q_record: selectedRecord });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedRecord, recordTemplate]);

  if (catalog.phase === "loading") return <p role="status">Loading the verified store catalog…</p>;
  if (catalog.phase === "refused") {
    return (
      <div role="alert">
        <Refused thing="the verified store" page="Execution" />
      </div>
    );
  }
  if (catalog.phase === "error") {
    return (
      <LoadError
        things="the verified store catalog"
        onRetry={() => {
          setCatalog({ phase: "loading" });
          setCatalogAttempt((value) => value + 1);
        }}
      />
    );
  }
  if (classTemplate === null || recordTemplate === null || roster.length === 0) {
    return (
      <Empty message="The Tier-3 browse templates (t3-records-in-class, t3-record) are not registered in this deployment, so records cannot be enumerated here." />
    );
  }

  const recordRows =
    records.state.phase === "ok" && records.state.key === selectedClass
      ? records.state.result.answer.rows
      : null;
  const fieldRows =
    fields.state.phase === "ok" && fields.state.key === selectedRecord
      ? fields.state.result.answer.rows
      : null;

  return (
    <div className="ne-t3-browser">
      <section className="ne-t3-browser__panel" aria-label="Verified store classes">
        <h3>Classes in the verified release</h3>
        <ul className="ne-t3-browser__classes">
          {roster.map((className) => (
            <li key={className}>
              <button
                type="button"
                className={className === selectedClass ? "is-selected" : ""}
                aria-pressed={className === selectedClass}
                onClick={() => {
                  onSelectRecord(null);
                  onSelectClass(className);
                }}
              >
                {className}
              </button>
            </li>
          ))}
        </ul>
        <p className="ne-t3-browser__note">
          This roster is the closed vocabulary the server advertises on the reviewed enumeration
          query. Reads are ACL-scoped: a class whose graph your role does not hold returns no
          records.
        </p>
      </section>

      {selectedClass === null ? (
        <Empty message="Pick a class to enumerate its records through the reviewed query." />
      ) : (
        <section className="ne-t3-browser__panel" aria-label={`${selectedClass} records`}>
          <h3>{selectedClass} records</h3>
          {records.state.phase === "loading" ? <p role="status">Enumerating records…</p> : null}
          {records.state.phase === "refused" ? (
            <div role="alert">
              <Refused thing="this class enumeration" page="Execution" />
            </div>
          ) : null}
          {records.state.phase === "error" ? (
            <div>
              <LoadError
                things="the class records"
                onRetry={() =>
                  records.start(selectedClass, classTemplate, { q_class: selectedClass })
                }
              />
              {records.state.detail === null ? null : <p>{records.state.detail}</p>}
            </div>
          ) : null}
          {recordRows !== null && recordRows.length === 0 ? (
            <Empty
              message={`The server returned no ${selectedClass} records for your role — either none exist in this release, or their ACL graph is not one your role holds.`}
            />
          ) : null}
          {recordRows !== null && recordRows.length > 0 ? (
            <>
              <ul className="ne-t3-browser__records">
                {recordRows.map((row) => {
                  const recordIri = row[0] ?? "";
                  return (
                    <li key={recordIri}>
                      <button
                        type="button"
                        aria-pressed={recordIri === selectedRecord}
                        onClick={() => onSelectRecord(recordIri)}
                      >
                        {recordIri}
                      </button>
                    </li>
                  );
                })}
              </ul>
              {records.state.phase === "ok" && records.state.result.answer.truncated ? (
                <p className="ne-t3-browser__note" role="note">
                  Showing {records.state.result.answer.returned_rows} of{" "}
                  {records.state.result.answer.total_rows ?? "an unstated total of"} records — the
                  row cap truncated this enumeration.
                </p>
              ) : null}
            </>
          ) : null}
        </section>
      )}

      {selectedRecord === null ? null : (
        <section className="ne-t3-browser__panel" aria-label="Record detail">
          <h3>
            <code>{selectedRecord}</code>
          </h3>
          {fields.state.phase === "loading" ? <p role="status">Loading record fields…</p> : null}
          {fields.state.phase === "refused" ? (
            <div role="alert">
              <Refused thing="this record read" page="Execution" />
            </div>
          ) : null}
          {fields.state.phase === "error" ? (
            <div>
              <LoadError
                things="the record fields"
                onRetry={() =>
                  fields.start(selectedRecord, recordTemplate, { q_record: selectedRecord })
                }
              />
              {fields.state.detail === null ? null : <p>{fields.state.detail}</p>}
            </div>
          ) : null}
          {fieldRows !== null && fieldRows.length === 0 ? (
            <Empty message="The server returned no fields for this record — it is outside your role's ACL graphs or absent from this release." />
          ) : null}
          {fieldRows !== null && fieldRows.length > 0 ? (
            <table className="ne-t3-browser__fields">
              <caption className="ne-sr-only">Served fields of {selectedRecord}</caption>
              <thead>
                <tr>
                  <th scope="col">Field</th>
                  <th scope="col">Served value</th>
                </tr>
              </thead>
              <tbody>
                {fieldRows.map((row, index) => {
                  const predicate = row[1] ?? "";
                  const value = row[2] ?? "";
                  return (
                    <tr key={`${predicate}-${index}`}>
                      <th scope="row" title={predicate}>
                        {shortIri(predicate)}
                      </th>
                      <td>
                        {value.startsWith("http") || value.startsWith("urn:") ? (
                          <code title={value}>{shortIri(value)}</code>
                        ) : (
                          value
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : null}
          <p className="ne-t3-browser__note">
            Every row above is a value the server stated for this record through the reviewed record
            query — nothing is joined, inferred, or fetched from anywhere else.
          </p>
        </section>
      )}
    </div>
  );
}
