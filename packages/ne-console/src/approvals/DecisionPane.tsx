// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §8 — THE DECISION PANE, and the receipt it converts into.
//
// Six blocks, in the spec's order: header · what happens if approved ·
// unconfirmed facts · request facts · actions · in-flight. The pane never
// navigates away from the list, so "open a row, read it, decide, open the next"
// costs two keys rather than two page loads — the second week of use.
//
// THE PAYLOAD IS REVEALED, NOT DELIVERED. "What happens if approved" is a
// separate governed read, gated server-side to the named approver(s) for this
// request and audited on every call — so the block opens as a Reveal control and
// fetches nothing until it is pressed, and it renders the server's answer
// (a payload, or a refusal in the server's own words) and never its own.
//
// THE RECEIPT IS A STATE, NOT A TOAST. On a decision the pane converts IN PLACE:
// the verdict the server returned, the approver the SERVER derived (this console
// never sends one), the time it stamped, and the chain link once the shared poll
// resolves it. A toast would take the one artifact the approver is accountable
// for and remove it from the screen after four seconds.
//
// WHAT IS NOT HERE: no bulk anything. §8 rules it out permanently — each approval
// is an individually accountable decision with its own confirm and its own
// reason, and a select-all-approve control is the exact failure a gate exists to
// prevent.

import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { Link } from "react-router";
import type { ApprovalActionResult, PendingApprovalView } from "../api/types";
import { useApprovalDecisionAudit } from "../hooks/useAuditPoll";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { StatusPill } from "../primitives/StatePill";
import { ABSENT, absoluteTime } from "../primitives/format";
import { PrincipalLabel } from "../primitives/PrincipalLabel";
import { Loading, LoadError, Refused } from "../primitives/states";
import {
  approveWithheld,
  disclosedFields,
  refLabel,
  refProposalHref,
  sourceDetailWithheld,
  unconfirmedFacts,
} from "./detail";
import type { Disclosure } from "./detail";
import { useDisclosure } from "./useDisclosure";
import { isExpired, statusWord } from "./queue";
import { buildReceipt, verdictWord } from "./receipt";
import { ARREST_CLASS, motionClass, receiptMotion, useVerdictArrival } from "./verdictMotion";
import { useApprovalOwner } from "./approvalOwner";

/** Everything the screen knows about a submission in flight or already answered. */
export interface DecisionState {
  result?: ApprovalActionResult;
  /** A write that failed. `conflict` is the gate's "already decided" answer. */
  error?: { detail: string | null; conflict: boolean };
  /** Wall-clock stamp of the submit, so the wait is counted rather than guessed. */
  startedAt?: number;
}

export interface DecisionPaneProps {
  approval: PendingApprovalView;
  now: number;
  state: DecisionState;
  /** The row is no longer in the queue and this console did not decide it. */
  decidedElsewhere: boolean;
  /** Legacy newest-page hint retained for the existing parent binding. The
   * receipt deliberately ignores it and uses the exact server-owned lookup. */
  auditSeq: number | null;
  /** Whether this key can read the chain at all. False ⇒ no seq line, ever. */
  chainReadable: boolean;
  onClose: () => void;
  onApprove: () => void;
  onDeny: (reason: string) => void;
}

