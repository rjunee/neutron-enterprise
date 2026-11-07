# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ServeSettings: defaults, env override, and the no-``/v1``-suffix invariant."""

from __future__ import annotations

from pathlib import Path

import pytest

from ne.serve.config import ServeSettings


def test_defaults() -> None:
    settings = ServeSettings()
    assert settings.base_url == "http://localhost:8090"
    assert settings.model == "qwen3.6-35b-a3b"
    assert settings.timeout_s == 120.0
    assert settings.weights_root == Path("models")


def test_base_url_has_no_v1_suffix() -> None:
    # /health lives at the root, so the configured base_url must not carry /v1.
    assert not ServeSettings().base_url.rstrip("/").endswith("/v1")


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_SERVE_BASE_URL", "http://vllm.internal:8000")
    monkeypatch.setenv("NE_SERVE_MODEL", "glm-5.2")
    settings = ServeSettings()
    assert settings.base_url == "http://vllm.internal:8000"
    assert settings.model == "glm-5.2"
