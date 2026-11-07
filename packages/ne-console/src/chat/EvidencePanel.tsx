// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The single principal-scoped evidence-replay seam, shared by BOTH chat
// citations and search results. Clicking a citation calls the governed
// /brain/retrieve with exactly the one chunk_id; the server re-checks ACLs and
// returns the chunk ONLY if the logged-in principal may see it. If retrieve
// comes back empty, we render an access-denied message and NEVER any content —
// ACL-safe by construction (the client has no ACL logic of its own).
//
// ROUND-3 MAJOR — "do you land somewhere useful or somewhere raw": raw.
//
// PROVE is the affordance styled as the primary citation-follow, in link blue
// beside the [1] badge. It opened this panel, and this panel rendered:
//   * a heading that was `source_system · document_id` — and on this deployment
//     the document id IS the 64-character chunk hash, so the heading was a bare
//     hash with no legend, violating the four-part readout rule the rest of the
//     console holds to; and
//   * a body that was `chunk.text` printed verbatim — which on this deployment is
//     a JSON envelope, so the reader got
//     {"content": "# 2025 Renewal Agreement…\n\nThis agreement…", "doc_id": …}
//     with literal \n sequences.
// Meanwhile the quieter "Show evidence" disclosure one line below rendered the
// SAME record properly, through search/resultRecord.ts. The good path was hidden
// behind the small link and the promoted one was the raw one.
//
// This panel now runs the retrieved chunk through the SAME parse
// (`parseResultRecord`) and renders the same labelled fields, with a real legend
// and the hash kept as a labelled SOURCE REF line. Nothing is invented: the parse
// is an unwrap of an envelope the server chose to serialize into a string field,
// and a chunk whose text is not an envelope renders verbatim exactly as before.
//
// AND IT MOVES FOCUS. On /search the panel opens ~1,700px below the fold on a
// 2,485px page: measured, clicking PROVE at scrollY=0 left scrollY at 0 and left
// focus on the PROVE button, so nothing in the viewport changed. It now takes
// focus and scrolls itself into view on open, which serves keyboard and mouse
// users with one mechanism.

import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { ApiError } from "../api/client";
import type { Citation } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { LoadError, Refused } from "../primitives/states";
import { parseResultRecord, type ResultRecord } from "../search/resultRecord";

// A REFUSAL AND A FAULT ARE TWO DIFFERENT ANSWERS, so this panel holds two
// different states for them. `denied` is the per-source ACL trim (an empty
// retrieval); `refused` is the server refusing this principal the read itself;
// `error` is a fault. Folding the middle one into the last gave a correctly
// refused read the fault colour and a Retry that could only refuse again —
// exactly what primitives/states.tsx forbids ("THE REFUSAL IS NEUTRAL, NEVER A
// FAULT").
type State =
  | { status: "loading" }
  | { status: "denied" }
  | { status: "refused" }
  | { status: "error" }
  | { status: "ready"; record: ResultRecord };

