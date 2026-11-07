// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// HOW THE EVIDENCE DRAWER SAYS WHAT THE SERVER SENT.
//
// Everything the drawer receives is a closed server enum or a closed set of
// server literals, and the drawer used to paint several of them onto the screen
// exactly as they arrived. The worst case was an evidence record whose pointer
// could not be resolved: a BUTTON labelled "Evidence locator unavailable", a
// badge reading "Unavailable" beside it, and — because every piece of row detail
// is gated on the record being available — nothing else on the row at all. One
// missing pointer, said twice in machine words, on a control that led nowhere.
//
// This module is the single place those values become sentences. Three rules
// hold throughout it:
//
//   1. A sentence never claims more than its value carries — in EITHER
//      direction. "Not established" means this console cannot say how something
//      was checked. It never means a check failed, and it never means the
//      server holds nothing: the server routinely holds more than this console
//      is willing to badge, and a sentence about the server's records is a
//      sentence this module is not in a position to write.
//   2. A NON-DISCLOSURE IS NEVER REPORTED AS AN ABSENCE — AND ITS CAUSE IS
//      NEVER NAMED. On the wire, `withheld` means the server HAS the thing and
//      this read did not get it, while `unavailable` means there is nothing to
//      disclose. They are different facts and they get different badges. A
//      reader whose claim went withheld after an entitlement change needs to
//      look at their access; told instead that nothing was recorded, they
//      would raise a ticket against the grounding pipeline for a record that
//      is perfectly intact.
//
//      THE SECOND HALF OF THAT RULE IS NEW, AND THE JUSTIFICATION IT REPLACES
//      IS WHY IT HAD TO BE. This rule used to add "the projection will only
//      pair that state with a disclosure decision (`evidence_projection.py`
//      rejects any other message for it)", and the `Withheld` gloss below was
//      written against it: it told the reader a decision had been made about
//      their access. What that file constrains is the MESSAGE. It does not
//      constrain the CAUSE. An evidence row is projected `withheld`
//      (`evidence_projection.py:1171`) whenever `resolve_locator` came back
//      anything but `authorized`, and FIVE distinct conditions reach that:
//      a source stamp evaluated PERMANENT_WITHHELD (:298) — the access decision
//      — but also a stamp that evaluated RETRYABLE_UNKNOWN (:296), an evaluator
//      or authenticated-context read that raised (:294), a context whose
//      principal or hash did not match the projection's or a payload carrying
//      no stamps at all (:291), and a store that could not load the retained
//      locator (:352). So a source-authority service that is briefly
//      unreachable during a projection puts a row on this screen in exactly the
//      state a permissions change does, and `claimAssurance` inherits every one
//      of those through the `evidence_withheld` cause (:1310, :891). The badge
//      may say the record is held and was not released. It may not say why, and
//      a reader sent to their access administrator over a platform outage is
//      the same misdirection as one sent to the grounding team over their own
//      access, arriving by the opposite route.
//   3. A value this build has not seen is reported as unrecognised, carrying the
//      raw value in its tooltip. Inventing a friendly label for an unknown arm
//      would state a fact the server never sent; printing the raw token alone
//      leaves a reader unable to tell a machine word from a sentence written for
//      them.

import type { AgentEvidenceEnvelopeWire } from "../api/types";

export type AssuranceLabel =
  | "Direct"
  | "Inferred"
  | "Extracted"
  | "Verified"
  | "Grounded"
  | "Refused"
  | "Withheld"
  | "Not established";

