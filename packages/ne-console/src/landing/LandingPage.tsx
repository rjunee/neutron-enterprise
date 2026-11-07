// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §4 — HOME. Every signed-in user, at the start of every session, asking one
// question: what needs me, and did anything change?
//
// WHAT THIS REPLACES. The route measured, on the deployed console, at zero
// tables, zero rows and zero inputs: a headline, a band of placards, a
// browser-local journal of "what you asked", and a deployment-facts strip. None
// of it was work. A home screen that announces a number and sends you elsewhere
// has spent the visit; this one lists the actual items, sorted by what is
// closest to breaching, and each row opens the thing it names.
//
// THREE ANSWERS, IN THIS ORDER:
//   1  the tiles       — four standing facts, each a link, each omitted or
//                        dashed rather than guessed at;
//   2  Waiting on you  — the merged attention list, urgency first, eight rows;
//   3  Recent activity — the last ten decisions on the chain, for ops keys.
//
// NO DECIDE-IN-PLACE. A row links to Approvals; it carries no approve button.
// There is exactly ONE decision surface in this product, with its confirm step,
// its reason capture and its receipt, and duplicating it here re-creates the
// two-surfaces-disagreeing class of defect this rebuild exists to end.
//
// DELETED HERE, NOT MOVED: the browser-local "recent work" journal and its
// disclaimers, the "since your last visit" localStorage diff, the closed-lane
// explanation cards, the inspect drawer, the deployment-facts band, and the
// first-run tour as page content. Session history returns when the SERVER keeps
// it; a private note in one browser was never the answer to "what did I ask".

import { useCallback, useContext, useMemo } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router";
import { RuntimeConfigContext } from "../AppBootstrap";
import type { AuditRecordView } from "../api/types";
import { useNow } from "../approvals/useNow";
import { eventActionCode, eventActionLabel } from "../audit/eventClass";
import { entitlementsOf } from "../auth/entitlements";
import { usePrincipal } from "../auth/PrincipalContext";
import { useApiClient } from "../auth/useApiClient";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { usePendingApprovalsRead } from "../hooks/usePendingApprovals";
import { usePosture } from "../posture/PostureContext";
import { DataTable } from "../primitives/DataTable";
import { PrincipalLabel } from "../primitives/PrincipalLabel";
import type { Column, TableStatus } from "../primitives/DataTable";
import { ListScreen } from "../primitives/PageHeader";
import { StatePill } from "../primitives/StatePill";
import { Empty, LoadError } from "../primitives/states";
import { absoluteTime, formatCount, formatDeadline, relativeTime } from "../primitives/format";
import {
  HOME_QUEUE_LIMIT,
  mergeQueue,
  overflowCounts,
  queueStatusWord,
  statusRank,
} from "./homeQueue";
import type { QueueRow } from "./homeQueue";
import { recentActivity } from "./homeActivity";
import { approvalsTile, auditTile, controlsTile, sourcesTile } from "./homeTiles";
import type { TileState } from "./homeTiles";
import { useGovernedRead } from "./useGovernedRead";
import "./landing.css";

// ===========================================================================
// The tile
// ===========================================================================

// A standing fact: one served value, a link to the screen that owns it, demoted
// from the page's lead to the margin column. Same three rules the tile always
// had — refused omits, errored dashes, loading is a skeleton — in the recomposed
// `ne-factcard` geometry. The label carries a `›` affordance from CSS, not markup.
function FactCard({ label, to, state }: { label: string; to: string; state: TileState }) {
  if (state.kind === "hidden") return null;
  // `ne-tile` is retained as an inert query hook (no CSS): a cross-screen test
  // (models/ModelsPage.airgap.test.tsx) navigates here and reads the Controls
  // fact by `.ne-tile[href="/posture"]`, and §13.2 keeps that hook rather than
  // reaching into an out-of-scope file. `ne-factcard` carries all the styling.
  const className = `ne-tile ne-factcard${state.kind === "error" ? " ne-factcard--error" : ""}`;
  return (
    <Link className={className} to={to}>
      <span className="ne-factcard__label">{label}</span>
      {state.kind === "loading" ? (
        <>
          <span className="ne-factcard__value">
            <span className="ne-skeleton ne-factcard__shimmer" />
          </span>
          <span className="ne-visually-hidden" role="status">
            Loading {label.toLowerCase()}
          </span>
        </>
      ) : state.kind === "error" ? (
        <>
          <span className="ne-factcard__value ne-absent">—</span>
          <span className="ne-factcard__sub">Couldn&rsquo;t load</span>
        </>
      ) : (
        <>
          <span className="ne-factcard__value">{state.value}</span>
          {state.sub === undefined ? null : <span className="ne-factcard__sub">{state.sub}</span>}
        </>
      )}
    </Link>
  );
}

