// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.4 / §13b — the selected span, as the shared detail pane: 420px, Esc closes,
// focus-trapped, and it never navigates away from the run.
//
// Every value is a server field rendered verbatim (C6/C7); the formatters
// reformat what already arrived and derive nothing. Redaction is rendered
// shape-not-value — the category and the count, never a redacted value. A kind
// this pane does not recognize renders only its safe event metadata. Arbitrary
// raw detail stays withheld: an unknown emitter must not turn the inspector
// into a payload or hidden-reasoning escape hatch.

import type { ReactNode } from "react";
import type { TraceNodeView } from "../api/types";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { HashChip } from "../primitives/HashChip";
import {
  ABSENT,
  absoluteTime,
  formatCount,
  formatDuration,
  formatMoney,
} from "../primitives/format";
import { formatTokens, kindLabel, nodeName, routingFact } from "./format";

/** §2.1 column resilience, in the pane: an absent field renders the em dash and
 * nothing else — never a blank row that reads as a rendering fault. */
function Field({ label, children, mono }: { label: string; children: ReactNode; mono?: boolean }) {
  const absent = children === null || children === undefined || children === "";
  return (
    <DetailField label={label} mono={mono}>
      {absent ? <span className="ne-absent">{ABSENT}</span> : children}
    </DetailField>
  );
}

function Time({ iso }: { iso: string }) {
  return <time dateTime={iso}>{absoluteTime(iso)}</time>;
}

/**
 * The answer check's failure, as a fact rather than as a symbol.
 *
 * `grounding.error_class` is `type(exc).__name__`, written server-side
 * (ne-api `agentic_workflows.py::fail_agentic_run`, and the same shape on the
 * grounding gate). It is a PYTHON CLASS NAME: "UnknownTaskError" under a field
 * labelled "Error" tells an operator that something is wrong and nothing about
 * what, and it reads to a non-engineer as a crash rather than as the check
 * declining to run.
 *
 * So the pane states what happened in words and keeps the class name where
 * detail belongs — after the sentence, in parentheses, because it is the only
 * identity the server recorded and a reader forwarding this to an engineer
 * needs it verbatim. Nothing is invented: the console does not know what the
 * class MEANS and does not guess, it only says which stage stopped.
 */
function groundingErrorLine(errorClass: string | null): string | null {
  if (errorClass === null || errorClass === "") return null;
  return `The check did not complete (${errorClass})`;
}

/** The kind-typed block. Returns null for a kind with no typed span, so the
 * caller falls through to the server's raw detail. */
