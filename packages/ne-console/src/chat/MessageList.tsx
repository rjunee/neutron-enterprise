// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — THE THREAD, on the transcript system.
//
// This file is ASK'S ADAPTER and nothing else. The anatomy — question bubble,
// folded work band, promoted gate, answer, sources, receipt — belongs to
// src/transcript/, which two screens share; what belongs here is the mapping
// from Ask's own wire shapes onto it, and every judgement that mapping needs.
//
// THE PAIRING. Ask's stream is a flat list of messages: `send` appends the
// user's question and the assistant's reply together, and stored history reads
// back the same alternation. A turn is therefore a question plus the reply that
// followed it, and a question still waiting for its reply is a turn with a
// bubble and nothing under it yet.
//
// ONE FLAT `steps[]`, IN ARRIVAL ORDER. Nothing here sorts, groups or filters
// the governed calls: which of them leave the fold is transcript/promotion.ts's
// rule, and handing it a pre-sorted list would be this file quietly deciding
// what the design decided. The ordered stream items win over the accumulated
// `tools` list where both exist, because only an exact lifecycle identity
// replaces a transit in place there.
//
// THE ANSWER SURFACE MAY CONTAIN ANSWER TEXT AND NOTHING ELSE. The stream body
// is kept raw in state and classified here (chat/answerFrames.ts): model
// reasoning is not the answer, and a tool-call frame the runtime printed instead
// of dispatching is not the answer either — it is a failed turn, and it lands in
// the promoted failure card with its verbatim body, never in answer typography.
//
// WHAT THE WIRE DOES NOT SERVE, and what this therefore does not draw:
//   * NO STEP OR BAND DURATION. `ToolTransitEvent` carries no elapsed value and
//     the stream carries no band total, so both are handed in null and the
//     transcript draws no time slot at all (owner ruling, 2026-08-24: a dash on
//     every row of every turn advertises an absence nobody can act on). The only
//     measured numbers on this screen are the in-flight line's, and they are
//     measured by chat/TurnProgress.tsx against this machine's own clock.
//   * NO SERVED MODEL. The chat wire never names the model that answered, so the
//     receipt's model slot is the em dash with a factual note, and the alias the
//     REQUESTER asked for is a separate, separately-labelled fact beside it.
//   * NO MESSAGE TIMESTAMP. The question's stamp in the render has no field
//     behind it here, so no stamp is drawn. Reading this browser's clock would
//     date the render, not the question.
//
// CITATIONS ARE THE PRODUCT. The chips sit after the answer's prose and the
// source lines under it carry the two actions that matter: open it, or say it is
// wrong. The wire carries no span offsets for a citation, so nothing here guesses
// a sentence position.

import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import type { Citation } from "../api/types";
import { RUN_RECEIPT_LABEL } from "../agents/RunReceiptPane";
import { Transcript, isPromoted, partitionTurnSteps } from "../transcript";
import type {
  TranscriptFact,
  TranscriptFailure,
  TranscriptFailureTone,
  TranscriptReceipt,
  TranscriptStep,
  TranscriptTurn,
} from "../transcript";
import { traceDecisionHref } from "../trace/traceLink";
import { AnswerExport } from "./AnswerExport";
import { classifyAnswer } from "./answerFrames";
import { CitationChip } from "./CitationChip";
import { ProvenanceChip } from "./ProvenanceChip";
import { toTranscriptStep } from "./ToolRow";
import { askFailure } from "./TurnFailureReadout";
import { useTurnProgress } from "./TurnProgress";
import type { TurnProgressFacts } from "./TurnProgress";
import {
  ASK_ANSWER_LABEL,
  ASK_CITATIONS_RESOLVING,
  ASK_AWAITING_DECISION,
  ASK_INTERNAL_REASONING_HIDDEN,
  ASK_NO_MODEL_NAMED,
  ASK_NO_MODEL_REPLY,
  ASK_OPEN_TRACE,
  ASK_RECEIPT_COMPLETE,
  ASK_RECEIPT_FAILED,
  ASK_RECEIPT_PAUSED,
  ASK_RECEIPT_REFUSED,
  ASK_RECEIPT_STOPPED,
  ASK_MODEL_AS_REQUESTED,
  ASK_MODEL_FELL_BACK,
  ASK_MODEL_ROUTING_UNRECORDED,
  ASK_PAUSED_SENTENCE,
  ASK_REQUESTED_MODEL,
  ASK_REVIEW_IN_APPROVALS,
  ASK_TURN_FAILED,
  ASK_UNDISPATCHED_FRAME,
  CITATION_FLAG,
  CITATION_FLAGGED,
  FEEDBACK_FAILED,
  FEEDBACK_HELPFUL,
  FEEDBACK_NOT_HELPFUL,
  FEEDBACK_RECORDED,
  SOURCE_VIEW,
  askModelFellBackFrom,
  askRawFrameLabel,
  askRefusedCount,
} from "./askCopy";
import type { ToolTransitEntry, UiMessage } from "./useAgentStream";

