// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE MAPPER'S ONE OBLIGATION, held from both sides.
//
// It must surface everything the Run recorded — that is the whole point, and a
// turn that did eighteen things and drew none of them is the defect this file
// exists to prevent. It must ALSO surface nothing the Run did not record: an
// invented step is worse than a blank screen, because a blank screen at least
// does not lie about what a governed system did.
//
// Every test below therefore pairs a "shows the work" assertion with a "and
// invents nothing" assertion over the same input.

import { describe, expect, it } from "vitest";
import type { AgentRunDetailWire, AgentRunEventWire } from "../api/types";
import { eventMetadata, runActivitySteps, runPlanAbsentNote, runTurnSteps } from "./runActivity";
import { bandStepCount } from "../transcript";
import {
  RUN_PLAN_UNAVAILABLE,
  RUN_STEP_ACCEPTED,
  RUN_STEP_CLAIM_CHECK,
  RUN_STEP_DELIVERY,
  RUN_STEP_EVIDENCE,
  RUN_STEP_EVIDENCE_ROLLUP_LABEL,
  RUN_STEP_EVIDENCE_ROW,
  RUN_STEP_EXECUTION,
  RUN_STEP_EXECUTION_RECOVERED,
  RUN_STEP_EXECUTION_STARTED,
  RUN_STEP_FAILED,
  RUN_STEP_MODEL_CALL,
  RUN_STEP_MODEL_CALL_NOTE,
  RUN_STEP_NOT_DISCLOSABLE,
  RUN_STEP_NO_RECORDS,
  RUN_STEP_RECORDED,
  RUN_STEP_RECOVERED,
  RUN_STEP_REQUEST_ACCEPTED,
  RUN_STEP_SERVED,
  RUN_STEP_STARTED,
  RUN_STEP_TOOL_CALL,
  RUN_STEP_UNNAMED,
  RUN_STEP_VERIFIED,
  RUN_STEP_WITHHELD,
  runEvidenceEmptyReadsLabel,
  runEvidenceRetrievedName,
  runEvidenceVerifiedLabel,
  runFoldedStepsName,
} from "./askCopy";

function event(over: Partial<AgentRunEventWire> = {}): AgentRunEventWire {
  return {
    event_id: "evt-1",
    run_id: "run-1",
    attempt_id: "attempt-1",
    sequence: 1,
    event_kind: "model.served",
    event_status: "served",
    parent_event_id: null,
    step_id: null,
    metadata: [],
    detail_hash: "c".repeat(64),
    created_at: "2026-08-27T12:00:00Z",
    ...over,
  };
}

function run(over: Partial<AgentRunDetailWire> = {}): AgentRunDetailWire {
  return {
    run_id: "run-1",
    conversation_id: "conversation-1",
    agent_id: "agent-1",
    agent_version: 1,
    sequence: 1,
    version: 1,
    predecessor_run_id: null,
    root_run_id: "run-1",
    retry_ordinal: 0,
    display_label: "q",
    strategy: "agentic",
    status: "running",
    output_verdict: null,
    model_policy_id: null,
    model_request_mode: "auto",
    context_snapshot_hash: null,
    requested_model: null,
    requested_model_lane: null,
    served_model: null,
    route_position: "unavailable",
    fallback_from: null,
    output_preview: null,
    output_state: "unavailable",
    config_hash: "a".repeat(64),
    capability_snapshot_hash: "b".repeat(64),
    created_at: "2026-08-27T12:00:00Z",
    updated_at: "2026-08-27T12:00:00Z",
    terminal_at: null,
    approval_id: null,
    failure_reason: null,
    agentic_plan: null,
    execution_profile_receipt: null,
    skill_activation_receipt: null,
    claims: [],
    claim_mapping_state: "mapping_pending",
    claims_truncated: false,
    ...over,
  };
}

