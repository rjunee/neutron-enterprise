// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE TRANSCRIPT VIEW MODEL — one turn is one legible unit.
//
// This module is PRESENTATIONAL. It holds no client, no clock, no derivation of
// a governance fact: every value below is something a server said, carried here
// by an adapter that owns its own wire shape (Ask reads chat/answerFrames.ts and
// the SSE frames; a Run reads agents/types.ts). Two consumers, one anatomy.
//
// THE ABSENCE RULE, which is the reason these fields are nullable rather than
// defaulted. A value the server did not serve renders as the em dash plus a
// short factual note ("Model · — no model reply was served"). It never renders
// as "No", never as 0, never as a plausible sample. There is no field on this
// model that a caller can fill by inference, and no code path in this module
// that turns a null into a number.
//
// THE PROMOTION RULE lives in promotion.ts and is the one behaviour here that is
// not a straight render: routine allowed steps demote into the collapsed work
// band, and anything pending a human decision or refused promotes OUT of it into
// a gate card that never collapses. The tool calls are the governance story, so
// nothing governance-bearing is hidden — only compressed.

import type { ReactNode } from "react";

/** What the policy decision point said about one step, as the server reported.
 * `pending` and `refused` PROMOTE (promotion.ts); the rest stay in the band. */
export type TranscriptStepVerdict = "allowed" | "running" | "failed" | "pending" | "refused";

/** One governed tool call, as one row of the band's step ledger. */
export interface TranscriptStep {
  /** Stable React key. The adapter's own identity for the transit. */
  readonly key: string;
  /** The tool name, verbatim. */
  readonly name: string;
  /** The server's verdict. The console decides none of these. */
  readonly verdict: TranscriptStepVerdict;
  /** The word drawn in the row's verdict slot ("Allowed", "Running", "Error").
   * Supplied by the adapter, which owns its wire's vocabulary. */
  readonly verdictLabel: string;
  /** Server-resolved policy tier. `null` renders the em dash — never a 0. */
  readonly tier: number | null;
  /** Duration the SERVER measured, in seconds. `null` renders the em dash;
   * this module owns no clock and can produce no substitute. */
  readonly durationSeconds: number | null;
  /** Optional served aux text between the name and the verdict — the Run
   * render's `step_intent` ("renewal terms · Allowed"). It is drawn FIRST in
   * the aux slot, ahead of the tier, because the act is what a reader scanning
   * a working turn is looking for; the tier still always draws (Transcript.tsx). */
  readonly intent?: string | null;

  /**
   * HOW THIS ROW RANKS AGAINST ITS SIBLINGS. Presentational only — one step of
   * type contrast (transcript.css `.tr-step--act`), nothing else. It sets no
   * verdict, is not counted by `bandTally`, and `isPromoted` never reads it, so
   * no rank can move a row into or out of the fold.
   *
   *   `act`     the TOOL CALL itself — the thing the agent did. The only rows
   *             that get it are the ones that carry a real, server-served tool
   *             name (a plan row with its `step_intent`, or a tool receipt the
   *             chain named).
   *   `detail`  the recorded machinery around it — a model hop, a governed
   *             check. Real work, genuinely subordinate to the act. Default.
   *
   * WHY IT EXISTS. Fifteen rows of identical weight is not a progress display;
   * a reader could not find the one row that said what the agent was doing.
   * Ranking is the alternative to deleting the quieter rows, which would be a
   * thinner account of the same run.
   */
  readonly rank?: "act" | "detail";

  /**
   * THIS ROW STANDS FOR `n` RECORDED EVENTS, not one — the fold.
   *
   * The Run's projection clamps every kind outside its safe set to
   * `event.unavailable`, and such rows' entire content is "Step · Not
   * disclosable". They are a detail OF the work, not peers of it, and
   * chat/runActivity.ts folds a consecutive run of them into ONE row carrying
   * this count. It folds ONLY those: a step the server named — which is now
   * every kind and every governed audit action it enumerates — is a peer and
   * keeps its own row.
   *
   * WHAT THE FOLD MAY NOT DO. It never folds a `withheld` record and never
   * folds anything that promotes: "Not disclosable" is a governance statement
   * about content, and a count of such rows is still that statement, but a
   * withheld or refused step keeps its own word and its own row. Absent here
   * means the row is exactly one recorded event.
   */
  readonly foldedCount?: number;

