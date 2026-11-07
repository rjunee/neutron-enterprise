// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE RUN'S RECORDED ACTIVITY, AS TRANSCRIPT STEPS.
//
// WHY THIS FILE EXISTS. Canonical Ask's SSE carries one `accepted` frame and
// closes, so a turn that makes eighteen model calls over ten minutes used to
// draw a counter and nothing else — and a run that was CANCELLED looked exactly
// like a run that was merely slow. The work was never missing: the Run's own
// event log recorded every model call, every retrieval, every delivery decision
// as it happened. It was simply never read until the Run went terminal. This
// module turns that already-durable log into the same `TranscriptStep` rows the
// band already draws, so the activity appears while it is happening.
//
// NOTHING HERE IS INVENTED — the rule this module is built to satisfy. Every
// row corresponds to exactly one event the server recorded and served. There is
// no synthetic "thinking" row, no placeholder for a step that has not happened,
// no progress fraction, and no clock of this browser's own: an events page with
// no items produces an empty list, and the band then draws nothing at all. The
// only thing this file adds to an event is a WORD FOR ITS KIND (askCopy.ts).
//
// WHY EVERY ROW IS `allowed`, which is a claim about disclosure and not a fresh
// policy assertion. The Runs event wire carries no per-step verdict and no
// policy tier — `AgentRunEventWire` has no analogue of Ask's `ToolTransitEvent`
// — so `tier` is `null` on every row here and Transcript.tsx now draws nothing
// in that slot rather than repeating "Tier —" down the whole band. `allowed` is
// the same reading `runPlanSteps` (agent-harness/adapters/runWorkspace.ts)
// already takes of the same wire: an event that was RECORDED is work that
// actually ran, so the row reports that it ran and claims nothing further. What
// it never does is guess a verdict the wire withheld.
//
// THE DURATIONS ARE THE SERVER'S, NOT THIS BROWSER'S. Every event carries the
// `created_at` the server stamped on it, so the distance between one record and
// the next is a measurement made entirely on the server's clock. That is the
// row's duration. The NEWEST row has none — the interval it sits in has not
// ended — and it is marked `latest` instead, which says the one thing the wire
// supports: nothing after it has been recorded yet. No row ever reads a clock
// in this process, and no row is ever drawn before its event exists.
//
// WHY NOTHING HERE PROMOTES. `pending` and `refused` pull a step out of the
// band into a gate card that cannot fold (transcript/promotion.ts), and that is
// reserved for a decision a HUMAN owes or a policy refusal of an action. A Run
// event is neither: `event.unavailable` is the projection declining to name a
// step's kind, and turning each into an un-foldable card would bury the turn in
// cards while asserting a refusal that never occurred. Those rows stay in the
// band and SAY WHAT THEY ARE, which the
// band supports precisely because it compresses rather than conceals: its
// summary line is the audit précis. The turn's real gates still promote — they
// arrive as `ToolTransitEvent`s through ToolRow.tsx, untouched by this file.
//
// `run.terminal` DELIBERATELY PRODUCES NO ROW. The turn's outcome is already
// one object — the receipt, or the failure readout beside the band — and the
// promotion rule is explicit that a turn's ending is stated once, not once per
// step. A terminal row would restate it a second time, inside the fold. The
// same holds for the audit intent of that same ending (`isTerminalStatement`).
//
// WHERE THE WORDS COME FROM, since half the log used to have none. The
// projection served five of the thirteen kinds the store writes and clamped the
// rest to `event.unavailable`, so a real turn drew "Step · Not disclosable"
// over and over — every tool call, every model call, every verified evidence
// row. It now serves all thirteen, and for `run.audit.intent` — whose own safe
// detail is three hashes — it also serves `audit_action`, the readable action
// NAME already committed to the governed audit chain. `auditActionWords` turns
// that name into the row's words. Nothing here reads a hash, a prompt, an
// argument or a result; a step this console cannot name still says so.

import { runPlanAbsentNote, runPlanSteps } from "../agent-harness/adapters/runWorkspace";
import type {
  AgenticPlanStepWire,
  AgentRunDetailWire,
  AgentRunEventMetadataWire,
  AgentRunEventWire,
} from "../api/types";
import type { TranscriptStep } from "../transcript";
import {
  RUN_STEP_ACCEPTED,
  RUN_STEP_CANCEL_ACCEPTED,
  RUN_STEP_CHANGED,
  RUN_STEP_CLAIM_CHECK,
  RUN_STEP_DECIDED,
  RUN_STEP_DELIVERY,
  RUN_STEP_EVIDENCE,
  RUN_STEP_EVIDENCE_ROW,
  RUN_STEP_EXECUTION,
  RUN_STEP_EXECUTION_RECOVERED,
  RUN_STEP_EXECUTION_STARTED,
  RUN_STEP_FAILED,
  RUN_STEP_MODEL_CALL,
  RUN_STEP_MODEL_ROUTED,
  RUN_STEP_NOT_DISCLOSABLE,
  RUN_STEP_PRODUCED,
  RUN_STEP_RECORDED,
  RUN_STEP_RECOVERED,
  RUN_STEP_REQUEST_ACCEPTED,
  RUN_STEP_RETRY_ACCEPTED,
  RUN_STEP_SERVED,
  RUN_STEP_STARTED,
  RUN_STEP_EVIDENCE_ROLLUP_LABEL,
  RUN_STEP_MODEL_CALL_NOTE,
  RUN_STEP_NO_RECORDS,
  RUN_STEP_TOOL_CALL,
  RUN_STEP_UNNAMED,
  RUN_STEP_VERIFIED,
  RUN_STEP_WITHHELD,
  runEvidenceContributorsLabel,
  runEvidenceEmptyReadsLabel,
  runEvidenceRetrievedName,
  runEvidenceSourceClause,
  runEvidenceSourceWords,
  runEvidenceVerifiedLabel,
  runFoldedStepsName,
} from "./askCopy";
import { runIsTerminal } from "./useRunCompletion";

