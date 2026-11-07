# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""LightRAG custom-KG projector — runs INSIDE the isolated lightrag sidecar venv.

Reads an exported-graph JSON ``{entities, relationships, chunks}`` (produced by
``ne.brain_graph.lightrag.projector.LightRagProjector``) and inserts it into local
file-based LightRAG storages under ``--workdir`` via ``ainsert_custom_kg``. The KG
is rebuildable from scratch (``ne-brain-graph rebuild-lightrag``).

Governance (provable in this script):
* ``embedding_func`` is a DETERMINISTIC local hash-embedding — ZERO network (C3).
* ``llm_model_func`` RAISES — insertion of a *custom* KG needs no LLM, so it is
  never invoked; any code path that reaches it crashes (the ban is provable).
* No API server is ever started (the bundled server is BANNED — CVEs; see README).

Usage (driven by ``ne.brain_graph.lightrag._lightrag.LightRagSidecar`` via
subprocess)::

    python project.py --input kg.json --workdir <dir>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

_EMBED_DIM = 256


def _hash_embed_one(text: str) -> list[float]:
    """A deterministic, network-free unit-ish embedding from a SHA-256 stream."""
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
        "LLM calls are BANNED in the LightRAG sidecar (C3); custom-KG insertion "
        "must never invoke the LLM. This path is unreachable by design."
    )


def _build_rag(workdir: Path) -> Any:
    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc

    return LightRAG(
        working_dir=str(workdir),
        embedding_func=EmbeddingFunc(embedding_dim=_EMBED_DIM, max_token_size=8192, func=_embed),
        llm_model_func=_llm_banned,
    )


async def _run(input_path: Path, workdir: Path) -> int:
    from lightrag.kg.shared_storage import initialize_pipeline_status

    workdir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    custom_kg: dict[str, Any] = {
        "chunks": payload.get("chunks", []),
        "entities": payload.get("entities", []),
        "relationships": payload.get("relationships", []),
    }
    rag = _build_rag(workdir)
    await rag.initialize_storages()
    await initialize_pipeline_status()
    try:
        await rag.ainsert_custom_kg(custom_kg)
    finally:
        await rag.finalize_storages()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args(argv)
    return asyncio.run(_run(Path(args.input), Path(args.workdir)))


if __name__ == "__main__":
    raise SystemExit(main())
