# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Tier-2 C7 guardian (permanent, unskippable) — clones ne-brain's mechanism.

(a) STRUCTURAL SWEEP — every class in ``src/ne/brain_graph`` with an ``async def``
    taking a ``principal`` arg is PINNED to ``REGISTERED_PRINCIPAL_READ_PATHS``;
    the store adapters are asserted principal-FREE.
(b) BEHAVIORAL NO-BYPASS — a match-nothing/groupless principal yields empty
    results; a real label set reaches the store as ``$acl_labels`` on EVERY node
    AND edge variable; returned payloads never carry an ACL label.
(c) PRODUCTION-HOOK INVARIANT — the imported ``acl_match_labels`` is non-empty,
    contains ``principal.id``, and an expander exception propagates.
(d) RAW-CYPHER CONFINEMENT — ``.cypher(`` call sites and ``def cypher`` live ONLY
    in the allowlisted modules.
(e) SOLE-WRITER STRUCTURAL SWEEP (NE-T2X-3) — a repo-wide AST scan pins which
    files may hold the ``"ne:llm"`` literal, reference ``LLM_SOURCE_SYSTEM``,
    or STAMP ``source_system`` — making F2 (truthful provenance) structural
    against FUTURE code, not just today's tree. Proven non-vacuous with
    planted-offender RED cases (a guardian without a demonstrated red case is
    unproven).
"""

from __future__ import annotations

import ast
import asyncio
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ne.brain.trimming import acl_match_labels
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.lightrag.retrieval import GraphRetriever
from ne.brain_graph.model import EntityKey
from ne.brain_graph.read import EntityGraph
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.connect.identity import source_acl_hash
from ne.contracts.source_authority import (
    M365SourcePoint,
    SourceAuthorityDecision,
    SourceAuthorityResolution,
    SourceAuthorityStamp,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.contracts.types import Citation, Principal, RetrievalResult

_SRC = Path(__file__).resolve().parents[1] / "src" / "ne" / "brain_graph"
_REPO = Path(__file__).resolve().parents[3]

# NE-T2X-3: verified UNCHANGED — extracted (ne:llm) elements flow through
# these SAME two surfaces (design C-P1-6, extension not exemption). A new
# principal-scoped read path here would be an authorization-surface design
# change this unit is not authorized to make.
REGISTERED_PRINCIPAL_READ_PATHS = {"EntityGraph", "GraphRetriever"}

_GUARD = (
    "C7 graph guardian: the set of principal-scoped read paths changed. A new "
    "such surface is an un-trimmed authorization bypass until it is registered in "
    "REGISTERED_PRINCIPAL_READ_PATHS AND behaviorally covered here."
)


def _authority_stamp(receipt_id: str, *, acl: tuple[str, ...] = ("g",)) -> SourceAuthorityStamp:
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a", site_id="", drive_id="drive-a", item_id=receipt_id
        ),
        generation=1,
    )
    acl_hash = source_acl_hash(acl)
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash=acl_hash,
        source_contract=contract,
        artifact_schema_version=1,
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision="head-a",
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=1,
        authority_receipt_id=receipt_id,
        authority_receipt_hash="e" * 64,
    )


class _AuthorityReader:
    async def resolve(
        self,
        stamp: SourceAuthorityStamp,
        *,
        principal_id: str,
        access_context_hash: str,
    ) -> SourceAuthorityResolution:
        del principal_id, access_context_hash
        decision = (
            SourceAuthorityDecision.AUTHORIZED
            if stamp.authority_receipt_id == "current"
            else SourceAuthorityDecision.RETRYABLE_UNKNOWN
        )
        return SourceAuthorityResolution.model_construct(decision=decision)


@pytest.mark.parametrize(
    "raw_acl",
    [None, [], "g", {"g"}, ["g", 7], ["g", ""], ["g", "x" * 2049]],
)
def test_stored_acl_must_be_a_nonempty_bounded_string_sequence(raw_acl: object) -> None:
    node = {
        "key": "malformed",
        "entity_class": "Account",
        "display_name": "MALFORMED_ACL_SENTINEL",
        "source_system": "m365",
        "source_ref": "malformed",
        "acl": raw_acl,
    }
    graph = EntityGraph(
        _RecordingStore(rows=[{"node": node}]),
        group_expander=None,
        audit_sink=None,
    )

    result = asyncio.run(
        graph.lookup(
            [EntityKey(entity_class="Account", key="malformed")],
            principal=Principal(id="u", groups=("g",)),
        )
    )

    assert result == ()


def test_current_stamp_acl_hash_matches_canonical_stored_acl() -> None:
    stamp = _authority_stamp("current", acl=("u", "g"))
    node = {
        "key": "matching",
        "entity_class": "Account",
        "display_name": "MATCHING_ACL_SENTINEL",
        "source_system": "m365",
        "source_ref": "matching",
        # Ordering and duplicates are canonicalized only by source_acl_hash.
        "acl": ["g", "u", "g"],
        "source_authority_stamps": [stamp.model_dump(mode="json")],
    }
    graph = EntityGraph(
        _RecordingStore(rows=[{"node": node}]),
        group_expander=None,
        audit_sink=None,
        source_authority=_AuthorityReader(),
        access_context_hash=lambda _principal: "f" * 64,
    )

    result = asyncio.run(
        graph.lookup(
            [EntityKey(entity_class="Account", key="matching")],
            principal=Principal(id="u", groups=("g",)),
        )
    )

    assert [item.display_name for item in result] == ["MATCHING_ACL_SENTINEL"]


def test_contracted_stamp_withholds_stale_wider_path_but_not_unrelated_path() -> None:
    contracted = _authority_stamp("current", acl=("g",)).model_dump(mode="json")

    def node(key: str, *, label: str) -> dict[str, object]:
        return {
            "key": key,
            "entity_class": "Account",
            "display_name": label,
            "source_system": "m365",
            "source_ref": key,
            "acl": ["g"],
            "source_authority_stamps": [contracted],
        }

    stale_nodes = [
        node("stale-a", label="STALE_PATH_SENTINEL_A"),
        node("stale-b", label="STALE_PATH_SENTINEL_B"),
    ]
    stale_edge = {
        "key": "stale-edge",
        "edge_kind": "RELATED_TO",
        "from_key": "stale-a",
        "to_key": "stale-b",
        "source_system": "m365",
        "source_ref": "stale-edge",
        # The graph copy still carries the former wider ACL while the retained
        # current stamp is already contracted to only "g".
        "acl": ["g", "revoked-group"],
        "source_authority_stamps": [contracted],
    }
    matching_nodes = [
        node("matching-a", label="UNRELATED_MATCHING_A"),
        node("matching-b", label="UNRELATED_MATCHING_B"),
    ]
    matching_edge = {
        "key": "matching-edge",
        "edge_kind": "RELATED_TO",
        "from_key": "matching-a",
        "to_key": "matching-b",
        "source_system": "m365",
        "source_ref": "matching-edge",
        "acl": ["g"],
        "source_authority_stamps": [contracted],
    }
    graph = EntityGraph(
        _RecordingStore(
            rows=[
                {"path_nodes": stale_nodes, "path_rels": [stale_edge]},
                {"path_nodes": matching_nodes, "path_rels": [matching_edge]},
            ]
        ),
        group_expander=None,
        audit_sink=None,
        source_authority=_AuthorityReader(),
        access_context_hash=lambda _principal: "f" * 64,
    )

    result = asyncio.run(
        graph.paths(
            EntityKey(entity_class="Account", key="any-a"),
            EntityKey(entity_class="Account", key="any-b"),
            principal=Principal(id="u", groups=("g",)),
        )
    )

    assert len(result) == 1
    assert [node.key for node in result[0].nodes] == ["matching-a", "matching-b"]
    assert [edge.key for edge in result[0].edges] == ["matching-edge"]
    assert "STALE_PATH_SENTINEL" not in repr(result)


# A `.cypher(` METHOD call may live only here (read.py = production caller;
# testing/* = the conformance scaffolding around the in-memory reference store).
# The AGE SQL function `ag_catalog.cypher(...)` in _pg.py is a string literal, not
# a Python method call, so the AST sweep never sees it.
_CYPHER_CALL_ALLOWLIST = {
    "read.py",
    "cli.py",
    "age/store.py",
    "testing/memory.py",
    "testing/kits/graph_store.py",
}
# A REAL `async def cypher` IMPLEMENTATION (non-stub body) may live only here;
# Protocol declarations (``...`` bodies) elsewhere are type-only and exempt.
_CYPHER_DEF_ALLOWLIST = {"age/store.py", "testing/memory.py"}


def _param_names(func: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
    a = func.args
    return [arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def _rel(path: Path) -> str:
    return str(path.relative_to(_SRC))


# -- Layer (a) -----------------------------------------------------------------


def _principal_scoped_classes() -> set[str]:
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and "principal" in _param_names(item):
                    found.add(node.name)
    return found


def test_a_principal_scoped_read_paths_pinned() -> None:
    assert _principal_scoped_classes() == REGISTERED_PRINCIPAL_READ_PATHS, _GUARD


def test_a_store_adapters_are_principal_free() -> None:
    for rel in ("age/store.py", "testing/memory.py"):
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                assert "principal" not in _param_names(node), (
                    f"{rel}:{node.name} must stay unscoped-by-design (no principal). {_GUARD}"
                )


# -- Layer (b) -----------------------------------------------------------------


class _RecordingStore:
    """Captures (text, params) of every cypher; returns canned rows."""

    def __init__(self, rows: Sequence[Mapping[str, Any]] = ()) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    async def cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append((query, dict(params or {})))
        return self.rows


def _all_variables(text: str) -> set[str]:
    return set(re.findall(r"\b([am]\d|n0|r\d)\b", text))


def test_b_real_labels_reach_store_on_every_variable() -> None:
    store = _RecordingStore()
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    principal = Principal(id="u", groups=("g",))

    async def scenario() -> None:
        await graph.neighbors([EntityKey(entity_class="X", key="k")], principal=principal, hops=2)
        await graph.paths(
            EntityKey(entity_class="X", key="a"),
            EntityKey(entity_class="X", key="b"),
            principal=principal,
        )

    asyncio.run(scenario())
    assert store.calls
    for text, params in store.calls:
        assert params.get("acl_labels") == sorted({"g", "u"})
        for var in _all_variables(text):
            assert f"{var}.acl" in text, f"variable {var} lacks the ACL predicate"
        for label in ("g", "u"):
            assert label not in text  # labels only via params


def test_b_groupless_principal_self_label_matches_nothing_foreign() -> None:
    # Store returns a node visible only to 'secret' — a groupless stranger cannot see it.
    node = {"key": "k", "entity_class": "Account", "acl": ["secret"], "display_name": "x"}
    store = _RecordingStore(rows=[{"node": node}])
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    stranger = Principal(id="stranger")

    result = asyncio.run(
        graph.lookup([EntityKey(entity_class="Account", key="k")], principal=stranger)
    )
    assert result == ()
    # The self-label DID reach the store (non-empty, fail-closed).
    assert store.calls[0][1]["acl_labels"] == ["stranger"]


def test_current_source_drift_withholds_the_entire_graph_path() -> None:
    current = _authority_stamp("current").model_dump(mode="json")
    stale = _authority_stamp("stale").model_dump(mode="json")
    nodes = [
        {
            "key": "a",
            "entity_class": "Account",
            "display_name": "A",
            "source_system": "m365",
            "source_ref": "a",
            "acl": ["g"],
            "source_authority_stamps": [current],
        },
        {
            "key": "b",
            "entity_class": "Account",
            "display_name": "B",
            "source_system": "m365",
            "source_ref": "b",
            "acl": ["g"],
            "source_authority_stamps": [current],
        },
    ]
    edge = {
        "key": "e",
        "edge_kind": "RELATED_TO",
        "from_key": "a",
        "to_key": "b",
        "source_system": "m365",
        "source_ref": "e",
        "acl": ["g"],
        "source_authority_stamps": [stale],
    }
    store = _RecordingStore(rows=[{"path_nodes": nodes, "path_rels": [edge]}])
    graph = EntityGraph(
        store,
        group_expander=None,
        audit_sink=None,
        source_authority=_AuthorityReader(),
        access_context_hash=lambda _principal: "f" * 64,
    )
    result = asyncio.run(
        graph.paths(
            EntityKey(entity_class="Account", key="a"),
            EntityKey(entity_class="Account", key="b"),
            principal=Principal(id="u", groups=("g",)),
        )
    )
    assert result == ()


def test_stamped_path_retains_exact_hop_stamps_without_changing_legacy_paths() -> None:
    stamp = _authority_stamp("current")
    raw = stamp.model_dump(mode="json")
    nodes = [
        {
            "key": key,
            "entity_class": "Account",
            "display_name": key.upper(),
            "source_system": "m365",
            "source_ref": key,
            "acl": ["g"],
            "source_authority_stamps": [raw],
        }
        for key in ("a", "b")
    ]
    edge = {
        "key": "e",
        "edge_kind": "RELATED_TO",
        "from_key": "a",
        "to_key": "b",
        "source_system": "m365",
        "source_ref": "e",
        "acl": ["g"],
        "source_authority_stamps": [raw],
    }
    store = _RecordingStore(rows=[{"path_nodes": nodes, "path_rels": [edge]}])
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    principal = Principal(id="u", groups=("g",))
    args = (
        EntityKey(entity_class="Account", key="a"),
        EntityKey(entity_class="Account", key="b"),
    )

    legacy = asyncio.run(graph.paths(*args, principal=principal))
    stamped = asyncio.run(graph.paths_with_source_stamps(*args, principal=principal))

    assert tuple(item.path for item in stamped) == legacy
    assert stamped[0].hop_stamps == ((stamp,),)


@pytest.mark.parametrize("raw_stamps", [None, [], [{"not": "a stamp"}]])
def test_stamped_path_fails_closed_when_any_element_stamp_is_absent_or_invalid(
    raw_stamps: object,
) -> None:
    raw = _authority_stamp("current").model_dump(mode="json")
    nodes = [
        {
            "key": key,
            "entity_class": "Account",
            "display_name": key.upper(),
            "source_system": "m365",
            "source_ref": key,
            "acl": ["g"],
            "source_authority_stamps": raw_stamps if key == "a" else [raw],
        }
        for key in ("a", "b")
    ]
    edge = {
        "key": "e",
        "edge_kind": "RELATED_TO",
        "from_key": "a",
        "to_key": "b",
        "source_system": "m365",
        "source_ref": "e",
        "acl": ["g"],
        "source_authority_stamps": [raw],
    }
    graph = EntityGraph(
        _RecordingStore(rows=[{"path_nodes": nodes, "path_rels": [edge]}]),
        group_expander=None,
        audit_sink=None,
    )
    result = asyncio.run(
        graph.paths_with_source_stamps(
            EntityKey(entity_class="Account", key="a"),
            EntityKey(entity_class="Account", key="b"),
            principal=Principal(id="u", groups=("g",)),
        )
    )
    assert result == ()


def test_b_returned_payloads_never_leak_acl() -> None:
    node = {
        "key": "k",
        "entity_class": "Account",
        "display_name": "Acme",
        "source_system": "salesforce",
        "source_ref": "Account:A1",
        "acl": ["g"],
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "1.0.0",
        "synced_at": "t",
        "name": "Acme",
    }
    store = _RecordingStore(rows=[{"node": node}])
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    principal = Principal(id="u", groups=("g",))
    result = asyncio.run(
        graph.lookup([EntityKey(entity_class="Account", key="k")], principal=principal)
    )
    assert len(result) == 1
    _assert_no_acl_leak(result[0].model_dump())


def _assert_no_acl_leak(value: Any) -> None:
    # NE-T2X-3 (scoped by Argus P3-3): "evidence" is denylisted here too, but
    # ONLY because every existing call site below feeds this helper either a
    # backbone node with no "evidence" property at all, or an ne:llm node
    # where the strip is unconditional (closing the U2-documented Argus-P2
    # over-return) — read.py itself strips "evidence" ONLY from ne:llm
    # elements (see `_LLM_ONLY_STRIP`), not unconditionally; do not reuse
    # this helper for a fixture where a NON-ne:llm element legitimately
    # carries an "evidence" business field (see
    # test_backbone_contract_declared_evidence_business_field_still_surfaces,
    # which asserts directly instead). "assurance" deliberately does NOT
    # join this denylist — it is a typed, intentionally-surfaced field, not
    # a leak.
    if isinstance(value, Mapping):
        for k, v in value.items():
            assert k not in {
                "acl",
                "content_hash",
                "config_hash",
                "schema_version",
                "synced_at",
                "evidence",
            }, f"leaked governance key {k!r} in a returned payload"
            _assert_no_acl_leak(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_acl_leak(item)


def test_backbone_contract_declared_evidence_business_field_still_surfaces() -> None:
    """P3-3 decision proof (scoped strip, not unconditional): a non-``ne:llm``
    (backbone connector) element may legitimately declare an ``evidence``
    business field through its own contract — ``construct/mappers.py`` has
    no reserved-key wall for backbone properties the way
    ``extraction.writer._PLATFORM_OWNED_KEYS`` does for the extraction path,
    so an UNCONDITIONAL strip would silently drop real customer data. Since
    extraction ``evidence`` refs exist ONLY on ``ne:llm``-written elements,
    scoping the strip to that ``source_system`` closes the U2 leak (see
    ``test_b_extracted_payload_never_leaks_acl_evidence_or_governance``)
    without widening it onto backbone business fields — this node's
    ``acl``/``content_hash``/``config_hash``/``synced_at`` still strip as
    always."""
    node = {
        "key": "bb1",
        "entity_class": "Case",
        "display_name": "Case 1",
        "source_system": "jira",
        "source_ref": "Case:1",
        "acl": ["g"],
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "1.0.0",
        "synced_at": "t",
        "evidence": ["case-ref-1"],
    }
    store = _RecordingStore(rows=[{"node": node}])
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    principal = Principal(id="u", groups=("g",))
    result = asyncio.run(
        graph.lookup([EntityKey(entity_class="Case", key="bb1")], principal=principal)
    )
    assert len(result) == 1
    assert result[0].properties["evidence"] == ["case-ref-1"]
    for stripped_key in ("acl", "content_hash", "config_hash", "synced_at"):
        assert stripped_key not in result[0].properties


# -- Layer (b): GraphRetriever no-bypass ---------------------------------------


class _FakeT1:
    def __init__(self, result: RetrievalResult) -> None:
        self._result = result

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Any = None,
    ) -> RetrievalResult:
        return self._result

    async def retrieve(self, chunk_ids: Any, *, principal: Principal) -> RetrievalResult:
        return self._result


def test_b_graph_retriever_no_citations_short_circuits_store_untouched() -> None:
    store = InMemoryGraphStore()
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    empty = _FakeT1(RetrievalResult(chunks=(), citations=()))
    retriever = GraphRetriever(graph, empty, BrainGraphSettings())
    result = asyncio.run(retriever.search("q", principal=Principal(id="stranger")))
    assert result.chunks == ()
    assert result.citations == ()
    assert result.graph_paths == ()
    assert store.cypher_calls == 0  # provably untouched


def test_b_graph_retriever_trims_invisible_graph_entities() -> None:
    store = InMemoryGraphStore()
    secret = make_node("s1", "Account", ("secret",), source_ref="S:1")
    asyncio.run(store.upsert_nodes([secret]))
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    citation = Citation(chunk_id="c", document_id="d", source_system="salesforce", source_ref="S:1")
    t1 = _FakeT1(RetrievalResult(chunks=(), citations=(citation,)))
    retriever = GraphRetriever(graph, t1, BrainGraphSettings())
    # The stranger's T1 citation points at a node only 'secret' can see: the graph
    # expansion must yield NO paths (trim-before-return, fail-closed).
    result = asyncio.run(retriever.search("q", principal=Principal(id="stranger")))
    assert result.graph_paths == ()


# -- Layer (b): ne:llm (extracted-element) fixtures — C-P1-6 guardian extension,
# NOT exemption. Extracted elements flow through the SAME EntityGraph/
# GraphRetriever surfaces pinned above; every existing (b) assertion above stays
# verbatim, these are additions.


def test_b_extracted_node_match_nothing_principal_gets_empty() -> None:
    store = InMemoryGraphStore()
    node = make_node(
        "llmx",
        "Account",
        ("acl-secret-lbl",),
        source_system="ne:llm",
        assurance="extracted",
        evidence=["sha256:p1|c1"],
    )
    asyncio.run(store.upsert_nodes([node]))
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    stranger = Principal(id="stranger")

    result = asyncio.run(
        graph.lookup([EntityKey(entity_class="Account", key="llmx")], principal=stranger)
    )
    assert result == ()


def test_b_extracted_payload_never_leaks_acl_evidence_or_governance() -> None:
    node = {
        "key": "k",
        "entity_class": "Account",
        "display_name": "Extracted Co",
        "source_system": "ne:llm",
        "source_ref": "llm:abc123:c1",
        "acl": ["acl-secret-lbl"],
        "assurance": "extracted",
        "evidence": ["sha256:p1|c1"],
        "content_hash": "h",
        "config_hash": "c",
        "schema_version": "1.0.0",
        "synced_at": "t",
    }
    store = _RecordingStore(rows=[{"node": node}])
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    principal = Principal(id="u", groups=("acl-secret-lbl",))
    result = asyncio.run(
        graph.lookup([EntityKey(entity_class="Account", key="k")], principal=principal)
    )
    assert len(result) == 1
    _assert_no_acl_leak(result[0].model_dump())
    assert "acl-secret-lbl" not in result[0].model_dump_json()
    assert result[0].assurance == "extracted"
    assert "evidence" not in result[0].properties
    assert "assurance" not in result[0].properties


def test_b_extracted_edge_trimmed_and_carries_assurance() -> None:
    store = InMemoryGraphStore()
    a = make_node("ea", "Account", ("g-eng",))
    b = make_node("eb", "Account", ("g-eng",))
    edge = make_edge("ABOUT", "ea", "eb", ("g-eng",), key="ea_eb")
    edge["source_system"] = "ne:llm"
    edge["assurance"] = "extracted"

    async def setup() -> None:
        await store.upsert_nodes([a, b])
        await store.upsert_edges([edge])

    asyncio.run(setup())
    graph = EntityGraph(store, group_expander=None, audit_sink=None)

    visible = Principal(id="u", groups=("g-eng",))
    sub = asyncio.run(
        graph.neighbors([EntityKey(entity_class="Account", key="ea")], principal=visible, hops=1)
    )
    assert {e.key for e in sub.edges} == {"ea_eb"}
    edge_out = next(e for e in sub.edges if e.key == "ea_eb")
    assert edge_out.assurance == "extracted"

    stranger = Principal(id="stranger")
    sub2 = asyncio.run(
        graph.neighbors([EntityKey(entity_class="Account", key="ea")], principal=stranger, hops=1)
    )
    assert sub2.edges == ()
    assert sub2.nodes == ()


def test_b_llm_element_missing_or_forged_assurance_reads_extracted_never_source() -> None:
    principal = Principal(id="u", groups=("g",))

    missing = {
        "key": "m1",
        "entity_class": "Account",
        "display_name": "M1",
        "source_system": "ne:llm",
        "source_ref": "llm:x:c1",
        "acl": ["g"],
    }
    store1 = _RecordingStore(rows=[{"node": missing}])
    graph1 = EntityGraph(store1, group_expander=None, audit_sink=None)
    result1 = asyncio.run(
        graph1.lookup([EntityKey(entity_class="Account", key="m1")], principal=principal)
    )
    assert result1[0].assurance == "extracted"

    forged = {
        "key": "m2",
        "entity_class": "Account",
        "display_name": "M2",
        "source_system": "ne:llm",
        "source_ref": "llm:x:c2",
        "acl": ["g"],
        "assurance": "source",  # forged toward the trusted-sounding value
    }
    store2 = _RecordingStore(rows=[{"node": forged}])
    graph2 = EntityGraph(store2, group_expander=None, audit_sink=None)
    result2 = asyncio.run(
        graph2.lookup([EntityKey(entity_class="Account", key="m2")], principal=principal)
    )
    assert result2[0].assurance == "extracted"

    # Malformed (unhashable) stored assurance — e.g. a corrupted list/mapping
    # value — must degrade gracefully to "extracted" rather than crash the
    # read path with a TypeError on the frozenset membership test (Codex C1).
    malformed = {
        "key": "m3",
        "entity_class": "Account",
        "display_name": "M3",
        "source_system": "ne:llm",
        "source_ref": "llm:x:c3",
        "acl": ["g"],
        "assurance": ["extracted"],  # unhashable — must not raise
    }
    store3 = _RecordingStore(rows=[{"node": malformed}])
    graph3 = EntityGraph(store3, group_expander=None, audit_sink=None)
    result3 = asyncio.run(
        graph3.lookup([EntityKey(entity_class="Account", key="m3")], principal=principal)
    )
    assert result3[0].assurance == "extracted"


def test_b_backbone_element_cannot_claim_confirmed() -> None:
    node = {
        "key": "s1",
        "entity_class": "Account",
        "display_name": "Acme",
        "source_system": "salesforce",
        "source_ref": "Account:1",
        "acl": ["g"],
        "assurance": "confirmed",  # smuggled stored prop
    }
    store = _RecordingStore(rows=[{"node": node}])
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    principal = Principal(id="u", groups=("g",))
    result = asyncio.run(
        graph.lookup([EntityKey(entity_class="Account", key="s1")], principal=principal)
    )
    assert result[0].assurance == "source"
    assert "assurance" not in result[0].properties


def test_b_graph_retriever_hops_carry_explicit_assurance() -> None:
    store = InMemoryGraphStore()
    bb1 = make_node("bb1", "Account", ("g-eng",), source_ref="S:1")
    bb2 = make_node("bb2", "Account", ("g-eng",))
    llmn = make_node("llmn", "Account", ("g-eng",), source_system="ne:llm", assurance="extracted")
    edge_llm = make_edge("ABOUT", "bb1", "llmn", ("g-eng",), key="bb1_llmn")
    edge_llm["source_system"] = "ne:llm"
    edge_llm["assurance"] = "extracted"
    edge_er = make_edge("SAME_AS", "bb1", "bb2", ("g-eng",), key="bb1_bb2")
    edge_er["source_system"] = "ne:er"

    async def setup() -> None:
        await store.upsert_nodes([bb1, bb2, llmn])
        await store.upsert_edges([edge_llm, edge_er])

    asyncio.run(setup())
    graph = EntityGraph(store, group_expander=None, audit_sink=None)
    citation = Citation(chunk_id="c", document_id="d", source_system="salesforce", source_ref="S:1")
    t1 = _FakeT1(RetrievalResult(chunks=(), citations=(citation,)))
    retriever = GraphRetriever(graph, t1, BrainGraphSettings())
    principal = Principal(id="u", groups=("g-eng",))
    result = asyncio.run(retriever.search("q", principal=principal))

    hops = [hop for gp in result.graph_paths for hop in gp.hops]
    llm_hops = [h for h in hops if h.edge_kind == "ABOUT" and h.obj.entity_key == "llmn"]
    assert llm_hops
    for hop in llm_hops:
        assert hop.assurance == "extracted"
        assert hop.obj.assurance == "extracted"
        assert hop.subject.assurance == "source"

    er_hops = [h for h in hops if h.edge_kind == "SAME_AS"]
    assert er_hops
    for hop in er_hops:
        assert hop.assurance == "source"


def test_b_assurance_label_set_is_single_sourced() -> None:
    """P3-1 proof (derive, don't transliterate): ``extraction.writer``'s
    ``ASSURANCE_VALUES`` IS (identity, not equality) the constants-leaf
    ``extraction.ASSURANCE_VALUES`` — a re-export, not a second definition —
    and ``read.py`` holds NO local duplicate at all. ``_assurance_of``
    accepts exactly the labels in that single set."""
    from ne.brain_graph import read as read_module
    from ne.brain_graph.extraction import ASSURANCE_VALUES
    from ne.brain_graph.extraction import writer as extraction_writer

    assert extraction_writer.ASSURANCE_VALUES is ASSURANCE_VALUES
    assert not hasattr(read_module, "_VALID_STAMPED_ASSURANCE")
    for label in ASSURANCE_VALUES:
        stamped = read_module._assurance_of({"source_system": "ne:llm", "assurance": label})
        assert stamped == label


# -- Layer (c) -----------------------------------------------------------------


class _RaisingExpander:
    def expand(self, principal: Principal | str) -> frozenset[str]:
        raise RuntimeError("expander boom")


def test_c_production_hook_invariant() -> None:
    labels = acl_match_labels(Principal(id="u", groups=("g",)), None)
    assert labels  # never empty
    assert "u" in labels  # always contains principal.id
    with pytest.raises(RuntimeError, match="boom"):
        acl_match_labels(Principal(id="u"), _RaisingExpander())


# -- Layer (d) -----------------------------------------------------------------


def _is_stub_body(func: ast.AsyncFunctionDef) -> bool:
    body = [
        n
        for n in func.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    ]
    return (
        len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and body[0].value.value is ...
    )


def test_d_cypher_call_sites_confined() -> None:
    offenders: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "cypher"
            ):
                rel = _rel(path)
                if rel not in _CYPHER_CALL_ALLOWLIST:
                    offenders.add(rel)
    assert not offenders, f"raw .cypher( call outside the allowlist: {offenders}"


def test_d_cypher_definitions_confined() -> None:
    definers: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "cypher"
                and not _is_stub_body(node)
            ):
                definers.add(_rel(path))
    assert definers == _CYPHER_DEF_ALLOWLIST, f"unexpected `def cypher` impls: {definers}"


# -- Layer (e): the sole-writer structural sweep (NE-T2X-3) --------------------
#
# Pins WHO in the repo may (1) hold the raw "ne:llm" literal, (2) reference the
# LLM_SOURCE_SYSTEM constant, and (3) actually STAMP source_system="ne:llm".
# Rule (3) is the headline sole-writer invariant — it makes design F2
# (truthful provenance) structural against FUTURE code, not just today's tree.
#
# Rule (3)'s detection surface is FIVE statically-recognizable forms (F1-F5;
# see `_files_stamping_llm_source_system`'s docstring for the full
# enumeration and the KNOWINGLY-OUT-OF-REACH forms it deliberately does NOT
# claim to catch). A prior round of this sweep recognised only F1 (dict-
# literal) and F2 (call keyword) — Argus review found it satisfiable by
# CONCEALMENT via F3, the store's own `d["source_system"] = "ne:llm"`
# stamping idiom, precisely the failure mode this sweep's docstring claims to
# guard against. F3 (store-target assignment, incl. AnnAssign/AugAssign), F4
# (pair-tuple), and F5 (adjacent-positional-args) were added to close that
# gap; each is proven non-vacuous below with its own planted RED case (a
# guardian without a demonstrated red case per claimed form is unproven — the
# precedent this sweep explicitly guards against is the repo's
# architecture-doc guardian, which passed its own mutation test for REMOVAL
# but was silently satisfiable by CONCEALMENT).
#
# _scan_roots' OWN boundary is stated, not implied: see its docstring below
# for what is scanned and what is deliberately excluded.


def _scan_roots() -> list[Path]:
    """The scan surface: ``packages/*/src`` + ``tools/*/src`` — the
    IMPORTABLE first-party package code, the only Python that can reach a
    ``GraphStore`` through the platform import graph.

    DELIBERATELY EXCLUDED (stated, not implied):

    - sidecar env dirs (``packages/ne-brain-graph/lightrag_env``,
      ``packages/ne-brain-graph/splink_env``,
      ``packages/ne-memory-graph/graphiti_env``, ``ne-eval``'s
      ``ragas_env``, etc.) — isolated, hash-pinned, out-of-process venvs
      that cannot import ``ne-brain-graph``.
    - ``deploy/airgap/scripts`` — operational scripts outside the package
      import graph.
    - ``tests/`` — this very file plants offenders here; scanning it would
      make every RED case below also a sweep failure.

    A NEW root category (e.g. a package shipped without a ``src/`` layout)
    would be UNSCANNED by this glob — adding a root category is a
    review-owned act, not something this sweep can protect itself against;
    ``test_e_scan_roots_nonvacuous`` below only guards against the glob
    silently yielding nothing at all (a typo making every sweep vacuously
    green), not against a category never being added here.
    """
    roots = sorted(_REPO.glob("packages/*/src")) + sorted(_REPO.glob("tools/*/src"))
    return [r for r in roots if r.is_dir()]


def _iter_py_files(roots: Sequence[Path]):
    for root in roots:
        yield from root.rglob("*.py")


def _rel_to(path: Path, base: Path) -> str:
    return path.resolve().relative_to(base.resolve()).as_posix()


def _docstring_expr_ids(tree: ast.AST) -> set[int]:
    """id()s of ``ast.Expr`` nodes that ARE a docstring: the first statement of
    a ``Module``/``ClassDef``/``FunctionDef``/``AsyncFunctionDef`` body, when
    that statement is a bare string constant."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0]))
    return ids


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _files_with_llm_literal(roots: Sequence[Path], *, base: Path = _REPO) -> set[str]:
    """Files containing a NON-DOCSTRING string constant containing ``ne:llm``
    (including an ``ast.JoinedStr`` f-string constant part)."""
    found: set[str] = set()
    for path in _iter_py_files(roots):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        docstring_ids = _docstring_expr_ids(tree)
        parents = _parent_map(tree)
        hit = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "ne:llm" not in node.value:
                    continue
                parent = parents.get(id(node))
                if isinstance(parent, ast.Expr) and id(parent) in docstring_ids:
                    continue  # excluded: this constant IS a docstring
                hit = True
                break
            if isinstance(node, ast.JoinedStr):
                for part in node.values:
                    if (
                        isinstance(part, ast.Constant)
                        and isinstance(part.value, str)
                        and "ne:llm" in part.value
                    ):
                        hit = True
                        break
                if hit:
                    break
        if hit:
            found.add(_rel_to(path, base))
    return found


def _files_referencing_llm_constant(roots: Sequence[Path], *, base: Path = _REPO) -> set[str]:
    """Files with an ``ast.Name``/``ast.Attribute``/``ImportFrom`` alias named
    ``LLM_SOURCE_SYSTEM``."""
    found: set[str] = set()
    for path in _iter_py_files(roots):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "LLM_SOURCE_SYSTEM":
                found.add(_rel_to(path, base))
                break
            if isinstance(node, ast.Attribute) and node.attr == "LLM_SOURCE_SYSTEM":
                found.add(_rel_to(path, base))
                break
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "LLM_SOURCE_SYSTEM" for alias in node.names
            ):
                found.add(_rel_to(path, base))
                break
    return found


def _is_llm_value(value: ast.AST) -> bool:
    if isinstance(value, ast.Constant) and value.value == "ne:llm":
        return True
    if isinstance(value, ast.Name) and value.id == "LLM_SOURCE_SYSTEM":
        return True
    return bool(isinstance(value, ast.Attribute) and value.attr == "LLM_SOURCE_SYSTEM")


def _is_source_system_key(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Constant) and expr.value == "source_system"


def _contains_llm_value(expr: ast.expr) -> bool:
    """True if ANY subexpression of ``expr`` is an ``_is_llm_value`` site —
    lets the value side of an assignment match whether it is a bare literal/
    name or a larger expression wrapping one."""
    return any(_is_llm_value(n) for n in ast.walk(expr))


def _targets_source_system(target: ast.expr) -> bool:
    """True if ``target`` (an assignment LHS) is a store-target keyed/named
    ``source_system``: ``ast.Subscript`` with a ``"source_system"`` slice —
    ``d["source_system"] = ...``, the codebase's own stored-prop mutation
    idiom (``extraction/writer.py``'s ``current["assurance"] = assurance``
    shares this exact shape) — or ``ast.Attribute`` with ``attr ==
    "source_system"`` (``obj.source_system = ...``). Recurses through
    ``Tuple``/``List``/``Starred`` targets so a tuple-unpacking assignment
    with ONE element targeting ``source_system`` is still caught."""
    if isinstance(target, ast.Subscript):
        return _is_source_system_key(target.slice)
    if isinstance(target, ast.Attribute):
        return target.attr == "source_system"
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_targets_source_system(elt) for elt in target.elts)
    if isinstance(target, ast.Starred):
        return _targets_source_system(target.value)
    return False


