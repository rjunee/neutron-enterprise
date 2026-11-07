// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { AgentEvidenceEnvelopeWire } from "../api/types";
import {
  ASSURANCE_EXPLANATIONS,
  claimAssurance,
  evidenceAssurance,
  evidenceRowCanOpen,
  evidenceRowSentence,
  governanceGapNames,
  governanceStatusSentence,
  graphHopReviewTerm,
  outputAssurance,
  toolDecisionLabel,
} from "./assuranceVocabulary";

const claim = {
  claim_id: "c",
  claim_verdict: "grounded" as const,
  state: "available" as const,
  message: "Verified Direct Refused",
  display: "Verified Direct Refused",
  evidence_id: "e",
  association_hash: "a",
  verifier_event_id: null,
  verifier_receipt_hash: null,
};
const evidence = {
  evidence_id: "e",
  state: "available" as const,
  message: "Evidence available",
  assurance: "direct" as const,
  locator: null,
};
const envelope = {
  schema_version: 1 as const,
  agent_id: "a",
  run_id: "r",
  run_status: "completed" as const,
  mapping_state: "complete" as const,
  mapping_message: "Claim mapping complete",
  output: {
    verdict: "refused" as const,
    state: "unavailable" as const,
    message: "Answer unavailable",
    display: null,
  },
  claims: [claim],
  evidence: [evidence],
  governance: {
    status: "unavailable" as const,
    delegated_user_label: null,
    agent_service_identity: null,
    connector_credential_owner_label: null,
    effective_scopes: [],
    policy_environment: null,
    tool_tier: null,
    tool_decision: null,
    // Every join came back None and no receipt matched, so `project_governance`
    // emits all seven tokens. An empty list beside seven nulls is a wire the
    // projection cannot produce, and fixtures that cannot happen hide bugs.
    unavailable_reasons: [
      "delegation_join_unavailable",
      "service_identity_join_unavailable",
      "credential_owner_join_unavailable",
      "scope_join_unavailable",
      "policy_join_unavailable",
      "tool_decision_join_unavailable",
      "audit_receipt_join_unavailable",
    ],
  },
} satisfies AgentEvidenceEnvelopeWire;

describe("PRE-015D assurance vocabulary", () => {
  it.each([
    ["direct", "Direct"],
    ["inferred", "Inferred"],
    ["extracted", "Extracted"],
  ] as const)("maps available %s evidence only", (assurance, label) => {
    expect(evidenceAssurance({ ...evidence, assurance })).toBe(label);
    // The state gates the badge: an assurance value on a row the server did not
    // disclose is not this row's assurance to report. What happens next depends
    // on WHY, and the pair below is the whole point — one state carries a fact
    // worth a badge, the other carries none and asks for no badge at all.
    expect(evidenceAssurance({ ...evidence, assurance, state: "locator_unavailable" })).toBeNull();
    expect(evidenceAssurance({ ...evidence, assurance, state: "withheld" })).toBe("Withheld");
  });
  it("requires exact claim receipt pairs and ignores adversarial prose", () => {
    expect(claimAssurance(claim)).toBe("Grounded");
    expect(claimAssurance({ ...claim, association_hash: null })).toBe("Not established");
    expect(
      claimAssurance({
        ...claim,
        claim_verdict: "verified",
        verifier_event_id: "v",
        verifier_receipt_hash: "h",
      }),
    ).toBe("Verified");
    expect(claimAssurance({ ...claim, claim_verdict: "verified" })).toBe("Not established");
  });
  it("refuses only completed refused output", () => {
    expect(outputAssurance(envelope)).toBe("Refused");
    expect(outputAssurance({ ...envelope, run_status: "failed" })).toBe("Not established");
  });

  it("the Withheld gloss says the record is HELD without saying why it was not released", () => {
    // `evidence_projection.py` projects `state="withheld"` (:1171) whenever the
    // locator resolution came back anything but `authorized`, and five distinct
    // conditions reach that: a PERMANENT_WITHHELD source stamp (:298), a
    // RETRYABLE_UNKNOWN one (:296), an evaluator or authenticated-context read
    // that raised (:294), a context whose principal or hash did not match or a
    // payload with no stamps (:291), and a store that could not load the
    // retained locator (:352). `claimAssurance` inherits all five through the
    // `evidence_withheld` cause (:1310, :891). So a badge that names ONE of
    // them is false on the other four — and the one it named sent a reader
    // whose source-authority service was briefly unreachable to their access
    // administrator over a platform outage.
    const gloss = ASSURANCE_EXPLANATIONS.Withheld;
    expect(gloss).not.toContain("decision about your access");
    expect(gloss).toContain("either your access does not reach it");
    expect(gloss).toContain("could not complete the check");

    // AND THE ROUND-2 GAIN IS NOT SPENT BUYING IT. "Withheld" still has to read
    // as a record the server HOLDS, or this badge is back in the absence bucket
    // that sent a reader whose access had narrowed to the grounding team.
    expect(gloss).toContain("The server recorded this");
    expect(gloss).toContain("not a gap in the record");
    expect(gloss).not.toBe(ASSURANCE_EXPLANATIONS["Not established"]);
    expect(ASSURANCE_EXPLANATIONS["Not established"]).not.toContain("The server recorded this");
  });
});

