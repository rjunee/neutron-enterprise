# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""LightRAG candidate query — runs INSIDE the isolated lightrag sidecar venv.

Returns ``{"entity_keys": [...], "chunk_ids": [...]}`` — candidate node keys +
chunk ``source_id``s for a natural-language query, obtained WITHOUT any LLM call.

Governance (provable in this script):
* ``embedding_func`` is the SAME deterministic local hash-embedding as project.py
  — retrieval is pure vector similarity, ZERO network (C3).
* ``llm_model_func`` RAISES. Keyword extraction (the only step that would call the
  LLM in ``local`` mode) is BYPASSED by pre-supplying ``ll_keywords``/``hl_keywords``
  from the query tokens, and ``only_need_context=True`` skips answer generation —
  so the LLM is never invoked (the ban is provable).
* The results are CANDIDATES only. The platform re-validates every key/chunk id
  against the principal-scoped ``EntityGraph`` + T1 retriever (trim-before-return,
  C7) before anything is used — this script is never trusted.

Usage (driven by ``ne.brain_graph.lightrag._lightrag.LightRagSidecar`` via
subprocess)::

    python query.py --query "..." --workdir <dir> --top-k N --output out.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_EMBED_DIM = 256


def _hash_embed_one(text: str) -> list[float]:
    out: list[float] = []
    counter = 0
    while len(out) < _EMBED_DIM:
        digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        for i in range(0, len(digest), 2):
            if len(out) >= _EMBED_DIM:
                break
            word = digest[i] << 8 | digest[i + 1]
            out.append((word / 65535.0) * 2.0 - 1.0)
        counter += 1
    return out


async def _embed(texts: list[str]) -> Any:
    import numpy as np

    return np.array([_hash_embed_one(t) for t in texts], dtype=np.float32)


async def _llm_banned(*args: Any, **kwargs: Any) -> str:
    raise RuntimeError(
        "LLM calls are BANNED in the LightRAG sidecar (C3). Queries run "
        "only_need_context=True with pre-supplied keywords; this path is "
        "unreachable by design."
    )


def _build_rag(workdir: Path) -> Any:
    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc

    return LightRAG(
        working_dir=str(workdir),
        embedding_func=EmbeddingFunc(embedding_dim=_EMBED_DIM, max_token_size=8192, func=_embed),
        llm_model_func=_llm_banned,
    )


def _tokens(query: str) -> list[str]:
    return [t for t in re.split(r"\W+", query.lower()) if t]


async def _run(query: str, workdir: Path, top_k: int, output_path: Path) -> int:
    from lightrag import QueryParam

    rag = _build_rag(workdir)
    await rag.initialize_storages()
    try:
        keywords = _tokens(query)
        param = QueryParam(
            mode="local",
            only_need_context=True,
            top_k=top_k,
            ll_keywords=keywords,
            hl_keywords=keywords,
        )
        context = await rag.aquery(query, param=param)
        entity_keys, chunk_ids = _parse_context(str(context))
    finally:
        await rag.finalize_storages()

    output_path.write_text(
        json.dumps({"entity_keys": entity_keys, "chunk_ids": chunk_ids}),
        encoding="utf-8",
    )
    return 0


def _parse_context(context: str) -> tuple[list[str], list[str]]:
    """Extract candidate entity names + chunk source_ids from a context blob.

    ``only_need_context=True`` returns entity/relation/chunk tables. We pull the
    entity ``entity`` names (our node keys) and any ``source_id``-shaped tokens;
    parsing is best-effort — the platform re-validates every candidate anyway.
    """
    entities: list[str] = []
    chunks: list[str] = []
    for match in re.finditer(r'"entity"\s*:\s*"([^"]+)"', context):
        entities.append(match.group(1))
    for match in re.finditer(r'"source_id"\s*:\s*"([^"]+)"', context):
        for part in match.group(1).split("<SEP>"):
            part = part.strip()
            if part:
                chunks.append(part)
    # De-dup, preserve order.
    return list(dict.fromkeys(entities)), list(dict.fromkeys(chunks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.query, Path(args.workdir), args.top_k, Path(args.output)))


if __name__ == "__main__":
    raise SystemExit(main())