export function DecisionPane({
  approval,
  now,
  state,
  decidedElsewhere,
  chainReadable,
  onClose,
  onApprove,
  onDeny,
}: DecisionPaneProps) {
  const owner = useApprovalOwner();
  const result = state.result;
  const awaitingSecond = result?.status === "pending_second";
  const decided = result !== undefined && !awaitingSecond;
  const word = statusWord(approval, now, awaitingSecond);
  // §4.5's arrival gate. True only for a pane that watched this request go from
  // undecided to answered — the verdict grammar dresses an answer that LANDED,
  // never one that was already on screen when the pane opened.
  const arrived = useVerdictArrival(approval.request_id, result !== undefined);
  // Bound to THIS request id: the pane is one component that re-renders for
  // whichever row is open, and a disclosure that outlived its request would be
  // read under the next one.
  const { disclosure, reveal } = useDisclosure(approval.request_id);

  return (
    <DetailPane
      title={<span className="ne-mono ne-approvals__paneaction">{approval.action}</span>}
      subtitle={
        <span className="ne-approvals__panepills">
          <span className="ne-approvals__tier">TIER {approval.tier}</span>
          <StatusPill word={result ? verdictWord(result.status) : word} />
        </span>
      }
      onClose={onClose}
    >
      {decided ? (
        <Receipt
          approval={approval}
          result={result}
          chainReadable={chainReadable}
          arrived={arrived}
        />
      ) : (
        <PendingBody
          key={`${owner.generation}:${approval.request_id}`}
          approval={approval}
          now={now}
          state={state}
          awaitingSecond={awaitingSecond}
          decidedElsewhere={decidedElsewhere}
          disclosure={disclosure}
          arrived={arrived}
          onReveal={reveal}
          onApprove={onApprove}
          onDeny={onDeny}
        />
      )}
    </DetailPane>
  );
}

function PendingBody({
  approval,
  now,
  state,
  awaitingSecond,
  decidedElsewhere,
  disclosure,
  arrived,
  onReveal,
  onApprove,
  onDeny,
}: {
  approval: PendingApprovalView;
  now: number;
  state: DecisionState;
  awaitingSecond: boolean;
  decidedElsewhere: boolean;
  disclosure: Disclosure;
  arrived: boolean;
  onReveal: () => void;
  onApprove: () => void;
  onDeny: (reason: string) => void;
}) {
  return (
    <>
      <PayloadBlock disclosure={disclosure} onReveal={onReveal} />
      {disclosure.kind === "revealed" && disclosure.payload.run_context !== undefined ? (
        <p>
          <Link
            to={`/agents/${encodeURIComponent(disclosure.payload.run_context.agent_id)}/runs/${encodeURIComponent(disclosure.payload.run_context.run_id)}/approval/${encodeURIComponent(disclosure.payload.run_context.approval_id)}`}
          >
            Open inline Run approval
          </Link>
        </p>
      ) : null}
      <UnconfirmedBlock approval={approval} />
      <RequestFacts approval={approval} now={now} copyable={!sourceDetailWithheld(disclosure)} />
      {awaitingSecond ? (
        // An affirmation: the request moved forward by exactly one approver, so
        // it settles like any other (§4.5).
        <p
          className={motionClass(
            "ne-approvals__second",
            receiptMotion("AWAITING SECOND", arrived).verdict,
          )}
          role="status"
        >
          Approved by you. A second, different approver must decide.
        </p>
      ) : decidedElsewhere ? (
        // Somebody else's verdict closed this request out from under the reader.
        // A stop is a stop: it arrests (cl. 4).
        <p className={motionClass("ne-approvals__conflict", ARREST_CLASS)} role="status">
          Already decided{conflictClause(state.error?.detail)}.
        </p>
      ) : sourceDetailWithheld(disclosure) ? (
        // 015A0-ENFORCEMENT: the fixed safe shell. No Approve, no Deny, no
        // keyboard `a`/`d` — the Actions component is not rendered at all, so
        // there is no control here to disable and none to re-enable by state.
        <SafeShell disclosure={disclosure} />
      ) : (
        <Actions
          approval={approval}
          now={now}
          state={state}
          disclosure={disclosure}
          onApprove={onApprove}
          onDeny={onDeny}
        />
      )}
    </>
  );
}

/**
 * 015A0-ENFORCEMENT's fixed safe shell for a request whose source authority is
 * no longer current.
 *
 * FIXED means fixed: the two sentences below are the whole of it. Nothing here
 * is parameterised by the request, so there is no field through which a target,
 * a parameter, an evidence ref or a source label could reach the screen — not
 * even as a title attribute.
 *
 * The two states are named separately because they are different facts. A
 * permanent withholding is settled; an unresolved one may become determinate,
 * and telling an approver their request was withdrawn when the gate merely
 * could not resolve it is a false statement about their work.
 */
