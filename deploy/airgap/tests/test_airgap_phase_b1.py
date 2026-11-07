# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-073 AIR_GAP phase B1 — qdrant + tei-embed served fully offline inside
the sealed namespace, with a re-assertion that raw-IP egress is still dead
while the brain profile serves real requests.

NE-TEI-ONNX also adds ``test_tei_embed_parity_smoke``: proves the LIVE,
sealed tei-embed server's /embed vectors match the committed golden vectors
(the safetensors-reference embeddings, produced by ``tools/model-export``)
within a cosine headroom — the serving-time half of the vector-parity truth
gate (the build-time half is ``export_env/export_and_gate.py``'s cosine
>= 0.9999 gate). ANTI-VACUOUS (§0-law-5): this test FAILS (never skips) if
selected while the golden vectors file is unpublished — a workflow-level
``--deselect`` with an echoed reason is the ONLY sanctioned way to skip it
(see .github/workflows/no-egress.yml); ``run-airgap-pytest.sh`` hard-fails on
any 'skipped' output, so a ``pytest.skip`` here would silently defeat that
gate.
"""

from __future__ import annotations

import json
import math
import socket

import httpx
import pytest

from .conftest import AIRGAP_ACTIVE, REPO_ROOT

if not AIRGAP_ACTIVE:
    pytest.skip(
        "AIR_GAP harness only runs inside the sealed netns (no-egress.yml)",
        allow_module_level=True,
    )

_GOLDEN_VECTORS_PATH = REPO_ROOT / "tools" / "model-export" / "golden" / "qwen3-embedding-0.6b.json"


def _cosine(a: list[float], b: list[float]) -> float:
    """Pure-python cosine similarity — httpx + math only, no heavy deps."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise ValueError("zero-norm vector — cannot compute cosine similarity")
    return dot / (norm_a * norm_b)


def test_qdrant_healthz() -> None:
    response = httpx.get("http://127.0.0.1:6333/healthz", timeout=10)
    assert response.status_code == 200


def test_tei_embed_returns_vector() -> None:
    response = httpx.post(
        "http://127.0.0.1:8085/embed",
        json={"inputs": "air gap probe"},
        timeout=30,
    )
    assert response.status_code == 200
    body = response.json()
    # TEI /embed returns a list of embedding vectors (one per input); a single
    # string input yields a single non-empty vector of floats.
    assert body
    vector = body[0] if isinstance(body, list) else body
    assert isinstance(vector, list)
    assert len(vector) > 0
    assert all(isinstance(x, float) for x in vector)


def test_raw_ip_egress_still_dead_during_brain_serving() -> None:
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=5)


def test_tei_embed_parity_smoke() -> None:
    """The 16-golden-text /embed parity smoke (design §5 serving-time layer).

    ANTI-VACUOUS: FAILS (never skips) when the golden vectors are absent — the
    workflow deselects this test WITH A REASON while the artifact is
    unpublished (see no-egress.yml); a bare pytest.skip here would be
    silently accepted and defeat the whole point of this guardian.
    """
    if not _GOLDEN_VECTORS_PATH.is_file():
        pytest.fail(
            f"golden vectors unpublished at {_GOLDEN_VECTORS_PATH} — this test must be "
            "workflow-DESELECTED with an echoed reason (see no-egress.yml's Phase B1 step), "
            "never silently skipped: a pytest.skip here would defeat run-airgap-pytest.sh's "
            "fail-on-skip gate (NE-TEI-ONNX §0-law-5 anti-vacuous truth gate)."
        )

    goldens = json.loads(_GOLDEN_VECTORS_PATH.read_text(encoding="utf-8"))
    texts: list[str] = goldens["texts"]
    vectors: list[list[float]] = goldens["vectors"]
    dim: int = goldens["dim"]
    assert len(texts) == 16
    assert len(vectors) == 16

    response = httpx.post(
        "http://127.0.0.1:8085/embed",
        json={"inputs": texts},
        timeout=120,
    )
    assert response.status_code == 200
    served = response.json()
    assert isinstance(served, list)
    assert len(served) == len(texts)

    for i, (served_vector, golden_vector) in enumerate(zip(served, vectors, strict=True)):
        assert len(served_vector) == dim, f"text[{i}]: dim {len(served_vector)} != {dim}"
        similarity = _cosine(served_vector, golden_vector)
        assert similarity >= 0.999, f"text[{i}]: cosine {similarity!r} below the 0.999 threshold"