export const ASSURANCE_EXPLANATIONS: Record<AssuranceLabel, string> = {
  Direct: "Recorded as read straight from a source record.",
  Inferred: "Recorded as inferred from other records rather than read directly.",
  Extracted: "Recorded as extracted from the text of a source document.",
  // These two sentences are worn by a single claim AND by the answer as a
  // whole, so each says what the badge means in both places. The second half of
  // each is the run verdict's own definition, straight out of
  // `run_finalizer._mapping` — see `outputAssurance` below.
  Verified:
    "Checked against its source by a verifier, and the receipt for that check is on file. On the answer, the Run recorded that every claim in it was.",
  Grounded:
    "Tied to one named piece of evidence, and the receipt for that link is on file. On the answer, the Run recorded that every claim in it was tied to evidence, without every one also carrying a verifier's check.",
  Refused: "The completed Run answered by refusing, and recorded that refusal itself.",
  // THE DISCLOSURE IS THE REASON, NOT THE RECORD. This badge exists because the
  // absence bucket below used to swallow `withheld` too, and told a reader
  // whose access had narrowed that the platform had never recorded the check.
  // It is the one state whose blankness is about what this READ got rather than
  // about what was recorded — and that is as far as the wire goes. Rule 2 above
  // enumerates the five conditions that reach it; only one of them is a
  // decision about access, so the sentence offers both readings and picks
  // neither. Naming one made this the sixth instance of the same class, with
  // the sign flipped: the round-2 fix over-claimed in the direction it had just
  // been corrected away from.
  Withheld:
    "The server recorded this and did not release it on this read: either your access does not reach it, or the server could not complete the check that would release it. That is not a gap in the record — so how it was checked is not shown here.",
  // NOT A VERDICT. This badge is the absence of a finding, not a finding of
  // absence, and it used to read "Unavailable" — which a reader on the answer
  // screen took to mean the ANSWER was unavailable, next to an answer they were
  // looking straight at. The replacement over-corrected in the other direction:
  // "Nothing on file says how this was checked" is a claim about what the
  // SERVER holds, and it was false wherever this console simply declined to
  // badge something the server had recorded — a completed VERIFIED answer, or a
  // claim whose verdict arrived without the receipt this console asks for. The
  // sentence is now scoped to what this console can say, which is the only
  // thing this module knows.
  "Not established":
    "This console cannot say how this was checked: the facts it would need are not all here. That is not a finding against it — it is the absence of one.",
};

type Claim = AgentEvidenceEnvelopeWire["claims"][number];
type Evidence = AgentEvidenceEnvelopeWire["evidence"][number];

/**
 * WHAT AN EVIDENCE ROW'S BADGE SAYS — OR `null` WHEN IT HAS NOTHING TO SAY.
 *
 * `null` is the point of this signature. The drawer badges only rows that can
 * be opened, so for one round this function's non-available arms were computed
 * and never drawn: the fix "withheld is not absent" landed on claims and missed
 * the evidence row it was also written for, and a test pinned a "Withheld" that
 * no surface rendered. Returning a label the caller may or may not draw is what
 * let those two drift apart, so the decision lives here now and the caller draws
 * exactly what it is handed.
 *
 * The rule the `null` encodes: a badge must carry a FACT, and on an evidence row
 * only two states carry one. `withheld` says the server holds this and did not
 * release it on this read — not WHY, see rule 2 at the head of this file — and
 * it is worth its own word precisely because the row's own sentence reads like a
 * gap. `available` carries the producer's assurance. Every other state is an
 * absence, and its row already says so in one sentence; a badge repeating that
 * absence in badge-words is the same non-answer twice, on the row with the least
 * to say.
 *
 * ⚠️ THE BADGE COMES FROM `state`, NEVER FROM `assurance`. A withheld row is
 * projected with `assurance="unavailable"` (`evidence_projection.py`), so
 * reading the assurance field first would badge a record the server holds as an
 * empty one and send the reader to the grounding team over a record that is
 * intact. `available` with an assurance value this build has never seen is the
 * one place the quiet badge still belongs: the row is disclosed and full of
 * detail, so a missing badge there would read as a missing answer.
 */
export function evidenceAssurance(evidence: Evidence): AssuranceLabel | null {
  if (evidence.state === "withheld") return "Withheld";
  if (evidence.state !== "available") return null;
  if (evidence.assurance === "direct") return "Direct";
  if (evidence.assurance === "inferred") return "Inferred";
  if (evidence.assurance === "extracted") return "Extracted";
  return "Not established";
}

export function claimAssurance(claim: Claim): AssuranceLabel {
  if (claim.state === "withheld") return "Withheld";
  if (claim.state !== "available") return "Not established";
  if (claim.claim_verdict === "verified" && claim.verifier_event_id && claim.verifier_receipt_hash)
    return "Verified";
  if (claim.claim_verdict === "grounded" && claim.evidence_id && claim.association_hash)
    return "Grounded";
  return "Not established";
}