export type { TurnProgressFacts };

type SignalFn = () => Promise<void>;

/**
 * A one-shot signal affordance: fires exactly once, then states the outcome. The
 * SYNCHRONOUS ref is the load-bearing guard — state updates are async and cannot
 * stop a same-tick second click, so exactly one signal reaches the intake per
 * answer per affordance. A FAILED emit (nothing arrived) re-opens for one retry.
 */
function useSignalOnce() {
  const [status, setStatus] = useState<"idle" | "sending" | "recorded" | "error">("idle");
  const inFlight = useRef(false);
  const fire = async (fn: SignalFn) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setStatus("sending");
    try {
      await fn();
      // Left in flight: one-shot after success, so the affordance is spent.
      setStatus("recorded");
    } catch {
      inFlight.current = false;
      setStatus("error");
    }
  };
  return { status, fire, disabled: status === "sending" };
}

/** §5: two quiet text buttons on a finished answer, in the receipt's foot. */
function AnswerFeedback({
  message,
  onThumbs,
}: {
  message: UiMessage;
  onThumbs: (message: UiMessage, up: boolean) => Promise<void>;
}) {
  const { status, fire, disabled } = useSignalOnce();
  if (status === "recorded") {
    return (
      <span className="ne-ask__signalnote" role="status">
        {FEEDBACK_RECORDED}
      </span>
    );
  }
  return (
    <span className="ne-ask__feedback">
      <button
        type="button"
        className="ne-ask__quiet"
        disabled={disabled}
        onClick={() => void fire(() => onThumbs(message, true))}
      >
        {FEEDBACK_HELPFUL}
      </button>
      <button
        type="button"
        className="ne-ask__quiet"
        disabled={disabled}
        onClick={() => void fire(() => onThumbs(message, false))}
      >
        {FEEDBACK_NOT_HELPFUL}
      </button>
      {status === "error" ? (
        <span className="ne-ask__signalerror" role="alert">
          {FEEDBACK_FAILED}
        </span>
      ) : null}
    </span>
  );
}

/** §5: the second action on a citation line — this source is wrong. */
function FlagCitation({
  message,
  citation,
  onFlagSource,
}: {
  message: UiMessage;
  citation: Citation;
  onFlagSource: (message: UiMessage, citation: Citation) => Promise<void>;
}) {
  const { status, fire, disabled } = useSignalOnce();
  if (status === "recorded") {
    return (
      <span className="ne-ask__signalnote" role="status">
        {CITATION_FLAGGED}
      </span>
    );
  }
  return (
    <>
      <button
        type="button"
        className="ne-ask__quiet"
        disabled={disabled}
        onClick={() => void fire(() => onFlagSource(message, citation))}
      >
        {CITATION_FLAG}
      </button>
      {status === "error" ? (
        <span className="ne-ask__signalerror" role="alert">
          {FEEDBACK_FAILED}
        </span>
      ) : null}
    </>
  );
}

/** The runtime's undispatched tool-call frames, verbatim, in the failure card.
 * Mono, never answer typography, never re-serialised. */
