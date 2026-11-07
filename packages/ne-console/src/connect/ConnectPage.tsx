// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §9 — SOURCES. What the knowledge layer may read, how fresh each connector is,
// whether its permissions mirror still passes, and what got quarantined.
//
// The job, weekly or on an ingestion incident: is every connector fresh, did any
// ACL mirror degrade, what is in quarantine and why. That is a list, so this is
// §1.4's list screen and nothing else — title row, toolbar, table, pane, then
// the quarantine section.
//
// WHAT LEFT, AND WHY
//   * The eight hardcoded canonical lanes. The server returns `connectors[]`;
//     a row for a system this deployment was never connected to is a row about
//     nothing, and five of them made "no connectors" look like five connectors.
//     With none connected the table renders §9's empty line and stops.
//   * The env-var drawers. Internal setting names are not interface text (§0).
//   * The CONNECTED n-of-8 hero readout, the purpose line, the provenance essay
//     and the quarantine framing sentence. §1.3 deletes hero strips and §0
//     deletes copy that argues the product is trustworthy. The data is the claim.
//
// DEFAULT SORT IS SOURCE A–Z (§9). An operator checking connectors reads the
// same list in the same order every week; freshness order is one click away and
// is a question, not a resting state.
//
// THIN CLIENT (C7). Every value is a field of the one governed read; sorting and
// filtering reorder and hide rows the server already sent.

import { useCallback, useMemo, useState } from "react";
import type { ConnectorStatusView, QuarantinedItemView } from "../api/types";
import { entitlementsOf } from "../auth/entitlements";
import { usePrincipal } from "../auth/PrincipalContext";
import { useSurfaceAccess } from "../auth/useSurfaceAccess";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { ListScreen } from "../primitives/PageHeader";
import { StatusPill } from "../primitives/StatePill";
import { Empty, LoadError, Refused } from "../primitives/states";
import { FilterChip, FilterSelect, Toolbar, ToolbarButton } from "../primitives/Toolbar";
import { buildCsv, downloadCsv } from "../primitives/csv";
import { absoluteTime, formatCount, relativeTime } from "../primitives/format";
import { filterRows, sortRows } from "../primitives/tableSort";
import type { SortState } from "../primitives/tableSort";
import { QuarantinePane } from "./QuarantinePane";
import { QuarantineSection } from "./QuarantineSection";
import { AuthorityAttemptsSection } from "./AuthorityAttemptsSection";
import { SourcePane } from "./SourcePane";
import {
  ALL_KINDS,
  applyFilters,
  checkedRecencyOf,
  countText,
  filterLabels,
  kindsOf,
  lastCheckedAt,
  newestRecordAt,
  NO_FILTERS,
  recordRecencyOf,
} from "./sources";
import type { SourceFilters } from "./sources";
import { useConnectStatus } from "./useConnectStatus";
import { useQuarantineItems } from "./useQuarantineItems";
import { useSourceSync } from "./useSourceSync";
import type { SourceSyncUiState } from "./useSourceSync";
import "./connect.css";

