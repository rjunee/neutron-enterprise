# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TEIEmbedder LIVE acceptance test against a real TEI server.

Skipped (LIVE-PARKED) when NE_TEI_EMBED_URL is unreachable. To run for real:

    ne dev up
    NE_TEI_EMBED_URL=http://localhost:8085 uv run pytest packages/ne-brain -q
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence

import httpx
import pytest

from ne.brain.embedder import TEIEmbedder


def _tei_url() -> str:
    return os.environ.get("NE_TEI_EMBED_URL", "http://localhost:8085")


def _tei_reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


_URL = _tei_url()

if not _tei_reachable(_URL):
    pytest.skip(
        "LIVE-PARKED: real-TEI embedding pending — start `ne dev up` and "
        "re-run `NE_TEI_EMBED_URL=http://localhost:8085 uv run pytest packages/ne-brain -q` "
        f"(NE_TEI_EMBED_URL={_URL} unreachable)",
        allow_module_level=True,
    )


def test_embed_returns_1024_dim_vector() -> None:
    embedder = TEIEmbedder(_URL)

    async def scenario() -> Sequence[Sequence[float]]:
        vectors = await embedder.embed(["hello world"])
        await embedder.aclose()
        return vectors

    vectors = asyncio.run(scenario())
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024
