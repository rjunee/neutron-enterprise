// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §11 — THE PROPOSAL DETAIL PANE.
//
// Progressive disclosure through the shared <DetailPane>: a row click opens it,
// Esc closes it, the list keeps its scroll position and its selection. It shows
// what §11 lists and nothing else — title · lane · cluster id · confidence ·
// evidence counts (ids behind a disclosure) · the diff · the actions.
//
// THE ARTIFACT IS INERT, ABSOLUTELY. `artifact.patch`, `artifact.pr_body` and a
// skill candidate's `body` are proposal-carried text that the server already
// sanitised. They reach the screen as PLAIN REACT TEXT NODES inside <pre> and
// nowhere else: no raw-HTML React prop, no innerHTML, no markdown parser, no
// re-sanitisation here. A skill body is additionally attacker-influenced content
// that an apply hands to every agent it matches, so it carries the spec's one
// warning line and is read as untrusted input.
//
// THIN CLIENT (C7). Which actions exist follows the SERVER's `status` and
// nothing else; a button's absence is a courtesy, legality is the server's. Every
// submission carries NO actor, principal, surface or session field — the server
// derives the steward from the verified bearer, and the returned actor and time
// render as sent. A decided proposal leaves the queue on the server's next
// answer, never by a local splice.

import { useState } from "react";
import type { ReviewEventView, RejectionCodeWire, StewardProposalView } from "../api/types";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { StatusPill } from "../primitives/StatePill";
import { absoluteTime } from "../primitives/format";
import { enumLabel, isSkillProposal, statusWord, WITHHELD_LABEL } from "./queue";
import type { QueueRowView } from "./queue";

/**
 * 015A0-ENFORCEMENT's fixed safe shell for a proposal the server did not
 * disclose.
 *
 * It renders the opaque proposal id, the kind-neutral status and the time — the
 * three fields SPEC keeps on the withheld branch — plus the literal
 * **Content withheld**. There is no diff, no evidence list, no skill body, no
 * approve, no reject, no record-apply and no copy: a decision cannot be taken
 * on a proposal whose contributors the server would not resolve, and this pane
 * offers none.
 *
 * The two undisclosed kinds are named apart. `content_withheld` is the server's
 * settled decision; anything else means the server stated no decision this
 * console could read, which is a different fact and is not asserted as a
 * refusal.
 */
function WithheldPane({ row, onClose }: { row: QueueRowView; onClose: () => void }) {
  const word = statusWord(row.status);
  return (
    <DetailPane
      title={WITHHELD_LABEL}
      subtitle={word === null ? enumLabel(row.status) : <StatusPill word={word} />}
      onClose={onClose}
    >
      <DetailFields>
        <DetailField label="Proposal id" mono>
          {row.proposalId}
        </DetailField>
        <DetailField label="Created">{absoluteTime(row.createdAt)}</DetailField>
      </DetailFields>
      <p className="ne-fly__withheld" role="status">
        {row.permanentlyWithheld
          ? "This proposal’s source access changed. Its content and every action on it are withheld."
          : "The server stated no disclosure decision for this proposal. Its content and every action on it are withheld."}
      </p>
    </DetailPane>
  );
}

/** §11's reject reason codes, in the spec's order. The value is the wire's; the
 * label is the spec's word. */
const REJECTION_CODES: ReadonlyArray<{ value: RejectionCodeWire; label: string }> = [
  { value: "incorrect", label: "Incorrect" },
  { value: "unsafe", label: "Unsafe" },
  { value: "duplicate", label: "Duplicate" },
  { value: "stale", label: "Stale" },
  { value: "out_of_scope", label: "Out of scope" },
  { value: "other", label: "Other" },
];

/** §11's one warning line on a skill candidate's text, verbatim. */
export const SKILL_TEXT_WARNING =
  "⚠ On apply this text is added to every agent that matches it. Review it as untrusted input.";

export interface ProposalActionState {
  busy?: boolean;
  /** The server's own review event — the only account of who decided and when. */
  review?: ReviewEventView;
  /** A failed submission. `detail` is the server's string, rendered verbatim. */
  error?: { detail: string | null };
}

export interface SkillApplyDecision {
  aclLabels: string[];
  globalVisibility: boolean;
  note: string;
  edits: { name?: string; description?: string; triggers?: string[] } | undefined;
}

type Mode = "idle" | "reject" | "apply";