def _files_stamping_llm_source_system(roots: Sequence[Path], *, base: Path = _REPO) -> set[str]:
    """Files with a STAMPING site — a statically-recognizable Python
    expression that associates the value ``"ne:llm"``/``LLM_SOURCE_SYSTEM``
    with a ``source_system`` key or attribute. Five forms are DETECTED
    (each with its own planted RED test below — a guardian without a
    demonstrated red case per claimed form is unproven):

    F1 dict-literal            ``{"source_system": "ne:llm"}`` /
                                ``{"source_system": LLM_SOURCE_SYSTEM}``.
    F2 keyword-arg              ``f(source_system="ne:llm")`` — covers
                                ``dict(source_system=...)``,
                                ``store.upsert(source_system=...)``,
                                ``.update(source_system=...)``.
    F3 store-target assignment  ``d["source_system"] = "ne:llm"`` — THE
                                codebase's OWN stored-prop mutation idiom
                                (``extraction/writer.py``'s
                                ``current["assurance"] = assurance`` and this
                                suite's own ``edge["source_system"] =
                                "ne:llm"`` fixtures share this shape — the
                                exact concealment Argus proved invisible in
                                the prior round) — and ``obj.source_system =
                                ...``; also ``ast.AnnAssign``
                                (``d["source_system"]: str = ...``) and
                                ``ast.AugAssign`` (``d["source_system"] +=
                                ...``).
    F4 pair-tuple                ``("source_system", "ne:llm")`` /
                                ``["source_system", "ne:llm"]`` — covers
                                ``dict([(k, v)])``, ``.update([(k, v)])``.
    F5 adjacent-positional-args  ``f("source_system", "ne:llm", ...)`` —
                                covers ``setattr(obj, "source_system",
                                "ne:llm")``, ``obj.__setitem__(
                                "source_system", "ne:llm")``,
                                ``operator.setitem(obj, "source_system",
                                "ne:llm")``.

    KNOWINGLY OUT OF REACH (no data-flow analysis — static AST only; this
    boundary is declared, not implied):

    - computed VALUE strings (``"ne:" + "llm"``) or computed KEYS
      (``"source" + "_system"``) — the declared residual, proven by
      ``test_e_residual_computed_string_evasion_documented``.
    - intermediate-variable data flow WITHIN THE SAME FILE (``v = "ne:llm";
      d["source_system"] = v``) — F3's per-SITE precision misses this, BUT
      any such file still holds the literal itself or an import/alias of
      ``LLM_SOURCE_SYSTEM`` (including an ``import ... as`` alias, caught by
      the ``ast.alias`` check in ``_files_referencing_llm_constant``) SOMEWHERE
      in that same file, to PRODUCE that intermediate value — so rules 1/2
      (the file-level literal/constant-reference sweeps below) still force
      any such file into allowlist review. F3's per-site precision is BACKED
      by rules 1/2's per-file net, not a substitute for it, PROVIDED the
      value both originates AND is stamped in the same file.
    - a ``**mapping`` unpack of a NON-LITERAL mapping (``f(**cfg)`` where
      ``cfg`` is assembled elsewhere in the SAME file) — same intermediate-
      variable gap, same rule-1/2 backstop, same same-file proviso.
    - values sourced from OUTSIDE the stamping file entirely — a value read
      back off an existing store row, a config/env value, a function
      parameter, or another module's return value (``d["source_system"] =
      row["source_system"]``; ``new_node["source_system"] =
      fetched["source_system"]``) — carry NO literal ``"ne:llm"`` string and
      NO reference to ``LLM_SOURCE_SYSTEM`` anywhere in the file that does
      the stamping. THIS IS A GENUINELY DIFFERENT RESIDUAL from the
      same-file intermediate-variable case above: rules 1/2 do **NOT**
      backstop it, because the file performing the stamp holds neither the
      literal nor the constant name — only the file that originally
      produced the value (if any, and if it is even in this repo) might,
      and that file may itself do nothing more suspicious than read rows
      out of the store. Declared residual, proven by
      ``test_e_residual_externally_sourced_value_evasion_documented``; see
      also ``test_e_green_provenance_copy_is_not_flagged``, which pins the
      mechanical reason F3 does not fire on this shape (no ``_is_llm_value``
      site anywhere in the value expression) without claiming the shape is
      benign.
    - ``exec``/``getattr``-based indirection, or any other string-eval/
      reflection trick — outside AST's reach entirely.

    Given the above, this sweep has (at least) THREE declared residual
    classes, not one — computed-string forgery, same-file intermediate-
    variable production (backed by rules 1/2), and cross-file/externally-
    sourced values (NOT backed by rules 1/2). None of the three is silently
    assumed away: each is owned by human review of layer (d)'s call-site
    confinement plus the read-side ``_assurance_of`` fail-closed floor,
    which holds regardless of how a forged/copied ``source_system`` got
    onto a node. This sweep does not claim, and must not be read as
    claiming, TOTAL coverage of every way a file could come to write
    ``source_system="ne:llm"`` — only of the five statically-recognizable
    forms enumerated above.

    ``ast.Compare`` sites (``x == "ne:llm"``) are naturally excluded by every
    rule above — purge/gate filters COMPARE, they do not STAMP.
    """
    found: set[str] = set()
    for path in _iter_py_files(roots):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        hit = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=False):
                    if key is not None and _is_source_system_key(key) and _is_llm_value(value):
                        hit = True
                        break
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "source_system" and _is_llm_value(kw.value):
                        hit = True
                        break
                if not hit:
                    args = node.args
                    for i in range(len(args) - 1):
                        if _is_source_system_key(args[i]) and _is_llm_value(args[i + 1]):
                            hit = True
                            break
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if _targets_source_system(target) and _contains_llm_value(node.value):
                        hit = True
                        break
            elif isinstance(node, ast.AnnAssign):
                if (
                    node.value is not None
                    and _targets_source_system(node.target)
                    and _contains_llm_value(node.value)
                ):
                    hit = True
            elif isinstance(node, ast.AugAssign):
                if _targets_source_system(node.target) and _contains_llm_value(node.value):
                    hit = True
            elif isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 2:
                first, second = node.elts
                if _is_source_system_key(first) and _is_llm_value(second):
                    hit = True
            if hit:
                break
        if hit:
            found.add(_rel_to(path, base))
    return found


