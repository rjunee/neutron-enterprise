// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE TRANSCRIPT — the turn system, as one presentational module.
//
// Composed from design/round3/transcript/ask.html and agent-run.html; the
// renders govern the anatomy and the `tr-*` class vocabulary. Ask (its thread,
// composer and source pane) and the Agents Run workspace both mount this, each
// through its own adapter — this file knows about neither.
//
// WHAT THIS MODULE WILL NOT DO, stated as code rather than as a comment:
//
//   * It never invents a value. Every absent field goes through <Absent>, which
//     draws the em dash and the caller's short factual note. There is no branch
//     here that renders "No", a 0, or a plausible-looking sample row.
//   * It never runs a clock. The elapsed seconds and the liveness sentence on
//     the streaming line are handed in already measured; nothing here ticks, and
//     transcript.test.tsx pins that structurally.
//   * A TIMING SLOT WITH NOTHING BEHIND IT DRAWS NOTHING (owner ruling,
//     2026-08-24), and the same rule now makes those slots DRAW: the Runs event
//     wire stamps every event with the server's own `created_at`, so
//     chat/runActivity measures each row against the next record and the band
//     against its own first and last. The slots are still omitted entirely when
//     there is nothing behind them — no dash, no note, no element holding space
//     — and the rule underneath is unchanged: nothing here may compute,
//     estimate or client-time a duration, and every number it draws was
//     measured on the server's clock.
//   * THE TIER SLOT OBEYS THE SAME RULE AS OF THIS CHANGE. It used to always
//     draw, so a Run band — whose wire carries a tier on no kind at all — read
//     "Tier —" on every one of fifteen rows and buried the part of each line
//     that said something. A resolved tier still always draws.
//   * It never decides a verdict. `allowed`, `refused`, `pending` come off the
//     wire; promotion.ts only decides WHERE each one is drawn.
//
// THE ONE ASYMMETRY, and the reason this unit exists: routine allowed steps
// demote into the collapsed work band, whose summary line is the audit précis;
// anything pending a human decision or refused promotes into a gate card that
// is a SIBLING of the band, not a child of it. A reader who never expands
// anything still sees every gate — see promotion.ts for the rule and
// transcript.test.tsx for the control that proves the containment both ways.

import type { ReactNode } from "react";
import { NeutronMark } from "../assets/NeutronMark";
import { ABSENT } from "../primitives/format";
import { HashChip } from "../primitives/HashChip";
import { proseBlocks, renderInline } from "./prose";
import {
  bandNames,
  bandStepCount,
  bandTally,
  formatSeconds,
  hasLiveStep,
  partitionTurnSteps,
} from "./promotion";
import type {
  TranscriptAnswer,
  TranscriptFact,
  TranscriptFailure,
  TranscriptFailureTone,
  TranscriptGate,
  TranscriptProgress,
  TranscriptReceipt,
  TranscriptReceiptTone,
  TranscriptSource,
  TranscriptSourceAnchor,
  TranscriptStep,
  TranscriptTurn,
} from "./types";
import "./transcript.css";

/** The word on the frontier row (`TranscriptStep.latest`). Deliberately not
 * "Running": the Runs event wire reports no per-step execution state, so the
 * only defensible claim is about the RECORD — this is the newest one, and there
 * is nothing after it yet. */
const LATEST_STEP_WORD = "latest";

/** THE ROW ANCHOR VOCABULARY — one set of words for every surface that draws a
 * served row anchor (this source block, the run receipt, the evidence drawer,
 * the run compare view), so the same fact is never named two ways. The words
 * are presentational and adapter-free, which is why they live here and not in
 * an adapter module. */
export const ROW_ANCHOR_LABEL = "Row identity";
/** The one-line gloss (title text) explaining what the value is. */
export const ROW_ANCHOR_GLOSS =
  "The server's canonical hash of the verified row this answer rests on. Identical served rows carry an identical hash — it is the row's identity, served with the evidence, never computed here.";
/** The copy button's accessible name. */
export const ROW_ANCHOR_COPY_LABEL = "Copy row anchor";

/** The em dash, and the short factual note that says why the value is missing.
 * The ONE way this module renders an absent value. */