describe("the drawer's wire vocabulary", () => {
  it("rewrites the one row sentence that named a mechanism, and passes the rest through", () => {
    // "Evidence locator unavailable" was the BUTTON LABEL of a row that carried
    // nothing else. The other two server literals are already sentences a reader
    // can use, so restating them here would only add a place to drift from.
    const rewritten = evidenceRowSentence("locator_unavailable", "Evidence locator unavailable");
    expect(rewritten).not.toContain("locator");
    expect(rewritten).toContain("could not resolve where it came from");
    expect(rewritten).toContain("nothing to open");
    expect(evidenceRowSentence("available", "Evidence available")).toBe("Evidence available");
    expect(evidenceRowSentence("withheld", "Evidence withheld — access changed")).toBe(
      "Evidence withheld — access changed",
    );
  });

  it("falls back to the server's own sentence for a state this build has never seen", () => {
    expect(evidenceRowSentence("some_future_arm", "Evidence quarantined")).toBe(
      "Evidence quarantined",
    );
  });

  it("opens only an available row — every other state, known or not, has nothing behind it", () => {
    expect(evidenceRowCanOpen("available")).toBe(true);
    expect(evidenceRowCanOpen("withheld")).toBe(false);
    expect(evidenceRowCanOpen("locator_unavailable")).toBe(false);
    expect(evidenceRowCanOpen("some_future_arm")).toBe(false);
  });

  it("says each hop review outcome in words, and admits an unknown one by name", () => {
    expect(graphHopReviewTerm("not_applicable").label).toBe("No review needed");
    expect(graphHopReviewTerm("accepted").label).toBe("Reviewed and accepted");
    expect(graphHopReviewTerm("pending").label).toBe("Waiting for review");
    expect(graphHopReviewTerm("rejected").label).toBe("Reviewed and rejected");
    const unknown = graphHopReviewTerm("escalated");
    expect(unknown.label).toBe("Not recognised");
    // The raw value survives in the tooltip: unreadable on screen is not the
    // same as unavailable to an operator.
    expect(unknown.gloss).toContain("escalated");
  });

  it("says `partial` is about joins that did not resolve, never about what was recorded", () => {
    // `project_governance` builds every reason token from a join coming back
    // None — `scope_join_unavailable`, `policy_join_unavailable` and the rest.
    // "The rest were never recorded" turned that into a claim about the
    // SERVER'S storage, which is the one claim this module cannot make.
    const sentence = governanceStatusSentence("partial");
    expect(sentence).toContain("did not resolve");
    expect(sentence).toContain("named below");
    expect(sentence).not.toMatch(/never recorded|on file/i);
  });

  it("says `complete` means six joins and a receipt, not that the record is whole", () => {
    // "Every governance fact for this Run is on file" claimed two things the
    // projection never says: that the server holds every governance fact there
    // is, and that this console knows what it holds. `complete` means the six
    // joins in `ExactGovernanceJoins` resolved and a receipt matched.
    const sentence = governanceStatusSentence("complete");
    expect(sentence).toContain("resolved");
    expect(sentence).toContain("audit receipt");
    expect(sentence).not.toMatch(/on file/i);
    expect(sentence).not.toMatch(/every governance fact/i);
  });

  it("still admits a governance status this build does not know, by name", () => {
    expect(governanceStatusSentence("degraded")).toContain("degraded");
    expect(governanceStatusSentence("degraded")).toContain("does not know");
  });

  it("says `unavailable` names a missing AUDIT RECEIPT, never missing facts", () => {
    // `governance_projection.project_governance` sets this status from the audit
    // receipts alone — `status = "unavailable" if not valid` — and projects the
    // four producer-authored facts regardless, so an envelope arrives
    // `unavailable` WITH a delegated user, a service identity, a credential
    // owner and a tool decision. The sentence that used to sit here read "No
    // governance facts are on file for this Run." and the drawer printed it
    // directly above them.
    const sentence = governanceStatusSentence("unavailable");
    expect(sentence).toContain("No audit receipt could be tied to this Run");
    expect(sentence).toContain("audit log");
    expect(sentence).not.toMatch(/governance fact/i);
    expect(sentence).not.toMatch(/on file/i);
  });

  it("names the tool decision plainly and keeps an unknown one legible", () => {
    expect(toolDecisionLabel("allow")).toBe("Allowed");
    expect(toolDecisionLabel("deny")).toBe("Denied");
    expect(toolDecisionLabel("approval_required")).toBe("Approval required first");
    expect(toolDecisionLabel("quarantine")).toBe("Not recognised (quarantine)");
  });
});