  /**
   * THE ROLLED-UP ROW'S OWN LEDGER — the recorded events this row compresses,
   * each as a full row of its own, one disclosure away.
   *
   * A row that stands for more than one recorded event (`foldedCount`), or that
   * says in plain language what its event says in schema vocabulary, carries
   * the verbatim per-event rows here. The renderer draws them inside a nested
   * disclosure on the row itself, so the compression DEMOTES the detail and
   * never deletes it: every served identifier, code and count is still on the
   * screen, one hit away. Presentational only — `bandTally` never counts these
   * (the parent's `foldedCount` already does), `isPromoted` never reads them,
   * and nothing in a detail row can promote (the adapter only rolls up rows
   * that stay in the band).
   */
  readonly detail?: readonly TranscriptStep[];

  /**
   * THE FRONTIER OF WHAT THE SERVER HAS REPORTED — presentational only.
   *
   * True on at most ONE row: the most recent thing the Run recorded, and only
   * while the Run has not reached a terminal state. It is NOT a verdict and NOT
   * a claim that this step is still executing — `bandTally` never counts it,
   * `isPromoted` never reads it, and it cannot move a row into or out of the
   * fold. It says exactly one thing, which the wire does state: nothing newer
   * than this has been recorded yet.
   *
   * WHY IT EXISTS. A canonical Ask turn runs 60–90 s and the band was a list of
   * finished-looking rows the whole time, so a reader could not tell a working
   * Run from a stalled one. Marking the frontier is the honest alternative to a
   * spinner on a step whose execution state the wire never reports.
   */
  readonly latest?: boolean;

  // ---- carried only by a step that PROMOTES (verdict pending / refused) ----
  // A step that stays in the band ignores all three. They live on the step
  // rather than on a parallel `gates` list so that the promotion rule has ONE
  // input to decide from and callers cannot pre-sort their way around it.

  /** The server's sentence about this decision, VERBATIM. This module never
   * rewrites it, and writes none of its own when it is absent — an absent
   * sentence renders the em dash and a note. */
  readonly sentence?: string | null;
  /** The governed reference — approval id, decision id, plus whatever the Run
   * render appends ("approval-9d44 · requested 12:06 · decision due 16:00").
   * Absent renders the em dash: a gate with no reference says so. */
  readonly reference?: string | null;
  /** The gate's one action, rendered only when the caller resolved a route to
   * it. A control with nothing behind it is a fabricated capability. */
  readonly action?: ReactNode;
  /** The promoted state word. Defaults to the verdict's own word. */
  readonly stateLabel?: string;
}

/** A step that pends a human decision or was refused. It is a different object
 * from a step row, not a louder one: it leaves the band, takes the turn's only
 * hue, holds the action, and never collapses.
 *
 * DERIVED, never authored: `partitionTurnSteps` (promotion.ts) produces these
 * from the promoted steps. There is no way for a caller to hand the transcript
 * a gate that did not come from a step the server reported. */
export type TranscriptGateKind = "pending" | "refused";

export interface TranscriptGate {
  readonly key: string;
  readonly kind: TranscriptGateKind;
  /** The state word — "Awaiting approval", "Refused". */
  readonly state: string;
  /** The tool name the decision is about. */
  readonly name: string;
  /** Server-resolved tier; `null` renders the em dash. */
  readonly tier: number | null;
  /** The server's sentence, verbatim; `null` renders the em dash and a note. */
  readonly sentence: string | null;
  /** The governed reference; `null` renders the em dash. */
  readonly reference: string | null;
  /** The one action, when the caller resolved a route to it. */
  readonly action?: ReactNode;
}