/**
 * HOW THE ANSWER AS A WHOLE WAS CHECKED.
 *
 * `output.verdict` is a server-authored fact about this Run, not a decoration.
 * `run_finalizer._mapping` writes it from the Run's own claim and verifier rows
 * at finalization: `verified` when every claim carried a verifier receipt,
 * `grounded` when every claim was tied to evidence without all of them being
 * verified, `refused` for a refusal, and `unavailable` when the claim mapping
 * was empty or only partial. Nothing but a completed Run can carry the first
 * three — `run_workflow` rejects the write — so the status check below is
 * belt-and-braces against a wire that contradicts itself, not the thing doing
 * the work.
 *
 * This function used to badge everything except `refused` as "Not established".
 * So the good case — a completed, VERIFIED answer, the one a buyer is reading
 * on screen — was labelled as having no established check, and the tooltip told
 * them the record held nothing while the record in front of them said
 * `verified`. It reports the verdict now.
 *
 * It deliberately does NOT re-derive that verdict from the claims in this
 * envelope. Those are projected per reader: a claim this reader may not see
 * arrives `withheld`, so re-deriving would quietly downgrade a verified answer
 * to "Not established" for exactly the reader whose access is narrower — the
 * same false statement, moved somewhere harder to see. The verdict is the
 * server's; the claims are alongside it for anyone who wants to read behind it.
 *
 * AN `aggregateAssurance` USED TO SIT BELOW THIS ONE and did exactly that
 * re-derivation, folding the claim labels into one. It had no caller in `src`
 * before this branch and none after, and the paragraph above is the reason it
 * should never get one: a per-reader aggregate contradicts a server verdict for
 * the narrowest reader. Deleted rather than left as a trap. The one idea worth
 * keeping out of it, recorded here so it is not re-derived from scratch: if a
 * surface ever must fold per-claim labels, `withheld` should survive the fold
 * only when it is the ONLY thing standing between the answer and a badge — a
 * run that also holds a claim this console could not establish is a run with a
 * gap, and one withheld claim does not get to explain the rest.
 */
export function outputAssurance(envelope: AgentEvidenceEnvelopeWire): AssuranceLabel {
  if (envelope.run_status !== "completed") return "Not established";
  if (envelope.output.verdict === "refused") return "Refused";
  if (envelope.output.verdict === "verified") return "Verified";
  if (envelope.output.verdict === "grounded") return "Grounded";
  return "Not established";
}

/** One value as the drawer draws it: a short label on screen, the whole sentence
 * in the tooltip. The same shape the conversation ledger uses for its own stored
 * vocabulary, so the two screens read alike. */
export interface WireTerm {
  label: string;
  gloss: string;
}

/** The shared fallback — rule 2 at the head of this file, in one place. */
function unrecognised(kind: string, value: string): WireTerm {
  return {
    label: "Not recognised",
    gloss: `This console does not know the ${kind} “${value}” that the server sent.`,
  };
}

/**
 * WHAT AN EVIDENCE ROW SAYS ABOUT ITSELF.
 *
 * The server's own literals for `available` and `withheld` already read as
 * English, so they pass through untouched — restating them here would only
 * create a second place for the wording to drift from. (`withheld`'s literal,
 * "Evidence withheld — access changed", names a cause the projection does not
 * establish — see rule 2 at the head of this file — but it is the SERVER's
 * sentence about its own read, said the same way on five other surfaces, and
 * rewriting it here would leave this console saying two different things about
 * one wire. The badge beside it is this module's own sentence and is where the
 * over-claim was removed.) `locator_unavailable` is the one arm that had to
 * change, and for a different reason: "Evidence locator unavailable" names an
 * internal mechanism, and it is the one arm whose row carries nothing else at
 * all, so it is the only sentence its reader gets.
 *
 * The cause is a backend gap, tracked separately. This sentence does not
 * diagnose it and does not promise a fix. It says the two things a reader can
 * actually act on: there is nothing to open here, and nothing about the record
 * itself is known from this page. It does NOT say the evidence is missing —
 * the server holds a record; what it could not produce is the way back to it.
 *
 * An unrecognised state falls through to the server's own message, which is the
 * only honest thing left to show for an arm this build has never seen.
 */
const EVIDENCE_ROW_REWRITES: Record<string, string> = {
  locator_unavailable:
    "This Run recorded a piece of evidence here, but the system could not resolve where it came from. There is nothing to open, and nothing more about it is known from this page.",
};