function Absent({ note }: { note?: string | null }) {
  return (
    <span className="tr-absent">
      {ABSENT}
      {note ? <span className="tr-absent__note"> {note}</span> : null}
    </span>
  );
}

/** A served duration, or the em dash. There is no third branch: this module
 * cannot measure a duration the server did not send. */
function Duration({ seconds, note }: { seconds: number | null; note?: string | null }) {
  if (seconds === null) return <Absent note={note ?? "no duration served"} />;
  return <>{formatSeconds(seconds)}</>;
}

const STEP_GLYPH: Record<TranscriptStep["verdict"], string> = {
  allowed: "✓",
  running: "●",
  failed: "✕",
  pending: "⏸",
  refused: "⊘",
};

/* ------------------------------------------------------------ question -- */

function Question({ question }: { question: TranscriptTurn["question"] }) {
  return (
    <div className="tr-q">
      <div className="tr-q__bubble">{question.text}</div>
      {question.meta ? <span className="tr-q__meta">{question.meta}</span> : null}
    </div>
  );
}

/** One step row's line — glyph, name, aux, frontier word, time. Extracted so a
 * rolled-up row's summary and its nested per-event rows draw the SAME grammar,
 * rather than two copies that can drift apart. */
function StepLine({ step }: { step: TranscriptStep }) {
  return (
    <>
      <span className="tr-step__glyph" aria-hidden="true">
        {STEP_GLYPH[step.verdict]}
      </span>
      {/* THE FULL IDENTIFIER IS ALWAYS ON THE ELEMENT. The name may be
          clipped to an ellipsis by transcript.css when the band is drawn
          into a narrow column; it is never shortened in the DATA, and the
          title puts the whole of it one hover away. */}
      <span className="tr-step__name" title={step.name}>
        {step.name}
      </span>
      <span className="tr-step__sep" aria-hidden="true">
        ·
      </span>
      {/* THE ACT LEADS. The served intent — the sentence the Run recorded
          for this tool call — comes first, because it is what a reader
          watching a working turn is actually looking for.

          THE TIER SLOT IS DRAWN ONLY WHEN THERE IS A TIER. It used to
          draw "Tier —" on every row, and the Runs event wire carries a
          tier on NONE of them, so a fifteen-row band repeated the same
          em dash fifteen times and buried the one part of each line that
          said anything. An absent governance fact is stated by its
          absence here; where a tier IS resolved it still always draws. */}
      <span className="tr-step__aux">
        {step.intent ? `${step.intent} · ` : ""}
        {step.tier === null ? "" : `Tier ${step.tier} · `}
        {step.verdictLabel}
      </span>
      {step.latest ? (
        // The frontier, named rather than animated: this is the newest
        // thing the Run has recorded, and nothing newer exists yet. It
        // asserts nothing about whether this step is still executing —
        // the wire does not say — so it is not a verdict word and the
        // tally never sees it.
        <span className="tr-step__latest">{LATEST_STEP_WORD}</span>
      ) : null}
      {step.durationSeconds === null ? null : (
        <span className="tr-step__time">
          <Duration seconds={step.durationSeconds} />
        </span>
      )}
    </>
  );
}

/* ----------------------------------------------------------- work band -- */

/**
 * The demoted work: a native <details>, closed by default.
 *
 * Native on purpose. The disclosure is the browser's — no measured heights, no
 * animated max-height, no JS toggle state — because animating a layout property
 * is the one thing that would make this screen feel slower than the one it
 * replaces. The only motion in the band is the chevron's `transform`.
 *
 * The summary carries count · names · verdict tally · elapsed, and it is the
 * summary's own text, not an aria-label, so the précis reaches assistive
 * technology exactly as it reaches the eye.
 */
