# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Answer Ledger orchestration over the composed retrieval, lineage, and model seams."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import anyio.to_thread

from ne.brain.answer_ledger import (
    AnswerEvent,
    AnswerQuestion,
    CorpusChunkStamp,
    ReaskSchedule,
    SqlAlchemyAnswerLedgerStore,
)
from ne.brain.current_source_filter import authority_stamp_from_chunk
from ne.brain.lineage import SqlAlchemyDocumentLineageStore
from ne.brain.pipeline import RetrievalPipeline
from ne.brain.retriever import HybridRetriever
from ne.brain.trimming import GroupExpander, acl_match_labels
from ne.contracts.frozen import FrozenDict
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.models import CompletionRequest, ModelGateway
from ne.contracts.types import ChatMessage, Chunk, Lane, Principal
from ne.events import new_audit_event
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import run_principal_scope, run_trace_scope
from ne.schema import AuditOutcome

_SYSTEM = (
    "Answer the pinned business question using only the served evidence below. "
    "If the evidence does not answer it, say so plainly. Do not invent facts."
)


class AnswerLedgerEvidenceUnavailableError(RuntimeError):
    """No complete, versioned evidence set can truthfully support an answer event."""


class AnswerLedgerService:
    """One principal-owned question, re-asked through the composed C3/C7 seams."""

    def __init__(
        self,
        *,
        store: SqlAlchemyAnswerLedgerStore,
        retriever: KnowledgeRead,
        lineage: SqlAlchemyDocumentLineageStore,
        gateway: ModelGateway,
        sink: AuditSink,
        expander: GroupExpander,
        runtime_settings: RuntimeSettings,
        config_hash: str,
        top_k: int,
    ) -> None:
        self._store = store
        self._retriever = retriever
        self._lineage = lineage
        self._gateway = gateway
        self._sink = sink
        self._expander = expander
        self._runtime = runtime_settings
        self._config_hash = config_hash
        self._top_k = top_k

    async def pin(self, principal: Principal, question: str) -> AnswerQuestion:
        pinned = await anyio.to_thread.run_sync(self._store.pin, principal.id, question)
        try:
            await self._sink.append(
                new_audit_event(
                    action="answer_ledger.pin",
                    resource=f"answer-ledger://question/{pinned.question_id}",
                    outcome=AuditOutcome.success,
                    actor=principal.id,
                    detail=json.dumps({"question_id": pinned.question_id}, sort_keys=True),
                )
            )
        except Exception:
            await anyio.to_thread.run_sync(self._store.unpin, principal.id, pinned.question_id)
            raise
        return pinned

    async def schedule(
        self, principal: Principal, schedule: ReaskSchedule | None
    ) -> AnswerQuestion:
        """Opt the owner's pinned question into or out of scheduled re-asks."""

        previous = await anyio.to_thread.run_sync(self._store.question, principal.id)
        if previous is None:
            from ne.brain.answer_ledger import AnswerQuestionNotFoundError

            raise AnswerQuestionNotFoundError("pinned question not found")
        updated = await anyio.to_thread.run_sync(
            self._store.set_reask_schedule, principal.id, schedule
        )
        try:
            await self._sink.append(
                new_audit_event(
                    action="answer_ledger.schedule",
                    resource=f"answer-ledger://question/{updated.question_id}",
                    outcome=AuditOutcome.success,
                    actor=principal.id,
                    detail=json.dumps(
                        {"question_id": updated.question_id, "schedule": schedule},
                        sort_keys=True,
                    ),
                )
            )
        except Exception:
            await anyio.to_thread.run_sync(self._store.restore_reask_schedule, previous)
            raise
        return updated

    async def reask(self, principal: Principal, question: AnswerQuestion) -> AnswerEvent:
        """Resolve all evidence and the model result before creating a durable row."""

        with run_trace_scope() as trace_id, run_principal_scope(principal.id):
            found = await self._retriever.search(
                question.question, principal=principal, top_k=self._top_k
            )
            requested_ids = tuple(dict.fromkeys(row.chunk.chunk_id for row in found.chunks))
            if not requested_ids:
                raise AnswerLedgerEvidenceUnavailableError("no evidence was served")

            exact = await self._retrieve_exact(requested_ids, principal, trace_id)
            by_id = {row.chunk.chunk_id: row.chunk for row in exact.chunks}
            if set(by_id) != set(requested_ids):
                raise AnswerLedgerEvidenceUnavailableError(
                    "the served evidence changed before it could be pinned"
                )
            chunks = tuple(by_id[chunk_id] for chunk_id in requested_ids)
            stamp = await self._stamp(chunks, principal)
            completion = await self._gateway.complete(
                CompletionRequest(
                    messages=(
                        ChatMessage(role="system", content=_SYSTEM),
                        ChatMessage(
                            role="user",
                            content=self._prompt(question.question, chunks),
                        ),
                    ),
                    task=self._runtime.task,
                    lane=Lane(self._runtime.lane) if self._runtime.lane else None,
                    params=(
                        FrozenDict({"kpi_tag": self._runtime.kpi_tag})
                        if self._runtime.kpi_tag is not None
                        else FrozenDict()
                    ),
                    trace_id=trace_id,
                    principal_id=principal.id,
                )
            )

        event = await anyio.to_thread.run_sync(
            lambda: self._store.record(
                question=question,
                answer=completion.text,
                corpus_stamp=stamp,
                config_hash=self._config_hash,
                served_model_id=completion.model_id,
                trace_id=trace_id,
            )
        )
        try:
            await self._sink.append(
                new_audit_event(
                    action="answer_ledger.reask",
                    resource=f"answer-ledger://event/{event.event_id}",
                    outcome=AuditOutcome.success,
                    actor=principal.id,
                    detail=json.dumps(
                        {
                            "event_id": event.event_id,
                            "question_id": question.question_id,
                            "served_chunks": len(stamp),
                        },
                        sort_keys=True,
                    ),
                    trace_id=trace_id,
                )
            )
        except Exception:
            await anyio.to_thread.run_sync(self._store.remove_event, principal.id, event.event_id)
            raise
        return event

    async def _retrieve_exact(self, chunk_ids: Sequence[str], principal: Principal, trace_id: str):
        if isinstance(self._retriever, HybridRetriever | RetrievalPipeline):
            return await self._retriever.retrieve(chunk_ids, principal=principal, trace_id=trace_id)
        return await self._retriever.retrieve(chunk_ids, principal=principal)

    async def _stamp(
        self, chunks: Sequence[Chunk], principal: Principal
    ) -> tuple[CorpusChunkStamp, ...]:
        labels = acl_match_labels(principal, self._expander)
        versions: dict[str, int] = {}
        by_document: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            by_document.setdefault(chunk.document_id, []).append(chunk)
        for document_id, document_chunks in by_document.items():
            journey = await anyio.to_thread.run_sync(self._lineage.journey, document_id, labels)
            current = (
                next((version for version in journey.versions if version.status == "current"), None)
                if journey is not None
                else None
            )
            if current is None:
                raise AnswerLedgerEvidenceUnavailableError(
                    f"document version unavailable for served document {document_id}"
                )
            # BD-3 records one governed connector record as one document
            # version. Match those bytes before assigning the version so a
            # retrieve→lineage race cannot stamp a predecessor's chunk as the
            # newly-current version.
            if (
                len(document_chunks) != 1
                or self._lineage_content_hash(document_chunks[0]) != current.content_hash
            ):
                raise AnswerLedgerEvidenceUnavailableError(
                    f"served content does not match document version for {document_id}"
                )
            versions[document_id] = current.version_seq
        return tuple(
            CorpusChunkStamp(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                content_hash=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                document_version=versions[chunk.document_id],
                authority_stamp=authority_stamp_from_chunk(chunk),
            )
            for chunk in chunks
        )

    @staticmethod
    def _prompt(question: str, chunks: Sequence[Chunk]) -> str:
        evidence = "\n\n".join(
            f"[chunk {chunk.chunk_id} | document {chunk.document_id}]\n{chunk.text}"
            for chunk in chunks
        )
        return f"Question: {question}\n\nServed evidence:\n{evidence}"

    @staticmethod
    def _lineage_content_hash(chunk: Chunk) -> str:
        content = chunk.text
        try:
            payload = json.loads(chunk.text)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("body"), str):
            content = payload["body"]
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