// ⚠️ EVERY LOOKUP BELOW GOES THROUGH `hasOwn`, AND `??` ALONE IS NOT ENOUGH.
// `MAP["constructor"]` is Object's constructor FUNCTION and `MAP["__proto__"]`
// is `Object.prototype` — both NON-nullish, so `??` passes them straight
// through, out of a function typed to return a string. React logs an error for
// the function and THROWS "Objects are not valid as a React child" for the
// object, taking the pane down. (`toString`/`valueOf` do NOT reach: these keys
// arrive from closed server enums and the sibling map lowercases its key, while
// those members are camelCase. Only the two all-lowercase members are
// reachable — which is exactly enough to matter.)
function fromVocabulary<T>(map: Record<string, T>, key: string): T | undefined {
  return Object.hasOwn(map, key) ? map[key] : undefined;
}

export function evidenceRowSentence(state: string, serverMessage: string): string {
  return fromVocabulary(EVIDENCE_ROW_REWRITES, state) ?? serverMessage;
}

/**
 * WHETHER OPENING THE ROW CAN REVEAL ANYTHING.
 *
 * Every piece of row detail in the drawer — the row anchor, the graph path, the
 * served document versions, the Graph link — is gated on `state === "available"`,
 * so a row in any other state has nothing behind it, and an unrecognised state
 * has nothing this build knows how to draw. Both used to render a button anyway:
 * focusable, in the Tab cycle, aria-pressed, and going nowhere.
 */
export function evidenceRowCanOpen(state: string): boolean {
  return state === "available";
}

/** The per-hop review outcome. No producer persists one yet (`evidence_wire.py`
 * projects null and says why), so these sentences are drawn from the enum's own
 * words and claim nothing beyond them. */
const GRAPH_HOP_REVIEW: Record<string, WireTerm> = {
  accepted: {
    label: "Reviewed and accepted",
    gloss: "A reviewer looked at this connection and accepted it.",
  },
  pending: {
    label: "Waiting for review",
    gloss: "This connection has been recorded but not yet reviewed.",
  },
  rejected: {
    label: "Reviewed and rejected",
    gloss: "A reviewer looked at this connection and rejected it.",
  },
  not_applicable: {
    label: "No review needed",
    gloss: "This connection is not one that review applies to.",
  },
};

export function graphHopReviewTerm(state: string): WireTerm {
  return fromVocabulary(GRAPH_HOP_REVIEW, state) ?? unrecognised("review state", state);
}

/**
 * THE GOVERNANCE PANEL'S OPENING LINE. "Governance partial" was the machine word
 * for it, and a reader cannot tell from it whether that is normal or an alarm.
 *
 * ⚠️ FOURTH INSTANCE OF ONE SHAPE ON THIS BRANCH: THE CONSOLE DESCRIBING THE
 * SERVER'S RECORDS INSTEAD OF ITS OWN READING OF THEM. The answer badge glossed
 * a completed VERIFIED run as nothing-on-file; then `withheld` — a record the
 * server HOLDS — was collapsed into that same absence; then `unavailable` was read
 * as "no facts" when the projection sets it from the audit receipts alone; and
 * now these two sentences, which called a JOIN THAT DID NOT RESOLVE a thing that
 * "was never recorded", and called six joins resolving "every governance fact".
 * A future author needs only the one rule, and it is the same rule every time:
 * this module may say what the envelope says did not resolve, and may not say
 * what the server holds — the envelope never tells it that.
 *
 * Read `governance_projection.project_governance`. SIX producer joins each
 * append their own `*_join_unavailable` token when the field comes back None —
 * delegation, service identity, credential owner, SCOPE, POLICY, and tool
 * tier/decision — a seventh fires when no audit receipt matched, and
 * `status = "partial"` whenever a receipt matched and any of the six did not.
 * The tokens say `unavailable`, about a join: none of them is a claim that the
 * server never wrote the fact down.
 *
 * `unavailable` is the receipts alone and takes precedence over every join
 * reason, so an envelope routinely arrives `unavailable` while carrying a
 * delegated user, a service identity, a credential owner and a tool decision —
 * which is why that sentence names the receipt and stops there.
 *
 * The `partial` sentence PROMISES A LIST, and `governanceGapNames` below is what
 * keeps the promise: `partial` cannot fire with an empty reason set, and every
 * reason it can carry has a name. Any surface printing this sentence has to draw
 * those names beside it, or it is back to telling a reader something is missing
 * and refusing to say what.
 */