export function ProposalPane({
  row,
  state,
  decidedElsewhere,
  onClose,
  onApprove,
  onReject,
  onRecordApply,
  onApplySkill,
}: {
  row: QueueRowView;
  state: ProposalActionState;
  decidedElsewhere: boolean;
  onClose: () => void;
  onApprove: (note: string) => void;
  onReject: (code: RejectionCodeWire, note: string) => void;
  onRecordApply: (appliedRef: string, note: string) => void;
  onApplySkill: (decision: SkillApplyDecision) => void;
}) {
  const [mode, setMode] = useState<Mode>("idle");
  const [note, setNote] = useState("");
  const [rejectCode, setRejectCode] = useState<RejectionCodeWire>("incorrect");
  const [appliedRef, setAppliedRef] = useState("");

  // 015A0-ENFORCEMENT: an undisclosed row has no `proposal`, so nothing below
  // this branch can dereference an artifact, a diff, a skill body or an
  // evidence id — they are not on the object. The shell carries the opaque id,
  // the safe status and the time, and no action of any kind.
  const proposal = row.proposal;
  if (proposal === null) return <WithheldPane row={row} onClose={onClose} />;

  const { artifact, skill } = proposal;
  const word = statusWord(proposal.status);
  const isSkill = isSkillProposal(proposal);
  // The server's status is the whole rule. `proposed` can be approved or
  // rejected; `approved` can be applied or rejected.
  const canApprove = proposal.status === "proposed";
  const canReject = proposal.status === "proposed" || proposal.status === "approved";
  const canApply = proposal.status === "approved";

  function reset() {
    setMode("idle");
    setNote("");
    setAppliedRef("");
  }

  return (
    <DetailPane
      title={artifact.title}
      subtitle={word === null ? enumLabel(proposal.status) : <StatusPill word={word} />}
      onClose={onClose}
    >
      <DetailFields>
        <DetailField label="Lane">{enumLabel(proposal.lane)}</DetailField>
        <DetailField label="Kind">{enumLabel(proposal.kind)}</DetailField>
        <DetailField label="Cluster id" mono>
          {proposal.cluster_id}
        </DetailField>
        <DetailField label="Confidence">{proposal.confidence.toFixed(2)}</DetailField>
        <DetailField label="Priority">{enumLabel(proposal.priority)}</DetailField>
        <DetailField label="Evidence">
          <span>
            {proposal.evidence_event_ids.length} events · {proposal.evidence_signal_ids.length}{" "}
            signals
          </span>
          <details className="ne-fly__disclosure">
            <summary>Evidence ids</summary>
            <ul className="ne-fly__ids">
              {proposal.evidence_event_ids.map((id) => (
                <li key={`e-${id}`} className="ne-mono">
                  {id}
                </li>
              ))}
              {proposal.evidence_signal_ids.map((id) => (
                <li key={`s-${id}`} className="ne-mono">
                  {id}
                </li>
              ))}
            </ul>
          </details>
        </DetailField>
      </DetailFields>

      <section className="ne-fly__block">
        <h3 className="ne-fly__blocktitle">Diff</h3>
        {/* INERT: a plain text node inside <pre>. Never HTML, never markdown. */}
        <pre className="ne-fly__diff">{artifact.patch}</pre>
        <pre className="ne-fly__prbody">{artifact.pr_body}</pre>
      </section>

      {isSkill && skill ? (
        <section className="ne-fly__block">
          <h3 className="ne-fly__blocktitle">Skill candidate</h3>
          <DetailFields>
            <DetailField label="Name" mono>
              {skill.name}
            </DetailField>
            <DetailField label="Version">{skill.version}</DetailField>
            <DetailField label="Description">{skill.description}</DetailField>
            <DetailField label="Triggers">
              {skill.triggers.length === 0 ? null : (
                <ul className="ne-fly__ids">
                  {skill.triggers.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              )}
            </DetailField>
            <DetailField label="Suggested labels">
              {skill.suggested_acl_labels.length === 0
                ? null
                : skill.suggested_acl_labels.join(", ")}
            </DetailField>
            <DetailField label="Provenance">{skill.provenance_line}</DetailField>
          </DetailFields>
          <h3 className="ne-fly__blocktitle">Skill text</h3>
          <p className="ne-fly__warn">{SKILL_TEXT_WARNING}</p>
          {/* INERT, and adversarial: this is the text an apply injects. */}
          <pre className="ne-fly__skilltext">{skill.body}</pre>
        </section>
      ) : null}

      <section className="ne-fly__block ne-fly__actions">
        {state.review ? (
          <p className="ne-fly__reviewed">
            {state.review.action} by {state.review.actor} at{" "}
            <time dateTime={state.review.created_at}>{absoluteTime(state.review.created_at)}</time>
          </p>
        ) : null}

        {decidedElsewhere ? (
          <p className="ne-fly__reviewed" role="status">
            Already decided by another steward.
          </p>
        ) : null}

        {state.error ? (
          <div className="ne-state ne-state--error" role="alert">
            <p className="ne-state__line">Couldn&rsquo;t submit. Try again.</p>
            {state.error.detail ? <p className="ne-state__detail">{state.error.detail}</p> : null}
          </div>
        ) : null}

        {!decidedElsewhere && mode === "idle" && (canApprove || canReject || canApply) ? (
          <>
            <label className="ne-fly__field">
              <span className="ne-fly__fieldlabel">Note (optional)</span>
              <input
                type="text"
                className="ne-fly__input"
                value={note}
                onChange={(event) => setNote(event.target.value)}
              />
            </label>
            <div className="ne-fly__buttons">
              {canApprove ? (
                <button
                  type="button"
                  className="ne-btn ne-btn--primary"
                  disabled={state.busy}
                  onClick={() => onApprove(note)}
                >
                  Approve
                </button>
              ) : null}
              {canApply ? (
                <button
                  type="button"
                  className="ne-btn ne-btn--primary"
                  disabled={state.busy}
                  onClick={() => {
                    setNote("");
                    setMode("apply");
                  }}
                >
                  {isSkill ? "Apply" : "Record apply"}
                </button>
              ) : null}
              {canReject ? (
                <button
                  type="button"
                  className="ne-btn ne-btn--quiet"
                  disabled={state.busy}
                  onClick={() => {
                    setNote("");
                    setMode("reject");
                  }}
                >
                  Reject
                </button>
              ) : null}
            </div>
          </>
        ) : null}

        {!decidedElsewhere && mode === "reject" && canReject ? (
          <div className="ne-fly__form">
            <label className="ne-fly__field">
              <span className="ne-fly__fieldlabel">Reason</span>
              <select
                className="ne-fly__select"
                value={rejectCode}
                onChange={(event) => setRejectCode(event.target.value as RejectionCodeWire)}
              >
                {REJECTION_CODES.map((code) => (
                  <option key={code.value} value={code.value}>
                    {code.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="ne-fly__field">
              <span className="ne-fly__fieldlabel">Note (optional)</span>
              <input
                type="text"
                className="ne-fly__input"
                value={note}
                onChange={(event) => setNote(event.target.value)}
              />
            </label>
            <div className="ne-fly__buttons">
              <button
                type="button"
                className="ne-btn ne-btn--primary"
                disabled={state.busy}
                onClick={() => onReject(rejectCode, note)}
              >
                Reject
              </button>
              <button type="button" className="ne-btn ne-btn--quiet" onClick={reset}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {!decidedElsewhere && mode === "apply" && canApply && !isSkill ? (
          <div className="ne-fly__form">
            <label className="ne-fly__field">
              <span className="ne-fly__fieldlabel">Applied reference</span>
              <input
                type="text"
                className="ne-fly__input"
                value={appliedRef}
                placeholder="PR URL, commit, or bundle version"
                onChange={(event) => setAppliedRef(event.target.value)}
              />
            </label>
            <label className="ne-fly__field">
              <span className="ne-fly__fieldlabel">Note (optional)</span>
              <input
                type="text"
                className="ne-fly__input"
                value={note}
                onChange={(event) => setNote(event.target.value)}
              />
            </label>
            <div className="ne-fly__buttons">
              <button
                type="button"
                className="ne-btn ne-btn--primary"
                disabled={state.busy || appliedRef.trim() === ""}
                onClick={() => onRecordApply(appliedRef, note)}
              >
                Record apply
              </button>
              <button type="button" className="ne-btn ne-btn--quiet" onClick={reset}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {!decidedElsewhere && mode === "apply" && canApply && isSkill && skill ? (
          <SkillApplyForm
            skill={skill}
            kind={proposal.kind}
            busy={state.busy === true}
            note={note}
            onNote={setNote}
            onCancel={reset}
            onSubmit={onApplySkill}
          />
        ) : null}
      </section>
    </DetailPane>
  );
}

/**
 * §11's skill apply: the visibility decision IS the apply. Labels XOR everyone —
 * the same rule the server enforces, so the console cannot submit a body the
 * gate will refuse, and never both or neither.
 *
 * The label choices are the candidate's own SERVER-SUGGESTED labels. The wire
 * exposes no deployment-wide label vocabulary, so there is no larger set to
 * offer and none is invented here.
 */
function SkillApplyForm({
  skill,
  kind,
  busy,
  note,
  onNote,
  onCancel,
  onSubmit,
}: {
  skill: NonNullable<StewardProposalView["skill"]>;
  kind: string;
  busy: boolean;
  note: string;
  onNote: (next: string) => void;
  onCancel: () => void;
  onSubmit: (decision: SkillApplyDecision) => void;
}) {
  const [scope, setScope] = useState<"labels" | "everyone">("labels");
  const [checked, setChecked] = useState<readonly string[]>(skill.suggested_acl_labels);
  const [name, setName] = useState(skill.name);
  const [description, setDescription] = useState(skill.description);
  const [triggers, setTriggers] = useState(skill.triggers.join(", "));

  // An update candidate replaces the current version of an existing skill in
  // place; the server does not accept a rename, so the field is not offered.
  const renameable = kind !== "skill_update_candidate";
  const global = scope === "everyone";
  const labels = global ? [] : checked;
  const ready = labels.length > 0 !== global;

  const parsedTriggers = triggers
    .split(",")
    .map((t) => t.trim())
    .filter((t) => t !== "");

  function edits(): SkillApplyDecision["edits"] {
    const out: { name?: string; description?: string; triggers?: string[] } = {};
    if (renameable && name !== skill.name) out.name = name;
    if (description !== skill.description) out.description = description;
    if (JSON.stringify(parsedTriggers) !== JSON.stringify(skill.triggers)) {
      out.triggers = parsedTriggers;
    }
    return Object.keys(out).length === 0 ? undefined : out;
  }

  return (
    <div className="ne-fly__form">
      {renameable ? (
        <label className="ne-fly__field">
          <span className="ne-fly__fieldlabel">Name</span>
          <input
            type="text"
            className="ne-fly__input"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
      ) : null}
      <label className="ne-fly__field">
        <span className="ne-fly__fieldlabel">Description</span>
        <input
          type="text"
          className="ne-fly__input"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </label>
      <label className="ne-fly__field">
        <span className="ne-fly__fieldlabel">Triggers</span>
        <input
          type="text"
          className="ne-fly__input"
          value={triggers}
          onChange={(event) => setTriggers(event.target.value)}
        />
      </label>

      <fieldset className="ne-fly__fieldset">
        <legend className="ne-fly__fieldlabel">Visibility</legend>
        <label className="ne-fly__radio">
          <input
            type="radio"
            name="ne-skill-visibility"
            checked={scope === "labels"}
            onChange={() => setScope("labels")}
          />
          Only these labels:
        </label>
        <div className="ne-fly__labels">
          {skill.suggested_acl_labels.length === 0 ? (
            <p className="ne-fly__none">No suggested labels.</p>
          ) : (
            skill.suggested_acl_labels.map((label) => (
              <label key={label} className="ne-fly__radio">
                <input
                  type="checkbox"
                  checked={checked.includes(label)}
                  disabled={global}
                  onChange={(event) =>
                    setChecked((prev) =>
                      event.target.checked
                        ? [...prev, label]
                        : prev.filter((value) => value !== label),
                    )
                  }
                />
                {label}
              </label>
            ))
          )}
        </div>
        <label className="ne-fly__radio">
          <input
            type="radio"
            name="ne-skill-visibility"
            checked={global}
            onChange={() => setScope("everyone")}
          />
          Visible to everyone
        </label>
      </fieldset>

      <label className="ne-fly__field">
        <span className="ne-fly__fieldlabel">Note (optional)</span>
        <input
          type="text"
          className="ne-fly__input"
          value={note}
          onChange={(event) => onNote(event.target.value)}
        />
      </label>
      <div className="ne-fly__buttons">
        <button
          type="button"
          className="ne-btn ne-btn--primary"
          disabled={busy || !ready}
          onClick={() =>
            onSubmit({
              aclLabels: [...labels],
              globalVisibility: global,
              note,
              edits: edits(),
            })
          }
        >
          Apply
        </button>
        <button type="button" className="ne-btn ne-btn--quiet" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
