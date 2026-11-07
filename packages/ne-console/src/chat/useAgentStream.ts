// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The turn: session bootstrap plus the streamed message, reduced into one
// assistant message. Token deltas accumulate, a citations frame attaches
// citations, a tool frame lands on the turn it belongs to, done finalizes, and
// an error is classified before anything renders it. An AbortController cancels
// the stream on unmount so no dangling fetch survives navigation.
//
// `send` NEVER rejects. The session bootstrap lives INSIDE send's
// try/catch/finally so the whole createSession→sendMessage turn is
// abort/unmount-aware: an aborted or unmounted turn settles silently, and a real
// failure lands on the same classified state as a stream error rather than
// floating an unhandled rejection out of a DOM handler.
//
// TOOL CALLS BELONG TO THEIR TURN (§5). They used to live in a separate lane
// pinned under the composer, cleared at the start of every turn — so a decision
// taken during the second question erased the record of the first. §5 puts them
// in the thread, in sequence, and says the pending row "keeps its pill for the
// life of the thread", which is only true if the row is part of the thread.
//
// PER-CALL IDENTITY: every governed transit gets its own key — approval_id >
// audit_seq > per-turn arrival index. Two calls to the same tool in one turn are
// two audit-chain events with distinct audit_seq and stay two rows. Only an
// exact re-delivery of the same event replaces its row in place.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import { SseError } from "../api/sse";
import { DEFAULT_ASK_AGENT_ID } from "../api/types";
import type {
  AgentRunDetailWire,
  AgentRunStatusWire,
  Citation,
  StreamEvent,
  ToolTransitEvent,
} from "../api/types";
import type { TranscriptStep } from "../transcript";
import { provenanceOf } from "./provenance";
import type { AnswerProvenance } from "./provenance";
import {
  abortFailure,
  classifyTurnFailure,
  declinedFailure,
  groundingUnavailableFailure,
  isModelSelectionVerdict,
  modelSelectionFailure,
  runIncompleteFailure,
  runUnreadableFailure,
  streamEndedFailure,
  thrownTransportFailure,
  withheldFailure,
} from "./turnFailure";
import type { TurnFailure } from "./turnFailure";
import { runActivityElapsedSeconds, runPlanAbsentNote, runTurnSteps } from "./runActivity";
import { readRunCitations } from "./runCitations";
import type { VerifiedRowAnchor } from "../agents/rowAnchors";
import type { AgentRunTarget } from "../agents/types";
import { useRunCompletion } from "./useRunCompletion";
import type { RunPollOutcome } from "./useRunCompletion";

/** One PDP transit paired with its per-call identity key. */
export interface ToolTransitEntry {
  key: string;
  event: ToolTransitEvent;
}

/** One server frame in the exact order this browser received it. Adjacent
 * token frames may coalesce, but no different frame kind is bucketed around
 * them. The terminal carries only fields the done event actually served. */
export type TurnStreamItem =
  | { key: string; kind: "answer"; text: string }
  | { key: string; kind: "tool"; entry: ToolTransitEntry }
  | { key: string; kind: "citations"; citations: Citation[] }
  | { key: string; kind: "terminal"; outcome: "complete" | "failed"; traceId?: string };

export interface UiMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations: Citation[];
  streaming: boolean;
  /** Governed tool calls that transited during this turn, in arrival order. */
  tools: ToolTransitEntry[];
  /** Server frames in authoritative arrival order. Absent on stored legacy
   * turns whose history API does not serve frame-level evidence. */
  streamItems?: TurnStreamItem[];
  /** The alias sent by this requester. This is never a served-model claim. */
  requestedModel?: string;
  /** An ordinary run correlation from done.trace_id, independent of whether
   * the terminal also carried structural answer provenance. */
  traceId?: string;
  /** Opaque server-owned attachment id for correction signals. */
  messageId?: string;
  /** Latest material server transition for the transcript's one polite region. */
  announcement?: string;
  /** Structural terminal-frame provenance. Absent means the server made no
   * verified/grounded claim; ordinary generation must remain unbadged. */
  provenance?: AnswerProvenance;
  /**
   * A CLASSIFIED failure — never the server's raw string in answer typography.
   * The verbatim detail survives inside it (see chat/turnFailure.ts).
   */
  failure?: TurnFailure;
  /** The prompt that produced this turn, so a failed turn can be retried. */
  prompt?: string;
  /** The session that produced this message, stamped at send time so the
   * thumbs / wrong-source signals carry the exact session. */
  sessionId?: string;
  /**
   * THE KEY THIS TURN WAS BORN WITH, so a retry can resend the same turn
   * instead of minting a new run for it. Stamped once at send time and never
   * rewritten: the whole value of the key is that it does not change.
   */
  idempotencyKey?: string;

  // ---- the canonical Run this turn became -------------------------------
  //
  // Canonical Ask does not answer on the stream: it accepts the question, names
  // a Run, and closes. Everything below is what the console then READ BACK off
  // that Run, and each field is exactly one thing a server said. None of them
  // is derived from another, because the whole defect being fixed here was a
  // screen inferring "nothing was produced" from "I was not shown anything".

  /** The Run the acceptance named — the identity the poller reads and the two
   * links (Open Run, Review in Approvals) resolve against. */
  runRef?: { agentId: string; runId: string };
  /** The Run's last observed status. `waiting_approval` is the paused turn. */
  runStatus?: AgentRunStatusWire;
  /** The server's own verdict word on the settled Run. Rendered as a fact,
   * never used to decide whether the answer may be shown — that decision is
   * ne-api's and was already taken by the time this arrives. */
  runVerdict?: "verified" | "grounded" | "refused" | "unavailable";
  /** Whether the answer was disclosed, withheld, or could not be resolved.
   * THE FIELD THAT KEEPS "nothing was produced" APART FROM "you may no longer
   * read what was produced" — one blank used to say both. */
  outputState?: "available" | "withheld" | "unavailable";
  /** The approval this paused turn is parked on, when the Run named one. */
  approvalId?: string;
  /** The model the RUN reports as served. The chat stream names none, so this
   * is the first honest value the receipt's model slot has ever had; absent
   * still renders the em dash and its note. */
  servedModel?: string;
  /** WHERE IN THE ROUTE the served model sat, straight off the Run. The
   * receipt used to hold two model names and no relationship between them; this
   * is the relationship. Absent means the Run reported `"unavailable"` — the
   * receipt then states the divergence without claiming a cause. */
  routePosition?: "primary" | "fallback";
  /** What was passed over before the model that served, when the Run named it.
   * Declared model names only; the server never puts provider error text here. */
  fallbackFrom?: string;
  /**
   * THE RUN'S RECORDED ACTIVITY, as band rows — what makes a working turn
   * legible instead of a counter over a blank screen.
   *
   * One row per event the Run's own log has recorded SO FAR (chat/
   * runActivity.ts builds them; nothing here derives one). It grows while the
   * turn works and it is the reason a cancelled Run can say what step it
   * reached: the rows that landed before it died stay on the screen.
   *
   * Absent or empty means the server has recorded nothing yet, and the band
   * then draws nothing — never a placeholder, never a guessed step.
   */
  runSteps?: readonly TranscriptStep[];
  /**
   * WHY `runSteps` LISTS NO TOOL CALL, when it lists none — the band's note.
   *
   * The Run's tool plan is all-or-nothing (chat/askCopy RUN_PLAN_UNAVAILABLE),
   * and the event log cannot stand in for it: it carries no tool name. So the
   * ledger gets thinner and SAYS so. Absent means the plan was served fine and
   * there is nothing to explain.
   */
  runStepsNote?: string | null;
  /**
   * HOW MUCH OF THE WAIT THE RUN HAS ACCOUNTED FOR, in seconds — the band
   * summary's clock.
   *
   * First recorded event to last recorded event, both stamped by the SERVER
   * (chat/runActivity `runActivityElapsedSeconds`). It is deliberately a
   * different number from `TurnProgress`'s elapsed, which is this machine's
   * clock since the question was sent: the difference between the two is time
   * the Run has not yet recorded anything for, and a reader watching a long
   * turn is entitled to see both. Absent until the Run has recorded two events,
   * because one stamp spans nothing.
   */
  runActivitySeconds?: number | null;
  /**
   * WHY THE SOURCE LIST IS EMPTY OR SHORT — the server's reason, not a guess.
   *
   * Produced by chat/runCitations `citationsNote`. Absent means there is
   * nothing to explain: either every cited source is listed, or the turn is a
   * refusal, which cites nothing because it answered nothing and already says
   * so on its own card.
   */
  citationsNote?: string | null;
  /**
   * THE CITATION READ IS STILL IN FLIGHT — the third state of the source block.
   *
   * Set in the same patch that settles the turn (the moment the citation read
   * is issued) and cleared on every way that read can end: it resolved, it
   * failed (fail-closed and silent, exactly as before — no chip, no invented
   * absence sentence), or it outlived `CITATION_READ_DEADLINE_MS`. So it can
   * never spin forever, and while it is true the thread may say "no citations
   * YET" — which is a different fact from "the read landed and found none"
   * (`citationsNote`) and is drawn differently (transcript sourcesPending).
   */
  citationsPending?: boolean;
  /**
   * THE SERVED ROW ANCHORS — the canonical hash of each verified row the
   * answer's claims are associated with (chat/runCitations, off the evidence
   * envelope). A verified row has no passage and can never become a chip; its
   * anchor renders in the source block as the row's identity. Absent until the
   * citations read lands; empty means the envelope carried none.
   */
  rowAnchors?: readonly VerifiedRowAnchor[];
}

