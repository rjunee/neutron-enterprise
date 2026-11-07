// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §6 — SEARCH. The analyst who wants the record, not a synthesis.
//
// The job: run a retrieval, scan ranked results, open the underlying chunk, and
// — in a demo or an access review — run the same query as a second identity to
// see what that identity was and was not sent.
//
// What shipped here was one text box over a list of cards: no result table, no
// count, no sort, no filter, no export, and the comparison beat buried behind a
// button that could fail silently. It is replaced by §1.4's list screen: a title
// row, a query row, a toolbar, a real table with four sortable columns, and a
// detail pane.
//
// DEFAULT SORT IS RELEVANCE — score descending, which is the server's own
// ranking. The # column is deliberately NOT sortable: it is the identity of that
// ranking, and a row keeps the rank the server gave it however the table is
// ordered.
//
// THERE IS DELIBERATELY NO RECENCY SORT (§6.2). A chunk's `metadata` is an opaque
// map with no guaranteed timestamp; a date column over a field that may not exist
// would silently misorder the one screen whose whole value is the order.
//
// THIN CLIENT (C7). The result set is whatever the governed search returns for
// this bearer. This screen sorts and hides rows the server already sent; it never
// re-ranks, never re-scores, and holds no ACL. In the comparison view the two
// columns are two independently server-trimmed responses — the client never
// diffs privileged content, because it never receives any.

import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router";
import { ApiError } from "../api/client";
import {
  acquireCompareKey,
  createCompareClient,
  popupsBlocked,
  type CompareKey,
} from "../api/compareAuth";
import { useAuth } from "react-oidc-context";
import { RuntimeConfigContext } from "../AppBootstrap";
import type { AuthorizedRetrievalEnvelopeWire, RetrievalGraphPathWire } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { sourceRefResolvesToEntity } from "../chat/citationEntity";
import { SOURCE_VIEW_IN_GRAPH } from "../chat/askCopy";
import { graphCitationsHref, graphEntityHref } from "../knowledge/graph/graphLinks";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { ListScreen } from "../primitives/PageHeader";
import { StatePill } from "../primitives/StatePill";
import { Empty, Refused } from "../primitives/states";
import {
  FilterSelect,
  PrimaryButton,
  ToggleChip,
  Toolbar,
  ToolbarButton,
} from "../primitives/Toolbar";
import { buildCsv, downloadCsv } from "../primitives/csv";
import { ABSENT, absoluteTime, formatCount, relativeTime } from "../primitives/format";
import type { SortState } from "../primitives/tableSort";
import { filterRows, sortRows } from "../primitives/tableSort";
import {
  applySourceFilter,
  authorityLimitBounds,
  AUTHORITY_LIMITED_LINE,
  buildRows,
  COMPLETENESS_UNSTATED_LINE,
  markTerms,
  relatedEntities,
  reportsOrdinaryNoResults,
  retrievalCompleteness,
  searchCountText,
  sourceSystems,
  truncateMiddle,
  unionRows,
  withoutComparison,
  type RelatedEntity,
  type RetrievalCompleteness,
  type ResultRow,
} from "./results";
import type { ResultIdentity } from "./resultRecord";
import { MetadataValueOf } from "./MetadataFields";
import {
  accessContextKey,
  evictSearchHistoryFor,
  recordSearch,
  searchHistoryFor,
  type SearchHistoryEntry,
} from "./searchHistory";
import "./search.css";

/**
 * The client's give-up time for a run, in milliseconds — the DEFAULT only. A
 * deployment sets `search_deadline_ms` in the configuration it already serves,
 * because how long its retriever takes is a fact about that deployment and not
 * about this console. A number compiled into the bundle that disagrees with the
 * deployment it points at turns the one loop that works into a coin flip.
 */
const SEARCH_DEADLINE_DEFAULT_MS = 180_000;

/** A comparison run issues TWO governed searches. Run together they still
 * contend for one retriever, so the budget is widened rather than shared. */
const COMPARE_DEADLINE_MULTIPLIER = 2;

/** How long the second identity's sign-in window may sit unfinished before the
 * screen stops pretending something is happening. */
const SIGN_IN_DEADLINE_MS = 120_000;

/** §6.1's results-size select. Maps to the search request's result count. */
const SIZES = [
  { value: "10", label: "10" },
  { value: "25", label: "25" },
  { value: "50", label: "50" },
] as const;
type Size = (typeof SIZES)[number]["value"];

/** A stored size back to the select's domain; an unrecognised one is the
 * default, never a value the select cannot show. */
function parseSize(value: string): Size {
  return SIZES.find((s) => s.value === value)?.value ?? "10";
}

/** §6.3: "Default sort: relevance (score desc, the server's own ranking)." */
const RELEVANCE: SortState = { key: "score", dir: "desc" };
/** §6.2's other named sort. */
const BY_SOURCE: SortState = { key: "source", dir: "asc" };

type Phase = "idle" | "loading" | "done" | "error" | "timeout" | "cancelled" | "refused";

/** One completed run. Held whole so the toolbar's count, the table and the pane
 * all describe the SAME run and cannot drift apart mid-render. */
