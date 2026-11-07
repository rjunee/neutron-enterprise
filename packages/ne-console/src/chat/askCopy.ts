// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — ASK. Every string the screen renders, in one file.
//
// They live here for two reasons. The copy table in the spec is normative and
// must survive edits to the components that render it, so a test can hold the
// literal against the constant rather than against a JSX subtree. And an
// apostrophe typed straight in a .ts string stays the character the spec wrote;
// the same apostrophe typed into JSX text acquires an entity escape and stops
// matching.
//
// Nothing here explains the screen, argues for it, or names anything inside the
// repository. §2.4's grammar is fact, then next action, and no arguing.

/** The thread before anything has been asked. */
export const ASK_EMPTY_THREAD = "Ask a question about your connected sources.";

/** The server answered the session create with 422 — no department claim. */
export const ASK_NO_DEPARTMENT =
  "Your account isn't assigned to a department, so questions can't be routed. Contact your administrator.";

/** The server advertised grounding, then rejected that exact agent mapping
 * before session bootstrap completed. */
export const ASK_GROUNDING_UNAVAILABLE = "Grounding is no longer available for this question.";

/** The stream itself did not survive the turn. */
export const ASK_STREAM_ERROR = "The answer stream failed.";

/** The turn ran past the console's give-up time. */
export const ASK_TIMEOUT = "No answer arrived in time.";

/** The turn settled and produced no answer. */
export const ASK_NO_ANSWER = "No answer was produced.";

/** The reader pressed Stop. Honest about what Stop does: the console stopped
 * listening; a deployment that runs turns durably may still finish the turn,
 * and an approval it parked stays real. Never "the turn was cancelled". */
export const ASK_STOPPED =
  "Stopped. The turn may still finish on the server; an approval it requested stays in Approvals.";

/** A refusal from the policy decision point, on a turn that had started. */
export const ASK_REFUSED_BY_POLICY = "Refused by policy.";

/** THE AGENT DECLINED TO ANSWER, which is a correct outcome and the one beat
 * this product exists to demonstrate. It is not a fault and not an empty turn:
 * the agent read the question, judged it unanswerable from what it could
 * ground, and said so. The server's own words are the reason carried beside
 * this line, verbatim. */
export const ASK_DECLINED = "The agent declined to answer this question.";
export const ASK_DECLINED_STATE = "Declined";

/** The answer EXISTS and was produced; current authority over a source behind
 * it no longer permits showing it to this reader. Distinct from a refusal
 * (nobody decided against the question) and distinct from an empty turn
 * (something was produced) — rendering it as either states the wrong fact. */
export const ASK_WITHHELD = "Answer withheld — access to a source behind it changed.";
export const ASK_WITHHELD_STATE = "Withheld";

/** The Run the question became could not be read back. The turn is not known
 * to have failed — the console lost sight of it — and the Run keeps running. */
export const ASK_RUN_UNREADABLE = "Couldn't read this run's result. It may still be running.";

/** A Run that reached a terminal state without completing. The server's own
 * status word rides in `detail`; this is the stated line above it. */
export const ASK_RUN_DID_NOT_COMPLETE = "The run ended before it produced an answer.";

/** The turn is parked on a person. Polling continues; nothing is lost.
 *
 * It used to promise "the answer appears here once it is decided", and that is
 * a promise the platform does not make: an approval releases exactly the one
 * action it was asked about, the run performs THAT and ends. What lands here
 * afterwards is the record of the decision, not the answer the turn was
 * heading for. */
export const ASK_PAUSED_SENTENCE =
  "Waiting for an approval decision. An approval releases exactly that one action, and this turn closes here once it is decided.";

/** The one control a paused turn carries beside its receipt. The route to the
 * Run's own page is gone: the /agents redesign docks the record in place, and
 * a control that threw the reader onto another screen was the last survivor of
 * the IA that replaced. */
export const ASK_REVIEW_IN_APPROVALS = "Review in Approvals";

// The STATE WORD a promoted card carries (round-3 transcript render). It is the
// label above the stated line, not a second sentence — three words for three
// different things, because a refusal, a deliberate stop and a fault are three
// different facts and the reader acts on each differently.
export const ASK_REFUSED_STATE = "Refused";
export const ASK_STOPPED_STATE = "Stopped";
export const ASK_TURN_FAILED = "Turn failed";

