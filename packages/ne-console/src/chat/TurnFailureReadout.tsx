// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — WHAT A TURN THAT PRODUCED NO ANSWER SAYS, as the transcript's promoted
// failure card.
//
// Five stated lines, one per fact, in §2.4's grammar: fact, then next action, no
// arguing. The screen no longer writes a four-paragraph readout about what the
// deployment might be doing — a turn either produced an answer or it did not,
// and which of the five it was is the only thing the reader can act on.
//
// A REFUSAL IS NOT A FAULT and keeps its own word, its own shape and its own
// COLOUR: the classifier's `refused` kind mints a card of the `refusal` tone —
// slate, ⊘, "Refused", and no retry — while everything else mints the `fault`
// tone. The stated line is §5's, and the SERVER'S OWN REASON is carried through
// verbatim; that sentence is the only account of the decision that is not this
// console guessing. The product's argument depends on a reader telling "you
// were denied" from "it fell over" at a glance, and until the tone existed both
// were drawn as the same red fault card.
//
// A DELIBERATE STOP IS NEITHER, and now has the third tone rather than the word
// "Stopped" pinned to a fault card. Nothing broke and nothing was enforced, so
// the card spends no hue at all: ■, "Stopped", the line that is honest about
// what Stop does (the console stopped listening; a durable turn may still finish
// server-side), and a control labelled "Ask again" rather than "Try again",
// which would say the last attempt went wrong. It did what it was told.
//
// Explicit model selection failures also state their server reason directly:
// unlike a generic stage failure, 409 and 503 carry the authoritative reason
// the selected alias cannot run. Everything else keeps the server's words as
// secondary text behind a disclosure (§2.4). They belong to whoever has to fix
// the deployment, and they have never belonged in answer typography.

import { EvidenceDrawer } from "../primitives/EvidenceDrawer";
import type { TranscriptFailure, TranscriptRefusal } from "../transcript";
import {
  ASK_ASK_AGAIN,
  ASK_DECLINED,
  ASK_DECLINED_STATE,
  ASK_GROUNDING_UNAVAILABLE,
  ASK_NO_ANSWER,
  ASK_NO_DEPARTMENT,
  ASK_REFUSED_BY_POLICY,
  ASK_REFUSED_STATE,
  ASK_RETRY,
  ASK_RUN_DID_NOT_COMPLETE,
  ASK_RUN_UNREADABLE,
  ASK_STOPPED,
  ASK_STOPPED_STATE,
  ASK_STREAM_ERROR,
  ASK_TIMEOUT,
  ASK_TRY_AGAIN,
  ASK_TURN_FAILED,
  ASK_WITHHELD,
  ASK_WITHHELD_STATE,
} from "./askCopy";
import { explainStatedReason } from "./statedReason";
import type { TurnFailure } from "./turnFailure";

/** One classified turn failure as the transcript's one promoted failure card.
 * The card itself is transcript/Transcript.tsx — this decides only the words,
 * and which of them the reader can act on. */