/** The turn's answer — the only unboxed prose in a turn. */
export interface TranscriptAnswer {
  /** "Answer" on Ask, "Output" on a Run. */
  readonly label: string;
  /** ANSWER TEXT ONLY. The caller classifies the stream before it gets here
   * (chat/answerFrames.ts): reasoning and undispatched tool-call frames are a
   * different kind of thing and never reach this field. */
  readonly body: string;
  /** True while the answer is still arriving — draws the caret. */
  readonly streaming?: boolean;
  /** Why there is no body, when there is none: the paused Run's "No safe output
   * preview is available while the Run is paused." Rendered after the em dash. */
  readonly absentNote?: string | null;
  /** Citation chips the caller places after the prose. The wire carries no span
   * offsets, so nothing here guesses a sentence position. */
  readonly trailing?: ReactNode;
}

/** ONE SERVED ROW ANCHOR — the canonical hash of a verified row the answer
 * rests on, carried by the adapter verbatim off the evidence wire. A verified
 * row has no passage to open, so it is not a `TranscriptSource`; its identity
 * hash is the whole citation, and it renders in the source block as exactly
 * that. This module never computes, truncates into oblivion, or compares one:
 * the short form always carries the full served value with it. */
export interface TranscriptSourceAnchor {
  /** Stable render key — the adapter's identity for the evidence record. */
  readonly key: string;
  /** The served canonical row hash, verbatim. */
  readonly anchor: string;
}

/** One cited source, subordinate to the answer and still one hit away. */
export interface TranscriptSource {
  readonly key: string;
  /** The bracketed ordinal the answer's chips refer to. */
  readonly ordinal: number;
  /** Source system and document as served; `null` renders the em dash. */
  readonly system: string | null;
  readonly document: string | null;
  /** "View source" / "Flag" — the caller owns both. */
  readonly actions?: ReactNode;
}

/** The measured in-flight line. Never simulated: both numbers are handed in.
 * This module starts no timer — see the structural test in transcript.test.tsx. */
export interface TranscriptProgress {
  /** Elapsed seconds as MEASURED by the caller. `null` renders the em dash. */
  readonly elapsedSeconds: number | null;
  /** The caller's liveness sentence ("last word from the server 1s ago").
   * `null` renders the em dash and a note: this module writes no substitute. */
  readonly liveness: string | null;
}

/** WHY the turn ended without an answer, which is three different events:
 *
 *   fault    something broke — no answer arrived, the model failed, the stream
 *            died. Red, ✕, and a retry may be worth offering.
 *   refusal  policy refused the whole turn. That is the governance system
 *            working, not an outage: it takes --state-gated and ⊘, the same
 *            vocabulary a refused STEP's gate card and posture's whole-read
 *            refusal already use, and it offers nothing to click.
 *   stopped  the READER ended the turn. Not a fault and not a refusal: nobody
 *            enforced anything and nothing broke, so it spends no hue at all —
 *            the shell's own neutrals, a ■, and the state word. A colour here
 *            would say something happened TO the reader.
 *
 * Drawing a refusal in the fault's colour is the defect --state-gated was minted to fix
 * (tokens.css: a role-scoped refusal in the fault ramp put three red blocks on
 * a reader's home screen); drawing a deliberate stop in it is the same defect
 * one step further out, because there is not even a boundary to report. The
 * discriminator is REQUIRED on every member, so the fault treatment cannot be
 * reached by saying nothing. */
export type TranscriptFailureTone = "fault" | "refusal" | "stopped";

interface TranscriptFailureBase {
  /** The state word — "Turn failed", "Refused". Drawn beside the glyph, never
   * carried by colour alone. */
  readonly state: string;
  /** The stated line for this failure class. Verbatim from the caller, which
   * classified it (chat/turnFailure.ts); this module writes no failure prose. */
  readonly sentence: string;
}

/** Something broke. */
export interface TranscriptFault extends TranscriptFailureBase {
  readonly tone: "fault";
  /** "Retry" / "Open trace", when the caller can resolve them. */
  readonly action?: ReactNode;
}

/** Policy refused the turn. There is no `action` field ON THIS TYPE: a refusal
 * is not undone by clicking again, so a retry affordance here is not merely
 * discouraged, it is unrepresentable. */
export interface TranscriptRefusal extends TranscriptFailureBase {
  readonly tone: "refusal";
  /** The server's reason for the decision, VERBATIM — the only account of it
   * that is not this console guessing. Required, and `null` when the server
   * sent none: that renders the em dash and the same note the gate card uses.
   * There is no branch here that writes a reason on the server's behalf. */
  readonly reason: string | null;
}