export const ASK_TRY_AGAIN = "Try again";
export const ASK_RETRY = "Retry";
/** The control on a turn the READER stopped. Not "Try again" and not "Retry":
 * both say the last attempt went wrong, and this one did what it was told. */
export const ASK_ASK_AGAIN = "Ask again";

/** The composer's resting label; it becomes ASK_STOP for the life of a turn. */
export const ASK_SUBMIT = "Ask";
export const ASK_STOP = "Stop";
export const ASK_PLACEHOLDER = "Ask a question";

// ---- the history pane ------------------------------------------------------

export const ASK_NEW_QUESTION = "New question";
export const ASK_FILTER_QUESTIONS = "Filter questions…";

/** No server-backed history and no questions opened during this visit. */
export const ASK_HISTORY_VISIT_EMPTY = "No questions from this visit yet.";

/**
 * The degraded history pane's one line, at its foot.
 *
 * The API can create a session and stream a message; it cannot list a
 * principal's sessions or read their messages back — YET conversation turns
 * ARE persisted server-side (the runtime session store), so this line must
 * not claim nothing is kept. It states only what this pane can show. The
 * browser-storage journal the old home screen kept is not moved here: a
 * history that silently vanishes on another machine is worse than a stated
 * limit.
 */

/** Stored history, granted and empty (the state that exists once the server
 * serves a session list). */
export const ASK_HISTORY_EMPTY = "No questions yet.";

/** Stored history, filtered to nothing. */
export const ASK_HISTORY_NO_MATCHES = "No matching questions.";

// ---- the source pane -------------------------------------------------------

export const SOURCE_PANE_LABEL = "Source";
export const SOURCE_LOADING = "Loading source…";
export const SOURCE_REFUSED = "You don't have access to this source.";
export const SOURCE_ERROR = "Couldn't load the source.";
export const SOURCE_OPEN_IN_SYSTEM = "Open in source system";
export const SOURCE_VIEW_IN_GRAPH = "View in Graph";
export const SOURCE_VIEW_IN_DOCUMENTS = "View in Documents";

/** Why the grounding segment is frozen on an open conversation: the mode is
 * BOUND when the session is created (chat/useAgentStream hands the agent name
 * to the one `createSession` call and nowhere else; ne-api routers/chat.py
 * routes the whole session by it), so mid-conversation it is a fact to
 * display, not a control to flip. The segment stays on screen, disabled, so
 * the reader sees what the conversation is set to instead of watching the
 * control vanish — the owner's actual complaint, twice. */
export const GROUNDING_BOUND_NOTE =
  "Grounding is chosen when a conversation starts and applies to the whole conversation. Start a new conversation to change it.";
export const SOURCE_VIEW = "View source";

/** The source block's line while the settled turn's citation read is still in
 * flight (useAgentStream: the evidence envelope, then the authorized retrieve).
 * "Resolving", deliberately — the citations are FETCHED and MATCHED, nothing is
 * generated, and the word must not claim otherwise. Drawn only in the window
 * between the Run settling and the read landing (or failing, or timing out);
 * it is never how "the read landed and found nothing" renders. */
export const ASK_CITATIONS_RESOLVING = "Resolving citations…";

// ---- signals ---------------------------------------------------------------

export const FEEDBACK_HELPFUL = "Helpful";
export const FEEDBACK_NOT_HELPFUL = "Not helpful";
export const FEEDBACK_RECORDED = "Feedback recorded";
export const FEEDBACK_FAILED = "Couldn't save feedback — try again";
export const CITATION_FLAG = "Flag";
export const CITATION_FLAGGED = "Citation flagged";

// ---- governed tool calls ---------------------------------------------------

export const TOOL_ALLOWED = "Allowed";
// "run resumed" was the older word here and it invited the wrong inference:
// the run does resume, but only far enough to execute this one released call,
// and then it ends. Naming the ACT says what happened without implying the
// plan carried on. (`TOOL_APPROVED_ACTION_FAILED` below already spoke this way.)
export const TOOL_APPROVAL_EXECUTED = "Approved · action executed";
export const TOOL_APPROVAL_REJECTED = "Approval rejected";
export const TOOL_APPROVAL_EXPIRED = "Approval expired";
export const TOOL_APPROVAL_ESCALATED = "Approval escalated";
export const TOOL_APPROVAL_INTEGRITY_FAILED = "Approval integrity check failed";
export const TOOL_APPROVAL_FAILED = "Approval did not proceed";
export const TOOL_APPROVED_ACTION_FAILED = "Approved action failed during execution";
export const TOOL_VIEW_DECISION = "View decision";
export const TOOL_AWAITING_APPROVAL = "Awaiting approval";
/** The policy decision point refused the call. The state word of its gate. */
export const TOOL_REFUSED = "Refused";
/** What the number in a refused gate's reference slot IS: the audit sequence
 * the decision was recorded at. The number is the server's; this is its label. */