export function ConnectPage() {
  const { status, data, loadedAt, refetch } = useConnectStatus();
  const sourceSync = useSourceSync(refetch);
  const entitlements = entitlementsOf(usePrincipal());
  const opsRefused = useSurfaceAccess().refused("ops");
  // Do not race the server-authored /v1/me answer. On a settled legacy response
  // with no advisory, unknown-is-not-denied still asks the guarded endpoints.
  const opsDeferred = entitlements.pending;
  // The ops-gated drill-down read. Refused/error ⇒ null rows: the categories
  // ledger still renders for everyone; no record content is fabricated.
  const quarantine = useQuarantineItems(opsRefused, opsDeferred);

  const [filters, setFilters] = useState<SourceFilters>(NO_FILTERS);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortState>({ key: "source", dir: "asc" });
  const [openId, setOpenId] = useState<string | null>(null);
  const [openQuarantineId, setOpenQuarantineId] = useState<string | null>(null);
  const quarantineItems = quarantine.status === "ok" ? (quarantine.data?.items ?? []) : null;
  const openQuarantineItem =
    openQuarantineId === null || quarantineItems === null
      ? null
      : (quarantineItems.find((item) => item.item_id === openQuarantineId) ?? null);

  const rows = useMemo<readonly ConnectorStatusView[]>(() => data?.connectors ?? [], [data]);

  const columns = useMemo(
    () => buildColumns(loadedAt, sourceSync.bySource, sourceSync.start),
    [loadedAt, sourceSync.bySource, sourceSync.start],
  );

  const filtered = useMemo(
    () => filterRows(applyFilters(rows, filters), columns, search),
    [rows, filters, columns, search],
  );
  /** Exactly the rows the table renders, in the order it renders them — so the
   * export is the window on screen rather than a re-derivation of it. */
  const windowRows = useMemo(() => sortRows(filtered, columns, sort), [filtered, columns, sort]);

  const kindOptions = useMemo(
    () => [
      { value: ALL_KINDS, label: "All kinds" },
      ...kindsOf(rows).map((kind) => ({ value: kind, label: kind })),
    ],
    [rows],
  );

  const labels = useMemo(() => filterLabels(filters, search), [filters, search]);

  const exportCsv = useCallback(() => {
    if (windowRows.length === 0) return;
    downloadCsv(
      "sources.csv",
      buildCsv<ConnectorStatusView>({
        title: "Sources",
        filters: labels,
        columns: [
          { header: "Source", value: (row) => row.id },
          { header: "Kind", value: (row) => row.kind },
          {
            header: "Posture",
            value: (row) => (row.posture === "snapshot" ? "Frozen snapshot" : "Live"),
          },
          {
            header: "Last checked",
            value: (row) => {
              const at = lastCheckedAt(row, loadedAt);
              return at === null ? "Never checked" : absoluteTime(at);
            },
          },
          {
            header: "Newest record",
            // An absent record cursor exports EMPTY (csv.ts): unlike "Never
            // checked" it is not a state, just a measurement nobody holds.
            value: (row) => {
              const at = newestRecordAt(row, loadedAt);
              return at === null ? "" : absoluteTime(at);
            },
          },
          {
            header: "Permissions check",
            // An absent field exports EMPTY (csv.ts): a dash is a rendering
            // convention and a spreadsheet would read it as data.
            value: (row) => (row.acl_mirror === null ? "" : row.acl_mirror.toUpperCase()),
          },
          {
            header: "Quarantined",
            value: (row) => (row.quarantined_count === null ? "" : String(row.quarantined_count)),
          },
        ],
        rows: windowRows,
      }),
    );
  }, [windowRows, labels, loadedAt]);

  const openRow = openId === null ? null : (rows.find((row) => row.id === openId) ?? null);

  // §2.4: refused and error are different answers and render as different
  // screens. The refused screen keeps the title row, so someone who followed a
  // link still sees which page refused them.
  if (status === "refused") {
    return (
      <div className="ne-sources">
        <ListScreen title="Sources">
          <Refused thing="sources" page="Sources" />
        </ListScreen>
      </div>
    );
  }

  const tableStatus: TableStatus = status === "loading" ? "loading" : status;

  return (
    <div className="ne-sources">
      <ListScreen title="Sources">
        <Toolbar
          search={{ value: search, onChange: setSearch, placeholder: "Filter sources…" }}
          filters={
            <>
              <FilterSelect
                label="Kind"
                value={filters.kind}
                options={kindOptions}
                onChange={(kind) => setFilters((f) => ({ ...f, kind }))}
              />
              {filters.kind === ALL_KINDS ? null : (
                <FilterChip
                  label={`Kind: ${filters.kind}`}
                  onDismiss={() => setFilters((f) => ({ ...f, kind: ALL_KINDS }))}
                />
              )}
            </>
          }
          count={status === "ok" ? countText(windowRows.length) : null}
          actions={
            <ToolbarButton
              label="Export CSV"
              onClick={exportCsv}
              disabled={windowRows.length === 0}
            />
          }
        />

        <DataTable<ConnectorStatusView>
          columns={columns}
          rows={filtered}
          rowKey={(row) => row.id}
          label="Sources"
          things="sources"
          sort={sort}
          onSortChange={setSort}
          status={tableStatus}
          onRowActivate={(row) => {
            setOpenQuarantineId(null);
            setOpenId(row.id);
          }}
          selectedKey={openId}
          empty={
            rows.length === 0 ? (
              <Empty message="No sources connected." />
            ) : (
              <Empty
                message="No matching sources."
                action={
                  <button
                    type="button"
                    className="ne-btn ne-btn--quiet"
                    onClick={() => {
                      setFilters(NO_FILTERS);
                      setSearch("");
                    }}
                  >
                    Show all {rows.length}
                  </button>
                }
              />
            )
          }
          error={<LoadError things="sources" onRetry={refetch} />}
        />

        <AuthorityAttemptsSection knownRefused={opsRefused} deferred={opsDeferred} />

        {status === "error" ? null : (
          <QuarantineSection
            total={data?.quarantine_total ?? null}
            reasons={data?.quarantine_reasons ?? []}
            status={tableStatus}
            items={quarantineItems}
            onOpenItem={(item: QuarantinedItemView) => {
              setOpenId(null);
              setOpenQuarantineId(item.item_id);
            }}
            selectedItemId={openQuarantineId}
          />
        )}
      </ListScreen>

      {openRow === null ? null : (
        <SourcePane
          source={openRow}
          loadedAt={loadedAt}
          opsRefused={opsRefused}
          onClose={() => setOpenId(null)}
        />
      )}
      {openQuarantineItem === null ? null : (
        <QuarantinePane item={openQuarantineItem} onClose={() => setOpenQuarantineId(null)} />
      )}
    </div>
  );
}

