# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C12 leak assertions + default-identity pins for the extraction pathway."""

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
from ne.brain_graph import cli
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.extraction.job import (
    ExtractionChunk,
    ExtractionDisabledError,
    ExtractionJob,
    ExtractionModeNotImplementedError,
)
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.flywheel.proposals import SqlAlchemyProposalStore

_CFG = "d" * 64
_SENTINEL = "XYZZY-SENTINEL-NEVER-LEAKS"


@pytest.fixture(autouse=True)
def _frozen_config() -> Any:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(BrainGraphSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


class _ScanOnly:
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

    async def complete(self, request: CompletionRequest) -> Completion:
        return Completion(
            text=self._text, model_id="test", usage=Usage(prompt_tokens=0, completion_tokens=0)
        )

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class _ExplodingGateway:
    """Proves a refusal happens BEFORE any gateway call — ``complete`` must
    never be reached (Codex C1/C2 regression tests)."""

    async def complete(self, request: CompletionRequest) -> Completion:
        raise AssertionError("gateway.complete must never be called when the job refuses up front")

    def stream(self, request: CompletionRequest) -> Any:  # pragma: no cover
        raise NotImplementedError


def _engine() -> sa.Engine:
    from ne.flywheel.review import ensure_review_schema

    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ensure_transactional_sqlite(eng)
    ensure_review_schema(eng)
    return eng


def test_sentinel_chunk_text_never_leaks_into_audit_or_payload() -> None:
    store = InMemoryGraphStore()
    engine = _engine()
    text = json.dumps(
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
    chunk = ExtractionChunk(
        chunk_ref="c1", document_ref="d1", source_system="docs", text=_SENTINEL, acl=("g1",)
    )
    audit = _RecordingAudit()
    settings = BrainGraphSettings(
        enabled=True, extraction_enabled=True, extraction_sources=("docs",)
    )
    job = ExtractionJob(
        _ScanOnly(store),
        engine,
        settings,
        _ScriptedGateway(text),
        _StaticChunkSource({"docs": [chunk]}),
        audit_sink=audit,
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    report = asyncio.run(job.run())
    assert report.batches_built == 1

    assert len(audit.events) == 1
    for event in audit.events:
        assert _SENTINEL not in event.model_dump_json()

    for proposal in SqlAlchemyProposalStore(engine).read_proposals():
        assert _SENTINEL not in json.dumps(proposal.payload)


def test_exactly_one_graph_extraction_event_per_run() -> None:
    store = InMemoryGraphStore()
    engine = _engine()
    chunk1 = ExtractionChunk(
        chunk_ref="c1", document_ref="d1", source_system="docs", text="a", acl=("g1",)
    )
    chunk2 = ExtractionChunk(
        chunk_ref="c2", document_ref="d2", source_system="docs", text="b", acl=("g1",)
    )
    audit = _RecordingAudit()
    settings = BrainGraphSettings(
        enabled=True, extraction_enabled=True, extraction_sources=("docs",)
    )
    job = ExtractionJob(
        _ScanOnly(store),
        engine,
        settings,
        _ScriptedGateway('{"assertions": []}'),
        _StaticChunkSource({"docs": [chunk1, chunk2]}),
        audit_sink=audit,
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    asyncio.run(job.run())
    extraction_events = [e for e in audit.events if e.action == "graph.extraction"]
    assert len(extraction_events) == 1


def test_default_identity_cli_refuses_and_composition_never_called(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called = {"job": False, "purge": False}

    def _spy_job(settings: BrainGraphSettings) -> Any:
        called["job"] = True
        raise AssertionError(
            "compose_extraction_job must never be called when extraction_enabled=False"
        )

    def _spy_purge(settings: BrainGraphSettings) -> Any:
        called["purge"] = True
        raise AssertionError("compose_extraction_purge_context must never be called when disabled")

    monkeypatch.setattr(cli, "load_settings", lambda: BrainGraphSettings(enabled=True))
    monkeypatch.setattr(cli, "compose_extraction_job", _spy_job)
    monkeypatch.setattr(cli, "compose_extraction_purge_context", _spy_purge)

    assert cli.main(["extraction", "run"]) == 2
    assert "disabled" in capsys.readouterr().err
    assert cli.main(["extraction", "purge", "--confirm"]) == 2
    assert "disabled" in capsys.readouterr().err
    assert called == {"job": False, "purge": False}


def test_default_settings_are_disabled_with_no_eligible_sources() -> None:
    """Direct settings-shape pin, NOT a sweep: ``BrainGraphSettings()`` (all
    defaults) has ``extraction_enabled=False`` and no eligible sources.

    This test alone does NOT prove "no graph.extraction* event anywhere in
    the default test pass" — that stronger claim is unverifiable as a single
    unit test (it would require inspecting every OTHER test module's audit
    sink). What IS verified, jointly, by this module: (1) here, that the
    default settings shape is inert (disabled, empty sources); (2)
    ``test_default_identity_cli_refuses_and_composition_never_called``, that
    the CLI path refuses before ever invoking ``compose_extraction_job``/
    ``compose_extraction_purge_context`` when disabled, so no job — and
    therefore no ``graph.extraction*`` event — is ever constructed via that
    entrypoint. Every OTHER test in this file only emits an event when it
    explicitly sets ``extraction_enabled=True`` and drives a job itself
    (Argus P3: the previous name of this test overclaimed a suite-wide sweep
    its body did not perform)."""
    settings = BrainGraphSettings()
    assert settings.extraction_enabled is False
    assert settings.extraction_sources == ()


def test_job_run_refuses_when_extraction_disabled() -> None:
    """Codex C2: ``ExtractionJob`` is a public API — a caller that constructs
    it directly with ``extraction_enabled=False`` must get the SAME
    fail-closed refusal the CLI's own gate enforces (design §2.5: no gateway
    task, no proposal, no audit event, when the master gate is off), not a
    silent run over configured sources. Before this fix, only the CLI
    checked the flag; the job itself would happily scan, call the gateway,
    and emit an audit event as long as ``extraction_sources`` was non-empty."""
    store = InMemoryGraphStore()
    engine = _engine()
    chunk = ExtractionChunk(
        chunk_ref="c1", document_ref="d1", source_system="docs", text="x", acl=("g1",)
    )
    audit = _RecordingAudit()
    settings = BrainGraphSettings(
        enabled=True, extraction_enabled=False, extraction_sources=("docs",)
    )
    job = ExtractionJob(
        _ScanOnly(store),
        engine,
        settings,
        _ExplodingGateway(),
        _StaticChunkSource({"docs": [chunk]}),
        audit_sink=audit,
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    with pytest.raises(ExtractionDisabledError):
        asyncio.run(job.run())
    assert audit.events == []


def test_job_run_refuses_for_unimplemented_extraction_mode() -> None:
    """Codex C1 (NE-T2X-4 attestation semantics: this refusal fires exactly
    when NO host attests ``immediate_write_composed=True``). ``read_flagged``/
    ``autonomous`` promise an IMMEDIATE graph write (design §2.2) this job
    structurally cannot perform on its own (the T-ER-1 mirror: no
    write-capable store reachable from the job) — a direct caller (this test)
    passes no attestation, so the refusal still fires exactly as before
    NE-T2X-4. Before the original fix, selecting either mode still silently
    ran the ingest_review proposal-only workflow and reported that configured
    mode in ``ExtractionRunReport.mode`` — a deployment selecting either mode
    got materially different behavior than promised with no signal anything
    was wrong."""
    store = InMemoryGraphStore()
    engine = _engine()
    chunk = ExtractionChunk(
        chunk_ref="c1", document_ref="d1", source_system="docs", text="x", acl=("g1",)
    )
    audit = _RecordingAudit()
    settings = BrainGraphSettings(
        enabled=True,
        extraction_enabled=True,
        extraction_sources=("docs",),
        extraction_mode="read_flagged",
    )
    job = ExtractionJob(
        _ScanOnly(store),
        engine,
        settings,
        _ExplodingGateway(),
        _StaticChunkSource({"docs": [chunk]}),
        audit_sink=audit,
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    with pytest.raises(ExtractionModeNotImplementedError):
        asyncio.run(job.run())
    assert audit.events == []


def test_job_run_refuses_ingest_review_with_immediate_write_attestation() -> None:
    """NE-T2X-4 attestation semantics: mode 1 (``ingest_review``) has no
    immediate write — a caller passing ``immediate_write_composed=True`` under
    mode 1 is a composition bug (never a legitimate runtime condition), so it
    raises ``ValueError``, not the mode-not-implemented error. Fires BEFORE any
    work (no gateway call, no audit event)."""
    store = InMemoryGraphStore()
    engine = _engine()
    chunk = ExtractionChunk(
        chunk_ref="c1", document_ref="d1", source_system="docs", text="x", acl=("g1",)
    )
    audit = _RecordingAudit()
    settings = BrainGraphSettings(
        enabled=True, extraction_enabled=True, extraction_sources=("docs",)
    )  # extraction_mode defaults to 'ingest_review'
    job = ExtractionJob(
        _ScanOnly(store),
        engine,
        settings,
        _ExplodingGateway(),
        _StaticChunkSource({"docs": [chunk]}),
        audit_sink=audit,
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="composition bug"):
        asyncio.run(job.run(immediate_write_composed=True))
    assert audit.events == []