/** The reader pressed Stop. It carries an `action` because asking again is a
 * perfectly ordinary next move — but nothing on this type can make the card
 * loud, and the tone it declares is the one the receipt closes with. */
export interface TranscriptStopped extends TranscriptFailureBase {
  readonly tone: "stopped";
  /** "Ask again", when the caller can resolve it. */
  readonly action?: ReactNode;
}

/** The turn ended with no answer, so there is no answer to rank first. */
export type TranscriptFailure = TranscriptFault | TranscriptRefusal | TranscriptStopped;

/** One consolidated fact in the receipt line. */
export interface TranscriptFact {
  readonly key: string;
  /** "Claims", "Verdict" — or omitted for a bare span ("Awaiting a decision"). */
  readonly label?: string | null;
  /** `null` renders the em dash followed by `note`. Never "No", never 0. */
  readonly value: string | null;
  /** The short factual note that stands beside an em dash ("pending"). */
  readonly note?: string | null;
  /** Draw the value in the data face. */
  readonly mono?: boolean;
}

/** The receipt's tone, and the reason it is written as a UNION WITH THE FAILURE
 * TONE rather than as a parallel enum of its own.
 *
 * The receipt used to have three tones of its own — ok / paused / failed — so a
 * turn the policy decision point refused closed with "✕ Failed" in red one line
 * under a grey ⊘ REFUSED card. The footer contradicted the card, and the reader
 * who takes the loudest thing on screen as the outcome was told the deployment
 * had broken. Two enums for one fact is what made that expressible.
 *
 * There is now ONE vocabulary. A turn that ended in a failure closes with THAT
 * FAILURE'S OWN TONE — `tone: failure.tone`, an identity, not a mapping — so the
 * footer cannot disagree with the card, cannot be recoloured without recolouring
 * the card, and cannot fall back to the fault by omission: there is no branch
 * left to omit. `ok` and `paused` are the two outcomes no failure carries.
 * TONE_GLYPH (Transcript.tsx) is keyed by this type and is the ONE glyph table
 * both the card and the receipt read, so the ✕/⊘/■ they draw is one value. */
/**
 * `live` — THE TURN HAS NOT CLOSED, and the reason this tone had to exist.
 *
 * There was no word in this vocabulary for a turn still in flight, so the run
 * adapter's tone mapping fell through its `default` and every `submitted`,
 * `queued` and `running` Run closed `fault`: a red ✕ beside the word "Running",
 * on a turn the server had not finished, next to "no output was served" — a
 * sentence about a FINISHED turn. The footer asserted an outcome the server had
 * not stated, which is the one thing this module exists not to do.
 *
 * It takes ● and --cherenkov, the same glyph and hue a RUNNING STEP already
 * draws, so live means one thing on this screen. It is not a fourth failure
 * treatment and carries no hue from the fault ramp.
 */
export type TranscriptReceiptTone = "ok" | "paused" | "live" | TranscriptFailureTone;

/** The turn's one governance footer — and its closing edge. Exactly one of
 * these renders per turn (transcript.test.tsx pins the cardinality). */
export interface TranscriptReceipt {
  /** "Complete" / "Completed" / "Paused" / "Failed" / "Refused" / "Stopped" —
   * the word for the tone beside it, supplied by the adapter that owns the
   * wire's vocabulary. */
  readonly outcome: string;
  readonly tone: TranscriptReceiptTone;
  /** The verification pill, when the server made a claim. Absent means the
   * server made none, and ordinary generation stays unbadged. */
  readonly pill?: ReactNode;
  /** Ordered served facts between the outcome and the model. */
  readonly facts?: readonly TranscriptFact[];
  /** The SERVED model alias. `null` renders "Model · — {modelNote}". */
  readonly model: string | null;
  /** The factual note beside an absent model ("no model reply was served"). */
  readonly modelNote?: string | null;
  /** "Open trace", when the server served a correlation to open. */
  readonly trace?: ReactNode;
  /** Feedback / Evidence / Governance — the caller owns them. */
  readonly actions?: ReactNode;
}

