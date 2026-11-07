# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T-ER-1 structural mirror for mode-1 extraction, plus the B-P1-4 gateway-routing
no-swallow pair.

A full run over a scan-only store leaves the graph byte-identical and mutates
ONLY ``ne_fix_proposals`` (idempotently, content-addressed). The gateway pair
uses a LOCAL faithful ``TaskRoutingGateway`` double mirroring
``packages/ne-api/tests/conftest.py:216-248`` — this package must NOT gain an
``ne-gateway`` dependency, so ``UnroutedTaskError`` here is a LOCAL stand-in for
the real ``ne.gateway.errors.UnknownTaskError``; the real error type and the
``build_platform`` boot fail-loud land in NE-T2X-4 (design §9). This unit only
pins the JOB-SIDE half: an unrouted task must propagate, never be swallowed,
and must leave zero proposals appended.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ne.audit import ensure_transactional_sqlite
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.extraction.job import ExtractionChunk, ExtractionJob
from ne.brain_graph.testing.fixtures import make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.flywheel.proposals import SqlAlchemyProposalStore

_CFG = "b" * 64


@pytest.fixture(autouse=True)
def _frozen_config() -> Any:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(BrainGraphSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


class _ScanOnly:
    """A structurally scan-ONLY view — no upsert/delete reachable (T-ER-1)."""

    def __init__(self, inner: InMemoryGraphStore) -> None:
        self._inner = inner

    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Any]:
        return await self._inner.scan_nodes(entity_class)


class _StaticChunkSource:
    def __init__(self, by_source: dict[str, list[ExtractionChunk]]) -> None:
        self._by_source = by_source

    async def chunks(self, source_system: str) -> Sequence[ExtractionChunk]:
        return self._by_source.get(source_system, [])


class _ScriptedGateway:
    def __init__(self, text: str) -> None:
        self._text = text
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        return Completion(
            text=self._text, model_id="test", usage=Usage(prompt_tokens=0, completion_tokens=0)
        )

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