function WorkBand({
  steps,
  elapsedSeconds,
  open,
  note,
}: {
  steps: readonly TranscriptStep[];
  elapsedSeconds: number | null;
  open?: boolean;
  /** Why the ledger is thinner than the Run — see TranscriptTurn.workNote. It
   * is drawn under the rows and is NOT one of them: no glyph, no verdict, and
   * bandTally never sees it. */
  note?: string | null;
}) {
  if (steps.length === 0) return null;
  const tally = bandTally(steps);
  const live = hasLiveStep(steps);
  // The RECORDED step count, not the row count: a folded row stands for the
  // events it folded, and the précis says how many the Run actually recorded.
  const recorded = bandStepCount(steps);
  // What the turn did, characterised — "30 × read.knowledge.search", not the
  // first two names and an ellipsis. See bandNames.
  const names = bandNames(steps);
  return (
    <details className="tr-work" open={(open ?? live) ? true : undefined}>
      <summary className="tr-work__sum">
        <span className="tr-work__chev" aria-hidden="true">
          ▶
        </span>
        <span className="tr-work__count">
          {recorded} {recorded === 1 ? "step" : "steps"}
        </span>
        <span className="tr-work__names" title={names}>
          {names}
        </span>
        <span className="tr-work__verdict">
          {tally.map((group, i) => (
            <span key={group.verdict} className={`tr-work__tally tr-work__tally--${group.tone}`}>
              {i > 0 ? (
                <span className="tr-work__sep" aria-hidden="true">
                  ·
                </span>
              ) : null}
              {group.glyph ? (
                <span className="tr-work__glyph" aria-hidden="true">
                  {group.glyph}
                </span>
              ) : null}
              {group.count} {group.verdict}
            </span>
          ))}
        </span>
        {elapsedSeconds === null ? null : (
          <span className="tr-work__time">
            <Duration seconds={elapsedSeconds} />
          </span>
        )}
      </summary>
      <ol className="tr-work__steps">
        {steps.map((step) => (
          <li
            key={step.key}
            className={`tr-step tr-step--${step.verdict} tr-step--${step.rank ?? "detail"}${
              step.latest ? " tr-step--latest" : ""
            }${step.detail && step.detail.length > 0 ? " tr-step--has-detail" : ""}`}
          >
            {step.detail && step.detail.length > 0 ? (
              // THE ROLLED-UP ROW IS ITS OWN DISCLOSURE. The line compresses a
              // run of recorded events into plain language; the verbatim
              // per-event rows sit one hit below it. Native <details>, for the
              // same reason the band itself is one: the browser owns the
              // toggle, and nothing here measures or animates a height.
              <details className="tr-step__fold">
                <summary className="tr-step__foldsum">
                  <StepLine step={step} />
                  <span className="tr-step__foldchev" aria-hidden="true">
                    ▶
                  </span>
                </summary>
                <ol className="tr-step__detail">
                  {step.detail.map((row) => (
                    <li
                      key={row.key}
                      className={`tr-step tr-step--${row.verdict} tr-step--${row.rank ?? "detail"}`}
                    >
                      <StepLine step={row} />
                    </li>
                  ))}
                </ol>
              </details>
            ) : (
              <StepLine step={step} />
            )}
          </li>
        ))}
        {note ? <li className="tr-work__note">{note}</li> : null}
      </ol>
    </details>
  );
}

/* --------------------------------------------------------- promoted -- */

/**
 * The gate card. Rendered as a SIBLING of the band, never inside it — a step
 * pending a human decision or refused by policy is the product's content, not
 * its plumbing, and there is no state of this component in which it folds.
 */
function GateCard({ gate }: { gate: TranscriptGate }) {
  return (
    <div className={`tr-gate tr-gate--${gate.kind}`}>
      <div className="tr-gate__head">
        <span className="tr-gate__state">
          <span aria-hidden="true">{gate.kind === "pending" ? "⏸" : "⊘"}</span>
          {gate.state}
        </span>
        <span className="tr-gate__name" title={gate.name}>
          {gate.name}
        </span>
        <span className="tr-gate__tier">Tier {gate.tier ?? ABSENT}</span>
      </div>
      <p className="tr-gate__body">
        {/* The server's sentence, verbatim. Absent, it says so — this module
            writes no sentence of its own about a governance decision. */}
        {gate.sentence ?? <Absent note="the server sent no reason" />}
      </p>
      <div className="tr-gate__foot">
        {gate.action}
        <span className="tr-gate__ref">
          {gate.reference ?? <Absent note="no reference served" />}
        </span>
      </div>
    </div>
  );
}