function RawFrames({ frames }: { frames: readonly { raw: string }[] }) {
  return (
    <div className="ne-ask__unclassified">
      {frames.map((frame, index) => (
        <div key={`${index}:${frame.raw}`}>
          <p className="ne-ask__rawlabel">{askRawFrameLabel(index + 1)}</p>
          <pre className="ne-ask__rawframe">{frame.raw}</pre>
        </div>
      ))}
    </div>
  );
}

/** The governed transits of one turn, in the order the server reported them.
 * The ordered stream is authoritative where it exists: a re-delivered lifecycle
 * replaces its transit in place there, while `tools` is the accumulated list a
 * legacy turn read back from history carries instead. */
function transitsOf(message: UiMessage): readonly ToolTransitEntry[] {
  if (message.streamItems === undefined) return message.tools;
  return message.streamItems.flatMap((item) => (item.kind === "tool" ? [item.entry] : []));
}

/** Everything the adapter needs that is not on the message itself. */
interface TurnDeps {
  opsReadable: boolean;
  progress: ReturnType<typeof useTurnProgress>;
  onViewSource: (citation: Citation) => void;
  onThumbs?: (message: UiMessage, up: boolean) => Promise<void>;
  onFlagSource?: (message: UiMessage, citation: Citation) => Promise<void>;
  onRetry?: (prompt: string, idempotencyKey?: string) => void;
  /** Dock this turn's Run record in the pane beside the thread. Absent when the
   * surface has no dock to fill, and the control then does not render: an
   * affordance with nowhere to open is worse than no affordance. */
  onOpenReceipt?: (runId: string) => void;
}

/** The receipt's outcome WORD, keyed by the failure's own tone.
 *
 * The footer used to say "Failed" whichever way the turn ended, so a refused
 * turn closed with a red ✕ Failed one line under its grey ⊘ REFUSED card — the
 * card said the governance system worked and the footer said the deployment
 * broke. A `Record<TranscriptFailureTone, string>` rather than a ternary chain:
 * the word and the tone are now chosen by ONE value, so they cannot drift
 * apart, and a fourth failure tone fails to compile here rather than quietly
 * inheriting "Failed" from an else. */
const ASK_RECEIPT_OUTCOME: Record<TranscriptFailureTone, string> = {
  fault: ASK_RECEIPT_FAILED,
  refusal: ASK_RECEIPT_REFUSED,
  stopped: ASK_RECEIPT_STOPPED,
};

/** The receipt: one per settled turn, and the turn's closing edge. */
function receiptOf(
  message: UiMessage,
  {
    failure,
    steps,
    deps,
    exportable,
    reasoningObserved,
  }: {
    failure: TranscriptFailure | null;
    steps: readonly TranscriptStep[];
    deps: TurnDeps;
    exportable: boolean;
    reasoningObserved: boolean;
  },
): TranscriptReceipt {
  const { gates } = partitionTurnSteps(steps);
  // TWO ROUTES TO THE SAME FACT, and both are the server's. A tool frame that
  // arrived on the stream promotes a pending gate; a canonical Run has no such
  // frame and reports the pause as its own STATUS. Neither is inferred.
  const paused = gates.some((gate) => gate.kind === "pending") || turnIsPaused(message);
  const refused = gates.filter((gate) => gate.kind === "refused").length;
  const facts: TranscriptFact[] = [];
  if (paused) facts.push({ key: "decision", value: ASK_AWAITING_DECISION });
  if (refused > 0) facts.push({ key: "refused", value: askRefusedCount(refused) });
  // The suppression is a governance fact about this turn, so it is recorded in
  // the turn's governance footer. It is deliberately NOT a line inside the
  // answer: the answer surface may carry answer text and nothing else, and the
  // reasoning body itself never reaches the DOM at all (chat/answerFrames.ts).
  if (reasoningObserved) {
    facts.push({ key: "reasoning", value: ASK_INTERNAL_REASONING_HIDDEN });
  }
  facts.push(...modelFacts(message));
  // No answer, nothing to rate. A paused turn used to be unreachable here, and
  // offering thumbs on an answer that has not been produced asks the reader to
  // judge a blank.
  const feedback =
    failure === null && !paused && deps.onThumbs ? (
      <AnswerFeedback message={message} onThumbs={deps.onThumbs} />
    ) : null;
  const runLinks = runActions(message, deps.onOpenReceipt);
  return {
    outcome:
      failure !== null
        ? ASK_RECEIPT_OUTCOME[failure.tone]
        : paused
          ? ASK_RECEIPT_PAUSED
          : ASK_RECEIPT_COMPLETE,
    // An IDENTITY, not a mapping: a turn that ended in a failure closes with
    // that failure's OWN tone, so the footer cannot be recoloured away from the
    // card and there is no branch here that could pick the fault by omission.
    tone: failure !== null ? failure.tone : paused ? "paused" : "ok",
    ...(message.provenance ? { pill: <ProvenanceChip provenance={message.provenance} /> } : {}),
    facts,
    // The chat STREAM names no served model (see the header) — but a canonical
    // turn's Run does, and the poll reads it back. Absent is still the em dash
    // and its note; this never guesses a model from a requested alias.
    model: message.servedModel ?? null,
    modelNote:
      message.servedModel !== undefined
        ? undefined
        : failure === null
          ? ASK_NO_MODEL_NAMED
          : ASK_NO_MODEL_REPLY,
    ...(message.traceId
      ? {
          trace: (
            <Link
              className="ne-ask__trace-link"
              to={traceDecisionHref({ traceId: message.traceId })}
            >
              {ASK_OPEN_TRACE}
            </Link>
          ),
        }
      : {}),
    ...(exportable || feedback || runLinks
      ? {
          actions: (
            <>
              {exportable ? <AnswerExport message={message} /> : null}
              {feedback}
              {runLinks}
            </>
          ),
        }
      : {}),
  };
}