interface Run {
  query: string;
  rows: readonly ResultRow[];
  /** The other identity's label when this run was a comparison; else null. */
  compared: string | null;
  /** A second-key 403 is THEIR role boundary, not the reader's. In that case
   * primary rows survive and no comparison column is fabricated. */
  comparisonRefusal: { identity: string; detail: string | null } | null;
  /** Measured wall-clock for the run. */
  elapsedMs: number;
  /** `provisional_source_count` off the response. */
  provisionalCount: number;
  /** The other response's own count. Null outside a completed comparison. */
  comparedProvisionalCount: number | null;
  /** Entities the response's `graph_paths` traversed. */
  entities: readonly RelatedEntity[];
  /** The served traversal itself — the pane's "View in Graph" link is gated
   * on a hop actually evidencing the row's source_ref (the same rule Ask
   * applies), so it must survive into replayed runs too. */
  graphPaths: readonly RetrievalGraphPathWire[];
  /** 015A0-ENFORCEMENT: the server's own `complete|partial|exhausted` status
   * and scan bounds for THIS identity's response, held exactly as sent.
   * `undefined` is a server that stated none — never a stand-in for
   * `complete`. */
  retrieval: AuthorizedRetrievalEnvelopeWire | undefined;
  /** The comparison identity's INDEPENDENT status. Two identities scan under
   * two authorities, so one status may never be reported for both. */
  comparedRetrieval: AuthorizedRetrievalEnvelopeWire | undefined;
}

const NO_ROWS: readonly ResultRow[] = [];

/** A live seconds counter for a run in flight. `startedAt` is stamped in the
 * user event that issued the request, never during a render. */
function useElapsedSeconds(startedAt: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null) return;
    // The clock is READ in the timer callback and held in state — never called
    // during a render, and never written synchronously from the effect body.
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [startedAt]);
  if (startedAt === null) return 0;
  return Math.max(0, Math.floor((now - startedAt) / 1000));
}

