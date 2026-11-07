// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

export type ArtifactHealthState = "healthy" | "invalid" | "unavailable";
export type OntologyGraphState = "loaded" | "not_loaded" | "unavailable";
export type IssueTrackingState = "configured" | "unconfigured" | "unavailable";
export type ValidationHealth = "healthy" | "issues" | "unavailable";

export interface OntologyModelProvenance {
  schema_id: string;
  title: string;
  schema_version: string;
  linkml_version: string;
  source_sha256: string;
  json_schema_sha256: string;
  shapes_sha256: string;
  source_artifact: string;
}

export interface OntologyField {
  name: string;
  label: string;
  description: string | null;
  canonical_iri: string;
  value_type: string;
  required: boolean;
  multivalued: boolean;
  min_count: number;
  max_count: number | null;
  enum_values: string[];
  related_concepts: string[];
}

export interface OntologyConcept {
  name: string;
  label: string;
  description: string | null;
  canonical_iri: string;
  abstract: boolean;
  tree_root: boolean;
  parents: string[];
  children: string[];
  fields: OntologyField[];
}

export interface OntologyRelationship {
  kind: string;
  label: string;
  source_concept: string;
  target_concept: string;
  field_name: string;
  required: boolean;
  multivalued: boolean;
}

export interface OntologyRule {
  rule_id: string;
  label: string;
  purpose: string;
  condition: string;
  severity: "violation" | "warning" | "info";
  scope: "per-write" | "global";
  source: "generated" | "curated";
  concept: string | null;
  field_name: string | null;
  shape_iri: string;
  path_iri: string | null;
  artifact_sha256: string;
}

export interface OntologyStandard {
  label: string;
  role: string;
  namespace_iri: string;
  version: string | null;
}

export interface ActiveOntologyModel {
  provenance: OntologyModelProvenance;
  concept_count: number;
  rule_count: number;
  concepts: OntologyConcept[];
  relationships: OntologyRelationship[];
  rules: OntologyRule[];
  standards: OntologyStandard[];
}

export interface OntologyInspectionResponse {
  model: ActiveOntologyModel | null;
  artifact_health: { state: ArtifactHealthState; detail: string | null };
  ontology_graph: { state: OntologyGraphState; optional_class_count: number | null };
  issue_tracking: { state: IssueTrackingState; count: number | null };
  validation_health: ValidationHealth;
}

export interface OntologyIssueSummary {
  quarantine_id: string;
  created_at: string;
  graph_iri: string | null;
  shapes_hash: string;
  violation_count: number;
  quarantined_whole: boolean;
  provenance: string;
}

export interface OntologyIssueList {
  configured: boolean;
  count: number;
  entries: OntologyIssueSummary[];
}

export interface OntologyIssueReason {
  affected_reference: string | null;
  rule_reference: string | null;
  reason: string;
}

export interface OntologyIssueDetail {
  issue_id: string;
  created_at: string;
  graph_iri: string | null;
  shapes_sha256: string;
  provenance: string;
  lifecycle_state: "quarantined";
  reasons: OntologyIssueReason[];
}

export type OntologySection = "overview" | "model" | "rules" | "issues" | "standards";
export type OntologyView = "tree" | "list" | "relationships";