# NE-T2X-3 build-time correction to the design-brief's plan-time grep (per the
# dispatch instruction "GREP AT DISPATCH: do it yourself, do not trust this
# list"): ``cli.py``'s argparse ``help=`` text and its ``--confirm`` prompt
# string ("delete every ne:llm graph element...") independently hold the raw
# literal too. Verified separately (the stamping-set computation below never
# includes ``cli.py``): neither string is a ``Dict`` entry nor an
# ``ast.keyword`` ``source_system=`` site — it is a benign informational
# mention (help/print text), not a writer. Added here rather than silently
# dropped from the pin.
_LITERAL_ALLOWLIST = {
    "packages/ne-brain-graph/src/ne/brain_graph/cli.py",
    "packages/ne-brain-graph/src/ne/brain_graph/extraction/__init__.py",
    "packages/ne-brain-graph/src/ne/brain_graph/testing/memory.py",
    "packages/ne-brain-graph/src/ne/brain_graph/testing/kits/graph_store.py",
    "packages/ne-brain-graph/src/ne/brain_graph/age/store.py",
}

_CONSTANT_REF_ALLOWLIST = {
    "packages/ne-brain-graph/src/ne/brain_graph/extraction/__init__.py",
    "packages/ne-brain-graph/src/ne/brain_graph/extraction/identity.py",
    "packages/ne-brain-graph/src/ne/brain_graph/extraction/writer.py",
    "packages/ne-brain-graph/src/ne/brain_graph/extraction/apply.py",
    "packages/ne-brain-graph/src/ne/brain_graph/read.py",
}