describe("a silent badge says WHY it is silent", () => {
  // Two P1s in one sentence. The badge column had a single quiet arm,
  // "Not established", glossed "Nothing on file says how this was checked" —
  // and it was worn by a completed VERIFIED answer and by a claim the server
  // was deliberately withholding. Both readings were false, and the second one
  // sends the reader to the wrong team.
  const verifiedAnswer = {
    ...envelope,
    output: { ...envelope.output, verdict: "verified" as const },
  };
  const withheldClaim = { ...claim, state: "withheld" as const, display: null };
  const absentClaim = { ...claim, state: "unavailable" as const, display: null };

  it("badges a completed verified answer, and never says it has nothing on file", () => {
    const label = outputAssurance(verifiedAnswer);
    expect(label).toBe("Verified");
    // The sentence the reader actually hovers, not just the word on the badge.
    expect(ASSURANCE_EXPLANATIONS[label]).toContain("verifier");
    expect(ASSURANCE_EXPLANATIONS[label]).not.toMatch(/nothing/i);
  });

  it("badges a completed grounded answer the same way", () => {
    const label = outputAssurance({
      ...envelope,
      output: { ...envelope.output, verdict: "grounded" as const },
    });
    expect(label).toBe("Grounded");
    expect(ASSURANCE_EXPLANATIONS[label]).not.toMatch(/nothing/i);
  });

  it("keeps the quiet badge for a verdict that establishes nothing, and for a Run still going", () => {
    expect(
      outputAssurance({ ...envelope, output: { ...envelope.output, verdict: "unavailable" } }),
    ).toBe("Not established");
    expect(outputAssurance({ ...envelope, output: { ...envelope.output, verdict: null } })).toBe(
      "Not established",
    );
    // Belt-and-braces: the server cannot write a verdict onto a Run that has
    // not completed, so this arm only fires on a wire contradicting itself.
    expect(outputAssurance({ ...verifiedAnswer, run_status: "running" })).toBe("Not established");
  });

  it("pins that withheld is not absent — for a claim and for an evidence row", () => {
    expect(claimAssurance(withheldClaim)).toBe("Withheld");
    expect(claimAssurance(absentClaim)).toBe("Not established");
    expect(claimAssurance(withheldClaim)).not.toBe(claimAssurance(absentClaim));
    expect(evidenceAssurance({ ...evidence, state: "withheld" })).toBe("Withheld");
    // Withheld names a non-disclosure and refuses to describe the record as
    // empty; the quiet badge is about this console, never about the server.
    // WHY it was not disclosed is not this badge's to say — see the gloss test
    // above, and rule 2 at the head of the module.
    expect(ASSURANCE_EXPLANATIONS.Withheld).toContain("did not release it on this read");
    expect(ASSURANCE_EXPLANATIONS.Withheld).not.toMatch(/nothing|absence/i);
    expect(ASSURANCE_EXPLANATIONS["Not established"]).toContain("This console cannot say");
    expect(ASSURANCE_EXPLANATIONS["Not established"]).not.toMatch(/on file/i);
  });
});

/** One governance envelope, overridden per case. `satisfies` above keeps the
 * base honest; these overrides are what a real projection would have sent. */
function gov(
  over: Partial<AgentEvidenceEnvelopeWire["governance"]> = {},
): AgentEvidenceEnvelopeWire["governance"] {
  return { ...envelope.governance, ...over };
}