export function SearchPage() {
  const client = useApiClient();
  const config = useContext(RuntimeConfigContext);
  const auth = useAuth();
  /** The OIDC subject the bearer token carries — the identity every governed
   * read in this tab runs as, known synchronously. The history store binds to
   * it (searchHistory.ts rule 1); while unknown, nothing is recorded and
   * nothing is shown — fail closed on identity. */
  const subject = auth.user?.profile.sub;
  const principalId = typeof subject === "string" && subject !== "" ? subject : null;

  const [query, setQuery] = useState("");
  const [size, setSize] = useState<Size>("10");
  const [phase, setPhase] = useState<Phase>("idle");
  const [run, setRun] = useState<Run | null>(null);
  /** Non-null exactly when the run on screen is a REPLAYED capture rather than
   * a fresh server answer — the value is the capture instant the banner names. */
  const [cachedAt, setCachedAt] = useState<string | null>(null);
  const [captureInvalidated, setCaptureInvalidated] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);

  const [within, setWithin] = useState("");
  const [systems, setSystems] = useState<readonly string[]>([]);
  const [sort, setSort] = useState<SortState>(RELEVANCE);
  const [openKey, setOpenKey] = useState<string | null>(null);

  const [compareKey, setCompareKey] = useState<CompareKey | null>(null);
  const [signingIn, setSigningIn] = useState(false);
  /** §6.5: the sign-in did not finish — a blocked window, a closed one, or an
   * identity provider that never answered. One state, one sentence, one retry. */
  const [signInFailed, setSignInFailed] = useState(false);

  const inFlight = useRef<AbortController | null>(null);
  /** Invalidates late search, identity-check, and replay-validation answers. */
  const requestSeq = useRef(0);
  const elapsed = useElapsedSeconds(phase === "loading" ? startedAt : null);

  // Nothing governed is left in flight when this screen unmounts.
  useEffect(
    () => () => {
      requestSeq.current += 1;
      inFlight.current?.abort();
    },
    [],
  );

  const deadlineMs = config?.search_deadline_ms ?? SEARCH_DEADLINE_DEFAULT_MS;

  const cancel = useCallback(() => {
    requestSeq.current += 1;
    inFlight.current?.abort();
    inFlight.current = null;
    setRun(null);
    setStartedAt(null);
    setPhase("cancelled");
  }, []);

  /**
   * One submit = exactly one governed request per identity.
   *
   * The comparison's two requests are independent and neither depends on the
   * other's answer, so they run CONCURRENTLY under one deadline and one Cancel
   * rather than stacking two waits end to end.
   */
  const runQuery = useCallback(
    async (
      raw: string,
      key: CompareKey | null,
      sizeChoice?: Size,
      invalidatedOnSuccess?: boolean,
    ) => {
      setCaptureInvalidated(false);
      requestSeq.current += 1;
      const seq = requestSeq.current;
      const q = raw.trim();
      if (q === "") return;
      const topK = sizeChoice ?? size;
      inFlight.current?.abort();
      const controller = new AbortController();
      inFlight.current = controller;
      const budget = key === null ? deadlineMs : deadlineMs * COMPARE_DEADLINE_MULTIPLIER;
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, budget);
      const began = Date.now();
      setPhase("loading");
      setStartedAt(began);
      setOpenKey(null);
      // Whatever this run ends as, it is a LIVE answer: the cached banner must
      // never sit over rows the server just served.
      setCachedAt(null);
      // Start the context read in the same request window, but never let a
      // slow /v1/me delay or time out a successful search. If it cannot stamp
      // the capture, the live result still renders and simply is not cached.
      const accessKeyPromise =
        key === null && principalId !== null
          ? client
              .me()
              .then((me) => accessContextKey(me.id, me.groups))
              .catch(() => null)
          : Promise.resolve(null);
      try {
        const second = key === null ? null : createCompareClient(key, () => setCompareKey(null));
        const [mineOutcome, theirsOutcome] = await Promise.allSettled([
          client.search(q, Number(topK), controller.signal),
          second === null
            ? Promise.resolve(null)
            : second.search(q, Number(topK), controller.signal),
        ]);
        if (requestSeq.current !== seq) return;
        if (mineOutcome.status === "rejected") throw mineOutcome.reason;
        const mine = mineOutcome.value;
        const mineRows = buildRows(mine.chunks, mine.citations);
        const elapsedMs = Date.now() - began;
        if (theirsOutcome.status === "rejected") {
          const refusal = theirsOutcome.reason;
          if (key !== null && refusal instanceof ApiError && refusal.status === 403) {
            setRun({
              query: q,
              rows: mineRows,
              compared: null,
              comparisonRefusal: {
                identity: key.label,
                detail: refusal.detail ?? null,
              },
              elapsedMs,
              provisionalCount: mine.provisional_source_count,
              comparedProvisionalCount: null,
              entities: relatedEntities(mine.graph_paths),
              graphPaths: mine.graph_paths,
              retrieval: mine.authorized_retrieval,
              comparedRetrieval: undefined,
            });
            setSystems([]);
            setWithin("");
            setSort(RELEVANCE);
            setPhase("done");
            return;
          }
          throw refusal;
        }
        const theirs = theirsOutcome.value;
        setRun({
          query: q,
          rows:
            theirs === null
              ? mineRows
              : unionRows(mineRows, buildRows(theirs.chunks, theirs.citations)),
          compared: theirs === null ? null : (key?.label ?? null),
          comparisonRefusal: null,
          elapsedMs,
          provisionalCount: mine.provisional_source_count,
          comparedProvisionalCount: theirs?.provisional_source_count ?? null,
          entities: relatedEntities(mine.graph_paths),
          graphPaths: mine.graph_paths,
          retrieval: mine.authorized_retrieval,
          comparedRetrieval: theirs?.authorized_retrieval,
        });
        // Single-identity runs only, and only under a known principal — the
        // store binds every entry to the id that ran it (searchHistory.ts).
        if (theirs === null && principalId !== null) {
          const capture = {
            query: q,
            size: topK,
            capturedAt: new Date().toISOString(),
            rows: mineRows,
            entities: relatedEntities(mine.graph_paths),
            graphPaths: mine.graph_paths,
            provisionalCount: mine.provisional_source_count,
            elapsedMs,
            retrieval: mine.authorized_retrieval,
          };
          void accessKeyPromise.then((accessKey) => {
            if (requestSeq.current === seq && accessKey !== null) {
              recordSearch(principalId, { ...capture, accessKey });
            }
          });
        }
        // A new result set is a new facet set and a new ranking: the filters and
        // the sort return to their defaults rather than silently applying the
        // last run's narrowing to rows it was never chosen for.
        setSystems([]);
        setWithin("");
        setSort(RELEVANCE);
        setCaptureInvalidated(invalidatedOnSuccess === true);
        setPhase("done");
      } catch (err) {
        if (requestSeq.current !== seq) return;
        setRun(null);
        // ORDER MATTERS. A deadline abort aborts the controller too, so "we gave
        // up" is tested before "the user gave up"; and a refusal is ONLY a
        // refusal when the server said 403 — a network failure rendered as a
        // role boundary is a false statement about authorization.
        if (timedOut) setPhase("timeout");
        else if (controller.signal.aborted) setPhase("cancelled");
        else if (err instanceof ApiError && err.status === 403) setPhase("refused");
        else setPhase("error");
      } finally {
        clearTimeout(timer);
        if (inFlight.current === controller) inFlight.current = null;
        if (requestSeq.current === seq) setStartedAt(null);
      }
    },
    [client, deadlineMs, principalId, size],
  );

  // ---- recent searches (owner request 2026-08-04) --------------------------

  const historyEntries = searchHistoryFor(principalId);

  /** Replay a capture only after current /v1/me context AND the governed
   * retrieve seam both say its cached rows remain visible. A changed or
   * unverifiable answer defaults to asking search again. */
  const openHistoryEntry = useCallback(
    async (entry: SearchHistoryEntry) => {
      requestSeq.current += 1;
      const seq = requestSeq.current;
      inFlight.current?.abort();
      inFlight.current = null;
      const restoredSize = parseSize(entry.size);
      const cachedIds = [...new Set(entry.rows.map((row) => row.chunkId))];
      setQuery(entry.query);
      setSize(restoredSize);
      setRun(null);
      setPhase("loading");
      setStartedAt(Date.now());
      setCachedAt(null);
      setCaptureInvalidated(false);
      setOpenKey(null);

      try {
        const [me, visible] = await Promise.all([
          client.me(),
          cachedIds.length === 0 ? Promise.resolve(null) : client.retrieve(cachedIds),
        ]);
        if (requestSeq.current !== seq) return;
        const freshAccessKey = accessContextKey(me.id, me.groups);
        const visibleIds = new Set(visible?.chunks.map((chunk) => chunk.chunk.chunk_id) ?? []);
        const everyRowStillVisible =
          visible === null || cachedIds.every((chunkId) => visibleIds.has(chunkId));

        if (freshAccessKey === entry.accessKey && everyRowStillVisible) {
          setRun({
            query: entry.query,
            rows: entry.rows,
            compared: null,
            comparisonRefusal: null,
            elapsedMs: entry.elapsedMs,
            provisionalCount: entry.provisionalCount,
            comparedProvisionalCount: null,
            entities: entry.entities,
            graphPaths: entry.graphPaths,
            // A capture of an authority-limited page replays as an
            // authority-limited page. Dropping the status here would let a
            // replayed `partial` render as if the server had proved it whole.
            retrieval: entry.retrieval,
            comparedRetrieval: undefined,
          });
          setSystems([]);
          setWithin("");
          setSort(RELEVANCE);
          setOpenKey(null);
          setStartedAt(null);
          setCachedAt(entry.capturedAt);
          setPhase("done");
          return;
        }

        // A verified mismatch means every old capture under this subject is
        // suspect, not just the row the user happened to click.
        if (principalId !== null) evictSearchHistoryFor(principalId);
        void runQuery(entry.query, null, restoredSize, /* invalidatedOnSuccess */ true);
      } catch {
        if (requestSeq.current !== seq) return;
        // An unavailable validator is not evidence that access changed. It is
        // still evidence that replay is unsafe, so fail to a live query without
        // claiming a policy event we did not verify.
        void runQuery(entry.query, null, restoredSize);
      }
    },
    [client, principalId, runQuery],
  );

  /** Re-run: the same question asked of the server NOW — a real request, whose
   * answer replaces the capture entirely. */
  const rerunHistoryEntry = useCallback(
    (entry: SearchHistoryEntry) => {
      setQuery(entry.query);
      setSize(parseSize(entry.size));
      void runQuery(entry.query, null, parseSize(entry.size));
    },
    [runQuery],
  );

  const submit = useCallback(
    (event: FormEvent) => {
      event.preventDefault();
      void runQuery(query, compareKey);
    },
    [compareKey, query, runQuery],
  );

  // ---- the second identity (§6.5) ------------------------------------------

  const startCompare = useCallback(async () => {
    if (config === null || popupsBlocked()) {
      setSignInFailed(true);
      return;
    }
    setSignInFailed(false);
    setSigningIn(true);
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      setSigningIn(false);
      setSignInFailed(true);
    }, SIGN_IN_DEADLINE_MS);
    try {
      const key = await acquireCompareKey(config);
      settled = true;
      setCompareKey(key);
      setSigningIn(false);
      // The query the comparison is about has already been typed and run. Re-run
      // it under both identities so the table on screen is the comparison, not a
      // single-identity result set wearing a comparison's toolbar.
      void runQuery(query, key);
    } catch {
      settled = true;
      setSigningIn(false);
      setSignInFailed(true);
    } finally {
      clearTimeout(timer);
    }
  }, [config, query, runQuery]);

  const endCompare = useCallback(async () => {
    const key = compareKey;
    setCompareKey(null);
    setSignInFailed(false);
    // Your own result set is unchanged by the comparison ending, so it stays on
    // screen: only the other identity's column goes. Nothing is refetched,
    // because nothing about your rows was in question.
    setRun((prev) =>
      prev === null
        ? prev
        : {
            ...prev,
            rows: withoutComparison(prev.rows),
            compared: null,
            comparisonRefusal: null,
            comparedProvisionalCount: null,
          },
    );
    // The comparison's own columns go with it, so a sort held on one of them
    // would be a sort on a column that no longer exists.
    setSort(RELEVANCE);
    if (key) await key.withdraw();
  }, [compareKey]);

  // ---- the rendered window -------------------------------------------------

  const compared = run?.compared ?? null;
  const comparisonRefusal = run?.comparisonRefusal ?? null;
  // 015A0-ENFORCEMENT: the server's status for each identity, independently.
  const completeness = retrievalCompleteness(run?.retrieval);
  const comparedCompleteness = retrievalCompleteness(run?.comparedRetrieval);
  const columns = useMemo(
    () => (compared === null ? resultColumns(run?.query ?? "") : compareColumns(compared)),
    [compared, run?.query],
  );
  const allRows = run?.rows ?? NO_ROWS;
  const facets = useMemo(() => sourceSystems(allRows), [allRows]);
  const filtered = useMemo(
    () => filterRows(applySourceFilter(allRows, systems), columns, within),
    [allRows, systems, columns, within],
  );
  /** Exactly the rows the table renders, in the order it renders them — so the
   * export is the window on screen and not a re-derivation of it. */
  const windowRows = useMemo(() => sortRows(filtered, columns, sort), [filtered, columns, sort]);

  const filterLabels = useMemo(() => {
    const out: string[] = [];
    if (run !== null) out.push(`Query: ${run.query}`);
    if (systems.length > 0) out.push(`Source: ${systems.join(", ")}`);
    if (within.trim() !== "") out.push(`Filter: ${within.trim()}`);
    if (compared !== null) out.push(`Compared with: ${compared}`);
    return out;
  }, [compared, run, systems, within]);

  const exportCsv = useCallback(() => {
    if (windowRows.length === 0) return;
    downloadCsv(
      "search-results.csv",
      buildCsv<ResultRow>({
        title: "Search results",
        filters: filterLabels,
        columns: csvColumns(compared),
        rows: windowRows,
      }),
    );
  }, [compared, filterLabels, windowRows]);

  const openRow = openKey === null ? null : (allRows.find((r) => r.chunkId === openKey) ?? null);

  // §6.2's sort select and the sortable headers are ONE sort state. Two controls
  // over one table that disagree is worse than either alone, so the select reads
  // the same state the headers write.
  const sortChoice =
    sort.key === BY_SOURCE.key
      ? "source"
      : sort.key === RELEVANCE.key && sort.dir === "desc"
        ? "relevance"
        : "other";
  const sortOptions = [
    { value: "relevance", label: "Relevance" },
    { value: "source", label: "Source" },
    // A header click can land on a column the select does not name (Document, or
    // Score ascending). The select then names the column that IS in force, using
    // that column's own header — never one of the two it is not.
    ...(sortChoice === "other"
      ? [{ value: "other", label: columns.find((c) => c.key === sort.key)?.header ?? "" }]
      : []),
  ];

  const tableStatus: TableStatus =
    phase === "loading" ? "loading" : phase === "error" ? "error" : "ok";

  return (
    <div className="ne-search">
      <ListScreen title="Search">
        <form className="ne-search__query" onSubmit={submit}>
          <input
            type="search"
            className="ne-search__input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search connected sources"
            aria-label="Search connected sources"
          />
          <FilterSelect<Size>
            label="Results"
            value={size}
            options={SIZES}
            onChange={(next) => setSize(next)}
          />
          <PrimaryButton label="Search" type="submit" disabled={query.trim() === ""} />
          {phase === "loading" ? (
            <>
              <span className="ne-search__inflight" role="status">
                Searching… {elapsed}s
              </span>
              <ToolbarButton label="Cancel" onClick={cancel} />
            </>
          ) : null}
        </form>

        {signInFailed ? (
          <div className="ne-state ne-state--error" role="alert">
            <p className="ne-state__line">Sign-in wasn&rsquo;t completed.</p>
            <ToolbarButton label="Try again" onClick={() => void startCompare()} />
          </div>
        ) : null}

        {phase === "idle" ? (
          <>
            <Empty message="Search documents across your connected sources." />
            <RecentSearches
              entries={historyEntries}
              onOpen={openHistoryEntry}
              onRerun={rerunHistoryEntry}
            />
          </>
        ) : null}

        {phase === "cancelled" ? <Empty message="Search cancelled." /> : null}

        {phase === "timeout" ? (
          <div className="ne-state ne-state--error" role="alert">
            <p className="ne-state__line">The search timed out.</p>
            <ToolbarButton label="Try again" onClick={() => void runQuery(query, compareKey)} />
          </div>
        ) : null}

        {phase === "refused" ? <Refused thing="search" page="Search" /> : null}

        {phase === "loading" || phase === "done" || phase === "error" ? (
          <>
            {phase === "done" ? (
              captureInvalidated ? (
                <div className="ne-search__cached" role="status">
                  Fetched fresh results — the saved rows could not all be re-validated for your
                  current access, so the capture was discarded.
                </div>
              ) : cachedAt !== null ? (
                // Two facts, named separately (the "Last sync" lesson, SPEC §12
                // item 9): these rows are a CAPTURE, and this is WHEN. Re-run is
                // the same question put to the server now.
                <div className="ne-search__cached" role="status">
                  <span className="ne-search__cachedline">
                    Cached results, captured {absoluteTime(cachedAt)}. Current access re-validated;
                    Search was not re-run.
                  </span>
                  <ToolbarButton label="Re-run" onClick={() => void runQuery(query, null, size)} />
                </div>
              ) : null
            ) : null}
            {phase === "done" && run !== null ? (
              <Toolbar
                search={{ value: within, onChange: setWithin, placeholder: "Filter results…" }}
                filters={
                  <>
                    <FilterSelect
                      label="Sort"
                      value={sortChoice}
                      options={sortOptions}
                      onChange={(next) => {
                        if (next === "relevance") setSort(RELEVANCE);
                        if (next === "source") setSort(BY_SOURCE);
                      }}
                    />
                    {facets.map((system) => (
                      <ToggleChip
                        key={system}
                        label={system}
                        pressed={systems.includes(system)}
                        onChange={(on) =>
                          setSystems((prev) =>
                            on ? [...prev, system] : prev.filter((s) => s !== system),
                          )
                        }
                      />
                    ))}
                  </>
                }
                count={searchCountText(
                  windowRows.length,
                  run.elapsedMs,
                  run.provisionalCount,
                  compared === null || run.comparedProvisionalCount === null
                    ? undefined
                    : {
                        identity: compared,
                        provisional: run.comparedProvisionalCount,
                      },
                )}
                actions={
                  <>
                    <ToolbarButton
                      label="Export CSV"
                      onClick={exportCsv}
                      disabled={windowRows.length === 0}
                    />
                    {compared === null && comparisonRefusal === null ? (
                      <ToolbarButton
                        label="Compare as another user…"
                        onClick={() => void startCompare()}
                        disabled={signingIn}
                      />
                    ) : (
                      <ToolbarButton label="End comparison" onClick={() => void endCompare()} />
                    )}
                  </>
                }
              />
            ) : null}

            {phase === "done" ? (
              <CompletenessNotice completeness={completeness} identity={null} />
            ) : null}
            {phase === "done" && compared !== null ? (
              <CompletenessNotice completeness={comparedCompleteness} identity={compared} />
            ) : null}

            {compared === null ? null : (
              <p className="ne-search__caption">
                Each column is that identity&rsquo;s own result set, filtered by the server.
              </p>
            )}

            {comparisonRefusal === null ? null : (
              <div className="ne-state ne-state--refused" role="status">
                <StatePill word="GATED" detail={comparisonRefusal.identity} />
                <p className="ne-state__line">
                  {comparisonRefusal.identity}&rsquo;s role doesn&rsquo;t include search. Their
                  result set was not returned.
                </p>
                {comparisonRefusal.detail === null ? null : (
                  <p className="ne-state__detail">{comparisonRefusal.detail}</p>
                )}
                <ToolbarButton label="Try again" onClick={() => void runQuery(query, compareKey)} />
              </div>
            )}

            <DataTable<ResultRow>
              columns={columns}
              rows={filtered}
              rowKey={(row) => row.chunkId}
              label="Search results"
              things="results"
              sort={sort}
              onSortChange={setSort}
              status={tableStatus}
              onRowActivate={(row) => setOpenKey(row.chunkId)}
              selectedKey={openKey}
              empty={
                run === null ? null : allRows.length === 0 ? (
                  // An empty page is "nothing matched" ONLY when the server's
                  // own status proves it. An authority-limited scan returned
                  // nothing it was allowed to return, which is not the same
                  // claim and is never rendered as one.
                  reportsOrdinaryNoResults(run.retrieval) ? (
                    <Empty
                      message={`No results for "${run.query}". Try fewer or different terms.`}
                    />
                  ) : (
                    // The notice above already carries the spec sentence. This
                    // cell states the OTHER fact — what the scan actually did —
                    // and in particular does not suggest a narrower query, which
                    // would send the reader to fix a query that was never the
                    // reason the page came back empty.
                    <Empty
                      message={
                        completeness.kind === "limited"
                          ? "No authorized results were returned before the scan bound."
                          : "This page returned nothing and the server did not report whether more exists."
                      }
                    />
                  )
                ) : (
                  <Empty
                    message="No matching results."
                    action={
                      <button
                        type="button"
                        className="ne-btn ne-btn--quiet"
                        onClick={() => {
                          setSystems([]);
                          setWithin("");
                        }}
                      >
                        Show all {allRows.length}
                      </button>
                    }
                  />
                )
              }
              error={
                <div className="ne-state ne-state--error" role="alert">
                  <p className="ne-state__line">Search failed.</p>
                  <ToolbarButton label="Retry" onClick={() => void runQuery(query, compareKey)} />
                </div>
              }
              note={
                phase === "done" && sort.key !== RELEVANCE.key && allRows.length > 0
                  ? `Sorted within the ${allRows.length} results returned.`
                  : null
              }
            />
          </>
        ) : null}
      </ListScreen>

      {openRow === null ? null : (
        <ResultPane
          row={openRow}
          query={run?.query ?? ""}
          entities={run?.entities ?? []}
          // Ask's rule, shared implementation: the Graph link exists only
          // when the served traversal proves this citation anchors an entity.
          // Offered looser, it lands on an empty graph — the owner followed
          // exactly that link to "No entity with that key".
          graphResolves={
            openRow.sourceRef !== null &&
            sourceRefResolvesToEntity(run?.graphPaths ?? [], openRow.sourceRef)
          }
          onClose={() => setOpenKey(null)}
        />
      )}
    </div>
  );
}

