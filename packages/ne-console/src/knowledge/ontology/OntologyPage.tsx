// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useAuth } from "react-oidc-context";
import { useSearchParams } from "react-router";
import { ApiError } from "../../api/client";
import type { OntologyInspectionClient } from "../../api/ontologyInspectionClient";
import { HashChip } from "../../primitives/HashChip";
import { ListScreen } from "../../primitives/PageHeader";
import { Empty, LoadError, Loading, Refused } from "../../primitives/states";
import { Toolbar } from "../../primitives/Toolbar";
import { absoluteTime, formatCount } from "../../primitives/format";
import { T3StoreCallout } from "../../verified-queries/T3StoreCallout";
import { useOntologyInspectionClient } from "./useOntologyInspectionClient";
import type {
  ActiveOntologyModel,
  OntologyConcept,
  OntologyInspectionResponse,
  OntologyIssueDetail,
  OntologyIssueList,
  OntologyRelationship,
  OntologyRule,
  OntologySection,
  OntologyView,
} from "./types";
import "./ontology.css";

const ALL_SECTIONS: ReadonlyArray<{ id: OntologySection; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "model", label: "Model explorer" },
  { id: "rules", label: "Validation rules" },
  { id: "issues", label: "Validation issues" },
  { id: "standards", label: "Standards" },
];

// The "Validation issues" section is the write-quarantine exhibit. It exists
// only where the quarantine lane is actually configured — a deployment without
// it gets no tab at all rather than a tab opening onto an empty state (the same
// call as the Capabilities health column: don't show what cannot be reported).
function sectionsFor(response: OntologyInspectionResponse | null) {
  return response?.issue_tracking.state === "configured"
    ? ALL_SECTIONS
    : ALL_SECTIONS.filter((entry) => entry.id !== "issues");
}

const VIEWS: ReadonlyArray<{ id: OntologyView; label: string }> = [
  { id: "tree", label: "Tree" },
  { id: "list", label: "List" },
  { id: "relationships", label: "Relationships" },
];

type ReadPhase =
  "loading" | "ok" | "refused" | "unconfigured" | "unavailable" | "not_found" | "error";
type BoundRead<T> = {
  client: OntologyInspectionClient;
  key: string;
  phase: ReadPhase;
  value?: T;
};

function sectionFrom(
  value: string | null,
  sections: ReadonlyArray<{ id: OntologySection; label: string }>,
): OntologySection {
  return sections.some((entry) => entry.id === value) ? (value as OntologySection) : "overview";
}

function viewFrom(value: string | null): OntologyView {
  return VIEWS.some((entry) => entry.id === value) ? (value as OntologyView) : "tree";
}

function failurePhase(error: unknown): Exclude<ReadPhase, "loading" | "ok"> {
  if (!(error instanceof ApiError)) return "error";
  if (error.status === 403) return "refused";
  if (error.status === 404) return "not_found";
  if (error.status === 503 && error.detail === "issue-tracking-not-configured") {
    return "unconfigured";
  }
  return "error";
}

