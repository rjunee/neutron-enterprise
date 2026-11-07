// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// WHAT KIND OF RECORD IS THIS? — the classification behind §13's event-class
// chips (Decisions · Tool calls · Access refusals).
//
// A ledger page read as ops is mostly `api.*.unauthorized`: the reader's OWN
// refused page loads from the preceding five minutes, drawn identically to the
// decisions they came for. Those rows are correct and they belong on the chain.
// The defect is presentational — a refused READ and a decided ACTION are
// different kinds of record and were drawn the same.
//
// FOUR CLASSES:
//   decision        a verdict about an ACTION — policy decisions, approval
//                   verdicts, dispatches, gateway completions.
//   tool-call       a tool the runtime ran, or refused to run. The audited
//                   `tool.*` actions: execute, invoke, approval, unknown,
//                   toolset_denied.
//   access-refusal  someone asked to READ a surface and the server said no.
//   other           everything else. It has no chip, and is therefore always
//                   rendered — a class nobody was given a control over must
//                   never be hidden by one.
//
// Pure and exported so the classification is testable, and so /audit,
// /approvals and Home narrow the same way rather than each inventing a filter.

export type EventClass = "decision" | "tool-call" | "access-refusal" | "other";

/** `api.<surface>.unauthorized` is the server's own name for "this key asked to
 * read something and was refused" (authz.require_role's audit_action). It is the
 * exact and only shape, so the match is anchored rather than a substring sniff. */
const UNAUTHORIZED = /(^|\.)unauthorized$/;

/** Actions that are decisions ABOUT AN ACTION rather than about a read.
 *
 * The approval alternation is the gate's REAL emitter vocabulary
 * (`ne-runtime/approvals.py`): `approval.granted` is the approve verdict and
 * `approval.expired` the system verdict — both were missing here, so a grant
 * classified "other" and escaped the Decisions chip while a rejection did not.
 * (`approved`/`decided` stay for compatibility with older stored chains.) */
const DECISION =
  /(pdp\.|policy\.|approval\.(granted|approved|rejected|submitted|decided|expired)|subagent\.|dispatch|gateway\.complete|session\.run|scram)/;

/** The audited tool segment — `tool.execute`, `tool.invoke`, `tool.approval`,
 * `tool.unknown`, `tool.toolset_denied`. Anchored to a whole `tool.` segment so
 * neither `toolset_denied`'s own prefix nor a `tool_call` payload key can drag
 * an unrelated row into the class. */
const TOOL_CALL = /(^|\.)tool\./;

export function classifyEvent(event: { event_type: string; action: string | null }): EventClass {
  const action = (event.action ?? "").toLowerCase();
  const type = event.event_type.toLowerCase();
  if (UNAUTHORIZED.test(action) || UNAUTHORIZED.test(type)) return "access-refusal";
  // Decision is tested BEFORE tool: a policy verdict about a tool is a verdict,
  // and the chip a reviewer reaches for when auditing verdicts must contain it.
  if (DECISION.test(action) || DECISION.test(type)) return "decision";
  if (TOOL_CALL.test(action) || TOOL_CALL.test(type)) return "tool-call";
  return "other";
}

/** How many of a page are access refusals — the count a filter chip must carry,
 * because a filter that hides rows without saying how many is not a filter. */
export function countRefusals(
  events: ReadonlyArray<{ event_type: string; action: string | null }>,
): number {
  return events.filter((e) => classifyEvent(e) === "access-refusal").length;
}

/** The default view: everything that is not an access refusal. */
export function withoutAccessRefusals<T extends { event_type: string; action: string | null }>(
  events: T[],
): T[] {
  return events.filter((e) => classifyEvent(e) !== "access-refusal");
}