# The headline sole-writer invariant. ``ExtractionWriter`` is the ONLY
# production writer. ``testing/kits/graph_store.py`` is a conformance-kit
# fixture builder (``_node``/``_edge``) that SEEDS a store-under-test to prove
# purge/trim contracts (the ``purge_llm_extracted`` DETACH-semantics battery)
# — it composes no floor fields into any production write path, so its
# presence here is test-scaffolding, not a second writer.
#
# ``extraction/apply.py`` is DELIBERATELY ABSENT even though it is a
# constant-reference site: it only ever COMPARES ``source_system`` (refcounting
# revert scans) and delegates every actual write to ``ExtractionWriter`` — a
# stamp appearing in ``apply.py`` would be a design change, not a refactor.
_STAMPING_ALLOWLIST = {
    "packages/ne-brain-graph/src/ne/brain_graph/extraction/writer.py",
    "packages/ne-brain-graph/src/ne/brain_graph/testing/kits/graph_store.py",
}

_SWEEP_GUARD = (
    "NE-T2X-3 sole-writer sweep: a new file entered this set. It is an "
    "unproven ne:llm writer until reviewed AND allowlisted here."
)


def test_e_llm_literal_confined() -> None:
    assert _files_with_llm_literal(_scan_roots()) == _LITERAL_ALLOWLIST, _SWEEP_GUARD