/* ========================== completeness notice ========================== */

/**
 * 015A0-ENFORCEMENT's visible incompleteness, in the server's own terms.
 *
 * `limited` renders the SPEC literal and is not conditioned on the row count:
 * `partial` and `exhausted` are both authority-limited, and an EMPTY
 * authority-limited page is the one this notice exists for. The bounds beneath
 * it are the server's scan accounting, rendered as such — they name no corpus
 * total, no hidden candidate and no identity.
 *
 * `unstated` renders a DIFFERENT sentence and no bounds, because a server that
 * sent no envelope reported no authority decision and no counts. Saying
 * "authority-limited" there would attribute a decision to a server that made
 * none; saying nothing at all would let an unproven page read as a whole one.
 *
 * A comparison renders one of these per identity, because two identities scan
 * under two authorities and a single status could only be wrong for one.
 */
function CompletenessNotice({
  completeness,
  identity,
}: {
  completeness: RetrievalCompleteness;
  identity: string | null;
}) {
  if (completeness.kind === "complete") return null;
  const line =
    completeness.kind === "limited" ? AUTHORITY_LIMITED_LINE : COMPLETENESS_UNSTATED_LINE;
  return (
    <div className="ne-search__authority" role="status">
      <p className="ne-search__authorityline">
        {identity === null ? line : `${identity} — ${line}`}
      </p>
      {completeness.kind === "limited" ? (
        <p className="ne-search__authoritybounds">{authorityLimitBounds(completeness)}</p>
      ) : null}
    </div>
  );
}

