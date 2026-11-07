# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-135: the governed ontology + verified-answer HTTP surface.

Every route rides the EXISTING lanes — the C12-gated gateway ``sparql`` steward
surface for the class browser, the compiled shapes for the shape list, the
inspectable quarantine ``.list()`` (redacted) for the violation feed, and the frozen
``GovernedToolExecutor`` (``read.kg.<id>``) for invoke. The tests prove: disabled ⇒
503; the class tree comes ONLY from ``urn:ne:ontology:*`` graphs (a decoy instance
graph is invisible); invoke is governed + audited + C7-trimmed and fails closed on an
unknown/denied/version-mismatched/guardrail-violating call; the C12 gate the routes
ride is live on the COMPOSED app; and the router source keeps its single-call-site
invariants.
"""

from __future__ import annotations

import json
import pathlib
import re
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa

from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.brain_ontology import PropertyGraphLift
from ne.brain_ontology import inspection as ontology_inspection
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.lift.graph_to_rdf import entity_iri
from ne.brain_ontology.quarantine import QuarantinedTriples, SqliteTripleQuarantine
from ne.brain_ontology.shapes import build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.contracts import reset_config_hash_for_testing
from ne.govern.opa import OpaClient
from ne.govern.pdp import GovernedPdp

from .conftest import AppBuilder, get_state, run
from .conftest import _rego_faithful_doc as _rego_doc

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "ne" / "api"

# --- template TOMLs (verbatim from test_verified_answer_tools) --------------

_TEMPLATES = {
    "accounts_directory.toml": """
template_id = "accounts-directory"
version = 1
description = "Every account visible to the caller, with its name."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account a <https://neutron.enterprise/schema/operational/Account> .
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
}
\"\"\"

[scope]
graph_classes = ["alpha", "beta"]
result_shape = "enumerable"

[answer]
columns = ["account", "name"]
citation_var = "account"
label_var = "name"
""",
    "entity_by_iri.toml": """
template_id = "entity-by-iri"
version = 1
description = "Fetch one entity's name by its exact IRI."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
}
\"\"\"

[args.account]
type = "iri"

[scope]
graph_classes = ["alpha", "beta"]
result_shape = "enumerable"

[answer]
columns = ["account", "name"]
citation_var = "account"
""",
}

# A mini BFO/CCO/neop class hierarchy loaded into an ontology graph. Three owl:Class
# with rdfs:label + rdfs:subClassOf spanning the three source namespaces the router
# classifies (bfo / cco / neop).
_MINI_TTL = b"""
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

<http://purl.obolibrary.org/obo/BFO_0000001> a owl:Class ;
    rdfs:label "entity" .

<https://www.commoncoreontologies.org/ont00001180> a owl:Class ;
    rdfs:label "Agent" ;
    rdfs:subClassOf <http://purl.obolibrary.org/obo/BFO_0000001> .

<https://neutron.enterprise/schema/operational/Account> a owl:Class ;
    rdfs:label "Account" ;
    rdfs:subClassOf <https://www.commoncoreontologies.org/ont00001180> .