/**
 * THE TWO MODEL FACTS, AND THE ROUTING THAT RELATES THEM (askCopy.ts).
 *
 * The row used to print the requested alias and the served model side by side
 * with nothing saying how they related, and then — as a stopgap — said they
 * differed and pointed at the trace, because the reason lived only on the trace
 * wire. The Run carries it now: `route_position` and `fallback_from` come
 * straight off `AgentRunDetailWire`, so the receipt states the routing itself.
 *
 * NOTHING HERE INFERS A FALLBACK FROM TWO NAMES FAILING TO MATCH. A requested
 * ALIAS (`frontier-best`, what the composer offers) and a served model name
 * (`private-default`, what the gateway reports) are not the same kind of
 * string; comparing them answered "did a fallback happen?" with a guess that
 * was wrong in both directions. `route_position` is the server's own answer and
 * is the only thing consulted.
 *
 * Its three states are three different facts and none of them is a default:
 * `"primary"` is a positive claim that nothing was attempted first, `"fallback"`
 * is an involuntary provider fallback, and an ABSENT position (the wire's
 * `"unavailable"`) is a Run that recorded neither — which still is not
 * reassurance, so a turn that visibly changed model says so without claiming a
 * cause.
 */
function modelFacts(message: UiMessage): TranscriptFact[] {
  const facts: TranscriptFact[] = [];
  const requested = message.requestedModel;
  const servedAsRequested = message.routePosition === "primary";
  // What the reader asked for, whenever that is not already the whole story.
  // Suppressed under `"primary"` because the model slot beside this names what
  // served and "served as requested" says they are the same pick.
  if (requested !== undefined && !servedAsRequested) {
    facts.push({
      key: "requested-model",
      label: ASK_REQUESTED_MODEL,
      value: requested,
      mono: true,
    });
  }
  if (message.routePosition === "fallback") {
    // A fallback is a governance fact about the TURN, so it is stated whether
    // or not the reader pinned a model: an automatic turn that fell down the
    // chain fell down the chain.
    facts.push({
      key: "model-routing",
      value:
        message.fallbackFrom === undefined
          ? ASK_MODEL_FELL_BACK
          : askModelFellBackFrom(message.fallbackFrom),
    });
  } else if (servedAsRequested) {
    // Only meaningful against a pick. An automatic turn requested nothing, so
    // there is nothing for its first attempt to have honoured.
    if (requested !== undefined) {
      facts.push({ key: "requested-model-served", value: ASK_MODEL_AS_REQUESTED });
    }
  } else if (
    requested !== undefined &&
    message.servedModel !== undefined &&
    message.servedModel !== requested
  ) {
    // No position on the record AND a visibly different model. The last honest
    // thing left to say: it changed, and this turn does not know why.
    facts.push({ key: "model-routing", value: ASK_MODEL_ROUTING_UNRECORDED });
  }
  return facts;
}

