// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// MODELS › ROUTING. Which model answers each gateway task, and the governed way
// to change it.
//
// WHAT THIS REPLACES: a config file and a restart. Until now the model that
// served a task was `NE_GATEWAY_TASKS` env JSON, read once at boot — so
// switching models meant editing compose and bouncing the process, which drops
// every in-flight conversation. A change made here is durable, audited, and
// takes effect on the NEXT model call, in every process. Nothing restarts, and
// a conversation continues across the swap with its history intact (the
// transcript lives in the platform's own store and is reloaded at the top of
// every turn — the model is bound per turn, never per session).
//
// A ROUTE CHANGE IS A GOVERNED WRITE, NOT A SETTING. There is no Save button.
// A change is requested here, approved by a DIFFERENT human at the approval
// gate, and only then applied — and the apply lands on the hash-chained audit
// log naming the requester, the approver and the old → new model. The console
// drives that; it never writes a route.
//
// THIN CLIENT (C7), AND HERE IT IS LOAD-BEARING. Which models a task may be
// repointed to is `registered ∧ lane ∧ not-current ∧ air-gap` — a judgement over
// the boot model set, the live route register, the tier table and the live
// egress posture. All of them live on the server. The
// select below is built from the server's `selectable` flag and nothing else,
// and a refused target is listed WITH the server's reason rather than silently
// missing. models/routing.ts reshapes and decides nothing.
//
// RETAINED ROUTE != AVAILABLE ROUTE. `model` is the configured alias and the
// server separately reports whether it can currently serve. A pending change
// does NOT change the displayed configured alias; neither pending nor an
// unavailable retained route is presented as live serving truth.
//
// C12: model ALIASES and lane names only. No endpoint, no credential, ever.

import { useCallback, useMemo, useState } from "react";
import { ApiError } from "../api/client";
import type { ModelRoutingView, TaskRoutingView } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { useGovernedRead } from "../landing/useGovernedRead";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { StatePill } from "../primitives/StatePill";
import { Empty, LoadError, PolicyDenied, Refused, SaveError } from "../primitives/states";
import { PrimaryButton, ToolbarButton } from "../primitives/Toolbar";
import { absoluteTime } from "../primitives/format";
import {
  changeConsequence,
  isConfigurable,
  laneLabel,
  refusedTargets,
  selectableTargets,
  sourceLabel,
} from "./routing";

const COLUMNS: ReadonlyArray<Column<TaskRoutingView>> = [
  {
    key: "task",
    header: "Task",
    mono: true,
    cell: (row) => row.task,
    sortValue: (row) => row.task,
    searchText: (row) => row.task,
  },
  {
    key: "model",
    header: "Configured route",
    mono: true,
    cell: (row) => row.model,
    sortValue: (row) => row.model,
    searchText: (row) => row.model,
  },
  {
    key: "availability",
    header: "Availability",
    width: "150px",
    cell: (row) => <StatePill word={row.available ? "SERVING" : "UNAVAILABLE"} />,
    sortValue: (row) => (row.available ? "SERVING" : "UNAVAILABLE"),
    searchText: (row) => row.unavailable_reason ?? "",
  },
  {
    key: "lane",
    header: "Lane",
    width: "120px",
    cell: (row) => laneLabel(row.lane),
    sortValue: (row) => row.lane,
  },
  {
    key: "source",
    header: "Source",
    width: "190px",
    cell: (row) => sourceLabel(row),
    sortValue: (row) => row.source,
  },
];

/**
 * The write states. "Not visible to your role" is drawn on a server 403 and on
 * nothing else — a read in flight or an outage says so in its own words, because
 * stating a permission boundary over a load is a claim about authorization the
 * console is not entitled to make.
 */