/**
 * §9's connector table. Sortable: Source, Kind, Last checked, Newest record,
 * Quarantined — exactly those. Permissions check is a two-word state whose
 * order nobody would agree on, and §9 does not list it.
 *
 * Last checked and Newest record are DIFFERENT facts (SPEC §12 item 9): the run
 * age advances every cycle, the record age only when something new arrived. A
 * connector polling a quiet source reads "Last checked: 2 min ago · Newest
 * record: 1 d ago" — both true, each under its own name. "Never checked" is a
 * state and renders as words; an absent record cursor is a measurement nobody
 * holds and renders as a dash with the reason in the title.
 *
 * The columns close over `loadedAt` because each served age is an age AS OF THE
 * READ: the instant it describes is only knowable from the moment the response
 * arrived.
 */
function buildColumns(
  loadedAt: number,
  syncState: Readonly<Record<string, SourceSyncUiState>>,
  startSync: (source: string) => Promise<void>,
): ReadonlyArray<Column<ConnectorStatusView>> {
  return [
    {
      key: "source",
      header: "Source",
      mono: true,
      cell: (row) => row.id,
      sortValue: (row) => row.id,
      searchText: (row) => `${row.id} ${row.kind}`,
    },
    {
      key: "kind",
      header: "Kind",
      cell: (row) => row.kind,
      sortValue: (row) => row.kind,
      searchText: (row) => row.kind,
    },
    {
      key: "lastChecked",
      header: "Last checked",
      numeric: true,
      cell: (row) => {
        const at = lastCheckedAt(row, loadedAt);
        if (at === null)
          return (
            <span className="ne-absent" title="No sync cycle has run for this source">
              Never checked
            </span>
          );
        return (
          <time dateTime={at} title={absoluteTime(at)}>
            {relativeTime(at, loadedAt)}
          </time>
        );
      },
      // Recency, not age, so ▼ means newest first — see sources.ts.
      sortValue: checkedRecencyOf,
      firstSort: "desc",
    },
    {
      key: "newestRecord",
      header: "Newest record",
      numeric: true,
      cell: (row) => {
        const at = newestRecordAt(row, loadedAt);
        if (at === null)
          return (
            <span className="ne-absent" title="No record cursor recorded">
              —
            </span>
          );
        return (
          <time dateTime={at} title={absoluteTime(at)}>
            {relativeTime(at, loadedAt)}
          </time>
        );
      },
      sortValue: recordRecencyOf,
      firstSort: "desc",
    },
    {
      key: "permissions",
      header: "Permissions check",
      width: "180px",
      cell: (row) =>
        row.acl_mirror === "ok" ? (
          // This is the outcome of the LAST recorded mirror check, not a
          // current source-health verdict. Binding those words to the pill is
          // especially important when Last checked is visibly old.
          <StatusPill word="OK" detail="at last check" />
        ) : row.acl_mirror === "degraded" ? (
          // Same rule as OK: this is the LAST recorded check, not a current
          // verdict. An unqualified DEGRADED reads as "degraded right now".
          <StatusPill word="DEGRADED" detail="at last check" />
        ) : (
          <span className="ne-absent" title="No sync recorded yet">
            —
          </span>
        ),
    },
    {
      key: "quarantined",
      header: "Quarantined",
      numeric: true,
      width: "132px",
      cell: (row) =>
        row.quarantined_count === null ? (
          <span className="ne-absent" title="Quarantine store unreadable">
            —
          </span>
        ) : (
          formatCount(row.quarantined_count)
        ),
      sortValue: (row) => row.quarantined_count,
      firstSort: "desc",
    },
    {
      key: "posture",
      header: "Posture",
      width: "156px",
      cell: (row) =>
        row.posture === "snapshot" ? (
          <span className="ne-sources__snapshot" aria-label="Frozen snapshot">
            Frozen snapshot
          </span>
        ) : (
          "Live"
        ),
      searchText: (row) => (row.posture === "snapshot" ? "Frozen snapshot" : "Live"),
    },
    {
      key: "sync",
      header: "Sync",
      width: "132px",
      cell: (row) => {
        // A snapshot is historical evidence, never an execution lane. Keep the
        // client fail-closed even if an older/inconsistent server response also
        // carries sync_allowed=true; the POST remains authoritative too.
        if (!row.sync_allowed || row.posture === "snapshot") return null;
        const current = syncState[row.id];
        const busy = current?.phase === "queued" || current?.phase === "running";
        const label =
          current?.phase === "queued"
            ? "Queued…"
            : current?.phase === "running"
              ? "Syncing…"
              : current?.phase === "succeeded"
                ? "Synced"
                : current?.phase === "failed"
                  ? current.refused
                    ? "Sync refused"
                    : "Retry sync"
                  : "Sync now";
        const countsText =
          current?.phase === "succeeded" && current.counts !== null
            ? `${current.counts.records_seen} seen · ${current.counts.written} written · ${current.counts.skipped_unchanged} unchanged · ${current.counts.invalid} invalid · ${current.counts.quarantined} quarantined`
            : null;
        return (
          <div className="ne-sources__sync">
            <button
              type="button"
              className="ne-btn ne-btn--quiet"
              aria-label={`${label}: ${row.id}${countsText === null ? "" : `. ${countsText}`}`}
              disabled={busy}
              onClick={(event) => {
                event.stopPropagation();
                void startSync(row.id);
              }}
            >
              {label}
            </button>
            {countsText === null ? null : (
              <span className="ne-sources__synccounts" role="status" aria-live="polite">
                {countsText}
              </span>
            )}
          </div>
        );
      },
    },
  ];
}