/** The turn is parked on a human decision — the Run said so itself. */
function turnIsPaused(message: UiMessage): boolean {
  return message.runStatus === "waiting_approval";
}

/**
 * The two routes out of a turn that became a canonical Run.
 *
 * The answer arrives IN THE THREAD, so neither of these is how a reader finds
 * out what the agent said any more — that was the defect. They are the routes
 * to the record behind it: what the Run did, and the decision someone owes.
 * Each renders only when the server named the id it needs; a control with
 * nothing behind it is a fabricated capability.
 *
 * OPENING THE RECORD KEEPS YOU ON THE THREAD. This was an "Open Run" LINK to
 * `/agents/:id/runs/:runId` — "its so confusing opening a run and it takes me
 * to some other page". The /agents redesign docked that record in the pane
 * beside the thread and converted every other way in; this one was missed, so
 * the Ask footer went on throwing the reader onto another page while the
 * workspace's own conversation turns opened in place. It is the same control
 * as theirs now, with the same words, and no link to the run route survives on
 * this surface. (The route itself stays — audit entries and notifications hand
 * it out — and renders the same fields standalone.)
 */
function runActions(message: UiMessage, onOpenReceipt?: (runId: string) => void): ReactNode {
  const run = message.runRef;
  if (run === undefined) return null;
  const approvalId = turnIsPaused(message) ? message.approvalId : undefined;
  if (onOpenReceipt === undefined && approvalId === undefined) return null;
  return (
    <>
      {onOpenReceipt === undefined ? null : (
        <button type="button" className="ne-ask__quiet" onClick={() => onOpenReceipt(run.runId)}>
          {RUN_RECEIPT_LABEL}
        </button>
      )}
      {approvalId === undefined ? null : (
        <Link className="ne-ask__quiet" to={`/approvals?request=${encodeURIComponent(approvalId)}`}>
          {ASK_REVIEW_IN_APPROVALS}
        </Link>
      )}
    </>
  );
}

