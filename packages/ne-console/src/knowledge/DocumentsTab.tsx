// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import type { DocumentJourneyWire, DocumentStageWire, DocumentSummaryWire } from "../api/types";
import { ApiError } from "../api/client";
import { useApiClient } from "../auth/useApiClient";
import { HashChip } from "../primitives/HashChip";
import { Empty, LoadError, Refused } from "../primitives/states";
import { StatePill } from "../primitives/StatePill";

type Selector = { version?: number; as_of?: string } | undefined;
type Read<T> =
  { phase: "loading" } | { phase: "ok"; value: T } | { phase: "refused" } | { phase: "error" };

const STAGE_LABEL: Record<DocumentStageWire["stage"], string> = {
  parse: "PARSE",
  chunk: "CHUNK",
  acl: "ACL",
  embed: "EMBED",
  graph: "GRAPH",
  supersede: "SUPERSEDE",
};

function when(value: string | null): string {
  if (value === null) return "—";
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  }).format(new Date(value));
}

function StageState({ stage }: { stage: DocumentStageWire }) {
  const word =
    stage.status === "active"
      ? "SYNCING"
      : stage.status === "complete"
        ? "OK"
        : stage.status === "skipped"
          ? "IDLE"
          : "PENDING";
  return <StatePill word={word} />;
}