export function EvidencePanel({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  const client = useApiClient();
  const [state, setState] = useState<State>({ status: "loading" });
  // Bumped by Retry. The read is the whole panel, so a failed read with no way
  // to run it again is a dead end (§2.4: fact, then next action).
  const [attempt, setAttempt] = useState(0);
  const panelRef = useRef<HTMLElement | null>(null);

  // The parent remounts this component per citation (key=chunk_id), so initial
  // state is always "loading" and the effect only performs the fetch — no
  // synchronous setState in the effect body.
  useEffect(() => {
    let cancelled = false;
    client
      .retrieve([citation.chunk_id])
      .then((result) => {
        if (cancelled) return;
        const chunk = result.chunks[0]?.chunk;
        if (!chunk) {
          // Empty retrieval = the trimmed API withheld this source. No content.
          setState({ status: "denied" });
          return;
        }
        setState({ status: "ready", record: parseResultRecord(chunk.text) });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // 403 is the deployment declining this principal the read — a role
        // boundary held, not an outage, and the same mapping SourcePane makes
        // for the same call. Everything else (a 5xx, a malformed retrieval
        // envelope, a parse that threw) is a fault and keeps the read-error
        // grammar.
        //
        // Either way the throw's own strings stay unshown. The raw one is the
        // transport's, which api/client.ts parks on a property no screen may
        // name (transport-provenance guardian) — a sentence about our plumbing
        // addressed to a reader who came to check a citation. The served one
        // would be showable, but this panel has no disclosure slot to demote it
        // into: the state word IS the whole readout here.
        setState({ status: err instanceof ApiError && err.status === 403 ? "refused" : "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [client, citation.chunk_id, attempt]);

  // OPENING SOMETHING OFF-SCREEN IS NOT OPENING IT. Focus + scroll on mount, once
  // per citation (the parent keys this component by chunk_id).
  useEffect(() => {
    const el = panelRef.current;
    if (!el) return;
    el.focus();
    // Guarded: jsdom (and older engines) do not implement scrollIntoView, and
    // failing to bring a panel into view must never break rendering it.
    if (typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "nearest" });
  }, []);

  const record = state.status === "ready" ? state.record : null;
  // Never invented: the title is the envelope's own; with none, the heading falls
  // back to the identifiers the wire already carried.
  const heading = record?.title ?? record?.docId ?? citation.document_id;

  return (
    <aside
      className="ne-evidence"
      aria-label="the record this citation came from"
      ref={panelRef}
      tabIndex={-1}
    >
      <header className="ne-evidence__head">
        {/* A LEGEND, then the value. The heading used to BE the raw identifier. */}
        <span className="ne-evidence__legend">THE RECORD THIS CITATION CAME FROM</span>
        <span className="ne-evidence__title">{heading}</span>
        <button type="button" onClick={onClose} aria-label="Close evidence">
          ×
        </button>
      </header>
      {state.status === "loading" && (
        <p className="ne-note" role="status">
          Reading this record back from the source, under your own permissions…
        </p>
      )}
      {state.status === "denied" && (
        // The ACL trim: the server answered, and released nothing for this one
        // chunk. That is a fact about this source, not about the reader's role,
        // so it stays its own sentence.
        <p className="ne-evidence__denied">You don&apos;t have access to this source.</p>
      )}
      {state.status === "refused" && (
        // The shared neutral refusal — GATED pill, no Retry. `/v1/brain/retrieve`
        // trims per source rather than refusing per source, so a 403 here is the
        // read surface being closed to this principal, which is what this
        // primitive says.
        <Refused thing="reading this record back" page="Evidence" />
      )}
      {state.status === "error" && (
        // The shared read-error state, not a bespoke line: "Couldn't load this
        // evidence." plus Retry. `ne-evidence__error` was never styled anyway,
        // so the old line was an unstyled paragraph carrying an HTTP verb.
        <LoadError
          things="this evidence"
          onRetry={() => {
            setState({ status: "loading" });
            setAttempt((n) => n + 1);
          }}
        />
      )}
      {state.status === "ready" && record && (
        <>
          <dl className="ne-evidence__meta">
            <div>
              <dt className="ne-label">DOCUMENT</dt>
              <dd className="ne-mono">{record.docId ?? citation.document_id}</dd>
            </div>
            {record.uri ? (
              <div>
                <dt className="ne-label">URI</dt>
                <dd className="ne-mono">{record.uri}</dd>
              </div>
            ) : null}
            <div>
              <dt className="ne-label">SOURCE SYSTEM</dt>
              <dd className="ne-mono">{record.sourceSystem ?? citation.source_system}</dd>
            </div>
            {record.ownedBy ? (
              <div>
                <dt className="ne-label">OWNED BY</dt>
                <dd className="ne-mono">{record.ownedBy}</dd>
              </div>
            ) : null}
            <div>
              <dt className="ne-label">SOURCE REF</dt>
              <dd className="ne-mono ne-evidence__ref">{citation.source_ref}</dd>
            </div>
          </dl>
          {record.ownedBy ? (
            <p className="ne-note">
              OWNED BY is the principal id the record carries. This deployment serves no directory
              lookup, so the console has no display name to resolve it to and does not invent one.
            </p>
          ) : null}
          {/* The body, as text. `pre-wrap` because the document's own line breaks
              are part of it — but it is the UNWRAPPED body, never the envelope. */}
          <p className="ne-evidence__text">{record.body}</p>
          <p className="ne-evidence__provenance">
            Re-read from the source just now, under your own permissions — not replayed from the
            answer. The server re-checked your access before releasing it; the console holds no
            permission rules of its own and could not have shown you this on its own say-so.
            {record.wasEnvelope
              ? " The stored record is a JSON envelope; the fields above are its own, unwrapped, not derived."
              : ""}
          </p>
          {citation.source_url && (
            <a href={citation.source_url} target="_blank" rel="noopener noreferrer">
              Open in {record.sourceSystem ?? citation.source_system}
            </a>
          )}
          {/* The graph is a SEPARATE question — "what does the knowledge layer
              link this record to" — and it is honestly labelled as one, including
              the fact that on this deployment it usually answers "nothing". It
              used to be the primary PROVE destination. */}
          <Link
            className="ne-evidence__graph"
            to={`/knowledge?citation=${encodeURIComponent(citation.source_ref)}`}
          >
            LOOK THIS RECORD UP IN THE KNOWLEDGE GRAPH →
          </Link>
          <p className="ne-note">
            A different question, and often an empty answer: the graph holds entity nodes, and a
            corpus document that was never entity-extracted has none. The record itself is above.
          </p>
        </>
      )}
    </aside>
  );
}
