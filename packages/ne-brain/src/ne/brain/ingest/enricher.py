# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Optional contextual chunk enrichment (NE-020). Off by default.

Implements ``ne.brain.ingest.contracts.Enricher``. The Anthropic
contextual-retrieval pattern: for each chunk, ask a model to situate it within
the wider document, then prepend that context to the chunk text before it is
embedded/indexed. This is the ONLY stage that calls a model, and it does so
exclusively through the injected :class:`ne.contracts.models.ModelGateway`
PROTOCOL (C3 model independence) — this module imports no provider SDK and no
``ne.gateway`` concrete implementation.

Quarantine/resume semantics belong to NE-026's pipeline spine, not here:
gateway errors from :meth:`ContextualEnricher.enrich` PROPAGATE (raise).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from ne.brain.ingest.contracts import ParsedDocument
from ne.contracts.models import CompletionRequest, ModelGateway
from ne.contracts.types import ChatMessage, Chunk

_SYSTEM_PROMPT = (
    "You situate a chunk within a document for search retrieval. "
    "Answer with 1-2 short sentences of context only."
)


def _user_message(doc_text: str, chunk_text: str) -> str:
    return (
        f"<document>\n{doc_text}\n</document>\n\n"
        f"<chunk>\n{chunk_text}\n</chunk>\n\n"
        "Give a short context to situate this chunk within the overall document."
    )


class ContextualEnricher:
    """Contextual enrichment stage. Implements ``ne.brain.ingest.contracts.Enricher``."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        enabled: bool = False,
        max_concurrency: int = 4,
        max_context_chars: int = 8000,
    ) -> None:
        self._gateway = gateway
        self._enabled = enabled
        self._max_concurrency = max_concurrency
        self._max_context_chars = max_context_chars

    async def enrich(self, doc: ParsedDocument, chunks: Sequence[Chunk]) -> tuple[Chunk, ...]:
        if not self._enabled:
            return tuple(chunks)

        doc_text = "\n\n".join(block.text for block in doc.blocks)[: self._max_context_chars]
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def _enrich_one(chunk: Chunk) -> Chunk:
            async with semaphore:
                request = CompletionRequest(
                    messages=(
                        ChatMessage(role="system", content=_SYSTEM_PROMPT),
                        ChatMessage(role="user", content=_user_message(doc_text, chunk.text)),
                    ),
                    task="ingest.enrich",
                )
                completion = await self._gateway.complete(request)
            context = completion.text.strip()
            metadata = dict(chunk.metadata)
            metadata.update(
                {
                    "enriched": True,
                    "enrichment_model": completion.model_id,
                    "enrichment_context": context,
                }
            )
            return Chunk(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                text=f"{context}\n\n{chunk.text}",
                metadata=metadata,
                acl=chunk.acl,
            )

        results = await asyncio.gather(*(_enrich_one(chunk) for chunk in chunks))
        return tuple(results)