type WriteState =
  | { kind: "idle" }
  | { kind: "saving" }
  /** 403 — a POLICY denial, which is what PolicyDenied means. `reason` is the
   * gate's OWN served sentence, or null when it stated none. */
  | { kind: "denied"; reason: string | null }
  /** 409 — the server refused the change on its own rule (air gap engaged, an
   * unregistered target, a no-op). NOT a policy denial and NOT a transient save
   * failure: retrying changes nothing, so neither of those components may
   * speak for it. The server's sentence is the whole content — and null when it
   * sent none, which the render below answers in the console's own voice
   * rather than by putting a house sentence in the server's mouth. */
  | { kind: "refused"; reason: string | null }
  | { kind: "error"; detail: string | null };

export function RoutingTab() {
  const client = useApiClient();
  const read = useGovernedRead<ModelRoutingView>(
    useCallback(() => client.modelRouting(), [client]),
  );
  const [openTask, setOpenTask] = useState<string | null>(null);

  const rows = useMemo(() => read.data?.tasks ?? [], [read.data]);
  const open = openTask === null ? null : (rows.find((row) => row.task === openTask) ?? null);

  if (read.status === "refused") {
    return <Refused thing="model routing" page="Models" />;
  }

  const status: TableStatus =
    read.status === "loading" ? "loading" : read.status === "error" ? "error" : "ok";

  return (
    <>
      <DataTable<TaskRoutingView>
        columns={COLUMNS}
        rows={rows}
        rowKey={(row) => row.task}
        label="Model routing"
        things="tasks"
        defaultSort={{ key: "task", dir: "asc" }}
        status={status}
        onRowActivate={(row) => setOpenTask(row.task)}
        selectedKey={open?.task ?? null}
        empty={<Empty message="No tasks are routed on this deployment." />}
        error={<LoadError things="model routing" onRetry={read.reload} />}
        note={
          <span className="ne-models__routingnote">
            Configured routes are retained even when unavailable. Availability and its reason are
            reported by the server; an approved change takes effect on the next model call.
          </span>
        }
      />
      {open === null ? null : (
        <RoutePane row={open} onClose={() => setOpenTask(null)} onSettled={read.reload} />
      )}
    </>
  );
}

