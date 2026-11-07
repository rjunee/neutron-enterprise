# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint-A additive API surface (SPEC §11): fetch-by-id + ScheduledJob.job_id.

Asserts the two decided additive Protocol methods exist with the exact signatures
(``KnowledgeRead.retrieve`` principal-scoped, ``ChunkStore.get`` unscoped), the
``ChunkStore.query`` both-None ValueError contract is documented, and
``ScheduledJob.job_id`` is an optional field that keeps the model hashable.
"""

from __future__ import annotations

import inspect

from ne.contracts.knowledge import ChunkStore, KnowledgeRead
from ne.contracts.runtime import ScheduledJob, Scheduler


def test_knowledge_read_retrieve_signature() -> None:
    assert hasattr(KnowledgeRead, "retrieve")
    assert inspect.iscoroutinefunction(KnowledgeRead.retrieve)
    sig = inspect.signature(KnowledgeRead.retrieve)
    assert list(sig.parameters) == ["self", "chunk_ids", "principal"]
    assert sig.parameters["principal"].kind is inspect.Parameter.KEYWORD_ONLY
    # Module uses `from __future__ import annotations`, so annotations are strings.
    assert sig.return_annotation == "RetrievalResult"


def test_knowledge_read_docstring_mentions_search_and_retrieve() -> None:
    doc = KnowledgeRead.__doc__ or ""
    assert "search" in doc
    assert "retrieve" in doc


def test_chunk_store_get_signature() -> None:
    assert hasattr(ChunkStore, "get")
    assert inspect.iscoroutinefunction(ChunkStore.get)
    sig = inspect.signature(ChunkStore.get)
    assert list(sig.parameters) == ["self", "chunk_ids"]
    assert sig.return_annotation == "Sequence[Chunk]"


def test_chunk_store_query_documents_both_none_contract() -> None:
    doc = ChunkStore.query.__doc__ or ""
    assert "ValueError" in doc
    assert "both are None" in doc


def test_scheduled_job_id_optional_and_hashable() -> None:
    job = ScheduledJob(name="nightly", cron="0 0 * * *", agent="a")
    assert job.job_id is None
    assert isinstance(hash(job), int)
    job2 = ScheduledJob(name="nightly", cron="0 0 * * *", agent="a", job_id="x")
    assert job2.job_id == "x"
    assert isinstance(hash(job2), int)


def test_scheduler_docstring_mentions_job_id() -> None:
    assert "job_id" in (Scheduler.__doc__ or "")


def test_both_are_still_protocols() -> None:
    assert getattr(KnowledgeRead, "_is_protocol", False)
    assert getattr(ChunkStore, "_is_protocol", False)
