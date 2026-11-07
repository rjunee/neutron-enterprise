# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""RerankerContractKit over TEIReranker with a deterministic token-overlap
MockTransport scorer. Always runs in CI (no network)."""

from __future__ import annotations

import httpx

from ne.brain.reranker import TEIReranker
from ne.brain.testing.kits.reranker import RerankerContractKit
from ne.contracts.knowledge import Reranker


async def _handler(request: httpx.Request) -> httpx.Response:
    import json

    body = json.loads(request.read())
    qtokens = set(body["query"].lower().split())
    entries = [
        {"index": i, "score": len(qtokens & set(text.lower().split())) / max(len(qtokens), 1)}
        for i, text in enumerate(body["texts"])
    ]
    return httpx.Response(200, json=entries)


class TestTEIRerankerConformance(RerankerContractKit):
    def create_reranker(self) -> Reranker:
        client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        return TEIReranker("http://tei.local", client=client)