describe("the gap line names every join the projection says did not resolve", () => {
  // ⚠️ THE P1, IN ONE SENTENCE: `project_governance` joins SIX facts and sets
  // `partial` when ANY of them did not resolve; the drawer displays FOUR. The
  // gap line used to be derived from those four fields' nullness, so a Run with
  // all four present and `policy_environment is None` printed "some are
  // missing" and named nothing. These reasons are the projection's own account,
  // and they cover all six.
  const JOIN_REASONS = [
    ["delegation_join_unavailable", "delegated user"],
    ["service_identity_join_unavailable", "agent service"],
    ["credential_owner_join_unavailable", "credential owner"],
    ["scope_join_unavailable", "effective scopes"],
    ["policy_join_unavailable", "policy environment"],
  ] as const;

  it.each(JOIN_REASONS)("names %s in the reader's words", (reason, name) => {
    expect(governanceGapNames(gov({ status: "partial", unavailable_reasons: [reason] }))).toEqual([
      name,
    ]);
  });

  it("never leaves a `partial` Run told something is missing with nothing named", () => {
    // The invariant the `partial` sentence's "named below" rests on: every
    // reason token a `partial` envelope can carry produces at least one name.
    // Every displayed fact is PRESENT here on purpose — that is the envelope
    // the old field-derived gap line went silent on.
    const displayed = {
      status: "partial" as const,
      delegated_user_label: "Owner",
      agent_service_identity: "svc",
      connector_credential_owner_label: "Ops",
      tool_decision: "allow" as const,
    };
    for (const [reason] of JOIN_REASONS) {
      expect(
        governanceGapNames(gov({ ...displayed, unavailable_reasons: [reason] })).length,
      ).toBeGreaterThan(0);
    }
    expect(
      governanceGapNames(
        gov({ ...displayed, unavailable_reasons: ["tool_decision_join_unavailable"] }),
      ),
    ).toEqual(["tool tier"]);
  });

  it("names an unresolved scope join that the field alone cannot show", () => {
    // `effective_scopes` is projected as `tuple(sorted(set(... or ())))`, so an
    // unresolved join and a join that found no scopes are both `[]` here. Only
    // the token separates them, which is why this function reads the token.
    expect(
      governanceGapNames(
        gov({
          status: "partial",
          effective_scopes: [],
          unavailable_reasons: ["scope_join_unavailable"],
        }),
      ),
    ).toEqual(["effective scopes"]);
    expect(
      governanceGapNames(
        gov({ status: "complete", effective_scopes: [], unavailable_reasons: [] }),
      ),
    ).toEqual([]);
  });

  it("splits the one token that stands for two fields into the halves actually absent", () => {
    // `tool_decision_join_unavailable` fires when EITHER `tool_tier` or
    // `tool_decision` is None. Naming the whole token would print "tool
    // decision" in the gap line while the <dl> renders the decision beside it.
    const reasons = ["tool_decision_join_unavailable"] as const;
    expect(
      governanceGapNames(
        gov({ tool_decision: "allow", tool_tier: null, unavailable_reasons: [...reasons] }),
      ),
    ).toEqual(["tool tier"]);
    expect(
      governanceGapNames(
        gov({ tool_decision: null, tool_tier: 2, unavailable_reasons: [...reasons] }),
      ),
    ).toEqual(["tool decision"]);
    expect(
      governanceGapNames(
        gov({ tool_decision: null, tool_tier: null, unavailable_reasons: [...reasons] }),
      ),
    ).toEqual(["tool decision", "tool tier"]);
    // A wire contradicting itself still says something rather than nothing.
    expect(
      governanceGapNames(
        gov({ tool_decision: "deny", tool_tier: 1, unavailable_reasons: [...reasons] }),
      ),
    ).toEqual(["tool decision or tier"]);
  });

  it("leaves the audit receipt to the opening sentence, and says it once", () => {
    // `project_governance` appends this token exactly when `not valid`, which is
    // exactly when the status is `unavailable` — the status whose sentence is
    // entirely about the receipt. Repeating it here is one fact said twice.
    expect(
      governanceGapNames(
        gov({ status: "unavailable", unavailable_reasons: ["audit_receipt_join_unavailable"] }),
      ),
    ).toEqual([]);
    expect(
      governanceGapNames(
        gov({
          status: "unavailable",
          unavailable_reasons: ["policy_join_unavailable", "audit_receipt_join_unavailable"],
        }),
      ),
    ).toEqual(["policy environment"]);
  });

  it("admits a reason token this build has never seen, carrying its raw value", () => {
    const [name] = governanceGapNames(
      gov({ status: "partial", unavailable_reasons: ["residency_join_unavailable" as never] }),
    );
    expect(name).toContain("unrecognised");
    expect(name).toContain("residency_join_unavailable");
  });
});

describe("a prototype key is not a label", () => {
  // ⚠️ `MAP[k] ?? fallback` looks safe and is not: `MAP["constructor"]` is
  // Object's constructor FUNCTION and `MAP["__proto__"]` is `Object.prototype`,
  // both NON-nullish, so `??` never fires and a function or an object leaves a
  // string-typed function. React logs an error for the first and THROWS
  // "Objects are not valid as a React child" for the second — the pane dies.
  // These enums are server-authored and closed today; the guard is here because
  // "closed today" is not a property this console can enforce.
  it.each(["constructor", "__proto__", "toString", "valueOf", "hasOwnProperty"])(
    "renders a string for the prototype key %s, never a function or an object",
    (key) => {
      for (const actual of [
        evidenceRowSentence(key as never, "server said this"),
        governanceStatusSentence(key),
        governanceGapNames(gov({ unavailable_reasons: [key] as never }))[0] ?? "",
        toolDecisionLabel(key),
        graphHopReviewTerm(key).label,
      ]) {
        expect(typeof actual).toBe("string");
        expect(actual.trim()).not.toBe("");
      }
    },
  );
});