"""

# A decoy owl:Class in an INSTANCE-data graph (NOT urn:ne:ontology:*) — the classes
# route must NEVER surface it (proves the urn:ne:ontology:* graph containment).
_DECOY_TTL = b"""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<urn:decoy:Klass> a owl:Class .
"""

_BFO = "http://purl.obolibrary.org/obo/BFO_0000001"
_CCO = "https://www.commoncoreontologies.org/ont00001180"
_NEOP = "https://neutron.enterprise/schema/operational/Account"


def _write_templates(directory: Path, templates: dict[str, str] = _TEMPLATES) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in templates.items():
        (directory / name).write_text(body, encoding="utf-8")
    return directory


def _account(key: str, acl: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "entity_class": "Account",
        "display_name": "Acct",
        "source_system": "crm",
        "source_ref": f"{key}:1",
        "acl": acl,
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "v",
        "id": f"{key}-id",
        "name": f"{key} Corp",
    }


class _NullSink:
    def emit(self, event: Any) -> None:
        return None


def _seed_store() -> ValidatingTripleStore:
    """A REAL gateway over oxigraph, seeded with accounts + the mini ontology + a decoy."""
    settings = OntologySettings(include_ontology_graph=False)
    backend = OxigraphBackend()
    store = ValidatingTripleStore(
        backend,
        build_validator(settings),
        SqliteTripleQuarantine(),
        _NullSink(),
        settings=settings,
        config_hash="s" * 64,
        compiled_shapes=load_compiled_shapes(),
    )

    async def _seed() -> None:
        mem = InMemoryGraphStore()
        await mem.upsert_nodes(
            [
                _account("acc_alpha", ["alpha"]),
                _account("acc_beta", ["beta"]),
                _account("acc_both", ["alpha", "beta"]),
            ]
        )
        await PropertyGraphLift(mem, store).run()
        # Backend-direct loads (simulate the corpus load + instance-data drift).
        await backend.load_turtle(_MINI_TTL, graph_iri="urn:ne:ontology:test:mini")
        await backend.load_turtle(_DECOY_TTL, graph_iri="urn:ne:data:decoy")

    run(_seed())
    return store


def _enabled_settings(tmp_path: Path, *, quarantine_db: Path | None = None) -> OntologySettings:
    directory = _write_templates(tmp_path / "verified")
    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
        quarantine_db=quarantine_db,
    )


# --- a PDP that DENIES every read.kg.* action (mirrors RegoFaithfulPdpFactory) ---


def _deny_kg_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        action = payload["input"]["action"]
        if action.startswith("read.kg."):
            doc = {
                "effect": "deny",
                "tier": 0,
                "reason": "rego-faithful: deny (read.kg.*)",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        else:
            doc = _rego_doc(action)
        return httpx.Response(200, json={"result": doc})

    return httpx.MockTransport(handler)


class _DenyKgPdpFactory:
    """A ``policy_decision_point`` plugin denying every ``read.kg.*`` action."""

    @staticmethod
    def from_settings(_settings: Any, *, sink: Any) -> GovernedPdp:
        client = httpx.AsyncClient(base_url="http://opa.test", transport=_deny_kg_transport())
        return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


def _audit_rows(audit_url: str) -> list[dict[str, Any]]:
    engine = sa.create_engine(audit_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT action, outcome, config_hash, payload FROM ne_audit_log")
            ).fetchall()
    finally:
        engine.dispose()
    out: list[dict[str, Any]] = []
    for action, outcome, config_hash, payload in rows:
        data = payload if isinstance(payload, dict) else json.loads(payload)
        out.append(
            {
                "action": action,
                "outcome": outcome,
                "config_hash": config_hash,
                "detail": data.get("detail"),
                "payload": data,
            }
        )
    return out


# --- (1) disabled --------------------------------------------------------------


def test_ontology_routes_disabled_without_t3_config(make_app: AppBuilder) -> None:
    h = make_app()
    assert get_state(h.app).ontology is None

    async def _t() -> None:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            for path in (
                "/api/v1/ontology/classes",
                "/api/v1/ontology/shapes",
                "/api/v1/ontology/violations",
                "/api/v1/verified/templates",
            ):
                r = await client.get(path, headers=headers)
                assert r.status_code == 503, path
                assert r.json()["detail"] == "ontology-disabled", path
            r = await client.post(
                "/api/v1/verified/invoke", json={"template_id": "x"}, headers=headers
            )
            assert r.status_code == 503
            assert r.json()["detail"] == "ontology-disabled"
            # OIDC gate holds: no bearer → 401 (never a silent 503).
            unauth = await client.get("/api/v1/ontology/classes")
            assert unauth.status_code == 401

    run(_t())


# --- (2) classes -----------------------------------------------------------


def test_classes_route_returns_tree_from_ontology_graphs_only(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)

    async def _t() -> dict[str, Any]:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            r = await client.get("/api/v1/ontology/classes", headers=headers)
            assert r.status_code == 200
            return r.json()

    body = run(_t())
    by_iri = {c["iri"]: c for c in body["classes"]}
    assert _BFO in by_iri and _CCO in by_iri and _NEOP in by_iri
    # The decoy owl:Class (loaded into an INSTANCE graph) is absent — proves the
    # urn:ne:ontology:* graph containment.
    assert "urn:decoy:Klass" not in by_iri

    assert by_iri[_BFO]["label"] == "entity"
    assert by_iri[_BFO]["source"] == "bfo"
    assert by_iri[_BFO]["parents"] == []

    assert by_iri[_CCO]["label"] == "Agent"
    assert by_iri[_CCO]["source"] == "cco"
    assert by_iri[_CCO]["parents"] == [_BFO]

    assert by_iri[_NEOP]["label"] == "Account"
    assert by_iri[_NEOP]["source"] == "neop"
    assert by_iri[_NEOP]["parents"] == [_CCO]

    assert body["sources"]  # pinned corpus versions present
    assert len(body["shapes_hash"]) == 64
    int(body["shapes_hash"], 16)  # 64-hex


# --- (3) shapes ------------------------------------------------------------


def test_shapes_route_lists_compiled_shapes(make_app: AppBuilder, tmp_path: Path) -> None:
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)

    async def _t() -> dict[str, Any]:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            r = await client.get("/api/v1/ontology/shapes", headers=headers)
            assert r.status_code == 200
            return r.json()

    body = run(_t())
    assert body["shapes"]  # non-empty
    for entry in body["shapes"]:
        assert entry["iri"]
        assert entry["scope"] in {"per-write", "global"}
    assert body["shapes_hash"] == store.ontology_version().shapes_hash


# --- (4) violations (redacted) --------------------------------------------


def test_violations_route_summarizes_quarantine_redacted(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    quar_path = tmp_path / "quarantine.sqlite"
    store = _seed_store()
    h = make_app(
        ontology_settings=_enabled_settings(tmp_path, quarantine_db=quar_path),
        triple_store=store,
    )
    # Record one entry (with SMUGGLE sentinels) into the SAME sqlite file the route
    # reads via state.ontology.quarantine.
    lane = SqliteTripleQuarantine(quar_path)
    lane.record(
        QuarantinedTriples(
            quarantine_id="urn:ne:quarantine:test-1",
            delta_nt="<urn:s> <urn:p> <urn:SMUGGLE-DELTA> .",
            violations=("focus shape SMUGGLE-VIOLATION-MESSAGE",),
            provenance="ne:brain-ontology:gateway",
            shapes_hash="a" * 64,
            created_at="2026-07-10T00:00:00+00:00",
        )
    )

    async def _t() -> httpx.Response:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.get("/api/v1/ontology/violations", headers=headers)

    r = run(_t())
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["count"] == 1
    entry = body["entries"][0]
    assert entry["quarantine_id"] == "urn:ne:quarantine:test-1"
    assert entry["shapes_hash"] == "a" * 64
    assert entry["violation_count"] == 1
    assert entry["quarantined_whole"] is True
    # C12 redaction: neither the delta triples nor the violation message cross the wire.
    assert "SMUGGLE-DELTA" not in r.text
    assert "SMUGGLE-VIOLATION-MESSAGE" not in r.text

    # With quarantine_db unset ⇒ configured=false, no entries. (Second composition in
    # one test ⇒ reset the process-global frozen hash first.)
    reset_config_hash_for_testing()
    store2 = _seed_store()
    h2 = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store2)

    async def _t2() -> dict[str, Any]:
        headers = h2.bearer("user:a", groups=["alpha"], department="engineering")
        async with h2.client() as client:
            r2 = await client.get("/api/v1/ontology/violations", headers=headers)
            assert r2.status_code == 200
            return r2.json()

    body2 = run(_t2())
    assert body2["configured"] is False
    assert body2["entries"] == []


# --- (4b) active generated model + safe issue detail ----------------------


def test_active_model_route_uses_generated_model_when_optional_graph_is_empty(
    make_app: AppBuilder,
) -> None:
    h = make_app()

    async def _t() -> dict[str, Any]:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            response = await client.get("/api/v1/ontology/model", headers=headers)
            assert response.status_code == 200
            return response.json()

    body = run(_t())
    assert body["artifact_health"] == {"state": "healthy", "detail": None}
    assert body["ontology_graph"] == {"state": "not_loaded", "optional_class_count": 0}
    assert body["issue_tracking"] == {"state": "unconfigured", "count": None}
    assert body["validation_health"] == "unavailable"
    assert body["model"]["provenance"]["title"] == "Neutron Enterprise Operational Schema"
    assert body["model"]["concept_count"] == 11
    assert {concept["name"] for concept in body["model"]["concepts"]} >= {
        "Account",
        "Person",
        "Document",
        "Ticket",
        "Order",
        "OperationalRecordSet",
    }


def test_active_model_route_reports_loaded_and_unavailable_graph_states(
    make_app: AppBuilder, tmp_path: Path, monkeypatch: Any
) -> None:
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)
    headers = h.bearer("user:a", groups=["alpha"], department="engineering")

    async def _loaded() -> dict[str, Any]:
        async with h.client() as client:
            response = await client.get("/api/v1/ontology/model", headers=headers)
            assert response.status_code == 200
            return response.json()

    loaded = run(_loaded())
    assert loaded["ontology_graph"]["state"] == "loaded"
    assert loaded["ontology_graph"]["optional_class_count"] == 3

    async def unavailable(_query: str) -> Any:
        raise OSError("backend URL must not cross the wire")

    monkeypatch.setattr(store, "sparql", unavailable)
    unavailable_body = run(_loaded())
    assert unavailable_body["ontology_graph"] == {
        "state": "unavailable",
        "optional_class_count": None,
    }
    assert unavailable_body["model"] == loaded["model"]


def test_active_model_route_matches_generated_and_compiled_fingerprints(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)

    async def _t() -> dict[str, Any]:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return (await client.get("/api/v1/ontology/model", headers=headers)).json()

    body = run(_t())
    projection = ontology_inspection.load_active_model_projection()
    provenance = body["model"]["provenance"]
    assert provenance["schema_version"] == projection.provenance.schema_version
    assert provenance["source_sha256"] == projection.provenance.source_sha256
    assert provenance["json_schema_sha256"] == projection.provenance.json_schema_sha256
    assert provenance["shapes_sha256"] == store.ontology_version().shapes_hash
    assert body["model"]["rule_count"] == len(body["model"]["rules"])


def test_active_model_route_is_invariant_across_two_personas(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> tuple[dict[str, Any], dict[str, Any]]:
        broad = h.bearer("user:broad", groups=["alpha", "beta"], department="engineering")
        narrow = h.bearer("user:narrow", groups=["restricted"], department="legal")
        async with h.client() as client:
            first = await client.get("/api/v1/ontology/model", headers=broad)
            second = await client.get("/api/v1/ontology/model", headers=narrow)
            assert first.status_code == second.status_code == 200
            return first.json(), second.json()

    broad, narrow = run(_t())
    assert broad["model"] == narrow["model"]


def test_active_model_route_reports_unconfigured_issues_without_claiming_healthy(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=_seed_store())

    async def _t() -> dict[str, Any]:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return (await client.get("/api/v1/ontology/model", headers=headers)).json()

    body = run(_t())
    assert body["issue_tracking"] == {"state": "unconfigured", "count": None}
    assert body["validation_health"] == "unavailable"


def test_active_model_route_distinguishes_healthy_and_open_issues(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    quarantine_db = tmp_path / "quarantine.sqlite"
    h = make_app(
        ontology_settings=_enabled_settings(tmp_path, quarantine_db=quarantine_db),
        triple_store=_seed_store(),
    )
    headers = h.bearer("user:a", groups=["alpha"], department="engineering")

    async def _read() -> dict[str, Any]:
        async with h.client() as client:
            response = await client.get("/api/v1/ontology/model", headers=headers)
            assert response.status_code == 200
            return response.json()

    healthy = run(_read())
    assert healthy["issue_tracking"] == {"state": "configured", "count": 0}
    assert healthy["validation_health"] == "healthy"

    lane = SqliteTripleQuarantine(quarantine_db)
    lane.record(
        QuarantinedTriples(
            quarantine_id="issue-1",
            delta_nt="<urn:s> <urn:p> <urn:o> .",
            violations=("urn:record:1 urn:rule:required Required field is missing",),
            provenance="ne:brain-ontology:gateway",
            shapes_hash=load_compiled_shapes().shapes_hash,
            created_at="2026-08-20T00:00:00+00:00",
        )
    )
    issues = run(_read())
    assert issues["issue_tracking"] == {"state": "configured", "count": 1}
    assert issues["validation_health"] == "issues"


def test_active_model_route_fails_closed_on_shapes_hash_drift(
    make_app: AppBuilder, tmp_path: Path, monkeypatch: Any
) -> None:
    projection = ontology_inspection.load_active_model_projection()
    drifted = replace(
        projection,
        provenance=replace(projection.provenance, shapes_sha256="f" * 64),
    )
    monkeypatch.setattr(ontology_inspection, "load_active_model_projection", lambda: drifted)
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=_seed_store())

    async def _t() -> dict[str, Any]:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            response = await client.get("/api/v1/ontology/model", headers=headers)
            assert response.status_code == 200
            return response.json()

    body = run(_t())
    assert body["model"] is None
    assert body["artifact_health"] == {
        "state": "invalid",
        "detail": "compiled-shapes-fingerprint-mismatch",
    }


def test_violation_detail_requires_ops_role_and_audits_denial(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    quarantine_db = tmp_path / "quarantine.sqlite"
    h = make_app(
        ontology_settings=_enabled_settings(tmp_path, quarantine_db=quarantine_db),
        triple_store=_seed_store(),
        ops_roles=("ops",),
    )

    async def _t() -> httpx.Response:
        headers = h.bearer("user:no", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.get("/api/v1/ontology/violations/issue-1", headers=headers)

    response = run(_t())
    assert response.status_code == 403
    assert response.json()["detail"] == "not authorized"
    denied = [
        row
        for row in _audit_rows(h.audit_url)
        if row["action"] == "api.ontology.issue_detail.unauthorized"
    ]
    assert len(denied) == 1
    assert denied[0]["outcome"] == "denied"
    assert json.loads(denied[0]["detail"]) == {
        "reason": "not-authorized",
        "request_id": "issue-1",
    }


def test_violation_detail_empty_ops_roles_fails_closed_before_store_read(
    make_app: AppBuilder, tmp_path: Path, monkeypatch: Any
) -> None:
    quarantine_db = tmp_path / "quarantine.sqlite"
    h = make_app(
        ontology_settings=_enabled_settings(tmp_path, quarantine_db=quarantine_db),
        triple_store=_seed_store(),
    )
    ontology = get_state(h.app).ontology
    assert ontology is not None and ontology.quarantine is not None
    get_called = False

    def probed(_issue_id: str) -> Any:
        nonlocal get_called
        get_called = True
        return None

    monkeypatch.setattr(ontology.quarantine, "get", probed)

    async def _t() -> httpx.Response:
        headers = h.bearer("user:no", groups=["ops"], department="engineering")
        async with h.client() as client:
            return await client.get("/api/v1/ontology/violations/issue-1", headers=headers)

    response = run(_t())
    assert response.status_code == 403
    assert get_called is False


def test_violation_detail_returns_safe_reason_and_provenance_without_delta(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    quarantine_db = tmp_path / "quarantine.sqlite"
    lane = SqliteTripleQuarantine(quarantine_db)
    lane.record(
        QuarantinedTriples(
            quarantine_id="issue-1",
            delta_nt="<urn:SMUGGLE-SUBJECT> <urn:p> <urn:SMUGGLE-DELTA> .",
            violations=(
                "urn:record:1 urn:rule:required Required field is missing",
                "legacy-unstructured-SMUGGLE",
            ),
            provenance="ne:brain-ontology:gateway",
            shapes_hash="a" * 64,
            created_at="2026-08-20T00:00:00+00:00",
            graph_iri="urn:ne:graph:acl:opaque",
        )
    )
    h = make_app(
        ontology_settings=_enabled_settings(tmp_path, quarantine_db=quarantine_db),
        triple_store=_seed_store(),
        ops_roles=("ops",),
    )

    async def _t() -> httpx.Response:
        headers = h.bearer("user:ops", groups=["ops"], department="engineering")
        async with h.client() as client:
            return await client.get("/api/v1/ontology/violations/issue-1", headers=headers)

    response = run(_t())
    assert response.status_code == 200
    assert response.json() == {
        "issue_id": "issue-1",
        "created_at": "2026-08-20T00:00:00+00:00",
        "graph_iri": "urn:ne:graph:acl:opaque",
        "shapes_sha256": "a" * 64,
        "provenance": "ne:brain-ontology:gateway",
        "lifecycle_state": "quarantined",
        "reasons": [
            {
                "affected_reference": "urn:record:1",
                "rule_reference": "urn:rule:required",
                "reason": "Required field is missing",
            },
            {
                "affected_reference": None,
                "rule_reference": None,
                "reason": "Validation rule was not satisfied",
            },
        ],
    }
    assert "SMUGGLE-DELTA" not in response.text
    assert "SMUGGLE-SUBJECT" not in response.text
    assert "legacy-unstructured-SMUGGLE" not in response.text


def test_violation_detail_unknown_is_404_after_role_gate(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    quarantine_db = tmp_path / "quarantine.sqlite"
    h = make_app(
        ontology_settings=_enabled_settings(tmp_path, quarantine_db=quarantine_db),
        triple_store=_seed_store(),
        ops_roles=("ops",),
    )

    async def _t() -> httpx.Response:
        headers = h.bearer("user:ops", groups=["ops"], department="engineering")
        async with h.client() as client:
            return await client.get("/api/v1/ontology/violations/missing", headers=headers)

    response = run(_t())
    assert response.status_code == 404
    assert response.json()["detail"] == "validation-issue-not-found"


# --- (5) templates ---------------------------------------------------------


def test_templates_route_lists_registry(make_app: AppBuilder, tmp_path: Path) -> None:
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)

    async def _t() -> dict[str, Any]:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            r = await client.get("/api/v1/verified/templates", headers=headers)
            assert r.status_code == 200
            return r.json()

    body = run(_t())
    assert body["enabled"] is True
    assert body["registry_hash"]
    by_id = {t["template_id"]: t for t in body["templates"]}
    assert set(by_id) == {"accounts-directory", "entity-by-iri"}
    directory = by_id["accounts-directory"]
    assert directory["version"] == 1
    assert directory["title"] == "Every account visible to the caller, with its name."
    assert directory["tool_name"] == "read.kg.accounts_directory"
    assert directory["citation_var"] == "account"
    assert directory["columns"] == ["account", "name"]
    # The label/identifier distinction crosses the wire: label_var is the
    # template's own declaration, and a template that declares none serves null
    # (the console's degrade case) — never a guessed label.
    assert directory["label_var"] == "name"
    assert by_id["entity-by-iri"]["label_var"] is None
    assert isinstance(directory["args_schema"], dict)

    # ontology-enabled-but-verified-OFF (oxigraph_path set, seeded store) ⇒ enabled=false.
    # (Second composition in one test ⇒ reset the process-global frozen hash first.)
    reset_config_hash_for_testing()
    store2 = _seed_store()
    off_settings = OntologySettings(
        verified_answers_enabled=False,
        include_ontology_graph=False,
        backend="oxigraph",
        oxigraph_path=str(tmp_path / "unused-store"),
    )
    h2 = make_app(ontology_settings=off_settings, triple_store=store2)
    assert get_state(h2.app).ontology is not None

    async def _t2() -> dict[str, Any]:
        headers = h2.bearer("user:a", groups=["alpha"], department="engineering")
        async with h2.client() as client:
            r2 = await client.get("/api/v1/verified/templates", headers=headers)
            assert r2.status_code == 200
            return r2.json()

    body2 = run(_t2())
    assert body2["enabled"] is False
    assert body2["registry_hash"] is None
    assert body2["templates"] == []


# --- (6) invoke: governed + audited + C7-trimmed ---------------------------


def test_invoke_routes_through_governed_executor_and_audits(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)
    config_hash = get_state(h.app).config_hash

    async def _t() -> dict[str, Any]:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            r = await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "accounts-directory"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            return r.json()

    body = run(_t())
    answer = body["answer"]
    seen = {row[0] for row in answer["rows"]}
    assert entity_iri("acc_alpha") in seen  # visible to the alpha caller
    assert entity_iri("acc_beta") not in seen  # C7 trim over HTTP
    assert all(c.startswith("urn:") for c in answer["citations"])  # all IRIs
    assert answer["config_hash"] == config_hash
    assert body["replay_match"] is True
    # NE-CTXB-5 envelope on the wire: shown count pinned to rows, untruncated total
    # is an exact trusted count, no truncation reason.
    assert answer["returned_rows"] == len(answer["rows"])
    assert answer["total_rows"] == answer["returned_rows"]
    assert answer["truncation_reason"] is None

    rows = _audit_rows(h.audit_url)
    actions = [r["action"] for r in rows]
    assert "pdp.decide" in actions
    successes = [r for r in rows if r["action"] == "tool.execute" and r["outcome"] == "success"]
    assert len(successes) >= 2  # invoke + replay, both governed
    # config_hash stamped on every event.
    assert all(r["config_hash"] == config_hash for r in rows)
    # No row VALUE ('Corp') anywhere in the audit details (C6 + audited proof).
    assert "Corp" not in json.dumps([r["detail"] for r in rows])


# --- (7) invoke unknown template fails closed ------------------------------


def test_invoke_unknown_template_fails_closed(make_app: AppBuilder, tmp_path: Path) -> None:
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)

    async def _t() -> httpx.Response:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.post(
                "/api/v1/verified/invoke", json={"template_id": "nope"}, headers=headers
            )

    r = run(_t())
    # THE REGISTRY PRE-CHECK, and this test pins ONLY that cause: ``nope`` is absent
    # from the frozen registry, so the executor is never reached. The sibling test
    # below covers the OTHER 404 (a staged template with no executor binding), which
    # carries a DIFFERENT detail on purpose.
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown-template"

    rows = _audit_rows(h.audit_url)
    denied = [r for r in rows if r["action"] == "api.verified.invoke" and r["outcome"] == "denied"]
    assert len(denied) == 1
    assert json.loads(denied[0]["detail"])["reason"] == "unknown-template"
    # The executor was never reached: no tool.execute row.
    assert not any(r["action"] == "tool.execute" for r in rows)


def test_invoke_staged_template_without_a_binding_is_a_distinct_404(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    """A STAGED template whose executor binding is missing must NOT look like an
    absent template.

    Both raise 404 from ``/v1/verified/invoke``, but they mean opposite things: the
    registry pre-check is a configuration fact ("this deployment does not hold it"),
    while ``ToolNotRegisteredError`` out of the executor means the id IS staged and
    its binding is gone — a platform regression. They shared the body
    ``unknown-template`` until this pin, and the demo arc's not-staged predicate
    downgraded that body to DEGRADE, so a lost binding made ``ne demo run`` exit
    zero. The bodies are now distinct and this test is what keeps them distinct.

    The divergence is staged the way the regression would arrive: a template present
    in the frozen registry the route reads, with NO binding of its ``read.kg.<id>``
    tool name in the executor the route executes through.
    """
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)

    state = get_state(h.app)
    ontology = state.ontology
    assert ontology is not None
    setup = ontology.verified
    assert setup is not None
    staged = next(t for t in setup.registry.templates if t.template_id == "accounts-directory")
    orphan = staged.model_copy(update={"template_id": "unbound-template"})
    assert orphan.tool_name == "read.kg.unbound_template"
    registry = setup.registry.model_copy(update={"templates": (*setup.registry.templates, orphan)})
    state.ontology = replace(ontology, verified=replace(setup, registry=registry))

    async def _t() -> httpx.Response:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "unbound-template"},
                headers=headers,
            )

    r = run(_t())
    assert r.status_code == 404
    assert r.json()["detail"] == "template-tool-unregistered"
    # NOT the absent-template refusal — that is the whole point of the test.
    assert r.json()["detail"] != "unknown-template"

    rows = _audit_rows(h.audit_url)
    # The pre-check did NOT fire (the id IS in the registry); the executor's own
    # unknown-tool audit did, and nothing executed.
    assert not [
        r for r in rows if r["action"] == "api.verified.invoke" and r["outcome"] == "denied"
    ]
    assert not any(r["action"] == "tool.execute" and r["outcome"] == "success" for r in rows)


# --- (8) invoke PDP-denied fails closed ------------------------------------


def test_invoke_denied_by_pdp_fails_closed(make_app: AppBuilder, tmp_path: Path) -> None:
    store = _seed_store()
    h = make_app(
        ontology_settings=_enabled_settings(tmp_path),
        triple_store=store,
        pdp_factory=_DenyKgPdpFactory,
    )

    async def _t() -> httpx.Response:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "accounts-directory"},
                headers=headers,
            )

    r = run(_t())
    assert r.status_code == 403
    assert r.json()["detail"] == "tool-denied"

    rows = _audit_rows(h.audit_url)
    assert any(r["action"] == "pdp.decide" and r["outcome"] == "denied" for r in rows)
    # C6 fail-closed: no successful tool.execute through the real GovernedPdp.
    assert not any(r["action"] == "tool.execute" and r["outcome"] == "success" for r in rows)


# --- (9) invoke guardrail rejects unknown arg ------------------------------


def test_invoke_guardrail_rejects_unknown_arg(make_app: AppBuilder, tmp_path: Path) -> None:
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)

    async def _t() -> httpx.Response:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "accounts-directory", "args": {"surprise": "x"}},
                headers=headers,
            )

    r = run(_t())
    assert r.status_code == 422
    assert r.json()["detail"] == "guardrail-violation"


# --- (10) invoke version mismatch ------------------------------------------


def test_invoke_version_mismatch_409(make_app: AppBuilder, tmp_path: Path) -> None:
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)

    async def _t() -> httpx.Response:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "accounts-directory", "version": 2},
                headers=headers,
            )

    r = run(_t())
    assert r.status_code == 409
    assert r.json()["detail"] == "template-version-mismatch"

    rows = _audit_rows(h.audit_url)
    denied = [r for r in rows if r["action"] == "api.verified.invoke" and r["outcome"] == "denied"]
    assert len(denied) == 1
    assert json.loads(denied[0]["detail"])["reason"] == "version-mismatch"
    assert not any(r["action"] == "tool.execute" for r in rows)


# --- (11) C12 gate is live on the COMPOSED surface -------------------------


def test_c12_banned_sparql_rejected_on_composed_surface(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)
    ontology = get_state(h.app).ontology
    assert ontology is not None

    import pytest

    with pytest.raises(ValueError):
        run(ontology.store.sparql("DELETE WHERE { ?s ?p ?o }"))
    with pytest.raises(ValueError):
        run(
            ontology.store.sparql(
                "SELECT ?s WHERE { SERVICE <http://evil.test/sparql> { ?s ?p ?o } }"
            )
        )


# --- (12) source guardian --------------------------------------------------


def test_ontology_router_source_guardian() -> None:
    router_text = (_SRC / "routers" / "ontology.py").read_text(encoding="utf-8")
    # Exactly ONE .sparql( call site, passing the module-level _CLASSES_QUERY constant.
    assert router_text.count(".sparql(") == 1
    assert ".sparql(_CLASSES_QUERY)" in router_text
    # The load-bearing graph filter is present in the fixed query.
    assert 'STRSTARTS(STR(?g), "urn:ne:ontology:")' in router_text
    # executor.execute( only ever with template.tool_name.
    for line in router_text.splitlines():
        if "executor.execute(" in line:
            assert "template.tool_name" in line, line
    # No raw-SPARQL surface on the invoke request model.
    from ne.api.wire import VerifiedInvokeRequest

    assert "query" not in VerifiedInvokeRequest.model_fields
    assert "sparql" not in VerifiedInvokeRequest.model_fields

    # The new inspection surface is read-only and structurally cannot expose the
    # rejected RDF payload or management/query-execution inputs.
    model_text = (_SRC / "ontology_model.py").read_text(encoding="utf-8")
    assert "delta_nt" not in model_text
    assert "Release" not in model_text
    assert "Revalidate" not in model_text
    assert "query:" not in model_text

    # Exactly one resolve_triple_store( call in platform.py (single store instance).
    platform_text = (_SRC / "platform.py").read_text(encoding="utf-8")
    assert platform_text.count("resolve_triple_store(") == 1


# --- (13) NE-164b: read-path auditing on invoke -----------------------------


def _seed_store_alpha_beta_only() -> ValidatingTripleStore:
    """A REAL gateway seeded with exactly one alpha account + one beta account.

    Unlike :func:`_seed_store` (which also seeds an alpha+beta ``acc_both``), this
    gives an alpha-only caller EXACTLY one visible row — the precise count this
    unit's read-path audit test pins.
    """
    settings = OntologySettings(include_ontology_graph=False)
    backend = OxigraphBackend()
    store = ValidatingTripleStore(
        backend,
        build_validator(settings),
        SqliteTripleQuarantine(),
        _NullSink(),
        settings=settings,
        config_hash="s" * 64,
        compiled_shapes=load_compiled_shapes(),
    )

    async def _seed() -> None:
        mem = InMemoryGraphStore()
        await mem.upsert_nodes([_account("acc_alpha", ["alpha"]), _account("acc_beta", ["beta"])])
        await PropertyGraphLift(mem, store).run()

    run(_seed())
    return store


def test_invoke_emits_verified_read_audit_events(make_app: AppBuilder, tmp_path: Path) -> None:
    store = _seed_store_alpha_beta_only()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)
    config_hash = get_state(h.app).config_hash

    async def _t() -> httpx.Response:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "accounts-directory"},
                headers=headers,
            )

    r = run(_t())
    assert r.status_code == 200, r.text

    rows = _audit_rows(h.audit_url)
    events = [row for row in rows if row["action"] == "knowledge.verified_read"]
    assert len(events) == 2  # invoke + replay, each one actual store read
    for event in events:
        assert event["outcome"] == "success"
        assert event["config_hash"] == config_hash
        assert event["detail"] is not None
        assert re.match(
            r"^rows=\d+ row_limit=\d+ truncated=(true|false) "
            r"truncation_reason=(rows|tokens|none)$",
            event["detail"],
        )
        assert event["detail"] == "rows=1 row_limit=500 truncated=false truncation_reason=none"

    leaked_text = json.dumps(events)
    for leaked in ("Corp", "urn:ne:entity", "alpha", "beta"):
        assert leaked not in leaked_text


# --- V6 identity pin (T3) — design §3-V6, closes the platform.py:1304 substitution residual ---


def test_verified_tool_audit_sink_is_the_platform_sink_by_identity(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    """V6 identity pin for the T3 binding at ``platform.py:1304``, mirroring
    ``test_composition.py``'s T1 (``:93``) and T2 (``:116``) pins.

    Store-level emission (``test_invoke_emits_verified_read_audit_events`` above)
    proves an event LANDED — it cannot see sink IDENTITY. The substitution
    mutant recorded in ledger §2 (``platform.py:1304``'s ``audit_sink=sink`` ->
    a freshly-constructed ``create_audit_sink(audit_settings, registry)``, a
    different same-engine sink that bypasses the (4c)
    ``ToolTransitObservingSink`` wrap) leaves the full ne-api suite green
    (~516 passed) — a pure presence-insufficiency survivor.

    Unlike T1/T2, the T3 sink is not an attribute of any composed object: it
    is a FREE VARIABLE closed over by the ``_make_handler`` closure in
    ``ne.brain_ontology.verified.tools`` (``:191-218`` — ``_handler`` reads
    ``audit_sink`` from its enclosing scope, never storing it on ``self``).
    The pin therefore reaches into the closure cell directly. The
    ``FunctionType``/``co_freevars`` preconditions below are FAIL-LOUD re-pin
    signals if a future refactor changes ``_make_handler`` away from a plain
    closure (e.g. a callable class) — never a silent skip path.
    """
    store = _seed_store()
    h = make_app(ontology_settings=_enabled_settings(tmp_path), triple_store=store)
    state = get_state(h.app)

    kg = {
        name: binding
        for name, binding in state.executor._bindings.items()
        if name.startswith("read.kg.")
    }
    assert set(kg) == {"read.kg.accounts_directory", "read.kg.entity_by_iri"}

    for name, binding in kg.items():
        handler = binding.handler
        assert isinstance(handler, types.FunctionType), (
            f"{name}: T3 handler is no longer a plain closure — re-pin the audit-sink identity"
        )
        freevars = handler.__code__.co_freevars
        assert "audit_sink" in freevars, (
            f"{name}: _make_handler no longer closes over audit_sink — re-pin"
        )
        closure = handler.__closure__
        assert closure is not None
        assert closure[freevars.index("audit_sink")].cell_contents is state.sink, name


# --- NE-TEMPLATE-ENUM: enum refusal vs empty result over HTTP ----------------

_ENUM_TEMPLATES_HTTP = {
    **_TEMPLATES,
    "accounts_by_status.toml": """
