// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useAuth } from "react-oidc-context";
import { Link } from "react-router";
import { ApiError, sourceDisclosure } from "../api/client";
import { HashPlate } from "../primitives/HashPlate";
import { ListScreen } from "../primitives/PageHeader";
import { PrimaryButton } from "../primitives/Toolbar";
import { useAnswerLedgerClient } from "./useAnswerLedgerClient";
import type { AnswerEventView, AnswerLedgerView, ServedAnswerEventView } from "./types";
import "./answer-ledger.css";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; ledger: AnswerLedgerView }
  | { kind: "error"; detail: string };

function detailOf(error: unknown): string {
  if (error instanceof ApiError) return error.userMessage;
  return "The Answer Ledger could not be loaded.";
}

/** The timeline's word where a withheld event's classification would be. */
const WITHHELD_WORD = "WITHHELD";

/**
 * The timeline's words where a withheld event's served model would be.
 *
 * A model DID answer — the gate withheld its identity, not its existence — so
 * this states the non-disclosure the way the row above it does, instead of
 * leaving a blank where every disclosed row carries a name. "Served model" is
 * the same label the provenance list uses for this field.
 */
const WITHHELD_MODEL_WORDS = "Served model withheld";

/**
 * 015A0-ENFORCEMENT: the one reader of a ledger event's disclosure status.
 *
 * Returns the event only when the server stated it available; every other
 * answer — withheld, unresolved, unstated, unreadable — returns null and the
 * page renders its safe shell. Nothing here inspects which fields arrived.
 */
function disclosed(event: ServedAnswerEventView): AnswerEventView | null {
  const disclosure = sourceDisclosure(event);
  return disclosure.kind === "available" ? (event as AnswerEventView) : null;
}

function changeWord(changed: boolean): string {
  return changed ? "Changed" : "Unchanged";
}

function claimText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * Render the one inline convention the answer writer emits in the live corpus
 * without ever treating answer bytes as HTML. The exact server string remains
 * available in the adjacent native disclosure.
 */
function readableAnswer(answer: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const emphasis = /\*\*([^*\n]+)\*\*/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = emphasis.exec(answer)) !== null) {
    if (match.index > cursor) nodes.push(answer.slice(cursor, match.index));
    nodes.push(<strong key={`${match.index}:${match[1]}`}>{match[1]}</strong>);
    cursor = match.index + match[0].length;
  }
  if (cursor < answer.length) nodes.push(answer.slice(cursor));
  return nodes;
}

function IdentifierDisclosure({ label, value }: { label: string; value: string }) {
  return (
    <details className="ne-answer-ledger__identifier">
      <summary>{label}</summary>
      <code>{value}</code>
    </details>
  );
}

function RootCause({ event }: { event: AnswerEventView }) {
  const cause = event.root_cause;
  if (cause === null) {
    return <p className="ne-answer-ledger__baseline">Baseline answer — no predecessor.</p>;
  }
  return (
    <section className="ne-answer-ledger__causes" aria-label="Root cause">
      <article>
        <span className="ne-label">Answer</span>
        <strong>{changeWord(cause.answer_changed)}</strong>
        <p>{event.classification ?? "Baseline"}</p>
      </article>
      <article>
        <span className="ne-label">Corpus</span>
        <strong>{changeWord(cause.corpus_changed)}</strong>
        {cause.corpus_diff_hrefs.length === 0 ? (
          <p>No document-version delta.</p>
        ) : (
          cause.corpus_diff_hrefs.map((href, index) => (
            <Link key={href} to={href}>
              Document diff {index + 1}
            </Link>
          ))
        )}
      </article>
      <article>
        <span className="ne-label">Configuration</span>
        <strong>{changeWord(cause.config_changed)}</strong>
        {cause.config_diff_href === null ? (
          <p>No resolved-config delta.</p>
        ) : (
          <Link to={cause.config_diff_href}>Resolved configuration history</Link>
        )}
      </article>
    </section>
  );
}

/**
 * 015A0-ENFORCEMENT's fixed safe shell for one ledger event.
 *
 * The event id and its timestamp are the ledger's OWN record — that a re-ask
 * happened, and when — and they survive. Everything the sources produced does
 * not: no answer, no verbatim disclosure, no classification, no root cause, no
 * served model, no config hash, no trace link, no corpus stamp, and therefore
 * no copy or export affordance either. The shell is parameterised only by the
 * two owned fields, so no source-derived byte can reach it.
 */
