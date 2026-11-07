# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ne.contracts.types import Chunk, Principal, RetrievalResult, ScoredChunk
from ne.eval.config import EvalSettings
from ne.eval.golden import GoldenItem, GoldenSet
from ne.eval.route_a import run_route_a
from ne.eval.store import EvalRunStore, ensure_eval_schema
from ne.eval.thresholds import ThresholdSpec


class _KnowledgeRead:
    def __init__(self) -> None:
        self.seen: list[Principal] = []

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        del query, top_k, filters
        self.seen.append(principal)
        chunk = Chunk(
            chunk_id="chunk-1",
            document_id="doc-issued",
            text="The served result is grounded.",
            acl=(principal.id,),
        )
        return RetrievalResult(chunks=(ScoredChunk(chunk=chunk, score=1.0),), citations=())

    async def retrieve(self, chunk_ids, *, principal: Principal):  # pragma: no cover - protocol
        raise AssertionError("Route A must use the composed search path")


def _store() -> EvalRunStore:
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ensure_eval_schema(engine)
    return EvalRunStore(engine)


def test_route_a_uses_served_retrieval_and_persists_boot_config_hash() -> None:
    read = _KnowledgeRead()
    store = _store()
    settings = EvalSettings(
        metrics=["recall_at_10"],
        thresholds=[ThresholdSpec(metric="recall_at_10", threshold=1.0)],
        top_k=10,
    )
    golden = GoldenSet(
        items=(
            GoldenItem(
                id="route-a-1",
                question="What was served?",
                reference_answer="The served result is grounded.",
                relevant_document_ids=("doc-issued",),
            ),
        )
    )

    result = asyncio.run(
        run_route_a(
            golden=golden,
            knowledge_read=read,
            principal=Principal(id="user:eval", groups=("group:finance",)),
            settings=settings,
            store=store,
            config_hash="boot-config-hash",
            suite_name="route-a-demo",
        )
    )

    assert result.passed is True
    assert read.seen == [Principal(id="user:eval", groups=("group:finance",))]
    [persisted] = store.list_runs(suite="route-a-demo")
    assert persisted.config_hash == "boot-config-hash"
    assert persisted.metrics == {"recall_at_10": 1.0}
    assert persisted.judge_model_id == "ne-lexical-judge-v1"
    assert persisted.judge_family == "heuristic-lexical"