/** A card: the recomposed section wrapper. The heading is §4's, exactly; the head
 * carries EITHER a served count summary (the queue) OR a trailing link (activity),
 * never both, and never a count computed from anything but the rows on screen. */
function Card({
  heading,
  count,
  link,
  children,
}: {
  heading: string;
  count?: string;
  link?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="ne-card" aria-label={heading}>
      <div className="ne-cardhead">
        <h2 className="ne-cardhead__title">{heading}</h2>
        {/* A plain span, not an <output>: the count is summary text, and a live
            region here would announce every 30s poll and collide with the table's
            own loading status. */}
        {count === undefined ? null : <span className="ne-cardhead__count">{count}</span>}
        {link === undefined ? null : (
          <>
            <span className="ne-cardhead__spacer" />
            {link}
          </>
        )}
      </div>
      {children}
    </section>
  );
}

// ===========================================================================
// Waiting on you
// ===========================================================================

function ItemCell({ row }: { row: QueueRow }) {
  // An action name is an identifier and sets in mono; a proposal's title is
  // prose the server wrote and sets as prose.
  return row.kind === "approval" ? <span className="ne-mono">{row.item}</span> : <>{row.item}</>;
}

/** Relative in the cell, absolute in the title — §0's time grammar, in a
 * <time> element so the machine-readable instant travels with it. */
function Instant({ iso, text }: { iso: string; text: string }) {
  return (
    <time dateTime={iso} title={absoluteTime(iso)}>
      {text}
    </time>
  );
}

function useQueueColumns(now: number): Column<QueueRow>[] {
  return useMemo(
    () => [
      {
        key: "type",
        header: "Type",
        width: "104px",
        cell: (row) => row.typeLabel,
        sortValue: (row) => row.typeLabel,
        searchText: (row) => row.typeLabel,
      },
      {
        key: "item",
        header: "Item",
        cell: (row) => <ItemCell row={row} />,
        sortValue: (row) => row.item,
        searchText: (row) => row.item,
      },
      {
        key: "from",
        header: "From",
        width: "200px",
        cell: (row) =>
          row.kind === "approval" ? (
            <PrincipalLabel id={row.from} reference={{ scope: "approvals", reference: row.key }} />
          ) : (
            row.from
          ),
        sortValue: (row) => row.from,
        searchText: (row) => row.from,
      },
      {
        key: "age",
        header: "Age",
        width: "104px",
        numeric: true,
        firstSort: "asc",
        cell: (row) => <Instant iso={row.at} text={relativeTime(row.at, now)} />,
        sortValue: (row) => Date.parse(row.at),
      },
      {
        key: "due",
        header: "Due",
        width: "104px",
        numeric: true,
        firstSort: "asc",
        // A proposal has no deadline: the cell returns null and the table renders
        // the em dash. Nothing here can invent a countdown. A breached deadline
        // wears the amber the render marks it with — the styling only; the text is
        // whatever `formatDeadline` (the shared, served-time formatter) produces.
        cell: (row) =>
          row.due === null ? null : (
            <span
              className={queueStatusWord(row, now) === "OVERDUE" ? "ne-due--overdue" : "ne-due"}
            >
              <Instant iso={row.due} text={formatDeadline(row.due, now)} />
            </span>
          ),
        sortValue: (row) => (row.due === null ? null : Date.parse(row.due)),
      },
      {
        key: "status",
        header: "Status",
        width: "148px",
        cell: (row) => <StatePill word={queueStatusWord(row, now)} />,
        sortValue: (row) => statusRank(row, now),
      },
    ],
    [now],
  );
}

// ===========================================================================
// Recent activity
// ===========================================================================

