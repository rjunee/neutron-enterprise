// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE TYPED CHAT FAILURE.
//
// The console used to paste the server's raw exception class name into the
// answer bubble — `event.detail` -> `{message.error}` -> a bare `RedactionError`
// where an answer belongs. That made a POLICY REFUSAL and a SYSTEM FAULT
// byte-identical on screen, and the permission trap (two logins, one question,
// two results) is unobservable if both outcomes render as the same word.
//
// So the SSE `error` frame is classified HERE, once, and the classification is
// the only thing the answer surface reads. What it classifies into is now §5's
// copy table and nothing more: the screen renders one of five stated lines, so
// this module's job is to say WHICH, not to write a paragraph of its own.
//
//   refused        the policy decision point decided against this request.
//   no-department  the account carries no department claim, so nothing routed.
//   timeout        the console stopped waiting.
//   stream         the stream did not survive the turn.
//   stopped        the reader pressed Stop. The console stopped LISTENING — on
//                  a deployment that runs turns durably, the turn itself may
//                  still finish server-side, so this line never claims it died.
//   model-unavailable  the selected alias is no longer currently available.
//   model-unverified   current model availability could not be verified.
//   grounding-unavailable the advertised grounding session mapping went stale.
//   none           the turn settled and produced no answer.
//
// The server's own words survive verbatim in `detail`. They are rendered with
// policy and explicit model-selection verdicts, and are otherwise secondary
// text behind a disclosure. They are never the answer.

/** The three outcome classes a failed turn can belong to. */
export type TurnFailureKind = "refused" | "stage" | "transport";

/** Which of §5's stated lines this failure renders. */
export type TurnFailureReason =
  | "refused"
  | "no-department"
  | "timeout"
  | "stream"
  | "stopped"
  | "model-unavailable"
  | "model-unverified"
  | "grounding-unavailable"
  | "declined"
  | "withheld"
  | "run-unreadable"
  | "run-incomplete"
  | "none";

export interface TurnFailure {
  kind: TurnFailureKind;
  reason: TurnFailureReason;
  /** The server's VERBATIM words. Never rewritten, never summarised. */
  detail: string;
  /** True when trying again is a sensible next action — a fault, not a verdict. */
  retryable: boolean;
  /**
   * THE SERVER'S OWN SENTENCE ABOUT WHY, when it published one.
   *
   * Distinct from `detail`, and the distinction is the whole point. `detail` is
   * raw server words that belong behind the "what the server said" disclosure;
   * this is a reason the server explicitly MARKED publishable to a reader, so
   * it is promoted into the stated line itself and replaces the console's
   * generic one. A run that died on a governance refusal must not be reported
   * to the reader as "no answer arrived in time".
   *
   * Absent means the server published no marked reason — and then the console's
   * own stated line stands, because it is the only honest thing left to say.
   * Nothing writes this on the server's behalf.
   */
  statedReason?: string;
}

/**
 * Classify one failed turn from what the server put on the SSE `error` frame.
 *
 * THE INPUT IS THE SERVER'S OWN WORDS AND NOTHING ELSE. The patterns below are
 * read off the string the server actually sent, and that is the only kind of
 * string this function may be handed: it is matching a REMOTE vocabulary, which
 * changes only when the server changes. Nothing here speculates about a root
 * cause the wire did not carry; anything the server did not say stays in
 * `detail` and is not turned into a sentence.
 *
 * IT IS NOT FOR A THROWN TRANSPORT ERROR — use `thrownTransportFailure`. A
 * throw carries no server vocabulary, only whatever the client or the runtime
 * composed, and matching prose there classified by accident: a detail-less
 * stream death used to arrive carrying the client's own transport string, which
 * happened to contain two of the words the stream pattern below looks for. The
 * moment that string
 * became a sentence written for the reader, every dead stream started
 * reporting "no answer was produced" — a different fact, and a false one.
 */
