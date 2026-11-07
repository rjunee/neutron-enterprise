// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE AGENT WORKSPACE — ONE USER ACT, ONE NOUN.
//
// The owner, on the shipped version: "i click on Agents and get some janky
// list, then i click into one and get the two column view but everything looks
// ugly… its so confusing opening a run and it takes me to some other page, and
// then theres also a conversation turns page? i dont know what those weird blue
// buttons 'chat, conversations, runs, capabilities' are at the top."
//
// Every one of those is the same defect: ONE act — asking this agent to do
// something — was surfaced as three peer nouns plus two extra routes. So:
//
//   THE THREAD IS THE SCREEN. `/agents/:id` opens directly into the
//   conversation (agentic) or the run ledger (deterministic). Chat is not a
//   tab; it is the page. The conversation-turns page is deleted outright and
//   the Run page survives only as a deep-link target for audit entries and
//   notifications — nothing here links to it.
//
//   TWO TABS, AND THEY LOOK LIKE TABS. `Conversation | Capabilities` or
//   `Runs | Capabilities`, drawn as an underline strip on the mast's own rule.
//   Capabilities earns a tab because "what can this agent touch" is a different
//   question from "what did it do"; Conversations and Runs do not, because they
//   are the same question asked of two strategies and they now share one slot
//   in the sidebar.
//
//   RUN DETAIL NEVER NAVIGATES. A turn's receipt line carries a quiet "Run
//   receipt" action that fills the docked pane in place, beside the thread the
//   reader is still in. Citations fill that same pane.
//
//   FOUR CHROME COLUMNS BECOME THREE. The agent-switcher rail and the
//   question-history pane are both deleted into one 268px sidebar.
//
// THE STRATEGY IS A QUIET META WORD. It was a full-width pill competing with
// four filled buttons; it is now the server's `default_strategy` said once, in
// the title row, in the shell's own neutrals. It still comes only from the
// server and still renders nothing when the server has not answered.

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { TranscriptTurn } from "../transcript";
import { Link, useSearchParams } from "react-router";
import { DetailPane } from "../primitives/DetailPane";
import { Empty, LoadError, Loading } from "../primitives/states";
import { AgentCapabilityFacts } from "./AgentCapabilityFacts";
import { DeterministicThread } from "./DeterministicThread";
import { useConversationTurns } from "./conversationTurns";
import { UnderlineTabs } from "./UnderlineTabs";
import { WorkspaceSidebar } from "./WorkspaceSidebar";
import {
  runControlBody,
  runOffersRetry,
  RUN_RECEIPT_LABEL,
  RUN_RECEIPT_THING,
  RunReceiptExits,
  RunReceiptFields,
  runReceiptTitle,
} from "./RunReceiptPane";
import { strategyGloss, strategyWord } from "./StrategyBadge";
import { verifiedRowAnchors } from "./rowAnchors";
import { writeLastWorkspace } from "./lastWorkspace";
import { HashChip } from "../primitives/HashChip";
import { DEFAULT_ASK_AGENT_ID } from "../api/types";
import type {
  AgentCapabilityPreflight,
  AgentDetailView,
  AgentRunTarget,
  AgentsClient,
  ConversationSummary,
  RunDetail,
} from "./types";
import { useAgentsRead } from "./useAgentsRead";
import "./agents.css";

const VIEWS = ["thread", "capabilities"] as const;
export type AgentView = (typeof VIEWS)[number];

/** `?view=` is additive: an unknown value is not an error, it is the thread. A
 * stale bookmark from a future build must not strand a reader on a blank tab. */
export function viewFromSearch(raw: string | null): AgentView {
  return VIEWS.includes(raw as AgentView) ? (raw as AgentView) : "thread";
}

/** The first tab's label is the job it does for THIS agent's strategy. Its id
 * is `thread` in both cases, so one deep link means one tab on every agent. */
export function threadTabLabel(agent: AgentDetailView): string {
  return agent.default_strategy === "deterministic" ? "Runs" : "Conversation";
}

