// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 — MODELS. Which models this deployment routes to, what each is allowed to
// do, and what usage and spend look like.
//
// WHAT THIS REPLACES: a heading over a paragraph explaining what the page would
// one day show. The lanes were already served — they sat inside a compliance
// screen — so the operations home for them renders them now, in a real table
// with sortable headers, a lane filter, a live count and an export.
//
// WHAT IS DELIBERATELY NOT HERE. §10's Endpoint, Tasks routed and Status
// columns, the row's binding pane and the three model-policy write actions
// ("Allow a model…", "Edit binding…", "Remove from policy…") all need the
// governed policy router. Until it answers, those columns are ABSENT — not
// blank, not disabled — and no control on this screen offers a write the
// server has no endpoint for. The columns and the pane are built
// (models/policy.ts, models/reads.ts) and appear the moment the read is
// servable.
//
// WHAT IS HERE NOW (NE-AIRGAP-TOGGLE): the network row's governed air-gap
// change — POST /v1/network/air-gap ships server-side, so "Request change"
// renders (probed via airGapWriter, absent on a client that cannot serve it).
// Engaging applies immediately (Tier 1); disengaging parks for approval
// (Tier 2); both resolve by re-reading the posture (§2.5).
//
// CONFIGURATION AND MEASUREMENT SHARE THIS SCREEN AND MUST NOT SHARE A VOICE.
// The Policy table below states what this deployment is CONFIGURED to route to
// — including, for every lane, the model each alias resolves to and the window
// declared for it, so `frontier-default` is answerable without asking a human.
// The "Private serving health" panel above states what the server MEASURED,
// with an age and a latency, for the private lanes ne-api may probe. The table
// therefore labels its two model facts "Configured model" and "Declared
// window", and carries no timestamp, no pill and no freshness of its own: a
// configured mapping rendered as evidence of liveness would be a lie the
// operator has no way to catch.
//
// THIN CLIENT (C7). Every value below is a field of the governed read. The
// console derives no lane, no binding and no air-gap state of its own, and a
// refusal on any of these reads renders the SERVER's refusal — never a
// permission this console decided for itself.

import { useCallback, useMemo, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { useSearchParams } from "react-router";
import { ApiError } from "../api/client";
import type { PostureView } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
// The four-state governed read (loading · ok · refused · error) built for Home.
// Reused rather than re-implemented: refused and error are different facts and
// exactly one state machine in this console decides which is which.
import { useGovernedRead } from "../landing/useGovernedRead";
import type { GovernedRead } from "../landing/useGovernedRead";
import { usePosture } from "../posture/PostureContext";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { ListScreen } from "../primitives/PageHeader";
import { StatePill } from "../primitives/StatePill";
import {
  Empty,
  LoadError,
  PendingMarker,
  PolicyDenied,
  Refused,
  SaveError,
} from "../primitives/states";
import {
  FilterChip,
  FilterSelect,
  PrimaryButton,
  Toolbar,
  ToolbarButton,
} from "../primitives/Toolbar";
import { buildCsv, downloadCsv } from "../primitives/csv";
import { formatCount } from "../primitives/format";
import { filterRows, sortRows } from "../primitives/tableSort";
import type { SortState } from "../primitives/tableSort";
import {
  applyLaneFilter,
  laneOptions,
  maskedCredential,
  modelCount,
  policyRowsFromEntries,
  policyRowsFromLanes,
} from "./policy";
import type { PolicyRow } from "./policy";
import { airGapWriter, policyReader, usageReader } from "./reads";
import type { AirGapWriter } from "./reads";
import { RoutingTab } from "./RoutingTab";
import { ServingPanel } from "./ServingPanel";
import { LifecyclePanel } from "./LifecyclePanel";
import { UsageTab } from "./UsageTab";
import "./models.css";

/** §10: "Lanes table. Default sort: Lane A–Z, then Model." Model is the served
 * order of the rows (models/policy.ts) and the table's sort is stable, so
 * sorting that order by Lane is exactly the stated default. */
const DEFAULT_SORT: SortState = { key: "lane", dir: "asc" };

type Tab = "policy" | "routing" | "usage";

// ===========================================================================
// The tabs
// ===========================================================================

const TABS: ReadonlyArray<{ id: Tab; label: string }> = [
  { id: "policy", label: "Policy" },
  // NE-MODEL-ROUTING: which model answers each task, and the governed way to
  // change it — live, no restart. Sits beside Policy because Policy states what
  // this deployment is ALLOWED to route to and Routing states what it DOES.
  { id: "routing", label: "Routing" },
  { id: "usage", label: "Usage" },
];

function Tabs({ tab, onChange }: { tab: Tab; onChange: (next: Tab) => void }) {
  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const index = TABS.findIndex((entry) => entry.id === tab);
      let next: number;
      if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = TABS.length - 1;
      else return;
      event.preventDefault();
      const target = TABS[next];
      if (target) onChange(target.id);
    },
    [onChange, tab],
  );
  return (
    <div className="ne-models__tabs" role="tablist" aria-label="Models" onKeyDown={onKeyDown}>
      {TABS.map((entry) => {
        const selected = entry.id === tab;
        return (
          <button
            key={entry.id}
            type="button"
            role="tab"
            id={`ne-models-tab-${entry.id}`}
            aria-selected={selected}
            aria-controls={`ne-models-panel-${entry.id}`}
            tabIndex={selected ? 0 : -1}
            className={`ne-models__tab${selected ? " ne-models__tab--on" : ""}`}
            onClick={() => onChange(entry.id)}
          >
            {entry.label}
          </button>
        );
      })}
    </div>
  );
}

