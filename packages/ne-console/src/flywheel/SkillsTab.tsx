// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §11 — IMPROVEMENT › SKILLS. The library, restyled to the table standard.
//
// Default sort is Name A–Z; Name, Status, Fired and Last fired are sortable, as
// §11 states. Source, Description and Labels are not: a description sorts by its
// first character, which orders nothing anyone would look for.
//
// Every text field here is proposal-influenced content the server folded — a
// description can carry markup. It renders as a plain React text node and
// nowhere else: no raw-HTML React prop, no innerHTML, no markdown parser.
//
// THIN CLIENT (C7). Retire and restore submit a reason and nothing else; the
// server derives the steward from the verified bearer and owns the transition.
// The row's status flips because the SERVER's next answer says so, never because
// this file edited its own copy of the row.

import { useCallback, useEffect, useMemo, useState } from "react";
import type { SkillLibraryRow } from "../api/types";
import { ApiError } from "../api/client";
import { useApiClient } from "../auth/useApiClient";
import { useLatestRequest } from "../hooks/useLatestRequest";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { StatusPill } from "../primitives/StatePill";
import { Toolbar } from "../primitives/Toolbar";
import { filterRows } from "../primitives/tableSort";
import { Empty, LoadError, Refused } from "../primitives/states";
import { absoluteTime, formatCount, relativeTime } from "../primitives/format";
import { skillCount, skillStatusWord } from "./queue";

type ReadStatus = "loading" | "ok" | "refused" | "error";

/** §11: a fault-tinted TAMPERED marker. §2.3's state vocabulary is closed and
 * does not contain the word, so this is not a <StatePill> masquerading as one —
 * it is the row's own marker, drawn in the same geometry, carrying the word and
 * a glyph so the meaning is never in the colour alone. */
function TamperedMark() {
  return (
    <span className="ne-pill ne-pill--refuse" aria-label="TAMPERED">
      <span className="ne-pill__glyph" aria-hidden="true">
        ⚠
      </span>
      <span className="ne-pill__word">TAMPERED</span>
    </span>
  );
}

/** The lifecycle state as a pill, or — for a state outside §2.3's closed
 * vocabulary — the served word itself. */
function SkillState({ status }: { status: string }) {
  const word = skillStatusWord(status);
  return word === null ? <>{status}</> : <StatusPill word={word} />;
}

/** An empty label set on a REAL version is global visibility. A version-less
 * quarantined name has no pack and therefore no visibility verdict at all. */
function labelsText(row: SkillLibraryRow): string {
  if (row.current_version === null) return "No version";
  return row.acl_labels.length === 0 ? "Everyone" : row.acl_labels.join(", ");
}

const COLUMNS: ReadonlyArray<Column<SkillLibraryRow>> = [
  {
    key: "name",
    header: "Name",
    mono: true,
    width: "180px",
    cell: (row) => row.name,
    sortValue: (row) => row.name,
    searchText: (row) => row.name,
  },
  {
    key: "source",
    header: "Source",
    width: "104px",
    cell: (row) => row.source,
    searchText: (row) => row.source,
  },
  {
    key: "version",
    header: "Version",
    numeric: true,
    width: "88px",
    cell: (row) => row.current_version,
  },
  {
    key: "status",
    header: "Status",
    width: "180px",
    cell: (row) => {
      const word = skillStatusWord(row.status);
      return (
        <span className="ne-fly__statuscell">
          {word === null ? row.status : <StatusPill word={word} />}
          {row.tampered ? <TamperedMark /> : null}
        </span>
      );
    },
    sortValue: (row) => row.status,
    searchText: (row) => row.status,
  },
  {
    key: "description",
    header: "Description",
    cell: (row) => row.description,
    searchText: (row) => row.description,
  },
  {
    key: "labels",
    header: "Labels",
    width: "160px",
    cell: (row) => labelsText(row),
    searchText: (row) => row.acl_labels.join(" "),
  },
  {
    key: "fired",
    header: "Fired",
    numeric: true,
    width: "80px",
    cell: (row) => formatCount(row.fired_count),
    sortValue: (row) => row.fired_count,
    firstSort: "desc",
  },
  {
    key: "lastFired",
    header: "Last fired",
    numeric: true,
    width: "112px",
    cell: (row) =>
      row.last_fired_at === null ? null : (
        <time dateTime={row.last_fired_at} title={absoluteTime(row.last_fired_at)}>
          {relativeTime(row.last_fired_at)}
        </time>
      ),
    sortValue: (row) => (row.last_fired_at === null ? null : Date.parse(row.last_fired_at)),
    firstSort: "desc",
  },
];

/** The one lifecycle move a row offers, from the SERVER's status. A quarantined
 * name offers neither — the forge gate owns it. */
function rowAction(row: SkillLibraryRow): "retire" | "restore" | null {
  if (row.status === "active") return "retire";
  if (row.status === "retired") return "restore";
  return null;
}

const ACTION_LABEL = { retire: "Retire…", restore: "Restore…" } as const;
const SUBMIT_LABEL = { retire: "Retire", restore: "Restore" } as const;

