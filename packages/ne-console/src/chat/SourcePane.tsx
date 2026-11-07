// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — THE SOURCE PANE. The citation surviving challenge is the product.
//
// It is §1.4's detail pane, not a bespoke panel: 420px, resizable, Esc closes,
// focus-trapped, and it never navigates away from the thread. A reader opens a
// citation, reads it, closes it, opens the next — two keys, no page loads.
//
// It calls the governed retrieve with exactly the one chunk id. The server
// re-checks the permission and returns the chunk only if this key may see it;
// the console holds no ACL and has no code path that could evaluate one. An
// empty answer therefore means the server withheld the record, and the pane
// says exactly that — never "loading", never a fault, and never any content.
//
// THREE STATES, THREE DIFFERENT FACTS. Refused is a role boundary. Error is a
// read that could not be made. They are not interchangeable, and a screen that
// says "you don't have access" about a read that merely failed is making a false
// statement about authorization.
//
// The stored record is sometimes a JSON envelope serialised into the text field.
// It is unwrapped through the same parse the search results use, so the reader
// gets the document rather than the envelope. Nothing is synthesised: a chunk
// whose text is not an envelope renders exactly as it arrived.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import type { Citation } from "../api/types";
import { ApiError } from "../api/client";
import type { ApiClient } from "../api/client";
import { usePrincipal } from "../auth/PrincipalContext";
import { useApiClient } from "../auth/useApiClient";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { resolvesToEntity } from "./citationEntity";
import { graphCitationsHref } from "../knowledge/graph/graphLinks";
import { MetadataValueOf } from "../search/MetadataFields";
import { parseResultRecord, resultIdentity } from "../search/resultRecord";
import type { ResultRecord } from "../search/resultRecord";
import {
  SOURCE_ERROR,
  SOURCE_LOADING,
  SOURCE_OPEN_IN_SYSTEM,
  SOURCE_PANE_LABEL,
  SOURCE_REFUSED,
  SOURCE_VIEW_IN_DOCUMENTS,
  SOURCE_VIEW_IN_GRAPH,
} from "./askCopy";

type State =
  | { status: "loading" }
  | { status: "refused" }
  | { status: "error" }
  | { status: "ok"; record: ResultRecord; metadata: Record<string, unknown>; inGraph: boolean };

type SourceOwner = { client: ApiClient; generation: string };

