# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``ne ontology`` inspection command group (NE-ONTOLOGY-INSPECT).

Every subcommand runs against a FIXTURE in-memory oxigraph gateway (no live stack):
the ``_build_gateway`` seam is monkeypatched to return a REAL, seeded
``ValidatingTripleStore`` so the C12 rejection tests exercise the LIVE steward gate
(non-vacuous). ``verified`` reads the fixture template bundle under tests/data;
``conformance`` runs the REAL offline vendored-corpus gate + a seeded quarantine lane.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from ne.cli import main

# The ``ontology`` group name shadows the ``ne.cli.ontology`` submodule attribute on
# the ne.cli package (the connect.py/audit.py precedent), so grab the real module via
# importlib to monkeypatch its ``_build_gateway`` seam.
ontology_cli = importlib.import_module("ne.cli.ontology")

_DATA_DIR = Path(__file__).resolve().parent / "data" / "verified_templates"

_ALPHA_GRAPH = "urn:ne:graph:acl:alpha"
_BFO_GRAPH = "urn:ne:ontology:bfo:seed"

_ALPHA_TTL = b"""
<urn:x:a> <https://neutron.enterprise/schema/operational/name> "Alpha Corp" .
<urn:x:b> <https://neutron.enterprise/schema/operational/name> "Beta LLC" .
"""
_BFO_TTL = b'<urn:x:cls> <http://www.w3.org/2000/01/rdf-schema#label> "seed class" .\n'


@pytest.fixture(autouse=True)
def _scrub_ne_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ambient NE_* config so a developer's env never leaks into a test."""
    for name in (
        "NE_ONTOLOGY_BACKEND",
        "NE_ONTOLOGY_OXIGRAPH_PATH",
        "NE_ONTOLOGY_FUSEKI_URL",
        "NE_ONTOLOGY_FUSEKI_USER",
        "NE_ONTOLOGY_FUSEKI_PASSWORD",
        "NE_ONTOLOGY_QUARANTINE_DB",
        "NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED",
        "NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def _make_seeded_gateway() -> Any:
    """A REAL in-memory oxigraph gateway seeded with two named graphs.

    Package-internal test-only imports (outside import-linter's root packages), the
    ``verified_helpers.make_gateway`` precedent. The C12 gate inside this REAL
    ``ValidatingTripleStore`` stays live so the sparql rejection tests are non-vacuous.
    """
    from ne.brain_ontology.config import OntologySettings
    from ne.brain_ontology.gateway import ValidatingTripleStore
    from ne.brain_ontology.quarantine import SqliteTripleQuarantine
    from ne.brain_ontology.shapes import build_validator
    from ne.brain_ontology.store.oxigraph import OxigraphBackend

    class _NullSink:
        def emit(self, event: Any) -> None:
            return None

    settings = OntologySettings(include_ontology_graph=False)
    backend = OxigraphBackend()
    asyncio.run(backend.load_turtle(_ALPHA_TTL, graph_iri=_ALPHA_GRAPH))
    asyncio.run(backend.load_turtle(_BFO_TTL, graph_iri=_BFO_GRAPH))
    return ValidatingTripleStore(
        backend,
        build_validator(settings),
        SqliteTripleQuarantine(),
        _NullSink(),
        settings=settings,
        config_hash="a" * 64,
    )


@pytest.fixture
def seeded_gateway(monkeypatch: pytest.MonkeyPatch) -> Any:
    gateway = _make_seeded_gateway()
    monkeypatch.setattr(ontology_cli, "_build_gateway", lambda settings: gateway)
    return gateway


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, list(args))


# --- help ------------------------------------------------------------------


def test_ontology_help_lists_subcommands() -> None:
    result = _run("ontology", "--help")
    assert result.exit_code == 0
    for sub in ("graphs", "verified", "sparql", "conformance", "lint"):
        assert sub in result.output


# --- graphs ----------------------------------------------------------------


def test_graphs_lists_seeded_graphs(seeded_gateway: Any) -> None:
    result = _run("ontology", "graphs")
    assert result.exit_code == 0, result.output
    assert _ALPHA_GRAPH in result.output
    assert _BFO_GRAPH in result.output
    assert "2" in result.output  # alpha graph has 2 triples