/**
 * Fold one settled Run into the turn that produced it. PURE, and exported for
 * its own test: every rule below is assertable without a DOM, a clock or a
 * fetch.
 *
 * THE ORDER OF THE BRANCHES IS THE POLICY, and it is the whole point of the
 * fix. A turn that produced text renders the text. A turn the agent DECLINED
 * renders as a decision. A turn whose answer is WITHHELD says so. Only a turn
 * that genuinely produced nothing renders as empty. Those are four different
 * facts and the screen used to draw all four as a blank rectangle.
 */
export function applySettledRun(message: UiMessage, run: AgentRunDetailWire): UiMessage {
  const base: UiMessage = {
    ...message,
    streaming: false,
    runStatus: run.status,
    ...(run.output_verdict === null ? {} : { runVerdict: run.output_verdict }),
    outputState: run.output_state,
    ...(run.served_model === null ? {} : { servedModel: run.served_model }),
    // `"unavailable"` is dropped rather than carried: the receipt has one
    // branch for "the Run stated the routing" and one for "it did not", and an
    // absent field is the honest spelling of the second.
    ...(run.route_position === "unavailable" ? {} : { routePosition: run.route_position }),
    ...(run.fallback_from === null ? {} : { fallbackFrom: run.fallback_from }),
  };
  if (run.status !== "completed") {
    // A terminal Run that is not `completed` did not produce an answer. The
    // status word says WHAT happened; `failure_reason` is the reason the server
    // explicitly marked publishable and says WHY, where there is one. Nothing
    // here translates either into a cause of its own — and where the server
    // published no reason, the reader gets the status word and no invention.
    return {
      ...base,
      failure: runIncompleteFailure(run.status, run.failure_reason),
      announcement: "Run failed.",
    };
  }
  const text = run.output_preview ?? "";
  if (run.output_verdict === "refused") {
    // A DECLINE IS A CORRECT OUTCOME. The declining agent's own words are the
    // reason on the card, verbatim — they are NOT promoted into answer
    // typography, because they are an account of a decision, not an answer.
    return { ...base, text: "", failure: declinedFailure(text), announcement: "Run declined." };
  }
  if (run.output_state === "withheld") {
    return { ...base, text: "", failure: withheldFailure(), announcement: "Answer withheld." };
  }
  // Completed with nothing to show falls through with empty text: MessageList's
  // existing empty-answer rule states "No answer was produced." That branch is
  // reached ONLY here, after the three above have each been ruled out.
  //
  // The provenance chip is carried from the RUN's verdict, which is the same
  // server vocabulary the terminal frame's is (`verified` / `grounded`), read
  // off the same finalization. `refused` is deliberately absent: it is a
  // decision and renders as the decline card above, and the chip's own third
  // kind asserts UNGROUNDEDNESS, which a Run verdict does not claim.
  const provenance: AnswerProvenance | undefined =
    run.output_verdict === "verified" || run.output_verdict === "grounded"
      ? { kind: run.output_verdict }
      : undefined;
  return {
    ...base,
    text: text || message.text,
    ...(provenance ? { provenance } : {}),
    announcement: "Run complete.",
  };
}

let counter = 0;
function nextId(): string {
  counter += 1;
  return `m${counter}`;
}