const GOVERNANCE_STATUS: Record<string, string> = {
  complete: "Every governance lookup for this Run resolved, and an audit receipt is tied to it.",
  partial: "Some governance lookups for this Run did not resolve; those are named below.",
  unavailable:
    "No audit receipt could be tied to this Run, so nothing here can be checked against the audit log.",
};

export function governanceStatusSentence(status: string): string {
  return (
    fromVocabulary(GOVERNANCE_STATUS, status) ??
    `This console does not know the governance status “${status}” that the server sent.`
  );
}

type Governance = AgentEvidenceEnvelopeWire["governance"];

/** One producer join, in the words the panel uses for the fact it carries. */
const GOVERNANCE_LOOKUP_NAMES: Record<string, string> = {
  delegation_join_unavailable: "delegated user",
  service_identity_join_unavailable: "agent service",
  credential_owner_join_unavailable: "credential owner",
  scope_join_unavailable: "effective scopes",
  policy_join_unavailable: "policy environment",
};

/**
 * WHICH GOVERNANCE LOOKUPS DID NOT RESOLVE, NAMED.
 *
 * ⚠️ THIS READS `unavailable_reasons`, NOT THE FIELDS ON SCREEN, and that
 * difference is the whole P1. The drawer draws FOUR facts; the projection joins
 * SIX, and `partial` fires on any of them. So a Run whose four drawn facts all
 * joined, and whose policy join did not, arrived `partial` — and the panel said
 * part of the record was absent, filled every row it draws, and then named
 * nothing at all, because it worked the gap line out from the four fields it
 * happened to render. The reasons are the projection's own account of itself:
 * complete, closed, and the only place two of the six gaps are visible.
 *
 * `effective_scopes` is the sharpest case. It is projected as
 * `tuple(sorted(set(facts.effective_scopes or ())))`, so an unresolved scope
 * join reaches this console as `[]` — byte-identical to a join that resolved to
 * no scopes. The field cannot tell those apart. The token can, and does.
 *
 * The audit receipt is deliberately absent from this list. It is not a producer
 * join, it is the entire subject of the `unavailable` sentence above, and it can
 * only ever appear on that status (`project_governance` appends it exactly when
 * `not valid`, which is exactly when the status is `unavailable`). Naming it
 * here as well would be one fact said twice, which is what the last round of
 * this fix was for.
 *
 * `tool_decision_join_unavailable` is the one token standing for two fields — it
 * fires when EITHER `tool_tier` or `tool_decision` is None — so it is split back
 * into the halves that are actually absent. Printing the whole token's name
 * would put "tool decision" in the gap line while the <dl> above it shows the
 * decision: the same over-reach with the sign flipped. If a wire ever contradicts
 * itself by sending the token with both halves present, the token's own name is
 * used, because saying something imprecise beats the silence this fix exists to
 * end.
 */
export function governanceGapNames(governance: Governance): string[] {
  const names: string[] = [];
  for (const reason of governance.unavailable_reasons) {
    if (reason === "audit_receipt_join_unavailable") continue;
    if (reason === "tool_decision_join_unavailable") {
      const halves: string[] = [];
      if (governance.tool_decision === null) halves.push("tool decision");
      if (governance.tool_tier === null) halves.push("tool tier");
      names.push(...(halves.length ? halves : ["tool decision or tier"]));
      continue;
    }
    names.push(
      fromVocabulary(GOVERNANCE_LOOKUP_NAMES, reason) ?? `an unrecognised lookup (${reason})`,
    );
  }
  return names;
}

/** What policy decided about the tools this Run could reach. */
const TOOL_DECISION: Record<string, string> = {
  allow: "Allowed",
  deny: "Denied",
  approval_required: "Approval required first",
};

export function toolDecisionLabel(decision: string): string {
  // The raw value rides inside the fallback rather than in a tooltip: this one
  // renders into a definition list beside three other short values, where a
  // reader can see it without hovering.
  return fromVocabulary(TOOL_DECISION, decision) ?? `Not recognised (${decision})`;
}