/** One served metadata value, or `null`. The wire ships metadata as a list of
 * key/value pairs; this reads one key out of it and never substitutes a
 * default, so an absent key stays absent all the way to the em dash. */
export function eventMetadata(
  event: AgentRunEventWire,
  key: AgentRunEventMetadataWire["key"],
): string | null {
  const hit = event.metadata.find((entry) => entry.key === key);
  if (hit === undefined) return null;
  // The wire types the value as string | number | boolean. Only a string names
  // a model or a decision; a number or a boolean under one of these keys is not
  // a name, and coercing it would print a value the server never meant as one.
  if (typeof hit.value !== "string") return null;
  return hit.value.length === 0 ? null : hit.value;
}

/** One served COUNT, or `null`. The server serves `contributor_count` as a
 * number; anything else under a count key — a string, a float, a negative — is
 * not a count the server stated and is never coerced into one. */
export function eventMetadataCount(
  event: AgentRunEventWire,
  key: AgentRunEventMetadataWire["key"],
): number | null {
  const hit = event.metadata.find((entry) => entry.key === key);
  if (hit === undefined) return null;
  if (typeof hit.value !== "number" || !Number.isInteger(hit.value) || hit.value < 0) return null;
  return hit.value;
}

/**
 * THE PLAN, KEYED BY THE SERVER'S OWN JOIN KEY.
 *
 * `AgenticPlanStepWire.tool_call_id` is minted by the same executor frame that
 * stamps `tool_call_id` onto the step's `run.tool.pre_effect` event, so equality
 * of the two ids IS the server's statement that the event and the plan row are
 * one act. Steps the server did not bind (`null`) are simply absent from the
 * map, and a duplicated id — which the server cannot honestly mean — drops BOTH
 * entries rather than picking one.
 */
function planStepsByCallId(
  plan: readonly AgenticPlanStepWire[] | null | undefined,
): ReadonlyMap<string, AgenticPlanStepWire> {
  const map = new Map<string, AgenticPlanStepWire>();
  const duplicated = new Set<string>();
  for (const step of plan ?? []) {
    const id = step.tool_call_id;
    if (id === null || id === "") continue;
    if (map.has(id) || duplicated.has(id)) {
      map.delete(id);
      duplicated.add(id);
      continue;
    }
    map.set(id, step);
  }
  return map;
}

/** One row's words, plus whether the server actually NAMED the step.
 *
 * `named` is not decoration: it is the single source for the fold (see
 * `isAnonymousEvent`). A row the server named is a peer of the work around it
 * and is never counted away; a row it did not name is repetition, and the fold
 * exists for exactly those. Keeping the flag here makes the two rules one rule
 * rather than two switch statements that can drift apart. */
type RunStepWords = { name: string; label: string; named: boolean };

/** The plain-language words for one governed audit action.
 *
 * `run.audit.intent` events store only hashes of what they did — action,
 * resource and intent, all digested — so the readable action name comes from
 * the audit chain itself, through the projection's `audit_action` key. This is
 * a CLOSED map: an action outside it (the server enumerates seven, and drops
 * anything else) returns null and the caller falls back to the honest unknown
 * row. It never guesses from `step_id`, which names a phase and not an act. */
function auditActionWords(action: string): RunStepWords | null {
  switch (action) {
    case "run.tool.pre_effect":
      return { name: RUN_STEP_TOOL_CALL, label: RUN_STEP_RECORDED, named: true };
    case "run.model.pre_effect":
      return { name: RUN_STEP_MODEL_CALL, label: RUN_STEP_RECORDED, named: true };
    case "run.execution.pre_effect":
      return { name: RUN_STEP_EXECUTION, label: RUN_STEP_RECORDED, named: true };
    case "run.evidence.produced":
      return { name: RUN_STEP_EVIDENCE, label: RUN_STEP_PRODUCED, named: true };
    case "run.delivery.decided":
      return { name: RUN_STEP_DELIVERY, label: RUN_STEP_DECIDED, named: true };
    case "run.execution.failed":
      return { name: RUN_STEP_EXECUTION, label: RUN_STEP_FAILED, named: true };
    default:
      // Including `run.lifecycle.terminal`, which never reaches here: the turn's
      // ending is stated once, and `runActivitySteps` drops its row for exactly
      // the reason it drops `run.terminal` (see the header).
      return null;
  }
}

/** The name and verdict word for one event kind. Both come from askCopy; the
 * name prefers a SERVED identifier where the event carries one, because
 * "gpt-oss-120b · Served" tells the reader more than "Model call · Served"
 * and costs nothing — the alias is the server's own. */