/* ============================ recent searches ============================ */

/** The idle screen's reverse-chron list of this visit's runs. Each row replays
 * its capture (labelled as such) or re-runs the query live. Renders nothing
 * when there is nothing — never an empty shell. */
function RecentSearches({
  entries,
  onOpen,
  onRerun,
}: {
  entries: readonly SearchHistoryEntry[];
  onOpen: (entry: SearchHistoryEntry) => void;
  onRerun: (entry: SearchHistoryEntry) => void;
}) {
  if (entries.length === 0) return null;
  return (
    <section className="ne-search__history" aria-label="Recent searches">
      <h2 className="ne-search__historyhead">Recent searches</h2>
      <ul className="ne-search__historylist">
        {entries.map((entry) => (
          <li className="ne-search__historyrow" key={`${entry.size} ${entry.query}`}>
            <button
              type="button"
              className="ne-search__historyopen"
              onClick={() => onOpen(entry)}
              aria-label={`Show captured results for "${entry.query}"`}
            >
              <span className="ne-search__historyquery">{entry.query}</span>
              <span className="ne-search__historymeta">
                {formatCount(entry.rows.length)} results
                {" · "}
                <time dateTime={entry.capturedAt} title={absoluteTime(entry.capturedAt)}>
                  {relativeTime(entry.capturedAt)}
                </time>
              </span>
            </button>
            <button
              type="button"
              className="ne-btn ne-btn--quiet"
              onClick={() => onRerun(entry)}
              aria-label={`Re-run "${entry.query}"`}
            >
              Re-run
            </button>
          </li>
        ))}
      </ul>
      {/* The storage rule, disclosed where it applies (searchHistory.ts): this
          list is this tab's and this sign-in's, and nothing is kept. */}
      <p className="ne-search__historynote">Held in this tab only — nothing is persisted.</p>
    </section>
  );
}