// ===========================================================================
// WHAT HAPPENED, IN WORDS
// ===========================================================================
//
// The ledger's `action` is the SERVER's identifier — `pdp.decide`,
// `tool.toolset_denied`, `api.audit.unauthorized`. That is the right thing to
// store, to filter on and to export, and the wrong thing to put in front of a
// reader who has never seen this vocabulary. Home puts it on the FIRST screen
// of the product, one row under the tiles.
//
// So the code stays the record and this is its reading. Every surface that
// renders a label keeps the raw code within reach — a `title` on the cell, and
// the record pane's own Action row — because a compliance screen that hides the
// identifier is worse than one that shows only the identifier.
//
// THE VOCABULARY IS OPEN-ENDED AND ALWAYS WILL BE. `read.kg.*` is generated per
// deployment from the knowledge graph, `api.<surface>.unauthorized` gains a
// member with every new API surface, and connectors name their own actions. A
// closed map would be wrong the day after it shipped, so there are three tiers:
//
//   1  EXACT   the enumerated core the classes above already name, where a
//              stock phrase says it better than the code's own words.
//   2  FAMILY  `*.unauthorized` — the one open family whose meaning is fixed by
//              its emitter (`authz.require_role`): a role pre-check refused this
//              principal on this surface. The surface segment is carried through,
//              so `api.audit.…` reads "Audit access refused" and the reader still
//              knows WHAT was refused.
//              🔴 IT DOES NOT SAY "read". `require_role` is mounted on WRITES
//              too, and this cell said "read refused" over all of them: `POST
//              /v1/approvals/{id}/approve` emits `api.approval.unauthorized`
//              (`routers/approvals.py:690,830`), so a refused attempt to DECIDE a
//              tier-3 approval rendered as a refused LOOK — on the compliance
//              ledger, one click into the Access refusals chip. Same for `POST
//              /v1/network/air-gap` (`network.py:115,125`) and `POST
//              /v1/models/lifecycle/{id}/transition` (`models.py:459,471`).
//              Verified by reading each emitter's decorator. The verb is not in
//              the record, so this cell does not supply one; "access refused" is
//              true of a refused GET and a refused POST alike, and matches the
//              `/audit` chip that is already called "Access refusals". It is also
//              not misreadable as an approval DECISION the way a bare "Approval
//              refused" would be.
//   3  WORDS   everything else: the code's OWN segments, spaced and sentence
//              cased. `connector.sync` → "Connector sync".
//
// TIER 3 IS THE POINT OF THE DESIGN, not a leftover. An unrecognised code must
// never render as "Unknown event" or an em dash: that deletes the only account
// of what happened the console has, on the one screen whose entire job is to
// account for what happened. Re-spacing the server's own words invents nothing,
// stays readable for a code somebody adds next year, and for a single-segment
// action degrades to that action, capitalised — which is the honest floor.
//
// The floor is a claim about EVERY string, not only about well-formed ones, and
// it used to hold only for well-formed ones — see `NO_WORDS` and `eventActionCode`
// for the two ways a degenerate code got underneath it and what happens now.

/** The identifier a record is filtered, sorted and exported by. `action` is the
 * emitter's name for what it did; `event_type` is the fallback the wire carries
 * when a record has no action. Every Action cell reads it through here so the
 * rule is written once rather than at five call sites.
 *
 * A BLANK action is no action. This was `event.action ?? event.event_type`, and
 * `??` only catches `null`: `AuditRecordView.action` is `str | None` with NO
 * `min_length` (`wire.py:1041`), so an emitter that writes `""` — or a value
 * that is nothing but whitespace — sends it straight through to an empty Action
 * cell, an empty hover `title` and an empty sort key. An empty string is not an
 * identifier, so it takes the same path `null` takes and the record is read by
 * its type. The value is returned UNTRIMMED when it has content: the code is the
 * record, and this function is not the place to rewrite what the server stored. */
export function eventActionCode(event: { event_type: string; action: string | null }): string {
  const action = event.action;
  return action !== null && action.trim() !== "" ? action : event.event_type;
}

/** Tier 1. Only codes whose emitter was read: a phrase here is a claim about
 * what the server did, and a wrong one is worse than the raw code. */
const EXACT_LABELS: Readonly<Record<string, string>> = {
  // Decisions about an action.
  "pdp.decide": "Policy decision",
  "approval.requested": "Approval requested",
  "approval.granted": "Approval granted",
  // `approved`/`decided` are the older stored spellings the DECISION pattern
  // above still accepts; they are the same verdict and read as the same phrase.
  "approval.approved": "Approval granted",
  "approval.decided": "Approval decision",
  "approval.decide": "Approval decision",
  "approval.rejected": "Approval rejected",
  "approval.escalated": "Approval escalated",
  "approval.expired": "Approval expired — nobody decided in time",
  "guardrail.block": "Blocked by a guardrail",
  "session.run": "Agent session run",
  "subagent.route": "Subagent routed",
  "subagent.dispatch": "Subagent dispatched",
  "subagent.complete": "Subagent finished",
  "ne.gateway.complete": "Model call completed",
  "ne.gateway.stream": "Model call streamed",
  // Tool calls. The two denials name their reason, because "denied" in the
  // Outcome column does not say which of the two gates said no.
  "tool.execute": "Tool run",
  "tool.invoke": "Tool called",
  "tool.validate": "Tool arguments checked",
  "tool.approval": "Tool approval check",
  "tool.unknown": "Refused: tool is not registered",
  "tool.toolset_denied": "Refused: tool is outside this agent's toolset",
};

