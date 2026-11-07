# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic read projection of the active operational knowledge model.

The projection is built from the installed ``ne-schema`` generated artifacts and
the exact compiled SHACL graph used by Tier 3.  It deliberately does not read the
optional ontology instance graphs: those graphs may be absent while the generated
operational model remains active.  Human labels come from generated identifiers,
descriptions, or an explicit label for a curated rule; raw IRI fragments are never
promoted into display copy.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any, Literal, cast

from pydantic import BaseModel
from rdflib import RDF, RDFS, Graph, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import SH
from rdflib.term import Node

from ne.schema.generated import operational
from ne.schema.generated._manifest import LINKML_VERSION, SCHEMA_VERSION, SOURCE_SHA256
from ne.schema.generated.operational_graph import EDGE_DEFS

from .shapes import CompiledShapes, load_compiled_shapes

RuleSeverity = Literal["violation", "warning", "info"]
RuleScope = Literal["per-write", "global"]
RuleSource = Literal["generated", "curated"]

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_OPERATIONAL_JSON = "operational.schema.json"

# Curated shapes are authored alongside this package rather than generated from
# LinkML, so their presentation metadata is explicit and total.  Missing metadata
# is an integrity error, never an invitation to derive copy from an IRI fragment.
_CURATED_CONCEPT_LABELS = {
    "https://neutron.enterprise/schema/alignment/IdentityAssertion": "Identity assertion",
}
_CURATED_SHAPE_LABELS = {
    "https://neutron.enterprise/schema/alignment/IdentityAssertionShape": (
        "Identity assertion structure",
        "Identity assertions carry both related records and their source reference.",
    ),
    "urn:ne:shapes:global#UniqueLabelShape": (
        "Labels are unique",
        "Prevents two distinct resources from sharing one corpus label.",
    ),
}
_CURATED_FIELD_LABELS = {
    "https://neutron.enterprise/schema/alignment/same_as_left": "Left record",
    "https://neutron.enterprise/schema/alignment/same_as_right": "Right record",
    "https://neutron.enterprise/schema/alignment/source_ref": "Source reference",
}


class ProjectionIntegrityError(ValueError):
    """Generated or compiled artifacts disagree and cannot be presented truthfully."""


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    schema_id: str
    title: str
    schema_version: str
    linkml_version: str
    source_sha256: str
    json_schema_sha256: str
    shapes_sha256: str
    source_artifact: str


@dataclass(frozen=True, slots=True)
class ModelField:
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


@dataclass(frozen=True, slots=True)
class ModelConcept:
    name: str
    label: str
    description: str | None
    canonical_iri: str
    abstract: bool
    tree_root: bool
    parents: tuple[str, ...]
    children: tuple[str, ...]
    fields: tuple[ModelField, ...]


@dataclass(frozen=True, slots=True)
class ModelRelationship:
    kind: str
    label: str
    source_concept: str
    target_concept: str
    field_name: str
    required: bool
    multivalued: bool


@dataclass(frozen=True, slots=True)
class ValidationRule:
    rule_id: str
    label: str
    purpose: str
    condition: str
    severity: RuleSeverity
    scope: RuleScope
    source: RuleSource
    concept: str | None
    field_name: str | None
    shape_iri: str
    path_iri: str | None
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class StandardMapping:
    label: str
    role: str
    namespace_iri: str
    source_key: str | None = None


@dataclass(frozen=True, slots=True)
class ActiveModelProjection:
    provenance: ModelProvenance
    concepts: tuple[ModelConcept, ...]
    relationships: tuple[ModelRelationship, ...]
    rules: tuple[ValidationRule, ...]
    standards: tuple[StandardMapping, ...]

    @property
    def concept_count(self) -> int:
        return len(self.concepts)

    @property
    def rule_count(self) -> int:
        return len(self.rules)


def _humanize(identifier: str) -> str:
    words = _CAMEL_BOUNDARY.sub(" ", identifier).replace("_", " ").strip()
    if not words:
        raise ProjectionIntegrityError("empty generated identifier")
    return words[0].upper() + words[1:]


def _generated_inputs() -> tuple[bytes, dict[str, object], CompiledShapes]:
    schema_bytes = files("ne.schema.generated").joinpath(_OPERATIONAL_JSON).read_bytes()
    parsed = json.loads(schema_bytes)
    if not isinstance(parsed, dict):
        raise ProjectionIntegrityError("generated JSON Schema is not an object")
    return schema_bytes, parsed, load_compiled_shapes()