/* ============================== the columns ============================== */

function scoreText(score: number | null): string {
  return score === null ? ABSENT : score.toFixed(2);
}

/** "Returned (0.91)" / "Not returned" — §6.5's two cell values. */
function returnedText(score: number | null | undefined): string {
  return score === null || score === undefined ? "Not returned" : `Returned (${score.toFixed(2)})`;
}

/**
 * §Search (render `search.html`) — the explanatory note shown ONLY on a
 * fallback identity, never on a real title or heading: a visible "this is a
 * stand-in" cue distinguishing the document's own name from an identifier
 * standing in for one. Keyed by every `ResultIdentity.kind` except the two
 * that ARE a served name.
 */
const IDENTITY_FALLBACK_NOTE: Record<
  Exclude<ResultIdentity["kind"], "title" | "heading">,
  string
> = {
  "source-path": "No served title — the connector's resource path stands in",
  "record-id": "No served title or heading — the connector's record id stands in",
  "document-id": "No readable identity was served",
};

/**
 * Line one of a result row: the served human identity ladder (title →
 * heading → source path → record id → hash last), rendered plain when a real
 * name was served and dimmed/mono with an explanatory tooltip when nothing
 * better was served — the hash headlines only in that last case, never
 * while anything better exists (owner defect, 2026-08-04).
 */