function nameAndLabel(event: AgentRunEventWire): RunStepWords {
  switch (event.event_kind) {
    case "model.served":
      return {
        name: eventMetadata(event, "served_model") ?? RUN_STEP_MODEL_CALL,
        label: RUN_STEP_SERVED,
        named: true,
      };
    case "model.changed":
      // A routing change names the model it moved TO, which is the one the
      // reader now needs; `served_model` is not a key this kind carries.
      return {
        name: eventMetadata(event, "after_authorized_model") ?? RUN_STEP_MODEL_ROUTED,
        label: RUN_STEP_CHANGED,
        named: true,
      };
    case "evidence.produced":
      return { name: RUN_STEP_EVIDENCE, label: RUN_STEP_PRODUCED, named: true };
    case "delivery.decided":
      return {
        name: RUN_STEP_DELIVERY,
        // The decision word is the SERVER'S: `delivery.decided` carries its
        // outcome in `event_status`, and a served word beats a generic one.
        label: eventMetadata(event, "decision") ?? RUN_STEP_DECIDED,
        named: true,
      };
    case "run.audit.intent": {
      // THE ROW THIS WHOLE CHANGE EXISTS FOR. A tool call, a model call or an
      // execution step, named by the action the governed chain already holds
      // readable. If the server served no `audit_action` — or served one it
      // would not enumerate — this falls through to the unknown row below
      // rather than inventing a label the data does not support.
      const action = eventMetadata(event, "audit_action");
      const words = action === null ? null : auditActionWords(action);
      if (words !== null) return words;
      break;
    }
    case "run.start.accepted":
      return { name: RUN_STEP_REQUEST_ACCEPTED, label: RUN_STEP_ACCEPTED, named: true };
    case "run.cancel.accepted":
      return { name: RUN_STEP_CANCEL_ACCEPTED, label: RUN_STEP_ACCEPTED, named: true };
    case "run.retry.accepted":
      return { name: RUN_STEP_RETRY_ACCEPTED, label: RUN_STEP_ACCEPTED, named: true };
    case "run.execution.claimed":
      return { name: RUN_STEP_EXECUTION_STARTED, label: RUN_STEP_STARTED, named: true };
    case "run.execution.recovered":
      return { name: RUN_STEP_EXECUTION_RECOVERED, label: RUN_STEP_RECOVERED, named: true };
    case "evidence.verified_row":
      return { name: RUN_STEP_EVIDENCE_ROW, label: RUN_STEP_VERIFIED, named: true };
    case "claim.verifier":
      return { name: RUN_STEP_CLAIM_CHECK, label: RUN_STEP_VERIFIED, named: true };
    default:
      break;
  }
  // `event.unavailable`, an audit intent the server would not name, and any
  // kind a future server adds that this console does not yet know. All are
  // real recorded work whose kind is not disclosable here; none may borrow
  // another kind's word.
  return {
    name: RUN_STEP_UNNAMED,
    label: event.event_status === "withheld" ? RUN_STEP_WITHHELD : RUN_STEP_NOT_DISCLOSABLE,
    named: false,
  };
}

/**
 * The Run's recorded events as band rows, in the server's own sequence order.
 *
 * `run.terminal` is dropped (see header); everything else the server served
 * becomes exactly one row. The list is NOT re-sorted here — `reconcileRunEvents`
 * already ordered it by canonical sequence, and arrival time is never order
 * authority.
 */
/**
 * IS THIS EVENT ONE OF THE ANONYMOUS ONES — the rows the fold exists for.
 *
 * True for exactly the un-named fallback of `nameAndLabel`, and it asks that
 * function rather than re-deriving the answer, so the two can never disagree
 * about which rows the band is allowed to count away.
 *
 * THE FOLD SHRANK, DELIBERATELY. It used to swallow `evidence.verified_row`,
 * `claim.verifier`, every lifecycle kind and every audit intent, because the
 * projection clamped all of them to `event.unavailable` and the console had
 * nothing to draw but "Step". The server now names them, so they are peers of
 * the work around them and each gets its own row. Counting a step away is only
 * defensible while it is anonymous. What remains anonymous: a kind this console
 * does not know, and an audit intent the server declined to name.
 *
 * `withheld` IS EXCLUDED, deliberately and permanently. That status is the
 * projection reporting the RECORD ITSELF as withheld — a governance outcome
 * about one specific step — and it renders as itself, on its own row, always.
 * A count is a fair compression of repetition; it is not a fair compression of
 * a distinct governance statement.
 */
function isAnonymousEvent(event: AgentRunEventWire): boolean {
  if (event.event_status === "withheld") return false;
  return !nameAndLabel(event).named;
}

/**
 * THE MODEL AUTHORIZED AT EACH POINT IN THE LOG.
 *
 * A `run.model.pre_effect` audit intent carries no model name — it is written
 * before the call and stores only hashes — so a band of them read "Model call ·
 * Recorded" over and over with no way to tell WHICH model was thinking. The
 * name is in the log, one row earlier: `model.served` states the model that
 * served, and `model.changed` states, in `after_authorized_model`, the model
 * authorized FROM THAT POINT ON. Both are the server's own statements about the
 * Run's model at a position in its own ordered log.
 *
 * So this walks the log once and records, for each index, the model the server
 * had most recently named. Nothing is carried BACKWARD (a model call before any
 * naming event gets `null` and renders no model) and nothing is carried across
 * a re-authorization: the value at each index is the last one stated, and if
 * the server never stated one, it stays null and the row says nothing.
 */
function authorizedModelByIndex(events: readonly AgentRunEventWire[]): (string | null)[] {
  const out: (string | null)[] = [];
  let current: string | null = null;
  for (const event of events) {
    // The naming event's OWN row already prints the model in its name slot, so
    // the carry starts at the row AFTER it: push first, then update.
    out.push(current);
    if (event.event_kind === "model.changed") {
      current = eventMetadata(event, "after_authorized_model") ?? current;
    } else if (event.event_kind === "model.served") {
      current = eventMetadata(event, "served_model") ?? current;
    }
  }
  return out;
}

/**
 * WHAT ELSE THIS ROW CAN HONESTLY SAY, from the metadata the server served.
 *
 * The aux slot used to read "Tier —" and nothing else on every row of a real
 * turn. These are the served facts that belong beside a step, each printed only
 * when the server actually sent it:
 *
 *   the authorized model   for a model call, so a band of them names the model
 *                          that was thinking rather than repeating "Model call"
 *   `delivery_kind`        what KIND of delivery the decision was about
 *   `decision_kind`        the governed classification of an audit intent — the
 *                          one substantive field this deployment serves TODAY,
 *                          while `audit_action` is still absent, so the degraded
 *                          rows say something real instead of "Step"
 *   `step_id`             the Run's own identity for the step ("model:3"), the
 *                          only way to tell WHICH of eighteen calls this was
 *
 * Never a hash, never an argument, never a result. Joined with the middle dot
 * the band already uses, in decreasing order of what a reader is looking for.
 */