def _concept_types() -> dict[str, type[BaseModel]]:
    found: dict[str, type[BaseModel]] = {}
    for name, value in vars(operational).items():
        if not isinstance(value, type) or value is operational.ConfiguredBaseModel:
            continue
        if issubclass(value, operational.ConfiguredBaseModel):
            found[name] = value
    return found


def _mapping(value: object, *, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProjectionIntegrityError(f"{where} is not an object")
    return value


def _string(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectionIntegrityError(f"{where} is not a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _schema_ref(node: dict[str, object]) -> str | None:
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        return ref.removeprefix("#/$defs/")
    items = node.get("items")
    if isinstance(items, dict):
        return _schema_ref(items)
    return None


def _is_array(node: dict[str, object]) -> bool:
    typ = node.get("type")
    return typ == "array" or (isinstance(typ, list) and "array" in typ)


def _value_type(node: dict[str, object], definitions: dict[str, object]) -> str:
    ref = _schema_ref(node)
    if ref is not None:
        target = _mapping(definitions.get(ref), where=f"definition {ref}")
        if isinstance(target.get("enum"), list):
            return f"enum:{ref}"
        return f"concept:{ref}"
    typ = node.get("type")
    if isinstance(typ, list):
        values = [entry for entry in typ if entry != "null"]
        typ = values[0] if len(values) == 1 else "value"
    if typ == "array":
        items = _mapping(node.get("items"), where="array items")
        return f"list[{_value_type(items, definitions)}]"
    if not isinstance(typ, str):
        return "value"
    fmt = node.get("format")
    return f"{typ}:{fmt}" if isinstance(fmt, str) else typ


def _enum_values(node: dict[str, object], definitions: dict[str, object]) -> tuple[str, ...]:
    ref = _schema_ref(node)
    if ref is None:
        return ()
    target = _mapping(definitions.get(ref), where=f"definition {ref}")
    values = target.get("enum")
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ProjectionIntegrityError(f"enum {ref} contains a non-string value")
    return tuple(values)


def _build_concepts(
    schema: dict[str, object], schema_id: str
) -> tuple[tuple[ModelConcept, ...], tuple[ModelRelationship, ...]]:
    definitions = _mapping(schema.get("$defs"), where="$defs")
    concept_types = _concept_types()
    object_defs = {
        name
        for name, value in definitions.items()
        if isinstance(value, dict) and value.get("type") == "object"
    }
    if object_defs != set(concept_types):
        raise ProjectionIntegrityError("generated Pydantic concepts and JSON Schema objects differ")

    parents: dict[str, tuple[str, ...]] = {}
    children: dict[str, list[str]] = {name: [] for name in concept_types}
    for name, model_type in concept_types.items():
        direct = tuple(
            sorted(base.__name__ for base in model_type.__bases__ if base.__name__ in concept_types)
        )
        parents[name] = direct
        for parent in direct:
            children[parent].append(name)

    edge_targets: dict[tuple[str, str], list[str]] = {}
    relationships: list[ModelRelationship] = []
    for edge in EDGE_DEFS:
        edge_targets.setdefault((edge.subject_class, edge.slot_name), []).append(edge.object_class)
        relationships.append(
            ModelRelationship(
                kind=edge.edge_kind,
                label=_humanize(edge.edge_kind.lower()),
                source_concept=edge.subject_class,
                target_concept=edge.object_class,
                field_name=edge.slot_name,
                required=edge.required,
                multivalued=edge.multivalued,
            )
        )

    concepts: list[ModelConcept] = []
    for name in sorted(concept_types):
        definition = _mapping(definitions[name], where=f"definition {name}")
        properties = _mapping(definition.get("properties"), where=f"{name}.properties")
        required_raw = definition.get("required", [])
        if not isinstance(required_raw, list) or not all(
            isinstance(item, str) for item in required_raw
        ):
            raise ProjectionIntegrityError(f"{name}.required is invalid")
        required = set(required_raw)
        model_type = concept_types[name]
        pydantic_required = {
            field for field, info in model_type.model_fields.items() if info.is_required()
        }
        if required != pydantic_required:
            raise ProjectionIntegrityError(f"{name} JSON/Pydantic requiredness differs")

        meta = cast(Any, model_type).linkml_meta.root
        if not isinstance(meta, dict):
            raise ProjectionIntegrityError(f"{name} LinkML metadata is invalid")
        fields_out: list[ModelField] = []
        for field_name in sorted(properties):
            field_schema = _mapping(properties[field_name], where=f"{name}.{field_name}")
            related = tuple(sorted(edge_targets.get((name, field_name), [])))
            ref = _schema_ref(field_schema)
            multivalued = _is_array(field_schema)
            if name == "OperationalRecordSet" and ref in concept_types:
                related = (ref,)
                relationships.append(
                    ModelRelationship(
                        kind="CONTAINS",
                        label="Contains",
                        source_concept=name,
                        target_concept=ref,
                        field_name=field_name,
                        required=field_name in required,
                        multivalued=multivalued,
                    )
                )
            fields_out.append(
                ModelField(
                    name=field_name,
                    label=_humanize(field_name),
                    description=_optional_string(field_schema.get("description")),
                    canonical_iri=f"{schema_id}/{field_name}",
                    value_type=_value_type(field_schema, definitions),
                    required=field_name in required,
                    multivalued=multivalued,
                    min_count=1 if field_name in required else 0,
                    max_count=None if multivalued else 1,
                    enum_values=_enum_values(field_schema, definitions),
                    related_concepts=related,
                )
            )
        concepts.append(
            ModelConcept(
                name=name,
                label=_string(definition.get("title"), where=f"{name}.title"),
                description=_optional_string(definition.get("description")),
                canonical_iri=f"{schema_id}/{name}",
                abstract=bool(meta.get("abstract", False)),
                tree_root=bool(meta.get("tree_root", False)),
                parents=parents[name],
                children=tuple(sorted(children[name])),
                fields=tuple(fields_out),
            )
        )

    by_name = {concept.name: concept for concept in concepts}
    for relationship in relationships:
        source = by_name.get(relationship.source_concept)
        if source is None or relationship.target_concept not in by_name:
            raise ProjectionIntegrityError("generated relationship references an unknown concept")
        field = next((item for item in source.fields if item.name == relationship.field_name), None)
        if field is None or (field.required, field.multivalued) != (
            relationship.required,
            relationship.multivalued,
        ):
            raise ProjectionIntegrityError(
                "generated relationship cardinality differs from its field"
            )
    relationships.sort(
        key=lambda item: (item.source_concept, item.field_name, item.target_concept, item.kind)
    )
    return tuple(concepts), tuple(relationships)


def _literal_text(node: Node | None) -> str | None:
    return str(node) if isinstance(node, RdfLiteral) and str(node).strip() else None


def _severity(graph: Graph, node: Node, shape: URIRef) -> RuleSeverity:
    value = graph.value(node, SH.severity) or graph.value(shape, SH.severity)
    if value is None or value == SH.Violation:
        return "violation"
    if value == SH.Warning:
        return "warning"
    if value == SH.Info:
        return "info"
    raise ProjectionIntegrityError("compiled rule has an unknown SHACL severity")


def _constraint_facts(graph: Graph, node: Node) -> tuple[tuple[str, object], ...]:
    facts: list[tuple[str, object]] = []
    predicates = (
        (SH.minCount, "min_count"),
        (SH.maxCount, "max_count"),
        (SH.datatype, "datatype"),
        (SH["class"], "class"),
        (SH.nodeKind, "node_kind"),
        (SH.pattern, "pattern"),
        (SH.minInclusive, "min_inclusive"),
        (SH.maxInclusive, "max_inclusive"),
    )
    for predicate, key in predicates:
        value = graph.value(node, predicate)
        if value is not None:
            facts.append((key, str(value)))
    allowed = graph.value(node, SH["in"])
    if allowed is not None:
        facts.append(("in", tuple(str(item) for item in graph.items(allowed))))
    recognized = {
        SH.minCount,
        SH.maxCount,
        SH.datatype,
        SH["class"],
        SH.nodeKind,
        SH.pattern,
        SH.minInclusive,
        SH.maxInclusive,
        SH["in"],
        SH.path,
        SH.description,
        SH.name,
        SH.order,
        SH.severity,
    }
    for predicate in graph.predicates(node):
        if str(predicate).startswith(str(SH)) and predicate not in recognized:
            raise ProjectionIntegrityError("compiled property rule has an unsupported constraint")
    return tuple(facts)


def _condition(facts: tuple[tuple[str, object], ...]) -> str:
    rendered: list[str] = []
    datatype_names = {
        "http://www.w3.org/2001/XMLSchema#string": "text",
        "http://www.w3.org/2001/XMLSchema#integer": "an integer",
        "http://www.w3.org/2001/XMLSchema#decimal": "a decimal number",
        "http://www.w3.org/2001/XMLSchema#dateTime": "a date and time",
        "http://www.w3.org/2001/XMLSchema#boolean": "true or false",
    }
    node_kinds = {str(SH.IRI): "a record reference", str(SH.Literal): "a literal value"}
    for key, value in facts:
        if key == "min_count":
            rendered.append("Required" if value == "1" else f"At least {value} values")
        elif key == "max_count":
            rendered.append("At most one value" if value == "1" else f"At most {value} values")
        elif key == "datatype":
            label = datatype_names.get(str(value))
            rendered.append(f"Must be {label}" if label else "Must use the configured datatype")
        elif key == "class":
            rendered.append("Must reference the configured related concept")
        elif key == "node_kind":
            rendered.append(f"Must be {node_kinds.get(str(value), 'the configured value kind')}")
        elif key == "pattern":
            rendered.append("Must match the configured format")
        elif key == "in":
            assert isinstance(value, tuple)
            rendered.append("Allowed values: " + ", ".join(value))
        elif key == "min_inclusive":
            rendered.append(f"Must be at least {value}")
        elif key == "max_inclusive":
            rendered.append(f"Must be at most {value}")
    if not rendered:
        raise ProjectionIntegrityError("compiled property rule has no supported condition")
    return "; ".join(rendered)


def _rule_id(*parts: object) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _build_rules(
    compiled: CompiledShapes, concepts: tuple[ModelConcept, ...]
) -> tuple[ValidationRule, ...]:
    concepts_by_iri = {concept.canonical_iri: concept for concept in concepts}
    rules: list[ValidationRule] = []
    for graph, scope in (
        (compiled.per_write_graph(), "per-write"),
        (compiled.global_graph(), "global"),
    ):
        assert scope in ("per-write", "global")
        shapes = sorted(
            (node for node in graph.subjects(RDF.type, SH.NodeShape) if isinstance(node, URIRef)),
            key=str,
        )
        for shape in shapes:
            shape_iri = str(shape)
            target = graph.value(shape, SH.targetClass)
            concept = concepts_by_iri.get(str(target)) if isinstance(target, URIRef) else None
            generated = concept is not None and shape_iri == concept.canonical_iri
            curated_label = _CURATED_SHAPE_LABELS.get(shape_iri)
            concept_label = (
                concept.label
                if concept is not None
                else _CURATED_CONCEPT_LABELS.get(str(target))
                if target is not None
                else None
            )
            if not generated and curated_label is None:
                raise ProjectionIntegrityError("curated shape lacks authored presentation metadata")

            closed = graph.value(shape, SH.closed)
            if closed is not None and not isinstance(closed, RdfLiteral):
                raise ProjectionIntegrityError("sh:closed is not a literal")
            if isinstance(closed, RdfLiteral) and bool(closed.toPython()):
                label = (
                    f"{concept_label} uses defined fields"
                    if concept_label is not None
                    else curated_label[0]
                    if curated_label is not None
                    else ""
                )
                purpose = _literal_text(graph.value(shape, RDFS.comment)) or (
                    curated_label[1] if curated_label is not None else None
                )
                if not label or purpose is None:
                    raise ProjectionIntegrityError("closed shape lacks safe presentation metadata")
                facts = (("closed", True),)
                rules.append(
                    ValidationRule(
                        rule_id=_rule_id(scope, shape_iri, facts),
                        label=label,
                        purpose=purpose,
                        condition="Only fields defined by the model are accepted",
                        severity=_severity(graph, shape, shape),
                        scope=scope,
                        source="generated" if generated else "curated",
                        concept=concept.name if concept is not None else None,
                        field_name=None,
                        shape_iri=shape_iri,
                        path_iri=None,
                        artifact_sha256=compiled.shapes_hash,
                    )
                )

            properties = sorted(
                graph.objects(shape, SH.property),
                key=lambda node: str(graph.value(node, SH.path)),
            )
            for prop in properties:
                path = graph.value(prop, SH.path)
                if not isinstance(path, URIRef):
                    raise ProjectionIntegrityError("property shape has no stable IRI path")
                path_iri = str(path)
                field = (
                    next((item for item in concept.fields if item.canonical_iri == path_iri), None)
                    if concept is not None
                    else None
                )
                authored_name = _literal_text(graph.value(prop, SH.name))
                field_label = (
                    field.label if field is not None else _CURATED_FIELD_LABELS.get(path_iri)
                )
                if authored_name is None and field_label is None:
                    raise ProjectionIntegrityError(
                        "property rule lacks authored/generated label metadata"
                    )
                label = authored_name or f"{concept_label} — {field_label}"
                purpose = (
                    _literal_text(graph.value(prop, SH.description))
                    or (field.description if field is not None else None)
                    or (curated_label[1] if curated_label is not None else None)
                )
                if purpose is None:
                    raise ProjectionIntegrityError("property rule lacks a safe purpose")
                facts = _constraint_facts(graph, prop)
                rules.append(
                    ValidationRule(
                        rule_id=_rule_id(scope, shape_iri, path_iri, facts),
                        label=label,
                        purpose=purpose,
                        condition=_condition(facts),
                        severity=_severity(graph, prop, shape),
                        scope=scope,
                        source="generated" if generated else "curated",
                        concept=concept.name if concept is not None else None,
                        field_name=field.name if field is not None else None,
                        shape_iri=shape_iri,
                        path_iri=path_iri,
                        artifact_sha256=compiled.shapes_hash,
                    )
                )

            for constraint in sorted(graph.objects(shape, SH.sparql), key=str):
                message = _literal_text(graph.value(constraint, SH.message))
                if message is None or curated_label is None:
                    raise ProjectionIntegrityError("SPARQL rule lacks authored safe copy")
                rules.append(
                    ValidationRule(
                        rule_id=_rule_id(scope, shape_iri, message),
                        label=curated_label[0],
                        purpose=curated_label[1],
                        condition=message,
                        severity=_severity(graph, constraint, shape),
                        scope=scope,
                        source="curated",
                        concept=None,
                        field_name=None,
                        shape_iri=shape_iri,
                        path_iri=None,
                        artifact_sha256=compiled.shapes_hash,
                    )
                )

    rules.sort(
        key=lambda rule: (rule.concept or "", rule.field_name or "", rule.scope, rule.rule_id)
    )
    return tuple(rules)


def build_active_model_projection() -> ActiveModelProjection:
    """Build and cross-check the installed generated-model inspection projection."""
    schema_bytes, schema, compiled = _generated_inputs()
    metadata = operational.linkml_meta.root
    if not isinstance(metadata, dict):
        raise ProjectionIntegrityError("generated LinkML metadata is invalid")
    schema_id = _string(metadata.get("id"), where="schema id")
    title = _string(metadata.get("title"), where="schema title")
    if operational.version != SCHEMA_VERSION:
        raise ProjectionIntegrityError("generated module and manifest versions differ")
    concepts, relationships = _build_concepts(schema, schema_id)
    rules = _build_rules(compiled, concepts)
    return ActiveModelProjection(
        provenance=ModelProvenance(
            schema_id=schema_id,
            title=title,
            schema_version=SCHEMA_VERSION,
            linkml_version=LINKML_VERSION,
            source_sha256=SOURCE_SHA256,
            json_schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
            shapes_sha256=compiled.shapes_hash,
            source_artifact="ne.schema.generated.operational",
        ),
        concepts=concepts,
        relationships=relationships,
        rules=rules,
        standards=(
            StandardMapping(
                label="Neutron operational model",
                role="Active enterprise concepts and relationships",
                namespace_iri=f"{schema_id}/",
                source_key="ne",
            ),
            StandardMapping(
                label="LinkML",
                role="Schema source and artifact generation standard",
                namespace_iri="https://w3id.org/linkml/",
            ),
            StandardMapping(
                label="Basic Formal Ontology (BFO)",
                role="Upper-ontology alignment",
                namespace_iri="http://purl.obolibrary.org/obo/BFO_",
                source_key="bfo",
            ),
            StandardMapping(
                label="Common Core Ontologies (CCO)",
                role="Enterprise-domain alignment",
                namespace_iri="https://www.commoncoreontologies.org/",
                source_key="cco",
            ),
        ),
    )


@lru_cache(maxsize=1)
def load_active_model_projection() -> ActiveModelProjection:
    """Load the immutable package projection once per process."""
    return build_active_model_projection()


__all__ = [
    "ActiveModelProjection",
    "ModelConcept",
    "ModelField",
    "ModelProvenance",
    "ModelRelationship",
    "ProjectionIntegrityError",
    "StandardMapping",
    "ValidationRule",
    "build_active_model_projection",
    "load_active_model_projection",
]
