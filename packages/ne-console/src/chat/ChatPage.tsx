// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — ASK. The analyst, many times a day: ask a question, get a cited answer,
// open the citation, and be able to find yesterday's answer again.
//
// Three regions and nothing else (§1.4's work screen): the history pane at left,
// the thread at centre on a prose measure, and the source pane at right, which
// opens on demand and closes with Esc.
//
// WHAT LEFT THIS SCREEN (§20). The four hero counters over a cold session; the
// purpose line; the worked prompts labelled with what each DEMONSTRATES — a
// presenter's script sitting in the product; the browser-storage journal of
// questions this machine happened to see; the pinned lane of tool cards under
// the composer; and the in-flight strip that named the route the console was
// waiting on. A person who navigated here on purpose does not need the screen
// explained to them, and nobody needs a route name.
//
// THIN CLIENT (C7). Every state on this screen is a server verdict rendered:
// the refusal is a 403 the server returned to a request this console actually
// made, the tool decisions are the policy decision point's, and a citation opens
// through the governed retrieve, which re-checks the permission. Nothing here
// recomputes a verdict, and there is no path that could.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ApiClient } from "../api/client";
import type { Citation } from "../api/types";
import { principalOwnerReady, usePrincipalOwner } from "../auth/PrincipalContext";
import { useSurfaceAccess } from "../auth/useSurfaceAccess";
import { Empty, Refused } from "../primitives/states";
import { RunModelPicker } from "../models/RunModelPicker";
import { GroundingSwitch } from "../agents/GroundingSwitch";
import type { AgentRunTarget } from "../agents/types";
import { Transcript } from "../transcript";
import type { TranscriptTurn } from "../transcript";
import { ASK_EMPTY_THREAD, GROUNDING_BOUND_NOTE, SOURCE_VIEW } from "./askCopy";
import { CitationChip } from "./CitationChip";
import { MessageInput } from "./MessageInput";
import { MessageList } from "./MessageList";
import { readRunCitations, type RunCitationsResult } from "./runCitations";
import { SourcePane } from "./SourcePane";
import { useAgentStream } from "./useAgentStream";
import type { UiMessage } from "./useAgentStream";
import "./chat.css";

type GroundingCapabilityResult = {
  owner: AskOwner | null;
  settled: boolean;
  agentName: string | null;
};

type GroundingSelection = {
  owner: AskOwner | null;
  enabled: boolean;
};

type AskOwner = {
  client: ApiClient;
  principalId: string | null;
  generation: string;
};

/**
 * THE THREAD IS THE SCREEN.
 *
 * Ask is not a route any more and not a tab either: `/agents/:id` opens
 * directly into this thread, and `/ask` redirects onto the workspace of the one
 * Agent it always ran. SPEC has always said Ask is "a lightweight
 * route/projection onto one real configured default Q&A Agent... not a second
 * chat identity system"; the defect was that the projection was INVISIBLE, and
 * this is the last piece of making it legible.
 *
 * WHAT LEFT THIS COMPONENT. Its own history pane. It read chat SESSIONS through
 * a `listChatSessions` capability probe against a method `api/client.ts` does
 * not have, so it resolved to null on every load and only ever listed the
 * threads opened in the current tab — while the workspace's Conversations tab
 * listed the canonical Agent Conversations beside it. Two lists, one store:
 * `POST /v1/chat/sessions` mints a canonical Conversation and returns its id as
 * `session_id` (ne-api/routers/chat.py). The workspace sidebar now lists that
 * one store and hands the selection down as `conversationId`, and
 * chat/askHistory.ts, chat/useAskHistory.ts and chat/HistoryPane.tsx are
 * deleted.
 *
 * ONE DOCK, ONE SUBJECT. A citation and a turn's Run record are both detail
 * ABOUT the turn the reader is looking at, so they share the one docked pane
 * rather than navigating. When the workspace has a record open it hands the
 * pane down as `receiptPane` and this shows no source pane of its own.
 *
 * WHAT ELSE LEFT THIS COMPONENT. The "Context ledger" button and its panel. It
 * read `GET /v1/chat/sessions/{id}/context-ledger`, which resolves through the
 * LEGACY chat-session helper (`resolve_session`, ne-api/routers/chat.py) and
 * requires `conversation.agent_name` to be in the `chat_agents` allowlist. A
 * canonical Ask Conversation is stored under the catalog Agent id, never that
 * allowlist, so the call was a 404 on every session this screen can produce —
 * and the panel drew a generic load error with a Retry that could not succeed.
 * Repointing it would not have helped: the components it lists come from
 * `ne_agent_conversation_turns`, whose only writer the default-Ask fence blocks
 * outright, and its gauge, served model and flip placard read `session.run`
 * audit provenance that the canonical Run path never emits. The canonical
 * ledger route that DOES work is a different one
 * (`/v1/agents/{id}/conversations/{cid}/context-ledger`) over different facts,
 * and no console surface reads it. A dead control is worse than none.
 *
 * THIN CLIENT (C7). Every state on this screen is still a server verdict
 * rendered: the refusal is a 403 the server returned to a request this console
 * actually made, the tool decisions are the policy decision point's, and a
 * citation opens through the governed retrieve, which re-checks the permission.
 * Nothing here recomputes a verdict, and there is no path that could.
 */