function auxFacts(
  event: AgentRunEventWire,
  authorizedModel: string | null,
  planStep: AgenticPlanStepWire | null,
  /** WHETHER THE SCHEMA'S OWN WORDS PRINT ON THIS LINE. The governed decision
   * class ("state_transition") and the Run's step identity ("run-tool",
   * "model:2") are real served facts, but they are OUR vocabulary, and a
   * banking reader scanning the band cannot use them. On a top-level row the
   * server NAMED, they are demoted into the row's own detail disclosure; on an
   * UNNAMED row they stay, because they are the only substance the row has —
   * and on every per-event detail row they print verbatim. */
  schemaFacts = true,
): string | null {
  const parts: string[] = [];
  // THE STEP'S OWN INTENT SENTENCE, first, when the server joined this event to
  // its plan row by `tool_call_id`. It is the sentence the model stated and the
  // server durably recorded BEFORE the call ran — already served today on the
  // plan wire — so the row can finally read "read.knowledge.search · Search the
  // governed knowledge base for …" instead of a bare correlation id.
  if (planStep !== null) parts.push(planStep.step_intent);
  const action =
    event.event_kind === "run.audit.intent" ? eventMetadata(event, "audit_action") : null;
  // The model this exact call was pinned to is printed in the NAME slot when
  // the event states it (see `stepForEvent`); the carried authorization is the
  // fallback for records written before the server stated it per call.
  if (action === "run.model.pre_effect") {
    const pinned = eventMetadata(event, "requested_model");
    if (pinned === null && authorizedModel !== null) parts.push(authorizedModel);
  }
  // WHAT A RETRIEVAL PRODUCED — kind and count, both the server's own scalars.
  // Never the retrieved text, which the wire deliberately does not carry.
  if (event.event_kind === "evidence.produced" || event.event_kind === "evidence.verified_row") {
    const readKind = eventMetadata(event, "read_kind");
    if (readKind !== null) parts.push(readKind);
    const contributors = eventMetadataCount(event, "contributor_count");
    if (contributors !== null) parts.push(runEvidenceContributorsLabel(contributors));
    const evidenceStatus = eventMetadata(event, "evidence_status");
    if (evidenceStatus === "evidence_locator_unavailable") parts.push("locator unavailable");
  }
  const deliveryKind = eventMetadata(event, "delivery_kind");
  if (deliveryKind !== null) parts.push(deliveryKind);
  if (schemaFacts) {
    // The classification, not the act. It is the honest degraded label while
    // the deployment's audit join is unresolved and `audit_action` never
    // arrives.
    const decisionKind = eventMetadata(event, "decision_kind");
    if (decisionKind !== null) parts.push(decisionKind);
    if (event.step_id !== null && event.step_id !== "") parts.push(event.step_id);
  }
  return parts.length === 0 ? null : parts.join(" · ");
}

/** Seconds between two of the server's own timestamps, or `null` when either is
 * unparseable or the pair runs backwards. NEVER this browser's clock. */
function secondsBetween(from: string, to: string): number | null {
  const start = Date.parse(from);
  const end = Date.parse(to);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  const seconds = (end - start) / 1000;
  return seconds < 0 ? null : seconds;
}

/** One row for one recorded event — the shape every un-folded row takes.
 *
 * `next` is the event the server recorded AFTER this one, when there is one.
 * The gap between the two timestamps is the row's duration, and it is a
 * measurement the SERVER made: both stamps are its own. It is `null` on the
 * newest row, because the interval that row is in has not ended yet and
 * printing this browser's clock there would time the console, not the Run. */
function stepForEvent(
  event: AgentRunEventWire,
  next: AgentRunEventWire | null,
  authorizedModel: string | null,
  latest: boolean,
  planByCallId: ReadonlyMap<string, AgenticPlanStepWire> = new Map(),
  {
    verbose = false,
    keySuffix = "",
  }: {
    /** A per-event DETAIL row inside a rolled-up parent: it prints the schema's
     * own words verbatim, and it needs a key of its own so React never mistakes
     * it for the top-level row over the same event. */
    verbose?: boolean;
    keySuffix?: string;
  } = {},
): TranscriptStep {
  const { name: kindName, label, named } = nameAndLabel(event);
  // NAME THE ACT WHEN THE SERVER NAMED IT. A `run.tool.pre_effect` row can now
  // carry the registered tool name from the chain's own record (`tool_name`),
  // and its `tool_call_id` can join it to the plan row holding the model's
  // stated intent for exactly this call. A `run.model.pre_effect` row can carry
  // the exact model the call was pinned to (`requested_model`). Every one of
  // these is a value the server served for THIS event; when absent, the row
  // keeps its generic kind word — the honest unknown, not a guess.
  const action =
    event.event_kind === "run.audit.intent" ? eventMetadata(event, "audit_action") : null;
  let name = kindName;
  let planStep: AgenticPlanStepWire | null = null;
  if (action === "run.tool.pre_effect") {
    const callId = eventMetadata(event, "tool_call_id");
    planStep = callId === null ? null : (planByCallId.get(callId) ?? null);
    const toolName = eventMetadata(event, "tool_name");
    // Two server statements about the same call must AGREE before the join is
    // trusted with the intent sentence; a disagreement is corrupted data, and
    // the row keeps the chain's own tool name with no borrowed sentence.
    if (planStep !== null && toolName !== null && toolName !== planStep.tool) planStep = null;
    name = toolName ?? planStep?.tool ?? kindName;
  } else if (action === "run.model.pre_effect") {
    name = eventMetadata(event, "requested_model") ?? kindName;
  }
  // The schema's own words (decision class, step identity) stay on a detail
  // row, on an unnamed row — where they are the only substance — and on every
  // kind whose row was already legible ("model:2" on a lone served reply is
  // information, not noise). They demote into the row's own disclosure ONLY on
  // a NAMED audit-intent row, where "state_transition · run-model · Recorded"
  // was the schema leaking through a row that already says what happened.
  const schemaFacts = verbose || !named || event.event_kind !== "run.audit.intent";
  const plainIntent = auxFacts(event, authorizedModel, planStep, schemaFacts);
  const step: TranscriptStep = {
    // The server's own event identity, so a re-delivered page replaces a row
    // in place instead of duplicating it, and React never reorders the band.
    key: `run-event:${event.event_id}${keySuffix}`,
    name,
    verdict: "allowed",
    verdictLabel: label,
    stateLabel: label,
    // The events wire carries no policy tier on any kind, so the slot stays
    // absent and Transcript.tsx now draws nothing rather than "Tier —".
    tier: null,
    // MEASURED, BY THE SERVER. See `stepForEvent`'s `next` parameter: this is
    // the distance between two of the Run's own recorded timestamps.
    durationSeconds: next === null ? null : secondsBetween(event.created_at, next.created_at),
    // Everything the served metadata lets this row say — the joined plan step's
    // intent sentence for a tool call, the authorized model for a model call,
    // what a retrieval produced, the delivery kind, the governed decision
    // class, and the Run's own step identity.
    intent: plainIntent,
    // THE ACT, IN ITS CHRONOLOGICAL PLACE. A tool receipt the server actually
    // NAMED — the chain's own `tool_name`, or the plan row joined by the
    // server's `tool_call_id` — is the tool call itself, recorded in sequence
    // and timed against the next record, so it takes the `act` rank right here
    // in the band's chronology (the owner's ask: emphasise the call in its
    // regular position, never hoist it). An unnamed receipt keeps the generic
    // kind word and stays machinery; so does everything else the Run records —
    // a model hop, a governed check — and so does the verbatim per-event row
    // inside a rolled-up disclosure, which is subordinate by construction.
    rank: !verbose && action === "run.tool.pre_effect" && name !== kindName ? "act" : "detail",
    ...(latest ? { latest: true } : {}),
  };
  if (schemaFacts) return step;
  // NOTHING IS DELETED, ONLY DEMOTED: where the plain line withheld the schema
  // facts, the verbatim per-event row rides inside the row's own disclosure.
  const verboseIntent = auxFacts(event, authorizedModel, planStep, true);
  if (verboseIntent === plainIntent) return step;
  return {
    ...step,
    detail: [
      stepForEvent(event, next, authorizedModel, false, planByCallId, {
        verbose: true,
        keySuffix: ":detail",
      }),
    ],
  };
}