export const TOOL_AUDIT_REFERENCE = "audit";
/** The one control a gate awaiting a person carries. */
export const TOOL_REVIEW_IN_APPROVALS = "Review in Approvals";

/** The runtime exposed an internal reasoning span. Its body is never rendered,
 * announced, copied, or exported. */
export const ASK_INTERNAL_REASONING_HIDDEN =
  "Internal reasoning was received and is not displayed.";

/** A neutral link from the server's ordinary done.trace_id correlation. */
export const ASK_OPEN_TRACE = "Open trace";

// ---- the turn's answer and its one receipt -------------------------------

/** The attribution above the answer — the turn's only unboxed prose. */
export const ASK_ANSWER_LABEL = "Answer";

/** The receipt's outcome word. "Paused" is the turn parked on a person.
 *
 * A turn that ended without an answer ended for one of three reasons and the
 * footer names WHICH — "Failed" only when something broke. The receipt used to
 * stamp "Failed" on all three, so a refused turn closed with a red ✕ Failed one
 * line under its grey ⊘ REFUSED card and the footer called the governance
 * system an outage. MessageList.tsx picks the word out of a table keyed by the
 * failure's own tone, so the word and the colour are chosen by ONE value and a
 * new tone cannot ship wordless. */
export const ASK_RECEIPT_COMPLETE = "Complete";
export const ASK_RECEIPT_PAUSED = "Paused";
export const ASK_RECEIPT_FAILED = "Failed";
export const ASK_RECEIPT_REFUSED = "Refused";
export const ASK_RECEIPT_STOPPED = "Stopped";

/** Why a paused turn is paused, stated as the fact it is. */
export const ASK_AWAITING_DECISION = "Awaiting a decision";

/** How many of this turn's governed calls the server refused. Counted from the
 * gates the transcript actually drew, so it can never name one the ledger does
 * not list; omitted entirely at zero, because a zero here is a claim. */
export function askRefusedCount(count: number): string {
  return `${count} refused`;
}

/** The alias THIS REQUESTER asked for. Never a served-model claim: it is the
 * reader's own pick, and the model slot beside it is the Run's. */
export const ASK_REQUESTED_MODEL = "Requested model";

/**
 * THE RELATIONSHIP BETWEEN THE TWO MODEL FACTS, WHICH THE ROW USED TO OMIT.
 *
 * The footer rendered `Requested model · frontier-best · Model · private-default`
 * — two bare aliases side by side, both true, with nothing saying how they
 * relate. The composer had just promised `frontier-best` and the turn closed
 * naming something else one span later, silently. That is the SILENT DEGRADE
 * this product exists to make impossible: the reader asked for one model, a
 * different one answered, and no line on the screen reconciled the two.
 *
 * WHAT IT ACTUALLY WAS, in the case that surfaced this: every Anthropic call
 * was being rejected by the provider (`litellm.BadRequestError` on
 * `tool_call_id`), so `ne.gateway.complete` exhausted the frontier candidate
 * and fell down the chain to the local lane. Nothing was refused — the policy
 * decision point allowed frontier; the provider call failed. A lane fallback
 * after a provider failure is a real governance event and the reader is
 * entitled to see that one happened.
 *
 * WHAT THIS ROW SAYS NOW, AND WHERE EACH WORD COMES FROM. The row used to
 * state the divergence and point at the trace, because the reason existed only
 * on the trace wire. `RunView` now carries `route_position` / `fallback_from`
 * (ne-api/runs_wire.py) and `model.served` carries the same pair, so the
 * receipt states the routing itself:
 *   - `ASK_MODEL_AS_REQUESTED` — the Run said `route_position: "primary"`:
 *     nothing was attempted before the model that answered. A POSITIVE claim
 *     read off the record, never inferred from two names matching.
 *   - `askModelFellBackFrom` — the Run said `"fallback"` and named what was
 *     passed over. THIS IS THE INVOLUNTARY PROVIDER FALLBACK, said out loud.
 *   - `ASK_MODEL_FELL_BACK` — `"fallback"` with no usable name. The fallback
 *     happened; no name is invented for it.
 *   - `ASK_MODEL_ROUTING_UNRECORDED` — the Run recorded no position at all.
 *     Still not "served as requested": an absent record is not reassurance.
 *
 * WHAT IT STILL WILL NOT SAY. Not the provider's error text — a provider
 * message can echo prompt substrings back, so the wire carries names and
 * positions only and the server drops anything else. And not "policy moved
 * you": that is a `model.changed`, a deliberate change with an actor, and a
 * reader has to be able to tell the two apart. This copy describes the route
 * moving under an unchanged policy and says nothing about who chose what.
 */
