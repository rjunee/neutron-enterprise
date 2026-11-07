# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-073 AIR_GAP phase B2 — tei-rerank served fully offline inside the
sealed namespace, with a re-assertion that raw-IP egress is still dead."""

from __future__ import annotations

import socket

import httpx
import pytest

from .conftest import AIRGAP_ACTIVE

if not AIRGAP_ACTIVE:
    pytest.skip(
        "AIR_GAP harness only runs inside the sealed netns (no-egress.yml)",
        allow_module_level=True,
    )


def test_tei_rerank_returns_scores() -> None:
    response = httpx.post(
        "http://127.0.0.1:8086/rerank",
        json={"query": "q", "texts": ["a", "b"]},
        timeout=30,
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 2
    for entry in body:
        assert "score" in entry
        assert isinstance(entry["score"], float)


def test_raw_ip_egress_still_dead_during_rerank_serving() -> None:
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=5)