function useModelRead(client: OntologyInspectionClient) {
  const [attempt, setAttempt] = useState(0);
  const key = String(attempt);
  const [settled, setSettled] = useState<BoundRead<OntologyInspectionResponse> | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void client
      .readModel(controller.signal)
      .then((value) => {
        if (active) setSettled({ client, key, phase: "ok", value });
      })
      .catch((error: unknown) => {
        if (active && !controller.signal.aborted) {
          setSettled({ client, key, phase: failurePhase(error) });
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [client, key]);

  const state: BoundRead<OntologyInspectionResponse> =
    settled?.client === client && settled.key === key ? settled : { client, key, phase: "loading" };
  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  return { state, retry };
}

function useIssueListRead(
  client: OntologyInspectionClient,
  tracking: OntologyInspectionResponse["issue_tracking"] | null,
) {
  const [attempt, setAttempt] = useState(0);
  const trackingKey = tracking === null ? "pending" : `${tracking.state}:${tracking.count ?? "-"}`;
  const key = `${trackingKey}:${attempt}`;
  const [settled, setSettled] = useState<BoundRead<OntologyIssueList> | null>(null);

  useEffect(() => {
    if (tracking?.state !== "configured") return;
    const controller = new AbortController();
    let active = true;
    void client
      .listIssues(controller.signal)
      .then((value) => {
        if (active) setSettled({ client, key, phase: "ok", value });
      })
      .catch((error: unknown) => {
        if (active && !controller.signal.aborted) {
          setSettled({ client, key, phase: failurePhase(error) });
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [client, key, tracking?.state]);

  let state: BoundRead<OntologyIssueList>;
  if (tracking === null) state = { client, key, phase: "loading" };
  else if (tracking.state === "unconfigured") state = { client, key, phase: "unconfigured" };
  else if (tracking.state === "unavailable") state = { client, key, phase: "unavailable" };
  else if (settled?.client === client && settled.key === key) state = settled;
  else state = { client, key, phase: "loading" };

  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  return { state, retry };
}

function useIssueDetailRead(
  client: OntologyInspectionClient,
  issueId: string | null,
  enabled: boolean,
) {
  const [attempt, setAttempt] = useState(0);
  const key = `${issueId ?? "none"}:${attempt}`;
  const [settled, setSettled] = useState<BoundRead<OntologyIssueDetail> | null>(null);

  useEffect(() => {
    if (!enabled || issueId === null) return;
    const controller = new AbortController();
    let active = true;
    void client
      .readIssue(issueId, controller.signal)
      .then((value) => {
        if (active) setSettled({ client, key, phase: "ok", value });
      })
      .catch((error: unknown) => {
        if (active && !controller.signal.aborted) {
          setSettled({ client, key, phase: failurePhase(error) });
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [client, enabled, issueId, key]);

  const state: BoundRead<OntologyIssueDetail> | null =
    !enabled || issueId === null
      ? null
      : settled?.client === client && settled.key === key
        ? settled
        : { client, key, phase: "loading" };
  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  return { state, retry };
}

function Tabs({
  sections,
  selected,
  onSelect,
}: {
  sections: ReadonlyArray<{ id: OntologySection; label: string }>;
  selected: OntologySection;
  onSelect: (id: OntologySection) => void;
}) {
  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]'));
      const index = tabs.findIndex((tab) => tab === event.target);
      if (index < 0) return;
      let next: number;
      if (event.key === "ArrowRight") next = (index + 1) % sections.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + sections.length) % sections.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = sections.length - 1;
      else return;
      event.preventDefault();
      const target = sections[next];
      if (target) {
        tabs[next]?.focus();
        onSelect(target.id);
      }
    },
    [onSelect, sections],
  );
  return (
    <div
      className="ne-ontology__tabs"
      role="tablist"
      aria-label="Ontology sections"
      aria-orientation="horizontal"
      onKeyDown={onKeyDown}
    >
      {sections.map((entry) => (
        <button
          key={entry.id}
          type="button"
          role="tab"
          id={`ne-ontology-tab-${entry.id}`}
          aria-controls={`ne-ontology-panel-${entry.id}`}
          aria-selected={selected === entry.id}
          tabIndex={selected === entry.id ? 0 : -1}
          onClick={() => onSelect(entry.id)}
        >
          {entry.label}
        </button>
      ))}
    </div>
  );
}

function Panel({ section, children }: { section: OntologySection; children: ReactNode }) {
  return (
    <section
      className="ne-ontology__panel"
      role="tabpanel"
      id={`ne-ontology-panel-${section}`}
      aria-labelledby={`ne-ontology-tab-${section}`}
    >
      {children}
    </section>
  );
}

function StateCard({ label, value, tone }: { label: string; value: string; tone: string }) {
  return (
    <div className={`ne-ontology__state ne-ontology__state--${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Overview({ response }: { response: OntologyInspectionResponse }) {
  const model = response.model;
  const graph = response.ontology_graph;
  const tracking = response.issue_tracking;
  return (
    <div className="ne-ontology__overview">
      <div className="ne-ontology__states" aria-label="Ontology state">
        <StateCard
          label="Active model"
          value={model?.provenance.title ?? "Model unavailable"}
          tone={response.artifact_health.state}
        />
        <StateCard
          label="Concepts"
          value={model === null ? "Unavailable" : formatCount(model.concept_count)}
          tone={model === null ? "unavailable" : "healthy"}
        />
        <StateCard
          label="Validation rules"
          value={model === null ? "Unavailable" : formatCount(model.rule_count)}
          tone={model === null ? "unavailable" : "healthy"}
        />
        <StateCard
          label="Generated artifacts"
          value={response.artifact_health.state}
          tone={response.artifact_health.state}
        />
        {/* The optional BFO/CCO reference graph gets a card when it is loaded
            (a count) or genuinely failing (an honest fault). A deployment that
            simply doesn't include it shows nothing — absence of an optional
            feature is not a state worth a card. */}
        {graph.state === "loaded" ? (
          <StateCard
            label="Optional ontology graph"
            value={`${formatCount(graph.optional_class_count ?? 0)} optional classes loaded`}
            tone="loaded"
          />
        ) : null}
        {graph.state === "unavailable" ? (
          <StateCard label="Optional ontology graph" value="Unavailable" tone="unavailable" />
        ) : null}
        {/* Open-issue state is only shown where the quarantine lane can report
            it. A separate "validation health" indicator was removed outright:
            it was derived one-to-one from this same quarantine read and could
            never say anything this card doesn't. */}
        {tracking.state === "configured" ? (
          <StateCard
            label="Validation issues"
            value={
              (tracking.count ?? 0) === 0
                ? "No open issues"
                : `${formatCount(tracking.count ?? 0)} open issues`
            }
            tone={(tracking.count ?? 0) === 0 ? "healthy" : "issues"}
          />
        ) : null}
      </div>
      <div className="ne-ontology__tiers" aria-label="Knowledge tiers">
        <article>
          <strong>T1 · Documents</strong>
          <span>Governed source content and evidence.</span>
        </article>
        <article>
          <strong>T2 · Graph</strong>
          <span>Principal-visible records and relationships.</span>
        </article>
        <article>
          <strong>T3 · Ontology</strong>
          <span>Shared concepts and constraints those records follow.</span>
        </article>
      </div>
      <p className="ne-ontology__shared">
        Model definitions are shared metadata. Documents and Graph instances remain
        permission-trimmed for the signed-in person.
      </p>
      {model ? (
        <details className="ne-ontology__technical">
          <summary>Technical provenance</summary>
          <dl>
            <dt>Schema version</dt>
            <dd>{model.provenance.schema_version}</dd>
            <dt>LinkML version</dt>
            <dd>{model.provenance.linkml_version}</dd>
            <dt>Source artifact</dt>
            <dd>
              <code>{model.provenance.source_artifact}</code>
            </dd>
            <dt>Source fingerprint</dt>
            <dd>
              <HashChip hash={model.provenance.source_sha256} label="Copy source fingerprint" />
            </dd>
            <dt>JSON Schema fingerprint</dt>
            <dd>
              <HashChip
                hash={model.provenance.json_schema_sha256}
                label="Copy JSON Schema fingerprint"
              />
            </dd>
            <dt>Validation fingerprint</dt>
            <dd>
              <HashChip hash={model.provenance.shapes_sha256} label="Copy validation fingerprint" />
            </dd>
          </dl>
        </details>
      ) : null}
    </div>
  );
}

function matchesConcept(concept: OntologyConcept, query: string): boolean {
  const haystack = [
    concept.name,
    concept.label,
    concept.description ?? "",
    ...concept.fields.flatMap((field) => [field.name, field.label, field.description ?? ""]),
  ]
    .join(" ")
    .toLocaleLowerCase();
  return haystack.includes(query.trim().toLocaleLowerCase());
}

function ConceptButton({
  concept,
  selected,
  onSelect,
}: {
  concept: OntologyConcept;
  selected: boolean;
  onSelect: (name: string) => void;
}) {
  return (
    <button
      type="button"
      className={`ne-ontology__concept-button${selected ? " ne-ontology__concept-button--selected" : ""}`}
      aria-current={selected ? "true" : undefined}
      onClick={() => onSelect(concept.name)}
    >
      <strong>{concept.label}</strong>
      {concept.abstract ? <span>Abstract</span> : null}
    </button>
  );
}

function ConceptTree({
  model,
  shown,
  selected,
  onSelect,
}: {
  model: ActiveOntologyModel;
  shown: Set<string>;
  selected: string | null;
  onSelect: (name: string) => void;
}) {
  const itemRefs = useRef(new Map<string, HTMLLIElement>());
  const byName = new Map(model.concepts.map((concept) => [concept.name, concept]));
  const visible = (concept: OntologyConcept): boolean =>
    shown.has(concept.name) ||
    concept.children.some((name) => byName.get(name) && visible(byName.get(name)!));
  const childrenOf = (concept: OntologyConcept): OntologyConcept[] =>
    concept.children
      .map((name) => byName.get(name))
      .filter((entry): entry is OntologyConcept => entry !== undefined && visible(entry));
  const roots = model.concepts.filter(
    (concept) => concept.parents.length === 0 && visible(concept),
  );
  const flattened: OntologyConcept[] = [];
  const parentByName = new Map<string, string>();
  const levelByName = new Map<string, number>();
  const collect = (concept: OntologyConcept, parent: string | null, level: number) => {
    flattened.push(concept);
    levelByName.set(concept.name, level);
    if (parent !== null) parentByName.set(concept.name, parent);
    for (const child of childrenOf(concept)) collect(child, concept.name, level + 1);
  };
  for (const root of roots) collect(root, null, 1);
  const rovingName =
    selected !== null && flattened.some((concept) => concept.name === selected)
      ? selected
      : (flattened[0]?.name ?? null);

  const focusAndSelect = (name: string) => {
    itemRefs.current.get(name)?.focus();
    onSelect(name);
  };
  const onTreeItemKeyDown = (event: KeyboardEvent<HTMLLIElement>, concept: OntologyConcept) => {
    if (event.target !== event.currentTarget) return;
    const index = flattened.findIndex((entry) => entry.name === concept.name);
    let target: OntologyConcept | undefined;
    if (event.key === "ArrowDown") target = flattened[index + 1];
    else if (event.key === "ArrowUp") target = flattened[index - 1];
    else if (event.key === "Home") target = flattened[0];
    else if (event.key === "End") target = flattened.at(-1);
    else if (event.key === "ArrowRight") target = childrenOf(concept)[0];
    else if (event.key === "ArrowLeft") {
      const parent = parentByName.get(concept.name);
      target = parent === undefined ? undefined : byName.get(parent);
    } else if (event.key === "Enter" || event.key === " ") target = concept;
    else return;
    if (target === undefined) return;
    event.preventDefault();
    event.stopPropagation();
    focusAndSelect(target.name);
  };

  const render = (concept: OntologyConcept): ReactNode => {
    if (!visible(concept)) return null;
    const children = childrenOf(concept);
    const isSelected = selected === concept.name;
    return (
      <li
        key={concept.name}
        ref={(node) => {
          if (node === null) itemRefs.current.delete(concept.name);
          else itemRefs.current.set(concept.name, node);
        }}
        role="treeitem"
        aria-label={concept.label}
        aria-level={levelByName.get(concept.name)}
        aria-selected={isSelected}
        aria-expanded={children.length ? true : undefined}
        tabIndex={rovingName === concept.name ? 0 : -1}
        onClick={(event) => {
          event.stopPropagation();
          focusAndSelect(concept.name);
        }}
        onKeyDown={(event) => onTreeItemKeyDown(event, concept)}
      >
        <div
          className={`ne-ontology__concept-button${isSelected ? " ne-ontology__concept-button--selected" : ""}`}
        >
          <strong>{concept.label}</strong>
          {concept.abstract ? <span>Abstract</span> : null}
        </div>
        {children.length ? <ul role="group">{children.map(render)}</ul> : null}
      </li>
    );
  };
  return (
    <ul className="ne-ontology__tree" role="tree" aria-label="Concept tree">
      {roots.map(render)}
    </ul>
  );
}

function ConceptDetail({
  concept,
  onSelect,
}: {
  concept: OntologyConcept;
  onSelect: (name: string) => void;
}) {
  return (
    <article className="ne-ontology__concept" aria-label={`${concept.label} details`}>
      <header>
        <div>
          <h3>{concept.label}</h3>
          <p>{concept.description ?? "No description supplied."}</p>
        </div>
        {concept.abstract ? <span className="ne-ontology__badge">Abstract</span> : null}
      </header>
      <h4>Fields</h4>
      <div className="ne-ontology__field-table-wrap">
        <table className="ne-ontology__field-table">
          <thead>
            <tr>
              <th>Field</th>
              <th>Type</th>
              <th>Cardinality</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            {concept.fields.map((field) => (
              <tr key={field.name}>
                <th scope="row">
                  {field.label}
                  {field.required ? <span className="ne-ontology__required">Required</span> : null}
                </th>
                <td>{field.value_type}</td>
                <td>
                  {field.multivalued ? "Many" : field.required ? "Exactly one" : "Zero or one"}
                </td>
                <td>
                  {field.description ?? "—"}
                  {field.enum_values.length ? (
                    <small>Allowed: {field.enum_values.join(", ")}</small>
                  ) : null}
                  {field.related_concepts.map((name) => (
                    <button
                      key={name}
                      type="button"
                      className="ne-ontology__inline-link"
                      onClick={() => onSelect(name)}
                    >
                      Related: {name}
                    </button>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details className="ne-ontology__technical">
        <summary>Technical details</summary>
        <dl>
          <dt>Canonical IRI</dt>
          <dd>
            <code>{concept.canonical_iri}</code>
          </dd>
          <dt>Generated class</dt>
          <dd>
            <code>{concept.name}</code>
          </dd>
          <dt>Parents</dt>
          <dd>{concept.parents.join(", ") || "None"}</dd>
          <dt>Children</dt>
          <dd>{concept.children.join(", ") || "None"}</dd>
        </dl>
      </details>
    </article>
  );
}

const GRAPH_NODE_HEIGHT = 30;

// A deterministic ellipse layout: every concept named by a visible relationship
// sits on one ring, edges run centre-to-centre trimmed to the node boundary.
// Real model data only — nodes are the generated concepts, edges the generated
// relationships — so the picture can never claim structure the model lacks.
function RelationshipGraph({
  model,
  relationships,
  selected,
  onSelect,
}: {
  model: ActiveOntologyModel;
  relationships: OntologyRelationship[];
  selected: string | null;
  onSelect: (name: string) => void;
}) {
  const labelByName = new Map(model.concepts.map((concept) => [concept.name, concept.label]));
  const names: string[] = [];
  for (const relation of relationships) {
    for (const name of [relation.source_concept, relation.target_concept]) {
      if (!names.includes(name)) names.push(name);
    }
  }
  if (names.length === 0) return null;

  const widthOf = (name: string) => (labelByName.get(name) ?? name).length * 7.2 + 24;
  const maxWidth = Math.max(...names.map(widthOf));
  const count = names.length;
  const ry = Math.max(96, count * 24);
  const rx = ry * 1.5 + maxWidth / 2;
  const viewWidth = 2 * (rx + maxWidth / 2 + 12);
  const viewHeight = 2 * (ry + GRAPH_NODE_HEIGHT / 2 + 20);
  const nodes = new Map(
    names.map((name, index) => {
      const angle = -Math.PI / 2 + (2 * Math.PI * index) / count;
      return [
        name,
        {
          name,
          label: labelByName.get(name) ?? name,
          x: viewWidth / 2 + rx * Math.cos(angle),
          y: viewHeight / 2 + ry * Math.sin(angle),
          width: widthOf(name),
        },
      ] as const;
    }),
  );

  // Trim a centre-to-centre segment to the node's rectangular boundary so the
  // arrowhead lands on the box edge instead of under it.
  const trim = (x: number, y: number, node: { x: number; y: number; width: number }) => {
    const dx = x - node.x;
    const dy = y - node.y;
    const scale =
      1 / Math.max(Math.abs(dx) / (node.width / 2 + 6), Math.abs(dy) / (GRAPH_NODE_HEIGHT / 2 + 6));
    return { x: node.x + dx * scale, y: node.y + dy * scale };
  };

  const pairCounts = new Map<string, number>();
  const pairSeen = new Map<string, number>();
  const pairKey = (relation: OntologyRelationship) =>
    [relation.source_concept, relation.target_concept].sort().join("\u0000");
  for (const relation of relationships) {
    pairCounts.set(pairKey(relation), (pairCounts.get(pairKey(relation)) ?? 0) + 1);
  }

  return (
    <figure className="ne-ontology__graph">
      <svg
        viewBox={`0 0 ${viewWidth} ${viewHeight}`}
        role="group"
        aria-label="Concept relationship graph"
        preserveAspectRatio="xMidYMid meet"
      >
        <defs>
          <marker
            id="ne-ontology-graph-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" className="ne-ontology__graph-arrow" />
          </marker>
        </defs>
        {relationships.map((relation) => {
          const source = nodes.get(relation.source_concept)!;
          const target = nodes.get(relation.target_concept)!;
          const key = `${relation.source_concept}:${relation.field_name}:${relation.target_concept}:${relation.kind}`;
          if (relation.source_concept === relation.target_concept) {
            const loopTop = source.y - GRAPH_NODE_HEIGHT / 2;
            return (
              <g key={key} aria-hidden="true">
                <path
                  className="ne-ontology__graph-edge"
                  markerEnd="url(#ne-ontology-graph-arrow)"
                  d={`M ${source.x + 12} ${loopTop} C ${source.x + 44} ${loopTop - 40}, ${source.x - 44} ${loopTop - 40}, ${source.x - 12} ${loopTop - 2}`}
                />
                <text
                  className="ne-ontology__graph-edge-label"
                  x={source.x}
                  y={loopTop - 34}
                  textAnchor="middle"
                >
                  {relation.label}
                </text>
              </g>
            );
          }
          const seen = pairSeen.get(pairKey(relation)) ?? 0;
          pairSeen.set(pairKey(relation), seen + 1);
          const siblings = pairCounts.get(pairKey(relation)) ?? 1;
          const spread = (seen - (siblings - 1) / 2) * 16;
          const length = Math.hypot(target.x - source.x, target.y - source.y) || 1;
          const offsetX = (-(target.y - source.y) / length) * spread;
          const offsetY = ((target.x - source.x) / length) * spread;
          const from = trim(target.x + offsetX, target.y + offsetY, {
            ...source,
            x: source.x + offsetX,
            y: source.y + offsetY,
          });
          const to = trim(source.x + offsetX, source.y + offsetY, {
            ...target,
            x: target.x + offsetX,
            y: target.y + offsetY,
          });
          return (
            <g key={key} aria-hidden="true">
              <line
                className="ne-ontology__graph-edge"
                x1={from.x}
                y1={from.y}
                x2={to.x}
                y2={to.y}
                markerEnd="url(#ne-ontology-graph-arrow)"
              />
              <text
                className="ne-ontology__graph-edge-label"
                x={(from.x + to.x) / 2}
                y={(from.y + to.y) / 2 - 4}
                textAnchor="middle"
              >
                {relation.label}
              </text>
            </g>
          );
        })}
        {[...nodes.values()].map((node) => (
          <g
            key={node.name}
            role="button"
            tabIndex={0}
            aria-label={`${node.label} concept`}
            aria-pressed={selected === node.name}
            className={`ne-ontology__graph-node${selected === node.name ? " ne-ontology__graph-node--selected" : ""}`}
            onClick={() => onSelect(node.name)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect(node.name);
              }
            }}
          >
            <rect
              x={node.x - node.width / 2}
              y={node.y - GRAPH_NODE_HEIGHT / 2}
              width={node.width}
              height={GRAPH_NODE_HEIGHT}
              rx={GRAPH_NODE_HEIGHT / 2}
            />
            <text x={node.x} y={node.y + 4} textAnchor="middle">
              {node.label}
            </text>
          </g>
        ))}
      </svg>
    </figure>
  );
}

function ViewTabs({
  selected,
  onSelect,
}: {
  selected: OntologyView;
  onSelect: (view: OntologyView) => void;
}) {
  return (
    <div className="ne-ontology__view-tabs" role="group" aria-label="Model view">
      {VIEWS.map((entry) => (
        <button
          key={entry.id}
          type="button"
          aria-pressed={selected === entry.id}
          onClick={() => onSelect(entry.id)}
        >
          {entry.label}
        </button>
      ))}
    </div>
  );
}

function ModelExplorer({
  model,
  view,
  query,
  selectedName,
  onView,
  onQuery,
  onSelect,
}: {
  model: ActiveOntologyModel;
  view: OntologyView;
  query: string;
  selectedName: string | null;
  onView: (view: OntologyView) => void;
  onQuery: (query: string) => void;
  onSelect: (name: string) => void;
}) {
  const shown = useMemo(
    () =>
      new Set(
        model.concepts
          .filter((concept) => matchesConcept(concept, query))
          .map((concept) => concept.name),
      ),
    [model, query],
  );
  const selected = model.concepts.find((concept) => concept.name === selectedName) ?? null;
  const relationships = model.relationships.filter(
    (relation) =>
      !query.trim() ||
      [
        relation.kind,
        relation.label,
        relation.source_concept,
        relation.target_concept,
        relation.field_name,
      ]
        .join(" ")
        .toLowerCase()
        .includes(query.trim().toLowerCase()),
  );
  return (
    <div className="ne-ontology__explorer">
      <Toolbar
        search={{ value: query, onChange: onQuery, placeholder: "Filter concepts…" }}
        filters={<ViewTabs selected={view} onSelect={onView} />}
        count={`${formatCount(view === "relationships" ? relationships.length : shown.size)} ${view === "relationships" ? "relationships" : "concepts"}`}
      />
      {view === "relationships" ? (
        <RelationshipGraph
          model={model}
          relationships={relationships}
          selected={selectedName}
          onSelect={onSelect}
        />
      ) : null}
      <div className="ne-ontology__explorer-grid">
        <div className="ne-ontology__catalog">
          {view === "tree" ? (
            <ConceptTree model={model} shown={shown} selected={selectedName} onSelect={onSelect} />
          ) : null}
          {view === "list" ? (
            <table className="ne-ontology__list">
              <caption className="ne-visually-hidden">Concept list</caption>
              <thead>
                <tr>
                  <th>Concept</th>
                  <th>Parents</th>
                  <th>Fields</th>
                </tr>
              </thead>
              <tbody>
                {model.concepts
                  .filter((concept) => shown.has(concept.name))
                  .map((concept) => (
                    <tr key={concept.name} aria-selected={selectedName === concept.name}>
                      <th scope="row">
                        <ConceptButton
                          concept={concept}
                          selected={selectedName === concept.name}
                          onSelect={onSelect}
                        />
                      </th>
                      <td>{concept.parents.join(", ") || "—"}</td>
                      <td>{formatCount(concept.fields.length)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          ) : null}
          {view === "relationships" ? (
            <ul className="ne-ontology__relationships" aria-label="Concept relationships">
              {relationships.map((relation) => (
                <li
                  key={`${relation.source_concept}:${relation.field_name}:${relation.target_concept}:${relation.kind}`}
                >
                  <button type="button" onClick={() => onSelect(relation.source_concept)}>
                    {relation.source_concept}
                  </button>
                  <span>
                    <strong>{relation.label}</strong>
                    <small>
                      {relation.field_name}
                      {relation.required ? " · required" : ""}
                      {relation.multivalued ? " · many" : ""}
                    </small>
                  </span>
                  <span aria-hidden="true">→</span>
                  <button type="button" onClick={() => onSelect(relation.target_concept)}>
                    {relation.target_concept}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {(view === "relationships" ? relationships.length : shown.size) === 0 ? (
            <Empty message="No model items match this filter." />
          ) : null}
        </div>
        <div>
          {selected ? (
            <ConceptDetail concept={selected} onSelect={onSelect} />
          ) : (
            <Empty message="Select a concept to inspect its fields." />
          )}
        </div>
      </div>
    </div>
  );
}

function Rules({
  model,
  query,
  selectedId,
  onQuery,
  onSelect,
}: {
  model: ActiveOntologyModel;
  query: string;
  selectedId: string | null;
  onQuery: (query: string) => void;
  onSelect: (id: string) => void;
}) {
  const normalized = query.trim().toLowerCase();
  const rules = model.rules.filter(
    (rule) =>
      !normalized ||
      [rule.label, rule.purpose, rule.condition, rule.concept ?? ""]
        .join(" ")
        .toLowerCase()
        .includes(normalized),
  );
  const selected = model.rules.find((rule) => rule.rule_id === selectedId) ?? null;
  const grouped = new Map<string, OntologyRule[]>();
  for (const item of rules) {
    const group = item.concept ?? "Shared model";
    grouped.set(group, [...(grouped.get(group) ?? []), item]);
  }
  return (
    <div>
      <Toolbar
        search={{ value: query, onChange: onQuery, placeholder: "Filter validation rules…" }}
        count={`${formatCount(rules.length)} rules`}
      />
      <div className="ne-ontology__rules-layout">
        <div className="ne-ontology__rule-list">
          {[...grouped.entries()].map(([concept, entries]) => (
            <section key={concept}>
              <h3>{concept}</h3>
              <ul>
                {entries.map((rule) => (
                  <li key={rule.rule_id}>
                    <button
                      type="button"
                      aria-current={selectedId === rule.rule_id ? "true" : undefined}
                      onClick={() => onSelect(rule.rule_id)}
                    >
                      <strong>{rule.label}</strong>
                      <span>{rule.condition}</span>
                      <small>
                        {rule.severity} · {rule.scope}
                      </small>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
        {selected ? (
          <RuleDetail rule={selected} />
        ) : (
          <Empty message="Select a validation rule to inspect its purpose." />
        )}
      </div>
    </div>
  );
}

function RuleDetail({ rule }: { rule: OntologyRule }) {
  return (
    <article className="ne-ontology__rule-detail" aria-label={`${rule.label} rule details`}>
      <header>
        <h3>{rule.label}</h3>
        <span className="ne-ontology__badge">{rule.severity}</span>
      </header>
      <p>{rule.purpose}</p>
      <dl>
        <dt>Condition</dt>
        <dd>{rule.condition}</dd>
        <dt>Protects</dt>
        <dd>
          {rule.concept ?? "Shared model"}
          {rule.field_name ? ` · ${rule.field_name}` : ""}
        </dd>
        <dt>Scope</dt>
        <dd>{rule.scope}</dd>
        <dt>Governed source</dt>
        <dd>{rule.source}</dd>
      </dl>
      <details className="ne-ontology__technical">
        <summary>Technical details</summary>
        <dl>
          <dt>Rule id</dt>
          <dd>
            <code>{rule.rule_id}</code>
          </dd>
          <dt>Shape IRI</dt>
          <dd>
            <code>{rule.shape_iri}</code>
          </dd>
          <dt>Path IRI</dt>
          <dd>
            <code>{rule.path_iri ?? "Not applicable"}</code>
          </dd>
          <dt>Artifact fingerprint</dt>
          <dd>
            <HashChip hash={rule.artifact_sha256} label="Copy rule artifact fingerprint" />
          </dd>
        </dl>
      </details>
    </article>
  );
}

function IssueDetail({
  state,
  retry,
}: {
  state: BoundRead<OntologyIssueDetail>;
  retry: () => void;
}) {
  if (state.phase === "loading") return <Loading things="validation issue" />;
  if (state.phase === "refused")
    return <Refused thing="sensitive validation issue details" page="Ontology" />;
  if (state.phase === "unconfigured")
    return <Empty message="Issue tracking isn't configured on this deployment." />;
  if (state.phase === "unavailable")
    return <Empty message="Issue tracking is unavailable on this deployment." />;
  if (state.phase === "not_found")
    return <Empty message="This validation issue is no longer available." />;
  if (state.phase === "error") return <LoadError things="the validation issue" onRetry={retry} />;
  const detail = state.value!;
  return (
    <article className="ne-ontology__issue-detail" aria-label={`${detail.issue_id} issue details`}>
      <header>
        <div>
          <h3>Validation issue</h3>
          <code>{detail.issue_id}</code>
        </div>
        <span className="ne-ontology__badge">{detail.lifecycle_state}</span>
      </header>
      <dl>
        <dt>Time</dt>
        <dd>
          <time dateTime={detail.created_at}>{absoluteTime(detail.created_at)}</time>
        </dd>
        <dt>Provenance</dt>
        <dd>{detail.provenance}</dd>
        <dt>Graph reference</dt>
        <dd>
          <code>{detail.graph_iri ?? "Unavailable"}</code>
        </dd>
        <dt>Validation fingerprint</dt>
        <dd>
          <HashChip hash={detail.shapes_sha256} label="Copy issue validation fingerprint" />
        </dd>
      </dl>
      <h4>Safe reasons</h4>
      {detail.reasons.length === 0 ? (
        <Empty message="No safe reasons were reported for this issue." />
      ) : (
        <ul>
          {detail.reasons.map((reason, index) => (
            <li
              key={`${reason.affected_reference ?? "-"}:${reason.rule_reference ?? "-"}:${index}`}
            >
              <strong>{reason.reason}</strong>
              {reason.affected_reference ? (
                <span>
                  Affected: <code>{reason.affected_reference}</code>
                </span>
              ) : null}
              {reason.rule_reference ? (
                <span>
                  Rule: <code>{reason.rule_reference}</code>
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

function Issues({
  list,
  retryList,
  detail,
  retryDetail,
  selectedId,
  onSelect,
}: {
  list: BoundRead<OntologyIssueList>;
  retryList: () => void;
  detail: BoundRead<OntologyIssueDetail> | null;
  retryDetail: () => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  if (list.phase === "loading") return <Loading things="validation issues" />;
  if (list.phase === "refused") return <Refused thing="validation issues" page="Ontology" />;
  if (list.phase === "unconfigured")
    return <Empty message="Issue tracking isn't configured on this deployment." />;
  if (list.phase === "unavailable")
    return <Empty message="Issue tracking is unavailable on this deployment." />;
  if (list.phase === "error" || list.phase === "not_found")
    return <LoadError things="validation issues" onRetry={retryList} />;
  const value = list.value!;
  if (!value.configured)
    return <Empty message="Issue tracking isn't configured on this deployment." />;
  if (value.entries.length === 0) return <Empty message="No validation issues are open." />;
  return (
    <div className="ne-ontology__issues-layout">
      <div>
        <p className="ne-ontology__count">{formatCount(value.count)} open issues</p>
        <ul className="ne-ontology__issue-list">
          {value.entries.map((issue) => (
            <li key={issue.quarantine_id}>
              <button
                type="button"
                aria-current={selectedId === issue.quarantine_id ? "true" : undefined}
                onClick={() => onSelect(issue.quarantine_id)}
              >
                <strong>{absoluteTime(issue.created_at)}</strong>
                <span>
                  {formatCount(issue.violation_count)} violated rules ·{" "}
                  {issue.quarantined_whole ? "whole update rejected" : "rejected"}
                </span>
                <small>{issue.provenance}</small>
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div>
        {detail ? (
          <IssueDetail state={detail} retry={retryDetail} />
        ) : (
          <Empty message="Select an issue to inspect its safe details." />
        )}
      </div>
    </div>
  );
}

function Standards({ model }: { model: ActiveOntologyModel }) {
  return (
    <div className="ne-ontology__standards">
      <p>
        Human-readable mappings lead; immutable namespaces remain available for technical
        verification.
      </p>
      <dl>
        {model.standards.map((standard) => (
          <div key={standard.namespace_iri}>
            <dt>{standard.label}</dt>
            <dd>
              <span>{standard.role}</span>
              {standard.version ? (
                <span>Version {standard.version}</span>
              ) : (
                <span>Version not reported</span>
              )}
              <code>{standard.namespace_iri}</code>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function ModelUnavailable({ response }: { response: OntologyInspectionResponse }) {
  const state = response.artifact_health.state;
  return (
    <div role="alert" className="ne-ontology__artifact-failure">
      <strong>
        {state === "invalid"
          ? "Active model artifacts are invalid."
          : "Active model artifacts are unavailable."}
      </strong>
      <span>{response.artifact_health.detail ?? "No technical detail was reported."}</span>
    </div>
  );
}

function OntologyWorkspace({ client }: { client: OntologyInspectionClient }) {
  const [params, setParams] = useSearchParams();
  const view = viewFrom(params.get("view"));
  const query = params.get("q") ?? "";
  const concept = params.get("concept");
  const rule = params.get("rule");
  const issue = params.get("issue");
  const { state: modelRead, retry: retryModel } = useModelRead(client);
  const response = modelRead.phase === "ok" ? modelRead.value! : null;
  const sections = sectionsFor(response);
  const section = sectionFrom(params.get("section"), sections);
  const { state: issueList, retry: retryIssues } = useIssueListRead(
    client,
    response?.issue_tracking ?? null,
  );
  const detailEnabled = response?.issue_tracking.state === "configured";
  const { state: issueDetail, retry: retryIssue } = useIssueDetailRead(
    client,
    issue,
    detailEnabled,
  );

  const setParam = useCallback(
    (key: string, value: string | null, replace = false) => {
      const next = new URLSearchParams(params);
      if (value === null || value === "") next.delete(key);
      else next.set(key, value);
      setParams(next, { replace });
    },
    [params, setParams],
  );

  const selectSection = useCallback(
    (next: OntologySection) => setParam("section", next === "overview" ? null : next),
    [setParam],
  );
  const selectConcept = useCallback(
    (name: string) => {
      const next = new URLSearchParams(params);
      next.set("section", "model");
      next.set("concept", name);
      setParams(next);
    },
    [params, setParams],
  );

  const tabs = <Tabs sections={sections} selected={section} onSelect={selectSection} />;
  if (modelRead.phase === "loading")
    return (
      <ListScreen title="Ontology" tabs={tabs}>
        <Loading things="the active ontology model" />
      </ListScreen>
    );
  if (modelRead.phase === "refused")
    return (
      <ListScreen title="Ontology" tabs={tabs}>
        <Refused thing="the ontology model" page="Ontology" />
      </ListScreen>
    );
  if (modelRead.phase !== "ok")
    return (
      <ListScreen title="Ontology" tabs={tabs}>
        <LoadError things="the active ontology model" onRetry={retryModel} />
      </ListScreen>
    );

  const model = response!.model;
  let content: ReactNode;
  if (section === "overview") content = <Overview response={response!} />;
  else if (model === null && section !== "issues")
    content = <ModelUnavailable response={response!} />;
  else if (section === "model")
    content = (
      <>
        <T3StoreCallout surface="ontology" />
        <ModelExplorer
          model={model!}
          view={view}
          query={query}
          selectedName={concept}
          onView={(next) => setParam("view", next === "tree" ? null : next)}
          onQuery={(next) => setParam("q", next, true)}
          onSelect={selectConcept}
        />
      </>
    );
  else if (section === "rules")
    content = (
      <Rules
        model={model!}
        query={query}
        selectedId={rule}
        onQuery={(next) => setParam("q", next, true)}
        onSelect={(id) => setParam("rule", id)}
      />
    );
  else if (section === "issues")
    content = (
      <Issues
        list={issueList}
        retryList={retryIssues}
        detail={issueDetail}
        retryDetail={retryIssue}
        selectedId={issue}
        onSelect={(id) => setParam("issue", id)}
      />
    );
  else content = <Standards model={model!} />;

  return (
    <div className="ne-ontology">
      <ListScreen title="Ontology" tabs={tabs}>
        <Panel section={section}>{content}</Panel>
      </ListScreen>
    </div>
  );
}

export function OntologyPage() {
  const auth = useAuth();
  const client = useOntologyInspectionClient();
  const subject =
    auth.isAuthenticated && !auth.isLoading && typeof auth.user?.profile.sub === "string"
      ? auth.user.profile.sub
      : null;

  if (subject === null || subject.length === 0) {
    return (
      <ListScreen title="Ontology">
        <Loading things="the signed-in identity" />
      </ListScreen>
    );
  }

  // A subject change is an ownership change even when the composed client is
  // stable. Remounting synchronously withdraws all prior model and issue state;
  // each read hook separately binds results to the exact client instance.
  return <OntologyWorkspace key={subject} client={client} />;
}