describe("runActivitySteps — the work the Run recorded", () => {
  it("MUTATION `no-steps-invented`: an empty event log produces an empty band", () => {
    // THE PAIRED HALF of every test below. A Run that has recorded nothing
    // renders nothing — no placeholder row, no "starting…", no step 0 of N.
    // Mutating the mapper to seed a first row fails here.
    expect(runActivitySteps([])).toEqual([]);
    expect(runTurnSteps(run(), [])).toEqual([]);
  });

  it("MUTATION `steps-shown`: one row per recorded model call, in sequence order", () => {
    const steps = runActivitySteps([
      event({ event_id: "e1", sequence: 1, step_id: "model:0" }),
      event({ event_id: "e2", sequence: 2, step_id: "model:1" }),
      event({ event_id: "e3", sequence: 3, step_id: "model:2" }),
    ]);
    expect(steps).toHaveLength(3);
    // The Run's own step identity rides in the aux slot, so a reader can tell
    // WHICH of eighteen calls a row is. Mutating the mapper to collapse or drop
    // rows fails here.
    expect(steps.map((s) => s.intent)).toEqual(["model:0", "model:1", "model:2"]);
    expect(steps.map((s) => s.key)).toEqual(["run-event:e1", "run-event:e2", "run-event:e3"]);
  });

  it("names the served model the server named, and never invents an alias", () => {
    const named = runActivitySteps([
      event({ metadata: [{ key: "served_model", value: "qwen3.6-35b-a3b" }] }),
    ]);
    expect(named[0]?.name).toBe("qwen3.6-35b-a3b");
    expect(named[0]?.verdictLabel).toBe(RUN_STEP_SERVED);

    // ABSENT STAYS ABSENT: with no alias served, the row says what KIND of step
    // it was and claims no model. It does not borrow the Run's `served_model`.
    expect(runActivitySteps([event()])[0]?.name).toBe(RUN_STEP_MODEL_CALL);
  });

  it("never fabricates a tier or a duration, because the events wire carries none", () => {
    const [step] = runActivitySteps([event({ step_id: "model:0" })]);
    // `null` renders the em dash. A 0 here would be a claim about the world —
    // "tier 0", "0.0s" — that the server never made.
    expect(step?.tier).toBeNull();
    expect(step?.durationSeconds).toBeNull();
  });

  it("renders evidence in plain words and delivery with the server's own word", () => {
    const steps = runActivitySteps([
      event({ event_id: "e1", event_kind: "evidence.produced", event_status: "preterminal" }),
      event({
        event_id: "e2",
        event_kind: "delivery.decided",
        event_status: "preterminal",
        metadata: [{ key: "decision", value: "disclosed" }],
      }),
    ]);
    // The evidence roll-up states the outcome plainly; the verbatim event row
    // (RUN_STEP_EVIDENCE · Produced) survives in the row's own disclosure.
    expect(steps[0]?.name).toBe(runEvidenceRetrievedName(1));
    expect(steps[0]?.detail?.[0]?.name).toBe(RUN_STEP_EVIDENCE);
    expect(steps[1]?.name).toBe(RUN_STEP_DELIVERY);
    // The decision word is the SERVER'S, not a generic one, when it served one.
    expect(steps[1]?.verdictLabel).toBe("disclosed");
  });

  it("MUTATION `withheld-renders-as-itself`: an undisclosable step is a row, not a gap", () => {
    // The projection clamps every kind outside its safe set to `event.
    // unavailable`. The step still HAPPENED, so it still draws — dropping it
    // would under-report the Run's work, which is the same lie as the blank
    // screen. What it must not do is claim to know what it was.
    const steps = runActivitySteps([
      event({ event_id: "e1", event_kind: "event.unavailable", event_status: "preterminal" }),
      event({ event_id: "e2", event_kind: "event.unavailable", event_status: "withheld" }),
    ]);
    expect(steps).toHaveLength(2);
    expect(steps[0]?.name).toBe(RUN_STEP_UNNAMED);
    expect(steps[0]?.verdictLabel).toBe(RUN_STEP_NOT_DISCLOSABLE);
    // Withheld keeps its OWN word: a governance outcome, stated as one.
    expect(steps[1]?.verdictLabel).toBe(RUN_STEP_WITHHELD);
  });

  it("draws no row for the terminal event, which the receipt already states", () => {
    const steps = runActivitySteps([
      event({ event_id: "e1", step_id: "model:0" }),
      event({ event_id: "e2", event_kind: "run.terminal", event_status: "completed" }),
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.key).toBe("run-event:e1");
  });

  it("nothing the mapper produces can promote out of the band", () => {
    // The gate card is reserved for a decision a HUMAN owes or a policy refusal
    // of an action. A Run event is neither, and a mapper that emitted `pending`
    // or `refused` would bury the turn in un-foldable cards while asserting a
    // refusal that never happened.
    const steps = runActivitySteps([
      event({ event_id: "e1" }),
      event({ event_id: "e2", event_kind: "event.unavailable", event_status: "withheld" }),
      event({ event_id: "e3", event_kind: "delivery.decided", event_status: "preterminal" }),
    ]);
    for (const step of steps) {
      expect(step.verdict).toBe("allowed");
    }
  });
});

describe("eventMetadata", () => {
  it("reads a served string and refuses to coerce anything else into a name", () => {
    const e = event({
      metadata: [
        { key: "served_model", value: "gpt-oss-120b" },
        { key: "claim_count", value: 4 },
        { key: "context_fit", value: true },
      ],
    });
    expect(eventMetadata(e, "served_model")).toBe("gpt-oss-120b");
    // A number or a boolean under one of these keys is not a name; printing it
    // as one would put a value on screen the server never meant as a label.
    expect(eventMetadata(e, "claim_count")).toBeNull();
    expect(eventMetadata(e, "context_fit")).toBeNull();
    expect(eventMetadata(e, "requested_model")).toBeNull();
  });
});

describe("runTurnSteps — both accounts the Run keeps", () => {
  it("MUTATION `tool-calls-shown`: the agentic plan's tool calls become rows", () => {
    // `agentic_plan` is PROGRESS, not a plan decided up front: ne-runtime
    // appends one row per tool call, committed before the call runs, and the
    // projection serves it at every status. It is the only place a tool NAME
    // appears, so a band built from the event log alone would show a
    // retrieval-heavy turn doing no retrievals.
    const steps = runTurnSteps(
      run({
        strategy: "agentic",
        agentic_plan: [
          {
            ordinal: 0,
            tool: "search.contracts",
            step_intent: "renewal terms",
            tool_call_id: null,
          },
          { ordinal: 1, tool: "read.document", step_intent: "clause 7", tool_call_id: null },
        ],
      }),
      [event({ event_id: "e1", step_id: "model:0" })],
    );
    expect(steps.map((s) => s.name)).toEqual([
      "search.contracts",
      "read.document",
      RUN_STEP_MODEL_CALL,
    ]);
    expect(steps[0]?.intent).toBe("renewal terms");
  });

  it("a Run whose plan went null still shows its events — thinner, never invented", () => {
    // The projection makes `agentic_plan` all-or-nothing on any ordinal gap.
    // That must cost the tool rows and nothing else: the model calls the event
    // log recorded are still real and still draw.
    const steps = runTurnSteps(run({ agentic_plan: null }), [event({ step_id: "model:0" })]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.intent).toBe("model:0");
  });
});

/* --------------------------------------------------------------------------
 * THE FOLD — fifteen rows reading "Step · Not disclosable" are not a progress
 * display.
 *
 * `event.unavailable` is ROUTINE, not rare: the projection clamps every
 * `evidence.verified_row` and `claim.verifier` to it, so a normal turn emits a
 * dozen consecutive rows whose entire content is two generic words. They are a
 * detail OF the work around them, not peers of it.
 *
 * Every test here is paired the way the rest of this file is: the compression
 * half against the honesty half. A count of repeated content-free records is a
 * fair compression; relabelling them, dropping them, hiding a `withheld`
 * record inside one, or letting the fold shrink the audit précis is not.
 * ------------------------------------------------------------------------ */

function anon(over: Partial<AgentRunEventWire> = {}): AgentRunEventWire {
  return event({ event_kind: "event.unavailable", event_status: "preterminal", ...over });
}

describe("runActivitySteps — the fold", () => {
  it("MUTATION `content-free-events-fold`: a run of them becomes ONE counted row", () => {
    const steps = runActivitySteps([
      anon({ event_id: "a1", sequence: 1, step_id: "run-tool" }),
      anon({ event_id: "a2", sequence: 2, step_id: "run-model" }),
      anon({ event_id: "a3", sequence: 3, step_id: "run-model" }),
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.name).toBe(runFoldedStepsName(3));
    // THE COUNT SURVIVES AS DATA, so the band's summary — the audit précis —
    // still says three steps happened (transcript/promotion.ts weights by it).
    expect(steps[0]?.foldedCount).toBe(3);
    // AND SO DOES EVERY DISTINCT STEP IDENTITY the Run assigned. Folding
    // repetition may not cost a fact the server actually stated.
    expect(steps[0]?.intent).toBe("run-tool · run-model");
    // THE PAIRED HALF: the governance statement is unchanged, verbatim. A fold
    // that softened "Not disclosable" into something friendlier would be a lie
    // about what the viewer may see.
    expect(steps[0]?.verdictLabel).toBe(RUN_STEP_NOT_DISCLOSABLE);
  });

  it("MUTATION `withheld-never-folds`: a withheld record keeps its own row", () => {
    // `withheld` is the projection reporting the RECORD ITSELF as withheld — a
    // governance outcome about one specific step, not repetition. It renders as
    // itself, always, and it breaks the run around it.
    const steps = runActivitySteps([
      anon({ event_id: "a1", sequence: 1 }),
      anon({ event_id: "a2", sequence: 2 }),
      anon({ event_id: "w", sequence: 3, event_status: "withheld" }),
      anon({ event_id: "a3", sequence: 4 }),
      anon({ event_id: "a4", sequence: 5 }),
    ]);
    expect(steps.map((s) => s.key)).toEqual(["run-events:a1+2", "run-event:w", "run-events:a3+2"]);
    expect(steps[1]?.verdictLabel).toBe(RUN_STEP_WITHHELD);
    expect(steps[1]?.foldedCount).toBeUndefined();
  });

  it("a lone content-free event is left exactly as it was", () => {
    // Folding one row gains nothing and costs its event id, which is what lets
    // a re-delivered page replace the row in place.
    const steps = runActivitySteps([anon({ event_id: "a1", step_id: "run-model" })]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.key).toBe("run-event:a1");
    expect(steps[0]?.name).toBe(RUN_STEP_UNNAMED);
    expect(steps[0]?.foldedCount).toBeUndefined();
  });

  it("MUTATION `named-work-stays-visible`: the fold never reaches across a named row", () => {
    // The model calls are the lane actually serving the turn — subordinate to
    // the tool call, never swallowed by the noise between them. The folded row
    // also holds exactly the position its first event held, so the log is not
    // reordered.
    const steps = runActivitySteps([
      event({ event_id: "m1", sequence: 1, step_id: "model:1" }),
      anon({ event_id: "a1", sequence: 2 }),
      anon({ event_id: "a2", sequence: 3 }),
      event({ event_id: "m2", sequence: 4, step_id: "model:2" }),
    ]);
    expect(steps.map((s) => s.key)).toEqual(["run-event:m1", "run-events:a1+2", "run-event:m2"]);
  });

  it("nothing the fold produces can promote out of the band", () => {
    const steps = runActivitySteps([anon({ event_id: "a1" }), anon({ event_id: "a2" })]);
    expect(steps.every((s) => s.verdict === "allowed")).toBe(true);
  });
});

/* --------------------------------------------------------------------------
 * RANK, and the sentence a Run with no plan gets instead of an invented one.
 * ------------------------------------------------------------------------ */

describe("the act, and the absence of one", () => {
  it("MUTATION `tool-step-leads`: a tool call carries its name, its intent and the act rank", () => {
    const steps = runTurnSteps(
      run({
        strategy: "agentic",
        agentic_plan: [
          {
            ordinal: 0,
            tool: "read.knowledge.search",
            step_intent: "Find information about DR-4830 disaster declaration",
            tool_call_id: null,
          },
        ],
      }),
      [anon({ event_id: "a1" }), anon({ event_id: "a2" })],
    );
    expect(steps[0]?.name).toBe("read.knowledge.search");
    expect(steps[0]?.intent).toBe("Find information about DR-4830 disaster declaration");
    // The act outranks the machinery around it. Presentational only — it is
    // still `allowed`, still in the band, still counted.
    expect(steps[0]?.rank).toBe("act");
    expect(steps[1]?.rank).toBe("detail");
    expect(steps[0]?.verdict).toBe("allowed");
  });

  it("MUTATION `absent-plan-says-so`: a null plan states the absence, never a description", () => {
    // The projection makes the plan all-or-nothing, and the event log carries
    // no tool name at all, so there is nothing to reconstruct from.
    const detail = run({ strategy: "agentic", agentic_plan: null });
    expect(runPlanAbsentNote(detail)).toBe(RUN_PLAN_UNAVAILABLE);
    // THE PAIRED HALF: the band gets THINNER, not wrong. No synthesised tool
    // row appears in its place, and the recorded events still draw.
    const steps = runTurnSteps(detail, [event({ event_id: "m1", step_id: "model:0" })]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.rank).toBe("detail");
    expect(steps.some((s) => s.rank === "act")).toBe(false);
  });

  it("says nothing when the plan was served, and nothing when the shape is unknown", () => {
    expect(
      runPlanAbsentNote(
        run({
          strategy: "agentic",
          agentic_plan: [
            { ordinal: 0, tool: "read.knowledge.search", step_intent: "x", tool_call_id: null },
          ],
        }),
      ),
    ).toBeNull();
    // A strategy the server would not name: this console does not know which
    // plan field to expect, so asserting one is missing would be a claim about
    // a shape it cannot see.
    expect(runPlanAbsentNote(run({ strategy: "unavailable" }))).toBeNull();
  });
});

/* --------------------------------------------------------------------------
 * THE GOVERNED STEPS, NAMED.
 *
 * The projection used to serve five of the thirteen kinds the store writes and
 * clamp the rest, so a real turn drew "Step · Not disclosable" over and over —
 * the exact complaint this block exists to prevent regressing. It now serves
 * all thirteen, plus `audit_action` for `run.audit.intent`, whose own detail is
 * three hashes and nothing else.
 *
 * Paired the way the rest of this file is: every "it says what happened" is
 * matched by an "and it says nothing the server did not".
 * ------------------------------------------------------------------------ */

function intent(action: string, over: Partial<AgentRunEventWire> = {}): AgentRunEventWire {
  return event({
    event_kind: "run.audit.intent",
    event_status: "receipt_pending",
    step_id: "run-tool",
    metadata: [
      { key: "audit_action", value: action },
      { key: "decision_kind", value: "state_transition" },
    ],
    ...over,
  });
}

describe("runActivitySteps — the governed audit actions", () => {
  it("MUTATION `audit-actions-named`: each governed step reads in plain language", () => {
    const steps = runActivitySteps([
      intent("run.execution.pre_effect", { event_id: "x1", sequence: 1, step_id: "run-execution" }),
      intent("run.model.pre_effect", { event_id: "x2", sequence: 2, step_id: "run-model" }),
      intent("run.tool.pre_effect", { event_id: "x3", sequence: 3, step_id: "run-tool" }),
      intent("run.evidence.produced", { event_id: "x4", sequence: 4, step_id: "run-tool" }),
      intent("run.delivery.decided", { event_id: "x5", sequence: 5, step_id: "run-finalizer" }),
      intent("run.execution.failed", { event_id: "x6", sequence: 6, step_id: "run-execution" }),
    ]);
    expect(steps.map((s) => s.name)).toEqual([
      RUN_STEP_EXECUTION,
      RUN_STEP_MODEL_CALL,
      RUN_STEP_TOOL_CALL,
      RUN_STEP_EVIDENCE,
      RUN_STEP_DELIVERY,
      RUN_STEP_EXECUTION,
    ]);
    // The pre-effect record says the record EXISTS, never that the effect
    // finished — the event carries no outcome, so neither may the row.
    expect(steps.slice(0, 3).map((s) => s.verdictLabel)).toEqual([
      RUN_STEP_RECORDED,
      RUN_STEP_RECORDED,
      RUN_STEP_RECORDED,
    ]);
    expect(steps[5]?.verdictLabel).toBe(RUN_STEP_FAILED);
    // MUTATION `named-steps-never-fold`: six named peers, six rows. The count
    // is only a fair compression while the rows are anonymous.
    expect(steps).toHaveLength(6);
    expect(steps.every((s) => s.foldedCount === undefined)).toBe(true);
    expect(steps.map((s) => s.key)).toEqual([
      "run-event:x1",
      "run-event:x2",
      "run-event:x3",
      "run-event:x4",
      "run-event:x5",
      "run-event:x6",
    ]);
  });

  it("MUTATION `unnamed-intent-stays-honest`: no audit_action means no guess", () => {
    // THE HONESTY HALF. `step_id` names a PHASE ("run-tool"), not an act, and
    // reading a label out of it would put a claim on screen the server never
    // made. An intent the server would not name falls back to the unknown row.
    const steps = runActivitySteps([
      intent("run.tool.pre_effect", { event_id: "n1", sequence: 1, metadata: [] }),
      intent("ne.gateway.complete", { event_id: "n2", sequence: 2 }),
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.name).toBe(runFoldedStepsName(2));
    expect(steps[0]?.verdictLabel).toBe(RUN_STEP_NOT_DISCLOSABLE);
    // And the phase identity still rides in the aux slot, unchanged — it is a
    // fact the server stated, it is simply not a name for the act.
    expect(steps[0]?.intent).toBe("run-tool");
  });

  it("MUTATION `lifecycle-kinds-named`: the store's own kinds each read as themselves", () => {
    const steps = runActivitySteps([
      event({
        event_id: "l1",
        sequence: 1,
        event_kind: "run.start.accepted",
        event_status: "queued",
      }),
      event({
        event_id: "l2",
        sequence: 2,
        event_kind: "run.execution.claimed",
        event_status: "running",
      }),
      event({
        event_id: "l3",
        sequence: 3,
        event_kind: "run.execution.recovered",
        event_status: "uncertain",
      }),
      event({
        event_id: "l4",
        sequence: 4,
        event_kind: "evidence.verified_row",
        event_status: "preterminal",
      }),
      event({
        event_id: "l5",
        sequence: 5,
        event_kind: "claim.verifier",
        event_status: "verified",
      }),
    ]);
    expect(steps.map((s) => s.name)).toEqual([
      RUN_STEP_REQUEST_ACCEPTED,
      RUN_STEP_EXECUTION_STARTED,
      RUN_STEP_EXECUTION_RECOVERED,
      // The lone verified row is stated in plain words by the evidence
      // roll-up; its verbatim row (RUN_STEP_EVIDENCE_ROW · Verified) rides in
      // the row's own disclosure.
      runEvidenceVerifiedLabel(1),
      RUN_STEP_CLAIM_CHECK,
    ]);
    expect(steps.map((s) => s.verdictLabel)).toEqual([
      RUN_STEP_ACCEPTED,
      RUN_STEP_STARTED,
      RUN_STEP_RECOVERED,
      RUN_STEP_EVIDENCE_ROLLUP_LABEL,
      RUN_STEP_VERIFIED,
    ]);
    expect(steps[3]?.detail?.map((s) => s.name)).toEqual([RUN_STEP_EVIDENCE_ROW]);
    expect(steps[3]?.detail?.map((s) => s.verdictLabel)).toEqual([RUN_STEP_VERIFIED]);
    expect(steps.every((s) => s.foldedCount === undefined)).toBe(true);
  });

  it("MUTATION `unknown-kind-still-honest`: a kind this console cannot name says so", () => {
    // THE FAIL-CLOSED BRANCH, which no amount of new naming may delete. A kind
    // a future server adds may not borrow another kind's word.
    const steps = runActivitySteps([
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      event({ event_id: "u1", event_kind: "some.future.kind" as any }),
    ]);
    expect(steps[0]?.name).toBe(RUN_STEP_UNNAMED);
    expect(steps[0]?.verdictLabel).toBe(RUN_STEP_NOT_DISCLOSABLE);
  });

  it("MUTATION `ending-stated-once`: the terminal's audit intent draws no second row", () => {
    // `run.terminal` and the `run.lifecycle.terminal` audit intent are two
    // records of ONE ending, and the receipt beside the band already states it.
    const steps = runActivitySteps([
      intent("run.tool.pre_effect", { event_id: "t1", sequence: 1 }),
      intent("run.lifecycle.terminal", { event_id: "t2", sequence: 2, step_id: "run-finalizer" }),
      event({ event_id: "t3", sequence: 3, event_kind: "run.terminal", event_status: "completed" }),
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.key).toBe("run-event:t1");
    expect(steps[0]?.name).toBe(RUN_STEP_TOOL_CALL);
  });

  it("nothing the named rows produce can promote out of the band", () => {
    const steps = runActivitySteps([
      intent("run.tool.pre_effect", { event_id: "p1", sequence: 1 }),
      event({
        event_id: "p2",
        sequence: 2,
        event_kind: "claim.verifier",
        event_status: "verified",
      }),
    ]);
    expect(steps.every((s) => s.verdict === "allowed")).toBe(true);
    expect(steps.every((s) => s.rank === "detail")).toBe(true);
  });
});

/* ================================================== the substance of a row == */
//
// The owner's measurement of the deployed build, verbatim: the rows "are
// useless and dont contain any actual information" — every line read "Tier —"
// and a bare kind word. These tests hold the OTHER side of the mapper's
// obligation: a row must carry every served fact it can, and still carry no
// fact the server did not send.

describe("a row carries the substance the server actually served", () => {
  it("times each row against the NEXT record, on the server's clock", () => {
    const steps = runActivitySteps([
      event({ event_id: "a", sequence: 1, created_at: "2026-08-27T12:00:00Z" }),
      event({ event_id: "b", sequence: 2, created_at: "2026-08-27T12:00:04Z" }),
      event({ event_id: "c", sequence: 3, created_at: "2026-08-27T12:00:05.500Z" }),
    ]);
    expect(steps.map((s) => s.durationSeconds)).toEqual([4, 1.5, null]);
  });

  it("INVENTS NO DURATION: an unparseable or backwards pair yields null", () => {
    expect(
      runActivitySteps([
        event({ event_id: "a", sequence: 1, created_at: "not-a-time" }),
        event({ event_id: "b", sequence: 2, created_at: "2026-08-27T12:00:04Z" }),
      ])[0]?.durationSeconds,
    ).toBeNull();
    expect(
      runActivitySteps([
        event({ event_id: "a", sequence: 1, created_at: "2026-08-27T12:00:09Z" }),
        event({ event_id: "b", sequence: 2, created_at: "2026-08-27T12:00:04Z" }),
      ])[0]?.durationSeconds,
    ).toBeNull();
  });

  it("names the authorized model on a model call, carried FORWARD only", () => {
    const steps = runActivitySteps([
      // A model call before anything named a model says nothing about one.
      event({
        event_id: "a",
        sequence: 1,
        event_kind: "run.audit.intent",
        step_id: "run-model",
        metadata: [{ key: "audit_action", value: "run.model.pre_effect" }],
      }),
      event({
        event_id: "b",
        sequence: 2,
        event_kind: "model.changed",
        metadata: [{ key: "after_authorized_model", value: "gpt-oss-120b" }],
      }),
      event({
        event_id: "c",
        sequence: 3,
        event_kind: "run.audit.intent",
        step_id: "run-model",
        metadata: [{ key: "audit_action", value: "run.model.pre_effect" }],
      }),
    ]);
    // The phase identity is DEMOTED, not deleted: the named audit-intent row
    // keeps a plain line, and "run-model" rides in its verbatim detail row.
    expect(steps[0]?.intent).toBeNull();
    expect(steps[0]?.detail?.[0]?.intent).toBe("run-model");
    expect(steps[0]?.detail?.[0]?.intent).not.toContain("gpt-oss-120b");
    expect(steps[2]?.name).toBe(RUN_STEP_MODEL_CALL);
    // The carried authorization still names the model that was thinking.
    expect(steps[2]?.intent).toBe("gpt-oss-120b");
    expect(steps[2]?.detail?.[0]?.intent).toBe("gpt-oss-120b · run-model");
  });

  it("falls back to the SERVED decision class when audit_action is absent", () => {
    // The deployment's audit join can resolve to nothing, in which case no
    // `audit_action` arrives at all. The row is then honestly un-named — but it
    // still carries the classification the server DID send, instead of "Step"
    // and a bare phase id.
    const steps = runActivitySteps([
      event({
        event_id: "a",
        sequence: 1,
        event_kind: "run.audit.intent",
        step_id: "run-execution",
        metadata: [{ key: "decision_kind", value: "state_transition" }],
      }),
    ]);
    expect(steps[0]?.name).toBe(RUN_STEP_UNNAMED);
    expect(steps[0]?.intent).toBe("state_transition · run-execution");
  });

  it("carries the delivery kind on a delivery decision", () => {
    const steps = runActivitySteps([
      event({
        event_id: "a",
        sequence: 1,
        event_kind: "delivery.decided",
        step_id: "run-delivery",
        metadata: [
          { key: "delivery_kind", value: "protected_turn_content" },
          { key: "decision", value: "Served" },
        ],
      }),
    ]);
    expect(steps[0]?.name).toBe(RUN_STEP_DELIVERY);
    expect(steps[0]?.verdictLabel).toBe("Served");
    expect(steps[0]?.intent).toBe("protected_turn_content · run-delivery");
  });

  it("draws no tier on any row, because the wire carries none", () => {
    const steps = runActivitySteps([event({ event_id: "a", sequence: 1 })]);
    expect(steps.every((s) => s.tier === null)).toBe(true);
  });
});

describe("the frontier row — the newest record, and only while the Run is live", () => {
  const events = [
    event({ event_id: "a", sequence: 1, created_at: "2026-08-27T12:00:00Z" }),
    event({ event_id: "b", sequence: 2, created_at: "2026-08-27T12:00:04Z" }),
  ];

  it("marks the last row of a RUNNING Run and nothing else", () => {
    const steps = runTurnSteps(run({ status: "running" }), events);
    expect(steps.map((s) => s.latest ?? false)).toEqual([false, true]);
  });

  it("marks NOTHING once the Run is terminal", () => {
    for (const status of ["completed", "failed", "cancelled", "expired"] as const) {
      const steps = runTurnSteps(run({ status }), events);
      expect(steps.some((s) => s.latest === true)).toBe(false);
    }
  });

  it("never marks a row a RUN'S ENDING would have covered", () => {
    // The terminal record draws no row, so the frontier is the last row a
    // reader can actually see — not an invisible one behind it.
    const steps = runActivitySteps(
      [
        ...events,
        event({
          event_id: "t",
          sequence: 3,
          event_kind: "run.terminal",
          created_at: "2026-08-27T12:00:09Z",
        }),
      ],
      { live: true },
    );
    expect(steps).toHaveLength(2);
    expect(steps[1]?.latest).toBe(true);
    // …and its duration is measured against the ending it does not draw, so the
    // last visible row is not silently un-timed.
    expect(steps[1]?.durationSeconds).toBe(5);
  });
});

describe("one act draws one row, even where the server wrote two records", () => {
  const produced = event({
    event_id: "canonical",
    sequence: 1,
    event_kind: "evidence.produced",
    step_id: "evidence:0",
  });
  const mirror = event({
    event_id: "mirror",
    sequence: 2,
    event_kind: "run.audit.intent",
    step_id: "evidence:0",
    metadata: [{ key: "audit_action", value: "run.evidence.produced" }],
  });

  it("drops the audit intent that mirrors a canonical event it kept", () => {
    // The sink writes `run.evidence.produced` beside `evidence.produced`, the
    // same shape as `run.lifecycle.terminal` beside `run.terminal`. A reader
    // watching a turn wants one row per thing the agent DID. The canonical
    // event that survives is stated by the evidence roll-up, standing for
    // exactly the one recorded event.
    const steps = runActivitySteps([produced, mirror]);
    expect(steps.map((s) => s.key)).toEqual(["run-events:canonical+1"]);
    expect(steps[0]?.detail?.map((s) => s.key)).toEqual(["run-event:canonical:detail"]);
  });

  it("MUTATION `drop-the-only-record`: it keeps the intent when the twin is absent", () => {
    // The rule removes a DUPLICATE. Where the projection served the intent and
    // not its canonical twin, the intent is the only account of that act.
    const steps = runActivitySteps([mirror]);
    expect(steps.map((s) => s.name)).toEqual([RUN_STEP_EVIDENCE]);
  });

  it("NEVER drops a pre-effect intent, which has no canonical twin at all", () => {
    // A tool call is recorded ONLY as an audit intent. Dropping one would
    // delete a governed tool call from the account of the turn.
    const steps = runActivitySteps([
      event({
        event_id: "tool",
        sequence: 1,
        event_kind: "run.audit.intent",
        step_id: "tool:0",
        metadata: [{ key: "audit_action", value: "run.tool.pre_effect" }],
      }),
      event({
        event_id: "model",
        sequence: 2,
        event_kind: "run.audit.intent",
        step_id: "model:0",
        metadata: [{ key: "audit_action", value: "run.model.pre_effect" }],
      }),
    ]);
    expect(steps.map((s) => s.name)).toEqual([RUN_STEP_TOOL_CALL, RUN_STEP_MODEL_CALL]);
  });
});

/* ---------------------------------------------------------------------------
 * THE NAMED FEED. The server now serves the join keys the band was missing:
 * `tool_name` on a tool receipt, `requested_model` on a model receipt,
 * `tool_call_id` on both the event and its plan step, and what a retrieval
 * produced as class and count. Every case pairs "it says what happened" with
 * "and it never borrows a word the server did not serve".
 * ------------------------------------------------------------------------ */

describe("runActivitySteps — the named tool, model, and retrieval rows", () => {
  const planStep = {
    ordinal: 0,
    tool: "read.knowledge.search",
    step_intent: "Search governed knowledge base for Reg E provisional credit figures",
    tool_call_id: "run-tool:tool:0",
  };
  const toolIntent = (over: Partial<AgentRunEventWire> = {}) =>
    intent("run.tool.pre_effect", {
      event_id: "t1",
      sequence: 1,
      metadata: [
        { key: "audit_action", value: "run.tool.pre_effect" },
        { key: "tool_call_id", value: "run-tool:tool:0" },
        { key: "tool_name", value: "read.knowledge.search" },
      ],
      ...over,
    });

  it("MUTATION `tool-row-named`: a tool receipt reads as its tool plus its stated intent", () => {
    const steps = runActivitySteps([toolIntent()], { plan: [planStep] });
    expect(steps[0]?.name).toBe("read.knowledge.search");
    expect(steps[0]?.intent).toContain(
      "Search governed knowledge base for Reg E provisional credit figures",
    );
    // Still the pre-effect truth: recorded, not completed.
    expect(steps[0]?.verdictLabel).toBe(RUN_STEP_RECORDED);
  });

  it("names the tool from the plan join alone when the chain served no tool_name", () => {
    const steps = runActivitySteps(
      [
        toolIntent({
          metadata: [
            { key: "audit_action", value: "run.tool.pre_effect" },
            { key: "tool_call_id", value: "run-tool:tool:0" },
          ],
        }),
      ],
      { plan: [planStep] },
    );
    expect(steps[0]?.name).toBe("read.knowledge.search");
    expect(steps[0]?.intent).toContain("Reg E provisional credit figures");
  });

  it("MUTATION `no-join-no-guess`: an unjoinable tool receipt keeps its generic word", () => {
    // No tool_call_id on the event, an unbound plan step, and a plan for a
    // DIFFERENT call id: three ways the join honestly fails, zero borrowed
    // sentences.
    const bare = runActivitySteps(
      [
        toolIntent({
          metadata: [{ key: "audit_action", value: "run.tool.pre_effect" }],
        }),
      ],
      { plan: [planStep] },
    );
    expect(bare[0]?.name).toBe(RUN_STEP_TOOL_CALL);
    expect(bare[0]?.intent ?? "").not.toContain("Reg E");

    const unbound = runActivitySteps([toolIntent()], {
      plan: [{ ...planStep, tool_call_id: null }],
    });
    // The chain's own tool_name still names the row; only the sentence needs
    // the join.
    expect(unbound[0]?.name).toBe("read.knowledge.search");
    expect(unbound[0]?.intent ?? "").not.toContain("Reg E");
  });

  it("MUTATION `disagreeing-names-break-the-join`: two server statements must agree", () => {
    const steps = runActivitySteps([toolIntent()], {
      plan: [{ ...planStep, tool: "write.internal.note" }],
    });
    // The chain's name wins the name slot; the plan's sentence is NOT borrowed
    // onto a row the plan may not describe.
    expect(steps[0]?.name).toBe("read.knowledge.search");
    expect(steps[0]?.intent ?? "").not.toContain("Reg E");
  });

  it("MUTATION `model-row-named`: a model receipt reads as the model it was pinned to", () => {
    const steps = runActivitySteps([
      intent("run.model.pre_effect", {
        event_id: "m1",
        sequence: 1,
        step_id: "run-model",
        metadata: [
          { key: "audit_action", value: "run.model.pre_effect" },
          { key: "requested_model", value: "frontier-anthropic" },
        ],
      }),
    ]);
    expect(steps[0]?.name).toBe("frontier-anthropic");
    // The event's own statement replaces the carried fallback; the model is
    // not printed twice on one row. The phase identity is demoted into the
    // row's verbatim detail row, not printed on the plain line.
    expect(steps[0]?.intent).toBeNull();
    expect(steps[0]?.detail?.[0]?.intent).toBe("run-model");
  });

  it("states what a retrieval produced — plain words on the line, kind and count in the detail, never text", () => {
    const steps = runActivitySteps([
      event({
        event_id: "e1",
        sequence: 1,
        event_kind: "evidence.produced",
        event_status: "preterminal",
        step_id: "run-tool",
        metadata: [
          { key: "read_kind", value: "search" },
          { key: "contributor_count", value: 3 },
          { key: "evidence_status", value: "located" },
        ],
      }),
    ]);
    // The plain line: what came back and where from, in words a reader who has
    // never seen this schema can use.
    expect(steps[0]?.name).toBe(runEvidenceRetrievedName(1));
    expect(steps[0]?.intent).toBe("from knowledge search");
    // The verbatim account — raw read_kind code, contributor count, phase id —
    // is DEMOTED into the row's disclosure, not deleted.
    expect(steps[0]?.detail?.[0]?.name).toBe(RUN_STEP_EVIDENCE);
    expect(steps[0]?.detail?.[0]?.intent).toBe("search · 3 contributors · run-tool");
  });

  it("MUTATION `counts-are-numbers`: a string under contributor_count is not a count", () => {
    const steps = runActivitySteps([
      event({
        event_id: "e1",
        sequence: 1,
        event_kind: "evidence.produced",
        event_status: "preterminal",
        step_id: "run-tool",
        metadata: [{ key: "contributor_count", value: "three-ish" }],
      }),
    ]);
    // No served read_kind means no source clause on the plain line, and the
    // uncoercible count appears nowhere — not even in the verbatim detail row.
    expect(steps[0]?.name).toBe(runEvidenceRetrievedName(1));
    expect(steps[0]?.intent).toBeNull();
    expect(steps[0]?.detail?.[0]?.intent).toBe("run-tool");
  });

  it("MUTATION `one-row-per-call`: a call the event log recorded draws ONCE, in chronology", () => {
    // The owner's defect (2026-08-28): the same `tool_call_id` used to draw
    // twice — a hoisted, timing-less plan row at the head of the band AND the
    // event row with its duration. The event row is the same act with more to
    // say, so it is the one that survives, named by the join, ranked `act`.
    const steps = runTurnSteps(run({ strategy: "agentic", agentic_plan: [planStep] }), [
      toolIntent(),
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.key).toBe("run-event:t1");
    expect(steps[0]?.name).toBe("read.knowledge.search");
    expect(steps[0]?.intent).toContain("Reg E provisional credit figures");
    // The emphasis moved WITH the call: bold in its chronological position.
    expect(steps[0]?.rank).toBe("act");
  });

  it("MUTATION `unrecorded-call-keeps-its-row`: a plan row the event log has not recorded still draws", () => {
    // The checkpoint lands BEFORE the call runs, so a Run that died between the
    // two — or has simply not recorded the event yet — owes the reader the
    // attempt. Only the recorded call's plan row is dropped.
    const pending = {
      ordinal: 1,
      tool: "write.internal.note",
      step_intent: "Draft the provisional credit memo",
      tool_call_id: "run-tool:tool:1",
    };
    const steps = runTurnSteps(run({ strategy: "agentic", agentic_plan: [planStep, pending] }), [
      toolIntent(),
    ]);
    expect(steps.map((s) => s.name)).toEqual(["write.internal.note", "read.knowledge.search"]);
    expect(steps.map((s) => s.rank)).toEqual(["act", "act"]);
  });

  it("MUTATION `unbound-plan-row-never-dropped`: no `tool_call_id`, no dedup", () => {
    // Nothing proves any event row is the same call, so dropping the plan row
    // would delete a recorded act on an inference. Both accounts draw.
    const steps = runTurnSteps(
      run({ strategy: "agentic", agentic_plan: [{ ...planStep, tool_call_id: null }] }),
      [toolIntent()],
    );
    expect(steps.map((s) => s.name)).toEqual(["read.knowledge.search", "read.knowledge.search"]);
  });

  it("an unnamed tool receipt stays machinery: the generic word earns no act rank", () => {
    // The paired half of `one-row-per-call`'s rank claim: `act` is reserved for
    // a row carrying a real, server-served tool name. "Tool call" is not one.
    const steps = runActivitySteps([
      toolIntent({ metadata: [{ key: "audit_action", value: "run.tool.pre_effect" }] }),
    ]);
    expect(steps[0]?.name).toBe(RUN_STEP_TOOL_CALL);
    expect(steps[0]?.rank).toBe("detail");
  });
});

/* ---------------------------------------------------------------------------
 * THE ROLL-UPS. Measured from a real S1 run: 93 `evidence.produced` events
 * (60 of them `verified_t3_empty` — sixty rows saying "no rows found", sixty
 * times) plus 35 `evidence.verified_row`s, and two rows for every model call.
 * The band now compresses each consecutive evidence run into one plain row and
 * each adjacent model pre-effect + served pair into one row. Every test pairs
 * the compression with its honesty half: the précis's headline still counts
 * every recorded event, the verbatim rows survive in the row's disclosure,
 * withheld records never fold, and an empty result is still stated.
 * ------------------------------------------------------------------------ */

describe("the evidence roll-up — one plain row per consecutive evidence run", () => {
  const produced = (id: string, sequence: number, readKind: string) =>
    event({
      event_id: id,
      sequence,
      event_kind: "evidence.produced",
      event_status: "preterminal",
      step_id: "run-tool",
      metadata: [
        { key: "read_kind", value: readKind },
        { key: "contributor_count", value: 1 },
      ],
    });
  const verifiedRow = (id: string, sequence: number) =>
    event({
      event_id: id,
      sequence,
      event_kind: "evidence.verified_row",
      event_status: "preterminal",
      step_id: "run-tool",
      metadata: [{ key: "read_kind", value: "verified_t3" }],
    });

  it("MUTATION `evidence-rolls-up`: produced rows and verified rows become ONE counted row", () => {
    const steps = runActivitySteps([
      produced("e1", 1, "verified_t3"),
      produced("e2", 2, "verified_t3"),
      produced("e3", 3, "verified_t3"),
      verifiedRow("v1", 4),
      verifiedRow("v2", 5),
      event({
        event_id: "d1",
        sequence: 6,
        event_kind: "delivery.decided",
        event_status: "preterminal",
      }),
    ]);
    expect(steps).toHaveLength(2);
    expect(steps[0]?.name).toBe(runEvidenceRetrievedName(3));
    expect(steps[0]?.intent).toBe("from the verified graph · 2 rows verified");
    expect(steps[0]?.verdictLabel).toBe(RUN_STEP_EVIDENCE_ROLLUP_LABEL);
    // THE PRÉCIS MUST NOT SHRINK: the one row stands for five recorded events
    // and the headline count still says so.
    expect(steps[0]?.foldedCount).toBe(5);
    expect(bandStepCount([steps[0]!])).toBe(5);
    // DEMOTED, NEVER DELETED: the verbatim per-event rows, in order, with the
    // raw read_kind codes a reader who wants the schema can still open.
    expect(steps[0]?.detail?.map((s) => s.name)).toEqual([
      RUN_STEP_EVIDENCE,
      RUN_STEP_EVIDENCE,
      RUN_STEP_EVIDENCE,
      RUN_STEP_EVIDENCE_ROW,
      RUN_STEP_EVIDENCE_ROW,
    ]);
    expect(steps[0]?.detail?.[0]?.intent).toBe("verified_t3 · 1 contributor · run-tool");
    expect(steps[1]?.name).toBe(RUN_STEP_DELIVERY);
  });

  it("MUTATION `empty-result-stated-once`: sixty empty reads are one row that says NOTHING CAME BACK", () => {
    const empties = Array.from({ length: 60 }, (_, i) =>
      produced(`empty-${i}`, i + 1, "verified_t3_empty"),
    );
    const steps = runActivitySteps(empties);
    // Once — not sixty times, and not zero times. The absence is a served
    // fact and it stays visible, with its own count.
    expect(steps).toHaveLength(1);
    expect(steps[0]?.name).toBe(RUN_STEP_NO_RECORDS);
    expect(steps[0]?.intent).toBe(`from the verified graph · ${runEvidenceEmptyReadsLabel(60)}`);
    expect(steps[0]?.foldedCount).toBe(60);
    expect(bandStepCount(steps)).toBe(60);
    expect(steps[0]?.detail).toHaveLength(60);
  });

  it("mixed reads state both halves: what came back and what came back empty", () => {
    const steps = runActivitySteps([
      produced("e1", 1, "verified_t3"),
      produced("e2", 2, "verified_t3_empty"),
      produced("e3", 3, "search"),
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.name).toBe(runEvidenceRetrievedName(2));
    expect(steps[0]?.intent).toBe(
      `from the verified graph and knowledge search · ${runEvidenceEmptyReadsLabel(1)}`,
    );
  });

  it("MUTATION `withheld-evidence-never-rolls-up`: a withheld record keeps its own row", () => {
    const steps = runActivitySteps([
      produced("e1", 1, "verified_t3"),
      event({
        event_id: "w1",
        sequence: 2,
        event_kind: "evidence.produced",
        event_status: "withheld",
        step_id: "run-tool",
      }),
      produced("e2", 3, "verified_t3"),
    ]);
    // Three recorded events, three rows: the roll-up may compress repetition,
    // never a distinct governance statement.
    expect(steps).toHaveLength(3);
    expect(steps[1]?.verdictLabel).toBe("Produced");
    expect(steps[1]?.key).toBe("run-event:w1");
  });

  it("times the roll-up from its first record to the record after its last", () => {
    const steps = runActivitySteps([
      { ...produced("e1", 1, "verified_t3"), created_at: "2026-08-27T12:00:00Z" },
      { ...produced("e2", 2, "verified_t3"), created_at: "2026-08-27T12:00:01Z" },
      {
        ...event({
          event_id: "d1",
          sequence: 3,
          event_kind: "delivery.decided",
          event_status: "preterminal",
        }),
        created_at: "2026-08-27T12:00:02.800Z",
      },
    ]);
    expect(steps[0]?.durationSeconds).toBe(2.8);
  });

  it("nothing the roll-up produces can promote out of the band", () => {
    const steps = runActivitySteps([
      produced("e1", 1, "verified_t3"),
      produced("e2", 2, "verified_t3_empty"),
      verifiedRow("v1", 3),
    ]);
    for (const step of steps) {
      expect(step.verdict).toBe("allowed");
      for (const row of step.detail ?? []) expect(row.verdict).toBe("allowed");
    }
  });
});

describe("the model beat — two records of one call merge into one row", () => {
  const preEffect = (id: string, sequence: number) =>
    intent("run.model.pre_effect", {
      event_id: id,
      sequence,
      step_id: "run-model",
      metadata: [
        { key: "audit_action", value: "run.model.pre_effect" },
        { key: "decision_kind", value: "state_transition" },
        { key: "requested_model", value: "frontier-default" },
      ],
    });
  const served = (id: string, sequence: number) =>
    event({
      event_id: id,
      sequence,
      event_kind: "model.served",
      event_status: "served",
      step_id: "model:2",
      metadata: [{ key: "served_model", value: "frontier-default" }],
    });

  it("MUTATION `model-beat-merges`: an adjacent pre-effect + served pair is ONE row", () => {
    const steps = runActivitySteps([preEffect("m1", 1), served("m2", 2)]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.name).toBe("frontier-default");
    expect(steps[0]?.verdictLabel).toBe(RUN_STEP_SERVED);
    expect(steps[0]?.intent).toBe(RUN_STEP_MODEL_CALL_NOTE);
    // Two recorded events, honestly counted, both verbatim rows demoted into
    // the disclosure — "state_transition · run-model · Recorded" included.
    expect(steps[0]?.foldedCount).toBe(2);
    expect(bandStepCount(steps)).toBe(2);
    expect(steps[0]?.detail?.map((s) => s.verdictLabel)).toEqual([
      RUN_STEP_RECORDED,
      RUN_STEP_SERVED,
    ]);
    expect(steps[0]?.detail?.[0]?.intent).toContain("run-model");
    expect(steps[0]?.detail?.[1]?.intent).toBe("model:2");
  });

  it("MUTATION `no-merge-across-records`: anything between the pair keeps both rows", () => {
    const steps = runActivitySteps([
      preEffect("m1", 1),
      event({
        event_id: "c1",
        sequence: 2,
        event_kind: "model.changed",
        event_status: "changed",
        metadata: [{ key: "after_authorized_model", value: "fallback-model" }],
      }),
      served("m2", 3),
    ]);
    // The pre-effect's next record is NOT its reply, so pairing them would be
    // an invented chronology. Three records, three rows.
    expect(steps).toHaveLength(3);
    expect(steps[0]?.name).toBe("frontier-default");
    expect(steps[0]?.verdictLabel).toBe(RUN_STEP_RECORDED);
    expect(steps[2]?.verdictLabel).toBe(RUN_STEP_SERVED);
  });

  it("a pre-effect with no reply keeps its own row — a failed call is not smoothed over", () => {
    const steps = runActivitySteps([preEffect("m1", 1)]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.name).toBe("frontier-default");
    expect(steps[0]?.verdictLabel).toBe(RUN_STEP_RECORDED);
    expect(steps[0]?.foldedCount).toBeUndefined();
  });

  it("times the merged row from the pre-effect to the record after the reply", () => {
    const steps = runActivitySteps([
      { ...preEffect("m1", 1), created_at: "2026-08-27T12:00:00Z" },
      { ...served("m2", 2), created_at: "2026-08-27T12:00:06Z" },
      {
        ...event({
          event_id: "t1",
          sequence: 3,
          event_kind: "run.terminal",
          event_status: "completed",
        }),
        created_at: "2026-08-27T12:00:07Z",
      },
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0]?.durationSeconds).toBe(7);
  });

  it("marks the merged row as the frontier of a live Run, and never of a settled one", () => {
    const events = [preEffect("m1", 1), served("m2", 2)];
    expect(runActivitySteps(events, { live: true })[0]?.latest).toBe(true);
    expect(runActivitySteps(events)[0]?.latest).toBeUndefined();
  });
});
