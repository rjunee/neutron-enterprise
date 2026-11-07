// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §11 — IMPROVEMENT › QUEUE. The steward's triage surface.
//
// The shipped screen was a stack of cards: no table, no filters, no count, no
// sortable anything, an animated ring beside it and a paragraph naming the route
// it read from. It is replaced by §1.4's list screen — toolbar, table, pane —
// with the columns and the default sort §11 states and nothing else.
//
// DEFAULT SORT IS CREATED DESCENDING (§11). What the system proposed most
// recently is what the steward has not yet seen.
//
// THIN CLIENT (C7). The list is whatever the steward-gated read returns for this
// bearer; this file filters and orders rows the server already sent and computes
// no verdict, no confidence and no permission. A REFUSAL AND AN OUTAGE ARE
// DIFFERENT ANSWERS and render differently — the refused state is the server's
// 403 and is drawn for nothing else, because telling a reader their role is the
// reason a read failed, when it was not, is a false statement about their
// authorization.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import type { RejectionCodeWire, ReviewEventView } from "../api/types";
import { ApiError } from "../api/client";
import { useApiClient } from "../auth/useApiClient";
import { useLatestRequest } from "../hooks/useLatestRequest";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { StatusPill } from "../primitives/StatePill";
import { filterRows, sortRows } from "../primitives/tableSort";
import type { SortState } from "../primitives/tableSort";
import { FilterChip, FilterSelect, Toolbar, ToolbarButton } from "../primitives/Toolbar";
import { buildCsv, downloadCsv } from "../primitives/csv";
import { Empty, LoadError, Refused } from "../primitives/states";
import { ABSENT, absoluteTime, relativeTime } from "../primitives/format";
import { ProposalPane } from "./ProposalPane";
import type { ProposalActionState, SkillApplyDecision } from "./ProposalPane";
import {
  applyFilters,
  enumLabel,
  findProposal,
  LANES,
  NO_FILTERS,
  priorityRank,
  proposalCount,
  queueRowViews,
  statusWord,
  WITHHELD_LABEL,
} from "./queue";
import type { LaneFilter, QueueFilters, QueueRowView, StatusFilter } from "./queue";

type ReadStatus = "loading" | "ok" | "refused" | "error";

const LANE_OPTIONS: ReadonlyArray<{ value: LaneFilter; label: string }> = [
  { value: "all", label: "All lanes" },
  ...LANES.map((lane) => ({ value: lane as LaneFilter, label: enumLabel(lane) })),
];

const STATUS_OPTIONS: ReadonlyArray<{ value: StatusFilter; label: string }> = [
  { value: "all", label: "All statuses" },
  { value: "proposed", label: "Proposed" },
  { value: "approved", label: "Approved" },
];

/**
 * §11's Queue table. Sortable: Lane, Title, Confidence, Priority, Created,
 * Status — exactly the six the spec names. KIND IS NOT SORTABLE: it is a served
 * enum with no order a reader would agree on, and the lane filter is the control
 * that actually narrows by category.
 */
/**
 * 015A0-ENFORCEMENT: every protected cell reads `row.proposal`, which is null
 * for any row the server did not state as available. A withheld row therefore
 * renders the literal **Content withheld** in Title and the console's absent
 * mark everywhere else — and its lane, kind, cluster id, confidence and
 * priority are not in the sort keys or the search index, because they are not
 * on the object.
 */
const COLUMNS: ReadonlyArray<Column<QueueRowView>> = [
  {
    key: "lane",
    header: "Lane",
    width: "140px",
    cell: (row) => (row.proposal === null ? ABSENT : enumLabel(row.proposal.lane)),
    sortValue: (row) => row.proposal?.lane ?? "",
    searchText: (row) => row.proposal?.lane ?? "",
  },
  {
    key: "title",
    header: "Title",
    cell: (row) => (row.proposal === null ? WITHHELD_LABEL : row.proposal.artifact.title),
    sortValue: (row) => row.proposal?.artifact.title ?? WITHHELD_LABEL,
    searchText: (row) =>
      row.proposal === null
        ? row.proposalId
        : `${row.proposal.artifact.title} ${row.proposalId} ${row.proposal.cluster_id}`,
  },
  {
    key: "kind",
    header: "Kind",
    width: "160px",
    cell: (row) => (row.proposal === null ? ABSENT : enumLabel(row.proposal.kind)),
    searchText: (row) => row.proposal?.kind ?? "",
  },
  {
    key: "confidence",
    header: "Confidence",
    numeric: true,
    width: "104px",
    cell: (row) => (row.proposal === null ? ABSENT : row.proposal.confidence.toFixed(2)),
    sortValue: (row) => row.proposal?.confidence ?? -1,
    firstSort: "desc",
  },
  {
    key: "priority",
    header: "Priority",
    width: "96px",
    cell: (row) => (row.proposal === null ? ABSENT : enumLabel(row.proposal.priority)),
    sortValue: (row) => (row.proposal === null ? 4 : priorityRank(row.proposal.priority)),
    searchText: (row) => row.proposal?.priority ?? "",
  },
  {
    key: "created",
    header: "Created",
    numeric: true,
    width: "112px",
    cell: (row) => (
      <time dateTime={row.createdAt} title={absoluteTime(row.createdAt)}>
        {relativeTime(row.createdAt)}
      </time>
    ),
    sortValue: (row) => Date.parse(row.createdAt),
    firstSort: "desc",
  },
  {
    key: "status",
    header: "Status",
    width: "128px",
    cell: (row) => {
      const word = statusWord(row.status);
      return word === null ? null : <StatusPill word={word} />;
    },
    sortValue: (row) => statusWord(row.status),
  },
];

