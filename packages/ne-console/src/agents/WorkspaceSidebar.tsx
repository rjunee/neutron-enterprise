// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// FOUR CHROME COLUMNS BECOME THREE.
//
// The shipped workspace carried an agent-switcher rail AND, inside the mounted
// Ask surface, a second question-history pane. Two columns of chrome, 252px and
// ~200px, both permanently on screen, before the conversation got any width at
// all. This is one column that does both jobs: the agent roster on top, the
// SELECTED agent's one history list below.
//
// ONE HISTORY, ONE LIST — AND THAT REQUIRED PICKING A STORE.
//
// The two panes read different stores, which is why merging them was previously
// refused: Ask listed "chat sessions" and the Conversations tab listed canonical
// Agent Conversations, and a reader whose canonical list was empty would have
// concluded their history was gone. That reasoning was correct about the risk
// and wrong about the facts. `POST /v1/chat/sessions` MINTS A CANONICAL AGENT
// CONVERSATION and returns its id as `session_id`
// (ne-api/routers/chat.py: `session_id=conversation.conversation_id`), and
// `GET /v1/agents/{id}/conversations` lists the same principal-owned rows
// (routers/conversations.py). They were never two stores; they were one store
// read through two names, and the Ask-side read went through a `listChatSessions`
// capability probe against a method `api/client.ts` does not have and never had —
// so it resolved to null on every load and the pane only ever showed the threads
// opened in the current tab.
//
// So the canonical Conversations index is the store, the Ask-side read is
// deleted outright (chat/askHistory.ts, chat/useAskHistory.ts, chat/HistoryPane.tsx),
// and this component is the one place a history is listed.
//
// WHAT A ROW SAYS IS WHAT THE SERVER SAYS. A Conversation's title is
// server-authored from server time only (`Conversation · 2026-08-27 09:12`) and
// deliberately reads no prompt or output; this renders that title. It does not
// fetch each conversation's first turn to make a prettier excerpt, and it does
// not call `last_sequence` a turn count — that field is the optimistic-concurrency
// version the next Run must send, not a tally of anything a reader would count.
//
// AN AGENT WITH NO HISTORY RENDERS EMPTY, NOT ZERO-PADDED. No "0 conversations",
// no placeholder rows, no skeleton left standing. One factual sentence.

import { useCallback, useMemo } from "react";
import { NavLink } from "react-router";
import { runSettlementPresentation } from "../agent-harness/adapters/runWorkspace";
import { absoluteTime, relativeTime } from "../primitives/format";
import { LoadError } from "../primitives/states";
import type {
  AgentPage,
  AgentsInteriorClient,
  ConversationPage,
  RunPage,
  RunReceipt,
} from "./types";
import { strategyWord } from "./StrategyBadge";
import { useAgentsRead } from "./useAgentsRead";
import type { ReadState } from "./useAgentsRead";

/** One history row, in the single shape the list renders — whichever index the
 * selected agent's strategy makes its history. */
export interface HistoryRow {
  readonly id: string;
  /** The row's own excerpt: a Conversation's served title, or a Run's served
   * display label. Never composed here. */
  readonly excerpt: string;
  /** The status glyph's tone class, when the row has a served status. A
   * Conversation has none and renders none — an invented ✓ would be a claim. */
  readonly tone?: "g" | "a" | "r";
  readonly glyph?: string;
  /** The served stamp, already formatted. */
  readonly meta: string;
  readonly metaTitle?: string;
}

export function conversationRows(page: ConversationPage, now = Date.now()): HistoryRow[] {
  return page.items.map((conversation) => ({
    id: conversation.conversation_id,
    excerpt: conversation.title,
    meta: relativeTime(conversation.updated_at, now),
    metaTitle: absoluteTime(conversation.updated_at),
  }));
}

