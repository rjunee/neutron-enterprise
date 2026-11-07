# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for ne-brain tests."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from qdrant_client import AsyncQdrantClient

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.brain.lineage import SqlAlchemyDocumentLineageStore
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.sparse import FastEmbedBM25Encoder
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.testing.qdrant_live import (
    OwnedQdrantCollections,
    qdrant_server_url,
    retry_pending_drops,
    sweep_named_server,
)
from ne.brain.write import (
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ProvenanceClass,
    ShapeContract,
    SqliteWriteQuarantine,
)
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.events import ensure_outbox_schema

# A valid 64-char lowercase-hex SHA-256 digest for direct construction.
HEX_HASH = "a" * 64

# Canonical write-path shape contracts used across the NE-023 test suite.
NOTE_CONTRACT = ShapeContract(
    name="ne.knowledge.note",
    version="1",
    json_schema={
        "type": "object",
        "required": ["content"],
        "properties": {
            "content": {"type": "string", "minLength": 1},
            "metadata": {"type": "object"},
        },
    },
)
STRICT_NOTE_CONTRACT = ShapeContract(
    name="ne.knowledge.strict",
    version="2",
    json_schema={
        "type": "object",
        "required": ["content", "metadata"],
        "properties": {
            "content": {"type": "string", "minLength": 1},
            "metadata": {
                "type": "object",
                "required": ["topic"],
                "properties": {"topic": {"type": "string", "minLength": 1}},
            },
        },
    },
)


class _DemoSettings(NeSettings):
    demo: int = 7


@pytest.fixture
def frozen_config() -> Iterator[str]:
    """Freeze a deterministic config_hash for the duration of a test."""
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


@dataclass
class WriteStack:
    """A fully-wired KnowledgeWrite stack over REAL components (no mock past the seam)."""

    writer: KnowledgeWriter
    store: QdrantChunkStore
    quarantine: SqliteWriteQuarantine
    engine: sa.Engine


@pytest.fixture
def write_stack(tmp_path: Path) -> Callable[..., WriteStack]:
    """Factory building a :class:`WriteStack` over :memory: Qdrant + real sqlite lanes."""

    def _build(
        *,
        contracts: Mapping[str, ShapeContract] | None = None,
        default_shape: str = NOTE_CONTRACT.name,
        actor: str = "user:curator@acme.com",
        default_provenance_class: ProvenanceClass = ProvenanceClass.CURATED,
        kpi_tag: str | None = None,
        lineage: SqlAlchemyDocumentLineageStore | None = None,
    ) -> WriteStack:
        if contracts is not None:
            resolved: dict[str, ShapeContract] = dict(contracts)
        else:
            resolved = {
                NOTE_CONTRACT.name: NOTE_CONTRACT,
                STRICT_NOTE_CONTRACT.name: STRICT_NOTE_CONTRACT,
            }
        client = AsyncQdrantClient(":memory:")
        store = QdrantChunkStore(
            client, f"kw_{uuid.uuid4().hex}", HashEmbedder(32), HashSparseEncoder()
        )
        asyncio.run(store.ensure_collection())
        quarantine = SqliteWriteQuarantine(tmp_path / f"q_{uuid.uuid4().hex}.db")
        engine = sa.create_engine("sqlite://")
        ensure_outbox_schema(engine)
        ensure_audit_schema(engine)
        writer = KnowledgeWriter(
            store,
            quarantine,
            resolved,
            default_shape=default_shape,
            actor=actor,
            default_provenance_class=default_provenance_class,
            audit_sink=PostgresAuditSink(engine),
            violation_sink=OutboxShapeViolationSink(engine),
            kpi_tag=kpi_tag,
            lineage=lineage,
        )
        return WriteStack(writer=writer, store=store, quarantine=quarantine, engine=engine)

    return _build