export function SourcePane({
  citation,
  onClose,
  client: providedClient,
  principalId: providedPrincipalId,
  ownerGeneration: providedOwnerGeneration,
}: {
  citation: Citation;
  onClose: () => void;
  /** ChatPage passes its exact owner. Optional only for the standalone pane. */
  client?: ApiClient;
  principalId?: string | null;
  ownerGeneration?: string;
}) {
  const contextClient = useApiClient();
  const principal = usePrincipal();
  const client = providedClient ?? contextClient;
  const contextPrincipalId =
    principal.status === "ok" && principal.principal !== null ? principal.principal.id : null;
  const principalId = providedPrincipalId === undefined ? contextPrincipalId : providedPrincipalId;
  const generation =
    providedOwnerGeneration ?? (principalId === null ? null : `principal:${principalId}`);
  const owner = useMemo<SourceOwner | null>(
    () => (generation === null ? null : { client, generation }),
    [client, generation],
  );
  const currentOwnerRef = useRef(owner);
  useLayoutEffect(() => {
    currentOwnerRef.current = owner;
  }, [owner]);
  const [boundState, setBoundState] = useState<{ owner: SourceOwner | null; value: State }>({
    owner,
    value: { status: "loading" },
  });
  const state =
    owner !== null && boundState.owner === owner
      ? boundState.value
      : { status: "loading" as const };
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (owner === null) return;
    let cancelled = false;
    client
      .retrieve([citation.chunk_id])
      .then((result) => {
        if (cancelled || currentOwnerRef.current !== owner) return;
        const chunk = result.chunks[0]?.chunk;
        if (chunk === undefined) {
          // The server answered, and answered with nothing for a chunk it had
          // just cited. That is the trim, not a fault and not a missing read.
          setBoundState({ owner, value: { status: "refused" } });
          return;
        }
        setBoundState({
          owner,
          value: {
            status: "ok",
            record: parseResultRecord(chunk.text),
            metadata: chunk.metadata,
            inGraph: resolvesToEntity(result, citation),
          },
        });
      })
      .catch((err: unknown) => {
        if (cancelled || currentOwnerRef.current !== owner) return;
        setBoundState({
          owner,
          value: {
            status: err instanceof ApiError && err.status === 403 ? "refused" : "error",
          },
        });
      });
    return () => {
      cancelled = true;
    };
  }, [citation, client, nonce, owner]);

  // The reset belongs to the click, not to the effect: the pane is remounted per
  // citation, so "loading" is already this component's opening state.
  const retry = useCallback(() => {
    if (owner === null || currentOwnerRef.current !== owner) return;
    setBoundState({ owner, value: { status: "loading" } });
    setNonce((n) => n + 1);
  }, [owner]);

  // The shared identity ladder (search/resultRecord.ts): envelope title, the
  // body's own heading, the connector's source path or record id, and the hash
  // LAST — the same rule Search's pane headline follows, so the two panes
  // cannot title the same chunk two different ways.
  const title =
    state.status === "ok"
      ? resultIdentity(state.record, state.metadata, citation.document_id).label
      : null;

  return (
    <DetailPane
      title={state.status === "ok" ? title : SOURCE_PANE_LABEL}
      subtitle={state.status === "ok" ? citation.source_system : undefined}
      onClose={onClose}
    >
      {state.status === "loading" ? (
        <p className="ne-state ne-state--loading" role="status">
          {SOURCE_LOADING}
        </p>
      ) : null}

      {state.status === "refused" ? <p className="ne-state__line">{SOURCE_REFUSED}</p> : null}

      {state.status === "error" ? (
        <div className="ne-state ne-state--error" role="alert">
          <p className="ne-state__line">{SOURCE_ERROR}</p>
          <button type="button" className="ne-btn ne-btn--quiet" onClick={retry}>
            Retry
          </button>
        </div>
      ) : null}

      {state.status === "ok" ? (
        <>
          <p className="ne-ask__sourcetext">{state.record.body}</p>
          <DetailFields>
            <DetailField label="Document" mono>
              {state.record.docId ?? citation.document_id}
            </DetailField>
            <DetailField label="Source system">
              {state.record.sourceSystem ?? citation.source_system}
            </DetailField>
            <DetailField label="Source reference" mono>
              {citation.source_ref}
            </DetailField>
            {Object.entries(state.metadata).map(([key, value]) => (
              <DetailField key={key} label={key}>
                <MetadataValueOf value={value} />
              </DetailField>
            ))}
          </DetailFields>
          <div className="ne-ask__sourcelinks">
            {citation.source_url ? (
              <a href={citation.source_url} target="_blank" rel="noopener noreferrer">
                {SOURCE_OPEN_IN_SYSTEM}
              </a>
            ) : null}
            {/* THE WAY THROUGH TO THE DOCUMENT. DocumentsPage deep-links by its
             * `?document=` parameter (knowledge/documents/documentRouteState),
             * keyed by the same knowledge document_id the citation carries; a
             * document the directory cannot serve answers with that page's own
             * absent state. The envelope's docId wins when present because it
             * is the record's own statement of the id. */}
            <Link
              to={`/knowledge/documents?document=${encodeURIComponent(
                state.record.docId ?? citation.document_id,
              )}`}
            >
              {SOURCE_VIEW_IN_DOCUMENTS}
            </Link>
            {state.inGraph ? (
              <Link to={graphCitationsHref([citation.source_ref])}>{SOURCE_VIEW_IN_GRAPH}</Link>
            ) : null}
          </div>
        </>
      ) : null}
    </DetailPane>
  );
}
