// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §8 — APPROVALS. The highest-frequency screen in the product.
//
// The job: what is pending, who asked, what exactly happens if approved, what
// tier — and a decision in as few actions as possible, then a receipt. Measured
// on the shipped console this screen had no table, no filters, no count and no
// sortable anything; it is replaced, not edited.
//
// The shape is §1.4's list screen and nothing else: a title row, a toolbar, a
// table, a pane. No purpose sentence, no hero readouts, no queue notice, no
// per-card drawers, no demo trigger.
//
// DEFAULT SORT IS DUE ASCENDING. The item about to breach is the item being
// worked; any other default is the queue choosing an order for its own
// convenience.
//
// NO BULK SELECT, BY RULING. Each approval is an individually accountable
// decision with its own confirm and its own reason. A select-all-approve control
// is the exact failure a governed gate exists to prevent, so the table declares
// no selection and this screen has no path that decides more than one request.
// Bulk EXPORT is the whole-table Export CSV, which decides nothing.
//
// THIN CLIENT (C7). The queue is whatever the governed read returns for this
// bearer. Decisions are submitted with NO approver identity in the body — the
// server derives the approver from the verified bearer, and the returned
// approver and decided-at render as sent. A decided row leaves the pending table
// only via a server refetch, never a local splice.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import type { ApprovalActionResult, AuditRecordView, PendingApprovalView } from "../api/types";
import { ApiError } from "../api/client";
import { useDirectory } from "../directory/DirectoryContext";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { usePendingApprovalsQueue } from "../hooks/usePendingApprovals";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import type { SortState } from "../primitives/tableSort";
import { filterRows, sortRows } from "../primitives/tableSort";
import { ListScreen } from "../primitives/PageHeader";
import { displayPrincipal, PrincipalLabel } from "../primitives/PrincipalLabel";
import { StatusPill } from "../primitives/StatePill";
import { Empty, LoadError, Refused } from "../primitives/states";
import {
  FilterChip,
  FilterSelect,
  ToggleChip,
  Toolbar,
  ToolbarButton,
} from "../primitives/Toolbar";
import { buildCsv, downloadCsv } from "../primitives/csv";
import { ABSENT, absoluteTime, relativeTime } from "../primitives/format";
import { DecisionPane } from "./DecisionPane";
import type { DecisionState } from "./DecisionPane";
import { chainDecisions, decisionWord, namesRequest, seqForRequest } from "./chain";
import { verdictWord } from "./receipt";
import {
  applyFilters,
  countText,
  deadlineMs,
  dueCell,
  isOverdue,
  NO_FILTERS,
  requesters,
  statusWord,
  timeOrDate,
} from "./queue";
import type { QueueFilters, TierFilter } from "./queue";
import { useNow } from "./useNow";
import type { ApprovalOwner, ApprovalRequestTicket } from "./approvalOwner";
import {
  approvalOwnerReady,
  sameApprovalOwner,
  useApprovalOwner,
  useApprovalRequestGate,
} from "./approvalOwner";
import "./approvals.css";

const TIER_OPTIONS: ReadonlyArray<{ value: TierFilter; label: string }> = [
  { value: "all", label: "All tiers" },
  { value: "2", label: "Tier 2" },
  { value: "3", label: "Tier 3" },
];

type SubmissionOutcome =
  | { kind: "result"; result: ApprovalActionResult }
  | { kind: "error"; error: unknown }
  | { kind: "superseded" };

type DecisionKind = "approve" | "deny";