/** THE glyph table — one entry per tone, read by BOTH the failure card and the
 * receipt. The card and the footer of the same turn therefore draw the same
 * glyph by construction rather than by two tables agreeing: ⊘ is the glyph the
 * refused gate card and posture's whole-read refusal already use, ■ is the
 * reader's own Stop, and only a fault takes ✕. Keyed by TranscriptReceiptTone,
 * which the failure tones are a subset of, so a new tone fails to compile here
 * before it can reach a screen glyphless. */
const TONE_GLYPH: Record<TranscriptReceiptTone, string> = {
  ok: "✓",
  paused: "⏸",
  // The SAME glyph a running step draws (STEP_GLYPH above). A turn in flight
  // and a step in flight are the same fact at two scales, and the reader should
  // not have to learn two marks for it.
  live: "●",
  fault: "✕",
  refusal: "⊘",
  stopped: "■",
};

/** How each outcome is ANNOUNCED. A fault interrupts; a governed decision and a
 * stop the reader chose are announced politely. A Record rather than a ternary
 * on `fault`, so no tone gets its role by falling through someone else's else. */
const FAILURE_ROLE: Record<TranscriptFailureTone, "alert" | "status"> = {
  fault: "alert",
  refusal: "status",
  stopped: "status",
};

/** The turn ended with no answer, so there is no answer to rank first: this
 * takes the promoted slot instead. One per turn — a failed STEP stays in the
 * band.
 *
 * THREE TREATMENTS, because a fault, a refusal and a stop are not one event.
 * The fault keeps the red card, its ✕ and its retry. The refusal takes the
 * gated card, its ⊘ and the server's own reason, and draws NO foot at all — the
 * slot a control would sit in does not exist on that branch, which is the
 * render half of the type's promise that a refusal cannot offer a retry. The
 * stop takes the card with no hue on it at all (transcript.css): the reader
 * ended the turn, nothing enforced anything and nothing broke, so there is no
 * state for a colour to report. */