function SafeShell({ disclosure }: { disclosure: Disclosure }) {
  const permanent = disclosure.kind === "withheld";
  return (
    <div className="ne-approvals__actions">
      <p className="ne-approvals__withheld" role="status">
        {permanent
          ? "Content withheld — this request’s source access changed."
          : "Source access for this request could not be resolved."}
      </p>
      <p className="ne-approvals__withheld">
        {permanent
          ? "No decision can be made on it here."
          : "It stays undecided until the gate resolves it."}
      </p>
    </div>
  );
}

/**
 * §8 step 2, in four states and one heading.
 *
 * IDLE fetches nothing: the read is audited, so it happens when an operator
 * asks and at no other moment. LOADING and ERROR are §2.4's standard blocks
 * (the error's Retry re-runs the same read). REFUSED is the server's own
 * sentence, verbatim — this console never authors a denial, and never renders a
 * role verdict it decided itself. REVEALED is Target, then one labelled row per
 * material parameter with the value exactly as sent, then Reversibility.
 *
 * Every value here is server-served text rendered as INERT TEXT (C12) — the
 * payload is model-influenced content (tool arguments), never trusted markup.
 */
function PayloadBlock({ disclosure, onReveal }: { disclosure: Disclosure; onReveal: () => void }) {
  return (
    <section className="ne-approvals__block" aria-labelledby="ne-approvals-payload">
      <h3 className="ne-approvals__blocktitle" id="ne-approvals-payload">
        What happens if approved
      </h3>
      <PayloadBody disclosure={disclosure} onReveal={onReveal} />
    </section>
  );
}

function PayloadBody({ disclosure, onReveal }: { disclosure: Disclosure; onReveal: () => void }) {
  switch (disclosure.kind) {
    case "revealed":
      return (
        <DetailFields>
          {disclosedFields(disclosure.payload).map((field, index) => (
            // Index-keyed: a parameter whose key is literally "Target" must not
            // collide with the Target row and drop one of them from the pane.
            <DetailField key={`${index}:${field.label}`} label={field.label} mono={field.mono}>
              {field.value}
            </DetailField>
          ))}
        </DetailFields>
      );

    case "loading":
      return <Loading things="request details" />;

    case "refused":
      // The gate refused THIS approver this payload. Its words, or — when it
      // sent none — §2.4's refusal template, which is the only sentence this
      // console is allowed to supply and supplies nothing about the request.
      return disclosure.detail === null ? (
        <Refused thing="these request details" page="Approvals" />
      ) : (
        <div className="ne-state ne-state--refused ne-approvals__refusal" role="status">
          <p className="ne-state__line">{disclosure.detail}</p>
        </div>
      );

    case "withheld":
      // 015A0-ENFORCEMENT, permanent. The gate resolved this request's source
      // under the reader's CURRENT authority and will not disclose it. There is
      // no Retry: retrying a settled decision would only re-audit a read whose
      // answer is already known.
      return (
        <p className="ne-approvals__undisclosed" role="status">
          Content withheld.
        </p>
      );

    case "retryable_unknown":
      // 015A0-ENFORCEMENT, unresolved. Retry is offered because this answer CAN
      // change; the sentence claims nothing about the reader's access, which is
      // not what the gate reported.
      return (
        <>
          <p className="ne-approvals__undisclosed" role="status">
            Request details couldn&rsquo;t be resolved for your current access.
          </p>
          <button type="button" className="ne-btn ne-btn--quiet" onClick={onReveal}>
            Try again
          </button>
        </>
      );

    case "error":
      return <LoadError things="request details" onRetry={onReveal} />;

    case "idle":
      return (
        <>
          <p className="ne-approvals__undisclosed">
            Request details aren&rsquo;t shown until you ask. Every read is audited.
          </p>
          <button type="button" className="ne-btn ne-btn--quiet" onClick={onReveal}>
            Reveal
          </button>
        </>
      );
  }
}