export function ChatPage({
  agentId = null,
  conversationId = null,
  onConversationStarted,
  onOpenReceipt,
  receiptPane = null,
  capsNote = null,
  unavailableReason = null,
  pastTurns = null,
  agentLabel = null,
  runRouting = "default-ask",
  runTarget = null,
}: {
  /** The catalog Agent this thread projects — the identity the read-back
   * citation reads resolve against. `null` reads no citations for read-back
   * turns; the live stream's own citation read is unaffected. */
  agentId?: string | null;
  /** The workspace sidebar's selection: the canonical Conversation this thread
   * continues, or null for one that has not been opened yet. */
  conversationId?: string | null;
  /** The stream minted a Conversation. The workspace selects it and re-reads
   * its one history list. */
  onConversationStarted?: (conversationId: string) => void;
  /** Dock a turn's Run record in the pane beside the thread, in place. Handed
   * to every settled turn's footer rather than to one floating control above
   * the composer: the record belongs to the turn it is about, and a control
   * that only ever meant "the last run" could not say which turn that was. */
  onOpenReceipt?: (runId: string) => void;
  /** The docked pane's content when the workspace has a receipt open. While it
   * is non-null this renders no source pane of its own. */
  receiptPane?: ReactNode | null;
  /** The agent's SERVED execution caps, for the composer footer. */
  capsNote?: string | null;
  /** availability_reasons, verbatim. Non-null disables the composer. */
  unavailableReason?: string | null;
  /** The selected Conversation's canonical turns, read back by the workspace.
   * `null` means nothing is selected; an empty array means the server served no
   * turns for it, which is a different fact and renders as one. */
  pastTurns?: readonly TranscriptTurn[] | null;
  agentLabel?: string | null;
  /** WHICH DOOR THIS COMPOSER'S TURNS LEAVE THROUGH. "default-ask" is the
   * canonical chat wire — it reaches exactly the one activated default agent.
   * "agent-run" submits to the workspace agent's OWN canonical Run envelope;
   * while that envelope is not ready (`runTarget` null) the composer stays
   * CLOSED rather than silently falling back to the default agent, because a
   * turn typed at Customer Support must never run on the Analyst. */
  runRouting?: "default-ask" | "agent-run";
  runTarget?: AgentRunTarget | null;
}) {
  const principalOwner = usePrincipalOwner();
  const client = principalOwner.client;
  const principal = principalOwner.principal.principal;
  const principalId =
    principalOwnerReady(principalOwner) && principal !== null ? principal.id : null;
  const owner = useMemo<AskOwner>(
    () => ({ client, principalId, generation: principalOwner.generation }),
    [client, principalId, principalOwner.generation],
  );
  const currentOwnerRef = useRef(owner);
  const capabilityBindingRef = useRef({ client, principalId });
  useLayoutEffect(() => {
    currentOwnerRef.current = owner;
  }, [owner]);
  const access = useSurfaceAccess();
  const [groundingCapabilityResult, setGroundingCapabilityResult] =
    useState<GroundingCapabilityResult>({ owner, settled: false, agentName: null });
  const [groundingSelection, setGroundingSelection] = useState<GroundingSelection>({
    owner,
    enabled: false,
  });
  // Every served result belongs to exact client + ready principal. The render
  // that observes either half change withdraws it before passive cleanup runs.
  // The one exception is the first `/me` settlement for the same client: a
  // capability read made while that client was resolving may cross only from
  // its unbound generation into its first ready generation. A later ready
  // principal can never consume another ready principal's result.
  const capabilityOwnerMatches =
    groundingCapabilityResult.owner === owner ||
    (groundingCapabilityResult.owner?.client === owner.client &&
      groundingCapabilityResult.owner.principalId === null &&
      owner.principalId !== null);
  const currentGroundingCapability = capabilityOwnerMatches
    ? groundingCapabilityResult
    : { owner, settled: false, agentName: null };
  const groundingCapabilityPending = !currentGroundingCapability.settled;
  const groundedAgentName = currentGroundingCapability.agentName;
  const groundingEnabled =
    owner !== null && groundingSelection.owner === owner ? groundingSelection.enabled : false;
  const setGroundingEnabled = useCallback(
    (enabled: boolean) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      setGroundingSelection({ owner, enabled });
    },
    [owner],
  );
  // The agent-run door ignores the chat agent name entirely; grounding is a
  // default-Ask mode and never re-routes a non-default agent's composer.
  const runRouted = runRouting === "agent-run";
  const { messages, busy, refused, send, stop, startedAt, lastSignalAt, loadThread } =
    useAgentStream(
      client,
      owner?.generation ?? null,
      !runRouted && groundingEnabled && groundedAgentName !== null
        ? groundedAgentName
        : "console-chat",
      runRouted ? runTarget : null,
    );

  // THE SELECTION IS THE WORKSPACE'S, NOT THIS COMPONENT'S. The sidebar owns
  // which Conversation is open; this holds only the id the STREAM minted, so a
  // turn sent before the URL has caught up still goes into the right thread.
  const [mintedState, setMintedState] = useState<{
    owner: AskOwner | null;
    value: string | undefined;
  }>({ owner, value: undefined });
  const minted = owner !== null && mintedState.owner === owner ? mintedState.value : undefined;
  const sessionId = conversationId ?? minted;
  const setMinted = useCallback(
    (value: string | undefined) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      setMintedState({ owner, value });
    },
    [owner],
  );
  const [draftState, setDraftState] = useState({ owner, value: "" });
  const draft = owner !== null && draftState.owner === owner ? draftState.value : "";
  const setDraft = useCallback(
    (value: string) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      setDraftState({ owner, value });
    },
    [owner],
  );
  const handleDraftChange = useCallback(
    (value: string) => {
      setDraft(
        !runRouted && sessionId === undefined && groundingCapabilityPending ? draft + value : value,
      );
    },
    [draft, groundingCapabilityPending, runRouted, sessionId, setDraft],
  );
  const [requestedModelState, setRequestedModelState] = useState<{
    owner: AskOwner | null;
    value: string | null;
  }>({ owner, value: null });
  const requestedModel =
    owner !== null && requestedModelState.owner === owner ? requestedModelState.value : null;
  const setRequestedModel = useCallback(
    (value: string | null) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      setRequestedModelState({ owner, value });
    },
    [owner],
  );
  const [modelChoicesState, setModelChoicesState] = useState({ owner, ready: false });
  const modelChoicesReady =
    owner !== null && modelChoicesState.owner === owner && modelChoicesState.ready;
  const setModelChoicesReady = useCallback(
    (ready: boolean) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      setModelChoicesState({ owner, ready });
    },
    [owner],
  );
  const [modelChoiceRevisionState, setModelChoiceRevisionState] = useState({ owner, value: 0 });
  const modelChoiceRevision =
    owner !== null && modelChoiceRevisionState.owner === owner ? modelChoiceRevisionState.value : 0;
  const setModelChoiceRevision = useCallback(
    (update: (previous: number) => number) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      setModelChoiceRevisionState((previous) => ({
        owner,
        value: update(previous.owner === owner ? previous.value : 0),
      }));
    },
    [owner],
  );
  const [selectedState, setSelectedState] = useState<{
    owner: AskOwner | null;
    value: Citation | null;
  }>({ owner, value: null });
  const selected = owner !== null && selectedState.owner === owner ? selectedState.value : null;
  const setSelected = useCallback(
    (value: Citation | null) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      setSelectedState({ owner, value });
    },
    [owner],
  );
  const selectionGeneration = useRef(0);
  useLayoutEffect(() => {
    selectionGeneration.current += 1;
  }, [owner]);
  useEffect(() => {
    const previousBinding = capabilityBindingRef.current;
    capabilityBindingRef.current = { client, principalId };
    // The initial request may start while /me is resolving. Its result is
    // rebound below to that same client's first ready principal, so starting a
    // duplicate request for the loading -> ready transition would create an
    // unnecessary race. Ready -> ready identity changes still refetch.
    if (
      previousBinding.client === client &&
      previousBinding.principalId === null &&
      principalId !== null
    ) {
      return;
    }
    let mounted = true;
    void client
      .chatCapabilities()
      .then((capabilities) => {
        if (!mounted) return;
        const served = capabilities.grounded?.agent_name.trim() ?? "";
        setGroundingCapabilityResult({
          owner,
          settled: true,
          agentName: served === "" ? null : served,
        });
      })
      .catch(() => {
        if (!mounted) return;
        setGroundingCapabilityResult({ owner, settled: true, agentName: null });
      });
    return () => {
      const currentOwner = currentOwnerRef.current;
      const sameClientFirstPrincipal =
        owner.client === currentOwner.client &&
        owner.principalId === null &&
        currentOwner.principalId !== null;
      if (!sameClientFirstPrincipal) mounted = false;
    };
  }, [client, owner, principalId]);

  const invalidateModelChoices = useCallback(() => {
    // The selection and the evidence that offered it disappear in the same
    // event as the server's definitive refusal/unavailable response.
    setRequestedModel(null);
    setModelChoicesReady(false);
    setModelChoiceRevision((revision) => revision + 1);
  }, [setModelChoiceRevision, setModelChoicesReady, setRequestedModel]);

  const invalidateGroundingCapability = useCallback(() => {
    if (owner === null || currentOwnerRef.current !== owner) return;
    setGroundingCapabilityResult({ owner, settled: true, agentName: null });
    setGroundingEnabled(false);
  }, [owner, setGroundingEnabled]);

  // THE SELECTION IS AUTHORITATIVE. When the sidebar moves to another
  // Conversation the live thread is cleared and the workspace's read-back turns
  // take the screen. The one exception is the Conversation this component just
  // minted: the URL catching up with the stream is not a selection change, and
  // clearing there would wipe the turn the reader is watching arrive.
  const mintedRef = useRef<string | undefined>(undefined);
  useLayoutEffect(() => {
    mintedRef.current = minted;
  }, [minted]);
  useEffect(() => {
    if (owner === null) return;
    if (conversationId !== null && conversationId === mintedRef.current) return;
    selectionGeneration.current += 1;
    setMinted(undefined);
    loadThread([]);
  }, [conversationId, loadThread, owner, setMinted]);

  const handleSubmit = useCallback(
    // `resendKey` is present only on a RETRY of an existing turn, and carries
    // that turn's frozen idempotency key so the resend joins its run instead of
    // minting another. A new question passes none and one is minted for it.
    (prompt: string, resendKey?: string) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      // send() never rejects (the bootstrap is inside its guard); the void +
      // catch is the structural backstop so a DOM handler cannot float one.
      // FAIL CLOSED, NEVER FALL BACK: an agent-run composer with no ready
      // envelope submits nothing — a turn typed at this agent must not run on
      // the default agent instead.
      if (runRouted && runTarget === null) return;
      if (
        !modelChoicesReady ||
        (!runRouted &&
          ((sessionId === undefined && groundingCapabilityPending) ||
            (groundingEnabled && groundedAgentName === null)))
      )
        return;
      void send(
        sessionId,
        prompt,
        requestedModel ?? undefined,
        invalidateModelChoices,
        !runRouted && groundingEnabled ? invalidateGroundingCapability : undefined,
        resendKey,
      )
        .then((sid) => {
          if (sid === undefined || currentOwnerRef.current !== owner) return;
          setMinted(sid);
          onConversationStarted?.(sid);
        })
        .catch(() => {
          /* unreachable: send never rejects — this catch is the backstop. */
        });
    },
    [
      groundingEnabled,
      groundingCapabilityPending,
      groundedAgentName,
      invalidateGroundingCapability,
      invalidateModelChoices,
      modelChoicesReady,
      onConversationStarted,
      requestedModel,
      owner,
      runRouted,
      runTarget,
      send,
      setMinted,
      sessionId,
    ],
  );

  const handleViewSource = useCallback(
    (citation: Citation) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      setSelected(citation);
    },
    [owner, setSelected],
  );

  const handleThumbs = useCallback(
    async (message: UiMessage, up: boolean) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      const sid = message.sessionId;
      const messageRef = message.messageId;
      if (sid === undefined || messageRef === undefined) return;
      await client.signalThumbs(sid, {
        up,
        target_ref: `chat:${sid}:${messageRef}`,
        message_id: messageRef,
      });
      if (currentOwnerRef.current !== owner) return;
    },
    [client, owner],
  );

  const handleFlagSource = useCallback(
    async (message: UiMessage, citation: Citation) => {
      if (owner === null || currentOwnerRef.current !== owner) return;
      const sid = message.sessionId;
      const messageRef = message.messageId;
      if (sid === undefined || messageRef === undefined) return;
      await client.signalWrongSource(sid, {
        answer_ref: `chat:${sid}:${messageRef}`,
        citation_ref: citation.chunk_id,
        message_id: messageRef,
      });
      if (currentOwnerRef.current !== owner) return;
    },
    [client, owner],
  );

  // ------------------------------------------------------------- read-back --
  //
  // ONE TURN RENDERS ONCE. The same turn can reach this screen twice: the LIVE
  // copy this session streamed and is polling to settlement, and the
  // conversation store's READ-BACK copy the workspace fetched when the sidebar
  // selection caught up with the stream. The read-back copy is a snapshot taken
  // at acceptance, so it said "Submitted · the Run has not finished" directly
  // above the finished answer — with the question bubble drawn twice. The live
  // copy is the same Run said more currently (its poll follows the Run to its
  // terminal state), so where both exist the read-back copy yields.
  const liveRunIds = useMemo(() => {
    const ids = new Set<string>();
    for (const message of messages) {
      if (message.runRef !== undefined) ids.add(message.runRef.runId);
    }
    return ids;
  }, [messages]);
  const readBackTurns = useMemo(
    () =>
      pastTurns === null
        ? null
        : pastTurns.filter((turn) => turn.runId === undefined || !liveRunIds.has(turn.runId)),
    [liveRunIds, pastTurns],
  );

  // THE READ-BACK ANSWER'S SOURCES, through the SAME governed reads the live
  // thread uses (chat/runCitations `readRunCitations`): the evidence envelope
  // decides which chunks are cited — a citation only where the server
  // associated the claim with the evidence — and the authorized retrieval
  // decides whether this reader may still open them. One read per Run per
  // selection; a failed read stores nothing and invents no absence sentence.
  const [readBackSources, setReadBackSources] = useState<{
    owner: AskOwner | null;
    scope: string;
    value: ReadonlyMap<string, RunCitationsResult>;
  }>({ owner, scope: conversationId ?? "", value: new Map() });
  const sourceScope = conversationId ?? "";
  const attemptedSourceReads = useRef(new Set<string>());
  useEffect(() => {
    attemptedSourceReads.current = new Set();
  }, [owner, sourceScope]);
  useEffect(() => {
    if (owner === null || agentId === null || readBackTurns === null) return;
    for (const turn of readBackTurns) {
      const runId = turn.runId;
      if (runId === undefined) continue;
      // Only an answer that is actually on screen names its sources. A refusal
      // and a failed turn have no answer body and cite nothing.
      if (!turn.answer || turn.answer.body === "") continue;
      const attemptKey = `${sourceScope}:${runId}`;
      if (attemptedSourceReads.current.has(attemptKey)) continue;
      attemptedSourceReads.current.add(attemptKey);
      void readRunCitations(client, agentId, runId).then(
        (result) => {
          if (result === null || currentOwnerRef.current !== owner) return;
          setReadBackSources((previous) => {
            const base =
              previous.owner === owner && previous.scope === sourceScope
                ? previous.value
                : new Map<string, RunCitationsResult>();
            const next = new Map(base);
            next.set(runId, result);
            return { owner, scope: sourceScope, value: next };
          });
        },
        () => {
          // Fail-closed and silent: no chip, no invented absence sentence.
        },
      );
    }
  }, [agentId, client, owner, readBackTurns, sourceScope]);

  const displayTurns = useMemo(() => {
    if (readBackTurns === null) return null;
    const sources =
      readBackSources.owner === owner && readBackSources.scope === sourceScope
        ? readBackSources.value
        : null;
    if (sources === null || sources.size === 0) return readBackTurns;
    return readBackTurns.map((turn): TranscriptTurn => {
      const extra = turn.runId === undefined ? undefined : sources.get(turn.runId);
      if (extra === undefined) return turn;
      return {
        ...turn,
        ...(extra.citations.length === 0 || !turn.answer
          ? {}
          : {
              answer: {
                ...turn.answer,
                trailing: (
                  <>
                    {extra.citations.map((citation, index) => (
                      <CitationChip
                        key={citation.chunk_id}
                        index={index + 1}
                        citation={citation}
                        onSelect={handleViewSource}
                      />
                    ))}
                  </>
                ),
              },
              sources: extra.citations.map((citation, index) => ({
                key: citation.chunk_id,
                ordinal: index + 1,
                system: citation.source_system,
                document: citation.document_id,
                actions: (
                  <span className="ne-ask__citeactions">
                    <button
                      type="button"
                      className="ne-ask__quiet"
                      aria-label={`View source for citation ${index + 1}`}
                      onClick={() => handleViewSource(citation)}
                    >
                      {SOURCE_VIEW}
                    </button>
                  </span>
                ),
              })),
            }),
        ...(extra.note === null ? {} : { sourcesNote: extra.note }),
        // The row's identity, on the read-back copy exactly as on the live one:
        // each verified row's served canonical hash, drawn in the source block.
        ...(extra.rowAnchors.length === 0
          ? {}
          : {
              sourceAnchors: extra.rowAnchors.map((row) => ({
                key: row.evidenceId,
                anchor: row.anchor,
              })),
            }),
      };
    });
  }, [handleViewSource, owner, readBackSources, readBackTurns, sourceScope]);

  // An agent-run composer with no ready envelope is CLOSED, not re-routed:
  // the parts (platform hash, capability snapshot) arrive within the
  // workspace's own loads, and until they do a submit has nowhere honest to go.
  const composerDisabled = unavailableReason !== null || (runRouted && runTarget === null);
  // The grounding-capability read gates only the default-Ask door; the
  // agent-run door neither offers grounding nor waits on its capability read.
  const groundingGatePending = !runRouted && sessionId === undefined && groundingCapabilityPending;
  // WHICH PRECONDITION IS MISSING — said out loud, in the order the composer
  // itself refuses in.
  //
  // Every arm below was a silent lock. The envelope arm is the worst of them:
  // it is reached from FOUR separate reads (the shared posture read for the
  // platform configuration hash, the workspace's own agent read, its capability
  // preflight, and the principal generation all of them are keyed to), any of
  // which can settle late or fail, and none of which put a word on this screen
  // when they did. A reader saw a composer that worked, typed into it, and
  // found that Enter did nothing — and because the draft is retained, nothing
  // on the screen changed to mark the moment it stopped working.
  //
  // The unavailable arm is deliberately absent: `unavailableReason` already
  // renders VERBATIM in its own banner above the thread, and repeating the
  // server's sentence twice on one screen is noise, not clarity.
  const composerBlockedReason =
    unavailableReason !== null
      ? null
      : runRouted && runTarget === null
        ? "This agent's run envelope hasn't loaded, so nothing can be sent yet — the composer stays closed rather than routing this turn to a different agent."
        : groundingGatePending
          ? "Checking how this deployment answers — your question is held and will send once that read settles."
          : !modelChoicesReady
            ? "No model is ready for this run yet, so this question cannot be sent."
            : null;
  const placeholder = agentLabel === null ? "Ask a question" : `Ask ${agentLabel}…`;

  return (
    <div className={`ag-main${receiptPane === null && selected === null ? "" : " ag-main--pane"}`}>
      <section className="ag-thread" aria-label="Conversation">
        {unavailableReason === null ? null : (
          <div className="ag-composer">
            <p className="ag-unavail" role="status">
              <span aria-hidden="true">⚠</span>
              <span>{unavailableReason}</span>
            </p>
          </div>
        )}

        {refused ? (
          <Refused thing="Agents" page="Agents" />
        ) : (
          <>
            <div className="ne-ask__scroll">
              {/* The selected Conversation's canonical turns, read back by the
               * workspace — minus any turn the live thread below already holds
               * (one turn renders once, in its current state), plus each
               * answer's sources read off its Run. Anything sent since streams
               * in below them. */}
              {displayTurns !== null && displayTurns.length > 0 ? (
                <Transcript turns={displayTurns} label="Conversation" />
              ) : null}
              {messages.length === 0 && (displayTurns === null || displayTurns.length === 0) ? (
                <Empty message={ASK_EMPTY_THREAD} />
              ) : null}
              {messages.length === 0 ? null : (
                <MessageList
                  messages={messages}
                  onViewSource={handleViewSource}
                  onThumbs={handleThumbs}
                  onFlagSource={handleFlagSource}
                  onRetry={handleSubmit}
                  opsReadable={access.access("ops") === "granted"}
                  progress={startedAt === null ? null : { startedAt, lastSignalAt }}
                  {...(onOpenReceipt ? { onOpenReceipt } : {})}
                />
              )}
            </div>
            <MessageInput
              busy={busy}
              disabled={composerDisabled}
              placeholder={placeholder}
              capsNote={capsNote}
              value={groundingGatePending ? "" : draft}
              onChange={handleDraftChange}
              onSubmit={handleSubmit}
              onStop={stop}
              controls={
                <>
                  <RunModelPicker
                    surface="ask"
                    value={requestedModel}
                    onChange={setRequestedModel}
                    onReadinessChange={setModelChoicesReady}
                    disabled={busy || composerDisabled || groundingGatePending}
                    invalidationKey={modelChoiceRevision}
                  />
                  {/* THE GROUNDING CONTROL NEVER SILENTLY VANISHES (owner
                   * defect, twice). While no conversation is open it is the
                   * live choice. Once one IS open the choice is BOUND — the
                   * agent name is handed to the one createSession call and to
                   * nothing after it (useAgentStream), so there is no wire on
                   * which to flip it mid-conversation — and the segment stays
                   * on screen DISABLED, showing the bound state, with the
                   * reason on hover. The bound position is a served fact, not
                   * a guess: a conversation this tab minted shows the mode it
                   * was minted with, and a read-back conversation from the
                   * workspace sidebar is a canonical Ask conversation, which
                   * the server creates only on the ungrounded default-Ask path
                   * (ne-api routers/chat.py create_session — grounded sessions
                   * run the legacy store and are never listed there). */}
                  {!runRouted && groundedAgentName !== null ? (
                    sessionId === undefined ? (
                      <GroundingSwitch
                        grounded={groundingEnabled}
                        disabled={busy || composerDisabled}
                        onChange={setGroundingEnabled}
                      />
                    ) : (
                      <GroundingSwitch
                        grounded={minted === sessionId && groundingEnabled}
                        disabled
                        onChange={setGroundingEnabled}
                        title={GROUNDING_BOUND_NOTE}
                      />
                    )
                  ) : null}
                </>
              }
              modelReady={modelChoicesReady && !groundingGatePending}
              blockedReason={composerBlockedReason}
            />
          </>
        )}
      </section>

      {/* ONE DOCK, ONE SUBJECT. The workspace's receipt wins the slot while it
       * is open — a citation and a Run receipt are both detail about the same
       * turn, and two panes side by side would be two docks. */}
      {receiptPane}
      {receiptPane === null && selected !== null ? (
        <SourcePane
          key={selected.chunk_id}
          citation={selected}
          client={client}
          principalId={owner?.principalId ?? null}
          ownerGeneration={owner?.generation}
          onClose={() => setSelected(null)}
        />
      ) : null}
    </div>
  );
}