class _NullAudit:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def append(self, event: Any) -> None:
        self.actions.append(getattr(event, "action", ""))

    async def verify(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _engine() -> sa.Engine:
    from ne.flywheel.review import ensure_review_schema

    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ensure_transactional_sqlite(eng)
    ensure_review_schema(eng)
    return eng


def _settings(**overrides: Any) -> BrainGraphSettings:
    base: dict[str, Any] = dict(enabled=True, extraction_enabled=True, extraction_sources=("docs",))
    base.update(overrides)
    return BrainGraphSettings(**base)


def test_mode1_job_leaves_graph_byte_identical_and_is_idempotent() -> None:
    store = InMemoryGraphStore()
    asyncio.run(store.upsert_nodes([make_node("acct1", "Account", ("g1",), display_name="Acme")]))
    before_nodes = store.node_count()
    before_edges = store.edge_count()
    engine = _engine()
    text = json.dumps(
        {
            "assertions": [
                {
                    "kind": "new_entity",
                    "entity_class": "Account",
                    "display": "Globex",
                    "confidence": 0.9,
                    "properties": {"name": "Globex"},
                }
            ]
        }
    )
    chunk = ExtractionChunk(
        chunk_ref="c1", document_ref="d1", source_system="docs", text="chunk text", acl=("g1",)
    )
    job = ExtractionJob(
        _ScanOnly(store),  # structurally scan-only: no graph write reachable
        engine,
        _settings(),
        _ScriptedGateway(text),
        _StaticChunkSource({"docs": [chunk]}),
        audit_sink=_NullAudit(),
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )

    report = asyncio.run(job.run())
    assert report.batches_built == 1
    assert report.proposals_appended == 1
    # Graph is byte-identical — the job never touched it.
    assert store.node_count() == before_nodes
    assert store.edge_count() == before_edges
    proposals = SqlAlchemyProposalStore(engine).read_proposals()
    assert len(proposals) == 1

    # Re-run: content-addressed proposal id -> idempotent append (no new rows).
    # batch_proposal_ids stays constant (same batches BUILT) but nothing new
    # LANDS — counter honesty (Argus P3-1).
    again = asyncio.run(job.run())
    assert again.batch_proposal_ids == report.batch_proposal_ids
    assert again.appended_proposal_ids == ()
    assert again.proposals_appended == 0
    assert len(SqlAlchemyProposalStore(engine).read_proposals()) == 1


def test_mode1_job_grep_has_no_upsert_or_delete_identifier() -> None:
    """Structural pin: ``extraction/job.py`` never mentions an upsert/delete/purge
    identifier anywhere in its source — the T-ER-1 mirror, grep-verified."""
    import ne.brain_graph.extraction.job as job_module

    with open(job_module.__file__, encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("upsert_nodes", "upsert_edges", "delete_edge", "purge_llm_extracted"):
        assert forbidden not in source


class UnroutedTaskError(Exception):
    """LOCAL stand-in for ``ne.gateway.errors.UnknownTaskError`` (see module
    docstring) — ne-brain-graph must not gain an ne-gateway dependency."""


class _TaskRoutingGateway:
    """Mirrors ``packages/ne-api/tests/conftest.py::TaskRoutingGateway`` locally."""

    def __init__(self, inner: Any, tasks: dict[str, Any]) -> None:
        self._inner = inner
        self._tasks = dict(tasks)

    async def complete(self, request: CompletionRequest) -> Completion:
        if request.task not in self._tasks:
            raise UnroutedTaskError(request.task)
        return await self._inner.complete(request)

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


def test_unrouted_task_propagates_and_appends_nothing() -> None:
    store = InMemoryGraphStore()
    engine = _engine()
    chunk = ExtractionChunk(
        chunk_ref="c1", document_ref="d1", source_system="docs", text="x", acl=("g1",)
    )
    gateway = _TaskRoutingGateway(
        _ScriptedGateway('{"assertions": []}'), tasks={}
    )  # "graph-extract" absent
    job = ExtractionJob(
        _ScanOnly(store),
        engine,
        _settings(),
        gateway,
        _StaticChunkSource({"docs": [chunk]}),
        audit_sink=_NullAudit(),
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    with pytest.raises(UnroutedTaskError):
        asyncio.run(job.run())
    assert SqlAlchemyProposalStore(engine).read_proposals() == []


def test_edge_to_below_threshold_entity_is_quarantined_not_stranded() -> None:
    """Argus P1 regression. A below-threshold ``new_entity`` is discarded (never
    queued into the batch) — an ``new_edge`` in the SAME response naming it as
    an endpoint must therefore be quarantined too, not survive referencing a
    key no assertion will ever create. Ordinary, non-adversarial model output:
    edge confidence routinely differs from either endpoint's own confidence."""
    store = InMemoryGraphStore()
    engine = _engine()
    chunk = ExtractionChunk(
        chunk_ref="c1", document_ref="d1", source_system="docs", text="x", acl=("g1",)
    )
    text = json.dumps(
        {
            "assertions": [
                # Schema-valid, but below threshold (default
                # extraction_min_confidence=0.7) -> discarded, not quarantined.
                {
                    "kind": "new_entity",
                    "entity_class": "Person",
                    "display": "Jane Doe",
                    "confidence": 0.5,
                    "properties": {
                        "given_name": "Jane",
                        "family_name": "Doe",
                        "email": "jane@example.com",
                    },
                },
                {
                    "kind": "new_entity",
                    "entity_class": "Account",
                    "display": "MegaCorp",
                    "confidence": 0.95,
                    "properties": {"name": "MegaCorp"},
                },
                # References the dropped entity as an endpoint -> must be
                # quarantined, never batched (it would otherwise permanently
                # jam apply_extraction with zero writes for the WHOLE batch).
                {
                    "kind": "new_edge",
                    "subject_key": "Jane Doe",
                    "subject_class": "Person",
                    "edge_kind": "WORKS_AT",
                    "object_key": "MegaCorp",
                    "object_class": "Account",
                    "confidence": 0.95,
                },
            ]
        }
    )
    job = ExtractionJob(
        _ScanOnly(store),
        engine,
        _settings(),
        _ScriptedGateway(text),
        _StaticChunkSource({"docs": [chunk]}),
        audit_sink=_NullAudit(),
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    report = asyncio.run(job.run())
    assert report.mentions == 3
    assert report.discarded_below_threshold == 1  # Jane Doe
    assert report.quarantined_invalid == 1  # the edge, endpoint dropped
    assert report.batches_built == 1

    proposals = SqlAlchemyProposalStore(engine).read_proposals()
    assert len(proposals) == 1
    kinds = {a["kind"] for a in proposals[0].payload["assertions"]}
    assert kinds == {"new_entity"}  # MegaCorp only; no dangling new_edge


def test_routed_task_completes() -> None:
    store = InMemoryGraphStore()
    engine = _engine()
    chunk = ExtractionChunk(
        chunk_ref="c1", document_ref="d1", source_system="docs", text="x", acl=("g1",)
    )
    gateway = _TaskRoutingGateway(
        _ScriptedGateway(
            json.dumps(
                {
                    "assertions": [
                        {
                            "kind": "new_entity",
                            "entity_class": "Account",
                            "display": "Acme",
                            "confidence": 0.9,
                            "properties": {"name": "Acme"},
                        }
                    ]
                }
            )
        ),
        tasks={"graph-extract": object()},
    )
    job = ExtractionJob(
        _ScanOnly(store),
        engine,
        _settings(),
        gateway,
        _StaticChunkSource({"docs": [chunk]}),
        audit_sink=_NullAudit(),
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    report = asyncio.run(job.run())
    assert report.batches_built == 1
