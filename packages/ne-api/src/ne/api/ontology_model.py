# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Wire models for the read-only active Ontology inspector backend."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from ne.brain_ontology.inspection import ActiveModelProjection


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class OntologyModelProvenanceView(_View):
    schema_id: str
    title: str
    schema_version: str
    linkml_version: str
    source_sha256: str
    json_schema_sha256: str
    shapes_sha256: str
    source_artifact: str


class OntologyFieldView(_View):
    name: str
    label: str
    description: str | None
    canonical_iri: str
    value_type: str
    required: bool
    multivalued: bool
    min_count: int
    max_count: int | None
    enum_values: tuple[str, ...]
    related_concepts: tuple[str, ...]


class OntologyConceptView(_View):
    name: str
    label: str
    description: str | None
    canonical_iri: str
    abstract: bool
    tree_root: bool
    parents: tuple[str, ...]
    children: tuple[str, ...]
    fields: tuple[OntologyFieldView, ...]


class OntologyRelationshipView(_View):
    kind: str
    label: str
    source_concept: str
    target_concept: str
    field_name: str
    required: bool
    multivalued: bool


class OntologyRuleView(_View):
    rule_id: str
    label: str
    purpose: str
    condition: str
    severity: Literal["violation", "warning", "info"]
    scope: Literal["per-write", "global"]
    source: Literal["generated", "curated"]
    concept: str | None
    field_name: str | None
    shape_iri: str
    path_iri: str | None
    artifact_sha256: str


class OntologyStandardView(_View):
    label: str
    role: str
    namespace_iri: str
    version: str | None


class ActiveOntologyModelView(_View):
    provenance: OntologyModelProvenanceView
    concept_count: int
    rule_count: int
    concepts: tuple[OntologyConceptView, ...]
    relationships: tuple[OntologyRelationshipView, ...]
    rules: tuple[OntologyRuleView, ...]
    standards: tuple[OntologyStandardView, ...]

    @classmethod
    def from_projection(
        cls,
        projection: ActiveModelProjection,
        *,
        source_versions: Mapping[str, str] | None = None,
    ) -> ActiveOntologyModelView:
        versions = source_versions or {}
        data = asdict(projection)
        standards = []
        for standard in data.pop("standards"):
            source_key = standard.pop("source_key")
            standards.append(
                {
                    **standard,
                    "version": versions.get(source_key) if source_key is not None else None,
                }
            )
        return cls.model_validate(
            {
                **data,
                "concept_count": projection.concept_count,
                "rule_count": projection.rule_count,
                "standards": standards,
            }
        )


class ArtifactHealthView(_View):
    state: Literal["healthy", "invalid", "unavailable"]
    detail: str | None = None


class OntologyGraphStateView(_View):
    state: Literal["loaded", "not_loaded", "unavailable"]
    optional_class_count: int | None


class OntologyIssueTrackingView(_View):
    state: Literal["configured", "unconfigured", "unavailable"]
    count: int | None


class OntologyModelResponse(_View):
    model: ActiveOntologyModelView | None
    artifact_health: ArtifactHealthView
    ontology_graph: OntologyGraphStateView
    issue_tracking: OntologyIssueTrackingView
    validation_health: Literal["healthy", "issues", "unavailable"]


class OntologyIssueReasonView(_View):
    affected_reference: str | None
    rule_reference: str | None
    reason: str


class OntologyIssueDetailView(_View):
    issue_id: str
    created_at: str
    graph_iri: str | None
    shapes_sha256: str
    provenance: str
    lifecycle_state: Literal["quarantined"] = "quarantined"
    reasons: tuple[OntologyIssueReasonView, ...]


__all__ = [
    "ActiveOntologyModelView",
    "ArtifactHealthView",
    "OntologyGraphStateView",
    "OntologyIssueDetailView",
    "OntologyIssueReasonView",
    "OntologyIssueTrackingView",
    "OntologyModelResponse",
]