export function askFailure(failure: TurnFailure, onRetry?: () => void): TranscriptFailure {
  // The classifier's own outcome class decides the tone. Not the reason string:
  // `kind` is what turnFailure.ts calls the decision, so every classification it
  // calls a refusal draws as one, including any future reason under that kind.
  if (failure.kind === "refused") return refusedFailure(failure);

  if (failure.reason === "model-unavailable" || failure.reason === "model-unverified") {
    const { detail } = failure;
    return {
      tone: "fault",
      state: ASK_TURN_FAILED,
      sentence:
        failure.reason === "model-unavailable"
          ? "The selected model is unavailable."
          : "Model availability couldn’t be verified.",
      ...(detail === "" ? {} : { action: <ServerWords details={[detail]} /> }),
    };
  }

  if (failure.reason === "no-department") {
    return { tone: "fault", state: ASK_TURN_FAILED, sentence: ASK_NO_DEPARTMENT };
  }

  if (failure.reason === "grounding-unavailable") {
    return { tone: "fault", state: ASK_TURN_FAILED, sentence: ASK_GROUNDING_UNAVAILABLE };
  }

  // The reader ended this turn. It is the third outcome class and it takes the
  // third tone: no hue, ■, "Stopped", and a control that says what it does
  // rather than apologising for a failure that did not happen. Ahead of the
  // generic branch below on purpose — `stopped` arrives as a `transport` reason
  // and would otherwise inherit the fault card and its "Try again".
  if (failure.reason === "stopped") {
    return {
      tone: "stopped",
      state: ASK_STOPPED_STATE,
      sentence: ASK_STOPPED,
      ...(failure.retryable && onRetry
        ? {
            action: (
              <button type="button" className="ne-btn ne-btn--quiet" onClick={onRetry}>
                {ASK_ASK_AGAIN}
              </button>
            ),
          }
        : {}),
    };
  }

  // THE SERVER'S STATED CAUSE OUTRANKS EVERY LINE BELOW IT. Where the Run
  // published a reason it explicitly marked publishable, that is what the
  // reader is told — translated where it speaks the validators' own closed
  // vocabulary (chat/statedReason.ts: the finding classes and reply-shape
  // tokens become stated English; the verbatim sentence moves whole into the
  // disclosure below, so no byte of the server's account is lost). The
  // console's own lines are what it says when the server said nothing, and
  // reaching for one over a stated cause is exactly how a governance refusal
  // came to be reported as a timeout.
  const stated =
    failure.statedReason !== undefined && failure.statedReason.trim() !== ""
      ? explainStatedReason(failure.statedReason)
      : null;
  const line =
    stated?.sentence ??
    (failure.reason === "timeout"
      ? ASK_TIMEOUT
      : failure.reason === "stream"
        ? ASK_STREAM_ERROR
        : failure.reason === "run-unreadable"
          ? ASK_RUN_UNREADABLE
          : failure.reason === "run-incomplete"
            ? ASK_RUN_DID_NOT_COMPLETE
            : ASK_NO_ANSWER);
  // §5 names the control "Try again" on the stream and timeout lines, and
  // "Retry" on the turn that simply produced nothing. Two words for two states
  // is the spec's choice, kept.
  const action = failure.reason === "none" ? ASK_RETRY : ASK_TRY_AGAIN;
  // Destructured, so the raw string has exactly one route out of this branch:
  // into the disclosure below. There is no expression here that could put it
  // anywhere else. The translated stated reason's VERBATIM original rides
  // beside it — the exact technical string stays one click away.
  const { detail } = failure;
  const disclosed = [
    ...(stated?.technical !== null && stated?.technical !== undefined ? [stated.technical] : []),
    ...(detail === "" || detail === stated?.technical ? [] : [detail]),
  ];

  return {
    tone: "fault",
    state: ASK_TURN_FAILED,
    sentence: line,
    action: (
      <>
        {failure.retryable && onRetry ? (
          <button type="button" className="ne-btn ne-btn--quiet" onClick={onRetry}>
            {action}
          </button>
        ) : null}
        {disclosed.length === 0 ? null : <ServerWords details={disclosed} />}
      </>
    ),
  };
}

/** The server's raw words, behind the disclosure they have always belonged in.
 *
 * EXPORTED because the Ask card is no longer the only surface that owes the
 * reader this. The Run receipt pane printed the same raw strings as its
 * headline "Reason" with no disclosure at all — the pane opened to PROVE
 * governance was the one showing `[aw=154 sources=2 maxrun=1 floor=5]` — so it
 * renders a stated line and reaches for THIS drawer for the verbatim, rather
 * than growing a second one that could drift from this summary wording. */
export function ServerWords({ details }: { details: readonly string[] }) {
  return (
    <EvidenceDrawer summary="what the server said" tone="quiet">
      {details.map((entry) => (
        <p className="ne-ask__turndetail" key={entry}>
          {entry}
        </p>
      ))}
    </EvidenceDrawer>
  );
}

/** §5: "Refused by policy." + the server's reason, verbatim.
 *
 * The REFUSAL tone, which is the whole point of the branch: this is the
 * governed system deciding, not the deployment falling over, and the card it
 * mints says so in slate with a ⊘ rather than in the fault red. `TranscriptRefusal`
 * carries no `action` field, so there is no expression here that could offer a
 * retry — a refusal is not undone by asking the same question again. A server
 * that sent no words is `null` and renders the em dash and a note; nothing here
 * writes a reason on the server's behalf. */
function refusedFailure(failure: TurnFailure): TranscriptRefusal {
  // THREE DIFFERENT DECISIONS, ONE TONE. All three are the governed system
  // working rather than the deployment breaking, so all three take the refusal
  // card — but a reader acts on each differently and each therefore keeps its
  // own state word and its own stated line. Flattening them into "Refused by
  // policy." would tell a reader whose SOURCE ACCESS changed that they asked
  // something forbidden, and would tell a reader whose agent honestly declined
  // to guess that a policy blocked them.
  const { state, sentence } =
    failure.reason === "declined"
      ? { state: ASK_DECLINED_STATE, sentence: ASK_DECLINED }
      : failure.reason === "withheld"
        ? { state: ASK_WITHHELD_STATE, sentence: ASK_WITHHELD }
        : { state: ASK_REFUSED_STATE, sentence: ASK_REFUSED_BY_POLICY };
  return {
    tone: "refusal",
    state,
    sentence,
    reason: failure.detail === "" ? null : failure.detail,
  };
}