def test_e_llm_constant_reference_confined() -> None:
    assert _files_referencing_llm_constant(_scan_roots()) == _CONSTANT_REF_ALLOWLIST, _SWEEP_GUARD


def test_e_llm_stamping_sites_are_writer_and_conformance_kit_only() -> None:
    assert _files_stamping_llm_source_system(_scan_roots()) == _STAMPING_ALLOWLIST, _SWEEP_GUARD


def test_e_scan_roots_nonvacuous() -> None:
    """P3-2 vacuity guard: a glob typo, or a shrinking repo-layout change,
    could silently narrow ``_scan_roots`` to a subset of the real package
    tree — every allowlist pin above would still pass (there'd simply be
    nothing left to disagree with), so the vacuity would go unnoticed. This
    test catches it directly: the roots list must be non-empty and must
    include this very package's src tree."""
    roots = _scan_roots()
    assert roots, "_scan_roots yielded nothing — every layer-(e) sweep scans zero files"
    rel_roots = {_rel_to(r, _REPO) for r in roots}
    assert "packages/ne-brain-graph/src" in rel_roots


# -- Layer (e) RED cases: non-vacuity proof (planted offenders) ----------------


def _plant(tmp_path: Path, name: str, source: str) -> Path:
    pkg_src = tmp_path / "pkg" / "src"
    pkg_src.mkdir(parents=True, exist_ok=True)
    module = pkg_src / name
    module.write_text(source, encoding="utf-8")
    return pkg_src