/**
 * ONE ROW FOR A CONSECUTIVE RUN OF ANONYMOUS EVENTS.
 *
 * Only ever called with two or more — a single anonymous event is already one
 * row and folding it would gain nothing and lose its event id.
 *
 * WHAT IS KEPT: the count (in the name, so the closed band's précis names it,
 * and in `foldedCount`, so the précis's headline number still counts every one
 * of them — transcript/promotion.ts weights by it); the verdict word, verbatim;
 * and every DISTINCT step identity the Run assigned to the folded events, in
 * first-seen order, so "run-tool · run-model" survives the fold.
 *
 * WHAT IS NOT INVENTED: no kind, no duration, no tier, and no claim about what
 * those steps did. The fold compresses repetition and nothing else.
 */
function foldedStep(
  events: readonly AgentRunEventWire[],
  next: AgentRunEventWire | null,
  latest: boolean,
): TranscriptStep {
  const first = events[0]!;
  const label = nameAndLabel(first).label;
  const identities: string[] = [];
  for (const event of events) {
    const id = event.step_id;
    if (id !== null && id !== "" && !identities.includes(id)) identities.push(id);
  }
  return {
    key: `run-events:${first.event_id}+${events.length}`,
    name: runFoldedStepsName(events.length),
    verdict: "allowed",
    verdictLabel: label,
    stateLabel: label,
    tier: null,
    // The span the folded events actually occupy: from the first one's stamp to
    // the stamp of whatever the server recorded after the last of them. Both
    // ends are the server's; the fold measures nothing of its own.
    durationSeconds: next === null ? null : secondsBetween(first.created_at, next.created_at),
    intent: identities.length === 0 ? null : identities.join(" · "),
    rank: "detail",
    foldedCount: events.length,
    ...(latest ? { latest: true } : {}),
  };
}

/** The kinds the evidence roll-up may compress. A `withheld` record never
 * rolls up: that status is a distinct governance statement about one specific
 * record, and it keeps its own row — the same rule the anonymous fold holds. */
function isEvidenceEvent(event: AgentRunEventWire): boolean {
  return (
    (event.event_kind === "evidence.produced" || event.event_kind === "evidence.verified_row") &&
    event.event_status !== "withheld"
  );
}

/**
 * ONE ROW FOR A CONSECUTIVE RUN OF EVIDENCE EVENTS — the roll-up this change
 * exists for.
 *
 * A measured S1 turn recorded 93 `evidence.produced` events (60 of them
 * `verified_t3_empty` — the read returned NOTHING) plus 35
 * `evidence.verified_row`s, and the band drew every one: sixty rows to say "no
 * rows found", sixty times, in schema vocabulary. This compresses each
 * consecutive run of evidence events into ONE row that says, in plain words,
 * what came back: "Retrieved 12 records · from the verified graph · 12 rows
 * verified", or — once, with its own count — "No matching records · 60 lookups
 * returned nothing".
 *
 * EVERY NUMBER IS A COUNT OF RECORDED EVENTS. `records` counts the produced
 * events whose served `read_kind` was not the empty class, `empty` counts the
 * ones it was, `verified` counts the verified-row events. Nothing is estimated,
 * no event is dropped (`foldedCount` keeps the précis's headline honest), and
 * the verbatim per-event rows — raw `read_kind` code, contributor count,
 * locator status, step identity — ride in the row's own disclosure
 * (`TranscriptStep.detail`).
 *
 * WHY THE GROUP IS POSITIONAL, NOT ATTRIBUTED. The evidence EVENTS carry no
 * `tool_call_id` on the wire (only the evidence records do, and they are not
 * served here), so pinning counts onto a specific tool call's row would be an
 * inference the server did not state. The roll-up row instead sits exactly
 * where its first event sits in the server's own ordered log — directly after
 * the tool receipt that preceded it — which claims only the ordering the
 * server actually recorded.
 */