/** The Capabilities tab's quiet count. An empty grant renders as NOTHING — the
 * clause is omitted, never rendered as "0 tools" or "0 skills". A grant of zero
 * of everything produces no count at all. */
export function capabilityCount(agent: AgentDetailView): string | null {
  const parts: string[] = [];
  if (agent.tool_grants.length > 0) {
    parts.push(`${agent.tool_grants.length} ${agent.tool_grants.length === 1 ? "tool" : "tools"}`);
  }
  if (agent.skills.length > 0) {
    parts.push(`${agent.skills.length} ${agent.skills.length === 1 ? "skill" : "skills"}`);
  }
  return parts.length === 0 ? null : parts.join(" · ");
}

/** The composer footer's caps note: THE NUMBERS THIS TURN IS ACTUALLY HELD TO.
 *
 * It used to print the manifest's declared budgets — "Caps · 12 steps · 10 tool
 * calls" — and both halves of that were a problem.
 *
 * The word. "Steps" is what the transcript band counts, and a band row is a
 * recorded ACTIVITY row: a tool call, a model call, an evidence or delivery
 * decision. A turn that made ten tool calls and twenty other recorded steps
 * therefore read "30 steps" under a footer promising a cap of twelve, and a
 * governed number that reads as violated takes every other governed number on
 * the screen down with it. Nothing here says "steps" any more; the two counts
 * are different quantities and now have different words.
 *
 * The number. `max_steps` is not enforced as a step count anywhere — it narrows
 * the tool-call ceiling and it buys wall clock, and no counter is ever compared
 * against it. So it is not printed as a cap. What IS printed comes from
 * `enforced_caps`, which the server derives at the same place it builds the
 * executing adapter (agent_execution_caps.py), so the number shown and the
 * number held are the same value rather than two agreeing derivations.
 *
 * No enforced caps means no bounded agentic loop — nothing to state, so nothing
 * is stated. */
export function capsNote(agent: AgentDetailView): string | null {
  const caps = agent.enforced_caps;
  if (caps === null) return null;
  return `Caps · ${caps.max_tool_calls} tool calls · ${caps.max_model_requests} model calls · ${caps.wall_clock_seconds}s`;
}

/** availability_reasons, joined and VERBATIM. Never a rewrite, and never a
 * generic sentence standing in for the server's own words. An unavailable agent
 * with no served reasons says only that it is unavailable. */
export function unavailableReason(agent: AgentDetailView): string | null {
  if (agent.available) return null;
  return agent.availability_reasons.length === 0
    ? "Unavailable"
    : `Unavailable — ${agent.availability_reasons.join(", ")}`;
}

/** Everything the agentic thread needs from the workspace. The thread is
 * INJECTED rather than imported so this module stays testable against typed
 * client seams: importing ChatPage would drag its whole provider tree into
 * every Agents interior test. A deployment that passes no renderer simply has
 * no agentic thread. */
export interface AgenticThreadInjection {
  readonly agentId: string;
  readonly agentLabel: string;
  /** The sidebar's selection — the canonical Conversation to continue, or null
   * for a thread that has not been opened yet. */
  readonly conversationId: string | null;
  /** The stream minted a Conversation. The sidebar selects it and re-reads. */
  readonly onConversationStarted: (conversationId: string) => void;
  /** Dock a turn's Run record in the pane WITHOUT leaving the thread. */
  readonly onOpenReceipt: (runId: string) => void;
  /** The docked pane's current content, when the workspace has one open. The
   * thread renders it in its own pane slot and, while it is non-null, shows no
   * pane of its own — one dock, one subject. */
  readonly receiptPane: ReactNode | null;
  readonly capsNote: string | null;
  /** availability_reasons, verbatim. Non-null disables the composer. */
  readonly unavailableReason: string | null;
  /** The selected Conversation's canonical turns, read back from the ONE store.
   * `null` when nothing is selected; an empty array is the different, positive
   * fact that the server served no turns for it. */
  readonly pastTurns: readonly TranscriptTurn[] | null;
  /** WHICH DOOR THE COMPOSER'S TURNS LEAVE THROUGH. The default-Ask chat wire
   * reaches exactly ONE agent — the canonical default — so every OTHER agentic
   * agent's workspace routes its turns to that agent's own canonical Run
   * envelope ("agent-run"). This is what makes a turn typed at Customer
   * Support run on Customer Support instead of silently landing on the
   * Analyst. */
  readonly runRouting: "default-ask" | "agent-run";
  /** The frozen submission envelope for the "agent-run" door, built from this
   * workspace's own served reads (agent version, capability snapshot hash,
   * platform config hash). `null` while any part has not arrived — the thread
   * must then keep its composer closed rather than fall back to the default
   * agent. Always `null` on the "default-ask" door. */
  readonly runTarget: AgentRunTarget | null;
}

