// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §12 — EVALUATIONS. Suite results over time, and what regressed.
//
// The job is the platform owner's, before and after a model or config change:
// did the suites pass, is the judge healthy, what moved. Visited rarely; must be
// correct and exportable. So it is §1.4's list screen and nothing else — title
// row, toolbar, table, pane.
//
// WHAT THIS SCREEN NO LONGER TEACHES. The shipped version opened with a
// disclosure drawer explaining where golden sets come from and which CI pipeline
// enforces the gate. That is an architecture lecture on a results screen: the
// reader came to find out whether the safety suite regressed, and neither
// sentence helps them do it. It also drew a GATE headline — a conjunction over
// whatever rows happened to load — which §12 rules out, because "every loaded
// run passed" is a property of a CI pipeline and not of this deployment.
//
// THIN CLIENT (C7). The verdict is `passed`, rendered as sent. This console
// never compares a metric to its threshold; a run whose numbers beat every
// threshold and whose served verdict is false renders FAILED, and there is no
// code path here that could decide otherwise.
//
// THE SUITE FILTER IS SERVED, NOT SIMULATED. Choosing a suite re-reads the
// governed list route with `suite`, so the count and the export describe the
// server's answer for that suite. The options are the suites present in the
// unfiltered window — read from served rows, never invented.

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "../api/client";
import type { EvalRunView } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { useLatestRequest } from "../hooks/useLatestRequest";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { HashChip } from "../primitives/HashChip";
import { ListScreen } from "../primitives/PageHeader";
import { StatusPill } from "../primitives/StatePill";
import { FilterChip, FilterSelect, Toolbar, ToolbarButton } from "../primitives/Toolbar";
import { buildCsv, downloadCsv } from "../primitives/csv";
import { absoluteTime, relativeTime } from "../primitives/format";
import type { SortState } from "../primitives/tableSort";
import { filterRows, sortRows } from "../primitives/tableSort";
import { Empty, LoadError, Refused } from "../primitives/states";
import { RunPane } from "./RunPane";
import {
  countText,
  csvColumns,
  judgeErrorText,
  middleTruncate,
  RUNS_WINDOW,
  suitesOf,
} from "./runs";
import "./evals.css";

/**
 * The four answers the governed read can give, kept apart on purpose.
 *
 * A REFUSAL IS NOT AN OUTAGE AND AN OUTAGE IS NOT A REFUSAL. Collapsing 403,
 * 503 and "unreachable" into one state is how a screen ends up telling a reader
 * their role is the problem when the truth is that a service is down — a false
 * statement about authorization, made by software whose entire claim is that it
 * does not make those.
 */
type ScreenStatus = "loading" | "ok" | "refused" | "unconfigured" | "error";

const ALL_SUITES = "all";