function useActivityColumns(now: number): Column<AuditRecordView>[] {
  // NO SORTABLE COLUMN HERE, deliberately and for the reason §4 states: this is
  // a ten-row teaser of a screen that sorts, filters and exports properly, and
  // the real ledger is one click away. Sorting ten of several thousand rows
  // answers a question about ten rows while looking like an answer about the
  // chain.
  return useMemo(
    () => [
      {
        key: "seq",
        header: "Seq",
        width: "88px",
        numeric: true,
        mono: true,
        cell: (event) => formatCount(event.seq),
      },
      {
        key: "time",
        header: "Time",
        width: "116px",
        numeric: true,
        cell: (event) => (
          <Instant iso={event.occurred_at} text={relativeTime(event.occurred_at, now)} />
        ),
      },
      {
        key: "actor",
        header: "Actor",
        width: "220px",
        cell: (event) => (
          <PrincipalLabel
            id={event.actor}
            reference={{ scope: "audit", reference: event.event_id }}
          />
        ),
      },
      {
        key: "action",
        header: "Action",
        // NOT `mono` any more, and not the raw code. Mono is for identifiers,
        // and this cell stopped being one: Home is the first screen every user
        // sees, and it was printing `api.audit.unauthorized` at them. The code
        // is still exactly one hover away, and the audit log's record pane
        // shows it outright — see eventActionLabel for the three tiers.
        cell: (event) => <span title={eventActionCode(event)}>{eventActionLabel(event)}</span>,
      },
      {
        key: "outcome",
        header: "Outcome",
        width: "120px",
        cell: (event) => event.outcome,
      },
    ],
    [now],
  );
}

// ===========================================================================
// The screen
// ===========================================================================