export function SkillsTab() {
  const client = useApiClient();
  const runLatest = useLatestRequest();
  const [rows, setRows] = useState<readonly SkillLibraryRow[]>([]);
  const [status, setStatus] = useState<ReadStatus>("loading");
  const [search, setSearch] = useState("");
  const [openName, setOpenName] = useState<string | null>(null);
  /** Set when the pane was opened straight into its reason form from the row's
   * overflow menu. */
  const [formOpen, setFormOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ detail: string | null } | null>(null);

  const refetch = useCallback(async () => {
    const settled = await runLatest(() => client.stewardSkills());
    if (settled.kind === "superseded") return;
    if (settled.kind === "success") {
      setRows(settled.value.skills);
      setStatus("ok");
      return;
    }
    setRows([]);
    setStatus(
      settled.error instanceof ApiError && settled.error.status === 403 ? "refused" : "error",
    );
  }, [client, runLatest]);

  // Nothing in `refetch` sets state before its first await — the read is the
  // external system, and every setState is in its continuation.
  useEffect(() => {
    void (async () => {
      await refetch();
    })();
  }, [refetch]);

  const filtered = useMemo(() => filterRows(rows, COLUMNS, search), [rows, search]);

  const openRow = openName === null ? null : (rows.find((row) => row.name === openName) ?? null);

  const closePane = useCallback(() => {
    setOpenName(null);
    setFormOpen(false);
    setReason("");
    setError(null);
  }, []);

  const openPane = useCallback((row: SkillLibraryRow, withForm: boolean) => {
    setOpenName(row.name);
    setFormOpen(withForm);
    setReason("");
    setError(null);
  }, []);

  const submit = useCallback(
    async (row: SkillLibraryRow, action: "retire" | "restore") => {
      setBusy(true);
      setError(null);
      try {
        await (action === "retire"
          ? client.retireSkill(row.name, reason)
          : client.restoreSkill(row.name, reason));
        setFormOpen(false);
        setReason("");
      } catch (err) {
        setError({ detail: err instanceof ApiError ? err.userMessage : null });
      }
      setBusy(false);
      await refetch();
    },
    [client, reason, refetch],
  );

  if (status === "refused") {
    return <Refused thing="Improvement" page="Improvement" />;
  }

  const tableStatus: TableStatus =
    status === "loading" ? "loading" : status === "error" ? "error" : "ok";
  const action = openRow === null ? null : rowAction(openRow);

  return (
    <>
      <Toolbar
        search={{ value: search, onChange: setSearch, placeholder: "Filter skills…" }}
        count={status === "ok" ? skillCount(filtered.length) : null}
      />

      <DataTable<SkillLibraryRow>
        columns={COLUMNS}
        rows={filtered}
        rowKey={(row) => row.name}
        label="Skill library"
        things="skills"
        defaultSort={{ key: "name", dir: "asc" }}
        status={tableStatus}
        onRowActivate={(row) => openPane(row, false)}
        selectedKey={openName}
        rowMenu={(row) => {
          const next = rowAction(row);
          return next === null ? null : (
            <RowMenu
              name={row.name}
              label={ACTION_LABEL[next]}
              onSelect={() => openPane(row, true)}
            />
          );
        }}
        empty={
          search.trim() !== "" && rows.length > 0 ? (
            <Empty
              message="No matching skills."
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
          ) : (
            <Empty message="No skills in the library." />
          )
        }
        error={<LoadError things="skills" onRetry={() => void refetch()} />}
      />

      {openRow === null ? null : (
        <DetailPane
          title={openRow.name}
          subtitle={<SkillState status={openRow.status} />}
          onClose={closePane}
        >
          <DetailFields>
            <DetailField label="Source">{openRow.source}</DetailField>
            <DetailField label="Version">{openRow.current_version}</DetailField>
            <DetailField label="Description">{openRow.description}</DetailField>
            <DetailField label="Labels">{labelsText(openRow)}</DetailField>
            <DetailField label="Fired">{formatCount(openRow.fired_count)}</DetailField>
            <DetailField label="Last fired">
              {openRow.last_fired_at === null ? null : (
                <time dateTime={openRow.last_fired_at}>{absoluteTime(openRow.last_fired_at)}</time>
              )}
            </DetailField>
          </DetailFields>

          {openRow.tampered ? (
            <p className="ne-fly__warn">Stored content no longer matches its recorded hash.</p>
          ) : null}

          {error === null ? null : (
            <div className="ne-state ne-state--error" role="alert">
              <p className="ne-state__line">Couldn&rsquo;t submit. Try again.</p>
              {error.detail ? <p className="ne-state__detail">{error.detail}</p> : null}
            </div>
          )}

          {action === null ? null : formOpen ? (
            <div className="ne-fly__form">
              <label className="ne-fly__field">
                <span className="ne-fly__fieldlabel">Why?</span>
                <input
                  type="text"
                  className="ne-fly__input"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                />
              </label>
              <div className="ne-fly__buttons">
                <button
                  type="button"
                  className="ne-btn ne-btn--primary"
                  disabled={busy || reason.trim() === ""}
                  onClick={() => void submit(openRow, action)}
                >
                  {SUBMIT_LABEL[action]}
                </button>
                <button
                  type="button"
                  className="ne-btn ne-btn--quiet"
                  onClick={() => {
                    setFormOpen(false);
                    setReason("");
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="ne-fly__buttons">
              <button
                type="button"
                className="ne-btn ne-btn--quiet"
                onClick={() => setFormOpen(true)}
              >
                {ACTION_LABEL[action]}
              </button>
            </div>
          )}
        </DetailPane>
      )}
    </>
  );
}

/** §2.1's row overflow menu: revealed on row hover/focus, opened by `o` from the
 * table's own key handling (which clicks `.ne-rowmenu__trigger`). */
function RowMenu({ name, label, onSelect }: { name: string; label: string; onSelect: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="ne-fly__rowmenu">
      <button
        type="button"
        className="ne-rowmenu__trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Actions for ${name}`}
        onClick={() => setOpen((prev) => !prev)}
      >
        <span aria-hidden="true">⋯</span>
      </button>
      {open ? (
        <span className="ne-fly__menu" role="menu">
          <button
            type="button"
            role="menuitem"
            className="ne-fly__menuitem"
            onClick={() => {
              setOpen(false);
              onSelect();
            }}
          >
            {label}
          </button>
        </span>
      ) : null}
    </span>
  );
}