def test_e_red_inline_literal_second_writer_is_caught(tmp_path: Path) -> None:
    src_root = _plant(
        tmp_path,
        "rogue.py",
        "async def w(store):\n"
        '    await store.upsert_nodes([{"source_system": "ne:llm", "key": "k", "acl": ["g"]}])\n',
    )
    roots = [src_root]
    assert "pkg/src/rogue.py" in _files_with_llm_literal(roots, base=tmp_path)
    assert "pkg/src/rogue.py" in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_red_constant_importing_second_writer_is_caught(tmp_path: Path) -> None:
    src_root = _plant(
        tmp_path,
        "rogue2.py",
        "from ne.brain_graph.extraction import LLM_SOURCE_SYSTEM\n\n\n"
        "def build():\n"
        '    return {"source_system": LLM_SOURCE_SYSTEM}\n',
    )
    roots = [src_root]
    assert "pkg/src/rogue2.py" in _files_referencing_llm_constant(roots, base=tmp_path)
    assert "pkg/src/rogue2.py" in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_red_keyword_arg_second_writer_is_caught(tmp_path: Path) -> None:
    """F2 proof, PREVIOUSLY UNPROVEN: the earlier RED case for the keyword
    form (``test_e_red_inline_literal_second_writer_is_caught``) planted a
    Dict literal alongside the keyword call, so it is ambiguous whether F1
    (dict-literal) or F2 (call keyword) is what actually caught it. This
    plants ONLY a bare keyword-arg call with no ``Dict`` node anywhere in the
    module — a stamping-set hit here can only be F2 doing the work (the
    literal-confinement sweep is expected to ALSO flag this file, since
    ``"ne:llm"`` is a raw string constant regardless of which AST shape it
    sits in — that overlap is fine, it is not what this test is pinning)."""
    src_root = _plant(
        tmp_path,
        "rogue3.py",
        "async def w(store):\n    await store.upsert(source_system='ne:llm')\n",
    )
    roots = [src_root]
    assert "pkg/src/rogue3.py" in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_red_subscript_assignment_house_idiom_literal_is_caught(tmp_path: Path) -> None:
    """THE Argus P1 case. ``node["source_system"] = "ne:llm"`` is THIS
    CODEBASE'S OWN idiom for mutating a stored-prop dict — identical in
    shape to ``extraction/writer.py``'s ``current["assurance"] = assurance``
    and to this very test suite's own ``edge["source_system"] = "ne:llm"``
    fixtures (see ``test_b_extracted_edge_trimmed_and_carries_assurance``
    et al.). Before F3 was added, a second writer using this exact idiom was
    INVISIBLE to the sweep — the precise concealment the layer-(e) docstring
    claims to guard against. This is the load-bearing RED case: F3 must
    catch it or the whole re-dispatch has not fixed anything."""
    src_root = _plant(
        tmp_path,
        "rogue4.py",
        "async def w(store):\n"
        "    node = {'key': 'k', 'acl': ['g']}\n"
        "    node['source_system'] = 'ne:llm'\n"
        "    await store.upsert_nodes([node])\n",
    )
    roots = [src_root]
    assert "pkg/src/rogue4.py" in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_red_subscript_assignment_constant_value_is_caught(tmp_path: Path) -> None:
    """The subscript idiom, stamped via the ``LLM_SOURCE_SYSTEM`` name rather
    than the raw literal — the other half of F3's value-side coverage."""
    src_root = _plant(
        tmp_path,
        "rogue5.py",
        "from ne.brain_graph.extraction import LLM_SOURCE_SYSTEM\n\n\n"
        "def w(node):\n    node['source_system'] = LLM_SOURCE_SYSTEM\n",
    )
    roots = [src_root]
    assert "pkg/src/rogue5.py" in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_red_attribute_assignment_is_caught(tmp_path: Path) -> None:
    """F3's attribute-target form: ``obj.source_system = "ne:llm"``."""
    src_root = _plant(
        tmp_path,
        "rogue6.py",
        "def w(obj):\n    obj.source_system = 'ne:llm'\n",
    )
    roots = [src_root]
    assert "pkg/src/rogue6.py" in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_red_annassign_subscript_is_caught(tmp_path: Path) -> None:
    """F3's ``AnnAssign`` statement-kind coverage: ``d["source_system"]: str
    = "ne:llm"``."""
    src_root = _plant(
        tmp_path,
        "rogue7.py",
        "def w(d):\n    d['source_system']: str = 'ne:llm'\n",
    )
    roots = [src_root]
    assert "pkg/src/rogue7.py" in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_red_augassign_subscript_is_caught(tmp_path: Path) -> None:
    """F3's ``AugAssign`` statement-kind coverage: ``d["source_system"] +=
    "ne:llm"``."""
    src_root = _plant(
        tmp_path,
        "rogue8.py",
        "def w(d):\n    d['source_system'] += 'ne:llm'\n",
    )
    roots = [src_root]
    assert "pkg/src/rogue8.py" in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_red_pair_tuple_update_is_caught(tmp_path: Path) -> None:
    """F4: the ``("source_system", "ne:llm")`` pair-tuple form, e.g.
    ``d.update([(\"source_system\", \"ne:llm\")])``."""
    src_root = _plant(
        tmp_path,
        "rogue9.py",
        "def w(d):\n    d.update([('source_system', 'ne:llm')])\n",
    )
    roots = [src_root]
    assert "pkg/src/rogue9.py" in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_red_setattr_adjacent_args_is_caught(tmp_path: Path) -> None:
    """F5: adjacent positional args, the ``setattr``/``__setitem__``-shaped
    call form."""
    src_root = _plant(
        tmp_path,
        "rogue10.py",
        "def w(obj):\n    setattr(obj, 'source_system', 'ne:llm')\n",
    )
    roots = [src_root]
    assert "pkg/src/rogue10.py" in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_green_comparison_filter_is_not_flagged(tmp_path: Path) -> None:
    """A purge/gate COMPARE idiom (``if row["source_system"] == "ne:llm":``)
    must never trip the STAMPING sweep — it reads, it does not write. (It
    legitimately DOES trip the literal sweep; only the stamping set is
    asserted here.)"""
    src_root = _plant(
        tmp_path,
        "green1.py",
        "def w(row):\n"
        "    if row['source_system'] == 'ne:llm':\n"
        "        return True\n"
        "    return False\n",
    )
    roots = [src_root]
    assert "pkg/src/green1.py" not in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_green_provenance_copy_is_not_flagged(tmp_path: Path) -> None:
    """``d["source_system"] = row["source_system"]`` targets
    ``source_system``, but the VALUE expression holds no ``ne:llm``
    literal/constant-reference anywhere in it, so F3 (which requires
    ``_contains_llm_value`` on the value side) mechanically does not fire.

    This is NOT a claim that the shape is benign (Argus P2, prior round:
    copying an existing element's provenance onto a NEW element IS a stamp
    in effect, whatever it looks like syntactically) — it is a pin of WHY
    the sweep's per-site rule does not trip here. The shape is exactly the
    externally-sourced-value residual declared in
    ``_files_stamping_llm_source_system``'s docstring and proven end-to-end
    by ``test_e_residual_externally_sourced_value_evasion_documented``
    below (which also asserts rules 1/2 do not backstop it)."""
    src_root = _plant(
        tmp_path,
        "green2.py",
        "def w(d, row):\n    d['source_system'] = row['source_system']\n",
    )
    roots = [src_root]
    assert "pkg/src/green2.py" not in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_green_other_key_subscript_is_not_flagged(tmp_path: Path) -> None:
    """``extraction/writer.py:222``'s exact shape —
    ``current["assurance"] = assurance`` — targets a DIFFERENT key
    (``assurance``, not ``source_system``) and must never trip this sweep.
    This is the guardrail against F3 over-matching every subscript
    assignment in the codebase."""
    src_root = _plant(
        tmp_path,
        "green3.py",
        "def w(current, assurance):\n    current['assurance'] = assurance\n",
    )
    roots = [src_root]
    assert "pkg/src/green3.py" not in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_red_split_concealment_still_trips_literal_sweep(tmp_path: Path) -> None:
    """The precedent trap: an earlier guardian in this repo (the
    architecture-doc one) passed its own mutation test for REMOVAL but was
    silently satisfiable by CONCEALMENT. Splitting the literal into a helper
    and the actual write into a second module does not evade rule 1: the
    literal-holding helper still trips the sweep, even though the module
    doing the actual store write (``writer2.py``, upserting a param-supplied
    dict with no literal of its own) is not itself caught by any rule — that
    residual gap is real and is what layer (e) DECLARES rather than hides
    (see ``test_e_residual_computed_string_evasion_documented``)."""
    pkg_src = tmp_path / "pkg" / "src"
    pkg_src.mkdir(parents=True, exist_ok=True)
    (pkg_src / "helper.py").write_text(
        'def label() -> str:\n    return "ne:llm"\n', encoding="utf-8"
    )
    (pkg_src / "writer2.py").write_text(
        "async def upsert(store, payload):\n    await store.upsert_nodes([payload])\n",
        encoding="utf-8",
    )
    roots = [pkg_src]
    assert "pkg/src/helper.py" in _files_with_llm_literal(roots, base=tmp_path)


