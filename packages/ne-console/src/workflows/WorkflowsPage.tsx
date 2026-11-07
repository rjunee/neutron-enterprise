// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-WORKFLOWS — the governed multi-step workflows this deployment offers, and
// the one control that runs one.
//
// WHAT THIS REPLACES: nothing. The engine has been on the wire since 2026-08-03
// and was reachable only by curl — the run that reads a customer's records,
// decides a write is needed, stops, and parks a REAL approval had no door in
// the browser at all.
//
// THE PERMISSION CONTRACT IS RENDERED BEFORE ANYTHING RUNS. `reads[]` and
// `writes[]` arrive in the SAME list payload as the title, so naming them costs
// no second call and no waiting: what the agent may read, and the single thing
// it may write, are on screen before the operator has decided to press
// anything. That ordering is the product, not decoration.
//
// A RUN BLOCKS FOR ABOUT HALF A MINUTE PER SUBJECT. The button therefore does
// not hand back a spinner and hope: it names what is happening, counts the
// seconds it has actually been waiting, and states the expectation up front. A
// control that looks hung is indistinguishable from a broken one.
//
// THIN CLIENT (C7). Every step row, proposal row and count below is a field of
// the server's `WorkflowRunView`. This screen computes no outcome, no verdict
// and no total; it does not decide which workflow "should" be runnable, and it
// does not pre-judge `max_subjects` against a ceiling it cannot read — an
// over-ask is answered by the server's own 422, rendered verbatim.

import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { ApiError, sourceDisclosure } from "../api/client";
import type { ApiClient } from "../api/client";
import type {
  AgenticWorkflowRunView,
  AgenticWorkflowRunStatusView,
  TraceAssemblyView,
  TraceNodeView,
  WorkflowFindingView,
  WorkflowProposalView,
  WorkflowResultView,
  WorkflowRunView,
  WorkflowStepView,
  WorkflowView,
} from "../api/types";
import { entitlementsOf } from "../auth/entitlements";
import { usePrincipal } from "../auth/PrincipalContext";
import { useApiClient } from "../auth/useApiClient";
import { useNow } from "../approvals/useNow";
import { useGovernedRead } from "../landing/useGovernedRead";
import { RunModelPicker } from "../models/RunModelPicker";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { formatCount, formatUsdCents } from "../primitives/format";
import { ListScreen } from "../primitives/PageHeader";
import { Empty, LoadError, PolicyDenied, Refused } from "../primitives/states";
import { PrimaryButton, Toolbar } from "../primitives/Toolbar";
import { SpanTree } from "../trace/SpanTree";
import { SpanPane } from "../trace/SpanPane";
import "../trace/trace.css";
import "./workflows.css";

/** The workflow list: title and what it does. `reads`/`writes` are NOT columns —
 * they are the contract panel below, where they get room to be read. */
const WORKFLOW_COLUMNS: readonly Column<WorkflowView>[] = [
  {
    key: "title",
    header: "Workflow",
    cell: (row) => row.title,
    sortValue: (row) => row.title,
    width: "240px",
  },
  { key: "description", header: "What it does", cell: (row) => row.description },
];

/**
 * A `detail` map is server-supplied metadata (identifiers, counts, enums, bools
 * — never customer content, C12). It is rendered as the pairs the server sent,
 * in the order it sent them; the console adds no key, drops none, and
 * interprets none. An absent or empty map renders as the table's em dash.
 *
 * THE KEY IS RESPELT, THE VALUE IS NOT. `subject_ref: crm_acct_1042 ·
 * rows_matched: 3` is a wire dump wearing a Detail column: the values are the
 * server's facts, but the KEYS are field names, and a reader who is not the
 * person who named them reads snake_case as machine output and stops reading.
 * The respelling is mechanical and reversible — separators become spaces and
 * the first letter is capitalised, exactly as `trace/format.ts::templateLabel`
 * does for a template id. No key is renamed, translated or glossed: this
 * console does not know what a server field MEANS and must not pretend to.
 */
function humanKey(key: string): string {
  const spaced = key.replace(/[-_]+/g, " ").trim();
  return spaced === "" ? key : `${spaced.charAt(0).toUpperCase()}${spaced.slice(1)}`;
}

function detailPairs(detail: Readonly<Record<string, unknown>> | undefined): string {
  if (detail === undefined) return "";
  return Object.entries(detail)
    .map(([key, value]) => `${humanKey(key)}: ${String(value)}`)
    .join(" · ");
}

const STEP_COLUMNS: readonly Column<WorkflowStepView>[] = [
  { key: "step", header: "Step", cell: (row) => row.step, numeric: true, width: "64px" },
  { key: "kind", header: "Kind", cell: (row) => row.kind, width: "120px" },
  { key: "tool", header: "Tool", cell: (row) => row.tool, mono: true, width: "220px" },
  { key: "outcome", header: "Outcome", cell: (row) => row.outcome, width: "120px" },
  { key: "detail", header: "Detail", cell: (row) => detailPairs(row.detail) },
];

const FINDING_COLUMNS: readonly Column<WorkflowFindingView>[] = [
  { key: "subject_ref", header: "Subject", cell: (row) => row.subject_ref, mono: true },
  { key: "related_ref", header: "Related", cell: (row) => row.related_ref, mono: true },
  { key: "outcome", header: "Outcome", cell: (row) => row.outcome, width: "120px" },
  {
    key: "evidence_refs",
    header: "Evidence",
    cell: (row) => row.evidence_refs?.join(" · "),
    mono: true,
  },
  { key: "detail", header: "Detail", cell: (row) => detailPairs(row.detail) },
];

/** The parked write's link into the queue that now holds it. `PendingMarker`'s
 * deep-link shape (`/approvals?request=…`) is reused so an approval opened from
 * here lands on the same row it would from anywhere else. */
function ApprovalLink({ requestId }: { requestId: string }) {
  return <Link to={`/approvals?request=${encodeURIComponent(requestId)}`}>{requestId}</Link>;
}

const PROPOSAL_COLUMNS: readonly Column<WorkflowProposalView>[] = [
  { key: "subject_ref", header: "Subject", cell: (row) => row.subject_ref, mono: true },
  { key: "crm_record_id", header: "CRM record", cell: (row) => row.crm_record_id, mono: true },
  { key: "outcome", header: "Outcome", cell: (row) => row.outcome, width: "120px" },
  // The engine's own account of a non-parked outcome. Rendered because a
  // proposal that reads "refused" beside five blank cells asks the one question
  // this screen exists to answer; a parked one carries none, and gets the em dash.
  { key: "reason", header: "Reason", cell: (row) => row.reason },
  {
    key: "body_chars",
    header: "Note length",
    cell: (row) => formatCount(row.body_chars),
    numeric: true,
    width: "110px",
  },
  {
    key: "evidence_refs",
    header: "Evidence",
    cell: (row) => row.evidence_refs?.join(" · "),
    mono: true,
  },
  {
    key: "approval_request_id",
    header: "Approval",
    cell: (row) =>
      row.approval_request_id ? <ApprovalLink requestId={row.approval_request_id} /> : null,
    mono: true,
  },
];