export function runRows(page: RunPage): HistoryRow[] {
  return page.items.map((run: RunReceipt) => {
    // The SETTLEMENT, so a Run that completed while serving nothing does not sit
    // in this list wearing a green ✓ Completed.
    const presentation = runSettlementPresentation(run);
    return {
      id: run.run_id,
      excerpt: run.display_label,
      tone: presentation.tone,
      glyph: presentation.glyph,
      meta: `${presentation.word} · ${absoluteTime(run.created_at)}`,
    };
  });
}

/** Substring filter over what the reader can actually see. A filter that also
 * matched an opaque id would narrow the list for reasons nobody can observe. */
export function filterHistory(rows: readonly HistoryRow[], filter: string): readonly HistoryRow[] {
  const needle = filter.trim().toLowerCase();
  if (needle === "") return rows;
  return rows.filter((row) => row.excerpt.toLowerCase().includes(needle));
}

export function WorkspaceSidebar({
  client,
  identityKey,
  agentId,
  strategy,
  selectedId,
  onSelect,
  onNew,
  filter,
  onFilterChange,
  historyKey = "",
}: {
  client: AgentsInteriorClient;
  identityKey: string;
  /** The open agent. `null` on the catalog, where nothing may claim to be open. */
  agentId: string | null;
  /** The SERVER's `default_strategy`, or null while it is still loading. It
   * decides which index this agent's one history list is — conversations for an
   * agentic agent, runs for a deterministic one. Runs and Conversations are
   * never two peer indexes again. */
  strategy: "agentic" | "deterministic" | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** Present only where a new thread is a thing the reader can start. */
  onNew?: () => void;
  filter: string;
  onFilterChange: (next: string) => void;
  /** Bumped by the caller when a write means the history has moved on. */
  historyKey?: string;
}) {
  const loadRoster = useCallback(() => client.listAgents(), [client]);
  const [roster, retryRoster] = useAgentsRead<AgentPage>(loadRoster, identityKey);

  const deterministic = strategy === "deterministic";
  const heading = deterministic ? "Runs" : "Conversations";

  const loadHistory = useCallback(async (): Promise<HistoryRow[]> => {
    if (agentId === null || strategy === null) return [];
    return deterministic
      ? runRows(await client.listAgentRuns(agentId))
      : conversationRows(await client.listAgentConversations(agentId));
  }, [agentId, client, deterministic, strategy]);
  const [history, retryHistory] = useAgentsRead<HistoryRow[]>(
    loadHistory,
    `${identityKey}:${agentId ?? ""}:${strategy ?? ""}:${historyKey}`,
  );

  const rows = useMemo(
    () => (history.phase === "ready" ? filterHistory(history.value, filter) : []),
    [filter, history],
  );

  return (
    <aside className="ag-side" aria-label={`Agents and ${heading.toLowerCase()}`}>
      <div className="ag-side__roster">
        <span className="ag-side__label" id="ag-roster-label">
          Agents
        </span>
        <Roster read={roster} onRetry={retryRoster} currentAgentId={agentId} />
        <div className="ag-side__all">
          <NavLink to="/agents/all" end>
            All agents
          </NavLink>
        </div>
      </div>
      <div className="ag-side__hist">
        <div className="ag-side__histhead">
          <span className="ag-side__label" id="ag-hist-label">
            {heading}
          </span>
          {onNew === undefined ? null : (
            <button type="button" className="ne-btn ne-btn--quiet ag-btn--sm" onClick={onNew}>
              New
            </button>
          )}
        </div>
        <input
          type="search"
          className="ne-searchwithin__input"
          value={filter}
          placeholder={`Filter ${heading.toLowerCase()}…`}
          aria-label={`Filter ${heading.toLowerCase()}`}
          onChange={(event) => onFilterChange(event.target.value)}
        />
        <HistoryList
          heading={heading}
          read={history}
          rows={rows}
          filtered={filter.trim() !== ""}
          selectedId={selectedId}
          onSelect={onSelect}
          onRetry={retryHistory}
        />
      </div>
    </aside>
  );
}

/** THE ORDER IS THE SERVER'S. `/v1/agents` pages through opaque cursors at 25 a
 * page; re-ordering inside one page describes a sequence the server did not
 * send. When there is another page, this says so and links to the inventory
 * view rather than growing a second pager inside a 268px column. */