function evidenceRollupStep(
  group: readonly { event: AgentRunEventWire; index: number }[],
  next: AgentRunEventWire | null,
  latest: boolean,
  after: (originalIndex: number) => AgentRunEventWire | null,
): TranscriptStep {
  const first = group[0]!.event;
  let records = 0;
  let empty = 0;
  let verified = 0;
  const sources: string[] = [];
  for (const { event } of group) {
    const readKind = eventMetadata(event, "read_kind");
    if (event.event_kind === "evidence.verified_row") {
      verified += 1;
    } else if (readKind === "verified_t3_empty") {
      empty += 1;
    } else {
      records += 1;
    }
    if (readKind !== null) {
      const words = runEvidenceSourceWords(readKind);
      if (words !== null && !sources.includes(words)) sources.push(words);
    }
  }
  // The name states the group's outcome: what was retrieved, or — when every
  // read came back empty — that nothing matched. A verified-only group (no
  // produced events at all) is named by its verification count.
  const name =
    records > 0
      ? runEvidenceRetrievedName(records)
      : empty > 0
        ? RUN_STEP_NO_RECORDS
        : runEvidenceVerifiedLabel(verified);
  const parts: string[] = [];
  const clause = runEvidenceSourceClause(sources);
  if (clause !== null) parts.push(clause);
  if (verified > 0 && (records > 0 || empty > 0)) parts.push(runEvidenceVerifiedLabel(verified));
  if (empty > 0) parts.push(runEvidenceEmptyReadsLabel(empty));
  return {
    key: `run-events:${first.event_id}+${group.length}`,
    name,
    verdict: "allowed",
    verdictLabel: RUN_STEP_EVIDENCE_ROLLUP_LABEL,
    stateLabel: RUN_STEP_EVIDENCE_ROLLUP_LABEL,
    tier: null,
    // The span the group actually occupies, both ends the server's own stamps.
    durationSeconds: next === null ? null : secondsBetween(first.created_at, next.created_at),
    intent: parts.length === 0 ? null : parts.join(" · "),
    rank: "detail",
    ...(group.length > 1 ? { foldedCount: group.length } : {}),
    // DEMOTED, NEVER DELETED: one verbatim row per recorded event, inside the
    // roll-up's own disclosure.
    detail: group.map(({ event, index }) =>
      stepForEvent(event, after(index), null, false, new Map(), {
        verbose: true,
        keySuffix: ":detail",
      }),
    ),
    ...(latest ? { latest: true } : {}),
  };
}

/**
 * ONE ROW FOR ONE MODEL CALL'S TWO RECORDS.
 *
 * The server writes a `run.audit.intent` (`run.model.pre_effect`) immediately
 * BEFORE each model call and a `model.served` when the reply arrives, so every
 * call drew two adjacent rows — "frontier-default · state_transition ·
 * run-model · Recorded" and "frontier-default · model:2 · Served" — which is
 * one event to a human. When the two records are ADJACENT in the drawn log they
 * merge into one row named by the served model, timed from the pre-effect
 * stamp to the record after the reply, with both verbatim rows in the
 * disclosure and `foldedCount: 2` keeping the précis honest.
 *
 * ADJACENCY IS THE GUARD. A pre-effect whose next drawn record is NOT the
 * served reply — the call failed, routing changed, anything intervened — keeps
 * its own row, because pairing it with a later reply would be a chronology
 * this console invented.
 */
function modelPairStep(
  pre: { event: AgentRunEventWire; index: number },
  served: { event: AgentRunEventWire; index: number },
  next: AgentRunEventWire | null,
  latest: boolean,
  authorizedModel: string | null,
  after: (originalIndex: number) => AgentRunEventWire | null,
): TranscriptStep {
  const name =
    eventMetadata(served.event, "served_model") ??
    eventMetadata(pre.event, "requested_model") ??
    RUN_STEP_MODEL_CALL;
  return {
    key: `run-events:${pre.event.event_id}+2`,
    name,
    verdict: "allowed",
    verdictLabel: RUN_STEP_SERVED,
    stateLabel: RUN_STEP_SERVED,
    tier: null,
    durationSeconds: next === null ? null : secondsBetween(pre.event.created_at, next.created_at),
    intent: RUN_STEP_MODEL_CALL_NOTE,
    rank: "detail",
    foldedCount: 2,
    detail: [
      stepForEvent(pre.event, after(pre.index), authorizedModel, false, new Map(), {
        verbose: true,
        keySuffix: ":detail",
      }),
      stepForEvent(served.event, after(served.index), authorizedModel, false, new Map(), {
        verbose: true,
        keySuffix: ":detail",
      }),
    ],
    ...(latest ? { latest: true } : {}),
  };
}

/**
 * DOES THIS EVENT RESTATE THE TURN'S ENDING — the one thing said exactly once.
 *
 * `run.terminal` is the canonical terminal record, and `run.lifecycle.terminal`
 * is the audit intent written for that same ending: the sink cannot bind a
 * terminal target to an existing event, so it writes its own `run.audit.intent`
 * row alongside. Two rows, one ending. The turn's outcome is already one object
 * — the receipt, or the failure readout beside the band — so neither draws.
 */
function isTerminalStatement(event: AgentRunEventWire): boolean {
  if (event.event_kind === "run.terminal") return true;
  return (
    event.event_kind === "run.audit.intent" &&
    eventMetadata(event, "audit_action") === "run.lifecycle.terminal"
  );
}