function WithheldEventDetail({ event }: { event: ServedAnswerEventView }) {
  const permanent = event.disclosure_status === "content_withheld";
  return (
    <div className="ne-answer-ledger__detail">
      <section className="ne-answer-ledger__answer" aria-label="Answer">
        <span className="ne-label">Answer</span>
        <p className="ne-answer-ledger__withheld" role="status">
          {permanent
            ? "Answer withheld — access changed."
            : "Answer withheld — the server stated no disclosure decision for this event."}
        </p>
      </section>
      <dl className="ne-answer-ledger__provenance">
        <div>
          <dt>Recorded</dt>
          <dd>
            <time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleString()}</time>
          </dd>
        </div>
      </dl>
    </div>
  );
}

function EventDetail({ event }: { event: AnswerEventView }) {
  return (
    <div className="ne-answer-ledger__detail">
      <RootCause event={event} />
      <section className="ne-answer-ledger__answer" aria-label="Answer">
        <span className="ne-label">Answer</span>
        <div className="ne-answer-ledger__readable-answer">{readableAnswer(event.answer)}</div>
        <details className="ne-answer-ledger__verbatim">
          <summary>Verbatim answer</summary>
          <pre>{event.answer}</pre>
        </details>
      </section>
      <dl className="ne-answer-ledger__provenance">
        <div>
          <dt>Served model</dt>
          <dd>{event.served_model_id}</dd>
        </div>
        <div>
          <dt>Configuration</dt>
          <dd>
            <HashPlate value={event.config_hash} collapsed />
          </dd>
        </div>
        <div>
          <dt>Trace</dt>
          <dd className="ne-answer-ledger__trace">
            <Link to={event.trace_href}>Open trace</Link>
            <IdentifierDisclosure label="Trace ID" value={event.trace_id} />
          </dd>
        </div>
      </dl>
      <ul className="ne-answer-ledger__chunks" aria-label="Served chunks">
        {event.corpus_stamp.map((chunk, index) => (
          <li key={`${chunk.document_id}:${chunk.document_version}:${chunk.chunk_id}`}>
            <div className="ne-answer-ledger__chunk-summary">
              <Link to={chunk.document_href}>
                {chunk.document_title ?? `Document ${index + 1}`} · Version {chunk.document_version}
              </Link>
            </div>
            <details className="ne-answer-ledger__chunk-identifiers">
              <summary>Technical details</summary>
              <dl>
                {/* Identifiers, not digests: these are the strings a reader
                    quotes back to the source system to find the record, so
                    they stay WHOLE. §2.7's truncation is a rule about hashes
                    (see approvals/DecisionPane.tsx, which refuses to chip a
                    request id for the same reason). Judging by appearance
                    would get this wrong — a connector document_id can be 64
                    hex characters and still not be a hash. */}
                <div>
                  <dt>Document ID</dt>
                  <dd>
                    <code>{chunk.document_id}</code>
                  </dd>
                </div>
                <div>
                  <dt>Chunk ID</dt>
                  <dd>
                    <code>{chunk.chunk_id}</code>
                  </dd>
                </div>
                {/* A digest, and §2.7 governs it: the server computes this as
                    sha256(chunk text). Rendered with the plate this page
                    already uses for config_hash — truncated to read, full
                    value one hover away to paste. */}
                <div>
                  <dt>Content hash</dt>
                  <dd>
                    <HashPlate value={chunk.content_hash} collapsed />
                  </dd>
                </div>
              </dl>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AnswerLedgerPage() {
  const client = useAnswerLedgerClient();
  const auth = useAuth();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [question, setQuestion] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      setState({ kind: "ready", ledger: await client.read() });
    } catch (error: unknown) {
      setState({ kind: "error", detail: detailOf(error) });
    }
  }, [client]);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    void client
      .read(controller.signal)
      .then((ledger) => {
        if (active) setState({ kind: "ready", ledger });
      })
      .catch((error: unknown) => {
        if (active && !controller.signal.aborted) {
          setState({ kind: "error", detail: detailOf(error) });
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [client]);

  const ledger = state.kind === "ready" ? state.ledger : null;
  // This endpoint is principal-owned: the server looks up the question by the
  // authenticated caller id. The caller's own OIDC name is therefore the
  // authoritative human-first label; the immutable ledger id remains below.
  const ownerLabel =
    claimText(auth.user?.profile.name) ??
    claimText(auth.user?.profile.preferred_username) ??
    "Current principal";
  const selected = useMemo(() => {
    const events = ledger?.events ?? [];
    return events.find((event) => event.event_id === selectedId) ?? events.at(-1) ?? null;
  }, [ledger, selectedId]);

  const save = useCallback(async (operation: () => Promise<AnswerLedgerView>) => {
    setSaving(true);
    setSaveError(null);
    try {
      const next = await operation();
      setState({ kind: "ready", ledger: next });
      setSelectedId(next.events.at(-1)?.event_id ?? null);
    } catch (error: unknown) {
      setSaveError(detailOf(error));
    } finally {
      setSaving(false);
    }
  }, []);

  return (
    <div className="ne-answer-ledger">
      <ListScreen
        title="Monitored answer"
        // The reverse door, for the same reason the forward one exists: a
        // reader who arrived here looking for the protected-content index of
        // conversation turns is one click from it rather than back through the
        // rail guessing which ledger is which.
        action={<Link to="/conversation-ledger">Conversation ledger</Link>}
      >
        {state.kind === "loading" ? <p role="status">Loading Answer Ledger…</p> : null}
        {state.kind === "error" ? (
          <div role="alert">
            <p>{state.detail}</p>
            <button type="button" className="ne-btn ne-btn--quiet" onClick={() => void load()}>
              Retry
            </button>
          </div>
        ) : null}
        {ledger?.question === null ? (
          <form
            className="ne-answer-ledger__pin"
            onSubmit={(event) => {
              event.preventDefault();
              if (question.trim()) void save(() => client.pin(question));
            }}
          >
            <label htmlFor="answer-ledger-question">Pin one business question</label>
            <textarea
              id="answer-ledger-question"
              maxLength={2000}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
            />
            <PrimaryButton
              type="submit"
              disabled={saving || !question.trim()}
              label={saving ? "Pinning…" : "Pin question"}
            />
          </form>
        ) : null}
        {ledger?.question ? (
          <>
            <header className="ne-answer-ledger__question">
              <div>
                <span className="ne-label">Monitored question</span>
                <h2>{ledger.question.question}</h2>
                <div className="ne-answer-ledger__owner">
                  <span>
                    Owner <span title={ledger.question.owner_id}>{ownerLabel}</span> · Manual re-ask
                  </span>
                  <IdentifierDisclosure label="Owner ID" value={ledger.question.owner_id} />
                </div>
              </div>
              <PrimaryButton
                disabled={saving}
                onClick={() => void save(() => client.reask())}
                label={saving ? "Re-asking…" : "Re-ask now"}
              />
            </header>
            {ledger.events.length === 0 ? (
              <p className="ne-answer-ledger__empty">
                No answers yet. Re-ask now to establish the baseline.
              </p>
            ) : (
              <div className="ne-answer-ledger__workspace">
                <ol className="ne-answer-ledger__timeline" aria-label="Answer timeline">
                  {ledger.events.map((event) => {
                    // Read the disclosure ONCE, so both lines of the row
                    // demonstrably answer the same question the same way.
                    const view = disclosed(event);
                    return (
                      <li key={event.event_id}>
                        <button
                          type="button"
                          onClick={() => setSelectedId(event.event_id)}
                          aria-pressed={selected?.event_id === event.event_id}
                        >
                          <time dateTime={event.occurred_at}>
                            {new Date(event.occurred_at).toLocaleString()}
                          </time>
                          <strong>
                            {view === null ? WITHHELD_WORD : (view.classification ?? "BASELINE")}
                          </strong>
                          <span>{view === null ? WITHHELD_MODEL_WORDS : view.served_model_id}</span>
                        </button>
                      </li>
                    );
                  })}
                </ol>
                {selected === null ? null : disclosed(selected) === null ? (
                  <WithheldEventDetail event={selected} />
                ) : (
                  <EventDetail event={disclosed(selected)!} />
                )}
              </div>
            )}
          </>
        ) : null}
        {saveError ? (
          <p className="ne-answer-ledger__error" role="alert">
            {saveError}
          </p>
        ) : null}
      </ListScreen>
    </div>
  );
}
