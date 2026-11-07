# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Mutation-verified pin for the module-docstring DECISION in ``job.py``
(``job.py:65-97``): ``links_auto`` is counted, never queued, never written,
in EVERY extraction mode. Before this module the ONLY thing enforcing that
decision was the comment above the ``return`` in ``_handle_link``'s
auto-band branch — Argus (NE-BRAIN-TIER2-AUTOLINK follow-up) reproduced that
deleting that single ``return`` left the full two-package suite (829 tests)
byte-identical green. This module closes that gap with mutation-verified
assertions, driven through the REAL ``ExtractionJob.run()`` mode gates for
all three ``extraction_mode`` literals, so a leak of an auto-band link into
the review queue — a review-gated proposal for a thing DECISION-3 (§3.6)
says the graph cannot even represent — turns red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ne.audit import ensure_transactional_sqlite
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.extraction.job import ExtractionChunk, ExtractionJob
from ne.brain_graph.extraction.prompt import example_triple
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.flywheel.proposals import GraphExtractionAssertionKind, SqlAlchemyProposalStore

_CFG = "e" * 64


@pytest.fixture(autouse=True)
def _frozen_config() -> Any:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(BrainGraphSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


class _StaticScanStore:
    """Scan-only (T-ER-1 mirror): a single, fixed candidate vocabulary."""

    def __init__(self, nodes: Sequence[Mapping[str, Any]]) -> None:
        self._nodes = nodes

    async def scan_nodes(self, entity_class: str | None = None) -> Sequence[Mapping[str, Any]]:
        return self._nodes


class _StaticChunkSource:
    def __init__(self, by_source: dict[str, list[ExtractionChunk]]) -> None:
        self._by_source = by_source

    async def chunks(self, source_system: str) -> Sequence[ExtractionChunk]:
        return self._by_source.get(source_system, [])


class _NullAudit:
    async def append(self, event: Any) -> None:
        return None

    async def verify(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


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


def _engine() -> sa.Engine:
    from ne.flywheel.review import ensure_review_schema

    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ensure_transactional_sqlite(eng)
    ensure_review_schema(eng)
    return eng


_Mode = Literal["ingest_review", "read_flagged", "autonomous"]


def _settings(mode: _Mode) -> BrainGraphSettings:
    # extraction_risk_accepted is only load-bearing for mode=="autonomous" (its
    # validator demands it there); passing it explicitly for every mode (rather
    # than via a **kwargs merge) keeps this call fully keyword-typed for pyright.
    return BrainGraphSettings(
        enabled=True,
        extraction_enabled=True,
        extraction_sources=("docs",),
        extraction_min_confidence=0.5,
        extraction_auto_link_threshold=0.9,
        extraction_mode=mode,
        extraction_risk_accepted=(mode == "autonomous"),
    )


def _candidates() -> list[dict[str, Any]]:
    entity_class = example_triple().object_class
    return [
        {"key": "existing-key-1", "entity_class": entity_class, "display_name": "Existing One"},
        {"key": "existing-key-2", "entity_class": entity_class, "display_name": "Existing Two"},
    ]


def _chunk() -> ExtractionChunk:
    return ExtractionChunk(
        chunk_ref="c1",
        document_ref="d1",
        source_system="docs",
        text="chunk text",
        acl=("eng",),
    )


def _run(mode: _Mode, text: str) -> Any:
    engine = _engine()
    job = ExtractionJob(
        _StaticScanStore(_candidates()),
        engine,
        _settings(mode),
        _ScriptedGateway(text),
        _StaticChunkSource({"docs": [_chunk()]}),
        audit_sink=_NullAudit(),
        config_hash=_CFG,
        clock=lambda: datetime(2026, 7, 30, tzinfo=UTC),
    )
    if mode == "ingest_review":
        report = asyncio.run(job.run())
    else:
        report = asyncio.run(job.run(immediate_write_composed=True))
    return report, engine


# -- the load-bearing guard ---------------------------------------------------


@pytest.mark.parametrize("mode", ["ingest_review", "read_flagged", "autonomous"])
def test_auto_band_link_is_counted_and_never_queued(mode: _Mode) -> None:
    text = json.dumps(
        {
            "assertions": [
                {"kind": "link", "entity_key": "existing-key-1", "confidence": 0.95},
                {"kind": "link", "entity_key": "existing-key-2", "confidence": 0.7},
            ]
        }
    )
    report, engine = _run(mode, text)

    assert report.mode == mode
    assert report.mentions == 2
    assert report.links_auto == 1
    assert report.discarded_below_threshold == 0
    assert report.quarantined_invalid == 0
    assert report.batches_built == 1

    proposals = SqlAlchemyProposalStore(engine).read_proposals()
    assert len(proposals) == 1
    stored = proposals[0].payload["assertions"]
    # Exact-set equality, not containment (vacuous-guards V1): the
    # auto-band link (existing-key-1) must be ABSENT, not merely "the
    # below-band link is present among others".
    assert [(a["kind"], a["entity_key"]) for a in stored] == [
        (GraphExtractionAssertionKind.LINK.value, "existing-key-2")
    ]
    assert stored[0]["confidence"] == pytest.approx(0.7)


def test_confidence_exactly_at_auto_threshold_is_auto_band() -> None:
    text = json.dumps(
        {"assertions": [{"kind": "link", "entity_key": "existing-key-1", "confidence": 0.9}]}
    )
    report, engine = _run("ingest_review", text)

    assert report.mentions == 1
    assert report.links_auto == 1
    assert report.discarded_below_threshold == 0
    assert report.quarantined_invalid == 0
    assert report.batches_built == 0

    assert SqlAlchemyProposalStore(engine).read_proposals() == []


def test_below_band_link_is_still_queued() -> None:
    """The over-block CONTROL: the rule is 'AUTO-BAND links are dropped', not
    'links are dropped'. A 0.7 confidence link is >= min_confidence (0.5) and
    < the auto threshold (0.9), so it must still be queued as a review-gated
    LINK proposal. A guard that stayed green whether or not ALL links were
    dropped would forbid this shipped, review-gated path."""
    text = json.dumps(
        {"assertions": [{"kind": "link", "entity_key": "existing-key-2", "confidence": 0.7}]}
    )
    report, engine = _run("ingest_review", text)

    assert report.links_auto == 0
    assert report.batches_built == 1

    proposals = SqlAlchemyProposalStore(engine).read_proposals()
    stored = proposals[0].payload["assertions"]
    assert [(a["kind"], a["entity_key"]) for a in stored] == [
        (GraphExtractionAssertionKind.LINK.value, "existing-key-2")
    ]