export function QueueTab() {
  const client = useApiClient();
  const runLatest = useLatestRequest();
  const [params, setParams] = useSearchParams();

  const [rows, setRows] = useState<readonly QueueRowView[]>([]);
  const [status, setStatus] = useState<ReadStatus>("loading");
  const [filters, setFilters] = useState<QueueFilters>(NO_FILTERS);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortState>({ key: "created", dir: "desc" });
  const [actions, setActions] = useState<Record<string, ProposalActionState>>({});
  /** The row the pane was opened on, kept so the pane survives the refetch that
   * removes a decided proposal from the queue. */
  const [openedRow, setOpenedRow] = useState<QueueRowView | null>(null);

  const openParam = params.get("proposal");

  const refetch = useCallback(async () => {
    const settled = await runLatest(() => client.listStewardProposals());
    if (settled.kind === "superseded") return;
    if (settled.kind === "success") {
      // Resolved through the server's disclosure status at the boundary, so no
      // component downstream ever holds an unresolved row.
      setRows(queueRowViews(settled.value.proposals));
      setStatus("ok");
      return;
    }
    // A decided proposal must never linger actionable under a failed read, so
    // the rows clear atomically with the failure. 403 is the server refusing
    // this ROLE; anything else is a read that did not arrive, and the two are
    // never rendered as one another.
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

  const submit = useCallback(
    async (id: string, run: () => Promise<ReviewEventView>) => {
      setActions((prev) => ({ ...prev, [id]: { busy: true } }));
      try {
        const review = await run();
        setActions((prev) => ({ ...prev, [id]: { review } }));
      } catch (err) {
        const detail = err instanceof ApiError ? err.userMessage : null;
        setActions((prev) => ({ ...prev, [id]: { error: { detail } } }));
      }
      // The row updates from the server's next answer either way.
      await refetch();
    },
    [refetch],
  );

  // ---- the rendered window -------------------------------------------------

  const filtered = useMemo(
    () => filterRows(applyFilters(rows, filters), COLUMNS, search),
    [rows, filters, search],
  );
  /** Exactly the rows the table renders, in the order it renders them — so the
   * export is the window on screen and not a re-derivation of it. */
  const windowRows = useMemo(() => sortRows(filtered, COLUMNS, sort), [filtered, sort]);

  const filterLabels = useMemo(() => {
    const out: string[] = [];
    if (filters.lane !== "all") out.push(`Lane: ${enumLabel(filters.lane)}`);
    if (filters.status !== "all") out.push(`Status: ${enumLabel(filters.status)}`);
    if (search.trim() !== "") out.push(`Search: ${search.trim()}`);
    return out;
  }, [filters, search]);

  const exportCsv = useCallback(() => {
    if (windowRows.length === 0) return;
    downloadCsv(
      "proposals.csv",
      buildCsv<QueueRowView>({
        title: "Improvement proposals",
        filters: filterLabels,
        // The export is the window on screen, withheld rows included and
        // withheld: a row the table would not show cannot be dereferenced by a
        // column here, because the protected fields hang off `proposal`.
        columns: [
          { header: "Lane", value: (row) => row.proposal?.lane ?? "" },
          { header: "Title", value: (row) => row.proposal?.artifact.title ?? WITHHELD_LABEL },
          { header: "Kind", value: (row) => row.proposal?.kind ?? "" },
          { header: "Confidence", value: (row) => row.proposal?.confidence.toFixed(2) ?? "" },
          { header: "Priority", value: (row) => row.proposal?.priority ?? "" },
          { header: "Created", value: (row) => absoluteTime(row.createdAt) },
          { header: "Status", value: (row) => row.status },
          { header: "Cluster id", value: (row) => row.proposal?.cluster_id ?? "" },
        ],
        rows: windowRows,
      }),
    );
  }, [windowRows, filterLabels]);

  // ---- the open pane -------------------------------------------------------

  const listedOpenRow = findProposal(rows, openParam);
  const openRow = listedOpenRow ?? (openParam !== null && openedRow !== null ? openedRow : null);
  const openState = openParam === null ? {} : (actions[openParam] ?? {});
  // The review response is the server's authoritative transition. Keep the
  // pane's status and available actions on that transition even when the queue
  // refetch still has, or has already removed, the row that opened the pane.
  const paneProposal =
    openRow === null || openState.review === undefined
      ? openRow
      : {
          ...openRow,
          status: openState.review.to_status,
          proposal:
            openRow.proposal === null
              ? null
              : { ...openRow.proposal, status: openState.review.to_status },
        };
  // A settled open-queue read no longer contains the row and this console did
  // not submit the decision: another steward closed it. The terminal status is
  // unknown, so say only what the served absence proves and offer no actions.
  const decidedElsewhere =
    openRow !== null && status === "ok" && openState.review === undefined && listedOpenRow === null;

  const openPane = useCallback(
    (row: QueueRowView) => {
      setOpenedRow(row);
      const next = new URLSearchParams(params);
      next.set("proposal", row.proposalId);
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  const closePane = useCallback(() => {
    setOpenedRow(null);
    const next = new URLSearchParams(params);
    next.delete("proposal");
    setParams(next, { replace: true });
  }, [params, setParams]);

  // ---- render --------------------------------------------------------------

  if (status === "refused") {
    return <Refused thing="Improvement" page="Improvement" />;
  }

  const tableStatus: TableStatus =
    status === "loading" ? "loading" : status === "error" ? "error" : "ok";
  const narrowed = filters.lane !== "all" || filters.status !== "all" || search.trim() !== "";

  return (
    <>
      <Toolbar
        search={{ value: search, onChange: setSearch, placeholder: "Filter proposals…" }}
        filters={
          <>
            <FilterSelect<LaneFilter>
              label="Lane"
              value={filters.lane}
              options={LANE_OPTIONS}
              onChange={(lane) => setFilters((f) => ({ ...f, lane }))}
            />
            <FilterSelect<StatusFilter>
              label="Status"
              value={filters.status}
              options={STATUS_OPTIONS}
              onChange={(next) => setFilters((f) => ({ ...f, status: next }))}
            />
            {filters.lane === "all" ? null : (
              <FilterChip
                label={`Lane: ${enumLabel(filters.lane)}`}
                onDismiss={() => setFilters((f) => ({ ...f, lane: "all" }))}
              />
            )}
            {filters.status === "all" ? null : (
              <FilterChip
                label={`Status: ${enumLabel(filters.status)}`}
                onDismiss={() => setFilters((f) => ({ ...f, status: "all" }))}
              />
            )}
          </>
        }
        count={status === "ok" ? proposalCount(windowRows.length) : null}
        actions={
          <ToolbarButton
            label="Export CSV"
            onClick={exportCsv}
            disabled={windowRows.length === 0}
          />
        }
      />

      <DataTable<QueueRowView>
        columns={COLUMNS}
        rows={filtered}
        rowKey={(row) => row.proposalId}
        label="Proposals"
        things="proposals"
        sort={sort}
        onSortChange={setSort}
        status={tableStatus}
        onRowActivate={openPane}
        selectedKey={openRow?.proposalId ?? null}
        empty={
          narrowed && rows.length > 0 ? (
            <Empty
              message="No matching proposals."
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
          ) : (
            <Empty message="No open proposals." />
          )
        }
        error={<LoadError things="proposals" onRetry={() => void refetch()} />}
      />

      {paneProposal === null ? null : (
        <ProposalPane
          row={paneProposal}
          state={openState}
          decidedElsewhere={decidedElsewhere}
          onClose={closePane}
          onApprove={(note) =>
            void submit(paneProposal.proposalId, () =>
              client.approveProposal(paneProposal.proposalId, note),
            )
          }
          onReject={(code: RejectionCodeWire, note: string) =>
            void submit(paneProposal.proposalId, () =>
              client.rejectProposal(paneProposal.proposalId, code, note),
            )
          }
          onRecordApply={(ref, note) =>
            void submit(paneProposal.proposalId, () =>
              client.recordApply(paneProposal.proposalId, ref, note),
            )
          }
          onApplySkill={(decision: SkillApplyDecision) =>
            void submit(paneProposal.proposalId, () =>
              client
                .applySkillProposal(paneProposal.proposalId, {
                  acl_labels: decision.aclLabels,
                  global_visibility: decision.globalVisibility,
                  note: decision.note,
                  // Omitted entirely when nothing changed — an unedited apply
                  // must not trigger a re-validation the steward did not ask for.
                  ...(decision.edits !== undefined ? { edits: decision.edits } : {}),
                })
                .then((result) => result.review),
            )
          }
        />
      )}
    </>
  );
}