export function ApprovalsPage() {
  const owner = useApprovalOwner();
  const [params, setParams] = useSearchParams();
  const [routeOwner, setRouteOwner] = useState<ApprovalOwner | null>(null);
  const routeOwned = routeOwner === null || sameApprovalOwner(routeOwner, owner);
  if (
    approvalOwnerReady(owner) &&
    (routeOwner === null || (!routeOwned && !params.has("request")))
  ) {
    // The first server-served ready principal claims an initial deep link; a
    // loading placeholder never does. Later ready owners can claim the route
    // only after the prior owner's request has been removed.
    setRouteOwner(owner);
  }
  const visibleParams = useMemo(() => {
    if (routeOwned) return params;
    const sanitized = new URLSearchParams(params);
    sanitized.delete("request");
    return sanitized;
  }, [params, routeOwned]);
  const requestId = visibleParams.get("request") ?? "";
  const { begin, owns } = useApprovalRequestGate(owner, requestId);
  const inFlightRef = useRef<ApprovalRequestTicket | null>(null);

  useLayoutEffect(() => {
    if (routeOwner === null || sameApprovalOwner(routeOwner, owner)) return;
    if (!params.has("request")) return;
    const sanitized = new URLSearchParams(params);
    sanitized.delete("request");
    setParams(sanitized, { replace: true });
  }, [owner, params, routeOwner, setParams]);

  const startDecision = useCallback(
    (
      expectedOwner: ApprovalOwner,
      approval: PendingApprovalView,
      kind: DecisionKind,
      reason?: string,
    ): Promise<SubmissionOutcome> | null => {
      const active = inFlightRef.current;
      // Synchronous guard: two clicks before React can paint `disabled` still
      // issue exactly one accountable write.
      if (active !== null && owns(active)) return null;
      const ticket = begin(expectedOwner, approval.request_id);
      if (ticket === null) return null;
      inFlightRef.current = ticket;

      return (async () => {
        try {
          const result =
            kind === "approve"
              ? await expectedOwner.client.approve(approval.request_id)
              : await expectedOwner.client.reject(approval.request_id, reason);
          if (!owns(ticket)) return { kind: "superseded" };
          inFlightRef.current = null;
          return { kind: "result", result };
        } catch (error) {
          if (!owns(ticket)) return { kind: "superseded" };
          inFlightRef.current = null;
          return { kind: "error", error };
        }
      })();
    },
    [begin, owns],
  );

  return (
    <ApprovalsPageOwned
      key={owner.generation}
      owner={owner}
      params={visibleParams}
      setParams={setParams}
      startDecision={startDecision}
    />
  );
}