function Panel({ tab, children }: { tab: Tab; children: ReactNode }) {
  return (
    <div
      className="ne-models__panel"
      role="tabpanel"
      id={`ne-models-panel-${tab}`}
      aria-labelledby={`ne-models-tab-${tab}`}
    >
      {children}
    </div>
  );
}

// ===========================================================================
// The network row (§10: "Air gap" + its state, above the table)
// ===========================================================================

/**
 * FOUR STATES, AND THE ROLE BOUNDARY IS ONLY ONE OF THEM.
 *
 * "Not visible to your role" renders on a server refusal and on nothing else. A
 * read still in flight, or one that failed to reach the server, says so in its
 * own words — stating a permission boundary over a load or an outage is a false
 * claim about authorization, made by the console, about the server.
 */
type AirGapWriteState =
  | { kind: "idle" }
  | { kind: "confirming" }
  | { kind: "saving" }
  /** 403: the server's reason, rendered verbatim where the submit happened. */
  /** 403. `reason` is the gate's OWN served sentence, or null when it sent
   * none — never a sentence the console wrote for it. */
  | { kind: "denied"; reason: string | null }
  | { kind: "error"; detail: string | null };

/**
 * The §2.5 write flow for the air-gap change (NE-AIRGAP-TOGGLE).
 *
 * C7 HOLDS THROUGHOUT: the console never decides whether the flip is permitted
 * and never renders a state it did not read back. Applied AND pending both
 * resolve by RE-READING the posture (`onSettled`) — the pending marker comes
 * from the server's `air_gap_pending`, the new value from the server's
 * `air_gap`, and the displayed posture stays the in-force one until the server
 * says otherwise. The tier is the server's too: the same submit answers
 * `applied` (engaging — Tier 1) or `pending` (disengaging — Tier 2); this
 * component learns which from the response, it does not predict it.
 */
type AirGapWriteResult = Awaited<ReturnType<AirGapWriter>>;

function useAirGapSubmit(
  writer: AirGapWriter | null,
  onSettled: (result: AirGapWriteResult) => void,
) {
  const [state, setState] = useState<AirGapWriteState>({ kind: "idle" });
  const confirm = useCallback(() => setState({ kind: "confirming" }), []);
  const reset = useCallback(() => setState({ kind: "idle" }), []);
  const submit = useCallback(
    async (target: boolean) => {
      if (writer === null) return;
      setState({ kind: "saving" });
      try {
        const result = await writer(target);
        setState({ kind: "idle" });
        onSettled(result);
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 403) {
          // THE GATE'S OWN SENTENCE OR NOTHING. `PolicyDenied` prints this
          // under "Denied by policy.", where it reads as the gate's account of
          // the denial — so a 403 that carried no `detail` passes null and the
          // card states the denial and no reason for it.
          setState({ kind: "denied", reason: err.detail ?? null });
          return;
        }
        setState({
          kind: "error",
          detail: err instanceof ApiError ? (err.detail ?? null) : null,
        });
      }
    },
    [writer, onSettled],
  );
  return { state, confirm, reset, submit };
}