/**
 * THE AUDIT INTENTS THAT MIRROR A CANONICAL EVENT — one act, two records.
 *
 * The Run's audit sink writes a `run.audit.intent` beside two kinds it ALSO
 * records canonically: `run.evidence.produced` beside `evidence.produced`, and
 * `run.delivery.decided` beside `delivery.decided`. That is the same shape as
 * `run.lifecycle.terminal` beside `run.terminal`, and `isTerminalStatement`
 * already drops that pair for the same reason: a reader watching a turn wants
 * one row per thing the agent DID, not one row per row the server wrote.
 *
 * The canonical event is the one kept, because it is the one carrying the
 * substance — `delivery.decided` has the delivery kind and the decision word;
 * its audit intent has three hashes and a classification.
 *
 * THE PRE-EFFECT THREE ARE NOT IN THIS MAP, deliberately. `run.tool.pre_effect`,
 * `run.model.pre_effect` and `run.execution.pre_effect` have no canonical twin —
 * they are the ONLY record of those acts, and dropping one would delete a tool
 * call from the account of the turn.
 */
const MIRRORED_ACTIONS: ReadonlyMap<string, AgentRunEventWire["event_kind"]> = new Map([
  ["run.evidence.produced", "evidence.produced"],
  ["run.delivery.decided", "delivery.decided"],
]);

/**
 * The intents to drop from THIS log — computed per log, never assumed.
 *
 * An intent is dropped only when the canonical event it mirrors is actually
 * present. If the projection served the intent and not its twin, the intent is
 * the only account of that act and it keeps its row: the rule removes a
 * DUPLICATE, and it must never remove the last record of something that
 * happened.
 */
function mirroringIntents(events: readonly AgentRunEventWire[]): ReadonlySet<string> {
  const present = new Set(events.map((event) => event.event_kind));
  const drop = new Set<string>();
  for (const event of events) {
    if (event.event_kind !== "run.audit.intent") continue;
    const action = eventMetadata(event, "audit_action");
    if (action === null) continue;
    const twin = MIRRORED_ACTIONS.get(action);
    if (twin !== undefined && present.has(twin)) drop.add(event.event_id);
  }
  return drop;
}

export function runActivitySteps(
  events: readonly AgentRunEventWire[],
  {
    live = false,
    plan = null,
  }: { live?: boolean; plan?: readonly AgenticPlanStepWire[] | null } = {},
): TranscriptStep[] {
  const planByCallId = planStepsByCallId(plan);
  const mirrors = mirroringIntents(events);
  // The rows the band will draw, each carrying its INDEX into the server's own
  // list. The turn's ending draws no row (see the header) but it is still a
  // record with a timestamp, so keeping the indices lets the last visible row
  // be timed against the ending it does not draw — which is the difference
  // between a final step that reports how long it took and one that reports
  // nothing.
  const drawn: { event: AgentRunEventWire; index: number }[] = [];
  events.forEach((event, index) => {
    if (!isTerminalStatement(event) && !mirrors.has(event.event_id)) drawn.push({ event, index });
  });
  const models = authorizedModelByIndex(events);
  /** The record after the one a row ends on, drawn or not. `null` only at the
   * true end of the log, where the interval has not closed yet. */
  const after = (originalIndex: number) => events[originalIndex + 1] ?? null;
  const steps: TranscriptStep[] = [];
  // THREE COMPRESSIONS, ALL POSITIONAL AND NONE REACHING ACROSS ANOTHER ROW:
  // a consecutive run of ANONYMOUS events folds (as it always did), a
  // consecutive run of EVIDENCE events rolls up into one plain-language row,
  // and a model call's ADJACENT pre-effect + served pair merges into one row.
  // Each compressed row occupies exactly the position its first event held,
  // carries `foldedCount` so the précis's headline number still counts every
  // recorded event, and holds its verbatim per-event rows in `detail`.
  let position = 0;
  while (position < drawn.length) {
    const { event, index } = drawn[position]!;
    // THE FRONTIER IS THE LAST DRAWN ROW OF A LIVE RUN, AND ONLY THAT. Both
    // halves are required: a settled Run's last row is finished, not pending,
    // and marking it would be the same lie in the other direction.
    const latestAt = (endPosition: number) => live && endPosition === drawn.length - 1;
    if (isAnonymousEvent(event)) {
      let end = position;
      while (end + 1 < drawn.length && isAnonymousEvent(drawn[end + 1]!.event)) end += 1;
      const group = drawn.slice(position, end + 1);
      const endIndex = group[group.length - 1]!.index;
      steps.push(
        group.length === 1
          ? stepForEvent(event, after(index), models[index] ?? null, latestAt(end), planByCallId)
          : foldedStep(
              group.map((entry) => entry.event),
              after(endIndex),
              latestAt(end),
            ),
      );
      position = end + 1;
      continue;
    }
    if (isEvidenceEvent(event)) {
      let end = position;
      while (end + 1 < drawn.length && isEvidenceEvent(drawn[end + 1]!.event)) end += 1;
      const group = drawn.slice(position, end + 1);
      steps.push(
        evidenceRollupStep(group, after(group[group.length - 1]!.index), latestAt(end), after),
      );
      position = end + 1;
      continue;
    }
    const following = drawn[position + 1];
    if (
      event.event_kind === "run.audit.intent" &&
      eventMetadata(event, "audit_action") === "run.model.pre_effect" &&
      following !== undefined &&
      following.event.event_kind === "model.served" &&
      following.event.event_status !== "withheld"
    ) {
      steps.push(
        modelPairStep(
          { event, index },
          following,
          after(following.index),
          latestAt(position + 1),
          models[index] ?? null,
          after,
        ),
      );
      position += 2;
      continue;
    }
    steps.push(
      stepForEvent(event, after(index), models[index] ?? null, latestAt(position), planByCallId),
    );
    position += 1;
  }
  return steps;
}

/**
 * THE SPAN THE RUN'S OWN RECORDS COVER, in seconds — the band summary's clock.
 *
 * First recorded stamp to last recorded stamp, both the server's. It is NOT the
 * turn's wall-clock age (TurnProgress owns that, measured from this machine's
 * send) and it is deliberately a different number: this one says how much of
 * the wait the Run has actually accounted for. `null` for fewer than two
 * events, because one stamp spans nothing.
 */