function ApprovalsPageOwned({
  owner,
  params,
  setParams,
  startDecision,
}: {
  owner: ApprovalOwner;
  params: URLSearchParams;
  setParams: ReturnType<typeof useSearchParams>[1];
  startDecision: (
    owner: ApprovalOwner,
    approval: PendingApprovalView,
    kind: DecisionKind,
    reason?: string,
  ) => Promise<SubmissionOutcome> | null;
}) {
  const directory = useDirectory();
  const poll = useAuditPoll();
  const now = useNow(1000);

  // The table, Home and the rail badge are three projections of ONE served
  // array. The provider owns polling/retry cadence; this screen owns no reader.
  const { rows, status, refetch } = usePendingApprovalsQueue();
  const [filters, setFilters] = useState<QueueFilters>(NO_FILTERS);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortState>({ key: "due", dir: "asc" });
  /** The pane's identity lives in the URL, so a deep link from a pending marker
   * (§2.5) and a row click are the same state — and no effect has to sync one
   * onto the other. */
  const openId = params.get("request");
  /** The row the pane was opened on, kept so the pane survives the refetch that
   * removes it — a row somebody else decided must not blank the pane of the
   * person reading it. */
  const [openedRow, setOpenedRow] = useState<PendingApprovalView | null>(null);
  const [decisions, setDecisions] = useState<Record<string, DecisionState>>({});
  /** The rows this sitting decided, held so the receipt and the Decided section
   * survive the refetch that removes them from the pending queue. */
  const [decidedRows, setDecidedRows] = useState<PendingApprovalView[]>([]);

  const submit = useCallback(
    async (approval: PendingApprovalView, kind: DecisionKind, reason?: string) => {
      const id = approval.request_id;
      const pending = startDecision(owner, approval, kind, reason);
      if (pending === null) return;
      setDecisions((prev) => ({ ...prev, [id]: { startedAt: Date.now() } }));
      const outcome = await pending;
      if (outcome.kind === "superseded") return;
      if (outcome.kind === "result") {
        setDecisions((prev) => ({ ...prev, [id]: { result: outcome.result } }));
        setDecidedRows((prev) =>
          prev.some((row) => row.request_id === id) ? prev : [approval, ...prev],
        );
      } else {
        const conflict = outcome.error instanceof ApiError && outcome.error.status === 409;
        // A CONFLICT LINE STATES THE CONFLICT ITSELF; what follows the dash is
        // the gate's own account of it or nothing at all (approvals/detail.ts
        // states the same rule for the disclosure read). `userMessage` there
        // would print "Already decided — Someone changed this first.": the
        // console's house sentence for a 409, repeating the line it is attached
        // to and reading as the gate's stated reason. The fault card is the
        // other branch and keeps the sentence — it attributes nothing, and
        // "The server is unavailable." under "Couldn't submit the decision.
        // Try again." is the reader being told what happened.
        const detail = !(outcome.error instanceof ApiError)
          ? null
          : conflict
            ? (outcome.error.detail ?? null)
            : outcome.error.userMessage;
        setDecisions((prev) => ({ ...prev, [id]: { error: { detail, conflict } } }));
      }
      // The queue is re-read either way. A decided row leaves the pending table
      // because the SERVER stopped returning it — never because this console
      // removed it.
      await refetch();
    },
    [owner, refetch, startDecision],
  );

  // ---- the rendered window -------------------------------------------------

  const columns = useMemo<ReadonlyArray<Column<PendingApprovalView>>>(
    () => buildColumns(now, decisions, (id) => directory.lookup(id)?.name),
    [now, decisions, directory],
  );

  const filtered = useMemo(
    () => filterRows(applyFilters(rows, filters, now), columns, search),
    [rows, filters, now, columns, search],
  );
  /** Exactly the rows the table renders, in the order it renders them — so the
   * export is the window on screen and not a re-derivation of it. */
  const windowRows = useMemo(() => sortRows(filtered, columns, sort), [filtered, columns, sort]);

  const overdueCount = useMemo(
    () => windowRows.filter((row) => isOverdue(row, now)).length,
    [windowRows, now],
  );
  /** What clearing the overdue toggle alone would show. */
  const withoutOverdue = useMemo(
    () => filterRows(applyFilters(rows, { ...filters, overdueOnly: false }, now), columns, search),
    [rows, filters, now, columns, search],
  );

  const requesterOptions = useMemo(
    () => [
      { value: "all", label: "All requesters" },
      ...requesters(rows).map((id) => ({
        value: id,
        label: displayPrincipal(id, directory.lookup(id)),
      })),
    ],
    [rows, directory],
  );

  const filterLabels = useMemo(() => {
    const out: string[] = [];
    if (filters.tier !== "all") out.push(`Tier ${filters.tier}`);
    if (filters.requester !== "all") out.push(`Requested by: ${filters.requester}`);
    if (filters.overdueOnly) out.push("Overdue only");
    if (search.trim() !== "") out.push(`Search: ${search.trim()}`);
    return out;
  }, [filters, search]);

  const exportCsv = useCallback(() => {
    if (windowRows.length === 0) return;
    downloadCsv(
      "approvals.csv",
      buildCsv<PendingApprovalView>({
        title: "Approvals",
        filters: filterLabels,
        columns: [
          { header: "Action", value: (row) => row.action },
          { header: "Tier", value: (row) => String(row.tier) },
          { header: "Requested by", value: (row) => row.principal_id },
          { header: "Requested", value: (row) => absoluteTime(row.requested_at) },
          { header: "Due", value: (row) => absoluteTime(row.sla_deadline) },
          { header: "Expires", value: (row) => absoluteTime(row.expires_at) },
          {
            header: "Status",
            value: (row) =>
              statusWord(row, now, decisions[row.request_id]?.result?.status === "pending_second"),
          },
          { header: "Request id", value: (row) => row.request_id },
        ],
        rows: windowRows,
      }),
    );
  }, [windowRows, filterLabels, now, decisions]);

  // ---- the open pane -------------------------------------------------------

  const openRow =
    openId === null
      ? null
      : (rows.find((row) => row.request_id === openId) ??
        decidedRows.find((row) => row.request_id === openId) ??
        (openedRow?.request_id === openId ? openedRow : null));
  // A deep link (`?request=` — a pending marker's "View request", a chat tool
  // row, the palette) whose id is not in the served queue MUST say so: the
  // request was decided, expired, or never existed, and a silent miss is
  // visually identical to a successful navigation (§12 item 8's shape). Only a
  // settled queue can assert the absence — a loading or failed read cannot.
  const missedLink = openId !== null && openRow === null && status === "ok";
  const openState: DecisionState = openId === null ? {} : (decisions[openId] ?? {});
  // The row left the queue and this console never submitted a decision for it:
  // somebody else decided it. The pane says so rather than offering buttons the
  // gate will refuse.
  const decidedElsewhere =
    openRow !== null &&
    status === "ok" &&
    openState.result === undefined &&
    openState.startedAt === undefined &&
    !rows.some((row) => row.request_id === openRow.request_id);

  const openPane = useCallback(
    (row: PendingApprovalView) => {
      setOpenedRow(row);
      const next = new URLSearchParams(params);
      next.set("request", row.request_id);
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  const closePane = useCallback(() => {
    setOpenedRow(null);
    const next = new URLSearchParams(params);
    next.delete("request");
    setParams(next, { replace: true });
  }, [params, setParams]);

  // §8's `e`. `a` and `d` belong to the pane (the control they open is its
  // state); `j/k`, `Enter`, `Esc` and `o` belong to the table standard.
  //
  // The export closure changes every second — it closes over the ticking clock
  // — so it rides a ref that an effect keeps current, rather than re-binding a
  // window listener once a second. Nothing fires while focus is in a field.
  const exportRef = useRef(exportCsv);
  useEffect(() => {
    exportRef.current = exportCsv;
  }, [exportCsv]);
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key !== "e") return;
      // §2.6's `g e` goes to Evaluations, and the shell consumes that key on the
      // document before this listener sees it. Without this guard, navigating
      // away by keyboard would also download a CSV on the way out.
      if (event.defaultPrevented) return;
      const target = event.target;
      if (target instanceof HTMLElement) {
        const tag = target.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable) {
          return;
        }
      }
      event.preventDefault();
      exportRef.current();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // ---- render --------------------------------------------------------------

  const tableStatus: TableStatus =
    status === "loading" ? "loading" : status === "error" ? "error" : "ok";

  // The refused screen is the SAME shell — the title row survives, so a person
  // who followed a link still sees which page refused them, and the heading is
  // not torn down and rebuilt between the read and its answer.
  if (status === "refused") {
    return (
      <div className="ne-approvals">
        <ListScreen title="Approvals">
          <Refused thing="approvals" page="Approvals" />
        </ListScreen>
      </div>
    );
  }

  return (
    <div className="ne-approvals">
      <ListScreen title="Approvals">
        <Toolbar
          search={{ value: search, onChange: setSearch, placeholder: "Filter approvals…" }}
          filters={
            <>
              <FilterSelect<TierFilter>
                label="Tier"
                value={filters.tier}
                options={TIER_OPTIONS}
                onChange={(tier) => setFilters((f) => ({ ...f, tier }))}
              />
              <FilterSelect
                label="Requested by"
                value={filters.requester}
                options={requesterOptions}
                onChange={(requester) => setFilters((f) => ({ ...f, requester }))}
              />
              <ToggleChip
                label="Overdue only"
                pressed={filters.overdueOnly}
                onChange={(overdueOnly) => setFilters((f) => ({ ...f, overdueOnly }))}
              />
              {filters.tier === "all" ? null : (
                <FilterChip
                  label={`Tier ${filters.tier}`}
                  onDismiss={() => setFilters((f) => ({ ...f, tier: "all" }))}
                />
              )}
              {filters.requester === "all" ? null : (
                <FilterChip
                  label={`Requested by: ${filters.requester}`}
                  onDismiss={() => setFilters((f) => ({ ...f, requester: "all" }))}
                />
              )}
            </>
          }
          count={status === "ok" ? countText(windowRows.length, overdueCount) : null}
          actions={
            <ToolbarButton
              label="Export CSV"
              onClick={exportCsv}
              disabled={windowRows.length === 0}
            />
          }
        />

        {missedLink ? (
          <p className="ne-approvals__missedlink" role="alert">
            Request <code>{openId}</code> is not in the pending queue — it was already decided,
            expired, or never existed.{" "}
            <button type="button" className="ne-btn ne-btn--quiet" onClick={closePane}>
              Dismiss
            </button>
          </p>
        ) : null}

        <DataTable<PendingApprovalView>
          columns={columns}
          rows={filtered}
          rowKey={(row) => row.request_id}
          label="Pending approvals"
          things="approvals"
          sort={sort}
          onSortChange={setSort}
          status={tableStatus}
          onRowActivate={openPane}
          selectedKey={openId}
          empty={
            filters.overdueOnly ? (
              <Empty
                message="Nothing is overdue."
                action={
                  <button
                    type="button"
                    className="ne-btn ne-btn--quiet"
                    onClick={() => setFilters((f) => ({ ...f, overdueOnly: false }))}
                  >
                    Show all {withoutOverdue.length}
                  </button>
                }
              />
            ) : rows.length > 0 ? (
              <Empty
                message="No matching approvals."
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
              <Empty message="No pending approvals." />
            )
          }
          error={<LoadError things="approvals" onRetry={() => void refetch()} />}
        />

        <DecidedSection
          decidedRows={decidedRows}
          decisions={decisions}
          events={poll.events}
          chainReadable={poll.status === "ok"}
        />
      </ListScreen>

      {openRow === null ? null : (
        <DecisionPane
          key={`${owner.generation}:${openRow.request_id}`}
          approval={openRow}
          now={now}
          state={openState}
          decidedElsewhere={decidedElsewhere}
          auditSeq={poll.status === "ok" ? seqForRequest(poll.events, openRow.request_id) : null}
          chainReadable={poll.status === "ok"}
          onClose={closePane}
          onApprove={() => void submit(openRow, "approve")}
          onDeny={(reason) => void submit(openRow, "deny", reason)}
        />
      )}
    </div>
  );
}