function kindFields(node: TraceNodeView): ReactNode {
  if (node.model_call) {
    const call = node.model_call;
    return (
      <>
        {/* The model that SERVED, as the server recorded it. Never the model a
            route declares for this task: the console holds no route table, and
            a call that fell back must not be dressed in the primary's name. */}
        <Field label="Requested model">{call.requested_model}</Field>
        <Field label="Served model">{call.model_name ?? call.response_model}</Field>
        <Field label="Task">{call.task}</Field>
        <Field label="Lane" mono>
          {call.lane}
        </Field>
        {/* Whether the route held. A record that stated no position renders the
            em dash — unknown, never "First attempt" (format.ts::routingFact). */}
        <Field label="Routing">{routingFact(call)?.text}</Field>
        {/* B12: formatDuration renders a served 0 as "0 ms" and only null as
            the em dash — a missing measurement and a measurement of zero are
            different facts, and this pane must not conflate them. */}
        <Field label="Latency">{formatDuration(call.latency_ms)}</Field>
        <Field label="First token">{formatDuration(call.ttft_ms)}</Field>
        <Field label="Cost">{formatMoney(call.cost_usd)}</Field>
        <Field label="Input tokens">{formatTokens(call.input_tokens)}</Field>
        <Field label="Output tokens">{formatTokens(call.output_tokens)}</Field>
      </>
    );
  }
  if (node.subagent) {
    const span = node.subagent;
    return (
      <>
        <Field label="Agent">{span.agent}</Field>
        <Field label="Depth">{span.depth === null ? null : formatCount(span.depth)}</Field>
        <Field label="Outcome">{span.outcome}</Field>
        <Field label="Duration">{formatDuration(span.duration_ms)}</Field>
        <Field label="Completed">{span.completed ? "Yes" : "No"}</Field>
        <Field label="Ended">{span.ended_at ? <Time iso={span.ended_at} /> : null}</Field>
      </>
    );
  }
  if (node.route) {
    const route = node.route;
    return (
      <>
        <Field label="Task">{route.route_task}</Field>
        <Field label="Lane" mono>
          {route.route_lane}
        </Field>
        <Field label="Tier">{route.tier}</Field>
        <Field label="Reason">{route.reason}</Field>
      </>
    );
  }
  if (node.pdp) {
    const pdp = node.pdp;
    return (
      <>
        <Field label="Effect">{pdp.effect}</Field>
        <Field label="Tier">{pdp.tier === null ? null : formatCount(pdp.tier)}</Field>
        <Field label="Reason">{pdp.reason}</Field>
        <Field label="Requested action" mono>
          {pdp.requested_action}
        </Field>
      </>
    );
  }
  if (node.approval) {
    const approval = node.approval;
    return (
      <>
        <Field label="Request" mono>
          {approval.approval_id}
        </Field>
        <Field label="Tier">{approval.tier === null ? null : formatCount(approval.tier)}</Field>
        <Field label="Decision">{approval.decision}</Field>
        <Field label="Subject" mono>
          {approval.subject_ref}
        </Field>
      </>
    );
  }
  if (node.knowledge) {
    const knowledge = node.knowledge;
    return (
      <>
        <Field label="Candidates">{formatCount(knowledge.candidates)}</Field>
        <Field label="Returned">{formatCount(knowledge.returned)}</Field>
        <Field label="Withheld by permissions">{formatCount(knowledge.acl_trim_delta)}</Field>
      </>
    );
  }
  if (node.redaction) {
    const entries = Object.entries(node.redaction.categories);
    return (
      <>
        <Field label="Lane" mono>
          {node.redaction.lane}
        </Field>
        <Field label="Replaced">
          {entries.length > 0
            ? entries.map(([category, count]) => `${category} (${count})`).join(" · ")
            : formatCount(node.redaction.hits)}
        </Field>
      </>
    );
  }
  if (node.guardrail) {
    return <Field label="Detail">{node.guardrail.detail}</Field>;
  }
  if (node.tool) {
    // The execution→approval join. The authorised-by line renders ONLY when
    // the server recorded which approval this execution consumed; a record
    // without the field (pre-join history, ungated tier-0/1 writes) shows no
    // line — the console never infers a join from adjacent approval spans.
    // The id is the server's durable approval request id, full value in
    // `title` and on the copy button (§2.7).
    // Template / arguments / rows are the server's typed lift of the
    // executor's redacted detail: the template id, the argument NAMES (values
    // never cross the chain), and the row count — a served 0 renders "0", an
    // absent count renders the dash (different facts).
    // `?.` / `== null`: an OLDER API (rolling upgrade) may not serialize the
    // lifted keys at all — absent renders the dash exactly like served-null;
    // the pane never crashes on, or invents, a field the server never sent.
    const tool = node.tool;
    return (
      <>
        {tool.approval_id ? (
          <Field label="Authorised by approval">
            <HashChip hash={tool.approval_id} label="Copy approval id" />
          </Field>
        ) : null}
        <Field label="Template" mono>
          {tool.template_id}
        </Field>
        <Field label="Arguments" mono>
          {tool.args?.length ? tool.args.join(", ") : null}
        </Field>
        <Field label="Rows returned">
          {tool.result_count == null ? null : formatCount(tool.result_count)}
        </Field>
        <Field label="Detail">{node.detail}</Field>
      </>
    );
  }
  if (node.verified_read) {
    // `== null` throughout: a key an older API omits (rolling upgrade) is the
    // same absent fact as a served null — the dash, never "No" or a NaN.
    const read = node.verified_read;
    return (
      <>
        <Field label="Rows returned">{read.rows == null ? null : formatCount(read.rows)}</Field>
        <Field label="Row limit">
          {read.row_limit == null ? null : formatCount(read.row_limit)}
        </Field>
        <Field label="Truncated">
          {read.truncated == null ? null : read.truncated ? "Yes" : "No"}
        </Field>
      </>
    );
  }
  if (node.grounding) {
    // `== null` on the verdict: an absent `adopted` (older API) renders the
    // dash — never "No", a verdict the server did not serve.
    const grounding = node.grounding;
    return (
      <>
        <Field label="Check" mono>
          {grounding.phase}
        </Field>
        <Field label="Rewrite used">
          {grounding.adopted == null ? null : grounding.adopted ? "Yes" : "No"}
        </Field>
        {/* Absent stays absent: a check that raised nothing renders the em
            dash, never a sentence about a failure that did not happen. */}
        <Field label="Error">{groundingErrorLine(grounding.error_class)}</Field>
      </>
    );
  }
  if (node.session) {
    // The conversation this run belongs to — the POINTER only. The chain
    // never carries what was asked or answered; those live in the governed
    // conversation record this id names.
    const session = node.session;
    return (
      <>
        <Field label="Agent">{session.agent}</Field>
        <Field label="Department">{session.department}</Field>
        <Field label="Conversation">
          {session.session_id ? (
            <HashChip hash={session.session_id} label="Copy conversation id" />
          ) : null}
        </Field>
        <Field label="Completed">
          {session.succeeded === null ? null : session.succeeded ? "Yes" : "No"}
        </Field>
      </>
    );
  }
  return null;
}

export function SpanPane({
  node,
  traceId,
  onClose,
  returnFocusTo,
}: {
  node: TraceNodeView;
  traceId: string;
  onClose: () => void;
  returnFocusTo: HTMLElement | null;
}) {
  const typed = kindFields(node);
  // A typed row that suppresses the internal action string names itself by
  // its kind ("Data read", "Answer check") rather than an empty title.
  const paneTitle = nodeName(node) || (kindLabel(node.kind) ?? "Event");
  return (
    <DetailPane
      title={paneTitle}
      subtitle={kindLabel(node.kind) ?? "Event"}
      onClose={onClose}
      returnFocusTo={returnFocusTo}
      focusKey={node.event_id}
    >
      <DetailFields>
        <Field label="When">
          <Time iso={node.occurred_at} />
        </Field>
        <Field label="Actor">{node.actor}</Field>
        <Field label="Action" mono>
          {node.action}
        </Field>
        <Field label="Resource" mono>
          {node.resource}
        </Field>
        <Field label="Outcome">{node.outcome}</Field>
        <Field label="Sequence">{formatCount(node.seq)}</Field>
        <Field label="Event">
          <HashChip hash={node.event_id} label="Copy event id" />
        </Field>
        <Field label="Run">
          <HashChip hash={traceId} label="Copy run id" />
        </Field>
        {typed}
        {typed === null ? (
          <Field label="Payload">Not displayed for an unclassified event.</Field>
        ) : null}
      </DetailFields>
    </DetailPane>
  );
}