/** One assistant reply, and the question it answered, as one turn. */
function answerTurn(
  message: UiMessage,
  question: UiMessage | null,
  deps: TurnDeps,
): TranscriptTurn {
  const { answer, reasoning, frames } = classifyAnswer(message.text);
  const settled = !message.streaming;
  const paused = turnIsPaused(message);
  const frameFailure = settled && frames.length > 0;
  // A settled turn with no answer text produced no answer — whether the runtime
  // returned nothing or emitted reasoning only. An undispatched tool call has
  // its own louder RAW failure, so it must not be collapsed into this one.
  //
  // A PAUSED TURN IS EXCLUDED, and this is the load-bearing exclusion. It has
  // no answer YET because a person owes a decision, which is the opposite of a
  // turn that finished and produced nothing — and the two were about to render
  // as the same "No answer was produced." card the moment the caret came off.
  const emptyAnswer =
    settled && !paused && answer === "" && message.failure === undefined && !frameFailure;
  // RETRY RESENDS THE TURN, IT DOES NOT START A NEW ONE. Carrying the key this
  // turn was born with is what makes the resend resolve to the run already in
  // flight; without it a reader who retried a turn the console had given up on
  // minted a second run for a question the server was still answering.
  const retry =
    deps.onRetry && message.prompt !== undefined
      ? () => deps.onRetry?.(message.prompt as string, message.idempotencyKey)
      : undefined;

  let failure: TranscriptFailure | null = null;
  if (message.failure) {
    failure = askFailure(message.failure, retry);
  } else if (frameFailure) {
    failure = {
      // A tool call the runtime printed where an answer belongs is the runtime
      // misbehaving, not a governed decision: the fault card, and its evidence.
      tone: "fault",
      state: ASK_TURN_FAILED,
      sentence: ASK_UNDISPATCHED_FRAME,
      action: <RawFrames frames={frames} />,
    };
  } else if (emptyAnswer) {
    failure = askFailure({ kind: "stage", reason: "none", detail: "", retryable: true }, retry);
  }

  // THE TURN'S STEPS, FROM BOTH ACCOUNTS THE SERVER KEEPS.
  //
  // The governed transits come off the stream and carry a real verdict, a tier
  // and a gate — they are the ones the promotion rule pulls out of the band.
  // A canonical Ask turn has none of those on its wire (the SSE closes after
  // `accepted`), and its work is instead read back off the Run it named:
  // `message.runSteps`, one row per tool call and per model call the Run has
  // actually RECORDED so far. That list is what makes a ten-minute turn legible
  // while it runs, and what a cancelled turn keeps so it can show the step it
  // reached.
  //
  // The transits come FIRST because a governed decision outranks a recorded
  // hop, and because on a legacy turn they are the only steps there are. On a
  // canonical turn the transits are empty and the Run's rows stand alone, so
  // the two never compete for the same line.
  // OWNER DEFECT 2026-08-28: the comment above assumed a canonical turn carries
  // no transits, so the two accounts could never describe the same hop. They
  // do. A canonical Ask turn streams governed transits AND records Run rows for
  // the same tool calls, so every call was drawn twice — once promoted and
  // timing-less at the top, once in the band with its duration. The Run's own
  // rows ARE the chronology when it has them; a SETTLED transit restates a row
  // that is already there. Keep only what the promotion rule pulls out — a
  // decision a human owes, or a refusal — and let the band carry the rest.
  const runSteps = message.runSteps ?? [];
  const transits = transitsOf(message).map((entry) => toTranscriptStep(entry, deps.opsReadable));
  const steps = [...(runSteps.length > 0 ? transits.filter(isPromoted) : transits), ...runSteps];
  const chips = message.citations.map((citation, index) => (
    <CitationChip
      key={citation.chunk_id}
      index={index + 1}
      citation={citation}
      onSelect={deps.onViewSource}
    />
  ));
  // The export is offered on the turns the block can actually vouch for: a
  // settled turn the SERVER did not fail, carrying something to copy.
  const exportable =
    settled && message.failure === undefined && (answer !== "" || frames.length > 0);

  return {
    id: message.id,
    question: { text: question?.text ?? message.prompt ?? "", meta: null },
    steps,
    // The span the Run's OWN records cover, first stamp to last — the server's
    // measurement, never this browser's. Absent until it has recorded two
    // events, and the summary line then draws the em dash.
    workElapsedSeconds: message.runActivitySeconds ?? null,
    // Why the ledger lists no tool call, when the Run served no plan. A note,
    // not a step: nothing counts it and it carries no verdict.
    ...(message.runStepsNote ? { workNote: message.runStepsNote } : {}),
    // Open while the turn is live, folded once it settles: the render's
    // Streaming state draws the ledger, its Complete state draws one line.
    ...(message.streaming ? { workOpen: true } : {}),
    ...(failure ? { failure } : {}),
    ...(message.streaming && deps.progress ? { progress: deps.progress } : {}),
    ...(answer === "" && !paused
      ? {}
      : {
          answer: {
            label: ASK_ANSWER_LABEL,
            body: answer,
            ...(message.streaming ? { streaming: true } : {}),
            // WHY THERE IS NO ANSWER YET, stated where the answer goes. The
            // absence rule: an em dash plus the fact, never a blank rectangle
            // the reader has to interpret.
            ...(answer === "" ? { absentNote: ASK_PAUSED_SENTENCE } : {}),
            trailing: <>{chips}</>,
          },
        }),
    sources: message.citations.map((citation, index) => ({
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
            onClick={() => deps.onViewSource(citation)}
          >
            {SOURCE_VIEW}
          </button>
          {settled && deps.onFlagSource ? (
            <FlagCitation message={message} citation={citation} onFlagSource={deps.onFlagSource} />
          ) : null}
        </span>
      ),
    })),
    // WHY THERE ARE FEWER SOURCES THAN THE ANSWER RESTS ON, or none — stated,
    // and only once the turn has settled. While it is still working there are
    // no citations YET, which is not the same fact and must not be drawn as it.
    ...(settled && message.citationsNote ? { sourcesNote: message.citationsNote } : {}),
    // THE THIRD STATE, occupied honestly: the turn HAS settled and the citation
    // read is still in flight (useAgentStream `citationsPending`, which owns
    // the flag's terminal condition). Its line renders in its own pending
    // treatment, never the settled note's — and Transcript ignores it the
    // moment any landed read (sources, anchors, note) is present.
    ...(settled && message.citationsPending ? { sourcesPending: ASK_CITATIONS_RESOLVING } : {}),
    // THE ROW'S IDENTITY. A verified row has no passage to open, so its served
    // canonical hash renders in the source block as the citation itself —
    // "server-verified record" stops being an unpointable claim. Same gating
    // as the note: nothing draws until the turn settles and the read landed.
    ...(settled && message.rowAnchors && message.rowAnchors.length > 0
      ? {
          sourceAnchors: message.rowAnchors.map((row) => ({
            key: row.evidenceId,
            anchor: row.anchor,
          })),
        }
      : {}),
    ...(settled
      ? {
          receipt: receiptOf(message, {
            failure,
            steps,
            deps,
            exportable,
            reasoningObserved: reasoning !== "",
          }),
        }
      : {}),
  };
}

