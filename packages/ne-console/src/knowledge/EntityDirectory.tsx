// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §7 — LIBRARY › ENTITIES, with nothing open yet.
//
// This is the way into the corpus. It used to be a sentence telling the reader
// to go and find a citation somewhere else; the knowledge was indexed and there
// was no door.
//
// WHAT IT DOES. It reads the registered verified-answer templates, offers the
// ones that need no argument, runs the chosen one, and renders the rows the
// server returned. Activating a row opens that row's entity record — through
// the row's OWN `urn:ne:entity:<key>` citation, which is the same anchor a
// citation from Ask carries. The citation path, walked deliberately.
//
// WHAT A PERSON READS FIRST. Columns render in DISPLAY order (displayOrder):
// the template's declared `label_var` column leads and its `citation_var`
// identifier renders last as secondary exact-key text. The immutable key stays
// visible and selectable and remains the row's governed deep link, without
// making a UUID the value a person has to read first. A
// template declaring no label renders its served order verbatim, including the
// identifier; no label is invented. And because the
// picker's entries read like object types when they are actually authored
// queries, a provenance line states what they are.
//
// WHAT IT DOES NOT DO. It does not assemble a list. Which rows exist, which of
// them this caller may see, what the columns are, how many there are and
// whether the set was capped are ALL the server's answers, rendered as they
// arrived. A deployment that registers no zero-argument template has no browse,
// and this screen says the spec's idle line instead of building one out of
// something else's results. The filter box narrows the rows the server already
// returned and the count says exactly that.
//
// THE REPLAY VERDICT IS THE SERVER'S — it re-executed the query and compared
// the two results itself. This renders which way that went and computes no part
// of it.

import { useCallback, useEffect, useMemo, useState } from "react";
import type { VerifiedInvokeWire, VerifiedTemplateWire } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { DataTable } from "../primitives/DataTable";
import type { Column } from "../primitives/DataTable";
import { Empty, LoadError, Refused } from "../primitives/states";
import { FilterSelect, Toolbar } from "../primitives/Toolbar";
import { filterRows } from "../primitives/tableSort";
import {
  ENTITIES_IDLE,
  REPLAY_DIVERGED,
  REPLAY_MATCH,
  VERIFIED_AUTHORED,
  VERIFIED_OFF,
} from "./copy";
import { parseEntityUrn } from "./entityUrn";
import type { DirectoryRow } from "./directory";
import {
  classifyDirectoryFailure,
  directoryCountText,
  directoryRows,
  directoryTemplates,
  displayOrder,
  truncationNote,
} from "./directory";
import { classifyFailure, tableStatus } from "./readState";
import type { ReadState } from "./readState";

type Catalog = { enabled: boolean; templates: VerifiedTemplateWire[] };

const EMPTY: readonly VerifiedTemplateWire[] = [];