function NetworkRow({
  read,
  writer,
}: {
  read: GovernedRead<PostureView>;
  writer: AirGapWriter | null;
}) {
  const { refresh: refreshSharedPosture } = usePosture();
  const reloadObjectPosture = read.reload;
  const onSettled = useCallback(
    (result: AirGapWriteResult) => {
      // Every settled result gets its object-local server read-back so a parked
      // change can render its pending marker. Only APPLIED means the in-force
      // posture may have moved, so only that server result refreshes the shared
      // shell/Home payload. No browser-side flip is ever published.
      reloadObjectPosture();
      if (result.status === "applied") void refreshSharedPosture();
    },
    [reloadObjectPosture, refreshSharedPosture],
  );
  const { state, confirm, reset, submit } = useAirGapSubmit(writer, onSettled);
  let value: ReactNode;
  let action: ReactNode = null;
  if (read.status === "loading") {
    value = (
      <>
        <span className="ne-skeleton ne-models__shimmer" />
        <span className="ne-visually-hidden" role="status">
          Loading network state
        </span>
      </>
    );
  } else if (read.status === "refused") {
    value = <span className="ne-models__networknote">Not visible to your role</span>;
  } else if (read.status === "error" || read.data === null) {
    value = (
      <>
        <span className="ne-absent">—</span>{" "}
        <span className="ne-models__networknote">Couldn&rsquo;t load</span>
      </>
    );
  } else {
    const posture = read.data;
    value = posture.air_gap ? "On" : "Off";
    if (posture.air_gap_pending !== null) {
      // §2.5 step 3: pending renders ON the object, read back from the server;
      // the in-force value above stays the displayed value.
      action = (
        <PendingMarker
          by={posture.air_gap_pending.requested_by}
          requestId={posture.air_gap_pending.request_id}
        />
      );
    } else if (writer !== null) {
      // The submit names the consequence (§2.5 step 1) — engaging cuts the
      // frontier lane now; disengaging asks a human first.
      const target = !posture.air_gap;
      const consequence = target
        ? "Engage air gap — frontier egress stops"
        : "Disengage air gap — requires approval";
      action =
        state.kind === "idle" ? (
          <ToolbarButton label="Request change" onClick={confirm} />
        ) : (
          <span className="ne-models__networkconfirm">
            <PrimaryButton
              label={consequence}
              onClick={() => void submit(target)}
              disabled={state.kind === "saving"}
            />
            <ToolbarButton label="Cancel" onClick={reset} disabled={state.kind === "saving"} />
          </span>
        );
    }
  }
  return (
    <>
      <dl className="ne-models__network">
        <dt className="ne-models__networklabel">Air gap</dt>
        <dd className="ne-models__networkvalue">{value}</dd>
        {action === null ? null : <dd className="ne-models__networkaction">{action}</dd>}
      </dl>
      {state.kind === "denied" ? <PolicyDenied reason={state.reason} /> : null}
      {state.kind === "error" ? <SaveError detail={state.detail} /> : null}
    </>
  );
}

// ===========================================================================
// The policy tab
// ===========================================================================

function useColumns(detailed: boolean): Column<PolicyRow>[] {
  return useMemo(() => {
    const served: Column<PolicyRow>[] = [
      {
        key: "model",
        header: "Model",
        mono: true,
        cell: (row) => row.model_id,
        sortValue: (row) => row.model_id,
        searchText: (row) => row.model_id,
      },
      {
        key: "provider",
        header: "Provider",
        width: "160px",
        cell: (row) => row.provider,
        sortValue: (row) => row.provider,
        searchText: (row) => row.provider,
      },
      {
        key: "lane",
        header: "Lane",
        width: "180px",
        cell: (row) => row.lane,
        sortValue: (row) => row.lane,
        searchText: (row) => row.lane,
      },
      // WHAT THE ALIAS RESOLVES TO — served for EVERY lane, which is the whole
      // fix. Before this, `frontier-default` and `frontier-best` rendered as two
      // aliases over the same provider and lane, and the screen could not answer
      // "what is frontier-default?" at all; the rich serving panel above answers
      // it only for lanes ne-api may probe, and it may never probe a provider
      // host (C2). So the frontier rows' only honest source is configuration —
      // and it is given to every row on the same terms, not bolted onto theirs.
      //
      // "Configured" IS THE CLAIM, and the header says so. The serving panel
      // says "Checked … · 76 ms" because the server measured; nothing in this
      // table measured anything, so nothing in it may read as freshness. A
      // column headed plainly "Model" would let a config mapping pass for an
      // observation — the one thing this table must not do.
      {
        key: "configured_model",
        header: "Configured model",
        mono: true,
        cell: (row) => row.configured_model,
        sortValue: (row) => row.configured_model,
        searchText: (row) => row.configured_model,
      },
      {
        key: "context_window",
        // "Declared", not "Window": the serving panel's Window is vLLM's
        // observed max_model_len for the model an endpoint SAID it was serving.
        // This one is the number the deployment configured and the context
        // budget derives from. Same word for both would merge a measurement
        // into a config read.
        header: "Declared window",
        width: "160px",
        numeric: true,
        firstSort: "desc",
        cell: (row) => formatCount(row.context_window),
        sortValue: (row) => row.context_window,
      },
    ];
    // The three columns the policy router serves. On a deployment without it
    // they are absent — a column of em dashes would state that every lane lacks
    // a binding, which is a different and false claim.
    if (!detailed) return served;
    return [
      ...served,
      {
        key: "endpoint",
        header: "Endpoint",
        cell: (row) =>
          row.binding === null ? null : (
            <>
              <span className="ne-mono">{row.binding.endpoint_host}</span>{" "}
              <span className="ne-models__sub">{maskedCredential(row.binding)}</span>
            </>
          ),
        sortValue: (row) => row.binding?.endpoint_host ?? null,
        searchText: (row) => row.binding?.endpoint_host ?? "",
      },
      {
        key: "tasks",
        header: "Tasks routed",
        width: "132px",
        numeric: true,
        firstSort: "desc",
        cell: (row) => (row.tasks === null ? null : formatCount(row.tasks.length)),
        sortValue: (row) => (row.tasks === null ? null : row.tasks.length),
      },
      {
        key: "status",
        header: "Status",
        width: "240px",
        cell: (row) =>
          row.pending === null ? (
            <StatePill word="ACTIVE" />
          ) : (
            <PendingMarker by={row.pending.requested_by} requestId={row.pending.request_id} />
          ),
        sortValue: (row) => (row.pending === null ? "ACTIVE" : "PENDING"),
      },
    ];
  }, [detailed]);
}