/** Tier 2. Same anchor as UNAUTHORIZED, with the surface captured so it can be
 * spoken rather than dropped. */
const UNAUTHORIZED_SURFACE = /^(?:(.*)\.)?unauthorized$/;

/** `api.` and `ne.` are namespaces, not words — a reader gains nothing from
 * "Api chat turn replayed" that they do not get from "Chat turn replayed". */
const NAMESPACE = /^(?:api|ne)\./;

/** The words a code actually says: its segments spaced and sentence cased, or
 * "" when it says none. Kept separate from `inWords` because tier 2 needs the
 * difference between "no surface" and "a surface with no words" — a bare
 * `.unauthorized` must still read "Access refused" and never inherit the floor
 * phrase below, which would make a sentence out of a non-word. */
function spokenWords(text: string): string {
  const words = text.replace(/[._\s]+/g, " ").trim();
  if (words === "") return "";
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The tier-3 floor for a code that has no words in it at all.
 *
 * Tier 3 forbids a placeholder so that the console never DELETES words the
 * emitter gave it — that is the whole argument above. It says nothing about a
 * code that gave no words, and there the old `return code` was the worse
 * outcome in both directions: `action: ""` printed an EMPTY Action cell, and
 * `"."`/`"_"`/`"api."` printed the raw punctuation, which a reader reads as a
 * broken console rather than as a record that named nothing. Neither is honest
 * about what happened. This phrase is: the record exists, it is on the chain,
 * its emitter did not name what it did. Nothing is lost by saying so, because
 * the raw code stays on the cell's `title` and spelled out in the record pane —
 * the invariant this whole section is built on. */
const NO_WORDS = "Unnamed action";

/** Tier 3. The code's own segments as a sentence-cased phrase. */
function inWords(code: string): string {
  // The namespace strip drops a word nobody reads; it must never be the thing
  // that empties the phrase. `api.` and `ne.` are whole codes whose only word
  // IS the namespace, and stripping first left `""`, which the old `return
  // code` then printed as the literal `api.` — a raw code with a dangling
  // separator, in a column of prose.
  const spoken = spokenWords(code.replace(NAMESPACE, "")) || spokenWords(code);
  return spoken === "" ? NO_WORDS : spoken;
}

/**
 * The human reading of a record's action — never a substitute for the code,
 * always safe to render for a code nobody has ever seen (see the three tiers
 * above; an unknown code becomes its own words, never a placeholder). Total
 * over `string`: it returns a non-empty, non-punctuation phrase for ANY input,
 * including one that is empty, blank, punctuation only, or a name off
 * `Object.prototype` — which is what "safe to render" has to mean when the
 * value goes straight into JSX.
 */
export function eventActionLabel(event: { event_type: string; action: string | null }): string {
  const code = eventActionCode(event);
  // Trimmed for the LOOKUP only: a stray space around a code the map knows must
  // not cost the reader the phrase. The code itself is still rendered as stored.
  const key = code.trim().toLowerCase();

  // `Object.hasOwn`, not a bare index. `EXACT_LABELS` is a plain object literal,
  // so `EXACT_LABELS["constructor"]` and `EXACT_LABELS["__proto__"]` are
  // `!== undefined` and hand back `Object`'s constructor FUNCTION and
  // `Object.prototype` — out of a function TypeScript has typed `: string`.
  // Both are then rendered as a React child (`AuditPage.tsx:763`,
  // `LandingPage.tsx:281`): the function makes React log an error, and the
  // object throws "Objects are not valid as a React child", which takes the
  // whole ledger page down. This is not hypothetical for the reason stated at
  // the top of this section — the vocabulary is generated per deployment, so
  // the map is asked about arbitrary strings by design.
  const exact = Object.hasOwn(EXACT_LABELS, key) ? EXACT_LABELS[key] : undefined;
  if (exact !== undefined) return exact;

  const refusal = UNAUTHORIZED_SURFACE.exec(key);
  if (refusal) {
    const surface = refusal[1] === undefined ? "" : spokenWords(refusal[1].replace(NAMESPACE, ""));
    // A bare `unauthorized` carries no surface to name, so it says only the
    // fact — still a sentence, still not a placeholder.
    return surface === "" ? "Access refused" : `${surface} access refused`;
  }

  return inWords(code);
}