def test_graphs_json_shape_stable(seeded_gateway: Any) -> None:
    result = _run("ontology", "graphs", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"graphs", "total_triples", "ontology"}
    assert set(payload["ontology"]) == {"sources", "shapes_hash"}
    assert set(payload["ontology"]["sources"]) >= {"bfo", "cco", "ne"}
    iris = [g["graph_iri"] for g in payload["graphs"]]
    assert iris == sorted(iris)
    assert {_ALPHA_GRAPH, _BFO_GRAPH} <= set(iris)
    for g in payload["graphs"]:
        assert isinstance(g["triples"], int)
    counts = {g["graph_iri"]: g["triples"] for g in payload["graphs"]}
    assert counts[_ALPHA_GRAPH] == 2
    assert counts[_BFO_GRAPH] == 1
    assert payload["total_triples"] == sum(g["triples"] for g in payload["graphs"])


# --- sparql ----------------------------------------------------------------


def test_sparql_select_rows(seeded_gateway: Any) -> None:
    result = _run("ontology", "sparql", "SELECT ?s ?p ?o WHERE { GRAPH ?g { ?s ?p ?o } }")
    assert result.exit_code == 0, result.output
    assert "Alpha Corp" in result.output


def test_sparql_ask(seeded_gateway: Any) -> None:
    result = _run("ontology", "sparql", "ASK { GRAPH ?g { ?s ?p ?o } }")
    assert result.exit_code == 0, result.output
    assert "True" in result.output

    json_result = _run("ontology", "sparql", "ASK { GRAPH ?g { ?s ?p ?o } }", "--json")
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["row_count"] == 1
    assert payload["rows"][0]["ask"] is True


def test_sparql_json_shape_stable(seeded_gateway: Any) -> None:
    result = _run("ontology", "sparql", "SELECT ?s WHERE { GRAPH ?g { ?s ?p ?o } }", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"rows", "row_count"}


def test_sparql_rejects_insert(seeded_gateway: Any) -> None:
    result = _run("ontology", "sparql", "INSERT DATA { <urn:a> <urn:b> <urn:c> }")
    assert result.exit_code != 0
    assert "read-only" in result.output or "refuses" in result.output


def test_sparql_rejects_load_and_service(seeded_gateway: Any) -> None:
    load = _run("ontology", "sparql", "LOAD <http://evil/>")
    assert load.exit_code != 0
    assert "refuses" in load.output or "read-only" in load.output

    service = _run(
        "ontology",
        "sparql",
        "SELECT ?s WHERE { SERVICE <http://evil/sparql> { ?s ?p ?o } }",
    )
    assert service.exit_code != 0
    assert "refuses" in service.output or "read-only" in service.output


def test_sparql_rejects_freeform_describe(seeded_gateway: Any) -> None:
    result = _run("ontology", "sparql", "DESCRIBE <urn:x:a>")
    assert result.exit_code != 0
    assert "SELECT/ASK" in result.output or "select/ask" in result.output.lower()


# --- verified --------------------------------------------------------------


def test_verified_disabled_default() -> None:
    result = _run("ontology", "verified")
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output

    json_result = _run("ontology", "verified", "--json")
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["enabled"] is False
    assert payload["templates"] == []


def test_verified_lists_fixture_bundle() -> None:
    result = _run("ontology", "verified", "--templates-dir", str(_DATA_DIR))
    assert result.exit_code == 0, result.output
    assert "accounts-by-name@v2" in result.output
    assert "accounts-directory@v1" in result.output
    assert "read.kg.accounts_by_name" in result.output