function RoutePane({
  row,
  onClose,
  onSettled,
}: {
  row: TaskRoutingView;
  onClose: () => void;
  onSettled: () => void;
}) {
  const client = useApiClient();
  const options = selectableTargets(row);
  const refused = refusedTargets(row);
  const [choice, setChoice] = useState<string>(options[0]?.model ?? "");
  const [state, setState] = useState<WriteState>({ kind: "idle" });
  const target = options.find((option) => option.model === choice) ?? null;

  // The chosen MODEL is the argument, not a closed-over derived object: the
  // callback's deps are then all stable, and the submit cannot capture a stale
  // target from a re-render.
  const submit = useCallback(
    async (model: string) => {
      setState({ kind: "saving" });
      try {
        await client.setModelRoute(row.task, model);
        setState({ kind: "idle" });
        // Applied AND pending both resolve by RE-READING: the new model appears
        // only once the server reports it, so a parked change never renders as
        // live.
        onSettled();
      } catch (err: unknown) {
        // THE SERVER'S OWN SENTENCE OR NONE AT ALL. Both of these states
        // render their reason AS THE SERVER'S — under "Denied by policy." in
        // one case, as the refusal itself in the other — so `userMessage` must
        // not be reached for here: it is the console's house sentence whenever
        // the server sent no `detail`, and it would read as the server's stated
        // reason when the server stated none.
        if (err instanceof ApiError && err.status === 403) {
          setState({ kind: "denied", reason: err.detail ?? null });
          return;
        }
        if (err instanceof ApiError && (err.status === 409 || err.status === 404)) {
          setState({ kind: "refused", reason: err.detail ?? null });
          return;
        }
        setState({ kind: "error", detail: err instanceof ApiError ? (err.detail ?? null) : null });
      }
    },
    [client, row.task, onSettled],
  );

  return (
    <DetailPane
      title={row.task}
      subtitle={`${row.model} · ${laneLabel(row.lane)}`}
      onClose={onClose}
    >
      <DetailFields>
        <DetailField label="Configured route" mono>
          {row.model}
        </DetailField>
        <DetailField label="Availability">
          <StatePill word={row.available ? "SERVING" : "UNAVAILABLE"} />
        </DetailField>
        {/* "Shipped with", not "Deployment default": the SOURCE field below
            already reads "Deployment default" when that is what is in force,
            and a label that repeats a value one line down is a screen a reader
            has to disambiguate. */}
        <DetailField label="Shipped with" mono>
          {row.default_model}
        </DetailField>
        <DetailField label="Source">{sourceLabel(row)}</DetailField>
        {row.source === "override" ? (
          <>
            <DetailField label="Requested by">{row.changed_by ?? "—"}</DetailField>
            <DetailField label="Approved by">{row.approved_by ?? "—"}</DetailField>
            <DetailField label="Changed">
              {row.changed_at === null ? "—" : absoluteTime(row.changed_at)}
            </DetailField>
          </>
        ) : null}
      </DetailFields>

      {!row.available && row.unavailable_reason !== null ? (
        <p className="ne-models__refusal" role="status">
          {row.unavailable_reason}
        </p>
      ) : null}

      {row.pending_change !== null ? (
        <section className="ne-models__routechange">
          <h2 className="ne-models__h2">Change pending approval</h2>
          <p className="ne-models__routingnote">
            {row.pending_change.model} requested by {row.pending_change.requested_by} · request{" "}
            {row.pending_change.request_id}
          </p>
        </section>
      ) : isConfigurable(row) ? (
        <section className="ne-models__routechange">
          <h2 className="ne-models__h2">Change the model</h2>
          <label className="ne-models__label" htmlFor="ne-routing-model">
            New model
          </label>
          <select
            id="ne-routing-model"
            className="ne-filter__select"
            value={choice}
            onChange={(event) => setChoice(event.target.value)}
          >
            {options.map((option) => (
              <option key={option.model} value={option.model}>
                {option.model} ({laneLabel(option.lane)})
              </option>
            ))}
          </select>
          {target === null ? null : (
            <p className="ne-models__consequence">{changeConsequence(target)}</p>
          )}
          <PrimaryButton
            label="Request change"
            onClick={() => (target === null ? undefined : void submit(target.model))}
            disabled={state.kind === "saving" || target === null}
          />
          <ToolbarButton label="Cancel" onClick={onClose} disabled={state.kind === "saving"} />
        </section>
      ) : (
        <p className="ne-models__routingnote">
          No other model on this deployment can serve this task.
        </p>
      )}

      {state.kind === "denied" ? <PolicyDenied reason={state.reason} /> : null}
      {state.kind === "refused" ? (
        <p className="ne-models__refusal" role="alert">
          {/* The server's own words when it sent them. When it did not, the
              console says so IN ITS OWN VOICE — an alert with nothing in it is
              not an option, and a house sentence here would be read as the
              server's reason for refusing. */}
          {state.reason ?? "The route wasn’t changed; the server stated no reason."}
        </p>
      ) : null}
      {state.kind === "error" ? <SaveError detail={state.detail} /> : null}

      {refused.length === 0 ? null : (
        <section className="ne-models__routerefused">
          <h2 className="ne-models__h2">Not available for this task</h2>
          {/* The server's own words. A missing option is explained, never
              silently absent — and the console invents no reason of its own. */}
          <ul className="ne-models__refusedlist">
            {refused.map((option) => (
              <li key={option.model}>
                {/* No state pill: §2.3's word list is a closed set and none of
                    its words means "this target is not on offer". The server's
                    reason is the whole content of the row. */}
                <span className="ne-mono">{option.model}</span>{" "}
                <span className="ne-models__sub">{option.reason}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </DetailPane>
  );
}