export function AgentWorkspace({
  client,
  agentId,
  identityKey,
  platformConfigHash,
  configurationState,
  renderChat,
}: {
  client: AgentsClient;
  agentId: string;
  identityKey: string;
  platformConfigHash: string | null;
  configurationState: "loading" | "ready" | "unavailable";
  renderChat?: (props: AgenticThreadInjection) => ReactNode;
}) {
  const load = useCallback(() => client.getAgent(agentId), [agentId, client]);
  const [read, retry] = useAgentsRead<AgentDetailView>(load, identityKey);
  const loadPreflight = useCallback(() => client.getAgentPreflight(agentId), [agentId, client]);
  const [preflight, retryPreflight] = useAgentsRead<AgentCapabilityPreflight>(
    loadPreflight,
    identityKey,
  );
  const [search, setSearch] = useSearchParams();
  const view = viewFromSearch(search.get("view"));
  const conversationId = search.get("conversation");
  const runId = search.get("run");
  const receiptRunId = search.get("receipt");
  const [filter, setFilter] = useState("");
  const [historyKey, setHistoryKey] = useState("0");

  useEffect(() => {
    if (read.phase === "ready") writeLastWorkspace(agentId);
  }, [agentId, read.phase]);

  const patch = useCallback(
    (changes: Readonly<Record<string, string | null>>) => {
      const next = new URLSearchParams(search);
      for (const [key, value] of Object.entries(changes)) {
        if (value === null) next.delete(key);
        else next.set(key, value);
      }
      setSearch(next, { replace: true });
    },
    [search, setSearch],
  );

  const strategy = read.phase === "ready" ? read.value.default_strategy : null;
  const deterministic = strategy === "deterministic";

  const openReceipt = useCallback((id: string) => patch({ receipt: id }), [patch]);
  const closeReceipt = useCallback(() => patch({ receipt: null }), [patch]);
  const selectHistory = useCallback(
    (id: string) =>
      patch(deterministic ? { run: id, receipt: null } : { conversation: id, receipt: null }),
    [deterministic, patch],
  );
  const startNew = useCallback(() => patch({ conversation: null, receipt: null }), [patch]);
  const conversationStarted = useCallback(
    (id: string) => {
      setHistoryKey((value) => String(Number(value) + 1));
      patch({ conversation: id });
    },
    [patch],
  );

  const receiptAction = useCallback(
    (runId: string) => (
      <button type="button" className="tr-receipt__quiet" onClick={() => openReceipt(runId)}>
        {RUN_RECEIPT_LABEL}
      </button>
    ),
    [openReceipt],
  );
  // THE ONE HISTORY READ. The selected Conversation's canonical turns — the
  // same rows the deleted conversation-turns page used to be a whole route for.
  const pastTurns = useConversationTurns(
    client,
    agentId,
    deterministic ? null : conversationId,
    identityKey,
    receiptAction,
  );

  const receiptPane =
    receiptRunId === null ? null : (
      <DockedRunReceipt
        client={client}
        agentId={agentId}
        runId={receiptRunId}
        identityKey={identityKey}
        onClose={closeReceipt}
        onOpenRun={openReceipt}
      />
    );

  if (read.phase === "loading") return <Loading things="Agent details" />;
  if (read.phase === "unavailable") return <Empty message="This Agent isn’t available." />;
  if (read.phase === "error") return <LoadError things="Agent details" onRetry={retry} />;
  const agent = read.value;
  const count = capabilityCount(agent);
  const blocked = unavailableReason(agent);

  return (
    <div className="ag-stage">
      <div className="ag-mast">
        <nav className="ag-crumbs" aria-label="Breadcrumb">
          <span>Execution</span>
          <span aria-hidden="true">›</span>
          <Link to="/agents/all">Agents</Link>
          <span aria-hidden="true">›</span>
          <b>{agent.label}</b>
        </nav>
        <div className="ag-titlerow">
          <h1 className="ag-titlerow__h1">{agent.label}</h1>
          <span className="ag-strategy">
            <span className="ag-strategy__word">{strategyWord(agent.default_strategy)}</span>
            <span className="ag-strategy__gloss">{strategyGloss(agent.default_strategy)}</span>
          </span>
          <span className="ag-titlerow__spacer" />
          <span className="ag-titlerow__meta">
            {agent.owner_label} · agent v{agent.version} ·{" "}
            <HashChip hash={agent.config_hash} label="Copy agent definition hash" />
          </span>
        </div>
        <UnderlineTabs
          label="Agent workspace"
          selected={view}
          onSelect={(next) => patch({ view: next })}
          tabs={[
            { id: "thread", label: threadTabLabel(agent) },
            { id: "capabilities", label: "Capabilities", count },
          ]}
        />
      </div>

      <div className="ag-work">
        <WorkspaceSidebar
          client={client}
          identityKey={identityKey}
          agentId={agentId}
          strategy={agent.default_strategy}
          selectedId={deterministic ? runId : conversationId}
          onSelect={selectHistory}
          onNew={deterministic ? undefined : startNew}
          filter={filter}
          onFilterChange={setFilter}
          historyKey={historyKey}
        />

        {view === "capabilities" ? (
          <div className="ag-panel ag-capstack">
            <AgentCapabilityFacts
              agent={agent}
              preflight={preflight}
              onRetryPreflight={retryPreflight}
            />
          </div>
        ) : deterministic ? (
          <DeterministicThread
            client={client}
            agent={agent}
            agentId={agentId}
            identityKey={identityKey}
            selectedRunId={runId}
            onSelectRun={(id) => patch({ run: id })}
            onOpenReceipt={openReceipt}
            onRunAccepted={(id) => {
              setHistoryKey((value) => String(Number(value) + 1));
              patch({ run: id });
            }}
            receiptPane={receiptPane}
            platformConfigHash={platformConfigHash}
            configurationState={configurationState}
            unavailableReason={blocked}
          />
        ) : renderChat === undefined ? (
          <div className="ag-panel">
            <Empty message="No conversational surface is mounted in this deployment." />
          </div>
        ) : (
          renderChat({
            agentId,
            agentLabel: agent.label,
            conversationId,
            onConversationStarted: conversationStarted,
            onOpenReceipt: openReceipt,
            receiptPane,
            capsNote: capsNote(agent),
            unavailableReason: blocked,
            pastTurns: pastTurns.phase === "ready" ? pastTurns.value : null,
            // The default agent keeps the canonical chat wire; every other
            // agentic agent submits to its OWN Run envelope, assembled here
            // from served values only. A missing part yields a null target —
            // the thread closes its composer instead of falling back.
            runRouting: agentId === DEFAULT_ASK_AGENT_ID ? "default-ask" : "agent-run",
            runTarget:
              agentId === DEFAULT_ASK_AGENT_ID ||
              platformConfigHash === null ||
              configurationState !== "ready" ||
              preflight.phase !== "ready" ||
              preflight.value.capabilities === null
                ? null
                : {
                    agentId,
                    agentVersion: agent.version,
                    configHash: platformConfigHash,
                    capabilityHash: preflight.value.capabilities.capability_hash,
                  },
          })
        )}
      </div>
    </div>
  );
}