export function classifyTurnFailure(detail: string): TurnFailure {
  const d = detail.toLowerCase();

  // --- REFUSED: the governed system decided. Not a fault. -----------------
  if (
    /\b(policydenied|policy denied|pdp|deny|denied|forbidden|not authorized|403)\b/.test(d) ||
    /\bair[_ -]?gap\b/.test(d)
  ) {
    return { kind: "refused", reason: "refused", detail, retryable: false };
  }
  if (/guardrail|injection|blocked/.test(d)) {
    return { kind: "refused", reason: "refused", detail, retryable: false };
  }

  // --- The console's own clock, when the server named the timeout. --------
  if (/timeout|timed out|deadline/.test(d)) {
    return { kind: "transport", reason: "timeout", detail, retryable: true };
  }

  // --- The stream did not survive. ----------------------------------------
  if (/unreachable|econnrefused|network|failed to fetch|stream failed|connection/.test(d)) {
    return { kind: "transport", reason: "stream", detail, retryable: true };
  }

  // --- Anything else: the turn ended without an answer. -------------------
  return { kind: "stage", reason: "none", detail, retryable: true };
}

/**
 * A turn that died on a THROWN transport error, classified from the throw.
 *
 * NO PROSE IS READ. The inputs are the error's own structured facts — the fact
 * that it threw at all, and the status it carried — because those are the only
 * things about a transport failure the console did not write itself. `detail`
 * is carried through to the disclosure and is never matched against; rewording
 * every sentence in the console cannot move a single verdict here.
 *
 * `status` is the server's status code, or null when there was no HTTP response
 * at all (fetch rejected, the runtime threw). Three outcomes, and each is a
 * different FACT rather than a different wording of one:
 *
 *   403  the gate decided. A refusal, not a fault: no retry, refusal tone.
 *   422  the session create's no-department answer, which has its own line.
 *   ELSE the answer stream did not survive. That includes a 409 the
 *        conversation refused and a 5xx from a proxy that never reached the
 *        API: no stream was ever opened, the server's own words (when it sent
 *        any) ride in the disclosure, and the line says the stream failed
 *        rather than claiming the turn ran and produced nothing.
 */
export function thrownTransportFailure(status: number | null, detail: string): TurnFailure {
  if (status === 403) return { kind: "refused", reason: "refused", detail, retryable: false };
  if (status === 422) return noDepartmentFailure(detail);
  return { kind: "transport", reason: "stream", detail, retryable: true };
}

/** The 422 the session create answers when the account carries no department
 * claim. A distinct fact with its own stated line — never a generic failure. */
export function noDepartmentFailure(detail: string): TurnFailure {
  return { kind: "stage", reason: "no-department", detail, retryable: false };
}

/** The capability read advertised a grounded agent, but session bootstrap
 * rejected that exact mapping. This is not the account's department 422. */
export function groundingUnavailableFailure(detail: string): TurnFailure {
  return { kind: "stage", reason: "grounding-unavailable", detail, retryable: false };
}

/** Whether a 403/409/503 on a model-carrying turn is actually a verdict ON THE
 * MODEL CHOICE, read off the server's own words rather than the status alone.
 *
 * `detail` MUST be the server's own `detail`, never a sentence the console
 * composed for the reader. The vocabulary matched below is the server's, so a
 * house sentence in its place matches nothing and turns every detail-less
 * verdict into a silent miss.
 *
 * REGRESSION GUARD: a turn that requested a model can still die on a verdict
 * about something else entirely — the canonical-Ask admission answers 409
 * "canonical default Ask conflict" when the CONVERSATION cannot accept the
 * turn, and 503 for its own bootstrap/queue seams. Classifying those by status
 * rendered "The selected model is unavailable." for a conflict that had
 * nothing to do with the model. The model-selection verdicts are a closed
 * server vocabulary (ne-api routers/models.py: "requested model is
 * unavailable[: alias]", "no model is currently available", "model selection
 * [evidence] is unavailable") and every one of them names the model; the
 * admission details never do. A 403 stays a policy refusal either way — that
 * card carries the server's words verbatim. */
export function isModelSelectionVerdict(status: 403 | 409 | 503, detail: string): boolean {
  return status === 403 || /\bmodel\b/i.test(detail);
}

/** A refusal/unavailable verdict on an explicit model choice. These statuses
 * invalidate the evidence that put the alias in the picker, so retrying the
 * same turn without first refreshing the server catalog is never offered. */