/** Ask's flat message list as transcript turns, in order. Exported for its own
 * test: the pairing and every absence rule above are assertable without a DOM. */
export function askTurns(messages: readonly UiMessage[], deps: TurnDeps): TranscriptTurn[] {
  const turns: TranscriptTurn[] = [];
  let question: UiMessage | null = null;
  for (const message of messages) {
    if (message.role === "user") {
      // Two questions in a row can only mean the first is still unanswered.
      if (question !== null) turns.push(unansweredTurn(question));
      question = message;
      continue;
    }
    turns.push(answerTurn(message, question, deps));
    question = null;
  }
  if (question !== null) turns.push(unansweredTurn(question));
  return turns;
}

/** A question with no reply under it yet: the bubble, and nothing invented. */
function unansweredTurn(question: UiMessage): TranscriptTurn {
  return { id: question.id, question: { text: question.text, meta: null }, steps: [] };
}

export function MessageList({
  messages,
  onViewSource,
  onThumbs,
  onFlagSource,
  onRetry,
  onOpenReceipt,
  opsReadable = false,
  progress = null,
}: {
  messages: readonly UiMessage[];
  onViewSource: (citation: Citation) => void;
  onThumbs?: (message: UiMessage, up: boolean) => Promise<void>;
  onFlagSource?: (message: UiMessage, citation: Citation) => Promise<void>;
  /** Re-send a failed turn's prompt. */
  onRetry?: (prompt: string, idempotencyKey?: string) => void;
  /** Dock a turn's Run record beside the thread, in place. */
  onOpenReceipt?: (runId: string) => void;
  /** The server's answer to whether this key holds the operations surface. */
  opsReadable?: boolean;
  /** Measured facts about the turn in flight; null when the thread is idle.
   * Only the streaming turn renders them. */
  progress?: TurnProgressFacts | null;
}) {
  // One clock for the thread, started only while a turn is in flight.
  const measured = useTurnProgress(progress);
  const announcement = messages.reduce<string | null>(
    (latest, message) => message.announcement ?? latest,
    null,
  );
  const turns = askTurns(messages, {
    opsReadable,
    progress: measured,
    onViewSource,
    ...(onThumbs ? { onThumbs } : {}),
    ...(onFlagSource ? { onFlagSource } : {}),
    ...(onRetry ? { onRetry } : {}),
    ...(onOpenReceipt ? { onOpenReceipt } : {}),
  });
  return (
    <>
      <p className="ne-visually-hidden" aria-live="polite" aria-atomic="true">
        {announcement}
      </p>
      <Transcript turns={turns} />
    </>
  );
}
