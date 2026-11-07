// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE RUN DEEP LINK — AND ONLY THAT.
//
// Round 4 moved the Run receipt into the workspace's docked pane, in place, so
// reading one no longer costs the conversation it came from. Nothing in the
// workspace links here any more. What still arrives at this URL is a link
// somebody else made: an audit entry, a notification, a pasted address. So this
// route survives as the STANDALONE FRAME for exactly the content that pane
// renders — `RunReceiptFields` and `RunReceiptExits`, the same components, not
// a second parallel description of a Run — plus the controls a receipt cannot
// hold: cancel, retry, and the plan/timeline disclosures an auditor comes here
// for.
//
// WHAT LEFT. The run-list rail (the workspace sidebar is the roster now), the
// "Back to Conversation" link and the crumb strip that duplicated it (the
// conversation-turns route is deleted; the crumb goes to the workspace with the
// conversation selected), and the four solid tab buttons, which are the
// underline strip the rest of the redesign uses.
//
// FROZEN DATA FLOW. This does not change how the workspace is read. Two real
// data gaps surfaced when the transcript adapter was built and are still NOT
// worked around:
//   * AgentRunEventWire carries no per-step verdict/tier/duration the way
//     Ask's ToolTransitEvent does, so a Run's work band renders its
//     server-projected SAFE PLAN as `allowed` steps with no tier and no
//     duration -- never a guessed verdict (adapters/runWorkspace.ts).
//   * A pending approval's gate carries no server-authored SENTENCE the way
//     a policy-decision-point tool call does (PendingApprovalView has no
//     reason/sentence field) -- the gate's body renders the transcript
//     module's own absent note rather than illustrative prose.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import type {
  AgentRunDetailWire,
  AgentRunEventMetadataWire,
  AgentRunEventWire,
  AgentRunStatusWire,
  PendingApprovalView,
} from "../api/types";
import {
  explicitEventGroups,
  reconcileRunEvents,
  runStatusPresentation,
  runTerminalOutcome,
} from "../agent-harness/adapters/runWorkspace";
import { eventActionLabel } from "../audit/eventClass";
import { humanizeProperty, readSchemaFields } from "./SchemaInputFields";
import { DetailPane } from "../primitives/DetailPane";
import { HashChip } from "../primitives/HashChip";
import { absoluteTime } from "../primitives/format";
import { Empty, LoadError, Loading, SaveError } from "../primitives/states";
import { Transcript } from "../transcript";
import { buildRunTurn } from "./runTurn";
import {
  RUN_RECEIPT_LABEL,
  RunReceiptExits,
  RunReceiptFields,
  runReceiptTitle,
} from "./RunReceiptPane";
import { UnderlineTabs } from "./UnderlineTabs";
import { strategyGloss, strategyWord } from "./StrategyBadge";
import type { AgentsClient, RunReceipt } from "./types";
import { EvidenceDrawer } from "./EvidenceDrawer";
import { verifiedRowAnchors } from "./rowAnchors";
import { ModelContinuityReceipt } from "./ModelContinuityReceipt";
import { RunApprovalPane } from "./RunApprovalPane";
import { useAgentsRead } from "./useAgentsRead";
import "./agents.css";

type Tab = "summary" | "plan" | "timeline";
const ACTIVE = new Set(["submitted", "queued", "running", "waiting_approval", "cancelling"]);
const RETRYABLE = new Set([
  "failed",
  "cancelled",
  "expired",
  "context_exhausted",
  "budget_exhausted",
]);

function controlBody(run: RunReceipt) {
  if (run.strategy === "unavailable") return null;
  return {
    expected_version: run.version,
    agent_version: run.agent_version,
    strategy: run.strategy,
    config_hash: run.config_hash,
    capability_snapshot_hash: run.capability_snapshot_hash,
  };
}