export const ASK_MODEL_AS_REQUESTED = "served as requested";
export const ASK_MODEL_FELL_BACK = "the requested model did not serve — a fallback answered";
export const ASK_MODEL_ROUTING_UNRECORDED =
  "a different model served — this turn recorded no routing";

/** The involuntary fallback, naming the candidate(s) that did not answer. The
 * server guarantees these are declared model names and nothing else. */
export function askModelFellBackFrom(passedOver: string): string {
  return `${passedOver} did not serve — a fallback answered`;
}

/** The factual notes beside that em dash. A finished turn was answered by
 * something the server declined to name; a failed turn produced no reply at all,
 * and the two are not the same absence. */
export const ASK_NO_MODEL_NAMED = "the server named no model";
export const ASK_NO_MODEL_REPLY = "no model reply was served";

/** The runtime printed a tool call where an answer belongs — it never reached a
 * decision point, so it is not a governed call, and the turn did not complete. */
export const ASK_UNDISPATCHED_FRAME =
  "Turn failed — the runtime emitted an undispatched tool-call frame.";

/** The label over one such frame's verbatim body. */
export function askRawFrameLabel(index: number): string {
  return `RAW — UNCLASSIFIED FRAME ${index}`;
}

// ---- the in-flight turn ----------------------------------------------------
//
// Everything on this line is MEASURED, never simulated: the elapsed seconds are
// this machine's clock since the question was sent, and the server-activity age
// is the time since the last thing — frame or keep-alive — actually arrived on
// the wire. There is no percentage, no synthetic step list, no spinner: the
// tool rows above the line are the turn's real hops, exactly as many as the
// server has reported. (Mirrors Search's measured `Searching… {n}s` + Cancel.)

/** Nothing — no frame, no keep-alive — has arrived on this turn's stream yet. */
export const ASK_NO_SERVER_WORD = "nothing from the server yet";
/** The measured age of the last thing that arrived on the wire. */
export function askServerWordAge(seconds: number): string {
  return `last word from the server ${seconds}s ago`;
}

// ---- the canonical Run's recorded activity ---------------------------------
//
// Canonical Ask answers off a durable Run, not off the stream (useRunCompletion
// explains why). The Run's own event log is therefore the ONLY account of what
// the turn is doing while it does it, and these are the words for the kinds the
// server projects. Every one of them names something the server RECORDED: there
// is no entry here for "thinking", none for a step nobody wrote, and the mapper
// that uses them (chat/runActivity.ts) invents no row the event log lacks.

/** `model.served` — one model call the Run recorded, in the band's name slot
 * when the event carried no served-model alias to name it with. */
export const RUN_STEP_MODEL_CALL = "Model call";
/** The verdict word for a model call the Run recorded as served. */
export const RUN_STEP_SERVED = "Served";
/** `model.changed` — routing moved this Run to a different model mid-flight. */
export const RUN_STEP_MODEL_ROUTED = "Model routing";
export const RUN_STEP_CHANGED = "Changed";
/** `evidence.produced` — a retrieval that yielded evidence for the answer. */
export const RUN_STEP_EVIDENCE = "Evidence";
export const RUN_STEP_PRODUCED = "Produced";
/** `delivery.decided` — the governed decision about delivering the output. */
export const RUN_STEP_DELIVERY = "Delivery";
export const RUN_STEP_DECIDED = "Decided";
// ---- the governed audit actions, in plain language -------------------------
//
// A Run event of kind `run.audit.intent` stores only HASHES of what it did, so
// on its own it renders as "Step · Not disclosable" — and a normal turn emits
// a dozen of them. The readable action name lives one join away, in the
// governed audit chain itself, and ne-api's projection now serves it as the
// `audit_action` metadata key. These are the words for the seven actions a
// Run's own audit sink may write. Every one names a class of step the server
// RECORDED; none of them describes what the step contained.
//
// THE PRE-EFFECT THREE. `run.tool.pre_effect`, `run.model.pre_effect` and
// `run.execution.pre_effect` are each written immediately BEFORE the governed
// effect they cover, which cannot proceed until that audit receipt persists.
// So the honest verdict word is RECORDED — the record exists — and never
// "Completed", which would claim an outcome the event does not carry.