function FailureCard({ failure }: { failure: TranscriptFailure }) {
  return (
    <div className={`tr-fail tr-fail--${failure.tone}`} role={FAILURE_ROLE[failure.tone]}>
      <span className="tr-fail__state">
        <span aria-hidden="true">{TONE_GLYPH[failure.tone]}</span>
        {failure.state}
      </span>
      <p className="tr-fail__body">{failure.sentence}</p>
      {failure.tone === "refusal" ? (
        <p className="tr-fail__reason">
          {/* Verbatim, or the em dash and the note — the gate card's rule, and
              its exact words, because it is the same absence. */}
          {failure.reason ?? <Absent note="the server sent no reason" />}
        </p>
      ) : failure.action ? (
        <div className="tr-fail__foot">{failure.action}</div>
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------- in flight -- */

function ProgressLine({ progress }: { progress: TranscriptProgress }) {
  return (
    <p className="tr-progress">
      <span>
        Working · <Duration seconds={progress.elapsedSeconds} note="no elapsed time measured" />
      </span>
      <span className="tr-progress__sep" aria-hidden="true">
        ·
      </span>
      {progress.liveness ?? <Absent note="no word from the server yet" />}
    </p>
  );
}

/* ------------------------------------------------------------- answer -- */

/** The turn's only unboxed prose, in the answer's own light-Markdown shape
 * (transcript/prose.tsx: paragraphs, lists, headings, `**bold**`, backtick
 * code — the closed subset the served answers actually carry; everything else
 * stays literal). This module still adds no word to the answer. */
function AnswerSlot({ answer }: { answer: TranscriptAnswer }) {
  const blocks = proseBlocks(answer.body);
  // The caret and the citation chips close the answer: inside the last block
  // when that block is a paragraph (the chips "sit after the answer's prose"),
  // in a closing line of their own when the answer ends in a list or heading.
  const closing = (
    <>
      {answer.trailing}
      {answer.streaming ? <span className="tr-cursor" aria-hidden="true" /> : null}
    </>
  );
  const last = blocks.at(-1);
  const closesInline = last?.kind === "paragraph";
  return (
    <div className="tr-ans">
      <div className="tr-ans__attr">
        <span className="tr-ans__mark" aria-hidden="true">
          <NeutronMark size={14} />
        </span>
        <span>{answer.label}</span>
      </div>
      {blocks.length === 0 ? (
        <p className="tr-ans__body">
          <Absent note={answer.absentNote ?? "no answer was served"} />
        </p>
      ) : (
        blocks.map((block, i) => {
          const isLast = i === blocks.length - 1;
          if (block.kind === "list") {
            const items = block.items.map((item, j) => (
              <li key={j}>{renderInline(item, `${i}:${j}`)}</li>
            ));
            return block.ordered ? (
              <ol className="tr-ans__body tr-ans__list" key={i}>
                {items}
              </ol>
            ) : (
              <ul className="tr-ans__body tr-ans__list" key={i}>
                {items}
              </ul>
            );
          }
          if (block.kind === "heading") {
            return (
              <p className="tr-ans__body" key={i}>
                <strong>{renderInline(block.text, String(i))}</strong>
              </p>
            );
          }
          return (
            <p className="tr-ans__body" key={i}>
              {renderInline(block.text, String(i))}
              {isLast && closesInline ? closing : null}
            </p>
          );
        })
      )}
      {blocks.length > 0 && !closesInline && (answer.trailing || answer.streaming) ? (
        <p className="tr-ans__body">{closing}</p>
      ) : null}
    </div>
  );
}

function SourceList({
  sources,
  anchors,
  note,
}: {
  sources: readonly TranscriptSource[];
  /** The served row anchors — each verified row's canonical hash, drawn as the
   * row's identity because a verified row has no passage to open. */
  anchors: readonly TranscriptSourceAnchor[];
  /** The absence, stated. Drawn under the lines and counted by nothing. */
  note?: string | null;
}) {
  return (
    <div className="tr-src">
      {/* No "· 0" over a note explaining the absence: the count draws only
       * when there are lines to count, and the note is the whole account
       * otherwise — "Sources · 0" above "rests on 2 server-verified records"
       * read as the screen disagreeing with itself. */}
      <span className="tr-src__label">
        {sources.length > 0 ? `Sources · ${sources.length}` : "Sources"}
      </span>
      <ol className="tr-src__lines">
        {sources.map((source) => (
          <li key={source.key} className="tr-src__line">
            <span className="tr-src__n">[{source.ordinal}]</span>
            <span>
              {source.system ?? <Absent note="no source system served" />}
              {" · "}
              {source.document ?? <Absent note="no document served" />}
            </span>
            {source.actions}
          </li>
        ))}
      </ol>
      {/* THE ROW'S IDENTITY, POINTABLE. Short form on screen, the full served
       * value in the title and behind the copy control (HashChip's contract) —
       * displayed, never recomputed and never compared by this client. */}
      {anchors.length > 0 ? (
        <p className="tr-src__anchors" title={ROW_ANCHOR_GLOSS}>
          <span className="tr-src__anchorlabel">
            {anchors.length > 1 ? `${ROW_ANCHOR_LABEL} · ${anchors.length} rows` : ROW_ANCHOR_LABEL}
          </span>
          {anchors.map((row) => (
            <HashChip key={row.key} hash={row.anchor} label={ROW_ANCHOR_COPY_LABEL} />
          ))}
        </p>
      ) : null}
      {note ? <p className="tr-src__note">{note}</p> : null}
    </div>
  );
}

/* ------------------------------------------------------------ receipt -- */

function Fact({ fact }: { fact: TranscriptFact }) {
  return (
    <span className={fact.mono ? "tr-receipt__model" : undefined}>
      {fact.label ? `${fact.label} · ` : null}
      {fact.value ?? <Absent note={fact.note} />}
    </span>
  );
}

/** Exactly one of these closes a turn. It is the turn boundary as well as its
 * governance footer, which is why nothing here is conditional on the outcome
 * except the words: an absent model renders the em dash, never "No".
 *
 * The tone is the TURN'S tone, and on a turn that ended without an answer it is
 * the failure card's own value (types.ts). This footer therefore cannot say
 * "✕ Failed" in red under a ⊘ REFUSED card, which is exactly what it used to do
 * — it reads the same TONE_GLYPH table the card reads, and its colour resolves
 * from the same tone through transcript.css. */
function Receipt({ receipt }: { receipt: TranscriptReceipt }) {
  const parts: ReactNode[] = [
    <span className="tr-receipt__state" key="state">
      <span className="tr-receipt__glyph" aria-hidden="true">
        {TONE_GLYPH[receipt.tone]}
      </span>
      {receipt.outcome}
    </span>,
  ];
  if (receipt.pill) parts.push(<span key="pill">{receipt.pill}</span>);
  for (const fact of receipt.facts ?? []) {
    parts.push(<Fact key={`fact:${fact.key}`} fact={fact} />);
  }
  parts.push(
    <span key="model">
      Model ·{" "}
      {receipt.model === null ? (
        <Absent note={receipt.modelNote ?? "no model reply was served"} />
      ) : (
        <span className="tr-receipt__model">{receipt.model}</span>
      )}
    </span>,
  );
  if (receipt.trace) parts.push(<span key="trace">{receipt.trace}</span>);
  const line: ReactNode[] = [];
  parts.forEach((part, i) => {
    if (i > 0) {
      line.push(
        <span className="tr-receipt__sep" aria-hidden="true" key={`sep:${i}`}>
          ·
        </span>,
      );
    }
    line.push(part);
  });
  return (
    <div className={`tr-receipt tr-receipt--${receipt.tone}`}>
      {line}
      <span className="tr-receipt__spacer" />
      {receipt.actions}
    </div>
  );
}

/* --------------------------------------------------------------- turn -- */

/**
 * One turn, in the render's order: question, the demoted work, everything
 * promoted out of it, then the answer, its sources, and the one receipt.
 *
 * The DOM order is ask.html's verbatim. The design's "reading order, loudest to
 * quietest" is an EMPHASIS order delivered by typography — the answer is the
 * only unboxed prose in the turn — not a re-ordering of the document; the
 * renders draw the band above the answer and nothing here reverses that.
 */
export function TranscriptTurnView({ turn }: { turn: TranscriptTurn }) {
  const { band, gates } = partitionTurnSteps(turn.steps);
  return (
    <li className="tr-turn" id={turn.id}>
      <Question question={turn.question} />
      <WorkBand
        steps={band}
        elapsedSeconds={turn.workElapsedSeconds ?? null}
        open={turn.workOpen}
        note={turn.workNote ?? null}
      />
      {gates.map((gate) => (
        <GateCard key={gate.key} gate={gate} />
      ))}
      {turn.failure ? <FailureCard failure={turn.failure} /> : null}
      {turn.progress ? <ProgressLine progress={turn.progress} /> : null}
      {turn.answer ? <AnswerSlot answer={turn.answer} /> : null}
      {/* The block draws when there are sources OR when there is a reason there
          are none. An answer that cited nothing now SAYS so instead of ending
          in blank space that could equally mean the screen failed to load.
          THE THIRD STATE: the citation read is still in flight. It draws its
          own pending line — the live hue, its own class, role="status" — and
          never the settled note's treatment, so "no citations YET" cannot be
          mistaken for "the read landed and found none". A landed read (sources,
          anchors, or a note) always outranks the pending claim. */}
      {(turn.sources && turn.sources.length > 0) ||
      (turn.sourceAnchors && turn.sourceAnchors.length > 0) ||
      turn.sourcesNote ? (
        <SourceList
          sources={turn.sources ?? []}
          anchors={turn.sourceAnchors ?? []}
          note={turn.sourcesNote ?? null}
        />
      ) : turn.sourcesPending ? (
        <div className="tr-src">
          <span className="tr-src__label">Sources</span>
          <p className="tr-src__pending" role="status">
            {turn.sourcesPending}
          </p>
        </div>
      ) : null}
      {turn.receipt ? <Receipt receipt={turn.receipt} /> : null}
    </li>
  );
}

/** The thread: turns in order. The scrolling surface around it belongs to the
 * screen that mounts this (Ask's thread column, the Run workspace's). */
export function Transcript({
  turns,
  label,
}: {
  turns: readonly TranscriptTurn[];
  /** The list's accessible name — "Answers" on Ask, "Runs" on a Run. */
  label?: string;
}) {
  return (
    <ol className="tr-turns" aria-label={label}>
      {turns.map((turn) => (
        <TranscriptTurnView key={turn.id} turn={turn} />
      ))}
    </ol>
  );
}