export function LandingPage() {
  const now = useNow(30_000);
  const client = useApiClient();
  const navigate = useNavigate();
  // A NULLABLE read of the runtime config: this page renders inside the
  // bootstrapped shell, but it must not be the thing that throws if it ever does
  // not. Absent a configured name, §1.2's fallback is the serving host alone —
  // and no instruction to go and configure one.
  const config = useContext(RuntimeConfigContext);
  const deploymentName =
    config?.deployment_name ?? (typeof window === "undefined" ? "" : window.location.host);

  const entitlements = entitlementsOf(usePrincipal());
  // Home and the rail badge project the SAME provider payload. Home must never
  // open a second one-shot reader whose stale rows disagree with the live badge.
  const approvals = usePendingApprovalsRead();
  const readProposals = useCallback(() => client.listStewardProposals(), [client]);
  const proposals = useGovernedRead(
    readProposals,
    entitlements.refused("steward"),
    readProposals,
    entitlements.pending,
  );
  const sources = useGovernedRead(useCallback(() => client.connectStatus(), [client]));
  const poll = useAuditPoll();
  const posture = usePosture();

  // ---- Waiting on you ----------------------------------------------------
  const rows = useMemo(
    () => mergeQueue(approvals.data?.approvals ?? [], proposals.data?.proposals ?? []),
    [approvals.data, proposals.data],
  );
  const shown = rows.slice(0, HOME_QUEUE_LIMIT);
  const overflow = overflowCounts(rows);
  const queueColumns = useQueueColumns(now);

  // The card head's count line — computed ONLY from the rows drawn on screen.
  // "N item(s)", and overdue/escalated only when non-zero: a "0 overdue" would
  // advertise an absence the reader can do nothing with, and this line never
  // names a total the page did not draw.
  const overdueCount = shown.filter((row) => queueStatusWord(row, now) === "OVERDUE").length;
  const escalatedCount = shown.filter((row) => queueStatusWord(row, now) === "ESCALATED").length;
  const queueCount =
    `${formatCount(shown.length)} item${shown.length === 1 ? "" : "s"}` +
    (overdueCount > 0 ? ` · ${formatCount(overdueCount)} overdue` : "") +
    (escalatedCount > 0 ? ` · ${formatCount(escalatedCount)} escalated` : "");

  // The queues this key's server-granted surfaces feed. A refused queue is not a
  // lane of this table at all: it contributes no rows, no zero and no placard.
  const granted = [approvals, proposals].filter((lane) => lane.status !== "refused");
  const anyLoaded = granted.some((lane) => lane.status === "ok");
  const anyLoading = granted.some((lane) => lane.status === "loading");
  const anyError = granted.some((lane) => lane.status === "error");
  // An empty response from one lane cannot turn an unknown sibling lane into a
  // terminal empty claim. Keep the table loading until every still-granted
  // empty lane has answered. If a lane already served real rows, retain those
  // rows and state explicitly that the combined queue is only partial.
  const partialLoading = anyLoaded && anyLoading && rows.length > 0;
  const queueStatus: TableStatus =
    rows.length === 0
      ? anyLoading
        ? "loading"
        : anyError
          ? "error"
          : anyLoaded
            ? "ok"
            : "error"
      : anyLoaded
        ? "ok"
        : "error";
  // One lane answered and the other failed: the rows that DID arrive are shown,
  // and the failure is stated under them rather than hidden behind them. The
  // opposite trade — an error state that discards the rows already in hand —
  // turns a partial answer into no answer.
  const partialFailure = rows.length > 0 && anyLoaded && anyError;

  const reloadApprovals = approvals.reload;
  const reloadProposals = proposals.reload;
  const retryQueues = useCallback(() => {
    reloadApprovals();
    reloadProposals();
  }, [reloadApprovals, reloadProposals]);

  const queueNote: ReactNode =
    overflow.approvals === 0 &&
    overflow.reviews === 0 &&
    !partialFailure &&
    !partialLoading ? null : (
      <>
        {overflow.approvals > 0 ? (
          <Link to="/approvals">{formatCount(overflow.approvals)} more in Approvals</Link>
        ) : null}
        {overflow.reviews > 0 ? (
          <Link to="/improvement">{formatCount(overflow.reviews)} more in Improvement</Link>
        ) : null}
        {partialLoading ? <span role="status">Loading the rest of your queue…</span> : null}
        {partialFailure ? <LoadError things="your queue" onRetry={retryQueues} /> : null}
      </>
    );

  // ---- Recent activity ---------------------------------------------------
  const events = useMemo(() => recentActivity(poll.events), [poll.events]);
  const activityColumns = useActivityColumns(now);
  const activityStatus: TableStatus =
    poll.status === "loading" ? "loading" : poll.status === "error" ? "error" : "ok";

  return (
    <ListScreen title={deploymentName}>
      <div className="ne-home">
        {/* THE QUEUE LEADS. The recomposition's whole argument: the first content
            on the page is the work waiting on this person, and the facts that used
            to open it are demoted to the margin column beside it. */}
        <div className="ne-home__main">
          {/* §4: with NO queue granted to this key the section is omitted
              entirely — not emptied, not locked, not explained. */}
          {granted.length === 0 ? null : (
            <Card heading="Waiting on you" count={shown.length > 0 ? queueCount : undefined}>
              <DataTable
                columns={queueColumns}
                rows={shown}
                rowKey={(row) => row.key}
                label="Waiting on you"
                things="your queue"
                status={queueStatus}
                onRowActivate={(row) => void navigate(row.to)}
                // The breached row wears the amber inset edge the render marks it
                // with. OVERDUE only — an escalated-but-not-breached row keeps its
                // warn pill without the edge, exactly as the render draws it.
                rowModifier={(row) =>
                  queueStatusWord(row, now) === "OVERDUE" ? "ne-tr--urgent" : undefined
                }
                empty={<Empty message="Nothing is waiting on you." />}
                error={<LoadError things="your queue" onRetry={retryQueues} />}
                note={queueNote}
              />
            </Card>
          )}

          {/* §4: ops keys only. The shared chain read IS the ops-gated one, and a
              refusal of it is the server's own answer — so a key without it sees
              no section at all. The one exit ("Open audit log") sits in the head. */}
          {poll.status === "degraded" ? null : (
            <Card heading="Recent activity" link={<Link to="/audit">Open audit log</Link>}>
              <DataTable
                columns={activityColumns}
                rows={events}
                rowKey={(event) => event.event_id}
                label="Recent activity"
                things="recent activity"
                status={activityStatus}
                empty={<Empty message="No recent activity." />}
                // The read belongs to the shell's shared chain poll, which retries
                // itself on a backoff; Home cannot re-trigger it without also
                // running the chain re-verification sweep, which has its own
                // meaning. So the next action offered is the ledger itself, which
                // owns the read and its Retry.
                error={
                  <p className="ne-state__line" role="alert">
                    Couldn&rsquo;t load recent activity.
                  </p>
                }
              />
            </Card>
          )}
        </div>

        {/* 3 · STANDING FACTS. Four served facts, each a link, demoted from the
            lead to a 288px margin column: glanceable, never the first thing read.
            A refused fact contributes nothing — no lock, no zero. */}
        <aside className="ne-facts" aria-label="Standing facts">
          <FactCard label="Approvals" to="/approvals" state={approvalsTile(approvals, now)} />
          <FactCard
            label="Audit log"
            to="/audit"
            // The LATCH, not this poll's raw read — the rail chip in the TopBar
            // above (App.tsx) resolves its word through the same `chainReading`,
            // and the two are in one viewport, so the inputs have to be the same
            // ones. See `auditTile` for the two contradictions this closes.
            state={auditTile(poll.status, poll.verdict, poll.verification, poll.observedBreaks)}
          />
          <FactCard
            label="Controls"
            to="/posture"
            state={controlsTile(posture.status, posture.posture)}
          />
          <FactCard label="Sources" to="/sources" state={sourcesTile(sources)} />
        </aside>
      </div>
    </ListScreen>
  );
}
