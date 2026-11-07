// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { Link } from "react-router";
import type {
  DocumentGraphEntityWire,
  DocumentGraphEvidenceWire,
  DocumentGraphRelationshipWire,
  DocumentSemanticChunksWire,
  DocumentStageArtifactWire,
  DocumentStageEvidenceWire,
  DocumentStageStatusWire,
} from "../../api/types";
import { HashChip } from "../../primitives/HashChip";
import { StatePill } from "../../primitives/StatePill";
import type { StateWord } from "../../primitives/StatePill";
import { graphEntityHref } from "../graph/graphLinks";

const GRAPH_PANEL_LIMIT = 20;

const STATUS_WORD: Record<DocumentStageStatusWire, StateWord> = {
  waiting: "PENDING",
  active: "SYNCING",
  complete: "OK",
  skipped: "IDLE",
  unavailable: "UNAVAILABLE",
};

const STATUS_DETAIL: Record<DocumentStageStatusWire, string> = {
  waiting: "Waiting for evidence",
  active: "Processing",
  complete: "Completed",
  skipped: "Skipped — no completion implied",
  unavailable: "Evidence unavailable — no completion implied",
};

function displayTime(value: string | null): string {
  if (value === null) return "Unavailable";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

function displayValue(value: string | number | boolean | null): string {
  if (value === null) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

function ArtifactFacts({ artifact }: { artifact: DocumentStageArtifactWire }) {
  const facts = [
    ["Identifier", artifact.identifier],
    ["Size", artifact.size_bytes === null ? null : `${artifact.size_bytes.toLocaleString()} bytes`],
    ["Count", artifact.artifact_count],
    ["Dimensions", artifact.dimensions],
    ["Model", artifact.model],
    ["Index reference", artifact.index_reference],
    ["Fingerprint", artifact.fingerprint],
  ].filter((fact): fact is [string, string | number] => fact[1] !== null);

  if (facts.length === 0) return null;
  return (
    <dl className="ne-documents__artifactfacts">
      {facts.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd className={label === "Identifier" || label === "Fingerprint" ? "ne-mono" : undefined}>
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function artifactSummary(artifact: DocumentStageArtifactWire): string | null {
  switch (artifact.state) {
    case "available":
      return null;
    case "empty":
      return artifact.reason ?? "No artifacts were recorded.";
    case "skipped":
      return artifact.reason ?? "Skipped.";
    case "unavailable":
      return artifact.reason === "unbound_audit_receipt"
        ? "Unavailable because no committed audit receipt is bound."
        : "Not recorded for this version.";
    case "withheld":
      return "Access refused: artifact details were withheld for this identity.";
  }
}

function Artifact({ artifact }: { artifact: DocumentStageArtifactWire }) {
  const summary = artifactSummary(artifact);
  return (
    <article className="ne-documents__artifact">
      <header>
        <h4>
          {artifact.label} {artifact.ordinal + 1}
        </h4>
        <span className="ne-documents__artifactstate">{artifact.state.replaceAll("_", " ")}</span>
      </header>
      {summary === null ? null : <p>{summary}</p>}
      <ArtifactFacts artifact={artifact} />
      {artifact.numeric_preview === null ? null : (
        <div className="ne-documents__numeric-preview">
          <h5>
            Numeric preview (first {artifact.numeric_preview.length} of {artifact.dimensions})
          </h5>
          <ol>
            {artifact.numeric_preview.map((value, index) => (
              <li key={index} className="ne-mono">
                {value}
              </li>
            ))}
          </ol>
        </div>
      )}
    </article>
  );
}

function SemanticChunks({ evidence }: { evidence: DocumentSemanticChunksWire }) {
  if (evidence.state === "unavailable") {
    return (
      <section aria-labelledby="ne-document-semantic-chunks">
        <h4 id="ne-document-semantic-chunks">Semantic chunks</h4>
        <p className="ne-documents__projectiongap">
          <strong>Semantic evidence unavailable</strong>
          {" — "}
          <span>
            {evidence.reason === "unbound_audit_receipt"
              ? "Semantic evidence is unavailable because no committed audit receipt is bound."
              : "Semantic chunks were not recorded for this version."}
          </span>
        </p>
      </section>
    );
  }
  return (
    <section aria-labelledby="ne-document-semantic-chunks">
      <h4 id="ne-document-semantic-chunks">Semantic chunks</h4>
      <p>
        Version-bound semantic chunk receipts visible to this identity. The separately labelled
        record chunk remains the indexed T1 artifact.
      </p>
      <div className="ne-documents__artifacts">
        {evidence.chunks.map((chunk, index) => (
          <article className="ne-documents__artifact" key={chunk.identifier}>
            <h5>Semantic chunk {index + 1}</h5>
            <dl className="ne-documents__artifactfacts">
              <div>
                <dt>Identifier</dt>
                <dd className="ne-mono">{chunk.identifier}</dd>
              </div>
              <div>
                <dt>Size</dt>
                <dd>{chunk.size_bytes.toLocaleString()} bytes</dd>
              </div>
            </dl>
          </article>
        ))}
      </div>
      {evidence.truncated ? (
        <p role="status">The governed semantic chunk projection was truncated.</p>
      ) : null}
    </section>
  );
}

function EntityRow({ entity }: { entity: DocumentGraphEntityWire }) {
  return (
    <li>
      <div>
        <strong>{entity.display_name}</strong>
        <span>
          {entity.entity_class} · {entity.source_system}
        </span>
        <span className="ne-mono">Evidence key: {entity.key}</span>
      </div>
      <Link to={graphEntityHref(entity.key, { mode: "record" })}>Open record</Link>
    </li>
  );
}

function RelationshipRow({ relationship }: { relationship: DocumentGraphRelationshipWire }) {
  return (
    <li>
      <div>
        <strong>{relationship.edge_kind}</strong>
        <span>
          {relationship.subject.display_name} → {relationship.object.display_name}
        </span>
        <span className="ne-mono">Evidence key: {relationship.key}</span>
      </div>
      <div className="ne-documents__graphlinks">
        <Link to={graphEntityHref(relationship.subject.key, { mode: "graph" })}>
          Subject neighborhood
        </Link>
        <Link to={graphEntityHref(relationship.object.key, { mode: "graph" })}>
          Object neighborhood
        </Link>
      </div>
    </li>
  );
}

function GraphEvidence({ evidence }: { evidence: DocumentGraphEvidenceWire | null }) {
  if (evidence === null) {
    return (
      <p className="ne-documents__projectiongap">Version-bound graph evidence is unavailable.</p>
    );
  }
  if (evidence.state !== "available") {
    const stateLabel = {
      empty: "No visible graph evidence",
      skipped: "Graph stage skipped",
      unavailable: "Graph evidence unavailable",
    }[evidence.state];
    return (
      <p className="ne-documents__projectiongap">
        <strong>{stateLabel}</strong>
        {" — "}
        <span>{evidence.reason}</span>
      </p>
    );
  }

  const entities = evidence.entities.slice(0, GRAPH_PANEL_LIMIT);
  const relationships = evidence.relationships.slice(0, GRAPH_PANEL_LIMIT);
  const locallyCapped =
    evidence.entities.length > entities.length ||
    evidence.relationships.length > relationships.length;
  return (
    <div className="ne-documents__graph">
      {entities.length === 0 ? null : (
        <section aria-labelledby="ne-document-graph-entities">
          <h4 id="ne-document-graph-entities">Created entities</h4>
          <ol>
            {entities.map((entity, index) => (
              <EntityRow key={`${entity.key}:${index}`} entity={entity} />
            ))}
          </ol>
        </section>
      )}
      {relationships.length === 0 ? null : (
        <section aria-labelledby="ne-document-graph-relationships">
          <h4 id="ne-document-graph-relationships">Created relationships</h4>
          <ol>
            {relationships.map((relationship, index) => (
              <RelationshipRow key={`${relationship.key}:${index}`} relationship={relationship} />
            ))}
          </ol>
        </section>
      )}
      {locallyCapped ? (
        <p role="status">Showing the first {GRAPH_PANEL_LIMIT} served rows per evidence kind.</p>
      ) : null}
      {evidence.truncated ? (
        <p role="status">The governed server projection was truncated after ACL filtering.</p>
      ) : null}
    </div>
  );
}

export function DocumentStagePane({
  evidence,
  onSelectVersion,
}: {
  evidence: DocumentStageEvidenceWire;
  onSelectVersion?: (version: number) => void;
}) {
  return (
    <section className="ne-documents__stagepane" aria-labelledby="ne-document-stage-heading">
      <header className="ne-documents__sectionhead">
        <div>
          <h3 id="ne-document-stage-heading">{evidence.stage.toUpperCase()} evidence</h3>
          <p>{evidence.detail}</p>
        </div>
        <StatePill word={STATUS_WORD[evidence.status]} />
      </header>

      <dl className="ne-documents__stagecontext">
        <div>
          <dt>Version</dt>
          <dd>v{evidence.version}</dd>
        </div>
        <div>
          <dt>Stage outcome</dt>
          <dd>{STATUS_DETAIL[evidence.status]}</dd>
        </div>
        <div>
          <dt>Content hash</dt>
          <dd>
            <HashChip hash={evidence.content_hash} label="Copy stage content hash" />
          </dd>
        </div>
        <div>
          <dt>Occurred</dt>
          <dd>{displayTime(evidence.occurred_at)}</dd>
        </div>
        <div>
          <dt>Audit</dt>
          <dd>
            {evidence.audit_seq === null ? (
              "Audit unavailable"
            ) : (
              <Link to={`/audit?seq=${evidence.audit_seq}`}>Audit #{evidence.audit_seq}</Link>
            )}
          </dd>
        </div>
      </dl>

      {evidence.facts.length === 0 ? null : (
        <dl className="ne-documents__facts">
          {evidence.facts.map((fact) => (
            <div key={fact.key}>
              <dt>{fact.label}</dt>
              <dd>{fact.available ? displayValue(fact.value) : "Not available"}</dd>
            </div>
          ))}
        </dl>
      )}

      {evidence.stage === "supersede" ? (
        evidence.comparison.predecessor_version === null ? (
          <p className="ne-documents__projectiongap">
            {evidence.comparison.state === "first_version"
              ? "First version — nothing superseded."
              : evidence.comparison.state === "predecessor_hidden"
                ? "The predecessor is not visible to this identity."
                : "No served predecessor relationship is available."}
          </p>
        ) : (
          <div className="ne-documents__supersede">
            <span>Served predecessor v{evidence.comparison.predecessor_version}</span>
            <a href="#ne-document-diff-heading">Open version comparison</a>
            {onSelectVersion === undefined ? null : (
              <button
                type="button"
                className="ne-btn ne-btn--quiet"
                onClick={() => onSelectVersion(evidence.comparison.predecessor_version!)}
              >
                Open predecessor
              </button>
            )}
          </div>
        )
      ) : null}

      {evidence.stage === "chunk" && evidence.semantic_chunks !== null ? (
        <SemanticChunks evidence={evidence.semantic_chunks} />
      ) : null}

      {evidence.stage === "graph" ? (
        <GraphEvidence evidence={evidence.graph_evidence} />
      ) : evidence.artifacts.length > 0 ? (
        <div className="ne-documents__artifacts">
          {evidence.artifacts.map((artifact) => (
            <Artifact key={`${artifact.kind}:${artifact.ordinal}`} artifact={artifact} />
          ))}
        </div>
      ) : null}
    </section>
  );
}