@pytest.fixture
def owned_qdrant_collections() -> Iterator[OwnedQdrantCollections]:
    """The ONLY handle onto a real Qdrant server, and the janitor for what it makes.

    Teardown runs in a ``finally``, so every collection the test named is dropped
    whether the test passed, failed or errored. The inline "delete at the end of
    the happy path" shape is what leaked 985 collections into the live store on
    2026-09-10 and took it to ``status: red`` on file-descriptor exhaustion; see
    ``ne.brain.testing.qdrant_live``.


    A delete that FAILS (or that the server rejects with ``False``) leaves the name
    in ``owner.pending`` and the owner registered for
    :func:`~ne.brain.testing.qdrant_live.retry_pending_drops`, which
    ``pytest_sessionfinish`` below drains — otherwise "kept for retry" would mean
    "dropped on the floor the moment this fixture goes out of scope".
    """
    url = qdrant_server_url()
    # Guaranteed by the module-level skip in every module that requests this.
    assert url is not None, "NE_QDRANT_URL is not set — this fixture must be gated"
    owner = OwnedQdrantCollections(url)
    try:
        yield owner
    finally:
        owner.drop_all()


def tei_url() -> str:
    """URL of the (possibly live-parked) real TEI embedding server."""
    return os.environ.get("NE_TEI_EMBED_URL", "http://localhost:8085")


def tei_reachable(url: str) -> bool:
    """Probe ``GET {url}/health``; returns False on any connection failure."""
    try:
        response = httpx.get(f"{url}/health", timeout=2.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session")
def bm25_encoder() -> Iterator[FastEmbedBM25Encoder]:
    """A shared FastEmbedBM25Encoder; skips if the BM25 model assets can't load.

    Assets are fetched from the HF hub on first use. If this build/CI env has
    no network path to the hub and nothing pre-cached, the model load fails
    and the dependent tests are skipped rather than failing (LIVE-PARKED).
    """
    encoder = FastEmbedBM25Encoder()
    try:
        from fastembed import SparseTextEmbedding

        encoder._model = SparseTextEmbedding(encoder.model_id)
    except Exception as exc:
        pytest.skip(f"LIVE-PARKED: fastembed BM25 model assets unavailable ({exc!r})")
    yield encoder


def pytest_sessionfinish(session: pytest.Session, exitstatus: object) -> None:
    """Retry the teardown that failed, then sweep what teardown never reached.

    ⛔ WITHOUT THIS HOOK BOTH HALVES OF THE JANITOR ARE DECORATIVE.

    ``OwnedQdrantCollections.drop_all`` keeps a name whose delete did not succeed
    — but the fixture that owned it is discarded one line later, so "kept for
    retry" meant "dropped on the floor". :func:`retry_pending_drops` is the retry:
    it re-attempts only names THIS process minted, so it is safe on a worker.

    ``sweep_named_server`` covers what a ``finally`` cannot reach at all — a
    SIGKILLed xdist worker, a cancelled CI job — and until now nothing outside its
    own unit test ever called it, so those leaks accumulated forever. That is how
    985 of them got into the live store. It visits EVERY server a test-consent
    variable names (both of them — ``NE_TEST_QDRANT_URL`` is where the promoted
    source-authority gate runs), and none that no variable names.

    ⚠️ THE SWEEP IS CONTROLLER-ONLY, AND THE GUARD IS LOAD-BEARING. It deletes
    every ``<sanctioned prefix>_<uuid4>`` on the named server, including ones a
    sibling worker is using RIGHT NOW. ``config.workerinput`` exists only in an
    xdist worker, and the controller's ``pytest_sessionfinish`` runs after every
    worker has exited — so this is the one moment at which no live collection can
    be swept out from under anybody.
    """
    leaked = retry_pending_drops()
    if hasattr(session.config, "workerinput"):
        return  # a worker must never bulk-sweep: its siblings are still running
    swept = sweep_named_server()
    _report_qdrant_teardown(session, leaked, swept)


def _report_qdrant_teardown(
    session: pytest.Session, leaked: tuple[str, ...], swept: list[str]
) -> None:
    """Put leaks on the TERMINAL. A WARNING log at session end is read by nobody."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    if swept:
        reporter.write_line(
            f"qdrant: swept {len(swept)} orphaned test collection(s) left by teardown "
            f"that never ran: {', '.join(sorted(swept))}"
        )
    if leaked:
        reporter.write_line(
            f"qdrant: {len(leaked)} collection(s) could NOT be dropped and are "
            f"LEAKED on the named server(s): {', '.join(sorted(leaked))}"
        )
