// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// A DETERMINISTIC AGENT IS STRUCTURALLY DIFFERENT, NOT BADGED.
//
// SPEC (PRE-DEMO-REHEARSAL-012): "A generic prompt box with a decorative mode
// badge is not completion." So this surface is not the agentic thread with a
// word changed. Its COMPOSER IS THE RUN FORM — workflow plate, fixed plan,
// frozen inputs, the typed field, budgets, Run — and there is deliberately no
// textarea anywhere on the screen, on any branch.
//
// PAST RUNS READ IN THE SAME TRANSCRIPT GRAMMAR AS QUESTIONS. The submitted
// typed input opens the turn where a question would, the governed calls demote
// into the same collapsed work band, and the same receipt closes it. One
// anatomy, two strategies — which is the whole reason Runs stopped being a peer
// index with a page of its own.
//
// THE RECORD OPENS IN PLACE. The turn's receipt line carries a quiet action
// that fills the docked pane beside the thread. Nothing here links to
// `/agents/:id/runs/:runId`. Its words are RunReceiptPane's, so this surface
// and Ask's thread name the same thing the same way.

import { useCallback, useEffect, type ReactNode } from "react";
import { Empty, LoadError, Loading } from "../primitives/states";
import { Transcript } from "../transcript";
import { DeterministicRunLauncher } from "./DeterministicRunLauncher";
import { RUN_RECEIPT_LABEL } from "./RunReceiptPane";
import { buildRunTurn } from "./runTurn";
import type { AgentDetailView, AgentsClient, RunDetail, RunPage } from "./types";
import { useAgentsRead } from "./useAgentsRead";

export function DeterministicThread({
  client,
  agent,
  agentId,
  identityKey,
  selectedRunId,
  onSelectRun,
  onOpenReceipt,
  onRunAccepted,
  receiptPane,
  platformConfigHash,
  configurationState,
  unavailableReason,
}: {
  client: AgentsClient;
  agent: AgentDetailView;
  agentId: string;
  identityKey: string;
  /** The sidebar's selection. `null` means "the most recent Run the server
   * served", which this resolves once the list lands — it never guesses an id. */
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
  onOpenReceipt: (runId: string) => void;
  onRunAccepted?: (runId: string) => void;
  receiptPane: ReactNode | null;
  platformConfigHash: string | null;
  configurationState: "loading" | "ready" | "unavailable";
  /** availability_reasons, verbatim. The surface stays visible and legible —
   * disabled, not hidden. */
  unavailableReason: string | null;
}) {
  const loadRuns = useCallback(() => client.listAgentRuns(agentId), [agentId, client]);
  const [runs] = useAgentsRead<RunPage>(loadRuns, identityKey);
  const siblings = runs.phase === "ready" ? runs.value.items : [];
  const firstRunId = siblings[0]?.run_id ?? null;

  // The server's own newest Run is the landing selection. Resolved from the
  // served list rather than assumed, and written into the URL so the selection
  // the sidebar highlights and the turn the thread renders are one value.
  useEffect(() => {
    if (selectedRunId === null && firstRunId !== null) onSelectRun(firstRunId);
  }, [firstRunId, onSelectRun, selectedRunId]);

  const runId = selectedRunId ?? firstRunId;
  const loadRun = useCallback(async () => {
    if (runId === null) return null;
    return (await client.getAgentRun(agentId, runId)) as RunDetail;
  }, [agentId, client, runId]);
  const [run, retryRun] = useAgentsRead<RunDetail | null>(loadRun, `${identityKey}:${runId ?? ""}`);

  return (
    <div className={`ag-main${receiptPane === null ? "" : " ag-main--pane"}`}>
      <div className="ag-thread">
        {unavailableReason === null ? null : (
          <div className="ag-runform">
            <p className="ag-unavail" role="status">
              <span aria-hidden="true">⚠</span>
              <span>{unavailableReason}</span>
            </p>
          </div>
        )}
        <div className="ag-thread__scroll">
          <RunLedger
            runs={runs.phase}
            run={run}
            onRetry={retryRun}
            siblings={siblings}
            onOpenReceipt={onOpenReceipt}
          />
        </div>
        <DeterministicRunLauncher
          key={`${identityKey}:${platformConfigHash ?? configurationState}`}
          client={client}
          agentId={agentId}
          identityKey={identityKey}
          platformConfigHash={platformConfigHash}
          configurationState={configurationState}
          {...(onRunAccepted ? { onAccepted: onRunAccepted } : {})}
        />
        <span className="ne-visually-hidden">{agent.label}</span>
      </div>
      {receiptPane}
    </div>
  );
}

function RunLedger({
  runs,
  run,
  onRetry,
  siblings,
  onOpenReceipt,
}: {
  runs: "loading" | "ready" | "unavailable" | "error";
  run: ReturnType<typeof useAgentsRead<RunDetail | null>>[0];
  onRetry: () => void;
  siblings: RunPage["items"];
  onOpenReceipt: (runId: string) => void;
}) {
  if (runs === "loading" || run.phase === "loading") return <Loading things="Runs" />;
  if (runs === "unavailable" || run.phase === "unavailable") {
    return <Empty message="These Runs aren’t available." />;
  }
  if (runs === "error" || run.phase === "error") {
    return <LoadError things="Runs" onRetry={onRetry} />;
  }
  // EMPTY IS EMPTY: no Runs, no placeholder turn, no zero. The run form below
  // is already present, which is the first act a reader can take here.
  if (run.value === null) {
    return <Empty message="No Runs for this Agent yet." />;
  }
  const detail = run.value;
  const turn = buildRunTurn({
    run: detail,
    siblings,
    approval: null,
    receiptActions: (
      <button
        type="button"
        className="tr-receipt__quiet"
        onClick={() => onOpenReceipt(detail.run_id)}
      >
        {RUN_RECEIPT_LABEL}
      </button>
    ),
  });
  return <Transcript turns={[turn]} label="Runs" />;
}