// ===========================================================================
// The agentic run's vocabulary, in words
// ===========================================================================

/**
 * WHAT THE SERVER RECORDED about a run it reported `failed` — and only that.
 * That the run failed is the card's LINE (`RunFault`, below); this is the
 * identity underneath it, which is why the sentence no longer repeats it.
 *
 * `error_class` is `type(exc).__name__`, written server-side by
 * `agentic_workflows.py::fail_agentic_run`. This used to read
 * `current.error_class ?? "AgenticRunFailed"`: a class name NO SERVER EVER
 * EMITS, printed as if it were the failure's identity. A console must not name
 * an error the deployment did not raise, so the fallback is gone — a run that
 * failed without a named class says exactly that, and says nothing more. NO
 * CAUSE IS GUESSED AT HERE, and neither is what a class MEANS.
 *
 * When there IS a class it is kept verbatim: it is the only identity the server
 * recorded, and a reader forwarding this to an engineer needs it exactly.
 */
function agenticFailureDetail(errorClass: string | null): string {
  return errorClass === null || errorClass === ""
    ? "The server did not name a cause."
    : `The server reported ${errorClass}.`;
}

/** Does this value carry a NAME — at least one letter or digit — as opposed to
 * whitespace or punctuation? Both wire vocabularies below fall back on the
 * server's own word, and neither has a word to keep when it is given none. */
const NAMES_SOMETHING = /[\p{L}\p{N}]/u;

/**
 * The terminal outcome, in words. The server's vocabulary is a closed union
 * (`agentic_workflows.py`), so this maps it exhaustively — closed on the
 * server, that is; what actually arrives is `outcomeWords`' problem.
 *
 * `budget-exhausted` IS LOAD-BEARING AND IS NOT SOFTENED: the run met a ceiling
 * and was cut short there, and the gloss says so — never "finished early",
 * "wrapped up" or any phrasing that would let a partial run read as a complete
 * one.
 *
 * WHAT IT NO LONGER SAYS IS "configured". `budget-exhausted` is one terminal
 * shared by five ceilings and one of them, `requests`, is DERIVED by
 * `runner.py::model_request_limit` (`max_tool_calls + 1 + _OUTPUT_RETRIES`)
 * rather than set by anybody; that same docstring calls naming it as an
 * operator ceiling "a terminal naming the wrong cause, since no ceiling the
 * operator set was actually reached". It is also the cap `runner.py` falls
 * through to when a `UsageLimitExceeded` matches none of the others, so it is
 * the one most likely to be printed. Which ceiling it was is said per cap
 * below; the outcome word states only what holds for all five.
 */
const OUTCOME_WORDS: Readonly<Record<AgenticWorkflowRunView["outcome"], string>> = {
  complete: "Completed",
  "budget-exhausted": "Stopped at a limit",
  "context-exhausted": "Stopped at the context limit",
};

function outcomeWords(outcome: AgenticWorkflowRunView["outcome"]): string {
  // `Object.hasOwn`, NOT a bare index — `capWords`' guard, one rung up, for the
  // same reason. The union above is a claim about the wire that nothing checks
  // on arrival: `client.ts` casts the parsed JSON straight into
  // `AgenticWorkflowRunView`, so `outcome` is whatever the server sent.
  // `OUTCOME_WORDS["constructor"]` is `Object`'s constructor FUNCTION and
  // `OUTCOME_WORDS["__proto__"]` is `Object.prototype`, and a bare index put
  // either of them under the heading "Terminal result".
  //
  // The PARAMETER STAYS THE UNION. Widening it to `string` would buy the guard
  // by letting every call site pass anything, and the exhaustive `Record` — the
  // thing that turns a new server outcome with no word for it into a `tsc`
  // error — is what this screen actually relies on. The type governs what the
  // console must have a word for; the guard governs what the wire may carry.
  const taught = Object.hasOwn(OUTCOME_WORDS, outcome) ? OUTCOME_WORDS[outcome] : undefined;
  if (taught !== undefined) return taught;
  // An outcome nobody taught this console prints VERBATIM, on `capWords`'
  // precedent: the server's own word is a fact about how the run ended, and
  // dropping it would leave a terminal result with no terminal. A value that
  // carries no word to keep — spaces, a lone separator — says that instead of
  // rendering "Outcome: " with nothing after it.
  return NAMES_SOMETHING.test(outcome) ? outcome : "The server did not name the outcome";
}

/**
 * WHICH ceiling stopped the run, in words. Five of these share the
 * `budget-exhausted` terminal, and which one it was is the whole difference
 * between "raise the tool budget" and "the model took too long".
 *
 * The wire field is an open `string`, so a cap this console has not been
 * taught renders VERBATIM rather than being dropped or guessed at: a named
 * ceiling nobody glossed is still a fact, and losing it would be worse than
 * printing it raw. (`runner.py` emits `context`, `wall-clock`, `tool-calls`,
 * `tokens`, `parked-writes` and `requests`.) The two values that are NOT
 * printed raw — an inherited property name, and a value carrying no name at
 * all — are handled in `capWords` and explained there.
 */
const CAP_WORDS: Readonly<Record<string, string>> = {
  context: "the context-window limit",
  "wall-clock": "the time limit",
  "tool-calls": "the limit on tool calls",
  tokens: "the token limit",
  "parked-writes": "the limit on writes held for approval",
  // NOT A SETTING, AND SAID SO. There is no model-request number to raise:
  // `model_request_limit` derives this ceiling from the tool-call cap, so an
  // operator sent looking for one would find nothing. Naming its source names
  // the number they can actually change.
  requests: "the model-request ceiling, which the runtime derives from the tool-call limit",
};

function capWords(cap: string): string {
  // `Object.hasOwn`, NOT a bare index with `??`. `CAP_WORDS` is an object, so
  // `CAP_WORDS["constructor"]` is `Object`'s constructor FUNCTION and
  // `CAP_WORDS["__proto__"]` is `Object.prototype` — both non-nullish, so `??`
  // waved them through a `string` return type and into the stopped-run
  // sentence, which then read "it reached function Object() { [native code] }".
  // The cap arrives as an open `string` straight off the wire, so this is the
  // server's to send.
  const taught = Object.hasOwn(CAP_WORDS, cap) ? CAP_WORDS[cap] : undefined;
  if (taught !== undefined) return taught;
  // An untaught cap prints VERBATIM — a named ceiling nobody glossed is still a
  // fact, and the rule exists so this console never deletes words the server
  // gave it. That rule says nothing about a value that gave none: a cap of
  // spaces or of a lone separator left the sentence reading "it reached ." with
  // its subject missing, which is not the server's word being preserved.
  return NAMES_SOMETHING.test(cap) ? cap : "an unnamed ceiling";
}

