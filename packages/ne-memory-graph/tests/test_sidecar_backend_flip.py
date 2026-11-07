# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Always-on pins for the NE-083c backend flip in ``graphiti_env/service.py``.

``_resolve_backend`` default/age/garbage matrix; the age branch fails loud without
``NE_MEMORY_GRAPH_AGE_DSN``; and ``service.py`` still loads platform-side (module-level
imports stdlib + fastapi only — no graphiti_core / graphiti_age at import time).
"""

from __future__ import annotations

import pytest

from ._sidecar_loader import load_sidecar_module


def _service():
    return load_sidecar_module("service.py", "_ne083c_service_flip")


def test_resolve_backend_default_is_neo4j() -> None:
    svc = _service()
    assert svc._resolve_backend(None) == "neo4j"
    assert svc._resolve_backend("") == "neo4j"
    assert svc._resolve_backend("neo4j") == "neo4j"
    assert svc._resolve_backend("  Neo4j ") == "neo4j"


def test_resolve_backend_age() -> None:
    svc = _service()
    assert svc._resolve_backend("age") == "age"
    assert svc._resolve_backend("AGE") == "age"


def test_resolve_backend_garbage_raises_with_env_var_name() -> None:
    svc = _service()
    with pytest.raises(RuntimeError) as exc:
        svc._resolve_backend("falkordb")
    assert "NE_MEMORY_GRAPH_BACKEND" in str(exc.value)


def test_age_branch_without_dsn_raises_naming_env(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()
    monkeypatch.setenv("NE_MEMORY_GRAPH_BACKEND", "age")
    monkeypatch.delenv("NE_MEMORY_GRAPH_AGE_DSN", raising=False)
    with pytest.raises(RuntimeError) as exc:
        svc._build_driver()
    assert "NE_MEMORY_GRAPH_AGE_DSN" in str(exc.value)


def test_service_module_loads_platform_side() -> None:
    # Loading the module executes only stdlib + fastapi imports; graphiti_core and
    # graphiti_age are imported lazily inside _build_driver / _build_graphiti.
    svc = _service()
    assert hasattr(svc, "app")
    assert hasattr(svc, "_build_driver")