/** `run.tool.pre_effect` — one governed tool call the Run was cleared to make. */
export const RUN_STEP_TOOL_CALL = "Tool call";
/** `run.execution.pre_effect` / `run.execution.failed` — the Run's own
 * execution boundary, as opposed to one model or tool call inside it. */
export const RUN_STEP_EXECUTION = "Execution step";
/** The verdict word for a pre-effect audit record: the record EXISTS. It does
 * not say the effect finished, because the event does not say so either. */
export const RUN_STEP_RECORDED = "Recorded";
/** `run.execution.failed` — the Run recorded a failure before finalization. */
export const RUN_STEP_FAILED = "Failed";

// ---- the lifecycle kinds the store writes ----------------------------------

/** `run.start.accepted` — the submission the Run was opened on. */
export const RUN_STEP_REQUEST_ACCEPTED = "Request accepted";
/** `run.cancel.accepted` — a cancellation the server admitted. */
export const RUN_STEP_CANCEL_ACCEPTED = "Cancellation accepted";
/** `run.retry.accepted` — a retry the server admitted. */
export const RUN_STEP_RETRY_ACCEPTED = "Retry accepted";
/** The verdict word for all three: admitted, which is all they assert. */
export const RUN_STEP_ACCEPTED = "Accepted";
/** `run.execution.claimed` — a worker took the Run and began executing it. */
export const RUN_STEP_EXECUTION_STARTED = "Execution started";
export const RUN_STEP_STARTED = "Started";
/** `run.execution.recovered` — the Run's lease was re-established after an
 * interruption. Its own status says whether the outcome was certain. */
export const RUN_STEP_EXECUTION_RECOVERED = "Execution recovered";
export const RUN_STEP_RECOVERED = "Recovered";
/** `evidence.verified_row` — one verified row of the evidence set. */
export const RUN_STEP_EVIDENCE_ROW = "Evidence row";
/** `claim.verifier` — the finalizer's check of one claim against its evidence. */
export const RUN_STEP_CLAIM_CHECK = "Claim check";
export const RUN_STEP_VERIFIED = "Verified";

/**
 * `event.unavailable` — the Run RECORDED a step here and the projection would
 * not name its kind (run_projection.event_view clamps every kind outside its
 * safe set to this one). The row still renders, because the step happened: an
 * omitted row would say the Run did less work than it did, which is the exact
 * lie the blank screen told. What it must not do is claim to know WHAT it was.
 */
export const RUN_STEP_UNNAMED = "Step";
export const RUN_STEP_NOT_DISCLOSABLE = "Not disclosable";
/** The same row where the projection reported the record itself as withheld —
 * a governance outcome, and it renders as ITSELF rather than as an absence. */
export const RUN_STEP_WITHHELD = "Withheld";

/**
 * THE FOLD'S WORD. A consecutive run of records the projection would not name —
 * real governed work, no word for it — folded into one row. This is now the
 * RARE case: the server names thirteen kinds and the seven governed audit
 * actions behind `run.audit.intent`, so the fold only ever sees a kind this
 * console does not know, or an intent the server declined to name.
 *
 * WHY THE COUNT IS IN THE NAME. The band's summary line is the audit précis and
 * it joins the rows' NAMES; a fold that read "Step" there would tell a reader
 * scanning the closed band that one step happened where twelve did. The row
 * still carries `foldedCount`, so the précis's headline number counts every one
 * of them (transcript/promotion.ts).
 *
 * WHAT IT DOES NOT DO: it does not relabel "Not disclosable" into something
 * softer. The folded row keeps that verdict word verbatim, because the fact
 * about those records is unchanged — the viewer may not see their content. Only
 * their REPETITION is compressed.
 */
