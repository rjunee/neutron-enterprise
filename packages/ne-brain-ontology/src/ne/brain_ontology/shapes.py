# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""pySHACL shape validator (NE-085-spike).

``PyShaclValidator`` structurally satisfies the FROZEN
``ne.contracts.knowledge.ShapeValidator`` Protocol. It runs pySHACL over a delta
graph supplied as either a triple list or serialized RDF, with owl:imports
resolution HARDCODED off (C2 — pySHACL's import resolver reaches the network).

Violation strings are ``"<focusNode> <sourceShape> <message>"`` — node IRIs and
shape names only, redaction-safe. Validator exceptions PROPAGATE: a malformed
shapes graph is a denial, never a silent ``conforms=True``.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import pyshacl
from pydantic import BaseModel, ConfigDict
from rdflib import RDF, Graph, Literal, Namespace, URIRef
from rdflib.term import Node

from ne.contracts.knowledge import ShapeReport

from .config import OntologySettings
from .ontology import iter_corpus, load_manifest

SH = Namespace("http://www.w3.org/ns/shacl#")

_IRI_SCHEMES = ("http://", "https://", "urn:")


def _as_graph(source: bytes | Graph) -> Graph:
    if isinstance(source, Graph):
        return source
    graph = Graph()
    # data= (in-memory bytes), never location=/URL (C2).
    graph.parse(data=source, format="turtle")
    return graph


def _object_node(value: str) -> URIRef | Literal:
    if value.startswith(_IRI_SCHEMES):
        return URIRef(value)
    return Literal(value)


def _graph_from_mapping(graph: Mapping[str, Any]) -> Graph:
    """Build the data graph from the spec §4.1 input mapping.

    ``{'triples': [(s, p, o), ...]}`` OR ``{'data': str, 'format': 'nt'|'turtle'}``.
    Neither / both present → ValueError.
    """
    triples = graph.get("triples")
    data = graph.get("data")
    if (triples is None) == (data is None):
        raise ValueError("supply exactly one of 'triples' or 'data'")
    result = Graph()
    if triples is not None:
        for subject, predicate, obj in triples:
            result.add((URIRef(subject), URIRef(predicate), _object_node(obj)))
        return result
    fmt = graph.get("format", "turtle")
    if fmt not in ("nt", "turtle"):
        raise ValueError(f"unsupported serialization format: {fmt!r}")
    result.parse(data=data, format=fmt)
    return result


def _render(node: Node | None) -> str:
    return str(node) if node is not None else "-"


class PyShaclValidator:
    """SHACL validator over a compiled shapes graph + optional ontology graph."""

    def __init__(self, shapes: bytes | Graph, *, ont: bytes | Graph | None = None) -> None:
        self._shapes = _as_graph(shapes)
        self._ont = _as_graph(ont) if ont is not None else None

    async def validate(self, graph: Mapping[str, Any]) -> ShapeReport:
        data_graph = _graph_from_mapping(graph)
        # SINGLE pyshacl call site. do_owl_imports=False is the ONLY allowed
        # value (C2 — owl:imports resolution reaches the network). DO NOT CHANGE.
        conforms, results_graph, _ = pyshacl.validate(
            data_graph,
            shacl_graph=self._shapes,
            ont_graph=self._ont,
            inference="none",
            do_owl_imports=False,
        )
        # pyshacl's stub over-broadens the tuple element types; the runtime
        # contract is (bool, rdflib.Graph, str). Narrow for the frozen report.
        violations = tuple(self._violations(cast(Graph, results_graph)))
        return ShapeReport(conforms=bool(conforms), violations=violations)

    @staticmethod
    def _violations(results_graph: Graph) -> list[str]:
        rendered: list[str] = []
        for result in results_graph.subjects(RDF.type, SH.ValidationResult):
            focus = results_graph.value(result, SH.focusNode)
            shape = results_graph.value(result, SH.sourceShape)
            message = results_graph.value(result, SH.resultMessage)
            rendered.append(f"{_render(focus)} {_render(shape)} {_render(message)}")
        return sorted(rendered)


# ---------------------------------------------------------------------------
# Compiled shapes: generated (ne-schema) + curated (per-write / global) split.
# ---------------------------------------------------------------------------

#: The generated LinkML→SHACL artifact, shipped as ne-schema package data.
_GENERATED_SHACL = "operational.shapes.ttl"
_CURATED_MANIFEST = "MANIFEST.toml"


class CompiledShapes(BaseModel):
    """The compiled T3 shapes graph, split by evaluation scope.

    ``per_write_ttl`` is validated on EVERY write (delta + CBD); ``global_ttl``
    holds non-local (``sh:sparql``) shapes evaluated only in the nightly
    full-graph job. ``shapes_hash`` is a sha256 over the canonical concatenation
    of the component bytes — a shape change is therefore a visible config change
    (it rides ``schema_version`` on every emitted ``ShapeViolation``).
    """

    model_config = ConfigDict(frozen=True)

    per_write_ttl: bytes
    global_ttl: bytes
    shapes_hash: str

    def per_write_graph(self) -> Graph:
        graph = Graph()
        if self.per_write_ttl.strip():
            graph.parse(data=self.per_write_ttl, format="turtle")
        return graph

    def global_graph(self) -> Graph:
        graph = Graph()
        if self.global_ttl.strip():
            graph.parse(data=self.global_ttl, format="turtle")
        return graph

    def combined_graph(self) -> Graph:
        graph = self.per_write_graph()
        if self.global_ttl.strip():
            graph.parse(data=self.global_ttl, format="turtle")
        return graph


def _read_generated_shapes() -> bytes:
    resource = files("ne.schema.generated").joinpath(_GENERATED_SHACL)
    return resource.read_bytes()


def _curated_root() -> Path:
    resource = files("ne.brain_ontology").joinpath("assets", "shapes")
    root = Path(str(resource))
    if not root.is_dir():
        raise RuntimeError(f"curated shapes dir not resolvable as package data: {root}")
    return root


def load_compiled_shapes() -> CompiledShapes:
    """Load + compile the generated + curated shapes, split by scope.

    ``per_write`` = the generated ``operational.shapes.ttl`` plus every curated
    file whose manifest ``scope = "per-write"``; ``global`` = curated files whose
    ``scope = "global"``. The hash is over a canonical, ordered concatenation so
    it is stable across processes.
    """
    generated = _read_generated_shapes()
    root = _curated_root()
    manifest = tomllib.loads((root / _CURATED_MANIFEST).read_text(encoding="utf-8"))

    per_write_curated: list[tuple[str, bytes]] = []
    global_curated: list[tuple[str, bytes]] = []
    for entry in manifest.get("shapes", []):
        filename = entry["file"]
        scope = entry["scope"]
        data = (root / filename).read_bytes()
        if scope == "per-write":
            per_write_curated.append((filename, data))
        elif scope == "global":
            global_curated.append((filename, data))
        else:
            raise ValueError(f"curated shape {filename!r}: unknown scope {scope!r}")

    per_write_parts = [generated, *[d for _, d in sorted(per_write_curated)]]
    global_parts = [d for _, d in sorted(global_curated)]
    per_write_ttl = b"\n".join(per_write_parts)
    global_ttl = b"\n".join(global_parts)

    hasher = hashlib.sha256()
    hasher.update(b"generated:")
    hasher.update(generated)
    for name, data in sorted(per_write_curated):
        hasher.update(f"per-write:{name}:".encode())
        hasher.update(data)
    for name, data in sorted(global_curated):
        hasher.update(f"global:{name}:".encode())
        hasher.update(data)

    return CompiledShapes(
        per_write_ttl=per_write_ttl,
        global_ttl=global_ttl,
        shapes_hash=hasher.hexdigest(),
    )


def load_corpus_graph() -> Graph:
    """Parse the whole vendored BFO/CCO corpus into one rdflib Graph (offline).

    Loaded once and reused as the pySHACL ``ont_graph`` so shape constraints that
    reference ontology axioms resolve. Local bytes only (C2).
    """
    manifest = load_manifest()
    graph = Graph()
    for _iri, _path, data in iter_corpus(manifest):
        graph.parse(data=data, format="turtle")
    return graph


def build_validator(settings: OntologySettings | None = None) -> PyShaclValidator:
    """Entry-point factory: a :class:`PyShaclValidator` over the PER-WRITE shapes.

    The global (``sh:sparql``) shapes are deliberately EXCLUDED from this
    per-write validator (they run in the gateway's ``validate_full_graph``). The
    vendored corpus is attached as the ont-graph when
    ``settings.include_ontology_graph`` (default True).
    """
    settings = settings or OntologySettings()
    compiled = load_compiled_shapes()
    ont = load_corpus_graph() if settings.include_ontology_graph else None
    return PyShaclValidator(compiled.per_write_graph(), ont=ont)