/** §8 step 3. Absent from the wire ⇒ nothing renders. No placeholder, no zero. */
function UnconfirmedBlock({ approval }: { approval: PendingApprovalView }) {
  const facts = unconfirmedFacts(approval);
  if (facts === null) return null;
  return (
    <section className="ne-approvals__block">
      <p className="ne-approvals__warn">
        <span aria-hidden="true">⚠</span> {facts.count} unconfirmed extracted fact
        {facts.count === 1 ? "" : "s"} in this request&rsquo;s context.
      </p>
      {facts.refs.length > 0 ? (
        <ul className="ne-approvals__refs">
          {facts.refs.map((ref) => {
            const href = refProposalHref(ref);
            return (
              <li key={ref} title={ref}>
                {href === null ? (
                  <span className="ne-mono">{refLabel(ref)}</span>
                ) : (
                  <Link className="ne-mono" to={href}>
                    {refLabel(ref)}
                  </Link>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}

/**
 * §8 step 4. Absolute times here — the table carries the relative ones.
 *
 * `copyable` is false in 015A0-ENFORCEMENT's safe shell. The request id itself
 * stays visible — it is payload-free queue metadata and the approver needs it
 * to ask about the request — but the shell offers no copy affordance, so the
 * drifted request presents no action of any kind.
 */
function RequestFacts({
  approval,
  now,
  copyable,
}: {
  approval: PendingApprovalView;
  now: number;
  copyable: boolean;
}) {
  return (
    <section className="ne-approvals__block">
      <DetailFields>
        <DetailField label="Requested by">
          <PrincipalLabel
            id={approval.principal_id}
            reference={{ scope: "approvals", reference: approval.request_id }}
          />
        </DetailField>
        <DetailField label="Requested at">
          <time dateTime={approval.requested_at}>{absoluteTime(approval.requested_at)}</time>
        </DetailField>
        <DetailField label="Due">
          <time dateTime={approval.sla_deadline}>{absoluteTime(approval.sla_deadline)}</time>
        </DetailField>
        <DetailField label="Expires">
          <time dateTime={approval.expires_at}>{absoluteTime(approval.expires_at)}</time>
        </DetailField>
        <DetailField label="Request id" mono>
          {copyable ? (
            <CopyValue value={approval.request_id} label="Copy request id" />
          ) : (
            <span className="ne-mono">{approval.request_id}</span>
          )}
        </DetailField>
      </DetailFields>
      {isExpired(approval, now) ? (
        <p className="ne-approvals__expired">This request expired before a decision.</p>
      ) : null}
    </section>
  );
}

/**
 * §8 step 5.
 *
 * Approve opens an inline confirm naming the consequence; Deny reveals a reason
 * field that Confirm deny will not fire without. Both are individually
 * accountable — there is no path here that decides more than one request.
 */
function Actions({
  approval,
  now,
  state,
  disclosure,
  onApprove,
  onDeny,
}: {
  approval: PendingApprovalView;
  now: number;
  state: DecisionState;
  disclosure: Disclosure;
  onApprove: () => void;
  onDeny: (reason: string) => void;
}) {
  const [mode, setMode] = useState<"idle" | "approve" | "deny">("idle");
  const [reason, setReason] = useState("");
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  const reasonRef = useRef<HTMLTextAreaElement | null>(null);

  const expired = isExpired(approval, now);
  // Keyed on THIS request's disclosure: a Tier-3 payload nobody has revealed
  // (or that the gate refused this approver) offers no Approve.
  const withheld = approveWithheld(approval, disclosure);
  const busy = state.startedAt !== undefined;

  // §8's `a` / `d`, bound HERE rather than plumbed down from the screen: the
  // control they open is this component's own state, and a decision key that
  // travels through a second component's props is a decision key that can
  // arrive one render late.
  //
  // The listener is on the window because the pane and the table trade focus —
  // `a` must work whether the reader is on the row or already in the pane — and
  // it declines every event whose target is a field, because the deny reason is
  // a textarea and a key that steals a keystroke from it corrupts a
  // justification.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key !== "a" && event.key !== "d") return;
      // §2.6's `g` chord completes on `a` (go to Approvals) and the shell
      // consumes that key on the document, which this listener sees first as a
      // defaulted-prevented event. A screen key that fires anyway would arm an
      // approval every time somebody navigated here by keyboard.
      if (event.defaultPrevented) return;
      const target = event.target;
      if (target instanceof HTMLElement) {
        const tag = target.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable) {
          return;
        }
      }
      if (event.key === "a" && (expired || withheld)) return;
      if (event.key === "d" && expired) return;
      event.preventDefault();
      setMode(event.key === "a" ? "approve" : "deny");
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [expired, withheld]);

  useEffect(() => {
    if (mode === "approve") confirmRef.current?.focus();
    if (mode === "deny") reasonRef.current?.focus();
  }, [mode]);

  // Esc closes the CONFIRM first, and only then the pane. The pane's own Esc
  // handler sits on an ancestor, so stopping propagation here is what makes the
  // two levels of Esc behave in the order the reader expects.
  const onKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      if (event.key !== "Escape" || mode === "idle") return;
      event.stopPropagation();
      setMode("idle");
    },
    [mode],
  );

  return (
    <div className="ne-approvals__actions" onKeyDown={onKeyDown}>
      {mode === "idle" ? (
        <div className="ne-approvals__buttons">
          <button
            type="button"
            className="ne-btn ne-btn--primary"
            disabled={expired || withheld || busy}
            onClick={() => setMode("approve")}
          >
            Approve
          </button>
          <button
            type="button"
            className="ne-btn ne-btn--quiet"
            disabled={expired || busy}
            onClick={() => setMode("deny")}
          >
            Deny
          </button>
        </div>
      ) : null}

      {withheld && mode === "idle" ? (
        <p className="ne-approvals__withheld">
          Approve is unavailable while the request details can&rsquo;t be shown.
        </p>
      ) : null}

      {mode === "approve" ? (
        <div className="ne-approvals__confirm">
          {approval.tier >= 3 ? (
            <p className="ne-approvals__tier3">
              Tier 3 — a second, different approver must also approve.
            </p>
          ) : null}
          <p className="ne-approvals__confirmline">Approve {approval.action}? This will execute.</p>
          <div className="ne-approvals__buttons">
            <button
              type="button"
              className="ne-btn ne-btn--primary"
              ref={confirmRef}
              disabled={busy}
              onClick={onApprove}
            >
              Approve
            </button>
            <button
              type="button"
              className="ne-btn ne-btn--quiet"
              disabled={busy}
              onClick={() => setMode("idle")}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {mode === "deny" ? (
        <div className="ne-approvals__confirm">
          <textarea
            className="ne-approvals__reason"
            ref={reasonRef}
            value={reason}
            rows={3}
            placeholder="Why are you denying this?"
            aria-label="Why are you denying this?"
            onChange={(event) => setReason(event.target.value)}
          />
          <div className="ne-approvals__buttons">
            <button
              type="button"
              className="ne-btn ne-btn--primary"
              disabled={busy || reason.trim() === ""}
              onClick={() => onDeny(reason.trim())}
            >
              Confirm deny
            </button>
            <button
              type="button"
              className="ne-btn ne-btn--quiet"
              disabled={busy}
              onClick={() => setMode("idle")}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {busy ? <Submitting startedAt={state.startedAt ?? null} /> : null}

      {/* A fault and a gate conflict both arrest (cl. 4), and cl. 2 forbids
          motion on a failed state outright. The class states both. */}
      {state.error && !state.error.conflict ? (
        <div className={motionClass("ne-state ne-state--error", ARREST_CLASS)} role="alert">
          <p className="ne-state__line">Couldn&rsquo;t submit the decision. Try again.</p>
          {state.error.detail ? <p className="ne-state__detail">{state.error.detail}</p> : null}
        </div>
      ) : null}

      {state.error?.conflict ? (
        <p className={motionClass("ne-approvals__conflict", ARREST_CLASS)} role="alert">
          Already decided{conflictClause(state.error.detail)}.
        </p>
      ) : null}
    </div>
  );
}

/**
 * What follows "Already decided", punctuated exactly once.
 *
 * THE LINE OWNS ITS OWN FULL STOP. The served sentence is appended verbatim
 * minus any trailing stop it brought with it — "Already decided — request
 * already decided.." is the console counting its own punctuation twice, and it
 * is on screen at the moment a second approver discovers they were beaten to
 * the decision. Nothing else about the string is touched, and a request the
 * server gave no reason for renders "Already decided." and stops there.
 */
function conflictClause(detail: string | null | undefined): string {
  const served = (detail ?? "").trim().replace(/\.+$/, "").trim();
  return served === "" ? "" : ` — ${served}`;
}

/** §8 step 6 — the wait is counted, not guessed. The clock is read in a timer
 * callback and held in state; it is never called during render. */
function Submitting({ startedAt }: { startedAt: number | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null) return;
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, [startedAt]);
  const seconds = startedAt === null ? 0 : Math.max(0, Math.floor((now - startedAt) / 1000));
  return (
    <p className="ne-approvals__submitting" role="status">
      Submitting… {seconds}s
    </p>
  );
}

/**
 * §4.5 + §4.6 — the receipt, and the one place the verdict grammar is visible.
 *
 * An APPROVED receipt settles into place and its audit-record line settles one
 * --motion-chain step later: decision first, record second, which is the
 * product's central claim taught in 260 ms without a sentence. A DENIED or
 * EXPIRED receipt arrests — both lines, instantly, at full strength. The reader
 * knows which one they are looking at before the word is read.
 *
 * Receipt linkage has four honest server-backed states. A lookup in flight is
 * only a lookup in flight; a 404 is an explicit miss; every other failure is an
 * unreadable lookup. None is converted into a claim that the gate is writing.
 */
function Receipt({
  approval,
  result,
  chainReadable,
  arrived,
}: {
  approval: PendingApprovalView;
  result: ApprovalActionResult;
  chainReadable: boolean;
  arrived: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const audit = useApprovalDecisionAudit(approval.request_id, chainReadable);
  const motion = receiptMotion(verdictWord(result.status), arrived);
  const copy = useCallback(() => {
    void navigator.clipboard?.writeText(buildReceipt(approval, result)).then(
      () => setCopied(true),
      () => {
        // The clipboard is unavailable in this context. The button does not
        // claim a copy that did not happen.
      },
    );
  }, [approval, result]);

  return (
    <div className="ne-approvals__receipt">
      <p className={motionClass("ne-approvals__verdict", motion.verdict)}>
        <StatusPill word={verdictWord(result.status)} />
        <span className="ne-approvals__verdictby">
          by {result.approver ?? ABSENT} at{" "}
          {result.decided_at ? (
            <time dateTime={result.decided_at}>{absoluteTime(result.decided_at)}</time>
          ) : (
            ABSENT
          )}
        </span>
      </p>

      {audit.status === "checking" ? (
        <p className="ne-approvals__auditlink" role="status">
          Checking the decision audit entry…
        </p>
      ) : audit.status === "found" ? (
        <p className={motionClass("ne-approvals__auditlink", motion.record)}>
          <Link to={`/audit?seq=${audit.seq}`}>View audit entry</Link>
        </p>
      ) : audit.status === "not_found" ? (
        <p className="ne-approvals__auditlink" role="status">
          No decision audit entry was found for this request.
        </p>
      ) : audit.status === "failure" ? (
        <p className="ne-approvals__auditlink" role="alert">
          Couldn&rsquo;t check the decision audit entry.
        </p>
      ) : null}

      <div className="ne-approvals__buttons">
        <button type="button" className="ne-btn ne-btn--quiet" onClick={copy}>
          Copy receipt
        </button>
        {copied ? (
          <span className="ne-approvals__copied" role="status">
            Copied
          </span>
        ) : null}
      </div>
    </div>
  );
}

/** A full identifier with a copy affordance. Not <HashChip>: that truncates to
 * eight characters by §2.7's hash rule, which would mangle a request id — the
 * one string on this pane an approver quotes to someone else. */
function CopyValue({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const copy = useCallback(() => {
    void navigator.clipboard?.writeText(value).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      },
      () => {},
    );
  }, [value]);
  return (
    <span className="ne-approvals__copyvalue">
      <span className="ne-mono">{value}</span>
      <button type="button" className="ne-hashchip__copy" aria-label={label} onClick={copy}>
        <span aria-hidden="true">{copied ? "✓" : "⧉"}</span>
      </button>
    </span>
  );
}