export function modelSelectionFailure(status: 403 | 409 | 503, detail: string): TurnFailure {
  if (status === 403) {
    return { kind: "refused", reason: "refused", detail, retryable: false };
  }
  return {
    kind: "stage",
    reason: status === 409 ? "model-unavailable" : "model-unverified",
    detail,
    retryable: false,
  };
}

/**
 * The readout for a turn that was ended on purpose. Three different facts:
 *
 *   deadline  the console's absolute give-up time elapsed.
 *   stall     the server went silent for longer than the stall window.
 *   reader    the reader pressed Stop.
 *
 * The first two are the same stated line ("No answer arrived in time"). The
 * third has its OWN line, and the line is honest about what Stop does: it stops
 * the console listening. Whether the turn itself died with the connection
 * depends on the deployment (an in-process turn is cancelled; a durable turn
 * keeps running and its approvals stay actionable), and the console cannot see
 * which — so the copy says "may still finish" and never claims the turn is dead.
 */
export function abortFailure(reason: "deadline" | "stall" | "reader", detail: string): TurnFailure {
  if (reason === "reader") return { kind: "transport", reason: "stopped", detail, retryable: true };
  return { kind: "transport", reason: "timeout", detail, retryable: true };
}

/**
 * THE AGENT DECLINED. `output_verdict === "refused"` on a completed Run.
 *
 * The `refused` KIND, so it takes the refusal tone — slate, ⊘, no retry — and
 * can never be drawn as the red fault card. The declining agent's own words
 * ride in `detail` and are rendered as the decision's reason, verbatim; a Run
 * that protected no refusal text passes "" and the card says the em dash
 * rather than inventing a reason.
 *
 * NOT retryable: asking the identical question again does not undo a decision.
 */
export function declinedFailure(detail: string): TurnFailure {
  return { kind: "refused", reason: "declined", detail, retryable: false };
}

/**
 * The answer exists; current source authority no longer authorizes disclosing
 * it to this reader (ne-api `SourceBoundRunOutputReader` -> `withheld`).
 *
 * ALSO the `refused` kind, and for the same structural reason: a boundary was
 * enforced. It is a different FACT from a decline — nobody judged the question
 * — so it carries its own reason and its own stated line, and `detail` stays
 * empty because there is no server sentence behind it, only a decision code.
 */
export function withheldFailure(): TurnFailure {
  return { kind: "refused", reason: "withheld", detail: "", retryable: false };
}

/** The Run could not be read back after repeated attempts. A fault of the
 * CONSOLE'S READ, not a verdict on the turn — so the line says the Run may
 * still be running and the retry offered re-asks rather than claiming a
 * failure. The transport reason keeps it out of the refusal vocabulary. */
export function runUnreadableFailure(detail: string): TurnFailure {
  return { kind: "transport", reason: "run-unreadable", detail, retryable: true };
}

/**
 * The Run reached a terminal state that is not `completed`. The server's own
 * status word is the detail; nothing here translates it into a cause.
 *
 * `statedReason` is the OPT-IN safe reason the Run published (ne-api
 * `run_projection._failure_reason`). Where the server marked one, it becomes
 * the stated line and the reader learns WHY; where it did not, the line stays
 * the console's generic one, because inventing a cause is the defect this
 * parameter exists to end, not a licence to guess.
 *
 * The classification is NOT changed by the presence of a reason. Deciding that
 * a stated cause makes a turn a governance refusal would be this console
 * writing a verdict, which it does nowhere else; a genuine refusal already
 * arrives as `output_verdict === "refused"` and has its own path.
 */
export function runIncompleteFailure(detail: string, statedReason?: string | null): TurnFailure {
  return {
    kind: "stage",
    reason: "run-incomplete",
    detail,
    retryable: true,
    ...(typeof statedReason === "string" && statedReason.trim() !== "" ? { statedReason } : {}),
  };
}

/** The stream closed without a terminal frame: the turn neither finished nor
 * refused nor errored — the CONNECTION ended. Rendered as the stream line;
 * without this the message would blink its cursor forever over a dead wire. */
export function streamEndedFailure(): TurnFailure {
  return { kind: "transport", reason: "stream", detail: "", retryable: true };
}