function Pipeline({ stages }: { stages: DocumentStageWire[] }) {
  return (
    <ol className="ne-docjourney__pipeline" aria-label="Document pipeline">
      {stages.map((stage, index) => (
        <li
          key={stage.stage}
          className={`ne-docjourney__stage${stage.status === "active" ? " ne-docjourney__stage--active" : ""}`}
        >
          <div className="ne-docjourney__stagehead">
            <strong>{STAGE_LABEL[stage.stage]}</strong>
            <StageState stage={stage} />
          </div>
          <span>{when(stage.occurred_at)}</span>
          <span>
            {stage.audit_seq === null ? (
              "Audit pending"
            ) : (
              <Link to={`/audit?seq=${stage.audit_seq}`}>Audit #{stage.audit_seq}</Link>
            )}
          </span>
          <span>{stage.detail}</span>
          {index < stages.length - 1 ? (
            <span className="ne-docjourney__arrow" aria-hidden="true">
              →
            </span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function Versions({
  journey,
  onVersion,
}: {
  journey: DocumentJourneyWire;
  onVersion: (version: number) => void;
}) {
  return (
    <div className="ne-table__wrap">
      <table className="ne-table" aria-label="Document versions">
        <thead>
          <tr>
            <th>Version</th>
            <th>Content hash</th>
            <th>Ingested</th>
            <th>Superseded</th>
            <th>Chunks</th>
            <th>Compare</th>
          </tr>
        </thead>
        <tbody>
          {journey.versions.map((version) => (
            <tr key={version.version_seq}>
              <td>v{version.version_seq}</td>
              <td>
                <HashChip hash={version.content_hash} label={`Copy v${version.version_seq} hash`} />
              </td>
              <td>
                {when(version.ingested_at)}
                {version.ingested_audit_seq === null ? null : (
                  <Link to={`/audit?seq=${version.ingested_audit_seq}`}>
                    #{version.ingested_audit_seq}
                  </Link>
                )}
              </td>
              <td>{when(version.superseded_at)}</td>
              <td>{version.chunk_count.toLocaleString()}</td>
              <td>
                <button
                  type="button"
                  className="ne-btn ne-btn--quiet"
                  onClick={() => onVersion(version.version_seq)}
                >
                  DIFF
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DocumentsTab({
  documentId,
  onOpenDocument,
}: {
  documentId: string | null;
  onOpenDocument: (documentId: string) => void;
}) {
  const api = useApiClient();
  const [documents, setDocuments] = useState<Read<DocumentSummaryWire[]>>({ phase: "loading" });
  const [journey, setJourney] = useState<Read<DocumentJourneyWire>>({ phase: "loading" });
  const [view, setView] = useState<"journey" | "versions">("journey");
  const [asOf, setAsOf] = useState("");
  const selector = useRef<Selector>(undefined);

  const loadDocuments = useCallback(async () => {
    try {
      const result = await api.listDocuments();
      setDocuments({ phase: "ok", value: result.documents });
      if (documentId === null && result.documents[0] !== undefined) {
        onOpenDocument(result.documents[0].document_id);
      }
    } catch {
      setDocuments({ phase: "error" });
    }
  }, [api, documentId, onOpenDocument]);

  const loadJourney = useCallback(
    async (next: Selector = selector.current) => {
      if (documentId === null) return;
      selector.current = next;
      try {
        setJourney({ phase: "ok", value: await api.documentJourney(documentId, next) });
      } catch (error) {
        if (error instanceof ApiError && error.status === 403) {
          setJourney({ phase: "refused" });
        } else {
          setJourney({ phase: "error" });
        }
      }
    },
    [api, documentId],
  );

  useEffect(() => {
    const initial = window.setTimeout(() => void loadDocuments(), 0);
    const timer = window.setInterval(() => void loadDocuments(), 2_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [loadDocuments]);

  useEffect(() => {
    selector.current = undefined;
    if (documentId === null) return;
    const initial = window.setTimeout(() => {
      setJourney({ phase: "loading" });
      void loadJourney(undefined);
    }, 0);
    const timer = window.setInterval(() => void loadJourney(), 2_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [documentId, loadJourney]);

  if (documents.phase === "loading") return <p role="status">Loading documents…</p>;
  if (documents.phase === "error") {
    return <LoadError things="documents" onRetry={() => void loadDocuments()} />;
  }
  if (documents.phase === "refused") {
    return <Refused thing="documents" page="Library" />;
  }
  if (documents.value.length === 0) {
    return <Empty message="No documents are visible to this identity." />;
  }

  return (
    <div className="ne-library__tab ne-docjourney">
      <div className="ne-docjourney__chooser">
        <label htmlFor="ne-document-picker">Document</label>
        <select
          id="ne-document-picker"
          value={documentId ?? ""}
          onChange={(event) => onOpenDocument(event.target.value)}
        >
          {documents.value.map((document) => (
            <option key={document.document_id} value={document.document_id}>
              {document.display_name}
              {document.indexing ? " — ingesting" : ""}
            </option>
          ))}
        </select>
      </div>
      {journey.phase === "loading" ? <p role="status">Loading document journey…</p> : null}
      {journey.phase === "refused" ? <Refused thing="this document" page="Library" /> : null}
      {journey.phase === "error" ? (
        <LoadError things="the document journey" onRetry={() => void loadJourney()} />
      ) : null}
      {journey.phase === "ok" ? (
        <>
          <div className="ne-docjourney__recordhead">
            <div>
              <h2>{journey.value.display_name}</h2>
              <span>{journey.value.source_system}</span>
            </div>
            <HashChip
              hash={
                journey.value.versions.find(
                  (version) => version.version_seq === journey.value.selected_version,
                )?.content_hash
              }
              label="Copy selected content hash"
            />
          </div>
          <div role="tablist" aria-label="Document journey sections" className="ne-library__tabs">
            <button
              type="button"
              role="tab"
              aria-selected={view === "journey"}
              className={`ne-library__tabbtn${view === "journey" ? " ne-library__tabbtn--on" : ""}`}
              onClick={() => setView("journey")}
            >
              JOURNEY
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={view === "versions"}
              className={`ne-library__tabbtn${view === "versions" ? " ne-library__tabbtn--on" : ""}`}
              onClick={() => setView("versions")}
            >
              VERSIONS
            </button>
          </div>
          {view === "journey" ? (
            <>
              <Pipeline stages={journey.value.stages} />
              <div className="ne-docjourney__asof">
                <label htmlFor="ne-document-asof">Retrieve as of</label>
                <input
                  id="ne-document-asof"
                  type="datetime-local"
                  value={asOf}
                  onChange={(event) => setAsOf(event.target.value)}
                />
                <button
                  type="button"
                  className="ne-btn ne-btn--quiet"
                  disabled={asOf === ""}
                  onClick={() => void loadJourney({ as_of: new Date(asOf).toISOString() })}
                >
                  Retrieve
                </button>
                <button
                  type="button"
                  className="ne-btn ne-btn--quiet"
                  onClick={() => {
                    setAsOf("");
                    selector.current = undefined;
                    void loadJourney();
                  }}
                >
                  Latest
                </button>
              </div>
              <pre className="ne-docjourney__content">{journey.value.selected_content}</pre>
              {journey.value.diff === null ? null : (
                <pre className="ne-docjourney__diff" aria-label="Version diff">
                  {journey.value.diff}
                </pre>
              )}
            </>
          ) : (
            <Versions
              journey={journey.value}
              onVersion={(version) => {
                setView("journey");
                void loadJourney({ version });
              }}
            />
          )}
        </>
      ) : null}
    </div>
  );
}