/** The stopped-run line: the outcome in words, the ceiling that was reached
 * when the server named one, and the reason nothing is shown — a partial
 * answer from a run that was cut short is not an answer. */
function stoppedRunMessage(
  outcome: AgenticWorkflowRunView["outcome"],
  exhaustedCap: string | null | undefined,
): string {
  const cap = exhaustedCap ? ` — it reached ${capWords(exhaustedCap)}` : "";
  return `${outcomeWords(outcome)}${cap}. The partial answer was withheld.`;
}

// ===========================================================================
// The fault card
// ===========================================================================

/**
 * A fault on this screen, headlined with what actually went wrong on it.
 *
 * NOT `<SaveError>`, WHICH THIS SCREEN NO LONGER USES. That primitive's line is
 * "Couldn't save. Try again." — the largest text in the card — and there is
 * nothing on this screen to save: it starts runs. Over a background run the
 * server reported `failed`, it asserted two things that were both untrue at
 * once: that a write had been attempted, and that repeating it was the action.
 * The run had already happened, on the server, spending its budget and possibly
 * parking writes; "try again" pointed at nothing the reader could usefully do.
 * That is the same defect — the console asserting something untrue of the
 * situation — as the fabricated exception name the sentence below it replaced,
 * sitting directly above the fix for it.
 *
 * THE FAULT STYLING IS THE PRIMITIVE'S, UNCHANGED: same block, same
 * `role="alert"`, same fault colour, same line/detail hierarchy. A failed run is
 * a real failure and is not softened into a neutral notice — only the claim is
 * this screen's own. (`SkillsTab`, `ProposalPane`, `DecisionPane`, `SearchPage`
 * and `RecordPane` all state their own line over this same block, for exactly
 * this reason: the block is the grammar, the sentence is the situation's.)
 */
function RunFault({ line, detail }: { line: string; detail?: string | null }) {
  return (
    <div className="ne-state ne-state--error" role="alert">
      <p className="ne-state__line">{line}</p>
      {detail ? <p className="ne-state__detail">{detail}</p> : null}
    </div>
  );
}

// ===========================================================================
// The run
// ===========================================================================

/**
 * FIVE STATES, AND THE ROLE BOUNDARY IS ONLY ONE OF THEM (the ModelsPage
 * air-gap precedent). A run still in flight, one the server refused, one it
 * declined on policy grounds and one that failed are four different facts, and
 * collapsing any pair of them tells the operator something untrue.
 *
 * Every state carries the `workflowId` it belongs to, so selecting a different
 * workflow can never show it that workflow's result.
 */
type RunState =
  | { kind: "idle" }
  | { kind: "running"; workflowId: string; subjects: number; startedAt: number }
  | { kind: "done"; workflowId: string; run: WorkflowRunView }
  /** 403: the server's own reason, rendered verbatim where the submit happened
   * — or null where the gate stated none, in which case the card states the
   * denial and nothing else. The console never writes the gate's reason. */
  | { kind: "denied"; workflowId: string; reason: string | null }
  | { kind: "error"; workflowId: string; detail: string | null };

function useRunWorkflow(client: ApiClient, owner: object) {
  const ownerRef = useRef(owner);
  useLayoutEffect(() => {
    ownerRef.current = owner;
  }, [owner]);
  const [slot, setSlot] = useState<{ owner: object; state: RunState }>({
    owner,
    state: { kind: "idle" },
  });
  const state: RunState = slot.owner === owner ? slot.state : { kind: "idle" };
  const run = useCallback(
    async (workflowId: string, subjects: number) => {
      if (ownerRef.current !== owner) return;
      setSlot({
        owner,
        state: { kind: "running", workflowId, subjects, startedAt: Date.now() },
      });
      try {
        const view = await client.runWorkflow(workflowId, subjects);
        if (ownerRef.current !== owner) return;
        setSlot({ owner, state: { kind: "done", workflowId, run: view } });
      } catch (err: unknown) {
        if (ownerRef.current !== owner) return;
        // THE GATE'S OWN SENTENCE OR NOTHING on the denial: `PolicyDenied`
        // prints it under "Denied by policy.", where a console sentence reads
        // as the gate's stated reason for denying. The fault branch below is
        // different and keeps `userMessage`: nothing there attributes the
        // string to anyone, both of its lines are the console's own voice, and
        // "The server is unavailable." under "Try again." is the reader being
        // told what happened.
        if (err instanceof ApiError && err.status === 403) {
          setSlot({
            owner,
            state: { kind: "denied", workflowId, reason: err.detail ?? null },
          });
          return;
        }
        setSlot({
          owner,
          state: {
            kind: "error",
            workflowId,
            detail: err instanceof ApiError ? err.userMessage : null,
          },
        });
      }
    },
    [client, owner],
  );
  return { state, run };
}

type AgenticRunState =
  | { kind: "idle" }
  | { kind: "starting"; workflowId: string; startedAt: number }
  | {
      kind: "running";
      workflowId: string;
      startedAt: number;
      ticket: AgenticWorkflowRunStatusView;
      trace: TraceAssemblyView | null;
      statusError: boolean;
      traceError: boolean;
    }
  | {
      kind: "done";
      workflowId: string;
      run: AgenticWorkflowRunView;
      /** The last exact assembly served for this run, if one arrived. */
      trace: TraceAssemblyView | null;
      /** A non-404 failure of the final trace read; never normalized to absence. */
      traceError: boolean;
    }
  /** 403: the gate's own reason, or null where it stated none. */
  | { kind: "denied"; workflowId: string; reason: string | null }
  /** The START REQUEST failed on the wire — nothing was accepted, and pressing
   *  Run again is a real retry of a thing that did not happen. */
  | { kind: "error"; workflowId: string; detail: string | null }
  /** THE SERVER TOOK THE RUN AND THEN REPORTED IT TERMINAL `failed`. Not
   *  `error`, and the difference is the whole reason this kind exists: the run
   *  ran, spent budget and may have parked writes, so there is no request to
   *  re-send and no action to offer. Collapsing the two told the operator that
   *  a write had failed and that retrying it was the next step — both untrue. */
  | { kind: "failed"; workflowId: string; errorClass: string | null };