export function EntityDirectory({ onOpenEntity }: { onOpenEntity: (key: string) => void }) {
  const client = useApiClient();
  const [attempt, setAttempt] = useState(0);
  const [catalog, setCatalog] = useState<ReadState<Catalog>>({ phase: "loading" });
  const [picked, setPicked] = useState<string | null>(null);
  const [answer, setAnswer] = useState<ReadState<VerifiedInvokeWire>>({ phase: "loading" });
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // The deferred continuation every governed read on this screen uses.
      await Promise.resolve();
      if (cancelled) return;
      setCatalog({ phase: "loading" });
      try {
        const res = await client.verifiedTemplates();
        if (cancelled) return;
        setCatalog({
          phase: "ok",
          value: { enabled: res.enabled, templates: directoryTemplates(res.templates) },
        });
      } catch (error) {
        if (cancelled) return;
        setCatalog({ phase: classifyFailure(error) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, attempt]);

  // Memoised so the empty case is ONE array across renders: a fresh `[]` every
  // render re-runs everything downstream of it while the catalog is loading.
  const templates = useMemo(
    () => (catalog.phase === "ok" ? catalog.value.templates : EMPTY),
    [catalog],
  );
  // Nothing picked yet means the first one the server listed — the screen opens
  // showing rows rather than opening on a control the reader has to work out.
  const open = useMemo(
    () => templates.find((t) => t.template_id === picked) ?? templates[0] ?? null,
    [templates, picked],
  );
  const openId = open?.template_id ?? null;
  const openVersion = open?.version;

  useEffect(() => {
    let cancelled = false;
    if (openId === null) return;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      setAnswer({ phase: "loading" });
      try {
        // EXACTLY {template_id, version, args} with NO argument invented: this
        // list only ever offers templates that declare none.
        const result = await client.verifiedInvoke({
          template_id: openId,
          version: openVersion,
          args: {},
        });
        if (cancelled) return;
        setAnswer({ phase: "ok", value: result });
      } catch (error) {
        if (cancelled) return;
        setAnswer({ phase: classifyDirectoryFailure(error) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, openId, openVersion, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  if (catalog.phase === "refused") return <Refused thing="verified answers" page="Library" />;
  if (catalog.phase === "error") return <LoadError things="the directory" onRetry={retry} />;
  if (catalog.phase === "ok" && !catalog.value.enabled) return <Empty message={VERIFIED_OFF} />;
  // A deployment whose registry holds nothing runnable without an argument has
  // no browse to offer. The spec's idle line, and no invented alternative.
  if (catalog.phase === "ok" && templates.length === 0) return <Empty message={ENTITIES_IDLE} />;

  return (
    <DirectoryBody
      templates={templates}
      open={open}
      answer={answer}
      filter={filter}
      onFilter={setFilter}
      onPick={(id) => {
        setPicked(id);
        setFilter("");
      }}
      onOpenEntity={onOpenEntity}
      onRetry={retry}
      loadingCatalog={catalog.phase === "loading"}
    />
  );
}

function DirectoryBody({
  templates,
  open,
  answer,
  filter,
  onFilter,
  onPick,
  onOpenEntity,
  onRetry,
  loadingCatalog,
}: {
  templates: readonly VerifiedTemplateWire[];
  open: VerifiedTemplateWire | null;
  answer: ReadState<VerifiedInvokeWire>;
  filter: string;
  onFilter: (next: string) => void;
  onPick: (id: string) => void;
  onOpenEntity: (key: string) => void;
  onRetry: () => void;
  loadingCatalog: boolean;
}) {
  const served = answer.phase === "ok" ? answer.value.answer : null;
  const citationVar = open?.citation_var ?? "";
  const labelVar = open?.label_var ?? null;
  const columns = useMemo<ReadonlyArray<Column<DirectoryRow>>>(
    () => buildColumns(served?.columns ?? [], citationVar, labelVar),
    [served, citationVar, labelVar],
  );
  const rows = useMemo(() => (served === null ? [] : directoryRows(served)), [served]);
  const shown = useMemo(() => filterRows(rows, columns, filter), [rows, columns, filter]);

  if (answer.phase === "refused") return <Refused thing="verified answers" page="Library" />;
  if (answer.phase === "off") return <Empty message={VERIFIED_OFF} />;

  return (
    <div className="ne-library__directory">
      <Toolbar
        search={{ value: filter, onChange: onFilter, placeholder: "Filter these rows…" }}
        filters={
          <FilterSelect
            label="Verified query"
            value={open?.template_id ?? ""}
            options={templates.map((t) => ({ value: t.template_id, label: t.title }))}
            onChange={onPick}
          />
        }
        count={served === null ? null : directoryCountText(shown.length, served.returned_rows)}
      />
      {/* What the picker's options ARE. Its entries read like object types
          ("every account…", "every order…") and were mistaken for exactly that
          in a live demo; they are authored queries, and only this line says so. */}
      <p className="ne-library__provenance">{VERIFIED_AUTHORED}</p>
      <DataTable<DirectoryRow>
        columns={columns}
        rows={shown}
        rowKey={(row) => String(row.index)}
        label="Directory"
        things="rows"
        status={loadingCatalog ? "loading" : tableStatus(answer)}
        onRowActivate={(row) => {
          if (row.entityKey !== null) onOpenEntity(row.entityKey);
        }}
        note={served === null ? null : truncationNote(served)}
        empty={
          <Empty
            message={
              // A filter that matched nothing and a directory that IS empty are
              // different facts; one message for both would state the wrong one
              // every time the reader is mid-filter.
              filter.trim() !== "" && rows.length > 0
                ? "No rows here match that filter."
                : "This directory returned no rows."
            }
          />
        }
        error={<LoadError things="the directory" onRetry={onRetry} />}
      />
      {answer.phase === "ok" ? (
        <p
          className={`ne-library__replay${
            answer.value.replay_match ? "" : " ne-library__replay--diverged"
          }`}
          role="status"
        >
          {answer.value.replay_match ? REPLAY_MATCH : REPLAY_DIVERGED}
        </p>
      ) : null}
    </div>
  );
}

/**
 * The template's own columns in DISPLAY order ({@link displayOrder}): the
 * declared `label_var` column leads and the `citation_var` identifier trails as
 * secondary exact-key text whose tooltip also preserves the citation URN.
 * A template that declares no label renders its own order verbatim
 * (identifier-led); no label is invented from another column.
 *
 * A citation cell whose value is an entity urn renders its exact key when the
 * template supplies a human label, with the full served URN in `title`; the row
 * still opens that exact key. Without an authoritative label, the key likewise
 * stays visible. That is presentation of the served values, not a substitution:
 * nothing is looked up, joined or guessed.
 *
 * Only the column the template itself declared as its `citation_var` is set in
 * monospace: it is the one that holds an identifier. Deciding that from a cell's
 * shape instead would type a person's name as an identifier the first time one
 * of them looked like a key.
 */
function buildColumns(
  names: readonly string[],
  citationVar: string,
  labelVar: string | null,
): Column<DirectoryRow>[] {
  return displayOrder(names, citationVar, labelVar).map((i) => ({
    key: `col-${i}`,
    header: names[i]!,
    mono: names[i] === citationVar,
    cell: (row) => {
      const value = row.cells[i] ?? null;
      if (value === null) return null;
      const key = parseEntityUrn(value);
      if (key === null) return value;
      if (names[i] === citationVar && labelVar !== null) {
        return (
          <code className="ne-library__entity-key" title={`Citation: ${value}`}>
            {key}
          </code>
        );
      }
      return <span title={value}>{key}</span>;
    },
    sortValue: (row) => row.cells[i] ?? null,
    searchText: (row) => row.cells[i] ?? "",
  }));
}