/**
 * The per-CALL identity of a tool transit. Priority, each strictly narrower:
 *   1. `approval:<approval_id>` — the same gate request; a re-delivered pending
 *      for it wins latest.
 *   2. `seq:<audit_seq>` — unique per governed event on the hash chain, so
 *      distinct calls never collide.
 *   3. `arrival:<arrival>` — a per-turn index that never collapses, so two
 *      null-seq null-approval calls to one tool stay distinct.
 * Pure: StrictMode double-invokes setState updaters, so this is side-effect free.
 */
export function transitKey(event: ToolTransitEvent, arrival: number): string {
  if (event.approval_id) return `approval:${event.approval_id}`;
  if (event.audit_seq != null) return `seq:${event.audit_seq}`;
  return `arrival:${arrival}`;
}

/**
 * Fold one transit into a turn's list, latest-wins IN PLACE: a new key appends
 * (preserving arrival order), a known key replaces at its index so the row order
 * never shuffles. Pure — safe under StrictMode's double-invoke.
 */
export function applyToolTransit(
  prev: readonly ToolTransitEntry[],
  event: ToolTransitEvent,
  arrival: number,
): ToolTransitEntry[] {
  const key = transitKey(event, arrival);
  const idx = prev.findIndex((entry) => entry.key === key);
  if (idx === -1) return [...prev, { key, event }];
  const next = [...prev];
  next[idx] = { key, event };
  return next;
}

/** Fold a tool frame into the ordered event stream. Only an exact lifecycle
 * identity replaces in place; a same-name call is always a separate frame. */
export function applyOrderedToolTransit(
  prev: readonly TurnStreamItem[],
  event: ToolTransitEvent,
  arrival: number,
): TurnStreamItem[] {
  const entry = { key: transitKey(event, arrival), event };
  const key = `tool:${entry.key}`;
  const idx = prev.findIndex((item) => item.kind === "tool" && item.key === key);
  const item: TurnStreamItem = { key, kind: "tool", entry };
  if (idx === -1) return [...prev, item];
  const next = [...prev];
  next[idx] = item;
  return next;
}

/** Adjacent token chunks are one readable answer segment. A tool, citation, or
 * terminal between chunks remains between them. */
export function appendAnswerDelta(
  prev: readonly TurnStreamItem[],
  delta: string,
  arrival: number,
): TurnStreamItem[] {
  if (delta === "") return [...prev];
  const last = prev.at(-1);
  if (last?.kind === "answer") {
    return [...prev.slice(0, -1), { ...last, text: last.text + delta }];
  }
  return [...prev, { key: `answer:${arrival}`, kind: "answer", text: delta }];
}

function materialToolAnnouncement(event: ToolTransitEvent): string {
  if (event.decision === "pending") return `Approval required for ${event.name}.`;
  if (event.decision === "allow" && event.approval_id) {
    // Not "run resumed": the resume executes this one released call and the
    // run ends there. The announcement names the act, like the row's label.
    return `Approval granted for ${event.name}; the approved action ran.`;
  }
  if (event.decision === "rejected") return `Approval rejected for ${event.name}.`;
  if (event.decision === "expired") return `Approval expired for ${event.name}.`;
  if (event.decision === "escalated") return `Approval escalated for ${event.name}.`;
  if (event.decision === "integrity-failed") {
    return `Approval integrity check failed for ${event.name}.`;
  }
  if (event.decision === "approval-failed") {
    return `Approval did not proceed for ${event.name}.`;
  }
  if (event.decision === "failed") {
    return `Approved action ${event.name} failed during execution.`;
  }
  if (event.decision === "deny") return `Tool denied: ${event.name}.`;
  return `Tool allowed: ${event.name}.`;
}

/**
 * THE TURN DEADLINE — a backstop on the CONNECTION, and never a verdict on the
 * turn.
 *
 * WHY IT IS NO LONGER 120 s. The claim above it ("the deployment serves no chat
 * deadline") was wrong: the deployment serves `NE_API_TURN_TIMEOUT_S: "300"`
 * (deploy/compose/pilot/compose.yaml), so a 120 s console clock pre-empted the
 * server's own timeout by three minutes and the server's stated verdict could
 * never arrive. An owner's genuinely multi-hop question was abandoned here at
 * 120 s while the server ran it to SUCCESS — six times over, once per resend,
 * the last finishing four minutes before he gave up on a screen that had
 * already discarded every one of them.
 *
 * So this now sits BEYOND the server's own window: whatever the turn's outcome
 * is, the server gets to state it, and this fires only if the wire is still
 * open after the deployment itself would have given up. Waiting is no longer
 * blind — the Run's recorded activity streams into the transcript while it
 * works (chat/runActivity.ts) — and Stop is available the entire time, so the
 * "no way out" hazard this constant was minted for is covered by the control
 * that always covered it rather than by a clock that outranks the server.
 *
 * IT DISCARDS NOTHING. Once the acceptance names a Run, every branch that could
 * write a failure is gated on `acceptedRunId` and the poll keeps following that
 * Run to its answer. A connection giving up is a statement about the
 * connection.
 */
export const TURN_DEADLINE_MS = 330_000;

/**
 * THE STALL DEADLINE — distinct from the turn deadline. A stream that emits one
 * frame and then dies would otherwise hold the composer for the remainder of two
 * minutes over a half-answer. The clock restarts on EVERY server signal — data
 * frames AND the server's `: keep-alive` comments (every 15 s while a turn
 * legitimately runs). Feeding it on frames alone would false-abort the exact
 * turns this product runs: per-hop tool frames now arrive EARLY, and the model
 * then thinks for longer than this window with only keep-alives on the wire. A
 * signal-fed stall clock distinguishes "long turn, live stream" from "dead
 * stream", which is the only distinction it exists to draw.
 */
export const STALL_DEADLINE_MS = 30_000;

/**
 * THE CITATION READ'S DEADLINE — the pending source line's terminal condition.
 *
 * Once the Run settles, the answer is on screen and two more server reads are
 * still in flight for its sources (the evidence envelope, then the authorized
 * retrieve). The thread says "Resolving citations…" for exactly that window —
 * and a window needs an end the reads themselves cannot guarantee: a hung
 * fetch has no client timeout of its own. The measured normal here is tens of
 * seconds (owner, 2026-08-28), so the ceiling sits well above normal and well
 * below the turn deadline. Expiry clears ONLY the pending line — the read is
 * not aborted, its result still lands if it ever arrives, and the source block
 * returns to stating nothing, which claims nothing.
 */
export const CITATION_READ_DEADLINE_MS = 120_000;