export function EvalsPage() {
  const client = useApiClient();
  const runLatest = useLatestRequest();

  const [suite, setSuite] = useState<string>(ALL_SUITES);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortState>({ key: "when", dir: "desc" });
  const [runs, setRuns] = useState<readonly EvalRunView[]>([]);
  /** The answer to the read named by `settledFor`, and nothing else. LOADING is
   * DERIVED from the two disagreeing rather than commanded by the effect: a read
   * whose parameters have changed has no answer yet, and that is a fact about
   * the state, not an assignment somebody has to remember to make. */
  const [settled, setSettled] = useState<Exclude<ScreenStatus, "loading">>("error");
  const [settledFor, setSettledFor] = useState<string | null>(null);
  /** The suites seen in the UNFILTERED window. Held across a filtered read,
   * whose response contains one suite by construction and would otherwise
   * collapse the control the reader needs to get back out of it. */
  const [suiteOptions, setSuiteOptions] = useState<readonly string[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  /** What the currently rendered answer would have to be an answer TO. */
  const readKey = `${suite}#${reloadToken}`;

  // ONE fetch path — the initial load, every suite change and every retry.
  // Latest-request-wins, and a FAILED read clears the rows: stale runs under an
  // error banner are last week's verdicts presented as this minute's.
  useEffect(() => {
    void (async () => {
      const outcome = await runLatest(() =>
        client.listEvalRuns({
          suite: suite === ALL_SUITES ? undefined : suite,
          limit: RUNS_WINDOW,
        }),
      );
      if (outcome.kind === "superseded") return;
      if (outcome.kind === "success") {
        setRuns(outcome.value.runs);
        if (suite === ALL_SUITES) setSuiteOptions(suitesOf(outcome.value.runs));
        setSettled("ok");
      } else {
        setRuns([]);
        const code = outcome.error instanceof ApiError ? outcome.error.status : 0;
        setSettled(code === 403 ? "refused" : code === 503 ? "unconfigured" : "error");
      }
      setSettledFor(`${suite}#${reloadToken}`);
    })();
  }, [client, runLatest, suite, reloadToken]);

  const status: ScreenStatus = settledFor === readKey ? settled : "loading";

  const columns = useMemo(() => buildColumns(), []);

  const filtered = useMemo(() => filterRows(runs, columns, search), [runs, columns, search]);
  /** Exactly the rows the table renders, in the order it renders them — so the
   * export is the window on screen rather than a re-derivation of it. */
  const windowRows = useMemo(() => sortRows(filtered, columns, sort), [filtered, columns, sort]);

  const filterLabels = useMemo(() => {
    const out: string[] = [];
    if (suite !== ALL_SUITES) out.push(`Suite: ${suite}`);
    if (search.trim() !== "") out.push(`Search: ${search.trim()}`);
    return out;
  }, [suite, search]);

  const exportCsv = useCallback(() => {
    if (windowRows.length === 0) return;
    downloadCsv(
      "evaluations.csv",
      buildCsv<EvalRunView>({
        title: "Evaluations",
        filters: filterLabels,
        columns: csvColumns(windowRows),
        rows: windowRows,
      }),
    );
  }, [windowRows, filterLabels]);

  const openRun = openId === null ? null : (runs.find((run) => run.run_id === openId) ?? null);

  // A role boundary and a page that failed to load are different facts, so they
  // are different screens. Neither renders a toolbar: a suite filter over a
  // refusal is a control that cannot work drawn as one that can.
  if (status === "refused") {
    return (
      <div className="ne-evaluations">
        <ListScreen title="Evaluations">
          <Refused thing="evaluation runs" page="Evaluations" />
        </ListScreen>
      </div>
    );
  }
  if (status === "unconfigured") {
    return (
      <div className="ne-evaluations">
        <ListScreen title="Evaluations">
          <Empty message="Evaluation storage isn't configured on this deployment." />
        </ListScreen>
      </div>
    );
  }

  const tableStatus: TableStatus = status === "loading" ? "loading" : status;

  return (
    <div className="ne-evaluations">
      <ListScreen title="Evaluations">
        <Toolbar
          search={{ value: search, onChange: setSearch, placeholder: "Filter runs…" }}
          filters={
            <>
              <FilterSelect
                label="Suite"
                value={suite}
                options={[
                  { value: ALL_SUITES, label: "All suites" },
                  ...suiteOptions.map((name) => ({ value: name, label: name })),
                ]}
                onChange={setSuite}
              />
              {suite === ALL_SUITES ? null : (
                <FilterChip label={`Suite: ${suite}`} onDismiss={() => setSuite(ALL_SUITES)} />
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

        <DataTable<EvalRunView>
          columns={columns}
          rows={filtered}
          rowKey={(run) => run.run_id}
          label="Evaluation runs"
          things="evaluation runs"
          sort={sort}
          onSortChange={setSort}
          status={tableStatus}
          onRowActivate={(run) => setOpenId(run.run_id)}
          selectedKey={openId}
          empty={
            suite !== ALL_SUITES && runs.length === 0 ? (
              <Empty
                message={`No runs for suite "${suite}".`}
                action={
                  <button
                    type="button"
                    className="ne-btn ne-btn--quiet"
                    onClick={() => setSuite(ALL_SUITES)}
                  >
                    Clear filter
                  </button>
                }
              />
            ) : runs.length > 0 ? (
              // The window is not empty; the search-within is hiding it. Saying
              // "no runs recorded" here would be a false statement about the
              // deployment made by a text field.
              <Empty
                message="No matching runs."
                action={
                  <button
                    type="button"
                    className="ne-btn ne-btn--quiet"
                    onClick={() => setSearch("")}
                  >
                    Show all {runs.length}
                  </button>
                }
              />
            ) : (
              <Empty message="No evaluation runs recorded. Runs appear after a suite executes." />
            )
          }
          error={
            <LoadError
              things="evaluation runs"
              onRetry={() => setReloadToken((token) => token + 1)}
            />
          }
          // §2.1: a sort over a full window is a sort over a page, and the page
          // says so. Below the cap there is nothing to disclose.
          note={
            status === "ok" && runs.length >= RUNS_WINDOW
              ? `Sorted within the loaded ${RUNS_WINDOW} rows`
              : null
          }
        />
      </ListScreen>

      {openRun === null ? null : <RunPane run={openRun} onClose={() => setOpenId(null)} />}
    </div>
  );
}

/**
 * §12's runs table. Sortable: Suite, When, Verdict, Judge error rate.
 *
 * Run, Judge and Config are NOT sortable, by rule rather than by omission: an
 * ordering of opaque identifiers and hashes is an ordering nobody reads down,
 * and Judge is a two-source fallback whose alphabetical order says nothing about
 * the judges. Suite and When are how this table is actually read; Verdict and
 * Judge error rate are how a regression is found.
 */
function buildColumns(): ReadonlyArray<Column<EvalRunView>> {
  return [
    {
      key: "suite",
      header: "Suite",
      cell: (run) => run.suite,
      sortValue: (run) => run.suite,
      searchText: (run) => run.suite,
    },
    {
      key: "run",
      header: "Run",
      mono: true,
      cell: (run) => <span title={run.run_id}>{middleTruncate(run.run_id)}</span>,
      searchText: (run) => run.run_id,
    },
    {
      key: "when",
      header: "When",
      numeric: true,
      width: "120px",
      cell: (run) => (
        <time dateTime={run.created_at} title={absoluteTime(run.created_at)}>
          {relativeTime(run.created_at)}
        </time>
      ),
      sortValue: (run) => Date.parse(run.created_at),
      firstSort: "desc",
    },
    {
      key: "verdict",
      header: "Verdict",
      width: "120px",
      // SERVER-AUTHORITATIVE. `passed` is read, never derived.
      cell: (run) => <StatusPill word={run.passed ? "PASSED" : "FAILED"} />,
      sortValue: (run) => run.passed,
      searchText: (run) => (run.passed ? "passed" : "failed"),
    },
    {
      key: "judge_error_rate",
      header: "Judge error rate",
      numeric: true,
      width: "140px",
      cell: (run) => judgeErrorText(run.judge_error_rate),
      sortValue: (run) => run.judge_error_rate,
      firstSort: "desc",
    },
    {
      key: "judge",
      header: "Judge",
      mono: true,
      cell: (run) => run.judge_model_id ?? run.judge_family,
      searchText: (run) => `${run.judge_model_id ?? ""} ${run.judge_family ?? ""}`,
    },
    {
      key: "config",
      header: "Config",
      width: "120px",
      cell: (run) => <HashChip hash={run.config_hash} label="Copy configuration hash" />,
      searchText: (run) => run.config_hash,
    },
  ];
}
