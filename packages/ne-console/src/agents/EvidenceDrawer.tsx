// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { Fragment, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { Link } from "react-router";
import type { AgentEvidenceEnvelopeWire } from "../api/types";
import { HashChip } from "../primitives/HashChip";
import { ROW_ANCHOR_COPY_LABEL, ROW_ANCHOR_GLOSS, ROW_ANCHOR_LABEL } from "../transcript";
import type { AgentsClient } from "./types";
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

const FOCUSABLE = 'button:not([disabled]),a[href],[tabindex]:not([tabindex="-1"])';

export function EvidenceDrawer({
  client,
  agentId,
  runId,
  view,
  focusedId,
  onFocus,
  onClose,
  trigger,
}: {
  client: AgentsClient;
  agentId: string;
  runId: string;
  view: "evidence" | "governance";
  focusedId: string | null;
  onFocus: (id: string) => void;
  onClose: () => void;
  trigger: RefObject<HTMLButtonElement | null>;
}) {
  const [data, setData] = useState<AgentEvidenceEnvelopeWire | null>(null);
  const [failed, setFailed] = useState(!client.getAgentRunEvidence);
  const drawer = useRef<HTMLElement>(null);
  useEffect(() => {
    let live = true;
    if (!client.getAgentRunEvidence) {
      return () => {
        live = false;
      };
    }
    void client.getAgentRunEvidence(agentId, runId).then(
      (v) => {
        if (live) setData(v);
      },
      () => {
        if (live) setFailed(true);
      },
    );
    return () => {
      live = false;
    };
  }, [agentId, client, runId]);
  useEffect(() => {
    drawer.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
  }, []);
  const close = () => {
    onClose();
    queueMicrotask(() => trigger.current?.focus());
  };
  const keyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== "Tab" || !drawer.current) return;
    const nodes = [...drawer.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (!nodes.length) return;
    const first = nodes[0]!,
      last = nodes[nodes.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  const exportJson = async () => {
    if (!client.exportAgentRunEvidence) return;
    const body = await client.exportAgentRunEvidence(agentId, runId);
    const url = URL.createObjectURL(new Blob([JSON.stringify(body)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "run-evidence.json";
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <aside
      ref={drawer}
      className="ne-evidence-drawer"
      role="dialog"
      aria-modal="true"
      aria-label={`${view} drawer`}
      onKeyDown={keyDown}
    >
      <header>
        <h2>{view === "evidence" ? "Evidence" : "Governance"}</h2>
        <button type="button" className="ne-btn ne-btn--quiet" onClick={close}>
          Close
        </button>
      </header>
      {failed ? (
        <p role="status">This Run’s governed evidence is unavailable.</p>
      ) : !data ? (
        <p role="status">Loading governed evidence…</p>
      ) : view === "governance" ? (
        <Governance data={data} />
      ) : (
        <Evidence data={data} focusedId={focusedId} onFocus={onFocus} />
      )}
      {data ? (
        <button type="button" className="ne-btn" onClick={() => void exportJson()}>
          Export governed JSON
        </button>
      ) : null}
    </aside>
  );
}

function Evidence({
  data,
  focusedId,
  onFocus,
}: {
  data: AgentEvidenceEnvelopeWire;
  focusedId: string | null;
  onFocus: (id: string) => void;
}) {
  const outputLabel = outputAssurance(data);
  return (
    <>
      <p role="status">{data.mapping_message}</p>
      <section aria-label="Run output">
        <h3>Answer</h3>
        <Assurance label={outputLabel} />
        <p>{data.output.state === "available" ? data.output.display : data.output.message}</p>
      </section>
      <section aria-label="Claims">
        <h3>Claims</h3>
        {data.claims.length ? (
          <ol>
            {data.claims.map((c) => (
              <li key={c.claim_id}>
                <button
                  type="button"
                  aria-pressed={focusedId === c.evidence_id}
                  disabled={!c.evidence_id}
                  onClick={() => c.evidence_id && onFocus(c.evidence_id)}
                >
                  {c.state === "available" ? c.display : c.message}
                </button>
                <Assurance label={claimAssurance(c)} />
              </li>
            ))}
          </ol>
        ) : (
          <p>No evidence-backed claims were served.</p>
        )}
      </section>
      <section aria-label="Evidence items">
        <h3>Evidence</h3>
        {data.evidence.length ? (
          <ol>
            {data.evidence.map((e) => (
              <li key={e.evidence_id} data-focused={focusedId === e.evidence_id}>
                {/* A DEAD BUTTON IS WORSE THAN A SENTENCE. Only an available
                 * record has anything behind it — every block below this one is
                 * gated on that — so a row in any other state used to be a
                 * control the reader could focus, press and get nothing from.
                 * It is now plain text. */}
                {evidenceRowCanOpen(e.state) ? (
                  <button
                    type="button"
                    aria-pressed={focusedId === e.evidence_id}
                    onClick={() => onFocus(e.evidence_id)}
                  >
                    {evidenceRowSentence(e.state, e.message)}
                  </button>
                ) : (
                  <p>{evidenceRowSentence(e.state, e.message)}</p>
                )}
                {/* THE BADGE IS NOT PART OF THE BUTTON. It used to be drawn
                 * inside the branch above, so only an openable row could carry
                 * one — and "openable" is `state === "available"`, which left
                 * the "withheld" badge unreachable on screen while a test
                 * asserted it. A withheld row is exactly the row worth badging:
                 * its server sentence reads like a gap, and the badge is the one
                 * place a reader is told the record is held rather than missing.
                 * It does NOT name why it was not released: five conditions reach
                 * `state="withheld"` and only one of them is a decision about
                 * access (`assuranceVocabulary.ts`, rule 2), so the gloss offers
                 * both readings and picks neither. `evidenceAssurance` decides —
                 * it returns null for the rows whose sentence is already the
                 * whole story. */}
                <Assurance label={evidenceAssurance(e)} />
                {/* THE ROW'S IDENTITY — the SERVED canonical hash of the
                 * verified row, straight off the locator. Short form on
                 * screen, full value in the title and one copy away. Never
                 * recomputed here, and absent when the server served none
                 * (the `verified_t3_empty` arm has no row and no anchor). */}
                {e.state === "available" &&
                typeof e.locator?.row_anchor === "string" &&
                e.locator.row_anchor !== "" ? (
                  <span className="ag-rowanchor" title={ROW_ANCHOR_GLOSS}>
                    <span className="ag-rowanchor__label">{ROW_ANCHOR_LABEL}</span>
                    <HashChip hash={e.locator.row_anchor} label={ROW_ANCHOR_COPY_LABEL} />
                  </span>
                ) : null}
                {e.state === "available" && e.locator?.graph_path?.length ? (
                  e.locator.graph_path.some((hop) => hop.review_state !== null) ? (
                    <ul aria-label="Graph hop review states">
                      {e.locator.graph_path.map((hop, index) =>
                        hop.review_state === null ? null : (
                          <li
                            key={`${hop.subject.entity_key}:${hop.edge_kind}:${hop.obj.entity_key}:${hop.source_ref}:${index}`}
                            title={graphHopReviewTerm(hop.review_state).gloss}
                          >
                            {/* `not_applicable` is a column value, not a
                             * sentence, and it was reaching the screen as one. */}
                            {graphHopReviewTerm(hop.review_state).label}
                          </li>
                        ),
                      )}
                    </ul>
                  ) : null
                ) : null}
                {e.state === "available" &&
                (e.locator?.entity_id || e.locator?.graph_path?.length) ? (
                  <Link
                    to={`/knowledge/graph?mode=graph&agent=${encodeURIComponent(data.agent_id)}&run=${encodeURIComponent(data.run_id)}&evidence=${encodeURIComponent(e.evidence_id)}`}
                  >
                    Open in Graph
                  </Link>
                ) : null}
                {e.state === "available" && e.locator?.served_document_versions?.length ? (
                  <ul aria-label="Served document versions">
                    {e.locator.served_document_versions.map((v) => (
                      <li key={`${v.document_id}:${v.version_seq}`}>
                        <Link
                          to={`/knowledge/documents?document=${encodeURIComponent(v.document_id)}&version=${encodeURIComponent(String(v.version_seq))}`}
                          title={`Content hash ${v.content_hash}`}
                        >
                          {v.display_name} · v{v.version_seq}
                        </Link>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ol>
        ) : (
          <p>No evidence was served.</p>
        )}
      </section>
    </>
  );
}

/** `null` draws nothing — a value whose source decided it has no fact to badge
 * (see `evidenceAssurance`), not a value this component chose to swallow. */
function Assurance({ label }: { label: keyof typeof ASSURANCE_EXPLANATIONS | null }) {
  if (label === null) return null;
  return (
    <span className="ne-assurance-label" title={ASSURANCE_EXPLANATIONS[label]}>
      {label}
    </span>
  );
}

function Governance({ data }: { data: AgentEvidenceEnvelopeWire }) {
  const g = data.governance;
  // ONE ROW PER FACT THE SERVER HAS; EVERY LOOKUP THAT DID NOT RESOLVE, NAMED ONCE.
  //
  // This panel used to render all four rows always, filling the absent ones
  // with "Unavailable" — so a partially-joined Run drew a column of four
  // identical non-answers, which reads as four separate faults rather than one
  // incomplete record. A fact that is on file gets its row.
  //
  // ⚠️ THE GAP LINE COMES FROM `unavailable_reasons`, NOT FROM THESE FOUR ROWS,
  // and deriving it from the rows was a P1. `project_governance` joins SIX facts
  // and sets `partial` when ANY of them did not resolve; this <dl> draws four of
  // them. A Run whose four drawn facts all joined and whose scope or policy join
  // did not therefore printed the partial sentence, filled all four rows, and —
  // with no null among them — drew NO gap line at all: the reader was told part
  // of the record was missing and given no way to learn which part, on the pane
  // whose whole job is accounting for the record. `governanceGapNames` reads the
  // server's own reason tokens, so the accounting covers all six.
  //
  // THE THREE LINES ARE THREE DIFFERENT FACTS, EACH SAID ONCE: the opening
  // sentence is about the audit receipt, the <dl> is what resolved, and the gap
  // line is what did not. That is also why the gap line is not suppressed on
  // `unavailable` — that status is set from the receipts alone, so its envelope
  // arrives carrying facts, and the two lines are not restatements of each other.
  //
  // A name can appear in the gap line with no matching row: scopes, policy
  // environment and tool tier are joined but not drawn here. That asymmetry is
  // deliberate. The accounting has to be complete even where the display is not,
  // and the alternative — staying silent about the joins this pane does not draw
  // — is precisely the bug. Drawing them is a separate question: an empty
  // `effective_scopes` array cannot be told apart from an unresolved scope join
  // by looking at the field, so a scopes row needs its own design pass, and
  // adding rows would not have fixed this anyway (a null row simply does not
  // render, so it names nothing either).
  const facts: Array<[string, string | null]> = [
    ["Delegated user", g.delegated_user_label],
    ["Agent service", g.agent_service_identity],
    ["Credential owner", g.connector_credential_owner_label],
    ["Tool decision", g.tool_decision === null ? null : toolDecisionLabel(g.tool_decision)],
  ];
  const known = facts.filter((fact): fact is [string, string] => fact[1] !== null);
  const gaps = governanceGapNames(g);
  return (
    <section aria-label="Server-authored governance">
      <p role="status">{governanceStatusSentence(g.status)}</p>
      {known.length ? (
        <dl>
          {known.map(([name, value]) => (
            <Fragment key={name}>
              <dt>{name}</dt>
              <dd>{value}</dd>
            </Fragment>
          ))}
        </dl>
      ) : null}
      {gaps.length ? <p>Did not resolve: {gaps.join(", ")}.</p> : null}
    </section>
  );
}