export function runFoldedStepsName(count: number): string {
  return `${count} governed steps`;
}

/**
 * WHAT A RETRIEVAL PRODUCED, as the server counted it. `contributor_count` is
 * the number of source-authority stamps supporting the served result — a scalar
 * fact ABOUT the retrieval, served on `evidence.produced` — and this is only
 * the plural. It never estimates, and the caller never prints it without the
 * served number.
 */
export function runEvidenceContributorsLabel(count: number): string {
  return count === 1 ? "1 contributor" : `${count} contributors`;
}

// ---- the evidence roll-up, in plain language --------------------------------
//
// A real turn records one `evidence.produced` event PER RETRIEVED RECORD and
// one `evidence.verified_row` per verified row — 128 events on a measured S1
// run, 60 of them saying "the template returned nothing", each drawn as its own
// "Evidence · Produced · verified_t3_empty · 1 contributor · run-tool" row. A
// banking reader cannot use that. chat/runActivity.ts now compresses each
// consecutive run of evidence events into ONE row wearing these words; the
// verbatim per-event rows survive inside the row's own disclosure
// (TranscriptStep.detail). Every number below is a count of events the server
// recorded — nothing is estimated, and an empty result is still stated, once,
// with its own count, rather than sixty times or not at all.

/** The rolled-up row's name when the reads produced records. */
export function runEvidenceRetrievedName(records: number): string {
  return records === 1 ? "Retrieved 1 record" : `Retrieved ${records} records`;
}

/** The rolled-up row's name when every read in the group came back empty. The
 * absence is stated, once — never smoothed over and never repeated per read. */
export const RUN_STEP_NO_RECORDS = "No matching records";

/** How many reads came back empty — the served fact behind the absence. */
export function runEvidenceEmptyReadsLabel(count: number): string {
  return count === 1 ? "1 lookup returned nothing" : `${count} lookups returned nothing`;
}

/** How many rows of the produced evidence the server verified. */
export function runEvidenceVerifiedLabel(count: number): string {
  return count === 1 ? "1 row verified" : `${count} rows verified`;
}

/** The verdict word on the rolled-up row: these events were RECORDED, which is
 * all they assert — the same honesty rule as the pre-effect rows. */
export const RUN_STEP_EVIDENCE_ROLLUP_LABEL = "Recorded";

/**
 * WHERE A READ WENT, in plain words — the closed `read_kind` vocabulary the
 * projection serves (ne-api run_projection `_SAFE_READ_KINDS`), translated for
 * the top-level row. The raw code still renders verbatim on the per-event
 * detail rows. A code outside this map returns null and the roll-up simply
 * names no source, rather than guessing one.
 */
export function runEvidenceSourceWords(readKind: string): string | null {
  switch (readKind) {
    case "search":
      return "knowledge search";
    case "graph_path":
    case "verified_t3":
    case "verified_t3_empty":
      return "the verified graph";
    default:
      return null;
  }
}

/** "from the verified graph", "from knowledge search and the verified graph" —
 * the roll-up's source clause, over the DISTINCT plain words present. */
export function runEvidenceSourceClause(words: readonly string[]): string | null {
  if (words.length === 0) return null;
  return `from ${words.join(" and ")}`;
}

/** The aux note on a model call's rolled-up row — the two records (the audit
 * receipt written before the call, the served reply after it) are one act to a
 * reader, and this is the act's plain name. */
export const RUN_STEP_MODEL_CALL_NOTE = "model call";

/**
 * WHY NO TOOL CALL IS LISTED, when the Run served no plan.
 *
 * `agentic_plan` is the only place a tool name and its intent appear, and it is
 * all-or-nothing: one ordinal gap nulls the whole field, and a tool call
 * rejected by name increments the ordinal without writing a checkpoint, which
 * nulls it permanently for the rest of that Run. The event log cannot stand in
 * for it — it carries no tool name at all — so the band gets THINNER and says
 * why. Reconstructing a plan from the events would be this console writing the
 * Run's history for it.
 */
export const RUN_PLAN_UNAVAILABLE =
  "No tool plan was served for this Run, so no tool call is listed here.";

/** The work band's accessible name on a canonical Ask turn. */
export const RUN_WORK_LABEL = "Steps in this turn";