function PolicyTab() {
  const client = useApiClient();
  const posture = useGovernedRead(useCallback(() => client.posture(), [client]));
  const writer = useMemo(() => airGapWriter(client), [client]);
  const reader = useMemo(() => policyReader(client), [client]);
  const detailed = reader !== null;
  const policy = useGovernedRead(
    useCallback(async () => (reader === null ? null : await reader()), [reader]),
  );

  const [lane, setLane] = useState("all");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const [openKey, setOpenKey] = useState<string | null>(null);

  const rows = useMemo<readonly PolicyRow[]>(() => {
    if (detailed) return policy.data === null ? [] : policyRowsFromEntries(policy.data.models);
    return posture.data === null ? [] : policyRowsFromLanes(posture.data.model_lanes);
  }, [detailed, policy.data, posture.data]);

  // The read that owns the table: the policy router when it answers, the
  // posture read otherwise. Its status IS the table's status — rows from one
  // read are never shown under the state of another.
  const source = detailed ? policy : posture;

  const columns = useColumns(detailed);
  const filtered = useMemo(
    () => filterRows(applyLaneFilter(rows, lane), columns, search),
    [rows, lane, columns, search],
  );
  /** Exactly the rows the table renders, in the order it renders them. */
  const windowRows = useMemo(() => sortRows(filtered, columns, sort), [filtered, columns, sort]);

  const exportCsv = useCallback(() => {
    if (windowRows.length === 0) return;
    const filters: string[] = [];
    if (lane !== "all") filters.push(`Lane: ${lane}`);
    if (search.trim() !== "") filters.push(`Search: ${search.trim()}`);
    downloadCsv(
      "models.csv",
      buildCsv<PolicyRow>({
        title: "Models",
        filters,
        columns: [
          { header: "Model", value: (row) => row.model_id },
          { header: "Provider", value: (row) => row.provider },
          { header: "Lane", value: (row) => row.lane },
          { header: "Configured model", value: (row) => row.configured_model },
          { header: "Declared window", value: (row) => String(row.context_window) },
          ...(detailed
            ? [
                { header: "Endpoint", value: (row: PolicyRow) => row.binding?.endpoint_host ?? "" },
                {
                  header: "Tasks routed",
                  value: (row: PolicyRow) => (row.tasks === null ? "" : String(row.tasks.length)),
                },
                {
                  header: "Status",
                  value: (row: PolicyRow) => (row.pending === null ? "Active" : "Pending"),
                },
              ]
            : []),
        ],
        rows: windowRows,
      }),
    );
  }, [windowRows, lane, search, detailed]);

  // §2.4: the server said no, and this renders that — with no toolbar, no
  // export and above all no count, because a zero counted out of a read this key
  // was never shown is a fabricated zero. The network row stays: it reports its
  // own read's state, and on a refusal that state is the boundary itself.
  if (source.status === "refused") {
    return (
      <>
        <NetworkRow read={posture} writer={writer} />
        <Refused thing="the model policy" page="Models" />
      </>
    );
  }

  const status: TableStatus =
    source.status === "loading" ? "loading" : source.status === "error" ? "error" : "ok";
  const open = openKey === null ? null : (rows.find((row) => row.key === openKey) ?? null);

  return (
    <>
      <NetworkRow read={posture} writer={writer} />
      <Toolbar
        search={{ value: search, onChange: setSearch, placeholder: "Filter models…" }}
        filters={
          <>
            <FilterSelect
              label="Lane"
              value={lane}
              options={laneOptions(rows)}
              onChange={setLane}
            />
            {lane === "all" ? null : (
              <FilterChip label={`Lane: ${lane}`} onDismiss={() => setLane("all")} />
            )}
          </>
        }
        // A COUNT OF A WINDOW THAT HAS NOT ARRIVED IS A FABRICATED ZERO. The
        // skeleton rows already say the read is in flight; "0 models" over them
        // is a number that is wrong on screen.
        count={status === "loading" ? "" : modelCount(windowRows.length)}
        actions={
          <ToolbarButton
            label="Export CSV"
            onClick={exportCsv}
            disabled={windowRows.length === 0}
          />
        }
      />
      <DataTable<PolicyRow>
        columns={columns}
        rows={filtered}
        rowKey={(row) => row.key}
        label="Model policy"
        things="models"
        sort={sort}
        onSortChange={setSort}
        // A row opens its binding only where there is a binding to open: on a
        // v1 deployment the pane would repeat the three cells already on screen.
        onRowActivate={detailed ? (row) => setOpenKey(row.key) : undefined}
        selectedKey={openKey}
        status={status}
        empty={<Empty message="No models configured." />}
        error={<LoadError things="models" onRetry={source.reload} />}
      />
      {open === null ? null : (
        <DetailPane title={open.model_id} subtitle={open.lane} onClose={() => setOpenKey(null)}>
          <DetailFields>
            <DetailField label="Model" mono>
              {open.model_id}
            </DetailField>
            <DetailField label="Provider">{open.provider}</DetailField>
            <DetailField label="Lane">{open.lane}</DetailField>
            <DetailField label="Configured model" mono>
              {open.configured_model}
            </DetailField>
            <DetailField label="Declared window">
              {formatCount(open.context_window)} tokens
            </DetailField>
            <DetailField label="Endpoint" mono>
              {open.binding?.endpoint_host ?? "—"}
            </DetailField>
            <DetailField label="Credential">{maskedCredential(open.binding) ?? "—"}</DetailField>
            <DetailField label="Tasks routed">
              {open.tasks === null || open.tasks.length === 0 ? (
                "—"
              ) : (
                <span className="ne-models__chips">
                  {open.tasks.map((route) => (
                    <span className="ne-models__chip" key={route.task}>
                      {route.task}
                    </span>
                  ))}
                </span>
              )}
            </DetailField>
            <DetailField label="Status">
              {open.pending === null ? (
                <StatePill word="ACTIVE" />
              ) : (
                <PendingMarker by={open.pending.requested_by} requestId={open.pending.request_id} />
              )}
            </DetailField>
          </DetailFields>
        </DetailPane>
      )}
    </>
  );
}