def test_e_red_docstring_only_mention_is_not_flagged(tmp_path: Path) -> None:
    """Pins the docstring exclusion that keeps ne-flywheel's ``proposals.py``
    class/function docstrings (which discuss ``entity_key_for("ne:llm", ...)``
    purely in prose) out of every allowlist."""
    src_root = _plant(
        tmp_path,
        "docs_only.py",
        '"""This module discusses ne:llm in its docstring only, no code reference."""\n\n\n'
        "def noop() -> None:\n    return None\n",
    )
    roots = [src_root]
    assert "pkg/src/docs_only.py" not in _files_with_llm_literal(roots, base=tmp_path)
    assert "pkg/src/docs_only.py" not in _files_referencing_llm_constant(roots, base=tmp_path)
    assert "pkg/src/docs_only.py" not in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_residual_computed_string_evasion_documented(tmp_path: Path) -> None:
    """DECLARED RESIDUAL (stated honestly, not hidden): a computed/concatenated
    string (``"ne:" + "llm"``) never appears as a single string ``Constant``
    containing ``ne:llm``, so NONE of the three rules catch it. This is
    deliberate fraud owned by human review, not this sweep's job — raw-cypher
    writes are separately confined by layer (d), and fabricating
    BACKBONE-looking provenance (avoiding ``ne:llm`` entirely) is out of this
    sweep's scope, governed instead by the connector-sync ledger/content-hash
    discipline. The READ-side floor still holds against exactly this forgery:
    ``read.py``'s ``_assurance_of`` truth-table (STEP 2, design §5.2) makes
    any element whose STORED ``source_system`` equals ``"ne:llm"`` read as
    ``"extracted"`` unless legitimately ``"confirmed"`` — the write-side
    concealment does not buy a trusted read.
    """
    src_root = _plant(
        tmp_path,
        "sneaky.py",
        "async def w(store):\n"
        '    await store.upsert_nodes([{"source_system": "ne:" + "llm", "key": "k"}])\n',
    )
    roots = [src_root]
    assert "pkg/src/sneaky.py" not in _files_with_llm_literal(roots, base=tmp_path)
    assert "pkg/src/sneaky.py" not in _files_referencing_llm_constant(roots, base=tmp_path)
    assert "pkg/src/sneaky.py" not in _files_stamping_llm_source_system(roots, base=tmp_path)


