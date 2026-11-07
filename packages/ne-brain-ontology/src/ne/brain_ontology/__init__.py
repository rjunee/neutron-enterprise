# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Brain T3: ontology knowledge tier (v1.2).

Public surface (NE-085a): the SHACL write gateway factories, the gateway errors,
and ``OntologySettings``. NE-085b adds the property-graph->RDF lift
(:class:`PropertyGraphLift`, :class:`LiftReport`, the injected
:class:`GraphScanSource` seam), the ACL named-graph partitioning helpers
(``graphs_for_labels``/``graph_iri_for_label``), and the ``OntologyVersion`` /
``SyncReport`` registry types. The raw triple-store backends remain DELIBERATELY
unexported — every write must go through :class:`ValidatingTripleStore`, so a
backend has no entry point and is never importable as a public name (the
backend-privacy invariant, brain-tier3-spec §5.1).
"""

from __future__ import annotations

from .acl import graph_iri_for_label, graphs_for_labels
from .config import OntologySettings
from .gateway import (
    ShapeValidationError,
    TripleQuarantinedError,
    ValidatingTripleStore,
    build_fuseki_store,
    build_oxigraph_store,
    build_release_staging_store,
)
from .lift import GraphScanSource, LiftReport, PropertyGraphLift
from .ontology import OntologyVersion
from .store.sync import SyncReport
from .verified import (
    GOVERNED_LOCAL_T3_SOURCE_KIND,
    MERIDIAN_TEMPLATE_IDS,
    SELECTED_RELEASE_ALGORITHM_VERSION,
    DateWindowSpec,
    GovernedLocalT3Instance,
    GovernedLocalT3UnavailableError,
    MeridianContributor,
    MeridianReleaseBuild,
    MeridianReleaseDescriptor,
    MeridianVerifiedLoadReport,
    ReleaseHeadDriftError,
    RouteDecision,
    RouteOutcome,
    RoutingRow,
    RoutingSpec,
    SelectedOxigraphRelease,
    SelectedReleaseRegistry,
    SelectedReleaseVerifiedReader,
    StoredVerifiedTemplate,
    TemplateRegistry,
    TemplateRegistryError,
    TemplateRegistryFingerprint,
    VerifiedAnswer,
    VerifiedAnswerArgsError,
    VerifiedAnswerScopeError,
    VerifiedAnswerToolDef,
    VerifiedTemplateConflictError,
    VerifiedTemplateGateError,
    VerifiedTemplateRecord,
    append_verified_template,
    build_meridian_release,
    build_routing_table,
    build_verified_answer_tools,
    contributor_artifact_identity,
    contributor_source_identity,
    ensure_verified_template_schema,
    governed_local_t3_admitted_target_digest,
    governed_local_t3_scope,
    load_meridian_template_registry,
    load_meridian_verified_fixture,
    load_template_registry,
    load_template_registry_with_overlay,
    match_selection,
    match_template,
    meridian_source_contract_identity,
    meridian_template_dir,
    parse_template_toml,
    pinned_release,
    publish_meridian_release,
    read_verified_templates,
    require_source_bearing_backend,
)

__version__ = "0.6.0"

__all__ = [
    "GOVERNED_LOCAL_T3_SOURCE_KIND",
    "MERIDIAN_TEMPLATE_IDS",
    "SELECTED_RELEASE_ALGORITHM_VERSION",
    "DateWindowSpec",
    "GovernedLocalT3Instance",
    "GovernedLocalT3UnavailableError",
    "GraphScanSource",
    "LiftReport",
    "MeridianContributor",
    "MeridianReleaseBuild",
    "MeridianReleaseDescriptor",
    "MeridianVerifiedLoadReport",
    "OntologySettings",
    "OntologyVersion",
    "PropertyGraphLift",
    "ReleaseHeadDriftError",
    "RouteDecision",
    "RouteOutcome",
    "RoutingRow",
    "RoutingSpec",
    "SelectedOxigraphRelease",
    "SelectedReleaseRegistry",
    "SelectedReleaseVerifiedReader",
    "ShapeValidationError",
    "StoredVerifiedTemplate",
    "SyncReport",
    "TemplateRegistry",
    "TemplateRegistryError",
    "TemplateRegistryFingerprint",
    "TripleQuarantinedError",
    "ValidatingTripleStore",
    "VerifiedAnswer",
    "VerifiedAnswerArgsError",
    "VerifiedAnswerScopeError",
    "VerifiedAnswerToolDef",
    "VerifiedTemplateConflictError",
    "VerifiedTemplateGateError",
    "VerifiedTemplateRecord",
    "append_verified_template",
    "build_fuseki_store",
    "build_meridian_release",
    "build_oxigraph_store",
    "build_release_staging_store",
    "build_routing_table",
    "build_verified_answer_tools",
    "contributor_artifact_identity",
    "contributor_source_identity",
    "ensure_verified_template_schema",
    "governed_local_t3_admitted_target_digest",
    "governed_local_t3_scope",
    "graph_iri_for_label",
    "graphs_for_labels",
    "load_meridian_template_registry",
    "load_meridian_verified_fixture",
    "load_template_registry",
    "load_template_registry_with_overlay",
    "match_selection",
    "match_template",
    "meridian_source_contract_identity",
    "meridian_template_dir",
    "parse_template_toml",
    "pinned_release",
    "publish_meridian_release",
    "read_verified_templates",
    "require_source_bearing_backend",
]