export interface AgentStream {
  messages: UiMessage[];
  busy: boolean;
  /** The server refused this key the Ask surface (403 on the session create). */
  refused: boolean;
  /** The server's browser-to-Run handoff for the LATEST turn.
   *
   * NO LONGER A HANDOFF THE READER HAS TO FOLLOW. It used to be the whole
   * answer: the stream ended, the placeholder was deleted, and an "Open Run"
   * link was all that was left on screen. The acceptance now starts a poll and
   * the answer lands in the thread; this stays exposed so a reader who WANTS
   * the Run workspace can still get there in one hit. */
  acceptedRun?: Extract<StreamEvent, { type: "accepted" }> | null;
  send: (
    sessionId: string | undefined,
    prompt: string,
    requestedModel?: string,
    onModelSelectionInvalidated?: () => void,
    onGroundingUnavailable?: () => void,
    /** A RETRY of an existing turn passes that turn's frozen idempotency key,
     * so the resend joins the run already in flight instead of minting one. */
    resendKey?: string,
  ) => Promise<string | undefined>;
  stop: () => void;
  startedAt: number | null;
  /** When the last thing — frame or keep-alive comment — arrived on the
   * in-flight turn's stream. Null until the first signal. MEASURED, so the
   * screen can state the server's liveness instead of implying it. */
  lastSignalAt: number | null;
  /** Replace the thread — a different session was selected, or a new one begun. */
  loadThread: (messages: UiMessage[]) => void;
}

/** The browser uses the server-advertised opaque agent mapping for an optional
 * mode; every grounding verdict still comes back from the terminal frame.
 *
 * A bearer-backed ApiClient may stay mounted while `/v1/me` changes. The
 * ready PrincipalOwner generation is therefore part of the key: no turn bytes or late
 * continuation owned by one served identity can enter another identity's
 * window. A null principal is deliberately inert and renders no prior state. */