/**
 * §8's pending table. Every column sortable except Status, which is derived and
 * whose five words carry no order a reader would agree on.
 *
 * The columns close over `now` so the Due countdown ticks; they are memoised on
 * it by the screen, so the identity churn is one object per second and not one
 * per render.
 */
function buildColumns(
  now: number,
  decisions: Record<string, DecisionState>,
  nameOf: (id: string) => string | undefined,
): ReadonlyArray<Column<PendingApprovalView>> {
  return [
    {
      key: "action",
      header: "Action",
      mono: true,
      cell: (row) => row.action,
      sortValue: (row) => row.action,
      searchText: (row) => `${row.action} ${row.request_id}`,
    },
    {
      key: "tier",
      header: "Tier",
      numeric: true,
      width: "72px",
      cell: (row) => row.tier,
      sortValue: (row) => row.tier,
      firstSort: "desc",
      searchText: (row) => `tier ${row.tier}`,
    },
    {
      key: "requester",
      header: "Requested by",
      cell: (row) => (
        <PrincipalLabel
          id={row.principal_id}
          reference={{ scope: "approvals", reference: row.request_id }}
        />
      ),
      sortValue: (row) => row.principal_id,
      searchText: (row) => `${row.principal_id} ${nameOf(row.principal_id) ?? ""}`,
    },
    {
      key: "requested",
      header: "Requested",
      numeric: true,
      cell: (row) => (
        <time dateTime={row.requested_at} title={absoluteTime(row.requested_at)}>
          {relativeTime(row.requested_at, now)}
        </time>
      ),
      sortValue: (row) => Date.parse(row.requested_at),
      firstSort: "desc",
    },
    {
      key: "due",
      header: "Due",
      numeric: true,
      cell: (row) => (
        <time
          dateTime={row.sla_deadline}
          title={absoluteTime(row.sla_deadline)}
          className={isOverdue(row, now) ? "ne-approvals__due--overdue" : undefined}
        >
          {dueCell(row.sla_deadline, now)}
        </time>
      ),
      sortValue: (row) => deadlineMs(row),
    },
    {
      key: "expires",
      header: "Expires",
      numeric: true,
      cell: (row) => (
        <time dateTime={row.expires_at} title={absoluteTime(row.expires_at)}>
          {timeOrDate(row.expires_at, now)}
        </time>
      ),
      sortValue: (row) => Date.parse(row.expires_at),
    },
    {
      key: "status",
      header: "Status",
      width: "148px",
      cell: (row) => (
        <StatusPill
          word={statusWord(
            row,
            now,
            decisions[row.request_id]?.result?.status === "pending_second",
          )}
        />
      ),
    },
  ];
}