def test_e_residual_externally_sourced_value_evasion_documented(tmp_path: Path) -> None:
    """DECLARED RESIDUAL (Argus P2, prior round — proven by a test, not just
    asserted in prose this time): a second writer that fetches an EXISTING
    node's row from the store and copies its ``acl``/``source_system`` onto
    a NEW node carries no ``"ne:llm"`` literal and no ``LLM_SOURCE_SYSTEM``
    reference anywhere in the file that does the stamping — the value comes
    from OUTSIDE this file entirely (a store row). Direct probe of the
    orchestrator-verified failure: this file yields empty literal,
    constant-reference, AND stamping sets, exactly as if it contained no
    ``source_system`` handling at all.

    UNLIKE the computed-string forgery above, rules 1/2 do NOT backstop this
    class — see ``_files_stamping_llm_source_system``'s docstring for why.
    The mitigation is the same two-part story as the computed-string case:
    layer (d)'s confinement of raw store/cypher call sites to a small,
    reviewed surface, plus the READ-side floor — ``read.py``'s
    ``_assurance_of`` truth-table reads ANY element whose STORED
    ``source_system`` equals ``"ne:llm"`` as ``"extracted"`` unless
    legitimately ``"confirmed"``, regardless of whether that ``source_system``
    value arrived via a literal, a constant reference, or a same-store copy
    like this one. A second writer using this shape still cannot forge a
    ``"confirmed"``/``"source"`` read.
    """
    src_root = _plant(
        tmp_path,
        "rekey.py",
        "async def rekey(store, key):\n"
        "    rows = await store.fetch_nodes([key])\n"
        "    src = rows[0]\n"
        "    new_node = {'key': 'nk', 'acl': src['acl']}\n"
        "    new_node['source_system'] = src['source_system']\n"
        "    await store.upsert_nodes([new_node])\n",
    )
    roots = [src_root]
    assert "pkg/src/rekey.py" not in _files_with_llm_literal(roots, base=tmp_path)
    assert "pkg/src/rekey.py" not in _files_referencing_llm_constant(roots, base=tmp_path)
    assert "pkg/src/rekey.py" not in _files_stamping_llm_source_system(roots, base=tmp_path)