/** The docked receipt. It reads the Run the reader asked about and renders the
 * SAME fields the `/agents/:id/runs/:runId` deep link renders — one body of
 * facts, two frames. */
export function DockedRunReceipt({
  client,
  agentId,
  runId,
  identityKey,
  onClose,
  onOpenRun,
  createIdempotencyKey = () => crypto.randomUUID(),
}: {
  client: AgentsClient;
  agentId: string;
  runId: string;
  identityKey: string;
  onClose: () => void;
  /** Re-point this pane at another Run without leaving the thread — the Run a
   * retry minted, or the predecessor of the one on screen. */
  onOpenRun?: (runId: string) => void;
  createIdempotencyKey?: () => string;
}) {
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const load = useCallback(async () => {
    const run = (await client.getAgentRun(agentId, runId)) as RunDetail;
    const [agent, conversation, envelope] = await Promise.all([
      client.getAgent(agentId).catch(() => null),
      client.getConversation(agentId, run.conversation_id).catch(() => null),
      // The evidence envelope, for the receipt's row-identity field: the
      // served canonical hash of each verified row the answer rests on.
      // Optional and fail-open to ABSENCE — a deployment without the read, or
      // a read that fails, draws no anchors rather than failing the receipt.
      client.getAgentRunEvidence
        ? client.getAgentRunEvidence(agentId, runId).catch(() => null)
        : Promise.resolve(null),
    ]);
    return { run, agent, conversation: conversation as ConversationSummary | null, envelope };
  }, [agentId, client, runId]);
  const [read, retry] = useAgentsRead(load, `${identityKey}:${runId}`);
  const title = useMemo(
    () => (read.phase === "ready" ? runReceiptTitle(read.value.run) : RUN_RECEIPT_LABEL),
    [read],
  );
  return (
    <DetailPane
      title={title}
      subtitle={<span className="ne-mono">{runId}</span>}
      onClose={onClose}
      footer={
        read.phase === "ready" ? (
          <RunReceiptExits
            agentId={agentId}
            run={read.value.run}
            {...(onOpenRun ? { onOpenRun } : {})}
            extra={
              // REFUSED IS NOT FAILED. Retry appears only for a run the server
              // says ended WITHOUT an outcome; a refusal is an outcome and
              // never offers one. See runOffersRetry.
              runOffersRetry(read.value.run) ? (
                <button
                  type="button"
                  className="ne-btn"
                  disabled={retrying}
                  onClick={() => {
                    const body = runControlBody(read.value.run);
                    if (body === null) return;
                    setRetrying(true);
                    setRetryError(null);
                    void client
                      .retryAgentRun(agentId, runId, body, createIdempotencyKey())
                      .then(
                        (receipt) => onOpenRun?.(receipt.run_id),
                        () => setRetryError("Couldn’t retry this Run."),
                      )
                      .finally(() => setRetrying(false));
                  }}
                >
                  Retry as new Run
                </button>
              ) : null
            }
          />
        ) : undefined
      }
    >
      {read.phase === "loading" ? <Loading things={RUN_RECEIPT_THING} /> : null}
      {read.phase === "unavailable" ? <Empty message="This Run isn’t available." /> : null}
      {read.phase === "error" ? <LoadError things={RUN_RECEIPT_THING} onRetry={retry} /> : null}
      {retryError === null ? null : (
        <p className="ag-runform__absent" role="alert">
          {retryError}
        </p>
      )}
      {read.phase === "ready" ? (
        <RunReceiptFields
          run={read.value.run}
          agent={read.value.agent}
          conversation={read.value.conversation}
          rowAnchors={read.value.envelope === null ? null : verifiedRowAnchors(read.value.envelope)}
        />
      ) : null}
    </DetailPane>
  );
}