interface DecidedRow {
  key: string;
  action: string;
  tier: number | null;
  verdict: "APPROVED" | "DENIED" | "AWAITING SECOND" | "EXPIRED" | null;
  approver: string | null;
  decidedAt: string | null;
  seq: number | null;
  eventId: string | null;
}

/**
 * §8's Decided section: this sitting's decisions, plus — for a key that can read
 * the chain — the approval decisions in the recent chain window. For a key that
 * cannot, the chain rows are simply absent; there is no paragraph about the role
 * gap, because the receipts above ARE the record this key holds.
 *
 * Collapsed by default when empty, and the header carries its own count, so a
 * section with nothing in it costs one line rather than a screenful.
 */
function DecidedSection({
  decidedRows,
  decisions,
  events,
  chainReadable,
}: {
  decidedRows: readonly PendingApprovalView[];
  decisions: Record<string, DecisionState>;
  events: readonly AuditRecordView[];
  chainReadable: boolean;
}) {
  const [open, setOpen] = useState<boolean | null>(null);

  const sessionRows = useMemo<DecidedRow[]>(
    () =>
      decidedRows.flatMap((row) => {
        const result = decisions[row.request_id]?.result;
        if (result === undefined) return [];
        return [
          {
            key: row.request_id,
            action: row.action,
            tier: row.tier,
            verdict: verdictWord(result.status),
            approver: result.approver ?? null,
            decidedAt: result.decided_at ?? null,
            seq: chainReadable ? seqForRequest(events, row.request_id) : null,
            eventId: events.find((event) => namesRequest(event, row.request_id))?.event_id ?? null,
          },
        ];
      }),
    [decidedRows, decisions, events, chainReadable],
  );

  const chainRows = useMemo<DecidedRow[]>(() => {
    if (!chainReadable) return [];
    // A verdict this sitting already holds as a receipt row is claimed by its
    // REQUEST ID, not by a resolved seq: `seqForRequest` finds the newest
    // record naming the id — often the execution or a payload read, not the
    // verdict — so seq-claiming let the same decision list twice.
    const sessionIds = decidedRows.map((row) => row.request_id);
    return chainDecisions(events)
      .filter((event) => !sessionIds.some((id) => namesRequest(event, id)))
      .map((event) => ({
        key: `seq-${event.seq}`,
        // The acted-on action, as the session rows name theirs. The gate's
        // verdict records carry it as `resource: "tool:{action}"`; the audit
        // action name (`approval.granted`) is the verdict, not the action.
        action: event.resource?.startsWith("tool:")
          ? event.resource.slice("tool:".length)
          : (event.action ?? ABSENT),
        tier: null,
        verdict: decisionWord(event.action),
        approver: event.actor,
        decidedAt: event.occurred_at,
        seq: event.seq,
        eventId: event.event_id,
      }));
  }, [chainReadable, events, decidedRows]);

  const rows = useMemo(() => [...sessionRows, ...chainRows], [sessionRows, chainRows]);
  const isOpen = open ?? rows.length > 0;

  const columns = useMemo<ReadonlyArray<Column<DecidedRow>>>(
    () => [
      {
        key: "action",
        header: "Action",
        mono: true,
        cell: (row) => row.action,
        sortValue: (row) => row.action,
      },
      {
        key: "tier",
        header: "Tier",
        numeric: true,
        width: "72px",
        cell: (row) => row.tier,
        sortValue: (row) => row.tier,
      },
      {
        key: "decision",
        header: "Decision",
        width: "148px",
        cell: (row) => (row.verdict === null ? null : <StatusPill word={row.verdict} />),
        sortValue: (row) => row.verdict,
      },
      {
        key: "approver",
        header: "Approver",
        cell: (row) =>
          row.approver === null ? null : (
            <PrincipalLabel
              id={row.approver}
              reference={
                row.eventId === null ? undefined : { scope: "audit", reference: row.eventId }
              }
            />
          ),
        sortValue: (row) => row.approver,
      },
      {
        key: "decided",
        header: "Decided",
        numeric: true,
        cell: (row) =>
          row.decidedAt === null ? null : (
            <time dateTime={row.decidedAt} title={absoluteTime(row.decidedAt)}>
              {relativeTime(row.decidedAt)}
            </time>
          ),
        sortValue: (row) => (row.decidedAt === null ? null : Date.parse(row.decidedAt)),
        firstSort: "desc",
      },
      {
        key: "seq",
        header: "Audit entry",
        numeric: true,
        width: "120px",
        cell: (row) =>
          row.seq === null ? null : <Link to={`/audit?seq=${row.seq}`}>{row.seq}</Link>,
        sortValue: (row) => row.seq,
      },
    ],
    [],
  );

  return (
    <details
      className="ne-approvals__decided"
      open={isOpen}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="ne-approvals__decidedhead">
        Decided <span className="ne-approvals__decidedcount">{rows.length}</span>
      </summary>
      {rows.length === 0 ? null : (
        <DataTable<DecidedRow>
          columns={columns}
          rows={rows}
          rowKey={(row) => row.key}
          label="Decided approvals"
          things="decisions"
          defaultSort={{ key: "decided", dir: "desc" }}
        />
      )}
    </details>
  );
}