function Roster({
  read,
  onRetry,
  currentAgentId,
}: {
  read: ReadState<AgentPage>;
  onRetry: () => void;
  currentAgentId: string | null;
}) {
  if (read.phase === "loading") {
    return <SidebarSkeleton label="Loading agents" widths={["82%", "64%", "74%"]} />;
  }
  if (read.phase === "unavailable") {
    return <p className="ag-runform__absent">The Agents catalog isn’t available.</p>;
  }
  if (read.phase === "error") return <LoadError things="agents" onRetry={onRetry} />;
  if (read.value.items.length === 0) {
    return <p className="ag-runform__absent">No agents are registered in this deployment.</p>;
  }
  return (
    <>
      <ul className="ag-roster" aria-labelledby="ag-roster-label">
        {read.value.items.map((agent) => (
          <li key={agent.agent_id}>
            <NavLink
              to={`/agents/${encodeURIComponent(agent.agent_id)}`}
              className={`ag-roster__item${agent.agent_id === currentAgentId ? " ag-roster__item--on" : ""}`}
              aria-current={agent.agent_id === currentAgentId ? "page" : undefined}
            >
              <span className="ag-roster__name">{agent.label}</span>
              <span className="ag-roster__meta">
                {strategyWord(agent.default_strategy)}
                {agent.available ? "" : " · unavailable"}
              </span>
            </NavLink>
          </li>
        ))}
      </ul>
      {read.value.next_cursor === null ? null : (
        <p className="ag-runform__absent">
          More agents than fit here. <NavLink to="/agents/all">Open the catalog</NavLink>
        </p>
      )}
    </>
  );
}

function HistoryList({
  heading,
  read,
  rows,
  filtered,
  selectedId,
  onSelect,
  onRetry,
}: {
  heading: string;
  read: ReadState<HistoryRow[]>;
  rows: readonly HistoryRow[];
  filtered: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onRetry: () => void;
}) {
  const noun = heading.toLowerCase();
  if (read.phase === "loading") {
    return <SidebarSkeleton label={`Loading ${noun}`} widths={["92%", "55%", "84%", "48%"]} />;
  }
  if (read.phase === "unavailable") {
    return <p className="ag-runform__absent">These {noun} aren’t available.</p>;
  }
  if (read.phase === "error") return <LoadError things={noun} onRetry={onRetry} />;
  // EMPTY IS EMPTY. One factual sentence and no rows — never a zero, never a
  // placeholder row, never a skeleton left standing in for absent history.
  if (read.value.length === 0) {
    return <p className="ag-runform__absent">No {noun} with this agent yet.</p>;
  }
  if (rows.length === 0 && filtered) {
    return <p className="ag-runform__absent">No {noun} match this filter.</p>;
  }
  return (
    <ul className="ag-hist" aria-labelledby="ag-hist-label">
      {rows.map((row) => (
        <li key={row.id}>
          <button
            type="button"
            className={`ag-hist__item${row.id === selectedId ? " ag-hist__item--on" : ""}`}
            aria-current={row.id === selectedId ? "true" : undefined}
            onClick={() => onSelect(row.id)}
          >
            <span className="ag-hist__prompt">{row.excerpt}</span>
            <span className="ag-hist__meta" title={row.metaTitle}>
              {row.glyph === undefined ? null : (
                <span className={row.tone} aria-hidden="true">
                  {row.glyph}
                </span>
              )}
              {row.meta}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/** Static placeholders. Nothing pulses: the page does not pretend to progress
 * it cannot measure. */
function SidebarSkeleton({ label, widths }: { label: string; widths: readonly string[] }) {
  return (
    <div className="ag-side__skeleton">
      <span className="ne-visually-hidden" role="status">
        {label}
      </span>
      {widths.map((width) => (
        <span key={width} className="ne-skeleton" style={{ width }} aria-hidden="true" />
      ))}
    </div>
  );
}