export function useAgentStream(
  client: ApiClient,
  ownerGeneration: string | null,
  agentName = "console-chat",
  /** Non-null routes every turn to THIS agent's own canonical Run envelope
   * (`POST /v1/agents/{id}/runs`) instead of the default-Ask chat wire, which
   * reaches exactly one agent. The acceptance then hands over to the same Run
   * poll the canonical chat path uses — one arrival pipeline, two doors in. */
  runTarget: AgentRunTarget | null = null,
): AgentStream {
  const generation = useMemo(
    () => (ownerGeneration === null ? null : { client, ownerGeneration }),
    [client, ownerGeneration],
  );
  const currentGenerationRef = useRef(generation);
  const [stateGeneration, setStateGeneration] = useState(generation);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState(false);
  const [acceptedRun, setAcceptedRun] = useState<Extract<StreamEvent, { type: "accepted" }> | null>(
    null,
  );
  /** When the in-flight turn went out. Null when idle. */
  const [startedAt, setStartedAt] = useState<number | null>(null);
  /** When the last frame or keep-alive arrived. Null until the first signal. */
  const [lastSignalAt, setLastSignalAt] = useState<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Per-turn arrival counter — the last-resort per-call identity. Incremented
  // OUTSIDE the (double-invoked) updater so StrictMode cannot skew it.
  const arrivalRef = useRef(0);
  /** Why the in-flight turn was aborted, when on purpose. An UNMOUNT leaves this
   * null, which is what keeps that path silent. */
  const cancelReasonRef = useRef<"deadline" | "stall" | "reader" | null>(null);

  /**
   * THE ANSWER'S ARRIVAL, for the canonical path.
   *
   * Keyed by the ASSISTANT MESSAGE ID, not by run id: the key is the thing
   * being patched, so a second question can never edit the first turn even if
   * the server reissued a run id, and a re-delivered `accepted` frame folds
   * onto the same key instead of opening a second loop.
   *
   * Guarded on the generation exactly as every other write in this hook is. A
   * poll that resolves after the served identity changed writes nothing: no
   * turn's bytes may enter another identity's window, and a durable Run's
   * answer arriving late is precisely the case that could.
   */
  /**
   * THE ANSWER'S SOURCES, read once the Run settles.
   *
   * Canonical Ask's stream carries no `citations` frame — it carries one
   * `accepted` frame and closes — so the chips, the source list and the source
   * pane had no writer on this path and every governed answer rendered as
   * though it cited nothing. They are fed here, from the Run, exactly the way
   * the answer itself is.
   *
   * TWO SERVER READS, AND BOTH ARE LOAD-BEARING. The evidence envelope states
   * which evidence records the answer's claims are ASSOCIATED with and names
   * their chunks; `retrieve` turns those chunk ids into the server's own
   * `Citation` objects — the only place `document_id` and `source_ref` exist,
   * and an authorized read, so a chunk the reader may no longer open simply
   * does not come back. Neither field is ever synthesised here (see
   * chat/runCitations.ts for the two rules this obeys).
   *
   * FAILURE IS SILENT AND FAIL-CLOSED. A turn whose answer is fine must not be
   * failed over a source list, so an unreachable evidence read leaves the
   * citations empty and the answer untouched. What it never does is invent a
   * chip or claim the answer cited nothing when it could not tell.
   */
  const readCitations = useCallback(
    (agentId: string, runId: string) => readRunCitations(client, agentId, runId),
    [client],
  );
  const citationReader = useRef(readCitations);
  useLayoutEffect(() => {
    citationReader.current = readCitations;
  }, [readCitations]);

  const handlePollOutcome = useCallback((key: string, outcome: RunPollOutcome) => {
    if (currentGenerationRef.current === null) return;
    // A poll response IS the server's last word on this turn. Feeding the same
    // measured clock the stream fed keeps the in-flight line honest across the
    // handover instead of freezing it at the acceptance frame.
    setLastSignalAt(Date.now());
    const patchById = (fn: (m: UiMessage) => UiMessage) =>
      setMessages((prev) => prev.map((m) => (m.id === key ? fn(m) : m)));
    if (outcome.kind === "unreachable") {
      setStartedAt(null);
      patchById((m) => ({
        ...m,
        streaming: false,
        failure: runUnreadableFailure(outcome.detail),
        announcement: "Run result unavailable.",
      }));
      return;
    }
    if (outcome.kind === "progress") {
      const run = outcome.run;
      const paused = run.status === "waiting_approval";
      // THE ACTIVITY, AS THE SERVER RECORDED IT. Built from this poll's events
      // and nothing else, so a Run that has recorded nothing yields an empty
      // list and the band stays empty rather than inventing a first step.
      const runSteps = runTurnSteps(run, outcome.events);
      const runStepsNote = runPlanAbsentNote(run);
      const runActivitySeconds = runActivityElapsedSeconds(outcome.events);
      patchById((m) => ({
        ...m,
        runSteps,
        runStepsNote,
        runActivitySeconds,
        runStatus: run.status,
        ...(run.approval_id === null ? {} : { approvalId: run.approval_id }),
        // A PAUSED TURN IS NOT ARRIVING. The caret says bytes are on their way;
        // a Run parked on a human decision is stopped, and drawing it as live
        // output is the same lie the blank screen told, in the other direction.
        streaming: !paused,
        ...(paused ? { announcement: "Waiting for an approval decision." } : {}),
      }));
      return;
    }
    setStartedAt(null);
    // The steps the Run reached survive its settlement — that is what lets a
    // CANCELLED turn say how far it got instead of just that it stopped.
    const settledSteps = runTurnSteps(outcome.run, outcome.events);
    const settledNote = runPlanAbsentNote(outcome.run);
    patchById((m) => ({
      ...applySettledRun(m, outcome.run),
      runSteps: settledSteps,
      runStepsNote: settledNote,
      runActivitySeconds: runActivityElapsedSeconds(outcome.events),
      // The citation read below is issued in this same tick, so the window it
      // opens is stated the moment it exists. The owner measured tens of
      // seconds of dead air here — answer on screen, source block silent — and
      // silence in that window read as "cites nothing", which is not the fact.
      citationsPending: true,
    }));
    // The sources, after the answer. Deliberately not awaited: the answer is on
    // the screen the moment the Run settles, and the citations land beside it a
    // request later rather than holding it back.
    const generation = currentGenerationRef.current;
    // THE PENDING STATE'S TERMINAL CONDITION. Every way the read can end clears
    // the flag — resolve, reject, or this deadline — so the pending line can
    // never outlive the read's own account by more than the deadline. On
    // timeout the flag clears and NOTHING else changes: the source block goes
    // back to saying nothing, which claims nothing — never "cited nothing". A
    // read that lands after the deadline still lands (the patch below is
    // guarded only by generation), just without the pending line above it.
    const clearPending = () =>
      patchById((m) => (m.citationsPending ? { ...m, citationsPending: false } : m));
    const pendingDeadline = setTimeout(() => {
      if (currentGenerationRef.current !== generation) return;
      clearPending();
    }, CITATION_READ_DEADLINE_MS);
    void citationReader.current(outcome.run.agent_id, outcome.run.run_id).then(
      (result) => {
        clearTimeout(pendingDeadline);
        // Guarded on the generation exactly as every other write here is: a
        // read that resolves after the served identity changed writes nothing.
        if (currentGenerationRef.current !== generation) return;
        if (result === null) {
          // This deployment's client has no evidence read: nothing was asked,
          // so nothing is pending — and nothing is stated.
          clearPending();
          return;
        }
        patchById((m) => ({
          ...m,
          citationsPending: false,
          citations: result.citations,
          rowAnchors: result.rowAnchors,
          ...(result.note === null ? {} : { citationsNote: result.note }),
        }));
      },
      () => {
        // Fail-closed and silent: no chip, no invented absence sentence — and
        // no pending line left claiming a read that has already died.
        clearTimeout(pendingDeadline);
        if (currentGenerationRef.current !== generation) return;
        clearPending();
      },
    );
  }, []);
  const readRun = useCallback(
    (agentId: string, runId: string) => client.getAgentRun(agentId, runId),
    [client],
  );
  const readRunEvents = useCallback(
    (agentId: string, runId: string, cursor?: string, limit?: number) =>
      client.listAgentRunEvents(agentId, runId, cursor, limit),
    [client],
  );
  const completion = useRunCompletion(readRun, handlePollOutcome, readRunEvents);
  const { watch: watchRun, reset: resetWatches, pending: pendingRuns } = completion;

  // Client and served principal jointly own the generation. The render that
  // sees either change returns no prior bytes; the layout effect aborts/reset
  // before paint. An unresolved principal is an empty, non-requesting owner.
  useLayoutEffect(() => {
    currentGenerationRef.current = generation;
    abortRef.current?.abort();
    abortRef.current = null;
    // A poll outlives the stream that started it, so the identity change has to
    // stop it explicitly: an abandoned watch would resolve into the NEW
    // identity's window carrying the old one's answer.
    resetWatches();
    /* THE RESET IS THE POINT, AND IT MUST LAND BEFORE PAINT. When the client or
       the served principal changes, the previous identity's thread must not
       survive into the next one's window for even one painted frame — that is
       the whole reason this is a LAYOUT effect and not an effect. A hook cannot
       be remounted by `key`, so there is no render-time form of this.
       Pre-existing and deliberate; the rule began reporting it only once the
       compiler's analysis of this file stopped bailing out earlier. */
    /* eslint-disable react-hooks/set-state-in-effect */
    setStateGeneration(generation);
    setMessages([]);
    setBusy(false);
    setRefused(false);
    setAcceptedRun(null);
    setStartedAt(null);
    setLastSignalAt(null);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [generation, resetWatches]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const loadThread = useCallback(
    (next: UiMessage[]) => {
      if (generation === null || currentGenerationRef.current !== generation) return;
      abortRef.current?.abort();
      resetWatches();
      setStateGeneration(generation);
      setMessages(next);
      setAcceptedRun(null);
      setStartedAt(null);
    },
    [generation, resetWatches],
  );

  // NEVER rejects. Resolves with the session id used for the turn, or undefined
  // when the turn was aborted/unmounted or no session was ever created.
  const send = useCallback(
    async (
      sessionId: string | undefined,
      prompt: string,
      requestedModel?: string,
      onModelSelectionInvalidated?: () => void,
      onGroundingUnavailable?: () => void,
      /** The turn's frozen idempotency key. A RETRY of an existing turn passes
       * the key that turn was born with, so the resend resolves to the run
       * already running instead of minting a second one; a genuinely new
       * question passes nothing and one is minted here. */
      resendKey?: string,
    ): Promise<string | undefined> => {
      const sendGeneration = generation;
      const current = () =>
        sendGeneration !== null && currentGenerationRef.current === sendGeneration;
      if (!current()) return undefined;
      const controller = new AbortController();
      abortRef.current = controller;
      setStateGeneration(sendGeneration);
      setBusy(true);
      setAcceptedRun(null);
      setStartedAt(Date.now());
      setLastSignalAt(null);
      // ONE KEY PER QUESTION, FROZEN. The server mints its turn id from this,
      // so it is the difference between a resend joining the run already in
      // flight and starting a sixth one beside it. It is stamped on the
      // assistant message below, which is where a retry reads it back from.
      const idempotencyKey = resendKey ?? crypto.randomUUID();
      let endedBy: "deadline" | "stall" | "reader" | null = null;
      cancelReasonRef.current = null;
      const deadline = setTimeout(() => {
        endedBy = "deadline";
        cancelReasonRef.current = "deadline";
        controller.abort();
      }, TURN_DEADLINE_MS);
      // Armed by the FIRST server signal — frame OR keep-alive — so it never
      // fires while the request is still in transit, then restarted by every
      // signal after it. Keep-alives count: a turn that is thinking (or parked
      // on an approval) is alive, and only a stream that goes fully silent
      // longer than the window is stalled.
      let stall: ReturnType<typeof setTimeout> | undefined;
      const armStall = () => {
        clearTimeout(stall);
        stall = setTimeout(() => {
          endedBy = "stall";
          cancelReasonRef.current = "stall";
          controller.abort();
        }, STALL_DEADLINE_MS);
      };
      /**
       * THE TERMINAL FRAME SETTLES THE TURN — AND ONLY THE TURN.
       *
       * `done` and `error` are the server's verdict, and once one is on the
       * screen nothing else may re-decide what the turn WAS. Four things
       * otherwise can, each replacing a stated cause with the console's own
       * generic one — a policy refusal read back as "the turn timed out", detail
       * blanked and `retryable` flipped, which is the readout lying about why
       * the turn failed:
       *   - the stall abort, on a wire held open past its window;
       *   - the TURN DEADLINE abort, armed per-send, at 120 s;
       *   - a reader Stop, live until the stream actually closes;
       *   - a transport throw, because the SSE reader keeps reading past a
       *     terminal frame until the body ends (api/sse.ts).
       * Every one of them is a decision ABOUT THE CONNECTION, and each is gated
       * on `sawTerminal` at the point where it would write the turn's state.
       *
       * THE CLOCKS ARE NOT DISARMED HERE, and that is the point. They watch the
       * CONNECTION, and after the terminal that is the only job left: both
       * server paths end at their terminal frame (ne-api `sse.py`), so a wire
       * still open afterwards is one nobody is going to close, and `busy` —
       * the disabled composer — is released only when the read loop ends.
       * Clearing them settles the connection along with the turn and hangs the
       * composer indefinitely over a delivered answer, which is the hang
       * `TURN_DEADLINE_MS` exists to forbid. Verdict and connection are settled
       * by different things, so they are settled in different places.
       */
      let sawTerminal = false;
      /** The Run this turn became, once the server named one. Its presence is
       * what makes a clean stream close ORDINARY rather than a broken wire. */
      let acceptedRunId: string | null = null;
      // Every server signal is measured once, here, whatever kind it was.
      const signal = () => {
        if (!current()) return;
        setLastSignalAt(Date.now());
        armStall();
      };
      arrivalRef.current = 0;

      let sid = sessionId;
      const userMsg: UiMessage = {
        id: nextId(),
        role: "user",
        text: prompt,
        citations: [],
        streaming: false,
        tools: [],
      };
      const assistantId = nextId();
      const assistantMsg: UiMessage = {
        id: assistantId,
        role: "assistant",
        text: "",
        citations: [],
        streaming: true,
        tools: [],
        streamItems: [],
        requestedModel,
        prompt,
        idempotencyKey,
        // May be undefined until the bootstrap stamps it — appending the pair
        // BEFORE bootstrap guarantees a failed bootstrap has a message to land
        // on, and the prompt echoes instantly.
        sessionId: sid,
      };
      setMessages((prev) => (current() ? [...prev, userMsg, assistantMsg] : prev));

      const patch = (fn: (m: UiMessage) => UiMessage) =>
        setMessages((prev) =>
          current() ? prev.map((m) => (m.id === assistantId ? fn(m) : m)) : prev,
        );

      let beganModelRequest = false;
      try {
        if (runTarget !== null) {
          // THE NON-DEFAULT AGENT'S TURN RUNS ON ITS OWN AGENT. The chat wire
          // below admits exactly one agent name and always answers with the
          // canonical default (ne-api routers/chat.py create_session), so a
          // turn for any other agentic agent submits where every canonical Run
          // is submitted: the agent's own Conversation plus
          // `POST /v1/agents/{id}/runs`, with the full frozen envelope the
          // server revalidates at admission. Everything AFTER acceptance is
          // shared with the canonical chat path — the same `watchRun` poll,
          // the same settlement, the same citation reads — so the two doors
          // can never disagree about what a turn becomes.
          let expectedVersion: number;
          if (!sid) {
            // The bootstrap lives INSIDE the guard so its failure or abort is
            // handled exactly like a submission failure — never floats.
            const conversation = await client.createAgentConversation(runTarget.agentId);
            if (controller.signal.aborted || !current()) return undefined;
            sid = conversation.conversation_id;
            expectedVersion = conversation.last_sequence;
            const stamped = sid;
            patch((m) => ({ ...m, sessionId: stamped }));
          } else {
            // The optimistic turn claim needs the Conversation's CURRENT
            // version — a continued thread re-reads it rather than guessing,
            // and a genuinely concurrent writer surfaces as the server's own
            // 409 instead of a silently forked history.
            const conversation = await client.getConversation(runTarget.agentId, sid);
            if (controller.signal.aborted || !current()) return sid;
            expectedVersion = conversation.last_sequence;
          }
          beganModelRequest = requestedModel !== undefined;
          const receipt = await client.startAgentRun(
            runTarget.agentId,
            {
              conversation_id: sid,
              expected_version: expectedVersion,
              agent_version: runTarget.agentVersion,
              strategy: "agentic",
              config_hash: runTarget.configHash,
              capability_snapshot_hash: runTarget.capabilityHash,
              input: prompt,
              requested_model: requestedModel ?? null,
              requested_model_lane: null,
            },
            idempotencyKey,
          );
          if (controller.signal.aborted || !current()) return sid;
          // The acceptance IS a server signal — measured like every other.
          setLastSignalAt(Date.now());
          acceptedRunId = receipt.run_id;
          setAcceptedRun({
            type: "accepted",
            agent_id: runTarget.agentId,
            conversation_id: sid,
            run_id: receipt.run_id,
            attempt_id: receipt.attempt_id,
            status: receipt.status === "queued" ? "queued" : "submitted",
          });
          patch((m) => ({
            ...m,
            runRef: { agentId: runTarget.agentId, runId: receipt.run_id },
            runStatus: "submitted",
            announcement: "Run accepted — working.",
          }));
          watchRun(assistantId, { agentId: runTarget.agentId, runId: receipt.run_id });
          return sid;
        }
        if (!sid) {
          // The bootstrap lives INSIDE the guard so its failure or abort is
          // handled exactly like a stream failure or abort — never floats.
          const session = await client.createSession(agentName);
          if (controller.signal.aborted || !current()) return undefined;
          const isCanonicalDefaultAsk =
            agentName === "console-chat" && session.agent_name === DEFAULT_ASK_AGENT_ID;
          if (session.agent_name !== agentName && !isCanonicalDefaultAsk) {
            // The console's own diagnosis, which is why it is not passed as
            // `detail`: that slot is the server's verbatim words and this
            // sentence was written here.
            const detail = "";
            if (onGroundingUnavailable !== undefined) {
              onGroundingUnavailable();
              patch((m) => ({
                ...m,
                streaming: false,
                failure: groundingUnavailableFailure(detail),
              }));
            } else {
              patch((m) => ({
                ...m,
                streaming: false,
                failure: classifyTurnFailure(detail),
              }));
            }
            return undefined;
          }
          sid = session.session_id;
          const stamped = sid;
          patch((m) => ({ ...m, sessionId: stamped }));
        }
        beganModelRequest = requestedModel !== undefined;
        await client.sendMessage(
          sid,
          prompt,
          (event) => {
            signal();
            // A SETTLED TURN TAKES NO MORE FRAMES. Both server paths end at
            // their terminal (ne-api `sse.py`: the relay returns at the FIRST
            // one), so anything arriving after it is a re-delivery or a proxy's
            // own invention — and folding it in would edit an answer the reader
            // has already read. The signal above is still measured: the wire is
            // demonstrably alive, which is a fact about the connection.
            if (sawTerminal) return;
            const arrival = (arrivalRef.current += 1);
            switch (event.type) {
              case "token":
                patch((m) => ({
                  ...m,
                  text: m.text + event.delta,
                  streamItems: appendAnswerDelta(m.streamItems ?? [], event.delta, arrival),
                }));
                break;
              case "citations":
                patch((m) => ({
                  ...m,
                  citations: event.citations,
                  streamItems: [
                    ...(m.streamItems ?? []),
                    {
                      key: `citations:${arrival}`,
                      kind: "citations",
                      citations: event.citations,
                    },
                  ],
                }));
                break;
              case "tool": {
                patch((m) => ({
                  ...m,
                  tools: applyToolTransit(m.tools, event, arrival),
                  streamItems: applyOrderedToolTransit(m.streamItems ?? [], event, arrival),
                  announcement: materialToolAnnouncement(event),
                }));
                break;
              }
              case "accepted":
                // ACCEPTANCE IS NOT THE END OF THE TURN. It is the end of one
                // CONNECTION, and this case used to conflate the two: it set
                // `sawTerminal` and deleted the unanswered placeholder, so
                // canonical Ask — every real question on this deployment —
                // rendered the reader's question, an "Open Run" link, and
                // nothing else, permanently. The comment it carried was
                // accurate about the mechanism and wrong about the conclusion:
                // the right response to "this frame carries no output bytes" is
                // to go and read the output, not to erase the place it goes.
                //
                // `sawTerminal` stays FALSE deliberately. It guards the turn's
                // VERDICT, and acceptance is not one — but the clean stream
                // close that follows must not stamp `streamEndedFailure` over a
                // turn that is proceeding normally, so the close branches below
                // consult `acceptedRunId` as well.
                acceptedRunId = event.run_id;
                setAcceptedRun(event);
                patch((m) => ({
                  ...m,
                  runRef: { agentId: event.agent_id, runId: event.run_id },
                  runStatus: "submitted",
                  announcement: "Run accepted — working.",
                }));
                watchRun(assistantId, { agentId: event.agent_id, runId: event.run_id });
                break;
              case "done": {
                sawTerminal = true;
                const traceId = event.trace_id?.trim() || undefined;
                patch((m) => ({
                  ...m,
                  text: event.output || m.text,
                  streaming: false,
                  provenance: provenanceOf(event),
                  traceId,
                  messageId: event.message_id,
                  streamItems: [
                    ...(m.streamItems?.some((item) => item.kind === "answer") || !event.output
                      ? (m.streamItems ?? [])
                      : appendAnswerDelta(m.streamItems ?? [], event.output, arrival)),
                    {
                      key: `terminal:${arrival}`,
                      kind: "terminal",
                      outcome: "complete",
                      ...(traceId ? { traceId } : {}),
                    },
                  ],
                  announcement: "Run complete.",
                }));
                break;
              }
              case "error":
                // SYMMETRIC WITH `done`, to the letter: an error frame is the
                // server's verdict on this turn just as `done` is, and the two
                // cases differ in what they render and in nothing else.
                sawTerminal = true;
                // NEVER `error: event.detail`. The raw string is classified; the
                // verbatim detail rides inside the classification.
                patch((m) => ({
                  ...m,
                  streaming: false,
                  failure: classifyTurnFailure(event.detail),
                  streamItems: [
                    ...(m.streamItems ?? []),
                    { key: `terminal:${arrival}`, kind: "terminal", outcome: "failed" },
                  ],
                  announcement: "Run failed.",
                }));
                break;
            }
          },
          controller.signal,
          signal,
          requestedModel,
          idempotencyKey,
        );
        // A cancelled read ends the loop WITHOUT throwing (api/sse.ts explains
        // why), so the deliberate-stop state is applied here as well as in the
        // catch. An unmount leaves `cancelReasonRef` null and stays silent.
        if (controller.signal.aborted) {
          const reason = endedBy ?? cancelReasonRef.current;
          if (reason === null) return undefined;
          // A settled turn keeps the server's verdict: an abort landing after
          // the terminal frame is the CONNECTION's end, not the turn's, and
          // stamping "you stopped this" over a delivered answer states the
          // wrong cause exactly as the timeout did. An accepted turn is the
          // same case one step earlier — the Run is durable and still being
          // read — so it keeps its state too.
          if (!sawTerminal && acceptedRunId === null) {
            patch((m) => ({ ...m, streaming: false, failure: abortFailure(reason, "") }));
          }
        } else if (!sawTerminal && acceptedRunId === null) {
          // The stream closed cleanly with NO done, NO error AND NO acceptance:
          // the connection ended, not the turn. Say so — a cursor left blinking
          // over a dead wire reads as a working system, which is worse than the
          // failure. An ACCEPTED turn reaches here on every canonical question
          // (the server yields one frame and closes) and is not failing: its
          // answer is being polled for, and stamping a stream failure over it
          // would replace a working turn with an invented outage.
          patch((m) => ({ ...m, streaming: false, failure: streamEndedFailure() }));
        }
        return sid;
      } catch (err: unknown) {
        let selectionError: { status: 403 | 409 | 503; detail: string } | null = null;
        if (beganModelRequest && err instanceof ApiError) {
          const status = err.status;
          if (
            (status === 403 || status === 409 || status === 503) &&
            // The status alone is NOT the verdict: a model-carrying turn can
            // die on a Conversation-admission conflict or a canonical-Ask
            // seam 503, and those must not be reported as a model failure
            // (turnFailure.isModelSelectionVerdict).
            isModelSelectionVerdict(status, err.detail ?? "")
          ) {
            selectionError = { status, detail: err.detail ?? "" };
          }
        }
        // A deliberate abort decides what the TURN says, but cannot make a
        // definitive server status disappear. Invalidate its catalog evidence
        // before the abort branch returns so Stop/deadline/stall cannot leave a
        // refused or unavailable alias immediately runnable.
        if (selectionError !== null) onModelSelectionInvalidated?.();

        if (controller.signal.aborted) {
          const reason = endedBy ?? cancelReasonRef.current;
          if (reason === null) return undefined;
          if (!sawTerminal && acceptedRunId === null) {
            const failure = abortFailure(reason, "");
            patch((m) => ({
              ...m,
              streaming: false,
              // Refreshing the server catalog is the only valid next action;
              // a direct turn retry has no current authorized alias to send.
              failure: selectionError === null ? failure : { ...failure, retryable: false },
            }));
          }
          return sid;
        }
        // The wire broke AFTER the server's verdict rendered — the reader reads
        // past a terminal frame until the body ends, so this is reachable.
        // Classifying the throw would replace a stated refusal with a retryable
        // transport failure: the same lie about the cause, by another path.
        if (sawTerminal || acceptedRunId !== null) return sid;
        // The server's own verdict on the SURFACE, not on this turn: a 403 on
        // the session create is a role boundary and renders as one, once, for
        // the screen. A 422 is the department claim and has its own stated line.
        if (selectionError !== null) {
          patch((m) => ({
            ...m,
            streaming: false,
            failure: modelSelectionFailure(selectionError.status, selectionError.detail),
          }));
          return sid;
        }
        if (err instanceof ApiError && err.status === 403) {
          if (current()) {
            setRefused(true);
            setMessages((prev) => prev.filter((m) => m.id !== assistantId && m.id !== userMsg.id));
          }
          return undefined;
        }
        // THE THROW'S OWN FACTS: the status the server answered with, and the
        // words the server itself sent. Both transport classes carry them, and
        // a stream that dies on a non-selection status arrives as the raw
        // SseError — reading only ApiError here would leave every one of those
        // with no status at all.
        //
        // `detail` IS THE SERVER'S WORDS OR NOTHING. It is rendered behind the
        // "what the server said" disclosure and `TurnFailure.detail` says it is
        // verbatim, so the only string that may go here is one the server
        // actually sent. `userMessage` is the console's own house sentence
        // whenever the server sent no `detail`, and putting that here signs the
        // console's prose with the server's name. A throw that carried no
        // served reason leaves this empty and the disclosure is not drawn.
        const transport = err instanceof ApiError || err instanceof SseError ? err : null;
        const detail = transport?.detail ?? "";
        if (
          sid === undefined &&
          onGroundingUnavailable !== undefined &&
          err instanceof ApiError &&
          err.status === 422 &&
          detail === "unknown agent_name"
        ) {
          onGroundingUnavailable();
          patch((m) => ({
            ...m,
            streaming: false,
            failure: groundingUnavailableFailure(detail),
          }));
          return undefined;
        }
        // CLASSIFIED FROM THE THROW, NOT FROM THE TEXT. `classifyTurnFailure`
        // matches the SERVER's error-frame vocabulary; a thrown transport error
        // carries none of it, and matching the reader's sentence against those
        // patterns is how a dead stream came to report "no answer was
        // produced". The error object already knows what it is.
        patch((m) => ({
          ...m,
          streaming: false,
          failure: thrownTransportFailure(transport?.status ?? null, detail),
        }));
        return sid;
      } finally {
        clearTimeout(deadline);
        clearTimeout(stall);
        if (current()) {
          setBusy(false);
          // THE CLOCK IS HANDED OVER, NOT STOPPED. On the canonical path the
          // stream closes seconds into a turn that runs for a minute; clearing
          // the start stamp here would reset the measured in-flight line to the
          // em dash for the whole remaining wait. The poller clears it when the
          // Run actually settles.
          if (acceptedRunId === null) setStartedAt(null);
        }
      }
    },
    [agentName, client, generation, runTarget, watchRun],
  );

  /** Stop the in-flight turn. The reader always has a way out. */
  const stop = useCallback(() => {
    if (generation === null || currentGenerationRef.current !== generation) return;
    cancelReasonRef.current = "reader";
    abortRef.current?.abort();
    // Stop stops the console LISTENING, on the poll exactly as on the stream.
    // The durable Run keeps running and its approvals stay actionable — which
    // is what ASK_STOPPED has always said — so the thread is left saying the
    // turn was stopped rather than claiming it died.
    resetWatches();
    setStartedAt(null);
    setMessages((prev) =>
      prev.map((m) =>
        m.streaming || m.runStatus === "waiting_approval"
          ? { ...m, streaming: false, failure: abortFailure("reader", "") }
          : m,
      ),
    );
  }, [generation, resetWatches]);

  const visible = generation !== null && stateGeneration === generation;
  return {
    messages: visible ? messages : [],
    // A TURN IS IN FLIGHT UNTIL ITS ANSWER LANDS, not until its stream closes.
    // Canonical Ask's stream closes in the first second; releasing the composer
    // there would invite a second question on top of a turn still working, and
    // would tell the reader the turn was over while the screen was still blank.
    busy: visible ? busy || pendingRuns > 0 : false,
    refused: visible ? refused : false,
    acceptedRun: visible ? acceptedRun : null,
    send,
    stop,
    startedAt: visible ? startedAt : null,
    lastSignalAt: visible ? lastSignalAt : null,
    loadThread,
  };
}