template_id = "accounts-by-status"
version = 1
description = "Accounts whose name matches one closed-vocabulary value."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account a <https://neutron.enterprise/schema/operational/Account> .
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
  FILTER(?name = ?status)
}
\"\"\"

[args]
status = { type = "str", enum = ["acc_alpha Corp", "ghost"] }

[scope]
graph_classes = ["alpha", "beta"]

[answer]
columns = ["account", "name"]
citation_var = "account"
""",
}


def _enum_enabled_settings(tmp_path: Path) -> OntologySettings:
    directory = _write_templates(tmp_path / "verified-enum", _ENUM_TEMPLATES_HTTP)
    return OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
    )


def test_templates_route_advertises_declared_enum(make_app: AppBuilder, tmp_path: Path) -> None:
    h = make_app(
        ontology_settings=_enum_enabled_settings(tmp_path),
        triple_store=_seed_store(),
    )

    async def _t() -> httpx.Response:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.get("/api/v1/verified/templates", headers=headers)

    response = run(_t())
    assert response.status_code == 200, response.text
    by_id = {row["template_id"]: row for row in response.json()["templates"]}
    status = by_id["accounts-by-status"]["args_schema"]["properties"]["status"]
    assert status["enum"] == ["acc_alpha Corp", "ghost"]


def test_invoke_out_of_vocabulary_arg_is_422_invalid_args(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    # SPEC §12 item 3 over HTTP: the pre-gate refusal maps to 422 invalid-args
    # (status token only, C12) — never a 200 with zero rows.
    store = _seed_store()
    h = make_app(ontology_settings=_enum_enabled_settings(tmp_path), triple_store=store)

    async def _t() -> httpx.Response:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "accounts-by-status", "args": {"status": "unresolved"}},
                headers=headers,
            )

    r = run(_t())
    assert r.status_code == 422
    assert r.json()["detail"] == "invalid-args"
    assert "unresolved" not in r.text  # the rejected value is never echoed (C12)


def test_invoke_in_vocabulary_no_match_is_200_with_zero_rows(
    make_app: AppBuilder, tmp_path: Path
) -> None:
    # CONTROL: a permitted value that matches nothing still EXECUTES — a 200 with
    # an empty answer, structurally distinct from the 422 refusal above.
    store = _seed_store()
    h = make_app(ontology_settings=_enum_enabled_settings(tmp_path), triple_store=store)

    async def _t() -> httpx.Response:
        headers = h.bearer("user:a", groups=["alpha"], department="engineering")
        async with h.client() as client:
            return await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": "accounts-by-status", "args": {"status": "ghost"}},
                headers=headers,
            )

    r = run(_t())
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert answer["returned_rows"] == 0
    assert answer["rows"] == []