// ===========================================================================
// The screen
// ===========================================================================

export function ModelsPage() {
  const client = useApiClient();
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: Tab = raw === "usage" ? "usage" : raw === "routing" ? "routing" : "policy";

  const setTab = useCallback(
    (next: Tab) => {
      const updated = new URLSearchParams(params);
      if (next === "policy") updated.delete("tab");
      else updated.set("tab", next);
      setParams(updated, { replace: true });
    },
    [params, setParams],
  );

  const usageRead = useMemo(() => usageReader(client), [client]);

  return (
    <div className="ne-models">
      <ListScreen title="Models" tabs={<Tabs tab={tab} onChange={setTab} />}>
        {/* Keep the server's private-endpoint observation in the presenter's
            first viewport beside both model-authority surfaces. It renders
            nothing when the deployment has no probeable lane or the server
            refuses this role's ops read. */}
        {tab === "usage" ? null : <ServingPanel />}
        {tab === "usage" ? null : <LifecyclePanel />}
        {tab === "policy" ? (
          <Panel tab="policy">
            <PolicyTab />
          </Panel>
        ) : tab === "routing" ? (
          <Panel tab="routing">
            <RoutingTab />
          </Panel>
        ) : (
          <Panel tab="usage">
            {usageRead === null ? (
              // §10, verbatim: one line, no preview, no invented rows.
              <p className="ne-models__gap">
                Usage reporting isn&rsquo;t available on this deployment yet.
              </p>
            ) : (
              <UsageTab read={usageRead} />
            )}
          </Panel>
        )}
      </ListScreen>
    </div>
  );
}