/** One turn: a question, the work it did, what it answered, and one receipt. */
export interface TranscriptTurn {
  readonly id: string;
  /**
   * THE CANONICAL RUN BEHIND THIS TURN, when the adapter knows one.
   *
   * Identity only, for reconciliation: the same turn can reach a thread twice —
   * once as the live streamed/polled copy and once as the conversation store's
   * read-back copy — and the consumer that holds both must be able to say
   * "same Run, render once" without parsing receipt prose. No renderer in this
   * module reads it; it draws nothing.
   */
  readonly runId?: string;
  /** The question bubble — the turn's opening edge. `meta` is the served stamp
   * ("12:02", "Run 3 · 11:58 · retry of Run 2"); `null` renders nothing. */
  readonly question: { readonly text: string; readonly meta?: string | null };
  /** Every governed step of this turn, in arrival order. promotion.ts splits
   * them into the band and the gate cards — callers do not pre-sort. */
  readonly steps: readonly TranscriptStep[];
  /** The band's total elapsed, as SERVED. `null` renders the em dash. */
  readonly workElapsedSeconds?: number | null;
  /** The band's accessible name — "Steps in this turn" / "Steps in this Run". */
  readonly workLabel?: string;
  /**
   * WHY THE LEDGER IS THINNER THAN THE RUN, said inside the ledger.
   *
   * The Run's tool plan is all-or-nothing: one ordinal gap nulls the whole
   * field, and a tool call rejected by name nulls it permanently for the rest
   * of that Run. When that happens there are no tool rows to draw, and the
   * honest move is to SAY so — not to reconstruct a plan from the event log,
   * which carries no tool name at all.
   *
   * It is a note, not a step: it draws no glyph, carries no verdict, and
   * `bandTally` never sees it, so the précis cannot count a sentence as work.
   */
  readonly workNote?: string | null;
  /** Force the band open. Omitted, it opens only while a step is running. */
  readonly workOpen?: boolean;
  readonly failure?: TranscriptFailure | null;
  readonly progress?: TranscriptProgress | null;
  readonly answer?: TranscriptAnswer | null;
  readonly sources?: readonly TranscriptSource[];
  /**
   * WHY THE SOURCE LIST IS SHORTER THAN THE EVIDENCE, or empty — stated, never
   * left as a gap.
   *
   * An answer with no sources under it used to render as nothing at all, which
   * on this product reads as "this answer cited nothing" whether that is what
   * happened or not. The sentence distinguishes the cases the server actually
   * distinguishes (chat/runCitations `citationsNote`): no claim was associated
   * with evidence, the evidence has no retrievable passage, or some cited
   * sources are no longer readable with the reader's current access. It is a
   * NOTE and not a source: it has no ordinal, no actions, and the "Sources · N"
   * count never includes it.
   */
  readonly sourcesNote?: string | null;
  /**
   * THE SERVED ROW ANCHORS, when the answer rests on verified rows. Drawn in
   * the source block under the source lines: each is the server's canonical
   * hash of one governed row, short form on screen, full value in the title
   * and one copy away. The adapter (chat/runCitations via agents/rowAnchors)
   * owns which records qualify; this module only draws what it is handed.
   */
  readonly sourceAnchors?: readonly TranscriptSourceAnchor[];
  /**
   * THE THIRD STATE OF THE SOURCE BLOCK: the turn has settled and the adapter's
   * citation read is STILL IN FLIGHT. It is the caller's sentence for that
   * window ("Resolving citations…"), drawn in the source block's place in its
   * own pending treatment — visibly NOT the settled `sourcesNote`, because
   * "the read has not landed yet" and "the read landed and found nothing" are
   * different facts and must not look alike. The caller owns its terminal
   * condition: it clears the field when the read resolves, fails, or exceeds
   * its deadline, and this module draws no timer of its own. When any of
   * `sources`, `sourceAnchors` or `sourcesNote` is present this field is
   * ignored — a landed read outranks the claim that one is pending.
   */
  readonly sourcesPending?: string | null;
  readonly receipt?: TranscriptReceipt | null;
}
