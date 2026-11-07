# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Determinism pins (design E-P1-1) — documenting the HONEST contrast with
``test_construction_determinism.py`` (T-CON-3): that lane's byte-determinism
holds because there is NO model in the loop (the same connector record always
maps to the same node). This lane's determinism lives in the content-addressed
PROPOSAL LEDGER, not in the model's output bytes: a re-run over the SAME
chunks with the SAME scripted output is store-idempotent (one proposal row,
stable across run ids); a re-run whose output varies ONLY within the WITHIN-
canonical-form noise band (confidence drift, an equivalent re-wording of a
property value) still hits the SAME proposal id (U1's documented lossy half);
a re-wording that changes the CANONICAL FORM of a display name (design §3.3's
deliberate fragmentation trade) mints a genuinely NEW proposal id.
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
from ne.brain_graph.extraction.job import ExtractionChunk, ExtractionJob, ExtractionRunReport
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.flywheel.proposals import SqlAlchemyProposalStore

_CFG = "c" * 64


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


class _NullAudit:
    async def append(self, event: Any) -> None:
        return None

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


def _settings() -> BrainGraphSettings:
    return BrainGraphSettings(enabled=True, extraction_enabled=True, extraction_sources=("docs",))


def _run_report(engine: sa.Engine, text: str) -> ExtractionRunReport:
    store = InMemoryGraphStore()
    chunk = ExtractionChunk(
        chunk_ref="c1", document_ref="d1", source_system="docs", text="chunk", acl=("g1",)
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
    return asyncio.run(job.run())


def _run(engine: sa.Engine, text: str) -> tuple[str, ...]:
    """Batches BUILT this run — the content-addressing pin (Argus P3-1: this
    is deliberately ``batch_proposal_ids``, not ``appended_proposal_ids``,
    because these determinism tests assert whether re-runs land on the SAME
    content-addressed batch id, independent of whether that batch's row
    already existed in the store)."""
    return _run_report(engine, text).batch_proposal_ids


def _entity_text(display: str, confidence: float, name: str | None = None) -> str:
    return json.dumps(
        {
            "assertions": [
                {
                    "kind": "new_entity",
                    "entity_class": "Account",
                    "display": display,
                    "confidence": confidence,
                    "properties": {"name": name if name is not None else display},
                }
            ]
        }
    )


def test_three_runs_same_output_are_store_idempotent() -> None:
    """Argus P3-1: ``batch_proposal_ids`` is the content-addressing pin —
    identical across all three runs, unchanged by this fix. But
    ``proposals_appended``/``appended_proposal_ids`` must tell the HONEST
    story: only run 1 actually appends a new row; runs 2 and 3 rebuild the
    SAME batch and find it already landed, so they append nothing."""
    engine = _engine()
    text = _entity_text("Globex Corp", 0.9)
    report1 = _run_report(engine, text)
    report2 = _run_report(engine, text)
    report3 = _run_report(engine, text)
    assert report1.batch_proposal_ids == report2.batch_proposal_ids == report3.batch_proposal_ids
    assert len(SqlAlchemyProposalStore(engine).read_proposals()) == 1

    assert report1.proposals_appended == 1
    assert report1.appended_proposal_ids == report1.batch_proposal_ids
    assert report2.proposals_appended == 0
    assert report2.appended_proposal_ids == ()
    assert report3.proposals_appended == 0
    assert report3.appended_proposal_ids == ()


def test_within_band_confidence_and_property_variance_keeps_same_id() -> None:
    engine = _engine()
    ids1 = _run(engine, _entity_text("Globex Corp", 0.90, name="Globex Corp"))
    ids2 = _run(engine, _entity_text("Globex Corp", 0.93, name="Globex Corporation Inc"))
    assert ids1 == ids2
    assert len(SqlAlchemyProposalStore(engine).read_proposals()) == 1


def test_canonical_form_changing_rewording_mints_a_new_id() -> None:
    """Design §3.3's deliberate trade: 'Globex Corp' vs 'Globex Corporation'
    survive casefold/whitespace collapse as DIFFERENT canonical forms, so a
    re-wording of the DISPLAY (which feeds entity_key, part of the content
    address) mints a genuinely new proposal id — healed later by Splink ER,
    not by this content-addressing scheme. Contrast:
    ``test_construction_determinism.py`` has no such variance path at all,
    because there is no model output in that lane's loop.
    """
    engine = _engine()
    ids1 = _run(engine, _entity_text("Globex Corp", 0.9))
    ids2 = _run(engine, _entity_text("Globex Corporation", 0.9))
    assert ids1 != ids2
    assert len(SqlAlchemyProposalStore(engine).read_proposals()) == 2