function useRunAgenticWorkflow(
  client: ApiClient,
  owner: object,
  onSelectionInvalidated: () => void,
) {
  const ownerRef = useRef(owner);
  useLayoutEffect(() => {
    ownerRef.current = owner;
  }, [owner]);
  const [slot, setSlot] = useState<{ owner: object; state: AgenticRunState }>({
    owner,
    state: { kind: "idle" },
  });
  const state: AgenticRunState = slot.owner === owner ? slot.state : { kind: "idle" };
  const run = useCallback(
    async (workflowId: string, requestedModel: string) => {
      if (ownerRef.current !== owner) return;
      setSlot({ owner, state: { kind: "starting", workflowId, startedAt: Date.now() } });
      try {
        const ticket = await client.startAgenticWorkflow(workflowId, requestedModel);
        if (ownerRef.current !== owner) return;
        setSlot({
          owner,
          state: {
            kind: "running",
            workflowId,
            startedAt: Date.now(),
            ticket,
            trace: null,
            statusError: false,
            traceError: false,
          },
        });
      } catch (err: unknown) {
        if (ownerRef.current !== owner) return;
        if (
          err instanceof ApiError &&
          (err.status === 403 || err.status === 409 || err.status === 503)
        ) {
          onSelectionInvalidated();
        }
        // THE GATE'S OWN SENTENCE OR NOTHING on the denial: `PolicyDenied`
        // prints it under "Denied by policy.", where a console sentence reads
        // as the gate's stated reason for denying. The fault branch below is
        // different and keeps `userMessage`: nothing there attributes the
        // string to anyone, both of its lines are the console's own voice, and
        // "The server is unavailable." under "Try again." is the reader being
        // told what happened.
        if (err instanceof ApiError && err.status === 403) {
          setSlot({
            owner,
            state: { kind: "denied", workflowId, reason: err.detail ?? null },
          });
          return;
        }
        setSlot({
          owner,
          state: {
            kind: "error",
            workflowId,
            detail: err instanceof ApiError ? err.userMessage : null,
          },
        });
      }
    },
    [client, onSelectionInvalidated, owner],
  );

  const ticket = state.kind === "running" ? state.ticket : null;
  useEffect(() => {
    if (ticket === null) return;
    let stopped = false;
    let timer: number | null = null;

    const poll = async () => {
      const [statusResult, traceResult] = await Promise.allSettled([
        client.agenticWorkflowRun(ticket.run_id),
        client.trace(ticket.trace_id),
      ]);
      if (stopped || ownerRef.current !== owner) return;

      let terminal = false;
      if (statusResult.status === "fulfilled") {
        const current = statusResult.value;
        const result = current.result;
        if (result) {
          terminal = true;
          const traceMissing =
            traceResult.status === "rejected" &&
            traceResult.reason instanceof ApiError &&
            traceResult.reason.status === 404;
          setSlot((previous) => ({
            owner,
            state: {
              kind: "done",
              workflowId: current.workflow_id,
              run: result,
              trace:
                traceResult.status === "fulfilled"
                  ? traceResult.value
                  : previous.owner === owner &&
                      previous.state.kind === "running" &&
                      previous.state.ticket.run_id === ticket.run_id
                    ? previous.state.trace
                    : null,
              traceError: traceResult.status === "rejected" && !traceMissing,
            },
          }));
        } else if (current.status === "failed") {
          terminal = true;
          setSlot({
            owner,
            state: {
              kind: "failed",
              workflowId: current.workflow_id,
              errorClass: current.error_class ?? null,
            },
          });
        } else {
          setSlot((previous) =>
            previous.owner === owner &&
            previous.state.kind === "running" &&
            previous.state.ticket.run_id === ticket.run_id
              ? { owner, state: { ...previous.state, statusError: false } }
              : previous,
          );
        }
      } else {
        // A polling transport failure is not a workflow terminal. Keep the
        // server-owned run state as running and visibly retry.
        setSlot((previous) =>
          previous.owner === owner &&
          previous.state.kind === "running" &&
          previous.state.ticket.run_id === ticket.run_id
            ? { owner, state: { ...previous.state, statusError: true } }
            : previous,
        );
      }

      if (traceResult.status === "fulfilled") {
        setSlot((previous) =>
          previous.owner === owner &&
          previous.state.kind === "running" &&
          previous.state.ticket.run_id === ticket.run_id
            ? { owner, state: { ...previous.state, trace: traceResult.value, traceError: false } }
            : previous,
        );
      } else {
        const missing = traceResult.reason instanceof ApiError && traceResult.reason.status === 404;
        setSlot((previous) =>
          previous.owner === owner &&
          previous.state.kind === "running" &&
          previous.state.ticket.run_id === ticket.run_id
            ? { owner, state: { ...previous.state, traceError: !missing } }
            : previous,
        );
      }

      if (!terminal && ownerRef.current === owner) {
        timer = window.setTimeout(() => void poll(), 2000);
      }
    };

    void poll();
    return () => {
      stopped = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [client, owner, ticket]);

  return { state, run };
}

/**
 * The honest wait. A run reads, drafts and parks a real Tier-2 approval before
 * it answers, which takes roughly half a minute per subject — so this states
 * that expectation AND counts the seconds actually elapsed. The elapsed count
 * is a measurement of this console's own wait, not a claim about the server.
 */
function RunningNotice({ subjects, startedAt }: { subjects: number; startedAt: number }) {
  const now = useNow(1000);
  const elapsed = Math.max(0, Math.floor((now - startedAt) / 1000));
  const what = subjects === 1 ? "1 subject" : `${subjects} subjects`;
  return (
    <div className="ne-workflows__running">
      {/* ONE text node, deliberately: a live region assembled from three JSX
          children announces in fragments and reads as three updates. */}
      <p className="ne-workflows__runningline" role="status">
        {`Running ${what} — ${elapsed}s elapsed`}
      </p>
      {/* WHAT THE RUN DOES, NOT WHAT IT WILL CONCLUDE. An earlier wording said
          the run "parks it for approval" — an outcome, promised before the run
          had one: a subject can come back `refused` or `failed` and park
          nothing, and the operator would have been told otherwise while the
          call was still open. The duration is named as an EXPECTATION, which is
          what it is; the elapsed count beside it is the only measured number
          here, and it measures this console's own wait. */}
      <p className="ne-workflows__runninghint">
        The run reads the records and drafts a write for each subject it selects, parking the ones
        it can for approval. Expect about 30 seconds per subject.
      </p>
    </div>
  );
}

/** The three counts the run reports about itself, rendered as the server sent
 * them. `formatCount` renders an absent count as the em dash — never as a zero
 * this console inferred from a key the server did not send. */
function RunCounts({ run }: { run: WorkflowRunView }) {
  const counts: ReadonlyArray<{ label: string; value: number | undefined }> = [
    { label: "Rows read", value: run.read_row_count },
    { label: "Subjects considered", value: run.subjects_considered },
    { label: "Subjects selected", value: run.subjects_selected },
  ];
  return (
    <dl className="ne-workflows__counts">
      {counts.map((count) => (
        <div className="ne-workflows__count" key={count.label}>
          <dt className="ne-workflows__countlabel">{count.label}</dt>
          <dd className="ne-workflows__countvalue">{formatCount(count.value)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A deterministic answer is server-owned data, distinct from the generic step
 * log and from the model-planned lane. Render every typed result surface the
 * server supplied; do not reinterpret a refusal as a failed run or a control
 * answer as the answer to the refused question. */
function DeterministicResult({ result }: { result: WorkflowResultView }) {
  const findings = result.findings;
  const missingFields = result.missing_fields ?? [];
  const controlEvidence = result.control_evidence_refs;
  const hasControl =
    result.control_outcome != null ||
    result.control_amount_cents != null ||
    (controlEvidence !== undefined && controlEvidence.length > 0);

  return (
    <section aria-label="Deterministic result">
      <h3>Deterministic result</h3>
      <dl className="ne-workflows__counts">
        <div className="ne-workflows__count">
          <dt className="ne-workflows__countlabel">Requested outcome</dt>
          <dd className="ne-workflows__countvalue">{result.outcome}</dd>
        </div>
        {result.policy_artifact_id == null ? null : (
          <div className="ne-workflows__count">
            <dt className="ne-workflows__countlabel">Policy artifact</dt>
            <dd className="ne-workflows__countvalue">{result.policy_artifact_id}</dd>
          </div>
        )}
        {result.policy_version == null ? null : (
          <div className="ne-workflows__count">
            <dt className="ne-workflows__countlabel">Policy version</dt>
            <dd className="ne-workflows__countvalue">{result.policy_version}</dd>
          </div>
        )}
      </dl>
      <p>{result.summary}</p>
      {missingFields.length === 0 ? null : (
        <div>
          <h4>Missing fields</h4>
          <ul aria-label="Missing fields">
            {missingFields.map((field) => (
              <li key={field}>{field}</li>
            ))}
          </ul>
        </div>
      )}
      {findings === undefined ? null : (
        <DataTable<WorkflowFindingView>
          columns={FINDING_COLUMNS}
          rows={findings}
          rowKey={(row) => `${row.subject_ref}:${row.related_ref ?? ""}`}
          label="Deterministic findings"
          things="deterministic findings"
          empty={<Empty message="This deterministic result reported no findings." />}
        />
      )}
      {hasControl ? (
        <section aria-label="Control result">
          <h4>Answerable control</h4>
          <dl className="ne-workflows__counts">
            <div className="ne-workflows__count">
              <dt className="ne-workflows__countlabel">Control outcome</dt>
              <dd className="ne-workflows__countvalue">{result.control_outcome ?? "—"}</dd>
            </div>
            <div className="ne-workflows__count">
              <dt className="ne-workflows__countlabel">Recorded-credit control total</dt>
              <dd className="ne-workflows__countvalue">
                {formatUsdCents(result.control_amount_cents)}
              </dd>
            </div>
          </dl>
          {controlEvidence === undefined || controlEvidence.length === 0 ? null : (
            <ul aria-label="Control evidence">
              {controlEvidence.map((reference) => (
                <li key={reference}>{reference}</li>
              ))}
            </ul>
          )}
        </section>
      ) : null}
    </section>
  );
}

/**
 * 015A0-ENFORCEMENT's fixed safe shell for a run whose source authority is no
 * longer current.
 *
 * A governed run reads customer records and its every output — steps, parked
 * writes, counts, the typed deterministic answer, the final answer text, the
 * served row identities — is derived from them. So the shell keeps only what
 * the run owns about itself: its trace id, and the fact that it ran. It offers
 * no trace link, because the trace is where the withheld detail lives; no
 * copy and no export; and no re-run control of its own.
 *
 * The two undisclosed kinds are named apart: `content_withheld` is the server's
 * settled decision, and anything else means it stated no decision this console
 * could read.
 */
function WithheldRunResult({ traceId, permanent }: { traceId: string; permanent: boolean }) {
  return (
    <div className="ne-workflows__result" aria-label="Withheld run result">
      <section className="ne-workflows__segment" aria-label="Terminal result">
        <h3>Answer withheld — access changed</h3>
        <p className="ne-workflows__withheld" role="status">
          {permanent
            ? "This run’s source access changed. Its answer, steps, parked writes and served identities are withheld."
            : "The server stated no disclosure decision for this run. Its answer, steps, parked writes and served identities are withheld."}
        </p>
        <dl className="ne-workflows__counts">
          <div className="ne-workflows__count">
            <dt className="ne-workflows__countlabel">Trace id</dt>
            <dd className="ne-workflows__countvalue ne-mono">{traceId}</dd>
          </div>
        </dl>
      </section>
    </div>
  );
}

function RunResult({ run }: { run: WorkflowRunView }) {
  const disclosure = sourceDisclosure(run);
  if (disclosure.kind !== "available") {
    return <WithheldRunResult traceId={run.trace_id} permanent={disclosure.kind === "withheld"} />;
  }
  const steps = run.steps ?? [];
  const proposals = run.proposals ?? [];
  return (
    <div className="ne-workflows__result">
      <div className="ne-workflows__resulthead">
        <RunCounts run={run} />
        <Link className="ne-btn ne-btn--quiet" to={`/trace/${encodeURIComponent(run.trace_id)}`}>
          View trace
        </Link>
      </div>
      {run.result == null ? null : (
        <>
          <p className="ne-visually-hidden" role="status">
            {`Workflow complete — deterministic outcome: ${run.result.outcome}.`}
          </p>
          <DeterministicResult result={run.result} />
        </>
      )}
      <DataTable<WorkflowStepView>
        columns={STEP_COLUMNS}
        rows={steps}
        rowKey={(row) => String(row.step)}
        label="Run steps"
        things="steps"
        empty={<Empty message="This run reported no steps." />}
      />
      <DataTable<WorkflowProposalView>
        columns={PROPOSAL_COLUMNS}
        rows={proposals}
        rowKey={(row) => row.subject_ref}
        label="Proposed writes"
        things="proposed writes"
        empty={<Empty message="This run parked no writes for approval." />}
      />
    </div>
  );
}

function AgenticRunningNotice({ run }: { run: Extract<AgenticRunState, { kind: "running" }> }) {
  const now = useNow(1000);
  const elapsed = Math.max(0, Math.floor((now - run.startedAt) / 1000));
  return (
    <div className="ne-workflows__running">
      <p className="ne-workflows__runningline" role="status">
        {`Model planning governed steps — ${elapsed}s elapsed`}
      </p>
      <p className="ne-workflows__runninghint">
        Every planned tool call still passes policy, guardrails, audit, and any required approval.
      </p>
      {run.statusError ? (
        <p role="status">
          Run status refresh failed; the run is still active and polling will retry.
        </p>
      ) : null}
      {run.traceError ? (
        <p role="status">
          Live trace refresh failed; the run is still active and polling will retry.
        </p>
      ) : null}
      <GovernedTrace trace={run.trace} traceId={run.ticket.trace_id} active />
    </div>
  );
}

function AgenticStartingNotice({ startedAt }: { startedAt: number }) {
  const now = useNow(1000);
  const elapsed = Math.max(0, Math.floor((now - startedAt) / 1000));
  return <p role="status">{`Starting governed run — ${elapsed}s elapsed`}</p>;
}

function findTraceNode(nodes: readonly TraceNodeView[], eventId: string): TraceNodeView | null {
  for (const node of nodes) {
    if (node.event_id === eventId) return node;
    const child = findTraceNode(node.children, eventId);
    if (child !== null) return child;
  }
  return null;
}

/**
 * The exact server assembly, with a real selection contract. `SpanTree` is an
 * interactive tree, so every selectable row opens the shared typed detail
 * pane; this surface never presents a click target backed by a no-op.
 */
function GovernedTrace({
  trace,
  traceId,
  active,
  traceError = false,
}: {
  trace: TraceAssemblyView | null;
  traceId: string;
  active: boolean;
  traceError?: boolean;
}) {
  const [selection, setSelection] = useState<{
    eventId: string;
    opener: HTMLElement;
  } | null>(null);
  const selected = useMemo(
    () =>
      trace !== null && selection !== null ? findTraceNode(trace.nodes, selection.eventId) : null,
    [selection, trace],
  );

  return (
    <section
      className="ne-workflows__segment ne-workflows__trace ne-trace"
      aria-label={active ? "Live governed trace" : "Governed trace"}
    >
      <div className="ne-workflows__segmenthead">
        <h3>Governed activity</h3>
        {active ? <p>Refreshes about every 2 seconds</p> : null}
      </div>
      {active ? (
        <p className="ne-trace__verdict">
          <span>Chain verification</span>
          <span aria-label="Verification unavailable while run is active">—</span>
        </p>
      ) : null}
      {traceError ? (
        <p className="ne-workflows__traceerror" role="status">
          The terminal result is available, but the final trace refresh failed.{" "}
          <Link to={`/trace/${encodeURIComponent(traceId)}`}>Open full trace to retry</Link>.
        </p>
      ) : null}
      {trace === null && !traceError ? (
        <Empty
          message={
            active
              ? "Waiting for the first governed trace event."
              : "No governed trace assembly was served before this run completed."
          }
        />
      ) : trace === null ? null : (
        <div className="ne-trace__body">
          <SpanTree
            nodes={trace.nodes}
            rollup={trace.rollup}
            capability="interactive"
            selectedId={selected?.event_id ?? null}
            onSelect={(node, opener) => setSelection({ eventId: node.event_id, opener })}
          />
          {selected === null ? null : (
            <SpanPane
              node={selected}
              traceId={trace.trace_id}
              returnFocusTo={selection?.opener ?? null}
              onClose={() => setSelection(null)}
            />
          )}
        </div>
      )}
    </section>
  );
}

function AgenticRunResult({
  run,
  trace,
  traceError,
}: {
  run: AgenticWorkflowRunView;
  trace: TraceAssemblyView | null;
  traceError: boolean;
}) {
  const disclosure = sourceDisclosure(run);
  if (disclosure.kind !== "available") {
    // The governed trace is not rendered either: its nodes carry the same
    // source-derived detail the answer does.
    return <WithheldRunResult traceId={run.trace_id} permanent={disclosure.kind === "withheld"} />;
  }
  const steps = run.steps ?? [];
  const served = run.served_row_ids ?? [];
  const budget = run.budgets;
  return (
    <div className="ne-workflows__result" aria-label="Agentic run result">
      <GovernedTrace trace={trace} traceId={run.trace_id} active={false} traceError={traceError} />

      <section className="ne-workflows__segment" aria-label="Stated intents">
        <h3>Stated intents</h3>
        {steps.length === 0 ? (
          <Empty message="This run reached no governed tool step." />
        ) : (
          <ul className="ne-workflows__intents">
            {steps.map((step) => (
              <li className="ne-workflows__intent" key={step.step}>
                <p className="ne-workflows__intenttext">
                  <span className="ne-workflows__stepid">{`Step ${formatCount(step.step)}`}</span>
                  <span>{step.stated_intent}</span>
                </p>
                <dl className="ne-workflows__intentfacts">
                  <div>
                    <dt>Tool</dt>
                    <dd className="ne-mono">{step.tool}</dd>
                  </div>
                  <div>
                    <dt>Outcome</dt>
                    <dd>{step.outcome}</dd>
                  </div>
                </dl>
                {step.served_row_ids === undefined || step.served_row_ids.length === 0 ? null : (
                  <p className="ne-workflows__intentrefs ne-mono">
                    {`Served: ${step.served_row_ids.join(" · ")}`}
                  </p>
                )}
                {step.evidence_refs === undefined || step.evidence_refs.length === 0 ? null : (
                  <p className="ne-workflows__intentrefs ne-mono">
                    {`Evidence: ${step.evidence_refs.join(" · ")}`}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="ne-workflows__segment" aria-label="Terminal result">
        <div className="ne-workflows__resulthead">
          <div>
            <h3>Terminal result</h3>
            {/* The server's enum, in words. The raw token is not printed
                anywhere on this screen: `budget-exhausted` under a heading
                called "Terminal result" is the wire talking, not the run. */}
            <p>{`Outcome: ${outcomeWords(run.outcome)}`}</p>
          </div>
          <Link className="ne-btn ne-btn--quiet" to={`/trace/${encodeURIComponent(run.trace_id)}`}>
            View full trace
          </Link>
        </div>
        <dl className="ne-workflows__counts" aria-label="Agentic run budgets">
          <div className="ne-workflows__count">
            <dt className="ne-workflows__countlabel">Tool calls</dt>
            <dd className="ne-workflows__countvalue">
              {`${formatCount(budget.tool_calls_used ?? undefined)} / ${formatCount(budget.max_tool_calls)}`}
            </dd>
          </div>
          <div className="ne-workflows__count">
            <dt className="ne-workflows__countlabel">Tokens</dt>
            <dd className="ne-workflows__countvalue">
              {`${formatCount(budget.tokens_used ?? undefined)} / ${formatCount(budget.token_budget)}`}
            </dd>
          </div>
          <div className="ne-workflows__count">
            <dt className="ne-workflows__countlabel">Wall-clock cap</dt>
            <dd className="ne-workflows__countvalue">{`${budget.wall_clock_seconds}s`}</dd>
          </div>
          <div className="ne-workflows__count">
            <dt className="ne-workflows__countlabel">Parked-write cap</dt>
            <dd className="ne-workflows__countvalue">{formatCount(budget.max_parked_writes)}</dd>
          </div>
        </dl>
        {run.outcome === "complete" ? (
          <section aria-label="Final answer">
            <h4>Final answer</h4>
            <p>{run.answer ?? "The completed run returned no answer text."}</p>
          </section>
        ) : (
          <Empty message={stoppedRunMessage(run.outcome, budget.exhausted_cap)} />
        )}
        <section aria-label="Served evidence identities">
          <h4>Served evidence identities</h4>
          {served.length === 0 ? (
            <Empty message="This run served no row identities." />
          ) : (
            <ul className="ne-workflows__tools">
              {served.map((ref) => (
                <li className="ne-workflows__tool" key={ref}>
                  {ref}
                </li>
              ))}
            </ul>
          )}
        </section>
      </section>
    </div>
  );
}

// ===========================================================================
// The contract panel — what this workflow may touch, before it touches anything
// ===========================================================================

function ToolGrant({ label, tools }: { label: string; tools: readonly string[] }) {
  const labelId = useId();
  return (
    <div className="ne-workflows__grant">
      <h3 className="ne-workflows__grantlabel" id={labelId}>
        {label}
      </h3>
      {tools.length === 0 ? (
        <p className="ne-workflows__grantnone">None declared.</p>
      ) : (
        <ul className="ne-workflows__tools" aria-labelledby={labelId}>
          {tools.map((tool) => (
            <li className="ne-workflows__tool" key={tool}>
              {tool}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function WorkflowDetail({
  workflow,
  state,
  agenticState,
  busyWith,
  runBusy,
  onRun,
  onRunAgentic,
  requestedModel,
  onRequestedModelChange,
  modelChoiceRevision,
}: {
  workflow: WorkflowView;
  state: RunState;
  agenticState: AgenticRunState;
  /** The title of ANOTHER workflow whose run is still in flight, or null. */
  busyWith: string | null;
  runBusy: boolean;
  onRun: (subjects: number) => void;
  onRunAgentic: (requestedModel: string) => void;
  requestedModel: string | null;
  onRequestedModelChange: (requestedModel: string | null) => void;
  modelChoiceRevision: number;
}) {
  // Held as the TYPED string, not a number: coercing every keystroke back to a
  // number makes the field unclearable (emptying it snaps to 1, so the next
  // digit appends). What is typed is what is in the box.
  const [subjects, setSubjects] = useState("1");
  const subjectsId = useId();
  // THE WHOLE VALUE, NEVER A PREFIX OF IT. `Number.parseInt` reads as far as it
  // can and discards the rest, so a typed `1.5` — which `type="number"` accepts
  // — became a runnable `1`: the box said one thing and the run swept another,
  // with no way for the operator to see the difference. `Number` parses the
  // value or nothing (`1.5` stays 1.5 and fails the integer test; `12abc` is
  // NaN), so what the box shows is what the server is asked for.
  const parsed = Number(subjects.trim());
  // A run needs at least one whole subject to sweep — that is what the control
  // means, not a policy. The CEILING is the server's and is not guessed at
  // here: an over-ask is sent, and the server's own 422 is what says no.
  const runnable = subjects.trim() !== "" && Number.isInteger(parsed) && parsed >= 1;
  const running = state.kind === "running" && state.workflowId === workflow.workflow_id;
  // A result belongs to the workflow it was run for and to no other.
  const mine = state.kind !== "idle" && state.workflowId === workflow.workflow_id ? state : null;
  const agenticMine =
    agenticState.kind !== "idle" && agenticState.workflowId === workflow.workflow_id
      ? agenticState
      : null;
  const agenticMode = workflow.modes?.find((mode) => mode.mode === "agentic");

  return (
    <section className="ne-workflows__detail" aria-label={`Workflow: ${workflow.title}`}>
      <h2 className="ne-workflows__title">{workflow.title}</h2>
      <p className="ne-workflows__desc">{workflow.description}</p>

      <div className="ne-workflows__grants">
        <ToolGrant label="Reads" tools={workflow.reads} />
        <ToolGrant label="Writes" tools={workflow.writes} />
      </div>

      <div className="ne-workflows__run">
        <label className="ne-workflows__subjectlabel" htmlFor={subjectsId}>
          Subjects
        </label>
        <input
          className="ne-workflows__subjects"
          id={subjectsId}
          type="number"
          min={1}
          step={1}
          value={subjects}
          disabled={running}
          onChange={(event) => setSubjects(event.target.value)}
        />
        <PrimaryButton
          label={agenticMode ? "Run deterministic" : "Run workflow"}
          disabled={runBusy || busyWith !== null || !runnable}
          onClick={() => onRun(parsed)}
        />
        {agenticMode?.status === "available" ? (
          <>
            <RunModelPicker
              surface="agentic-workflow"
              value={requestedModel}
              onChange={onRequestedModelChange}
              disabled={runBusy || busyWith !== null}
              invalidationKey={modelChoiceRevision}
            />
            <PrimaryButton
              label="Run agentic"
              disabled={runBusy || busyWith !== null || requestedModel === null}
              onClick={() => {
                if (requestedModel !== null) onRunAgentic(requestedModel);
              }}
            />
          </>
        ) : null}
      </div>
      {agenticMode?.status === "pending" ? (
        <p className="ne-workflows__busy">{agenticMode.reason ?? "Agentic mode is pending."}</p>
      ) : null}

      {/* A run holds this screen, not just its own row: the run state is one
          slot, and a second sweep started beside it would park a second set of
          Tier-2 approvals against the same accounts. Naming the workflow that
          holds it is what makes the closed control readable rather than broken. */}
      {busyWith === null ? null : (
        <p className="ne-workflows__busy" role="status">
          {`${busyWith} is still running — one run at a time.`}
        </p>
      )}
      {mine?.kind === "running" ? (
        <RunningNotice subjects={mine.subjects} startedAt={mine.startedAt} />
      ) : null}
      {mine?.kind === "denied" ? <PolicyDenied reason={mine.reason} /> : null}
      {mine?.kind === "error" ? (
        <RunFault line="Couldn’t run the workflow. Try again." detail={mine.detail} />
      ) : null}
      {mine?.kind === "done" ? <RunResult run={mine.run} /> : null}
      {agenticMine?.kind === "running" ? <AgenticRunningNotice run={agenticMine} /> : null}
      {agenticMine?.kind === "starting" ? (
        <AgenticStartingNotice startedAt={agenticMine.startedAt} />
      ) : null}
      {agenticMine?.kind === "denied" ? <PolicyDenied reason={agenticMine.reason} /> : null}
      {agenticMine?.kind === "error" ? (
        <RunFault line="Couldn’t start the run. Try again." detail={agenticMine.detail} />
      ) : null}
      {agenticMine?.kind === "failed" ? (
        <RunFault
          line="The agentic run failed."
          detail={agenticFailureDetail(agenticMine.errorClass)}
        />
      ) : null}
      {agenticMine?.kind === "done" ? (
        <AgenticRunResult
          run={agenticMine.run}
          trace={agenticMine.trace}
          traceError={agenticMine.traceError}
        />
      ) : null}
    </section>
  );
}

// ===========================================================================
// The screen
// ===========================================================================

export function WorkflowsPage() {
  const client = useApiClient();
  const principal = usePrincipal();
  const advisory = entitlementsOf(principal);
  const principalKey = `${principal.status}:${principal.principal?.id ?? ""}`;
  const owner = useMemo(() => ({ client, principalKey }), [client, principalKey]);
  const currentOwnerRef = useRef(owner);
  useLayoutEffect(() => {
    currentOwnerRef.current = owner;
  }, [owner]);
  // A DEPLOYMENT WITH THE ENGINE OFF IS NOT A BROKEN ONE. `workflows_enabled`
  // ships FALSE, and both routes then 404 — so the 404 is the shipped default,
  // not an edge. `useGovernedRead` separates 403 from every other failure and
  // collapses the rest into "error", which would render this healthy server as
  // "Couldn't load workflows." above a Retry that can never succeed. The 404 is
  // therefore resolved HERE, into a served value of `null` meaning "offered
  // nothing", and the read's error arm keeps meaning what it says.
  //
  // THE STATUS DISCRIMINATES, NOT THE `detail` STRING — deliberately, and the
  // copy is written to what that supports. `detail` is server prose with no
  // machine-readable code behind it, so matching on it would be a guess that
  // breaks on rewording. A 404 from anywhere else (a stale proxy route, a base
  // path that no longer resolves) is therefore ALSO reported as "not
  // available": the screen states that this deployment served nothing at this
  // route, which is true in every one of those cases, and does NOT claim the
  // cause — a flag it cannot read. Pinned in both directions by
  // `states the feature-off 404 …`, whose 404 fixture deliberately carries an
  // UNRELATED detail so the guard cannot pass by matching prose.
  const read = useCallback(async (): Promise<WorkflowView[] | null> => {
    try {
      return await client.listWorkflows();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 404) return null;
      throw err;
    }
  }, [client]);
  const workflows = useGovernedRead<WorkflowView[] | null>(read, advisory.refused("ops"), owner);
  const { state, run } = useRunWorkflow(client, owner);
  const [uiSlot, setUiSlot] = useState<{
    owner: object;
    requestedModel: string | null;
    modelChoiceRevision: number;
    openId: string | null;
  }>({ owner, requestedModel: null, modelChoiceRevision: 0, openId: null });
  const ui =
    uiSlot.owner === owner
      ? uiSlot
      : { owner, requestedModel: null, modelChoiceRevision: 0, openId: null };
  const setRequestedModel = useCallback(
    (requestedModel: string | null) => {
      if (currentOwnerRef.current !== owner) return;
      setUiSlot((previous) => ({
        owner,
        requestedModel,
        modelChoiceRevision: previous.owner === owner ? previous.modelChoiceRevision : 0,
        openId: previous.owner === owner ? previous.openId : null,
      }));
    },
    [owner],
  );
  const invalidateModelChoices = useCallback(() => {
    if (currentOwnerRef.current !== owner) return;
    setUiSlot((previous) => ({
      owner,
      requestedModel: null,
      modelChoiceRevision: (previous.owner === owner ? previous.modelChoiceRevision : 0) + 1,
      openId: previous.owner === owner ? previous.openId : null,
    }));
  }, [owner]);
  const { state: agenticState, run: runAgentic } = useRunAgenticWorkflow(
    client,
    owner,
    invalidateModelChoices,
  );
  const { requestedModel, modelChoiceRevision, openId } = ui;

  const rows = useMemo(() => workflows.data ?? [], [workflows.data]);

  // The contract is the point of this screen, so the first SERVED workflow's is
  // open the moment the list lands — nobody should have to click to find out
  // what an agent is allowed to touch. DERIVED, not an effect: an effect would
  // paint one frame of a list with no contract beside it, which is the frame a
  // camera catches. This picks a row to SHOW; it decides nothing about what may
  // run, and the server still owns every refusal.
  const open = (openId === null ? rows[0] : rows.find((row) => row.workflow_id === openId)) ?? null;

  // SELECTING A ROW CHANGES WHAT IS SHOWN AND NOTHING ELSE. A run is scoped to
  // the workflow it belongs to by `mine` below, so browsing away from one
  // already hides it — a reset here would ADD nothing to that and would take
  // something real away: a run costs about 30 seconds per subject and parks
  // Tier-2 approvals, so discarding its steps, its trace link and its approval
  // links because the operator looked at the next row would make re-reading
  // them cost a second governed sweep against the same accounts.
  const select = useCallback(
    (row: WorkflowView) => {
      if (currentOwnerRef.current !== owner) return;
      setUiSlot((previous) => ({
        owner,
        requestedModel: previous.owner === owner ? previous.requestedModel : null,
        modelChoiceRevision: previous.owner === owner ? previous.modelChoiceRevision : 0,
        openId: row.workflow_id,
      }));
    },
    [owner],
  );

  const onRun = useCallback(
    (subjects: number) => {
      if (open === null) return;
      void run(open.workflow_id, subjects);
    },
    [open, run],
  );
  const onRunAgentic = useCallback(
    (requestedModel: string) => {
      if (open === null) return;
      void runAgentic(open.workflow_id, requestedModel);
    },
    [open, runAgentic],
  );

  // The title of the workflow whose run is in flight, when it is not the open
  // one — read off the SERVED list, never composed from the id.
  const busy =
    state.kind === "running"
      ? state.workflowId
      : agenticState.kind === "running" || agenticState.kind === "starting"
        ? agenticState.workflowId
        : null;
  const runBusy =
    state.kind === "running" || agenticState.kind === "running" || agenticState.kind === "starting";
  const busyWith =
    busy === null || busy === open?.workflow_id
      ? null
      : (rows.find((row) => row.workflow_id === busy)?.title ?? null);

  if (workflows.status === "refused") {
    return (
      <div className="ne-workflows">
        <ListScreen title="Workflows">
          <Refused thing="the workflow engine" page="Workflows" />
        </ListScreen>
      </div>
    );
  }

  // The server answered, and what it answered was nothing at this route. Stated
  // as the fact it is — NOT as an outage, and NOT as a cause this cannot see.
  if (workflows.status === "ok" && workflows.data === null) {
    return (
      <div className="ne-workflows">
        <ListScreen title="Workflows">
          <Empty message="The workflow engine is not available in this deployment." />
        </ListScreen>
      </div>
    );
  }

  const status: TableStatus =
    workflows.status === "loading" ? "loading" : workflows.status === "error" ? "error" : "ok";

  return (
    <div className="ne-workflows">
      <ListScreen title="Workflows">
        {/* A COUNT OF A WINDOW THAT HAS NOT ARRIVED IS A FABRICATED ZERO — and a
            window that FAILED to arrive has not arrived either. `rows` is empty
            on both, so the count is gated on the served status, not on loading
            alone (the ApprovalsPage / AuditPage / EvalsPage form). */}
        <Toolbar count={status === "ok" ? `${formatCount(rows.length)} workflows` : null} />
        <DataTable<WorkflowView>
          columns={WORKFLOW_COLUMNS}
          rows={rows}
          rowKey={(row) => row.workflow_id}
          label="Workflows"
          things="workflows"
          status={status}
          selectedKey={openId}
          onRowActivate={select}
          empty={<Empty message="This deployment offers no workflows." />}
          error={<LoadError things="workflows" onRetry={workflows.reload} />}
        />
        {open === null ? null : (
          <WorkflowDetail
            workflow={open}
            state={state}
            agenticState={agenticState}
            busyWith={busyWith}
            runBusy={runBusy}
            onRun={onRun}
            onRunAgentic={onRunAgentic}
            requestedModel={requestedModel}
            onRequestedModelChange={setRequestedModel}
            modelChoiceRevision={modelChoiceRevision}
          />
        )}
      </ListScreen>
    </div>
  );
}