export function AgentRunPage({
  client,
  agentId,
  runId,
  identityKey,
}: {
  client: AgentsClient;
  agentId: string;
  runId: string;
  identityKey: string;
}) {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [tab, setTab] = useState<Tab>("summary");
  const [eventState, setEventState] = useState<{
    client: object;
    identityKey: string;
    agentId: string;
    runId: string;
    items: AgentRunEventWire[];
    failed: boolean;
  }>({ client, identityKey, agentId, runId, items: [], failed: false });
  const [followLive, setFollowLive] = useState(true);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [writing, setWriting] = useState<"cancel" | "retry" | null>(null);
  const [revision, setRevision] = useState(0);
  const evidenceTrigger = useRef<HTMLButtonElement>(null);
  const governanceTrigger = useRef<HTMLButtonElement>(null);
  const load = useCallback(async () => {
    const run = await client.getAgentRun(agentId, runId);
    const [agent, conversation, envelope] = await Promise.all([
      client.getAgent(agentId),
      client.getConversation(agentId, run.conversation_id),
      // The evidence envelope, for the receipt's row-identity field. Optional
      // and fail-open to ABSENCE: a deployment without the read, or a read
      // that fails, draws no anchors — never a receipt that fails to load
      // over a field it can honestly omit.
      client.getAgentRunEvidence
        ? client.getAgentRunEvidence(agentId, runId).catch(() => null)
        : Promise.resolve(null),
    ]);
    return { run, agent, conversation, envelope };
  }, [agentId, client, runId]);
  const [read, retryRead] = useAgentsRead(load, `${identityKey}:${revision}`);

  const eventStateIsCurrent =
    eventState.client === client &&
    eventState.identityKey === identityKey &&
    eventState.agentId === agentId &&
    eventState.runId === runId;
  const events = eventStateIsCurrent ? eventState.items : [];
  const eventsFailed = eventStateIsCurrent ? eventState.failed : false;

  useEffect(() => {
    let active = true;
    const owner = { client, identityKey, agentId, runId };
    const poll = () =>
      void client.listAgentRunEvents(agentId, runId).then(
        (page) => {
          if (active) {
            setEventState((current) => ({
              ...owner,
              items:
                current.client === client &&
                current.identityKey === identityKey &&
                current.agentId === agentId &&
                current.runId === runId
                  ? reconcileRunEvents(current.items, page.items)
                  : reconcileRunEvents([], page.items),
              failed: false,
            }));
          }
        },
        () => {
          if (active)
            setEventState((current) => ({
              ...owner,
              items:
                current.client === client &&
                current.identityKey === identityKey &&
                current.agentId === agentId &&
                current.runId === runId
                  ? current.items
                  : [],
              failed: true,
            }));
        },
      );
    poll();
    const timer = window.setInterval(poll, 2_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [agentId, client, runId, identityKey]);

  const priorStatus = useRef<string | null>(null);
  useEffect(() => {
    priorStatus.current = null;
  }, [agentId, client, identityKey, runId]);
  const currentStatus: AgentRunStatusWire | null =
    read.phase === "ready" ? read.value.run.status : null;
  useEffect(() => {
    if (currentStatus === null) return;
    if (priorStatus.current !== null && priorStatus.current !== currentStatus) {
      setAnnouncement(`Run status changed to ${currentStatus}.`);
    }
    priorStatus.current = currentStatus;
  }, [currentStatus]);
  const groups = useMemo(() => explicitEventGroups(events), [events]);

  // The run-list rail: every Run in the same Conversation, so outcome + retry
  // lineage render exactly as the server's sibling list says. A second call
  // to an already-exposed client method, not new plumbing (types.ts and
  // useAgentsRead.ts are untouched).
  const conversationId = read.phase === "ready" ? read.value.run.conversation_id : null;
  const [siblingState, setSiblingState] = useState<{
    client: object;
    identityKey: string;
    agentId: string;
    conversationId: string | null;
    items: readonly RunReceipt[];
  }>({ client, identityKey, agentId, conversationId: null, items: [] });
  useEffect(() => {
    if (conversationId === null) return;
    let active = true;
    void client.listConversationRuns(agentId, conversationId).then(
      (page) => {
        if (active)
          setSiblingState({ client, identityKey, agentId, conversationId, items: page.items });
      },
      () => {
        if (active) setSiblingState({ client, identityKey, agentId, conversationId, items: [] });
      },
    );
    return () => {
      active = false;
    };
  }, [client, agentId, identityKey, conversationId, revision]);
  const siblingRuns =
    siblingState.client === client &&
    siblingState.identityKey === identityKey &&
    siblingState.agentId === agentId &&
    siblingState.conversationId === conversationId
      ? siblingState.items
      : [];

  // The gate card's own display facts (tier, reference, decision-due line).
  // RunApprovalPane fetches the SAME list independently for the decide flow --
  // display-only duplication, not a new endpoint, and left that way rather
  // than lifting shared state across two mayTouch files under this unit's
  // time budget.
  const approvalId =
    read.phase === "ready" ? ((read.value.run as AgentRunDetailWire).approval_id ?? null) : null;
  const [approvalState, setApprovalState] = useState<{
    client: object;
    key: string;
    approval: PendingApprovalView | null;
  }>({ client, key: "", approval: null });
  useEffect(() => {
    const key = `${identityKey} ${agentId} ${runId} ${approvalId ?? ""}`;
    if (
      approvalId === null ||
      currentStatus !== "waiting_approval" ||
      !client.listPendingApprovals
    ) {
      let active = true;
      void Promise.resolve().then(() => {
        if (active) setApprovalState({ client, key, approval: null });
      });
      return () => {
        active = false;
      };
    }
    let active = true;
    void client.listPendingApprovals().then(
      ({ approvals }) => {
        if (!active) return;
        const match = approvals.find((candidate) => candidate.request_id === approvalId) ?? null;
        setApprovalState({ client, key, approval: match });
      },
      () => {
        if (active) setApprovalState({ client, key, approval: null });
      },
    );
    return () => {
      active = false;
    };
  }, [client, identityKey, agentId, runId, approvalId, currentStatus, revision]);
  const approvalKey = `${identityKey} ${agentId} ${runId} ${approvalId ?? ""}`;
  const pendingApproval =
    approvalState.client === client && approvalState.key === approvalKey
      ? approvalState.approval
      : null;

  if (read.phase === "loading") return <Loading things="Run workspace" />;
  if (read.phase === "unavailable") return <Empty message="This Run isn’t available." />;
  if (read.phase === "error") return <LoadError things="Run workspace" onRetry={retryRead} />;

  const { run, agent, conversation, envelope } = read.value;
  const detail = run as AgentRunDetailWire;
  const visibleAgenticPlan = detail.agentic_plan;
  const executionProfileReceipt = detail.execution_profile_receipt;
  const skillActivationReceipt = detail.skill_activation_receipt;
  const drawer = params.get("drawer");
  const focusedEvidenceId = params.get("evidence");
  const updateDrawer = (next: "evidence" | "governance" | null, evidence?: string) => {
    const updated = new URLSearchParams(params);
    if (next) updated.set("drawer", next);
    else updated.delete("drawer");
    if (evidence) updated.set("evidence", evidence);
    else if (!next) updated.delete("evidence");
    setParams(updated);
  };
  const submitControl = async (kind: "cancel" | "retry") => {
    const body = controlBody(run);
    if (body === null) return;
    setWriting(kind);
    setWriteError(null);
    try {
      const receipt = await (kind === "cancel"
        ? client.cancelAgentRun(agentId, runId, body, crypto.randomUUID())
        : client.retryAgentRun(agentId, runId, body, crypto.randomUUID()));
      if (kind === "retry")
        navigate(
          `/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(receipt.run_id)}`,
          { state: { predecessorRunId: runId } },
        );
      else setRevision((value) => value + 1);
    } catch {
      setWriteError(`Couldn’t ${kind} this Run.`);
    } finally {
      setWriting(null);
    }
  };

  // The conversation-turns route is deleted: the workspace IS where a
  // conversation is read, with the selection in the URL.
  const workspaceHref = `/agents/${encodeURIComponent(agentId)}?conversation=${encodeURIComponent(run.conversation_id)}`;
  const turn = buildRunTurn({
    run: detail,
    siblings: siblingRuns,
    approval: pendingApproval,
    // The one page that HOLDS the Run's events is the one page that can say
    // how a completed Run ended. The status alone cannot.
    terminalOutcome: runTerminalOutcome(events),
    gateAction:
      approvalId !== null ? (
        <Link
          className="ne-btn ne-btn--sm"
          to={`/approvals?request=${encodeURIComponent(approvalId)}`}
        >
          Review in Approvals
        </Link>
      ) : undefined,
    traceLink: (
      <Link className="tr-receipt__quiet" to={`/trace/${encodeURIComponent(run.run_id)}`}>
        Open trace
      </Link>
    ),
    receiptActions: (
      <span className="tr-receipt__fb">
        <button
          ref={evidenceTrigger}
          type="button"
          className="tr-receipt__quiet"
          onClick={() => updateDrawer("evidence")}
        >
          Evidence
        </button>
        <button
          ref={governanceTrigger}
          type="button"
          className="tr-receipt__quiet"
          onClick={() => updateDrawer("governance")}
        >
          Governance
        </button>
      </span>
    ),
  });

  return (
    <div className="ag-stage">
      <div className="ag-mast">
        <nav className="ag-crumbs" aria-label="Breadcrumb">
          <span>Execution</span>
          <span aria-hidden="true">›</span>
          <Link to="/agents/all">Agents</Link>
          <span aria-hidden="true">›</span>
          <Link to={workspaceHref}>{agent.label}</Link>
          <span aria-hidden="true">›</span>
          <b>{runReceiptTitle(detail)}</b>
        </nav>
        <div className="ag-titlerow">
          <h1 className="ag-titlerow__h1">{run.display_label}</h1>
          {run.strategy === "unavailable" ? null : (
            <span className="ag-strategy">
              <span className="ag-strategy__word">{strategyWord(run.strategy)}</span>
              <span className="ag-strategy__gloss">{strategyGloss(run.strategy)}</span>
            </span>
          )}
          <span className="ag-titlerow__spacer" />
          <span className="ag-titlerow__meta">
            {conversation.title} · Run {run.sequence}
          </span>
        </div>
        <UnderlineTabs
          label="Run"
          selected={tab}
          onSelect={setTab}
          tabs={[
            { id: "summary", label: "Summary" },
            { id: "plan", label: "Plan" },
            { id: "timeline", label: "Timeline" },
          ]}
        />
      </div>
      <div className="ag-main ag-main--pane">
        <div className="ag-thread">
          <div className="ag-panel">
            <RunApprovalPane
              client={client}
              approvalId={detail.approval_id}
              identityKey={identityKey}
              agentId={agentId}
              runId={runId}
              onRefetchRun={() => setRevision((value) => value + 1)}
            />
            <p className="ne-sr-only" aria-live="polite">
              {announcement}
            </p>
            {tab === "summary" ? (
              <section role="tabpanel" aria-label="Summary">
                <Transcript turns={[turn]} label="Run" />
              </section>
            ) : null}
            {tab === "plan" ? (
              <section role="tabpanel" aria-label="Plan">
                <h2>Plan</h2>
                {run.strategy === "deterministic" && executionProfileReceipt ? (
                  <>
                    <p>
                      <strong>Frozen deterministic execution receipt</strong>
                    </p>
                    <dl>
                      <div>
                        <dt>Workflow</dt>
                        <dd>{executionProfileReceipt.workflow_id}</dd>
                      </div>
                      <div>
                        <dt>Version</dt>
                        <dd>{executionProfileReceipt.profile_version}</dd>
                      </div>
                      <div>
                        <dt>Hash</dt>
                        <dd>
                          <HashChip
                            hash={executionProfileReceipt.profile_hash}
                            label="Copy execution profile hash"
                          />
                        </dd>
                      </div>
                      <div>
                        <dt>Attempt</dt>
                        <dd>
                          <HashChip
                            hash={executionProfileReceipt.attempt_id}
                            label="Copy attempt id"
                          />
                        </dd>
                      </div>
                      <div>
                        <dt>Receipt</dt>
                        <dd>
                          <HashChip
                            hash={executionProfileReceipt.receipt_hash}
                            label="Copy execution receipt hash"
                          />
                        </dd>
                      </div>
                    </dl>
                    <h3>Typed input schema</h3>
                    <SchemaReceipt schema={executionProfileReceipt.input_schema} />
                    <h3>Typed input</h3>
                    {executionProfileReceipt.typed_input_state === "available" &&
                    executionProfileReceipt.typed_input ? (
                      <TypedInputReceipt values={executionProfileReceipt.typed_input} />
                    ) : (
                      <p>Typed input unavailable.</p>
                    )}
                    <h3>Ordered fixed plan</h3>
                    {/* `ne-run-plan` suppresses the browser's own list marker. These rows
                        print the SERVER's `ordinal`, and inside a default <ol> the browser
                        added a second one -- rows read "1. 1. verified.invoke". The server's
                        number is the one that must survive: the agentic list below is
                        `visibleAgenticPlan`, i.e. FILTERED, so a positional marker would
                        disagree with the plan the run actually executed. */}
                    <ol className="ne-run-plan">
                      {executionProfileReceipt.ordered_plan.map((step) => (
                        <li key={step.step_id}>
                          {step.ordinal}. {step.action} — fixed workflow step
                        </li>
                      ))}
                    </ol>
                  </>
                ) : run.strategy === "deterministic" ? (
                  <p>
                    Deterministic execution receipt unavailable — no frozen exact-attempt profile
                    was served.
                  </p>
                ) : run.strategy === "agentic" ? (
                  <>
                    {skillActivationReceipt ? (
                      <section aria-label="Frozen Skill activation receipt">
                        <h3>Frozen Skill activation receipt</h3>
                        <dl>
                          <div>
                            <dt>Attempt</dt>
                            <dd>
                              <HashChip
                                hash={skillActivationReceipt.attempt_id}
                                label="Copy attempt id"
                              />
                            </dd>
                          </div>
                          <div>
                            <dt>Receipt</dt>
                            <dd>
                              <HashChip
                                hash={skillActivationReceipt.receipt_hash}
                                label="Copy Skill activation receipt hash"
                              />
                            </dd>
                          </div>
                          <div>
                            <dt>Index hash</dt>
                            <dd>
                              <HashChip
                                hash={skillActivationReceipt.index_hash}
                                label="Copy Skill index hash"
                              />
                            </dd>
                          </div>
                        </dl>
                        <ol>
                          {skillActivationReceipt.pins.map((pin) => (
                            <li key={`${pin.skill_id}:${pin.version}:${pin.content_hash}`}>
                              {pin.skill_id} v{pin.version} —{" "}
                              <HashChip
                                hash={pin.content_hash}
                                label={`Copy content hash for Skill ${pin.skill_id}`}
                              />
                            </li>
                          ))}
                        </ol>
                      </section>
                    ) : (
                      <p>Skill activation receipt unavailable.</p>
                    )}
                    {visibleAgenticPlan ? (
                      <ol className="ne-run-plan">
                        {visibleAgenticPlan.map((step) => (
                          <li key={`${step.ordinal}:${step.tool}`}>
                            {step.ordinal + 1}. {step.tool} — {step.step_intent}
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <p>
                        Model-selected plan unavailable — no persisted safe plan or intent is
                        present in the canonical Run projection.
                      </p>
                    )}
                  </>
                ) : (
                  <p>Plan unavailable.</p>
                )}
              </section>
            ) : null}
            {tab === "timeline" ? (
              <section role="tabpanel" aria-label="Timeline">
                <div className="ne-run-timeline-title">
                  <h2>Timeline</h2>
                  <button
                    type="button"
                    className="ne-btn ne-btn--quiet"
                    aria-pressed={followLive}
                    onClick={() => setFollowLive((value) => !value)}
                  >
                    Follow live
                  </button>
                </div>
                {eventsFailed ? (
                  <p role="status">Reconnecting to the canonical Run cursor…</p>
                ) : null}
                <ol className="ne-run-timeline">
                  {groups.map(({ event, children }) => (
                    <li key={event.event_id}>
                      <EventRow event={event} />
                      {children.length ? (
                        <ol aria-label={`Children of ${event.event_id}`}>
                          {children.map((child) => (
                            <li key={child.event_id}>
                              <EventRow event={child} />
                            </li>
                          ))}
                        </ol>
                      ) : null}
                    </li>
                  ))}
                </ol>
              </section>
            ) : null}
            {writeError ? <SaveError detail={writeError} /> : null}
          </div>
        </div>
        {/* THE SAME PANE CONTENT THE WORKSPACE DOCKS. Not a second description
         * of a Run: literally the same two components.
         *
         * It is titled by WHAT IT IS here, not by the Run. The page's own h1
         * and its breadcrumb already name the Run; a pane heading repeating the
         * served label a third time gave the screen two identical headings and
         * told the reader nothing new. In the workspace the pane carries the
         * Run's name instead, because there is no h1 there naming it. */}
        <DetailPane
          title={RUN_RECEIPT_LABEL}
          subtitle={<span className="ne-mono">{run.run_id}</span>}
          onClose={() => navigate(workspaceHref)}
          footer={
            <RunReceiptExits
              agentId={agentId}
              run={detail}
              extra={
                <>
                  {ACTIVE.has(run.status) && run.status !== "cancelling" ? (
                    <button
                      type="button"
                      className="ne-btn ne-btn--danger"
                      disabled={writing !== null}
                      onClick={() => void submitControl("cancel")}
                    >
                      Cancel Run
                    </button>
                  ) : null}
                  {RETRYABLE.has(run.status) ? (
                    <button
                      type="button"
                      className="ne-btn"
                      disabled={writing !== null}
                      onClick={() => void submitControl("retry")}
                    >
                      Retry as new Run
                    </button>
                  ) : null}
                  <Link className="ne-btn" to="/approvals">
                    Approval queue
                  </Link>
                </>
              }
            />
          }
        >
          <RunReceiptFields
            run={detail}
            agent={agent}
            conversation={conversation}
            rowAnchors={envelope === null ? null : verifiedRowAnchors(envelope)}
          />
        </DetailPane>
        {drawer === "evidence" || drawer === "governance" ? (
          <EvidenceDrawer
            client={client}
            agentId={agentId}
            runId={runId}
            view={drawer}
            focusedId={focusedEvidenceId}
            onFocus={(id) => updateDrawer("evidence", id)}
            onClose={() => updateDrawer(null)}
            trigger={drawer === "evidence" ? evidenceTrigger : governanceTrigger}
          />
        ) : null}
      </div>
    </div>
  );
}

/**
 * THE TIMELINE, IN WORDS — WITH THE SERVER'S OWN CODE STILL WITHIN REACH.
 *
 * `event_kind` and `event_status` are the SERVER's names for what happened
 * (`run.execution.claimed`, `receipt_pending`), and they led every row of the
 * one screen an auditor opens to reconstruct a decision. The code is not
 * dropped — it rides on each summary's `title`, exactly as /audit's own Action
 * cell keeps it — because a governance record that hides the identifier is
 * worse than one that shows only the identifier. What changed is what is READ.
 *
 * A VALUE THIS BUILD HAS NOT SEEN MUST STILL SAY SOMETHING. The fallback is
 * `eventActionLabel` (`audit/eventClass.ts`), the console's existing floor for
 * exactly this problem: it re-spaces the code's OWN segments — an unenumerated
 * `some.new.kind` reads "Some new kind" — invents nothing, and is total over
 * every string including `__proto__`, which a bare map index would hand to
 * React as `Object.prototype` and take the page down with. A second floor
 * written here would be the same rule said twice, in two places, drifting.
 */
type EventKind = AgentRunEventWire["event_kind"];
type EventStatus = AgentRunEventWire["event_status"];

/** Every kind `run_projection._SAFE_EVENT_KINDS` may serve. The `Record` over
 * the union is the point: a kind added to the wire fails `tsc` here rather than
 * reaching a reader unglossed.
 *
 * `event.unavailable` is NOT an event that failed. It is what `event_view`
 * clamps a kind outside its allowlist to, so the phrase says what the server
 * withheld and never that nothing happened. */
const EVENT_KIND_WORDS: Readonly<Record<EventKind, string>> = {
  "claim.verifier": "Claim checked by a verifier",
  "delivery.decided": "Delivery decided",
  "evidence.produced": "Evidence produced",
  "evidence.verified_row": "Evidence row verified",
  "model.changed": "Model changed",
  "model.served": "Model served",
  "run.audit.intent": "Audit receipt intent recorded",
  "run.cancel.accepted": "Cancellation accepted",
  "run.execution.claimed": "Execution claimed",
  "run.execution.recovered": "Execution recovered",
  "run.retry.accepted": "Retry accepted",
  "run.start.accepted": "Start accepted",
  "run.terminal": "Run reached its end state",
  "event.unavailable": "Kind not disclosed",
};

/** The event statuses that are NOT run statuses. The other twelve arms of
 * `event_status` ARE `AgentRunStatusWire`, and their words already exist in
 * `runStatusPresentation` — the run list and this timeline must not drift into
 * two spellings of "Awaiting approval". */
const STEP_STATUS_WORDS: Readonly<Record<Exclude<EventStatus, AgentRunStatusWire>, string>> = {
  // Recorded while the Run was still going — the writer's own name for "this is
  // not the terminal event" (`run_store`), not a claim about how it ended.
  preterminal: "Recorded mid-Run",
  changed: "Changed",
  served: "Served",
  withheld: "Withheld",
  receipt_pending: "Receipt pending",
  retry: "Retried",
  uncertain: "Uncertain",
  verified: "Verified",
};

/** Membership only, so the twelve run words stay in one place. Typed as a
 * `Record` over the union for the same reason as the map above: a new run
 * status has to be acknowledged here rather than silently missing its word. */
const RUN_STATUS_MEMBER: Readonly<Record<AgentRunStatusWire, true>> = {
  submitted: true,
  queued: true,
  running: true,
  waiting_approval: true,
  cancelling: true,
  completed: true,
  failed: true,
  cancelled: true,
  expired: true,
  context_exhausted: true,
  budget_exhausted: true,
  unavailable: true,
};

function isRunStatus(status: EventStatus): status is AgentRunStatusWire {
  // `Object.hasOwn`, not a bare index: `runStatusPresentation` indexes its own
  // map bare and would return `Object.prototype` for a `__proto__` status and
  // then read `.word` off it. The wire is cast, not validated, on arrival.
  return Object.hasOwn(RUN_STATUS_MEMBER, status);
}

function eventKindWords(kind: EventKind): string {
  const taught = Object.hasOwn(EVENT_KIND_WORDS, kind) ? EVENT_KIND_WORDS[kind] : undefined;
  return taught ?? eventActionLabel({ event_type: kind, action: null });
}

function eventStatusWords(status: EventStatus): string {
  if (isRunStatus(status)) return runStatusPresentation(status).word;
  const taught = Object.hasOwn(STEP_STATUS_WORDS, status) ? STEP_STATUS_WORDS[status] : undefined;
  return taught ?? eventActionLabel({ event_type: status, action: null });
}

/**
 * ONE METADATA VALUE, AND WHAT MAY NOT BE DONE TO IT.
 *
 * The key side is a LABEL and is de-snaked. The value side is the RECORD, so
 * the default is the server's bytes unchanged: a served model name, a lane, a
 * tool identifier and an audit action are what an auditor compares against
 * another system, and re-casing them would break that comparison silently.
 *
 * Two values are not that. A 64-hex digest is a `HashChip` like every other
 * digest on this page — nothing is lost, the full value stays in `title` and
 * the clipboard. (`run_projection._HASH_KEYS` drops any `*_hash` that is not
 * exactly 64 lowercase hex, so the suffix is the server's own rule, not a
 * guess — but the bool fast-path in `_safe_scalar` runs BEFORE that check, so
 * the type is tested first and a boolean on a hash key reads as a boolean.)
 * And a raw `true` is not a word: the key already asks the question.
 */
function metadataValue(field: AgentRunEventMetadataWire): ReactNode {
  if (typeof field.value === "boolean") return field.value ? "Yes" : "No";
  if (typeof field.value === "string" && field.key.endsWith("_hash")) {
    return <HashChip hash={field.value} label={`Copy ${field.key.replaceAll("_", " ")}`} />;
  }
  return String(field.value);
}

function EventRow({ event }: { event: AgentRunEventWire }) {
  const metadata = new Map(event.metadata.map((field) => [field.key, field.value]));
  const requestedModel = metadata.get("requested_model");
  const servedModel = metadata.get("served_model");
  return (
    <details>
      <summary>
        <span title={event.event_kind}>{eventKindWords(event.event_kind)}</span>{" "}
        <strong title={event.event_status}>{eventStatusWords(event.event_status)}</strong>{" "}
        <time dateTime={event.created_at}>{absoluteTime(event.created_at)}</time>
      </summary>
      <dl>
        <div>
          <dt>Sequence</dt>
          <dd>{event.sequence}</dd>
        </div>
        <div>
          <dt>Step</dt>
          <dd>{event.step_id ?? "Unknown"}</dd>
        </div>
        <div>
          <dt>Detail hash</dt>
          <dd>
            <HashChip hash={event.detail_hash} label="Copy event detail hash" />
          </dd>
        </div>
        {event.metadata.map((field) => (
          <div key={field.key}>
            <dt>{field.key.replaceAll("_", " ")}</dt>
            <dd>{metadataValue(field)}</dd>
          </div>
        ))}
      </dl>
      {event.event_kind === "model.served" ? (
        <dl
          className="ne-model-step-receipt"
          aria-label={`Model step ${event.step_id ?? "unknown"}`}
        >
          <div>
            <dt>Requested model</dt>
            <dd>{requestedModel ?? "Unavailable"}</dd>
          </div>
          <div>
            <dt>Served model</dt>
            <dd>{servedModel ?? "Unavailable"}</dd>
          </div>
        </dl>
      ) : null}
      <ModelContinuityReceipt event={event} />
    </details>
  );
}

/**
 * THE FROZEN SCHEMA, READ RATHER THAN DUMPED.
 *
 * This is a RECEIPT, so the exact server bytes may not be lost -- someone
 * reconstructing a decision needs the literal `input_schema` that was hashed,
 * not a rendering of it. But leading with `JSON.stringify(..., 2)` made the one
 * question a reader actually has here ("what could be typed in, and within what
 * bounds") the hardest thing on the page to answer.
 *
 * So: the readable table leads, and the exact JSON stays one disclosure away,
 * byte-identical. When the schema is a shape this console cannot read, there is
 * no table and the JSON is all there is -- which is the honest outcome, not a
 * degraded one.
 */
function SchemaReceipt({ schema }: { schema: Record<string, unknown> }) {
  const fields = readSchemaFields(schema);
  return (
    <>
      {fields === null || fields.length === 0 ? null : (
        <table className="ne-runform__frozen">
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Type</th>
              <th scope="col">Accepted</th>
            </tr>
          </thead>
          <tbody>
            {fields.map((field) => (
              <tr key={field.name}>
                <th scope="row">
                  {field.label}
                  <small className="ne-mono">{field.name}</small>
                </th>
                <td>{field.kind}</td>
                <td>
                  {field.kind === "integer" ? `${field.minimum}–${field.maximum}` : "true / false"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <details>
        <summary>Exact schema</summary>
        <pre>{JSON.stringify(schema, null, 2)}</pre>
      </details>
    </>
  );
}

/** The values actually submitted, as a table rather than a JSON literal. Same
 * rule as the schema above: readable first, exact bytes one disclosure away. */
function TypedInputReceipt({ values }: { values: Record<string, unknown> }) {
  const rows = Object.entries(values);
  if (rows.length === 0) return <p>This Run submitted no caller-supplied input.</p>;
  return (
    <>
      <table className="ne-runform__frozen">
        <thead>
          <tr>
            <th scope="col">Input</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, value]) => (
            <tr key={name}>
              <th scope="row">{humanizeProperty(name)}</th>
              <td>{typeof value === "object" ? JSON.stringify(value) : String(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <details>
        <summary>Exact input</summary>
        <pre>{JSON.stringify(values, null, 2)}</pre>
      </details>
    </>
  );
}