export function runActivityElapsedSeconds(events: readonly AgentRunEventWire[]): number | null {
  if (events.length < 2) return null;
  return secondsBetween(events[0]!.created_at, events[events.length - 1]!.created_at);
}

/**
 * THE TURN'S WHOLE RECORDED ACTIVITY: the tool calls, then the model calls and
 * the evidence and delivery decisions.
 *
 * WHY TWO SOURCES. The Run records its work in two durable places and neither
 * is a superset of the other, so a band built from one of them alone would
 * silently under-report the turn:
 *
 *   `agentic_plan`  ONE ROW PER TOOL CALL, appended immediately BEFORE the call
 *                   runs (ne-runtime runner.py `_RecordedExecutor.execute`
 *                   checkpoints `agentic-intent:{ordinal}` in its own committed
 *                   transaction) and projected at every status, not just at
 *                   terminal. It is the only place a TOOL NAME and its
 *                   `step_intent` appear. It carries no model call.
 *   the event log   `model.served` once per model call, plus `evidence.produced`
 *                   and `delivery.decided`. It carries no tool name.
 *
 * `runPlanSteps` is REUSED rather than reimplemented: the Agents Run workspace
 * already maps exactly this field to exactly these rows, and two mappers for one
 * wire field is how the two surfaces would drift apart.
 *
 * ORDER, STATED HONESTLY. Each group is in its own server-assigned order — the
 * plan by `ordinal`, the events by canonical `sequence` — and the two are
 * concatenated, NOT interleaved. The wire exposes no shared clock between them
 * (a checkpoint's `created_at` is not projected), so any interleaving would be
 * a chronology this console invented. Concatenating keeps every row true and
 * claims only the ordering the server actually stated.
 *
 * A Run whose plan went `null` — the projection makes it all-or-nothing on any
 * ordinal gap — contributes no tool rows, and the event rows still render. That
 * is a thinner account, never a fabricated one.
 *
 * ONE ROW PER TOOL CALL, NOT TWO (owner defect, 2026-08-28). The two sources
 * OVERLAP on exactly one thing: a call the executor checkpointed into the plan
 * is, moments later, also recorded as a `run.tool.pre_effect` event carrying
 * the SAME server-minted `tool_call_id`. Concatenating both drew every tool
 * call twice — once as a hoisted, timing-less plan row at the head of the band,
 * once in the event chronology with its duration — which the owner read,
 * correctly, as items "randomly inserted at the top". So: a plan row whose
 * `tool_call_id` the event log has recorded is DROPPED here, because the event
 * row is the same act with strictly more to say (its place in the sequence and
 * its measured duration; the tool name and stated intent ride onto it through
 * the `tool_call_id` join). A plan row the event log has NOT recorded keeps its
 * row — it is the only account of that call (the checkpoint lands before the
 * call runs, and a Run that died between the two still owes the reader the
 * attempt) — and an unbound plan row (`tool_call_id` null) is never dropped,
 * because nothing proves any event row is the same call.
 */
export function runTurnSteps(
  run: AgentRunDetailWire,
  events: readonly AgentRunEventWire[],
): TranscriptStep[] {
  // WHETHER THE RUN IS STILL GOING, read off the Run's own status and nowhere
  // else. `runIsTerminal` is the SAME predicate the poller stops on, imported
  // rather than restated, so the row the band marks as the frontier and the
  // moment the watch ends can never disagree.
  return [
    ...runPlanSteps(withoutEventRecordedPlanRows(run, events)),
    ...runActivitySteps(events, {
      live: !runIsTerminal(run.status),
      // The plan is ALSO handed to the event mapper so a `run.tool.pre_effect`
      // row can be joined — by the server's own `tool_call_id` — to the plan
      // step naming its tool and stated intent. The join adds words to an
      // event row; it invents no row and drops none.
      plan: run.strategy === "agentic" ? run.agentic_plan : null,
    }),
  ];
}

/** The `tool_call_id`s the event log records a `run.tool.pre_effect` row for —
 * the calls whose act row the band already draws in chronology. */
function recordedToolCallIds(events: readonly AgentRunEventWire[]): ReadonlySet<string> {
  const ids = new Set<string>();
  for (const event of events) {
    if (event.event_kind !== "run.audit.intent") continue;
    if (eventMetadata(event, "audit_action") !== "run.tool.pre_effect") continue;
    const id = eventMetadata(event, "tool_call_id");
    if (id !== null && id !== "") ids.add(id);
  }
  return ids;
}

/** The Run with its plan thinned to the calls the event log has NOT recorded —
 * the dedup `runTurnSteps`'s header states. `runPlanSteps` itself is shared
 * with the Agents workspace (which reads no event log and needs every plan
 * row), so the thinning happens on this, the only path that draws both. */
function withoutEventRecordedPlanRows(
  run: AgentRunDetailWire,
  events: readonly AgentRunEventWire[],
): AgentRunDetailWire {
  if (run.strategy !== "agentic" || !run.agentic_plan) return run;
  const recorded = recordedToolCallIds(events);
  if (recorded.size === 0) return run;
  return {
    ...run,
    agentic_plan: run.agentic_plan.filter(
      (step) =>
        step.tool_call_id === null || step.tool_call_id === "" || !recorded.has(step.tool_call_id),
    ),
  };
}

/**
 * WHY THE BAND LISTS NO TOOL CALL, for the band's own note slot — or `null`.
 *
 * Re-exported from the adapter that owns the rule so the two surfaces that draw
 * a Run (Ask's canonical turn, the Agents workspace) read one sentence rather
 * than two copies of a truthiness check. It never synthesises a description:
 * the sentence states the absence, and the band simply gets thinner.
 */
export { runPlanAbsentNote };