def test_verified_json_shape_stable() -> None:
    result = _run("ontology", "verified", "--templates-dir", str(_DATA_DIR), "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"enabled", "templates_dir", "registry_hash", "templates"}
    assert len(payload["registry_hash"]) == 64
    int(payload["registry_hash"], 16)  # 64-hex
    for template in payload["templates"]:
        assert set(template) == {
            "id",
            "version",
            "tool_name",
            "description",
            "args",
            "columns",
            "citation_var",
            "label_var",
            "graph_classes",
            "max_rows",
        }
    by_id = {t["id"]: t for t in payload["templates"]}
    assert "q_name" in by_id["accounts-by-name"]["args"]["properties"]


def test_verified_human_and_json_publish_the_same_enum(tmp_path: Path) -> None:
    from ne.cli.demo.corpus import write_verified_templates

    directory = write_verified_templates(tmp_path / "verified")
    human = _run("ontology", "verified", "--templates-dir", str(directory))
    assert human.exit_code == 0, human.output
    assert "arg status: string — permitted: open, pending, resolved, closed" in human.output

    machine = _run("ontology", "verified", "--templates-dir", str(directory), "--json")
    assert machine.exit_code == 0, machine.output
    templates = {row["id"]: row for row in json.loads(machine.output)["templates"]}
    assert templates["tickets-by-status"]["args"]["properties"]["status"]["enum"] == [
        "open",
        "pending",
        "resolved",
        "closed",
    ]


def test_verified_explicit_empty_bundle_fails_loud(tmp_path: Path) -> None:
    result = _run("ontology", "verified", "--templates-dir", str(tmp_path))
    assert result.exit_code != 0
    assert "bundle is empty" in result.output


def test_verified_enabled_missing_dir_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED", "true")
    result = _run("ontology", "verified")
    assert result.exit_code != 0
    assert "NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR" in result.output


def test_verified_malformed_bundle_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        'template_id = "bad"\n'
        "version = 1\n"
        'description = "no bindings marker + a write verb"\n'
        'sparql = "DELETE WHERE { ?s ?p ?o }"\n'
        "[scope]\n"
        'graph_classes = ["alpha"]\n'
        "[answer]\n"
        'columns = ["s"]\n'
        'citation_var = "s"\n'
    )
    result = _run("ontology", "verified", "--templates-dir", str(tmp_path))
    assert result.exit_code != 0


# --- conformance -----------------------------------------------------------


def _seed_quarantine(path: Path) -> str:
    from ne.brain_ontology.quarantine import QuarantinedTriples, SqliteTripleQuarantine

    qid = "urn:ne:quarantine:test-1"
    SqliteTripleQuarantine(path).record(
        QuarantinedTriples(
            quarantine_id=qid,
            delta_nt='<urn:a> <urn:b> "c" .\n',
            violations=("seeded",),
            provenance="test",
            shapes_hash="b" * 64,
            created_at="2026-07-10T00:00:00+00:00",
            graph_iri=None,
        )
    )
    return qid


def test_conformance_green_reports_seeded_quarantine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "q.db"
    qid = _seed_quarantine(db)
    monkeypatch.setenv("NE_ONTOLOGY_QUARANTINE_DB", str(db))

    result = _run("ontology", "conformance", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["gate"] == "passed"
    assert payload["quarantine"]["count"] == 1
    entry = payload["quarantine"]["entries"][0]
    assert entry["quarantine_id"] == qid
    assert entry["provenance"] == "test"
    assert entry["violations"] == ["seeded"]
    assert "delta_nt" not in entry


def test_conformance_json_shape_stable() -> None:
    result = _run("ontology", "conformance", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {
        "gate",
        "untriaged_errors",
        "warnings",
        "triaged_categories",
        "quarantine",
    }


def test_conformance_failure_exit1(monkeypatch: pytest.MonkeyPatch) -> None:
    from ne.brain_ontology.lint.validate_bfo import LintReport

    monkeypatch.setattr(
        "ne.brain_ontology.lint.gate.run_corpus_conformance",
        lambda extra=None: LintReport(errors=("[X] urn:s: bad",), warnings=()),
    )
    result = _run("ontology", "conformance", "--json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["gate"] == "failed"
    assert "[X] urn:s: bad" in payload["untriaged_errors"]


# --- lint (alias of conformance) --------------------------------------------


def test_lint_help_documents_alias() -> None:
    result = _run("ontology", "lint", "--help")
    assert result.exit_code == 0
    assert "conformance" in result.output.lower()


def test_lint_alias_matches_conformance_json() -> None:
    lint_result = _run("ontology", "lint", "--json")
    conf_result = _run("ontology", "conformance", "--json")
    assert lint_result.exit_code == 0, lint_result.output
    assert conf_result.exit_code == 0, conf_result.output
    assert json.loads(lint_result.output) == json.loads(conf_result.output)


def test_lint_failure_exit1(monkeypatch: pytest.MonkeyPatch) -> None:
    from ne.brain_ontology.lint.validate_bfo import LintReport

    monkeypatch.setattr(
        "ne.brain_ontology.lint.gate.run_corpus_conformance",
        lambda extra=None: LintReport(errors=("[X] urn:s: bad",), warnings=()),
    )
    result = _run("ontology", "lint", "--json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["gate"] == "failed"
    assert "[X] urn:s: bad" in payload["untriaged_errors"]