function IdentityLabel({ identity }: { identity: ResultIdentity }) {
  if (identity.kind === "title" || identity.kind === "heading") {
    return <span title={identity.label}>{identity.label}</span>;
  }
  if (identity.kind === "document-id") {
    return (
      <span
        className="ne-mono ne-search__identity--fallback"
        title={`${identity.label} — ${IDENTITY_FALLBACK_NOTE["document-id"]}`}
      >
        {truncateMiddle(identity.label)}
      </span>
    );
  }
  return (
    <span
      className="ne-mono ne-search__identity--fallback"
      title={IDENTITY_FALLBACK_NOTE[identity.kind]}
    >
      {identity.label}
    </span>
  );
}

/** Renders `text` with every verbatim query-term occurrence wrapped in
 * `<mark class="ne-hit">` (results.markTerms) — a fact about the text, never
 * a claim about the ranking; the score column stays the server's only
 * relevance claim. Unmarked text passes through as a single string so a
 * query-less render (the comparison view supplies no query here) costs
 * nothing extra. */
function MarkedText({ text, query }: { text: string; query: string }) {
  const segments = markTerms(text, query);
  if (segments.length === 1 && segments[0]!.marked === false) return <>{text}</>;
  return (
    <>
      {segments.map((segment, index) =>
        segment.marked ? (
          <mark className="ne-hit" key={index}>
            {segment.text}
          </mark>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      )}
    </>
  );
}

/** The score column's cell: the served number plus a quiet 56px meter — the
 * meter is decoration over the SAME served figure, never a second number. An
 * absent score (the compare view's union rows) renders nothing here, so the
 * table's own em-dash convention takes over. */
function ScoreCell({ score }: { score: number | null }) {
  if (score === null) return null;
  const pct = Math.max(0, Math.min(100, Math.round(score * 100)));
  return (
    <span className="ne-search__scorecell">
      <span className="ne-search__scoreval">{score.toFixed(2)}</span>
      <span className="ne-search__scorebar" aria-hidden="true">
        <i style={{ width: `${pct}%` }} />
      </span>
    </span>
  );
}

function rankColumn(): Column<ResultRow> {
  // NO sortValue, deliberately: this column IS the ranking's identity, and a
  // control that reorders a table by its own row numbers means nothing.
  return { key: "rank", header: "#", numeric: true, width: "52px", cell: (row) => row.rank };
}

function sourceColumn(): Column<ResultRow> {
  return {
    key: "source",
    header: "Source",
    width: "140px",
    // A chunk with no matching citation has no source system; the em dash is
    // rendered by the table and nothing is inferred from the document id.
    cell: (row) => row.sourceSystem,
    sortValue: (row) => row.sourceSystem,
    searchText: (row) => row.sourceSystem ?? "",
  };
}

/** §Search — line one of every row is the document's OWN name (the served
 * identity ladder), never the hash while anything better was served; the
 * column stays sortable and keyed by `documentId` (the render's own
 * "Document" header, not the render's non-sortable merged "Result" column —
 * see the unit's commit message for why sortability was kept). */
function documentColumn(): Column<ResultRow> {
  return {
    key: "document",
    header: "Document",
    width: "240px",
    cell: (row) => <IdentityLabel identity={row.identity} />,
    sortValue: (row) => row.documentId,
    searchText: (row) => `${row.documentId} ${row.identity.label}`,
  };
}

/** §6.3's five columns. */
function resultColumns(query: string): ReadonlyArray<Column<ResultRow>> {
  return [
    rankColumn(),
    sourceColumn(),
    documentColumn(),
    {
      key: "excerpt",
      header: "Excerpt",
      // Not sortable: an alphabetical ordering of prose is an ordering nobody
      // reads a result list by.
      cell: (row) => (
        <span className="ne-search__excerpt">
          <MarkedText text={row.excerpt} query={query} />
        </span>
      ),
      searchText: (row) => row.text,
    },
    {
      key: "score",
      header: "Score",
      numeric: true,
      width: "88px",
      cell: (row) => <ScoreCell score={row.score} />,
      sortValue: (row) => row.score,
      firstSort: "desc",
    },
  ];
}

/** §6.5's five columns: `# · Source · Document · You · {other id}`. */
function compareColumns(other: string): ReadonlyArray<Column<ResultRow>> {
  return [
    rankColumn(),
    sourceColumn(),
    documentColumn(),
    {
      // Deliberately the SAME key as the single-identity Score column: it holds
      // the same served figure, so the default relevance sort and §6.2's sort
      // select keep working across the switch into and out of a comparison.
      key: "score",
      header: "You",
      width: "160px",
      cell: (row) => returnedText(row.score),
      sortValue: (row) => row.score,
      firstSort: "desc",
      searchText: (row) => row.text,
    },
    {
      key: "other",
      header: other,
      width: "160px",
      cell: (row) => returnedText(row.otherScore),
      sortValue: (row) => row.otherScore ?? null,
      firstSort: "desc",
    },
  ];
}

/**
 * §6's export columns: rank, source_system, document_id, chunk_id, score,
 * excerpt. In the comparison view the file carries what the table carries —
 * each identity's own cell — because a comparison exported as one score column
 * is a claim about one identity made from two.
 */
function csvColumns(compared: string | null) {
  const head = [
    { header: "rank", value: (row: ResultRow) => String(row.rank) },
    { header: "source_system", value: (row: ResultRow) => row.sourceSystem ?? "" },
    { header: "document_id", value: (row: ResultRow) => row.documentId },
    { header: "chunk_id", value: (row: ResultRow) => row.chunkId },
  ];
  if (compared === null) {
    return [
      ...head,
      {
        header: "score",
        value: (row: ResultRow) => (row.score === null ? "" : row.score.toFixed(2)),
      },
      { header: "excerpt", value: (row: ResultRow) => row.excerpt },
    ];
  }
  return [
    ...head,
    { header: "you", value: (row: ResultRow) => returnedText(row.score) },
    { header: compared, value: (row: ResultRow) => returnedText(row.otherScore) },
  ];
}

/* ============================= the detail pane ============================ */

/** §6.4 — full chunk text, its metadata, the citation ref, and the two ways out
 * of it. Every field is a served field; absent ones render the em dash.
 *
 * THE TITLE IS THE SERVED HUMAN IDENTITY (resultRecord.resultIdentity), never
 * the hash while anything better exists — the hash stays in the Document field
 * below, because it is what the audit chain records. */
function ResultPane({
  row,
  query,
  entities,
  graphResolves,
  onClose,
}: {
  row: ResultRow;
  /** The run's own query — so the pane's full text marks the same verbatim
   * terms the list excerpt does. */
  query: string;
  entities: readonly RelatedEntity[];
  /** True when the run's served traversal evidences this row's source_ref —
   * the ONLY condition under which Graph can answer the link. */
  graphResolves: boolean;
  onClose: () => void;
}) {
  const metadata = Object.entries(row.metadata);
  return (
    <DetailPane
      title={row.identity.label}
      subtitle={row.sourceSystem ?? undefined}
      onClose={onClose}
      footer={
        <>
          {row.sourceUrl === null ? null : (
            // The deep link is SERVER enrichment (`source_url`). When the API
            // cannot resolve one there is no button, rather than a guessed URL.
            // §Search (render `search.html`): this exit is the FILLED button —
            // the render draws it plain `.ne-btn`, "View in Graph" quiet.
            <a className="ne-btn" href={row.sourceUrl} target="_blank" rel="noreferrer">
              Open in source system
            </a>
          )}
          {!graphResolves || row.sourceRef === null ? null : (
            // ENCODED. A source_ref is a source system's own pointer and may hold
            // '&', '#' or '+'; pasted raw it becomes a second query parameter, a
            // fragment, or a space, and Graph then anchors on a ref the server
            // never sent — a wrong record shown as the right one.
            <Link className="ne-btn ne-btn--quiet" to={graphCitationsHref([row.sourceRef])}>
              {SOURCE_VIEW_IN_GRAPH}
            </Link>
          )}
        </>
      }
    >
      <DetailFields>
        <DetailField label="Rank">{row.rank}</DetailField>
        <DetailField label="Score">{scoreText(row.score)}</DetailField>
        <DetailField label="Document" mono>
          {row.documentId}
        </DetailField>
        <DetailField label="Citation" mono>
          {row.sourceRef ?? ABSENT}
        </DetailField>
        <DetailField label="Text">
          <p className="ne-search__full">
            <MarkedText text={row.text} query={query} />
          </p>
        </DetailField>
        <DetailField label="Metadata">
          {metadata.length === 0 ? (
            ABSENT
          ) : (
            <dl className="ne-search__meta">
              {metadata.map(([key, value]) => (
                <div className="ne-search__metarow" key={key}>
                  <dt>{key}</dt>
                  <dd>
                    <MetadataValueOf value={value} />
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </DetailField>
      </DetailFields>
      {entities.length === 0 ? null : (
        <section className="ne-search__related">
          <h3 className="ne-search__relatedhead">Related entities</h3>
          <ul className="ne-search__chips" aria-label="Related entities">
            {entities.map((entity) => (
              <li key={entity.entityKey}>
                <Link
                  className="ne-chip"
                  to={graphEntityHref(entity.entityKey, { mode: "record" })}
                >
                  {entity.display}
                </Link>
                {entity.provisional ? <StatePill word="UNCONFIRMED" /> : null}
              </li>
            ))}
          </ul>
        </section>
      )}
    </DetailPane>
  );
}
